# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible /v1/chat/completions and /v1/models endpoints.

Tokens flow: Worker → WebSocket → Redis Pub/Sub → SSE → Client
The only difference from the Anthropic endpoint is the JSON envelope.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Optional
from uuid import uuid4

# DoS guard: cap total request size BEFORE sanitize-regex + tiktoken + Redis push, so a
# multi-MB prompt can't amplify CPU/memory (sec audit M3). Generous but bounded.
MAX_REQUEST_CHARS = int(os.getenv("MAX_REQUEST_CHARS", "200000"))   # ~50k tokens
MAX_REQUEST_MESSAGES = int(os.getenv("MAX_REQUEST_MESSAGES", "500"))
# Per-account in-flight cap on concurrent TEXT requests (each holds a worker slot
# + a token-stream subscription for the life of the stream). Generous by default
# — text is light; this only fences a runaway flood. Fail-open on Redis error.
TEXT_CONCURRENCY = int(os.getenv("GRID_TEXT_CONCURRENCY", "24"))
# When a client omits max_tokens we must still send SOME cap to the backend (and
# reserve against it). Default high so responses aren't artificially truncated —
# the model stops at EOS well before this on normal turns. Pydantic caps input at
# 32768 (le); keep this in step. Tunable via env.
DEFAULT_MAX_TOKENS = int(os.getenv("GRID_DEFAULT_MAX_TOKENS", "32768"))

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..ratelimit import limiter

from ..auth import extract_api_key

from .. import format as fmt
from ..models.openai import ChatCompletionRequest, ModelInfo, ModelListResponse
from ..services import accounts as accounts_svc
from ..services import concurrency
from ..services import credits, den, job_queue, media, quota, recipes, token_stream
from ..services.sanitizer import sanitize_messages
from .worker_ws import get_available_models
from ..services import router as router_svc
from .worker_ws import get_model_modalities

logger = logging.getLogger("grid_api.openai")


async def _observe_dry(user, model, prompt_tokens, completion_tokens, job_id):
    """Dry-run observability ONLY (GRID_CHARGING_ENABLED=0): log the would-charge
    against grid-counted usage so we can watch pricing on real traffic.

    LIVE settlement is NOT done here — it's durable and authoritative in the
    worker-WS handler (credits.settle_job), which reaches a terminal state for
    every job whether or not the client stayed connected. Doing it here too would
    double-settle and depend on the client staying connected. Never breaks a
    response (already sent), so errors are swallowed."""
    if credits.CHARGING_ENABLED:
        return
    try:
        await credits.charge_request(
            user, model, int(prompt_tokens or 0), int(completion_tokens or 0), job_id
        )
    except Exception:
        logger.debug("dry-run observe failed (non-fatal)", exc_info=True)

router = APIRouter()


def _content_to_text(content) -> str:
    """Flatten a message's content to plain text.

    Content may be a string, None (tool-only assistant turn), or a list of
    multimodal parts; we keep only the text so the legacy `prompt` string and
    the ledger prompt-hash stay well-defined. The faithful structured request
    (with images, tools, roles intact) is carried separately in payload.request.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return str(content)


def _messages_to_prompt(messages: list[dict]) -> str:
    """Convert OpenAI chat messages to a single prompt string.

    Used ONLY for legacy bookkeeping, the ledger prompt-hash, and as a fallback
    payload for pre-passthrough workers — never as the primary request anymore.
    """
    parts = []
    for msg in messages:
        role = msg.get("role")
        text = _content_to_text(msg.get("content"))
        if role in ("system", "developer"):
            parts.append(f"{text}\n")
        elif role == "user":
            parts.append(f"User: {text}\n")
        elif role == "assistant":
            parts.append(f"Assistant: {text}\n")
        elif role == "tool":
            parts.append(f"Tool: {text}\n")
    parts.append("Assistant:")
    return "".join(parts)


@router.post("/v1/chat/completions")
@limiter.limit("30/minute")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
    x_grid_user_token: Optional[str] = Header(None),
):
    """OpenAI-compatible chat completions with real streaming."""
    try:
        key = extract_api_key(apikey, authorization)
        return await _handle_chat_completions(
            body, key, x_grid_user_assertion, x_grid_user_token,
        )
    except HTTPException:
        raise
    except Exception as e:
        # Log the full detail server-side; return a generic message so we
        # never leak internal paths / SQL / stack details to the public.
        logger.error(f"chat_completions error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")


async def _detect_media_model(model: str) -> Optional[str]:
    """Return 'image'/'video' if `model` is a media model, else None.

    The recipe's jobType is AUTHORITATIVE for kind: a media worker may advertise the
    same model under both image+video job-types, so the worker list can't tell us
    which a model actually is (LTX-2.3 is video-only but shows in both). Fall back to
    the worker-advertised job-type only for non-recipe (legacy) models."""
    r = recipes.get_recipe(model)
    if r is not None and r.job_type in ("image", "video"):
        return r.job_type
    if model in await get_available_models(job_type="image"):
        return "image"
    if model in await get_available_models(job_type="video"):
        return "video"
    return None


def _last_user_prompt(messages: list) -> str:
    """Extract the most recent user message as a plain-text prompt."""
    for m in reversed(messages):
        if m.role == "user":
            text = _content_to_text(m.content)
            if text.strip():
                return text
    # Fall back to whatever text we can find.
    return " ".join(_content_to_text(m.content) for m in messages).strip()


def _last_user_image(messages: list) -> Optional[str]:
    """Most recent INLINE (data: URI) image in a user turn — the img2img/img2video
    source frame. http(s) URLs are ignored on purpose (SSRF: inline base64 only)."""
    for m in reversed(messages):
        if m.role != "user" or not isinstance(m.content, list):
            continue
        for part in m.content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:"):
                    return url
    return None


async def _chat_media(request: ChatCompletionRequest, kind: str, account_id=None, user=None):
    """Image/video generation abstracted behind /v1/chat/completions.

    When the requested model is a media model, the latest user turn is the
    prompt; we run a media job and return the result inside the assistant
    message as markdown (renders in any chat UI) plus a structured `images`
    array (OpenRouter-style) for programmatic clients. The dedicated
    /v1/images|videos endpoints remain for advanced control (size, seed, n…).
    """
    model = request.model
    prompt = _last_user_prompt(request.messages)

    # img2img / img2video from a pasted image in the turn. Chat has no `size`, so a
    # source frame always auto-matches the output dims to it. An image-only turn
    # (no text) is valid for img2video.
    source_image = _last_user_image(request.messages)
    if not prompt and not source_image:
        raise HTTPException(status_code=400, detail="No prompt or image found in messages.")

    recipe_inputs: dict = {}
    source_image_url = None
    if source_image:
        recipe_inputs, source_image_url = await media.prepare_source_image(
            model, source_image, size_was_set=False)

    steps, cfg_scale, sampler = media.diffusion_params(model, {})
    seed = media.normalize_seed(request.seed)
    seeds = media.seeds_for_outputs(seed, 1)
    if kind == "video":
        width, height = int(recipe_inputs.get("width", 768)), int(recipe_inputs.get("height", 512))
        frames, effective_seconds = media.normalize_video_timing(4.0, 24)
        recipe_inputs.update({"width": width, "height": height, "seconds": effective_seconds, "fps": 24, "frames": frames})
        payload = {
            "prompt": prompt, "n": 1, "width": width, "height": height,
            "frames": frames, "fps": 24, "length": frames, "video_length": frames,
            "steps": steps, "sampler_name": sampler, "cfg_scale": cfg_scale,
            "ext": "mp4", "seed": seed, "seeds": seeds,
        }
        timeout = media.VIDEO_TIMEOUT
    else:
        width, height = int(recipe_inputs.get("width", 1024)), int(recipe_inputs.get("height", 1024))
        recipe_inputs.update({"width": width, "height": height})
        payload = {
            "prompt": prompt, "n": 1, "width": width, "height": height,
            "steps": steps, "sampler_name": sampler, "cfg_scale": cfg_scale,
            "ext": "webp", "seed": seed, "seeds": seeds,
        }
        timeout = media.IMAGE_TIMEOUT
    if recipe_inputs:
        payload["recipe_inputs"] = recipe_inputs
    if source_image_url:
        payload["source_image_url"] = source_image_url
    outputs, _meta = await media.submit_and_wait(model, kind, payload, timeout,
                                          account_id=account_id, concurrency_limit=media.MEDIA_CONCURRENCY,
                                          billing_user=user)

    urls = [o["url"] for o in outputs if o.get("url")]
    if kind == "video":
        markdown = "\n".join(f"[video]({u})" for u in urls)
        images = []
        videos = [{"type": "video_url", "video_url": {"url": u}} for u in urls]
    else:
        markdown = "\n".join(f"![{prompt}]({u})" for u in urls)
        images = [{"type": "image_url", "image_url": {"url": u}} for u in urls]
        videos = []

    completion_id = fmt._gen_id()

    if request.stream:
        async def _gen():
            yield f"data: {json.dumps(fmt.openai_chunk('', model, completion_id, is_first=True))}\n\n"
            yield f"data: {json.dumps(fmt.openai_chunk(markdown, model, completion_id))}\n\n"
            yield f"data: {json.dumps(fmt.openai_chunk_raw({}, model, completion_id, finish_reason='stop'))}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    message = {"role": "assistant", "content": markdown}
    if images:
        message["images"] = images
    if videos:
        message["videos"] = videos
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _assert_request_size(messages: list) -> None:
    """Reject oversized requests before any CPU-heavy processing (sanitize/tokenize)."""
    if len(messages) > MAX_REQUEST_MESSAGES:
        raise HTTPException(status_code=413, detail=f"too many messages (max {MAX_REQUEST_MESSAGES})")
    total = sum(len(_content_to_text(m.content)) for m in messages)
    if total > MAX_REQUEST_CHARS:
        raise HTTPException(status_code=413,
                            detail=f"request too large ({total} chars; max {MAX_REQUEST_CHARS})")


async def _handle_chat_completions(request: ChatCompletionRequest, apikey: str,
                                   user_assertion: str | None = None,
                                   user_token: str | None = None):
    # Auth — v2 account keys first, legacy Haidra keys as fallback.
    user = await accounts_svc.authenticate(
        apikey, user_assertion, user_token=user_token,
        required_scope="inference.submit",
    )
    _assert_request_size(request.messages)

    # Media abstraction: if the requested model is an image/video model, run a
    # media job and return the asset in the assistant message. Keeps a single
    # /v1/chat/completions surface for "give me a picture of…" while the
    # dedicated /v1/images|videos endpoints stay for advanced control.
    media_kind = await _detect_media_model(request.model)
    if media_kind:
        await quota.check_and_consume(dict(user))
        return await _chat_media(
            request, media_kind, account_id=user.get("account_id"), user=user,
        )

    # Check for available text workers serving the OpenAI chat-completions API.
    available = await get_available_models(job_type="text", api_format="openai-chat")
    if not available:
        raise HTTPException(
            status_code=503,
            detail="No compatible text workers are currently online.",
        )

    # Resolve the virtual "auto" model to a concrete ONLINE model via the router
    # (heuristic gate + curated tier map). Only "auto*" is substituted; a real
    # model name is never silently swapped. routing_meta rides the `grid` block.
    routing_meta = None
    if request.model in router_svc.AUTO_MODELS:
        route_text = _messages_to_prompt([m.model_dump(exclude_none=True) for m in request.messages])
        request.model, routing_meta = await router_svc.resolve_auto_async(request.model, route_text, available)

    # Resolve model. Never silently substitute — a client asking for
    # llama-70b must not receive output from whatever random model happens
    # to be online, labeled as the one they asked for. Return a clear
    # model-not-available error listing what IS online.
    model = request.model
    if model not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{request.model}' is not available. Online models: {available}",
        )

    # Step 3: for auto-routed requests, steer to the best-scoring WORKER replica
    # of the resolved model (routes around a flaky replica). Soft affinity — if
    # the preferred worker isn't free the job still runs elsewhere. No-op when a
    # model has ≤1 online worker.
    preferred_worker = ""
    if routing_meta:
        try:
            from .stats import _active_workers
            preferred_worker, _wpick = router_svc.pick_worker(model, await _active_workers())
            if _wpick:
                routing_meta["worker_pick"] = _wpick
        except Exception as e:
            logger.warning(f"worker pick failed: {e}")

    # Free-tier daily quota. Checked here (after auth + worker availability)
    # so a user only spends quota on a request that's actually going to queue.
    await quota.check_and_consume(dict(user))

    # Sanitize messages — strip credentials before they reach workers.
    # We can read+scrub here because this is the OBSERVE-mode path (the grid is
    # a transparent-but-watching proxy). The future TEE/confidential path will
    # be a blind relay where this step moves client-side.
    clean_messages, was_redacted, redacted_types = sanitize_messages(
        [m.model_dump(exclude_none=True) for m in request.messages]
    )

    # Flattened prompt — legacy bookkeeping, ledger prompt-hash, and the
    # fallback payload for pre-passthrough workers.
    prompt = _messages_to_prompt(clean_messages)

    # Seed: grid-side randomization. If the caller didn't pin a seed, mint a
    # fresh one here so output varies per request regardless of the backend
    # engine's default RNG behavior (don't rely on each heterogeneous worker's
    # engine to randomize) — and echo it back for reproducibility. A
    # client-supplied seed is always honored. Mirrors the media path.
    if request.seed is None:
        request.seed = secrets.randbelow(2**53)

    # Normalize max_tokens ONCE so the reservation, the request body, and the
    # worker cap all agree. A client can send `null` (Optional); default it HIGH
    # (DEFAULT_MAX_TOKENS) so omitting it doesn't truncate the response — not the
    # old 4096, which was silently capping chat replies.
    request.max_tokens = request.max_tokens or DEFAULT_MAX_TOKENS

    # Faithful request: forward the developer's request as-is (tools,
    # tool_choice, multimodal content, seed, response_format, any extra params)
    # with only the sanitized messages swapped in. The worker overrides `model`
    # to its backend's name and forces streaming; everything else passes through
    # untouched so a model behaves on the grid exactly as it does locally.
    request_body = request.model_dump(exclude_none=True)
    request_body["messages"] = clean_messages

    # Create job
    job_id = str(uuid4())
    payload = {
        "request": request_body,
        "api_format": "openai-chat",
        # Legacy/fallback fields (also read by the den/context caps).
        "prompt": prompt,
        "max_length": request.max_tokens or DEFAULT_MAX_TOKENS,
        "temperature": request.temperature,
        "top_p": request.top_p,
    }

    # ── Billing gate: RESERVE before dispatch (live mode only) ──────────────
    # Fail CLOSED: paid work is never queued unless funds are held first. In
    # dry-run this is a no-op; the collectors only log would-charge observations.
    # Streaming is covered because this runs before the stream starts, i.e.
    # before the first token leaves the server.
    # Grid-side prompt count (tiktoken) — the ONLY prompt-token figure we bill on.
    # A worker could under/over-report `usage.prompt_tokens`; we never trust it for
    # money. Computed once so the reservation and the final settlement agree.
    prompt_toks = den.count_tokens(prompt)

    # Per-account in-flight cap BEFORE reserving/dispatching — a flooding account
    # is 429'd without holding credits or a worker slot. Fail-open (Redis blip →
    # allowed). Released in the finally below (collect/error) or, for a stream,
    # handed to the generator's finally (which fires on finish AND disconnect).
    aid = (user or {}).get("account_id")
    if aid and not await concurrency.acquire(aid, "text", TEXT_CONCURRENCY):
        raise HTTPException(status_code=429,
                            detail=f"Too many concurrent requests (limit {TEXT_CONCURRENCY}). Retry shortly.")
    inflight_held = bool(aid)
    try:
        if credits.CHARGING_ENABLED:
            auth = await credits.authorize_request(
                user, model, prompt_toks, request.max_tokens, job_id,
                record_reservation=True,
            )
            if not auth["ok"]:
                raise HTTPException(status_code=402, detail=auth.get("reason", "payment required"))

        # Submit to the Grid Redis Stream for workers.
        # If dispatch itself fails the job never runs, so the held reservation must be
        # released — otherwise funds are stranded with no settlement path.
        try:
            await job_queue.submit_job(job_id, payload, [model], preferred_worker=preferred_worker)
        except Exception:
            await credits.release_job(job_id)
            raise

        completion_id = fmt._gen_id()

        if request.stream:
            resp = StreamingResponse(
                _stream_openai(job_id, model, completion_id, user, request.seed, prompt_toks,
                               routing_meta, inflight_account=aid if inflight_held else None),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
            inflight_held = False  # ownership transferred to the generator's finally
            return resp
        else:
            # Awaited → the handler stays alive through it; the finally releases.
            return await _collect_response(job_id, model, user, request.seed, prompt_toks, routing_meta)
    finally:
        if inflight_held:
            await concurrency.release(aid, "text")


async def _stream_openai(job_id: str, model: str, completion_id: str, user: dict | None = None, seed: int | None = None, prompt_toks: int = 0, routing_meta: dict | None = None, inflight_account=None):
    """SSE generator for OpenAI streaming format.

    Faithful passthrough: the grid emits one leading role chunk, then relays
    each backend delta verbatim (content / reasoning_content / tool_calls), and
    finally a finish_reason chunk + an optional usage chunk. The grid stamps
    only id/model/created — it never rewrites the delta payload.

    Billing note: LIVE settlement is durable + authoritative in the worker-WS
    handler (credits.settle_job), independent of whether the client stays
    connected. Here we only feed dry-run OBSERVABILITY, counting completion from
    the text the grid actually relayed (never worker-reported usage).
    """
    # First chunk: role (the grid's single authoritative role delta).
    chunk = fmt.openai_chunk("", model, completion_id, is_first=True)
    yield f"data: {json.dumps(chunk)}\n\n"

    # Grid-side completion accumulator for dry-run observability — count what the
    # grid ACTUALLY relayed (content + reasoning), never worker `usage`.
    relayed = []
    observed = False  # log the dry-run would-charge exactly once
    terminal = False  # did we reach a natural end (done/error) vs. client disconnect?

    try:
        async for data in token_stream.subscribe_tokens(job_id):
            if data.get("text") == token_stream.DONE_SENTINEL:
                # A mid-stream error (bad request, worker/backend failure): surface
                # it as an OpenAI error event so the client sees the real reason
                # instead of a silently-truncated reply.
                err = data.get("error")
                if err:
                    terminal = True
                    yield f"data: {json.dumps({'error': {'message': err, 'type': 'invalid_request_error' if data.get('code') == 400 else 'upstream_error'}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return  # live settlement happens in worker_ws; nothing to observe on error
                # Terminal finish_reason chunk, then optional usage chunk.
                finish = data.get("finish_reason") or "stop"
                yield f"data: {json.dumps(fmt.openai_chunk_raw({}, model, completion_id, finish_reason=finish))}\n\n"
                usage = data.get("usage")
                grid_meta = data.get("grid")
                if seed is not None:
                    grid_meta = {**(grid_meta or {}), "seed": seed}
                if routing_meta:
                    grid_meta = {**(grid_meta or {}), "routing": routing_meta}
                if usage or grid_meta:
                    usage_chunk = fmt.openai_usage_chunk(model, completion_id, usage or {})
                    # Additive provenance on the final chunk (worker, gen_time, ttft,
                    # tokens_per_s) — standard clients ignore the extra `grid` key.
                    if grid_meta:
                        usage_chunk["grid"] = grid_meta
                    yield f"data: {json.dumps(usage_chunk)}\n\n"
                await _observe_dry(user, model, prompt_toks, den.count_tokens("".join(relayed)), job_id)
                observed = True
                break

            delta = data.get("delta")
            if delta is not None:
                # Faithful path — relay the raw backend delta untouched.
                if delta.get("content"):
                    relayed.append(delta["content"])
                if delta.get("reasoning_content"):
                    relayed.append(delta["reasoning_content"])
                chunk = fmt.openai_chunk_raw(delta, model, completion_id, finish_reason=data.get("finish_reason"))
            elif data.get("reasoning"):
                # Legacy worker path — reasoning channel.
                relayed.append(data.get("text", ""))
                chunk = fmt.openai_chunk("", model, completion_id, reasoning=data.get("text", ""))
            else:
                # Legacy worker path — plain content.
                relayed.append(data.get("text", ""))
                chunk = fmt.openai_chunk(data.get("text", ""), model, completion_id)
            yield f"data: {json.dumps(chunk)}\n\n"

        terminal = True
        yield "data: [DONE]\n\n"
    finally:
        # If the generator was torn down before a natural finish, the client
        # disconnected (closed the SSE / pressed stop) — tell the worker to abort
        # so it stops generating instead of running to completion on the GPU.
        if not terminal:
            await token_stream.request_cancel(job_id)
        # Dry-run only: observe the would-charge once even on disconnect/cancel.
        # (LIVE money is settled in worker_ws regardless of this generator.)
        if not observed:
            await _observe_dry(user, model, prompt_toks, den.count_tokens("".join(relayed)), job_id)
        # Release the in-flight slot the handler transferred to us — on natural
        # finish AND on client disconnect (this finally runs in both).
        if inflight_account:
            await concurrency.release(inflight_account, "text")


async def _collect_response(job_id: str, model: str, user: dict | None = None, seed: int | None = None, prompt_toks: int = 0, routing_meta: dict | None = None) -> dict:
    """Collect the stream and return a single non-streaming response.

    The worker always streams; the grid assembles. The DONE event carries the
    already-assembled content / reasoning / tool_calls / usage (assembled once,
    server-side, in worker_ws). We also accumulate from deltas as a fallback so
    a legacy worker that never sends a rich DONE still produces a valid reply.
    """
    content = ""
    reasoning = ""
    tool_calls = None
    usage = None
    finish_reason = "stop"
    grid_meta = None
    terminal = False  # natural finish vs. client disconnect

    try:
        async for data in token_stream.subscribe_tokens(job_id):
            if data.get("text") == token_stream.DONE_SENTINEL:
                err = data.get("error")
                if err:
                    # Surface the real failure with a meaningful status (400 for a
                    # caller fault, 502 for an upstream worker/backend failure).
                    terminal = True
                    raise HTTPException(status_code=data.get("code") or 502, detail=err)
                content = data.get("full_text") or content
                reasoning = data.get("full_reasoning") or reasoning
                tool_calls = data.get("tool_calls") or tool_calls
                usage = data.get("usage") or usage
                finish_reason = data.get("finish_reason") or finish_reason
                grid_meta = data.get("grid") or grid_meta
                terminal = True
                break

            delta = data.get("delta")
            if delta is not None:
                if delta.get("content"):
                    content += delta["content"]
                if delta.get("reasoning_content"):
                    reasoning += delta["reasoning_content"]
            elif data.get("reasoning"):
                reasoning += data.get("text", "")
            else:
                content += data.get("text", "")
    finally:
        # Client gave up on a non-streaming request before we got the result →
        # abort the worker rather than letting it finish on the GPU.
        if not terminal:
            await token_stream.request_cancel(job_id)

    # Grid-counted completion (tiktoken of the content+reasoning the grid actually
    # assembled) — used for dry-run observability and as the display fallback.
    # LIVE money is settled durably in worker_ws (credits.settle_job), never here.
    bill_completion = den.count_tokens(content + reasoning)
    await _observe_dry(user, model, prompt_toks, bill_completion, job_id)

    # Client-facing usage: prefer the worker's report (faithful), fall back to the
    # grid count so the envelope is never zeroed by a silent worker.
    prompt_tokens = (usage or {}).get("prompt_tokens") or prompt_toks
    completion_tokens = (usage or {}).get("completion_tokens") or bill_completion
    resp = fmt.openai_response(
        content,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning=reasoning,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )
    # Additive provenance sibling (worker, gen_time, ttft, tokens_per_s). Standard
    # OpenAI clients ignore unknown top-level fields; UIs that want it read `grid`.
    if seed is not None:
        grid_meta = {**(grid_meta or {}), "seed": seed}
    if routing_meta:
        grid_meta = {**(grid_meta or {}), "routing": routing_meta}
    if grid_meta:
        resp["grid"] = grid_meta
    return resp


@router.get("/v1/models")
async def list_models():
    """List TEXT models available from connected workers.

    This is the OpenAI chat-completions model list (what UI model pickers read),
    so it returns text models only — image/video models are served via the media
    job API, not chat-completions, and must not appear in a chat picker.
    """
    models = await get_available_models(job_type="text")
    modalities = await get_model_modalities()
    data = [
        ModelInfo(
            id=m,
            owned_by="aipowergrid",
            input_modalities=modalities.get(m, ["text"]),
        )
        for m in models
    ]
    # Surface the virtual "auto" model so pickers can offer it — only when at
    # least one text worker is online (else it'd resolve to nothing).
    if models:
        data = [
            ModelInfo(id="auto", owned_by="aipowergrid", input_modalities=["text"])
        ] + data
    return ModelListResponse(data=data)


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """Get info for a specific text model."""
    models = await get_available_models(job_type="text")
    if model_id not in models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return ModelInfo(id=model_id, owned_by="aipowergrid")


def _text_param_schema(model_id: str) -> dict:
    """Param contract for a TEXT model: the validated OpenAI chat knobs plus a note
    that any extra sampler params are forwarded faithfully (the grid is a passthrough
    proxy, so it does not restrict backend-specific samplers)."""
    return {
        "model": model_id,
        "job_type": "text",
        "passthrough": True,
        "params": {
            "temperature": {"type": "number", "minimum": 0, "maximum": 2, "default": 0.7},
            "top_p": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.9},
            "max_tokens": {"type": "integer", "minimum": 1, "maximum": DEFAULT_MAX_TOKENS,
                           "default": DEFAULT_MAX_TOKENS},
            "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
            "stop": {"type": "string|array"},
            "presence_penalty": {"type": "number", "minimum": -2, "maximum": 2},
            "frequency_penalty": {"type": "number", "minimum": -2, "maximum": 2},
            "seed": {"type": "integer"},
            "stream": {"type": "boolean", "default": False},
            "tools": {"type": "array"},
            "tool_choice": {"type": "string|object"},
            "response_format": {"type": "object", "description": "JSON schema / structured output"},
        },
        "passthrough_note": (
            "Extra sampler params (top_k, min_p, top_a, repetition_penalty, typical_p, "
            "mirostat, guided_json/regex, grammar, logit_bias) are forwarded to the "
            "backend untouched."
        ),
    }


@router.get("/v1/models/{model_id}/recipe")
async def get_model_recipe(model_id: str, apikey: Optional[str] = Header(None),
                           authorization: Optional[str] = Header(None)):
    """Worker preflight: the canary-resolved recipe(s) for a media model, plus the
    node types + model files a worker must have to run it. A worker fetches this at
    startup, checks nodes/files against its ComfyUI /object_info, then smoke-runs the
    `spec` — advertising the model only if it produces a real output. Authed (workers
    hold a grid key); recipe graphs aren't secret but we don't serve them anonymously.
    """
    from ..services import accounts as accounts_svc, recipes
    await accounts_svc.authenticate(extract_api_key(apikey, authorization))

    recs = recipes.recipes_for_model(model_id)
    if not recs:
        raise HTTPException(status_code=404, detail=f"No recipe serves model '{model_id}'")
    out = []
    for r in recs:
        canary: dict = {}  # minimum steps for a fast smoke test
        if "steps" in r.vars and "steps" in r.clamps:
            canary["steps"] = r.clamps["steps"][0]
        resolved = recipes.resolve(r.recipe_root, canary)
        out.append({
            "name": r.name,
            "recipe_root": r.recipe_root,
            "job_type": r.job_type,
            "required_models": r.required_models,
            "node_types": recipes.node_types(resolved["spec"]),
            "model_files": recipes.model_files(resolved["spec"]),
            "image_paths": resolved.get("image_paths"),
            "spec": resolved["spec"],
        })
    return {"model": model_id, "recipes": out}


@router.get("/v1/models/{model_id}/params")
async def get_model_params(model_id: str):
    """The adjustable parameter schema for a model — ranges, enums, capabilities.

    Media (image/video) models are recipe-governed: every knob is hard-gated to the
    band shown here (out-of-band values are rejected, not silently clamped), so this
    is the authoritative "what can I send" for a model. Text models report the
    standard OpenAI sampler set plus the faithful-passthrough note. Describes the
    parameter contract, independent of live worker availability.
    """
    from ..services import media, recipes

    sch = recipes.param_schema(model_id)
    if sch is not None:
        jt, p = sch["job_type"], sch["params"]
        # Global media limits not encoded per-recipe.
        p.setdefault("seed", {"type": "integer", "minimum": 0, "maximum": media.MAX_SEED})
        p.setdefault("size", {"type": "size", "minimum": media.MIN_DIM, "maximum": media.MAX_DIM,
                              "multiple_of": 64,
                              "description": "WIDTHxHEIGHT; img2img auto-matches the source if omitted"})
        p["n"] = {"type": "integer", "minimum": 1, "maximum": 2 if jt == "video" else 4}
        if jt == "video":
            p.setdefault("seconds", {"type": "number", "minimum": 1, "maximum": 10})
            p.setdefault("fps", {"type": "integer", "minimum": 8, "maximum": 30})
            p["output_format"] = {"type": "enum", "options": ["mp4"]}
        else:
            p["output_format"] = {"type": "enum", "options": ["png", "jpeg", "webp"]}
        p["response_format"] = {"type": "enum", "options": ["url", "b64_json"]}
        return sch

    if model_id in await get_available_models(job_type="text"):
        return _text_param_schema(model_id)
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
