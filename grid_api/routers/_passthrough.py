# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared plumbing for raw API passthrough endpoints.

The grid does NOT translate between API formats. Each non-OpenAI-chat endpoint
(Anthropic `/v1/messages`, OpenAI `/v1/responses`) routes to the pool of
workers whose backend NATIVELY serves that format. If that pool is empty the
endpoint returns 503 — the grid never fakes a format it can't serve.

A job carries the client's raw request plus an `api_format` tag; the worker
forwards it to the matching backend endpoint and relays the upstream events
verbatim, which we stream straight back.

Billing: even though the BYTES are relayed untouched, the grid still meters
money on its OWN token counts — never the worker/backend-reported `usage`. We
count the prompt server-side from the request (per-format flatten + tiktoken)
and the completion from the text the grid actually relayed (stream deltas) or
assembled (`full_json`). Reserve happens before dispatch; reconcile/refund on
the job's terminal event (and in a `finally` on disconnect) so a reservation is
never stranded and a silent worker can't drive the charge to zero.
"""

import json
import logging
import os
from uuid import uuid4

from fastapi import HTTPException

from ..services import credits, den, job_queue, token_stream
from ..services.sanitizer import sanitize

logger = logging.getLogger("grid_api.passthrough")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# When the client omits the output cap, default it HIGH so responses aren't
# truncated (same knob as the chat path's GRID_DEFAULT_MAX_TOKENS).
DEFAULT_OUTPUT_TOKENS = int(os.getenv("GRID_DEFAULT_MAX_TOKENS", "32768"))
MAX_OUTPUT_TOKENS = 32768


def normalize_output_budget(api_format: str, raw_request: dict) -> int:
    """Normalize the output cap into the raw request before reserve/dispatch."""
    if api_format == "anthropic":
        field = "max_tokens"
        value = raw_request.get(field)
    else:
        field = "max_output_tokens"
        value = raw_request.get(field)
        if value is None:
            value = raw_request.get("max_tokens")

    if value in (None, ""):
        max_len = DEFAULT_OUTPUT_TOKENS
    else:
        try:
            max_len = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"{field} must be an integer")
        if max_len < 1 or max_len > MAX_OUTPUT_TOKENS:
            raise HTTPException(
                status_code=422,
                detail=f"{field} must be between 1 and {MAX_OUTPUT_TOKENS}",
            )

    raw_request[field] = max_len
    return max_len


# Passthrough bodies (Anthropic /v1/messages, /v1/responses) are accepted raw
# and recursively walked. Bound them so a huge or pathologically-nested body
# can't burn CPU/stack in deep_sanitize. (Reuses the OpenAI char budget.)
MAX_PASSTHROUGH_CHARS = int(os.getenv("MAX_PASSTHROUGH_CHARS", os.getenv("MAX_REQUEST_CHARS", "200000")))
MAX_PASSTHROUGH_DEPTH = int(os.getenv("MAX_PASSTHROUGH_DEPTH", "64"))


def _nesting_depth(obj, _d=0):
    if _d > MAX_PASSTHROUGH_DEPTH:
        return _d  # deep enough to reject; stop counting
    if isinstance(obj, dict):
        return max((_nesting_depth(v, _d + 1) for v in obj.values()), default=_d)
    if isinstance(obj, list):
        return max((_nesting_depth(v, _d + 1) for v in obj), default=_d)
    return _d


def guard_passthrough_body(body) -> None:
    """Reject an oversized or too-deeply-nested passthrough body BEFORE the
    recursive sanitize/serialize walks it. Raises HTTPException(413/400)."""
    if not isinstance(body, (dict, list)):
        return
    try:
        size = len(json.dumps(body, default=str))
    except Exception:
        raise HTTPException(status_code=400, detail="unserializable request body")
    if size > MAX_PASSTHROUGH_CHARS:
        raise HTTPException(status_code=413,
                            detail=f"request too large ({size} chars; max {MAX_PASSTHROUGH_CHARS})")
    if _nesting_depth(body) > MAX_PASSTHROUGH_DEPTH:
        raise HTTPException(status_code=400,
                            detail=f"request nesting too deep (max {MAX_PASSTHROUGH_DEPTH})")


def deep_sanitize(obj, _depth=0):
    """Recursively scrub credentials from every string in a request body.

    Format-agnostic: works for Anthropic messages, Responses `input`, tool
    arguments, etc. Structure and keys are preserved; only string *values* are
    passed through the secret sanitizer (which is a no-op on normal text).
    Depth-capped as a defensive backstop (callers should `guard_passthrough_body`
    first): past the cap the subtree is returned unwalked rather than overflowing."""
    if _depth > MAX_PASSTHROUGH_DEPTH:
        return obj
    if isinstance(obj, str):
        return sanitize(obj).text
    if isinstance(obj, list):
        return [deep_sanitize(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {k: deep_sanitize(v, _depth + 1) for k, v in obj.items()}
    return obj


# ── Grid-side token counting (never trust worker `usage` for money) ──────────


def _flatten_content(content) -> list[str]:
    """Pull plain text out of an Anthropic/Responses `content` value.

    Handles str, a list of typed blocks (text / tool_result / tool_use / image),
    and nested tool_result content. Images are skipped (no text); tool inputs are
    serialized so tool-heavy turns aren't billed as empty."""
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return [str(content)]
    out: list[str] = []
    for b in content:
        if isinstance(b, str):
            out.append(b)
        elif isinstance(b, dict):
            if isinstance(b.get("text"), str):
                out.append(b["text"])
            tr = b.get("content")  # tool_result content (str or nested blocks)
            if isinstance(tr, str):
                out.append(tr)
            elif isinstance(tr, list):
                out.extend(_flatten_content(tr))
            if b.get("type") == "tool_use" and b.get("input") is not None:
                out.append(json.dumps(b["input"]))
    return out


def extract_prompt_text(api_format: str, req: dict) -> str:
    """Flatten a passthrough request to the text we bill the prompt on."""
    parts: list[str] = []
    if api_format == "anthropic":
        parts += _flatten_content(req.get("system"))
        for m in req.get("messages") or []:
            if isinstance(m, dict):
                parts += _flatten_content(m.get("content"))
        for t in req.get("tools") or []:
            if isinstance(t, dict):
                parts.append(str(t.get("name", "")))
                parts.append(str(t.get("description", "")))
                if t.get("input_schema") is not None:
                    parts.append(json.dumps(t["input_schema"]))
    else:  # openai-responses
        parts += _flatten_content(req.get("instructions"))
        inp = req.get("input")
        if isinstance(inp, str):
            parts.append(inp)
        elif isinstance(inp, list):
            for item in inp:
                if isinstance(item, dict):
                    parts += _flatten_content(item.get("content"))
                    if isinstance(item.get("output"), str):  # function_call_output
                        parts.append(item["output"])
                elif isinstance(item, str):
                    parts.append(item)
        for t in req.get("tools") or []:
            if isinstance(t, dict):
                parts.append(str(t.get("name", "")))
                parts.append(str(t.get("description", "")))
    return " ".join(p for p in parts if p)


def _stream_delta_text(raw_data: str) -> str:
    """Text contributed by ONE relayed SSE `data:` payload.

    Covers both formats with one shape rule: Anthropic deltas are
    `{"delta":{"text"|"thinking"|"partial_json": ...}}`; Responses deltas are
    `{"delta": "<str>"}`. Anything else (ping, message_start, usage frames)
    contributes no billable text."""
    try:
        obj = json.loads(raw_data)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    d = obj.get("delta")
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return d.get("text") or d.get("thinking") or d.get("partial_json") or ""
    return ""


def completion_tokens(api_format: str, accumulated: list[str] | None, full_json: dict | None) -> int:
    """Grid-side completion token count for billing a passthrough job.

    Prefer the assembled non-streaming body (`full_json`); otherwise sum the text
    from the relayed stream deltas. Counted by the grid (tiktoken), never the
    worker/backend `usage`. Used by the worker-WS terminal handler to settle."""
    if full_json:
        text = extract_output_text(api_format, full_json)
    else:
        text = "".join(_stream_delta_text(d) for d in (accumulated or []))
    return den.count_tokens(text)


def extract_output_text(api_format: str, full_json: dict) -> str:
    """Assemble the completion text from a non-streaming response body."""
    if not isinstance(full_json, dict):
        return ""
    parts: list[str] = []
    if api_format == "anthropic":
        for b in full_json.get("content") or []:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    parts.append(b["text"])
                if isinstance(b.get("thinking"), str):
                    parts.append(b["thinking"])
                if b.get("type") == "tool_use" and b.get("input") is not None:
                    parts.append(json.dumps(b["input"]))
        return " ".join(parts)
    # openai-responses
    if isinstance(full_json.get("output_text"), str) and full_json["output_text"]:
        parts.append(full_json["output_text"])
    else:
        for item in full_json.get("output") or []:
            if isinstance(item, dict):
                for c in item.get("content") or []:
                    if isinstance(c, dict) and isinstance(c.get("text"), str):
                        parts.append(c["text"])
    for item in full_json.get("output") or []:
        if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call") \
                and isinstance(item.get("arguments"), str):
            parts.append(item["arguments"])
    return " ".join(parts)


# ── Reserve / settle ─────────────────────────────────────────────────────────


async def authorize_passthrough(user: dict, model: str, api_format: str,
                                raw_request: dict, max_len: int, job_id: str) -> dict:
    """Count the prompt grid-side and reserve before dispatch.

    Reserves atomically (debit + grid_reservations row in one transaction) so the
    worker-WS handler is the durable, sole settler — same lifecycle as chat.
    Returns the authorize_request dict augmented with `prompt_toks`. In dry-run
    (charging off) it's a no-op: ok, reserved=0. The caller shapes its own
    402 from `{ok: False, reason}` so each endpoint keeps its native error body."""
    prompt_toks = den.count_tokens(extract_prompt_text(api_format, raw_request))
    if not credits.CHARGING_ENABLED:
        return {"ok": True, "reserved": 0, "prompt_toks": prompt_toks, "status": "dry_run"}
    auth = dict(await credits.authorize_request(
        user, model, prompt_toks, max_len, job_id, record_reservation=True))
    auth["prompt_toks"] = prompt_toks
    return auth


async def _observe_dry_passthrough(user, model, prompt_toks, completion_toks, job_id):
    """Dry-run observability ONLY: log the would-charge on grid counts. LIVE
    settlement is durable + authoritative in the worker-WS handler
    (credits.settle_job) — doing it here too would double-settle and depend on the
    client staying connected. Never breaks a response."""
    if credits.CHARGING_ENABLED:
        return
    try:
        await credits.charge_request(
            user, model, int(prompt_toks or 0), int(completion_toks or 0), job_id)
    except Exception:
        logger.debug("passthrough dry-run observe failed (non-fatal)", exc_info=True)


def new_passthrough_job_id() -> str:
    return str(uuid4())


async def submit_passthrough_job(job_id: str, model: str, api_format: str,
                                 raw_request: dict, max_length: int) -> None:
    """Queue a raw-passthrough job. If dispatch fails, release the held
    reservation (otherwise the funds would be stranded with no settlement path)."""
    payload = {
        "request": raw_request,
        "api_format": api_format,
        "max_length": max_length,
    }
    try:
        await job_queue.submit_job(job_id, payload, [model])
    except Exception:
        # Dispatch failed → release the held reservation (refund + flip settled).
        await credits.release_job(job_id)
        raise


async def stream_passthrough(job_id: str, *, api_format: str = "", user: dict | None = None,
                             model: str = "", prompt_toks: int = 0):
    """Relay raw upstream SSE events verbatim.

    LIVE billing is settled durably + authoritatively in the worker-WS handler
    (credits.settle_job), independent of whether the client stays connected. Here
    we only feed dry-run OBSERVABILITY on grid-counted output."""
    relayed: list[str] = []
    observed = False
    terminal = False  # natural end vs. client disconnect
    try:
        async for data in token_stream.subscribe_tokens(job_id):
            if data.get("text") == token_stream.DONE_SENTINEL:
                err = data.get("error")
                if err:
                    terminal = True
                    body = json.dumps({"type": "error", "error": {"type": "api_error", "message": err}})
                    yield f"event: error\ndata: {body}\n\n"
                    return  # live settlement is worker_ws's job; nothing to observe on error
                await _observe_dry_passthrough(user, model, prompt_toks, den.count_tokens("".join(relayed)), job_id)
                observed = True
                terminal = True
                return
            if data.get("raw"):
                ev = data.get("event")
                d = data.get("data", "")
                relayed.append(_stream_delta_text(d))
                # Anthropic + Responses both use named SSE events; relay the name
                # when present so the client's SDK dispatches correctly.
                if ev:
                    yield f"event: {ev}\ndata: {d}\n\n"
                else:
                    yield f"data: {d}\n\n"
    finally:
        # Torn down before a natural finish → client disconnected; abort the worker.
        if not terminal:
            await token_stream.request_cancel(job_id)
        # Dry-run only: observe once even on disconnect (live money is worker_ws's).
        if not observed:
            await _observe_dry_passthrough(user, model, prompt_toks, den.count_tokens("".join(relayed)), job_id)


async def collect_passthrough(job_id: str, *, api_format: str = "", user: dict | None = None,
                              model: str = "", prompt_toks: int = 0) -> dict:
    """Return the complete upstream JSON body. LIVE billing is settled in
    worker_ws; here we only feed dry-run observability on grid-counted output."""
    async for data in token_stream.subscribe_tokens(job_id):
        if data.get("text") == token_stream.DONE_SENTINEL:
            err = data.get("error")
            if err:
                raise HTTPException(status_code=data.get("code") or 502, detail=err)
            full = data.get("full_json") or {}
            await _observe_dry_passthrough(
                user, model, prompt_toks, den.count_tokens(extract_output_text(api_format, full)), job_id)
            return full
    raise HTTPException(status_code=504, detail="No response from worker")
