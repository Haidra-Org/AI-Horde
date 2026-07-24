# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebSocket endpoint for grid workers — the unified worker protocol.

One protocol for every worker type. Workers register with the job types they
serve (text | image | video), receive jobs pushed from the per-type Redis
Streams, and report results back:

  text  — stream tokens; relayed to Redis Pub/Sub for SSE clients
  media — upload outputs directly to R2 via presigned PUT URLs included in
          the job message (workers never hold storage credentials), then
          report the object keys + content hashes

Every completion appends an event to the grid_ledger (den + prompt/result
hashes) — the source of truth the on-chain settlement pays against.

Worker registry is stored in Redis (not in-memory) so multiple
uvicorn processes can share state.
"""

import asyncio
import json
import logging
from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..database import new_session
from ..redis_client import get_redis
from ..services import accounts as accounts_svc
from ..services import audio, credits, job_queue, signing, storage, token_stream
from ..services import ledger as ledger_svc
from ..services import worker_identity as worker_identity_svc
from ..services.den import calculate_den, calculate_media_den, count_tokens
from ..services.metrics_state import record_job_complete, record_job_failed
from ..v2.schema import workers as v2_workers_table

logger = logging.getLogger("grid_api.worker_ws")

router = APIRouter()

# Redis key prefixes for worker registry
WORKER_STATUS_PREFIX = "grid:worker:"
WORKER_STATUS_SUFFIX = ":status"
WORKER_ACTIVE_SET = "grid:workers:active"
# name → "1" with TTL, for O(1) "is this worker online?" by name (worker
# affinity). Refreshed on every heartbeat; TTL-reaped if a worker vanishes.
WORKER_ONLINE_BY_NAME = "grid:worker:online:"


async def _worker_online(worker_name: str) -> bool:
    """True if a worker with this name currently has a live registration."""
    if not worker_name:
        return False
    r = get_redis()
    return bool(await r.get(f"{WORKER_ONLINE_BY_NAME}{worker_name}"))


# In-process tracking for WebSocket handles (can't serialize these to Redis)
_local_ws: dict[str, WebSocket] = {}


def _can_connect_worker(user: dict) -> bool:
    """Allow only Grid keys carrying an explicit worker capability."""
    return user.get("source") == "v2" and bool(
        {"worker.connect", "inference.submit"} & set(user.get("scopes") or []),
    )


def _worker_key_matches_name(user: dict, worker_name: str) -> bool:
    """Manager-issued rig credentials may register only their labeled rig."""
    if user.get("source") != "v2" or user.get("key_kind") != "worker":
        return True
    return user.get("key_label") == f"worker:{worker_name}"


def _receipt_signers(worker_info: dict) -> list[str]:
    """A delegated signer supersedes the payout wallet for output receipts."""
    signer = worker_info.get("signer_address")
    return [signer] if signer else [worker_info.get("wallet_address", "")]


def _media_result_commitment(job_type: str, payload: dict, outputs: list[dict]):
    output_hashes = [item.get("sha256") or item["key"] for item in outputs]
    if job_type == "audio":
        return {"outputs": output_hashes, "recipe_root": payload.get("recipe_root")}
    return output_hashes


def _validated_audio_results(value: object, expected: int) -> dict[int, dict]:
    """Require one canonical output digest for every presigned audio slot."""
    if not isinstance(value, list) or len(value) != expected:
        raise ValueError("audio result count does not match the requested outputs")
    reported: dict[int, dict] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("audio result entry is malformed")
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= expected or index in reported:
            raise ValueError("audio result index is invalid or duplicated")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("audio result digest is not canonical SHA-256 hex")
        reported[index] = item
    return reported


def _media_output_units(job_type: str, payload: dict, n: int) -> int:
    if job_type == "audio":
        return max(n, int(round(float(payload.get("seconds", 0) or 0))))
    return max(n, int(payload.get("frames", 0) or 0))


def _requires_managed_profile(job_types: list[str], worker_profile: dict | None) -> bool:
    """Audio is a governed-profile capability, not a free-form job type."""
    return "audio" in job_types and worker_profile is None


def _requires_signed_receipt(job_type: str, worker_info: dict) -> bool:
    """Managed-profile work must be attributable to its delegated rig key."""
    return job_type == "audio" or worker_info.get("worker_profile") is not None


# ── Worker health enforcement ──
# A completion that is empty (zero tokens) or an explicit worker error counts
# as a strike. A worker that accumulates MAX_STRIKES within the decay window is
# evicted and barred from re-registering for EVICT_COOLDOWN_S — this is what
# stops a worker whose inference backend has died from silently swallowing a
# share of every job (the 2026-06-14 "empty every other message" outage).
MAX_STRIKES = 6
STRIKE_DECAY_S = 300  # strikes reset after 5 min without a failure
# Escalating quarantine: each eviction within EVICT_COUNT_DECAY_S ramps the
# re-register bar (30s → 2m → 10m → 1h) so a chronically-broken backend backs
# off instead of flapping back every 30s and eating fresh jobs. The repeat
# counter (per worker name) decays after a clean hour, so a one-off blip stays cheap.
EVICT_COOLDOWN_LADDER_S = [30, 120, 600, 3600]
EVICT_COUNT_DECAY_S = 3600


async def _record_strike(worker_id: str) -> int:
    """Increment a worker's failure strike count; returns the new total."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}:strikes"
    n = await r.incr(key)
    await r.expire(key, STRIKE_DECAY_S)
    return n


async def _clear_strikes(worker_id: str):
    """A successful job clears the strike count."""
    r = get_redis()
    await r.delete(f"{WORKER_STATUS_PREFIX}{worker_id}:strikes")


async def _evict_worker(worker_id: str, worker_name: str) -> int:
    """Deregister an unhealthy worker and bar it from re-registering, with an
    escalating cooldown per repeat eviction. Returns the cooldown (seconds)."""
    r = get_redis()
    await unregister_worker(worker_id, worker_name)
    # Count evictions for this logical worker — keyed by NAME, since worker_id
    # changes on every reconnect. Walk up the ladder; counter decays after a
    # clean hour so a recovered worker returns to the cheap 30s rung.
    ecount_key = f"grid:worker:evictions:{worker_name}"
    n = await r.incr(ecount_key)
    await r.expire(ecount_key, EVICT_COUNT_DECAY_S)
    cooldown = EVICT_COOLDOWN_LADDER_S[min(n - 1, len(EVICT_COOLDOWN_LADDER_S) - 1)]
    await r.setex(f"grid:worker:cooldown:{worker_name}", cooldown, "evicted")
    await r.delete(f"{WORKER_STATUS_PREFIX}{worker_id}:strikes")
    return cooldown


async def _is_in_cooldown(worker_name: str) -> bool:
    r = get_redis()
    return bool(await r.get(f"grid:worker:cooldown:{worker_name}"))


# ── Redis-backed worker registry ──


async def register_worker(worker_id: str, info: dict):
    """Register a worker in Redis. Visible to all uvicorn processes."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.setex(key, 60, json.dumps(info))
    await r.sadd(WORKER_ACTIVE_SET, worker_id)
    if info.get("name"):
        await r.setex(f"{WORKER_ONLINE_BY_NAME}{info['name']}", 60, "1")


async def unregister_worker(worker_id: str, worker_name: str = ""):
    """Remove a worker from the Redis registry."""
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.delete(key)
    await r.srem(WORKER_ACTIVE_SET, worker_id)
    if worker_name:
        await r.delete(f"{WORKER_ONLINE_BY_NAME}{worker_name}")


async def refresh_worker(worker_id: str, info: dict):
    """Refresh worker TTL in Redis AND re-assert active-set membership.

    Re-adding to the set on every refresh self-heals a reconnect race: when a
    worker reconnects under the same worker_id, the OLD handler's cleanup
    (unregister_worker: srem + del) can run AFTER the NEW handler's
    register_worker, leaving a live status key that's missing from
    grid:workers:active — making the worker invisible to get_available_models
    (so /v1/models returns [] and chat 503s) even though it's connected and
    healthy. Re-asserting membership here repairs that within one refresh (~10s).
    """
    r = get_redis()
    key = f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}"
    await r.setex(key, 60, json.dumps(info))
    await r.sadd(WORKER_ACTIVE_SET, worker_id)
    if info.get("name"):
        await r.setex(f"{WORKER_ONLINE_BY_NAME}{info['name']}", 60, "1")


async def get_available_models(job_type: str | None = None, api_format: str | None = None) -> list[str]:
    """Get models from connected workers (reads from Redis).

    When `job_type` is given (e.g. "text"), only models served by a worker of
    that modality are returned — so the OpenAI `/v1/models` chat list never
    surfaces image/video models like LTX-2.3 that can't be used via
    chat-completions. Each worker self-declares its `job_types` at registration.

    When `api_format` is given (e.g. "anthropic", "openai-responses"), only
    models served by a worker whose backend natively exposes that API are
    returned. This is what makes `/v1/messages` and `/v1/responses` honest:
    if no connected worker advertises the format, the model list is empty and
    the endpoint returns 503 — the grid never fakes a format it can't serve.
    Workers that don't advertise `api_formats` are treated as openai-chat.
    """
    r = get_redis()
    worker_ids = list(await r.smembers(WORKER_ACTIVE_SET))
    if not worker_ids:
        return []
    # Pipeline the per-worker GETs into ONE round-trip (was N+1 sequential GETs on the
    # hot path — every chat/image/video/models request, sometimes twice).
    keys = [f"{WORKER_STATUS_PREFIX}{wid}{WORKER_STATUS_SUFFIX}" for wid in worker_ids]
    pipe = r.pipeline()
    for key in keys:
        pipe.get(key)
    results = await pipe.execute()
    models = set()
    stale = []
    for wid, data in zip(worker_ids, results):
        if not data:
            stale.append(wid)  # expired worker — prune
            continue
        info = json.loads(data)
        if job_type and job_type not in (info.get("job_types") or ["text"]):
            continue
        if api_format and api_format not in (info.get("api_formats") or ["openai-chat"]):
            continue
        models.update(info.get("models", []))
    # Recipe-backed models: a media recipe can define a NEW model name served by an
    # EXISTING checkpoint (e.g. "LTX-2.3 Audio" runs on an "LTX-2.3" worker). Advertise
    # it when ALL its requiredModels are present among online workers of this modality,
    # so "add a recipe = new model" needs zero worker-side change. Media only.
    if job_type in ("image", "video") and not api_format:
        try:
            from ..services import recipes as _recipes

            for rc in _recipes.list_recipes():
                if rc.job_type == job_type and rc.required_models and all(m in models for m in rc.required_models):
                    models.add(rc.model_name)
        except Exception as e:
            logger.debug("recipe-backed model advertisement skipped: %s", e)
    if stale:
        await r.srem(WORKER_ACTIVE_SET, *stale)
    return sorted(models)


async def get_model_modalities() -> dict[str, list[str]]:
    """Map each connected TEXT model → the UNION of input modalities advertised by
    the workers serving it. A model is image-capable if ANY worker serving it
    declares "image". Used to surface input_modalities on /v1/models so the chat
    UI can enable image upload for vision-capable models. Mirrors the worker
    iteration in get_available_models (one pipelined round-trip)."""
    r = get_redis()
    worker_ids = list(await r.smembers(WORKER_ACTIVE_SET))
    if not worker_ids:
        return {}
    keys = [f"{WORKER_STATUS_PREFIX}{wid}{WORKER_STATUS_SUFFIX}" for wid in worker_ids]
    pipe = r.pipeline()
    for key in keys:
        pipe.get(key)
    results = await pipe.execute()
    out: dict[str, set] = {}
    for data in results:
        if not data:
            continue
        info = json.loads(data)
        if "text" not in (info.get("job_types") or ["text"]):
            continue
        mods = info.get("modalities") or ["text"]
        for m in info.get("models", []):
            out.setdefault(m, set()).update(mods)
    # Stable order: text first, then the rest alphabetically.
    return {model: (["text"] if "text" in mods else []) + sorted(mods - {"text"}) for model, mods in out.items()}


async def get_connected_worker_count() -> int:
    """Get count of active workers."""
    r = get_redis()
    return await r.scard(WORKER_ACTIVE_SET)


# ── WebSocket handler ──


@router.websocket("/v1/workers/ws")
async def worker_websocket(ws: WebSocket):
    """Persistent WebSocket connection for text generation workers."""
    await ws.accept()
    worker_info = None
    worker_id = None
    current_job = None  # Track in-progress job for retry on disconnect

    try:
        # ── Step 1: Auth handshake ──
        init_msg = await asyncio.wait_for(ws.receive_json(), timeout=30)

        apikey = init_msg.get("apikey", "")
        worker_name = init_msg.get("name", "")
        models = init_msg.get("models", [])
        max_length = init_msg.get("max_length", 512)
        max_context_length = init_msg.get("max_context_length", 2048)
        # Job types this worker serves. Accepts the new `job_types` list or the
        # legacy single `worker_type`; defaults to text for old text workers.
        job_types = init_msg.get("job_types") or [init_msg.get("worker_type", "text")]
        job_types = [t for t in job_types if t in ("text", "image", "video", "audio", "3d")] or ["text"]
        bridge_agent = init_msg.get("bridge_agent", "grid-ws")
        try:
            worker_profile = worker_identity_svc.normalize_worker_profile(init_msg.get("worker_profile"))
        except worker_identity_svc.WorkerIdentityError as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close(code=4003)
            return
        if _requires_managed_profile(job_types, worker_profile):
            await ws.send_json(
                {"type": "error", "message": "audio requires an approved managed profile"},
            )
            await ws.close(code=4003)
            return
        # API formats this worker's backend natively serves. The worker probes
        # its inference engine and advertises only what actually answers (vLLM
        # exposes openai-chat + openai-responses but NOT anthropic, for example).
        # The grid routes each API endpoint to the matching pool; a format with
        # no workers simply has no capacity (honest 503) — the grid never
        # translates between formats. Legacy workers that don't send this are
        # assumed to be plain OpenAI chat workers.
        api_formats = init_msg.get("api_formats") or ["openai-chat"]
        api_formats = [f for f in api_formats if f in ("openai-chat", "openai-responses", "anthropic")] or ["openai-chat"]
        # Input modalities the worker's model accepts (operator-declared). Surfaced
        # on /v1/models as input_modalities so the chat UI enables image upload for
        # vision models. Defaults to text-only for workers that don't advertise it.
        modalities = init_msg.get("modalities") or ["text"]
        modalities = [m for m in modalities if m in ("text", "image", "video")] or ["text"]
        # NOTE: the payout wallet is NOT taken from the worker. It's resolved
        # from the authenticated account below. This means an operator runs
        # workers on any number of rigs with ONLY an API key — no wallet or
        # private key on the rig — and a worker can't declare a wallet to
        # redirect another account's earnings.

        if not apikey or not worker_name:
            await ws.send_json({"type": "error", "message": "Missing apikey or name"})
            await ws.close(code=4001)
            return

        # Validate API key — v2 account keys first, legacy keys fall back.
        user = await accounts_svc.resolve_api_key(apikey)
        if not user:
            await ws.send_json({"type": "error", "message": "Invalid API key"})
            await ws.close(code=4001)
            return
        if not _can_connect_worker(user):
            await ws.send_json(
                {"type": "error", "message": "API key lacks worker.connect scope"},
            )
            await ws.close(code=4003)
            return
        if not _worker_key_matches_name(user, worker_name):
            await ws.send_json(
                {"type": "error", "message": "worker credential targets another rig"},
            )
            await ws.close(code=4003)
            return

        # Refuse workers we just evicted for failing health — gives a flapping
        # worker (dead backend) time to actually recover before rejoining.
        if await _is_in_cooldown(worker_name):
            await ws.send_json(
                {
                    "type": "error",
                    "message": "Worker recently evicted for failed generations; retry shortly.",
                },
            )
            await ws.close(code=4003)
            return

        # Payout wallet ALWAYS comes from the authenticated account, never the
        # worker. Prefer the explicit payout_wallet (settable by any operator,
        # mining-style, no proof) and fall back to the identity wallet for SIWE
        # users. If neither is set, den accrues unattributed until they set one
        # (see settlement.count_unattributed_den).
        wallet_address = user.get("payout_wallet") or user.get("wallet") or ""
        identity_required = get_settings().require_worker_identity or worker_profile is not None or "audio" in job_types
        try:
            verified_identity = await worker_identity_svc.verify_registration(
                proof=init_msg.get("worker_identity"),
                payout_wallet=wallet_address,
                worker_name=worker_name,
                models=models,
                job_types=job_types,
                bridge_agent=bridge_agent,
                worker_profile=worker_profile,
                required=identity_required,
            )
        except worker_identity_svc.WorkerIdentityError as exc:
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close(code=4003)
            return

        capabilities = {"job_types": job_types}
        if worker_profile is not None:
            capabilities["worker_profile"] = worker_profile
        if verified_identity is not None:
            capabilities.update(
                {
                    "signer_address": verified_identity.signer_address,
                    "delegation_id": verified_identity.delegation_id,
                    "delegation_expires_at": verified_identity.expires_at,
                },
            )

        now = datetime.utcnow()
        async with await new_session() as session:
            row = (
                await session.execute(
                    sa.select(v2_workers_table.c.id, v2_workers_table.c.account_id).where(
                        v2_workers_table.c.name == worker_name,
                    ),
                )
            ).first()
            # SECURITY: a worker name is owned by the account that created it. Without
            # this check any authenticated account could register an existing worker's
            # name and rebind its wallet → redirect another operator's den/earnings.
            if row and row[1] != user["account_id"]:
                await ws.send_json(
                    {
                        "type": "error",
                        "message": "worker name already registered to another account",
                    },
                )
                await ws.close(code=4003)
                return
            if row:
                worker_id = str(row[0])
                await session.execute(
                    sa.update(v2_workers_table)
                    .where(v2_workers_table.c.id == row[0])
                    .values(
                        last_seen=now,
                        models=models,
                        wallet=wallet_address or None,
                        type=job_types[0],
                        capabilities=capabilities,
                    ),
                )
            else:
                worker_id = str(uuid4())
                await session.execute(
                    sa.insert(v2_workers_table).values(
                        id=worker_id,
                        account_id=user["account_id"],
                        name=worker_name,
                        type=job_types[0],
                        wallet=wallet_address or None,
                        models=models,
                        capabilities=capabilities,
                        bridge_agent=bridge_agent,
                        maintenance=False,
                        first_seen=now,
                        last_seen=now,
                        jobs_completed=0,
                        den_earned=0.0,
                    ),
                )
            await session.commit()

        # Register in Redis (visible to all processes)
        worker_info = {
            "worker_id": worker_id,
            "user_id": user["id"],
            "name": worker_name,
            "models": models,
            "job_types": job_types,
            "api_formats": api_formats,
            "modalities": modalities,
            "max_length": max_length,
            "max_context_length": max_context_length,
            "wallet_address": wallet_address,
            "signer_address": (verified_identity.signer_address if verified_identity is not None else ""),
            "worker_profile": worker_profile,
        }
        # Single active connection per worker. A reconnect (restart / network blip)
        # can leave the previous WS half-open; if its server-side task is still
        # running it keeps refreshing the registry with stale data, so a renamed
        # model flip-flops between the old and new name (phantom). Claim the slot
        # FIRST (so the old task's cleanup, guarded by `is ws`, won't de-register
        # us), then close the prior socket so only this connection drives the
        # registry.
        _prev_ws = _local_ws.get(worker_id)
        _local_ws[worker_id] = ws
        if _prev_ws is not None and _prev_ws is not ws:
            try:
                await _prev_ws.close(code=1012)  # 1012 = service restart / replaced
            except Exception:
                pass
        await register_worker(worker_id, worker_info)

        await ws.send_json({"type": "ready", "worker_id": worker_id})
        logger.info(f"Worker '{worker_name}' ({worker_id}) connected, types={job_types}, models: {models}")

        # ── Step 2: Concurrent job polling + keepalive ──
        # A bounded queue (maxsize=1) decouples the Redis poll from the
        # dispatch loop with natural backpressure: at most one job is
        # prefetched. This replaces an earlier job_ready/busy-wait handshake
        # that could deadlock the poll task after the first dispatch (it spun
        # forever in `while job_ready.is_set()`), so it stopped calling
        # XREADGROUP entirely — silently stranding every later job while the
        # worker still looked online.
        local_jobs: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def _poll_jobs():
            """Background: pull jobs from Redis and hand them to the loop.

            Wrapped so a transient Redis/parse error can NEVER silently kill
            this task — if it died, the main loop kept refreshing registration
            (worker looks online) while no jobs were ever consumed again. That
            was the 'serves one job then goes deaf' bug.
            """
            while True:
                try:
                    job = await job_queue.pop_job(worker_id, timeout_ms=5000, job_types=job_types)
                    if job:
                        await local_jobs.put(job)  # blocks (backpressure) until taken
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"_poll_jobs error for {worker_id}: {e}", exc_info=True)
                    await asyncio.sleep(1)

        poll_task = asyncio.create_task(_poll_jobs())

        async def _keepalive_registry():
            """Keep the 60s registry TTL fresh for the WHOLE connection lifetime.

            The idle main loop refreshes on every iteration, but once a job is
            dispatched control blocks inside a handler's recv loop until the job
            finishes. A job that runs longer than the 60s TTL (a long video or a
            slow reasoning stream) would otherwise let the status key expire —
            dropping a healthy, BUSY worker out of grid:workers:active so
            /v1/models goes empty and new requests 503. Refresh independently of
            job state so presence tracks the connection, not the job length.
            """
            while True:
                await asyncio.sleep(20)
                try:
                    await refresh_worker(worker_id, worker_info)
                except Exception:
                    logger.debug("registry keepalive refresh failed", exc_info=True)

        refresh_task = asyncio.create_task(_keepalive_registry())

        try:
            while True:
                # Wait for the next job, or time out every 10s to keepalive.
                try:
                    job = await asyncio.wait_for(local_jobs.get(), timeout=10)
                except asyncio.TimeoutError:
                    job = None

                # Refresh registration every iteration regardless of socket
                # health (cheap Redis write; keeps the worker in the registry
                # even if the WS is momentarily slow).
                await refresh_worker(worker_id, worker_info)

                if job is None:
                    # Idle keepalive — BOUNDED. On a half-open connection a raw
                    # ws.send_json can block until the kernel TCP timeout
                    # (minutes), wedging the loop; time-box it and break cleanly
                    # on any failure so the worker reconnects + re-registers.
                    try:
                        await asyncio.wait_for(ws.send_json({"type": "ping"}), timeout=10)
                        try:
                            await asyncio.wait_for(ws.receive_json(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass
                    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError) as e:
                        logger.info(f"Worker '{worker_name}' keepalive failed " f"({type(e).__name__}) — closing for reconnect")
                        break
                    continue

                # Got a job → dispatch it (model check + text/media paths below).

                # ── Worker targeting ──
                # Validator probes use hard targeting. If Redis hands the job to
                # any other worker, that worker must not execute it and create
                # evidence for the wrong target.
                hard_target = job.get("hard_target_worker", "")
                if hard_target and hard_target != worker_name:
                    if await _worker_online(hard_target):
                        bounced = await job_queue.bounce_for_affinity(job)
                        if bounced:
                            continue
                    await token_stream.publish_error(
                        job["job_id"],
                        f"Target worker '{hard_target}' did not claim this validator probe.",
                        code=503,
                    )
                    await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    current_job = None
                    continue

                # ── Worker affinity (soft) ──
                # If the job prefers a different worker, release it back so that
                # worker can claim it — but only while the preferred worker is
                # online and the job hasn't bounced too many times. Otherwise run
                # it here: affinity is a preference, never a reason to stall.
                preferred = job.get("preferred_worker", "")
                if preferred and preferred != worker_name:
                    if await _worker_online(preferred):
                        bounced = await job_queue.bounce_for_affinity(job)
                        if bounced:
                            continue
                        # bounce limit hit → fall through and run it here.
                    # preferred worker offline → run it here (don't strand the job).

                # Check model compatibility. The shared stream + consumer group
                # hands jobs to a random worker regardless of served models, so
                # a mismatch is normal in a heterogeneous pool. Requeue for
                # another worker instead of discarding (which would strand the
                # client). If the job has bounced past the requeue limit, no
                # worker serves the model — fault it and tell the client.
                job_models = job["models"]
                matching = [m for m in job_models if m in models] if job_models else models
                if not matching:
                    requeued = await job_queue.requeue_for_mismatch(job)
                    if not requeued:
                        await token_stream.publish_error(
                            job["job_id"],
                            f"No worker available for the requested model(s): "
                            f"{', '.join(job_models) if job_models else 'unspecified'}.",
                        )
                        await credits.release_job(job["job_id"])  # gave up → refund the hold
                    continue

                selected_model = matching[0]

                # Check API-format compatibility too. A job for /v1/responses or
                # /v1/messages must land on a worker whose backend natively serves
                # that format; if this worker doesn't, requeue for one that does
                # (same heterogeneous-pool logic as model mismatch).
                job_format = job["payload"].get("api_format", "openai-chat")
                worker_formats = worker_info.get("api_formats") or ["openai-chat"]
                if job_format not in worker_formats:
                    requeued = await job_queue.requeue_for_mismatch(job)
                    if not requeued:
                        await token_stream.publish_error(
                            job["job_id"],
                            f"No worker available serving the '{job_format}' API for "
                            f"model(s): {', '.join(job_models) if job_models else 'unspecified'}.",
                        )
                        await credits.release_job(job["job_id"])  # gave up → refund the hold
                    continue

                # Track current job for retry on disconnect
                current_job = job
                job["worker_id"] = worker_id

                # ── Media path (image/video/audio/3D) ──
                if job.get("job_type", "text") != "text":
                    async with job_queue.maintain_job_claim(job):
                        ok = await _handle_media_job(
                            ws,
                            job,
                            selected_model,
                            worker_id,
                            worker_info,
                        )
                    if ok:
                        await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    current_job = None
                    continue

                # ── Raw passthrough path (Anthropic / OpenAI-Responses) ──
                # Natively-served formats are tunneled raw: the worker forwards
                # the request to the matching backend endpoint and relays the
                # upstream events verbatim. The grid tees usage for den but does
                # not transform the payload.
                if job_format != "openai-chat":
                    ok = await _handle_raw_passthrough(ws, job, selected_model, worker_id, worker_info)
                    if ok:
                        await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    current_job = None
                    continue

                # ── Text path ──
                if job["payload"].get("_validator_probe"):
                    ok = await _handle_validator_probe(ws, job, selected_model, worker_id, worker_info)
                    if ok:
                        await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    current_job = None
                    continue

                await ws.send_json(
                    {
                        "type": "job",
                        "id": job["job_id"],
                        "model": selected_model,
                        "payload": job["payload"],
                    },
                )

                # Wait for tokens + done
                import time as _time

                gen_start = _time.time()
                gen = await _handle_worker_generation(ws, job, worker_info)
                full_text = gen["full_text"]
                token_count = gen["metered"]
                failed = gen["failed"]
                client_error = gen["client_error"]
                ttft = gen["ttft"]
                gen_time = _time.time() - gen_start

                if client_error is not None:
                    # The request itself was bad (e.g. malformed tool schema,
                    # context too long). Surface the real reason to the client and
                    # ack — do NOT strike the worker or requeue (it is not the
                    # worker's fault and would fail identically on every worker).
                    await token_stream.publish_error(job["job_id"], client_error, code=400)
                    record_job_failed()
                    await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                    await credits.release_job(job["job_id"])  # terminal: refund the hold
                    current_job = None
                    continue

                if failed:
                    # The worker couldn't serve this job (dead backend / empty
                    # output). Strike it, and recover the job: if nothing was
                    # streamed to the client yet, silently requeue so a healthy
                    # worker can serve it; otherwise surface the error. NEVER pay
                    # den for a failed generation.
                    strikes = await _record_strike(worker_id)
                    record_job_failed()
                    if token_count == 0:
                        new_id = await job_queue.requeue_job(
                            job["job_id"],
                            job["payload"],
                            job["models"],
                            job.get("stream_id"),
                            job_type="text",
                            stream=job.get("stream"),
                            preferred_worker=job.get("preferred_worker", ""),
                            hard_target_worker=job.get("hard_target_worker", ""),
                            affinity_passes=job.get("affinity_passes", 0),
                        )
                        if new_id is None:
                            # Poison job hit MAX_REQUEUE — requeue_job already acked
                            # and gave up. This is terminal: surface an error and
                            # refund, else the client hangs to the idle timeout and
                            # the reservation stays stranded until the sweeper.
                            await token_stream.publish_error(job["job_id"], "No worker could complete this job; giving up.")
                            await credits.release_job(job["job_id"])
                            logger.warning(
                                f"Job {job['job_id']} dead-lettered after repeated empty " f"completions (worker '{worker_name}')",
                            )
                        else:
                            logger.warning(
                                f"Job {job['job_id']} requeued after worker '{worker_name}' " f"failed it (strike {strikes}/{MAX_STRIKES})",
                            )
                    else:
                        await token_stream.publish_error(job["job_id"], "Worker failed mid-generation; please retry.")
                        await job_queue.ack_job(job["stream_id"], stream=job.get("stream"))
                        # Terminal (surfaced to client, not requeued): refund the hold.
                        # NB: the token_count==0 branch above REQUEUES, so the
                        # reservation stays held for the retry — do not release there.
                        await credits.release_job(job["job_id"])
                    current_job = None

                    if strikes >= MAX_STRIKES:
                        cooldown = await _evict_worker(worker_id, worker_name)
                        logger.error(
                            f"Worker '{worker_name}' ({worker_id}) hit {MAX_STRIKES} "
                            f"strikes — evicting and barring re-register for {cooldown}s",
                        )
                        break  # drop the WS; cooldown blocks immediate rejoin
                    continue

                # Job completed successfully — clear any prior strikes.
                # NOTE: the queue ack, the worker ack, and the client's DONE are
                # all deferred until AFTER the atomic terminal commits — so a
                # settlement failure is never treated as success (no acked-but-
                # unsettled job, no DONE on an unpaid/uncharged completion).
                await _clear_strikes(worker_id)

                # Calculate den reward — but harden against gaming first.
                #
                # 1) Output tokens: NEVER trust the worker's self-reported count
                #    (a malicious worker inflates it). Count server-side from
                #    the text we actually received, and cap at the job's
                #    requested max_length — a worker can't be credited for more
                #    output than was asked for.
                requested_max = int(job["payload"].get("max_length", 512) or 512)
                # Real tokenizer (tiktoken) server-side — worker-independent and
                # far more accurate than word-splitting (which undercounts ~25%).
                # Count BOTH content and reasoning_content: reasoning tokens are
                # real generated work (the model spends most of its decode time on
                # them), so excluding them collapsed t/s for reasoning models to
                # ~2-3 and under-rewarded their den. Matches OpenAI (reasoning =
                # output tokens).
                server_token_count = count_tokens(full_text) + count_tokens(gen.get("full_reasoning") or "")
                effective_tokens = min(server_token_count, token_count or server_token_count, requested_max)

                # 2) Context: the prompt is user-controlled and the context
                #    multiplier scales up to 30x. Cap the prompt token count at
                #    the worker's advertised max_context_length so a self-dealer
                #    can't farm den by sending an enormous prompt to their own
                #    worker.
                prompt_text = job["payload"].get("prompt", "")
                ctx_cap = int(worker_info.get("max_context_length", 2048) or 2048)
                prompt_tokens = min(len(prompt_text.split()), ctx_cap)

                # The model-size multiplier comes from den.MODEL_REGISTRY (exact name
                # match), NOT from parsing the name — an unregistered/fake name gets the
                # conservative DEFAULT_MULTIPLIER (1.0), so it can't be inflated by
                # advertising "...-405b". Remaining gap (tracked): the registry isn't yet
                # ModelVault-synced, and the worker still isn't cryptographically proven to
                # serve the model it advertises — a registered-name worker serving a smaller
                # model is the open item, to be closed by validator re-exec / model-hash proof.
                den_awarded = calculate_den(
                    output_tokens=effective_tokens,
                    prompt_tokens=prompt_tokens,
                    model_name=selected_model,
                    generation_time_seconds=gen_time,
                )
                # ── ATOMIC terminal: worker-payout ledger row + demand
                # settlement commit together (or neither). grid_ledger is the
                # source of truth the on-chain settlement pays against; the
                # reservation is reconciled against a GRID-counted completion
                # (server tiktoken, capped at the requested max), never the
                # worker's self-report. A crash can't leave a paid worker with a
                # refundable hold. Idempotent on job_id (duplicate dispatch) and
                # on the reservation's held→settled flip; no-op for jobs with no
                # reservation (dry-run / legacy).
                bill_completion = min(server_token_count, requested_max)
                result_hash = ledger_svc.content_hash(full_text)
                payout_wallet = worker_info.get("wallet_address", "")
                # OPTIONAL "signed" tier: store the worker's signature ONLY if it
                # verifies to the payout wallet over this exact output commitment.
                # Absent/invalid → unsigned (floor). Fail-closed in signing.py.
                verified_sig = signing.verify_worker_sig(
                    job["job_id"],
                    result_hash,
                    gen.get("worker_sig"),
                    _receipt_signers(worker_info),
                )
                settle_result = await credits.record_and_settle(
                    ledger_values=dict(
                        job_id=job["job_id"],
                        worker_id=worker_id,
                        wallet=payout_wallet,
                        model=selected_model,
                        job_type="text",
                        den=den_awarded,
                        output_units=effective_tokens,
                        duration=gen_time,
                        ttft=ttft,
                        prompt_hash=ledger_svc.text_hash(prompt_text),
                        result_hash=result_hash,
                        worker_sig=verified_sig,
                    ),
                    completion_tokens=bill_completion,
                )

                if settle_result == "error":
                    # The terminal transaction did NOT commit (DB blip): no ledger
                    # row, reservation still held. Treat as NOT done — surface an
                    # error to the client and DO NOT ack the queue or the worker, so
                    # the message is reclaimed and re-served rather than silently
                    # becoming free inference with an unpaid worker. (current_job
                    # stays set → the disconnect/reclaim path recovers it.)
                    logger.critical(f"Terminal settlement failed for job {job['job_id']} — not acking; " f"leaving for stale-reclaim")
                    await token_stream.publish_error(job["job_id"], "Settlement failed; please retry.")
                    record_job_failed()
                    continue

                current_job = None
                if settle_result in _PAID_SETTLE:
                    # Paid success → tell the client DONE, ack queue + worker, metrics.
                    await token_stream.publish_done(
                        job["job_id"],
                        full_text,
                        gen["full_reasoning"],
                        tool_calls=gen["tool_calls"],
                        usage=gen["usage"],
                        finish_reason=gen["finish_reason"],
                        grid=gen["grid_meta"],
                    )
                    await job_queue.ack_job(job["stream_id"])
                    await ws.send_json({"type": "ack", "id": job["job_id"], "den": den_awarded})
                    record_job_complete(tokens=effective_tokens, den=den_awarded, duration=gen_time)
                else:
                    # 'stale_no_payout' / 'duplicate': the job is terminally CLOSED
                    # but this is NOT a paid completion — no DONE-as-success, no den,
                    # no success metrics. Ack the queue (done, don't reclaim) and ack
                    # the worker with den=0. For stale_no_payout surface a soft error
                    # so a still-waiting client isn't left hanging; for duplicate the
                    # winning dispatch already delivered the response.
                    logger.warning(f"Job {job['job_id']} closed without payout (settle={settle_result})")
                    if settle_result == "stale_no_payout":
                        await token_stream.publish_error(job["job_id"], "Job could not be settled (already closed); please retry.")
                    await job_queue.ack_job(job["stream_id"])
                    await ws.send_json({"type": "ack", "id": job["job_id"], "den": 0})
        finally:
            poll_task.cancel()
            refresh_task.cancel()

    except WebSocketDisconnect as e:
        logger.info(f"Worker '{worker_info['name'] if worker_info else 'unknown'}' disconnected (code={e.code})")
    except asyncio.TimeoutError:
        logger.warning(f"Worker '{worker_info['name'] if worker_info else 'unknown'}' timed out during handshake")
    except Exception as e:
        logger.error(f"Worker WebSocket error [{type(e).__name__}]: {e}", exc_info=True)
    finally:
        # ── Cleanup + job retry ──
        if worker_id:
            # Only tear down the registry if WE are still the active connection for
            # this worker. A newer connection may have superseded us (reconnect);
            # its registration must not be clobbered by this stale socket's cleanup.
            if _local_ws.get(worker_id) is ws:
                _local_ws.pop(worker_id, None)
                await unregister_worker(worker_id, worker_name or "")

        if current_job:
            # Worker disconnected with a job in progress — try to requeue it onto
            # another worker. Requeue and a terminal error are MUTUALLY EXCLUSIVE:
            # if the job lives on, we must NOT publish a terminal error or release
            # the hold (the same job_id continues; the next worker publishes to the
            # client's channel and the held reservation carries over). Only when the
            # requeue gives up (dead-lettered) is this terminal → error + release.
            job_id = current_job["job_id"]
            is_validator_probe = bool(current_job.get("payload", {}).get("_validator_probe"))
            if not is_validator_probe:
                record_job_failed()
            new_id = await job_queue.requeue_job(
                job_id,
                current_job["payload"],
                current_job["models"],
                current_job.get("stream_id"),
                job_type=current_job.get("job_type", "text"),
                stream=current_job.get("stream"),
                preferred_worker=current_job.get("preferred_worker", ""),
                hard_target_worker=current_job.get("hard_target_worker", ""),
                affinity_passes=current_job.get("affinity_passes", 0),
            )
            if new_id:
                logger.warning(f"Worker disconnected with job {job_id} in progress — requeued as {new_id}")
            else:
                logger.error(f"Worker disconnected with job {job_id}; requeue gave up — failing to client")
                await token_stream.publish_error(job_id, "Worker disconnected during generation; no capacity to retry.")
                if not is_validator_probe:
                    await credits.release_job(job_id)  # terminal: refund the hold

        # Drain a PREFETCHED-but-undispatched job (local_jobs, maxsize=1) so it
        # requeues immediately instead of waiting out the stale-reclaim loop. The
        # poller is already cancelled (inner finally), so nothing races this.
        try:
            prefetched = local_jobs.get_nowait()
        except Exception:
            prefetched = None  # empty, or the socket died before local_jobs existed
        if prefetched and prefetched.get("job_id") != (current_job or {}).get("job_id"):
            try:
                pid = prefetched["job_id"]
                nid = await job_queue.requeue_job(
                    pid,
                    prefetched["payload"],
                    prefetched["models"],
                    prefetched.get("stream_id"),
                    job_type=prefetched.get("job_type", "text"),
                    stream=prefetched.get("stream"),
                    preferred_worker=prefetched.get("preferred_worker", ""),
                    hard_target_worker=prefetched.get("hard_target_worker", ""),
                    affinity_passes=prefetched.get("affinity_passes", 0),
                )
                logger.warning("Requeued prefetched-but-undispatched job %s%s", pid, f" as {nid}" if nid else " (gave up)")
            except Exception:
                logger.error("failed to requeue prefetched job on cleanup", exc_info=True)

        if worker_info:
            logger.info(f"Worker '{worker_info['name']}' cleaned up")


async def _handle_validator_probe(ws: WebSocket, job: dict, selected_model: str, worker_id: str, worker_info: dict) -> bool:
    """Dispatch one assignment-bound validator probe.

    Validator probes are evidence collection, not paid inference. They must not
    reserve credits, append worker-payout ledger rows, award den, or strike
    workers directly. Any bad result becomes signed validator evidence later.
    """
    job_id = job["job_id"]
    payload = job["payload"]
    await ws.send_json(
        {
            "type": "job",
            "id": job_id,
            "model": selected_model,
            "payload": payload,
        },
    )

    gen = await _handle_worker_generation(ws, job, worker_info)
    if gen["client_error"] is not None:
        await token_stream.publish_error(job_id, gen["client_error"], code=400)
        await ws.send_json({"type": "ack", "id": job_id, "den": 0})
        return True

    if gen["failed"]:
        await token_stream.publish_error(job_id, "Validator probe failed on target worker.", code=502)
        await ws.send_json({"type": "ack", "id": job_id, "den": 0})
        return True

    grid_meta = {
        **(gen["grid_meta"] or {}),
        "worker_id": worker_id,
        "assignment_id": payload.get("_validator_assignment_id"),
        "grid_nonce": payload.get("_validator_grid_nonce"),
        "economic_effect": "none",
    }
    await token_stream.publish_done(
        job_id,
        gen["full_text"],
        gen["full_reasoning"],
        tool_calls=gen["tool_calls"],
        usage=gen["usage"],
        finish_reason=gen["finish_reason"],
        grid=grid_meta,
    )
    await ws.send_json({"type": "ack", "id": job_id, "den": 0})
    return True


async def _handle_media_job(ws: WebSocket, job: dict, selected_model: str, worker_id: str, worker_info: dict) -> bool:
    """Dispatch one image/video job to the worker and collect the result.

    The job message carries presigned PUT slots so the worker uploads outputs
    straight to R2. Completion is published on the job's token-stream channel
    as a JSON `full_text` payload, which the waiting HTTP handler parses.

    Returns True if the job finished (success or clean failure published to
    the client) and should be acked; raising propagates a socket failure so
    the caller's disconnect path requeues the job.
    """
    import time as _time

    job_id = job["job_id"]
    payload = job["payload"]
    job_type = job.get("job_type", "image")

    n = int(payload.get("n", 1) or 1)
    ext = payload.get("ext") or ("mp4" if job_type == "video" else "wav" if job_type == "audio" else "glb" if job_type == "3d" else "webp")
    upload_expires = audio.AUDIO_UPLOAD_URL_TTL if job_type == "audio" else 900
    try:
        upload_slots = storage.presign_outputs(
            job_id,
            n,
            ext,
            expires=upload_expires,
            job_type=job_type,
        )
    except Exception as e:
        logger.error(f"Presign failed for job {job_id}: {e}")
        await token_stream.publish_error(job_id, "Storage unavailable; please retry.")
        await credits.release_job(job_id)  # terminal: never dispatched → refund the hold
        return True

    await ws.send_json(
        {
            "type": "job",
            "id": job_id,
            "job_type": job_type,
            "model": selected_model,
            "payload": payload,
            "upload": [{"put_url": s["put_url"], "key": s["key"], "content_type": s["content_type"]} for s in upload_slots],
        },
    )

    gen_start = _time.time()
    receive_timeout = audio.AUDIO_WORKER_TIMEOUT if job_type == "audio" else 600
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=receive_timeout)
        msg_type = msg.get("type")

        if msg_type == "progress":
            pct = msg.get("pct", 0)
            # Relayed on the token channel (for any future SSE consumer); the
            # blocking HTTP handler ignores tokens.
            await token_stream.publish_token(
                job_id,
                json.dumps({"progress": pct, "preview": msg.get("preview_b64")}),
            )
            # Also stash the latest % under the client's progress token so it can
            # be polled at GET /v1/progress/{token} while the (synchronous) job runs.
            token = job.get("progress_token")
            if token:
                try:
                    await get_redis().setex(f"grid:progress:{token}", 180, str(int(pct)))
                except Exception:
                    pass  # progress is best-effort; never fail a job over it

        elif msg_type == "done":
            gen_time = _time.time() - gen_start
            expected_recipe_root = payload.get("recipe_root") if job_type == "audio" else None
            if expected_recipe_root and msg.get("recipe_root") != expected_recipe_root:
                logger.error("Audio worker returned the wrong recipe root for job %s", job_id)
                await token_stream.publish_error(job_id, "Worker recipe verification failed.")
                await credits.release_job(job_id)
                return True
            if job_type == "audio":
                try:
                    reported = _validated_audio_results(msg.get("results"), n)
                except ValueError as exc:
                    logger.error("Audio output verification failed for job %s: %s", job_id, exc)
                    await token_stream.publish_error(job_id, "Worker output verification failed.")
                    await credits.release_job(job_id)
                    return True
            else:
                reported = {r.get("index", i): r for i, r in enumerate(msg.get("results", []))}
            if job_type == "audio":
                try:
                    uploaded = await asyncio.to_thread(
                        storage.uploaded_outputs_present,
                        upload_slots,
                        min_bytes=audio.MIN_WAV_BYTES,
                        max_bytes=audio.MAX_AUDIO_BYTES,
                    )
                except Exception as exc:
                    logger.error("Audio storage verification unavailable for job %s: %s", job_id, exc)
                    await token_stream.publish_error(job_id, "Output verification unavailable; please retry.")
                    return False
                if not uploaded:
                    logger.error("Audio output object is missing or invalid for job %s", job_id)
                    await token_stream.publish_error(job_id, "Worker output verification failed.")
                    await credits.release_job(job_id)
                    return True
            outputs = []
            for i, slot in enumerate(upload_slots):
                rep = reported.get(i, {})
                outputs.append(
                    {
                        "url": slot["public_url"],
                        "key": slot["key"],
                        "seed": rep.get("seed"),
                        "sha256": rep.get("sha256"),
                    },
                )

            den_awarded = calculate_media_den(
                job_type=job_type,
                width=int(payload.get("width", 1024) or 1024),
                height=int(payload.get("height", 1024) or 1024),
                steps=int(payload.get("steps", 20) or 20),
                n=n,
                frames=int(payload.get("frames", 0) or 0),
                seconds=float(payload.get("seconds", 0) or 0),
            )

            # Result hash: digest over the worker-reported per-output sha256s.
            # ⚠️ UNATTESTED: the worker uploads to R2 directly and self-reports the
            # sha256 — the grid never fetches the bytes, so a malicious/lazy worker can
            # report an arbitrary hash for arbitrary content and still earn den (audit H4).
            # This hash is integrity bookkeeping, NOT proof-of-output. Real attestation
            # comes from the validator scan path (re-fetch + recompute, or sampled re-exec);
            # do not treat media result_hash as authoritative at settlement until then.
            result_commitment = _media_result_commitment(job_type, payload, outputs)
            result_hash = ledger_svc.canonical_hash(result_commitment)
            verified_sig = signing.verify_worker_sig(
                job_id,
                result_hash,
                msg.get("worker_sig"),
                _receipt_signers(worker_info),
            )
            if _requires_signed_receipt(job_type, worker_info) and not verified_sig:
                logger.error("Managed worker returned an invalid receipt for job %s", job_id)
                await token_stream.publish_error(job_id, "Worker receipt verification failed.")
                await credits.release_job(job_id)
                return True
            # ATOMIC terminal: worker-payout row + demand settlement in one txn.
            # Media reserves the EXACT cost up front, so success just lets the hold
            # stand (exact=True → flip held→settled, no ledger movement).
            settle_result = await credits.record_and_settle(
                ledger_values=dict(
                    job_id=job_id,
                    worker_id=worker_id,
                    wallet=worker_info.get("wallet_address", ""),
                    model=selected_model,
                    job_type=job_type,
                    den=den_awarded,
                    output_units=_media_output_units(job_type, payload, n),
                    duration=gen_time,
                    prompt_hash=ledger_svc.canonical_hash(payload),
                    result_hash=result_hash,
                    worker_sig=verified_sig,
                ),
                exact=True,
            )
            if settle_result == "error":
                # Terminal didn't commit → error out and DON'T ack (return False),
                # leaving the job for stale-reclaim rather than acking unsettled.
                logger.critical(f"Terminal settlement failed for media job {job_id} — not acking")
                await token_stream.publish_error(job_id, "Settlement failed; please retry.")
                return False

            if settle_result in _PAID_SETTLE:
                await token_stream.publish_done(
                    job_id,
                    json.dumps(
                        {
                            "media": outputs,
                            "model": selected_model,
                            "worker": worker_info.get("name", ""),
                            "gen_time": round(gen_time, 2),
                            "recipe_root": expected_recipe_root,
                        },
                    ),
                )
                await ws.send_json({"type": "ack", "id": job_id, "den": den_awarded})
                record_job_complete(tokens=0, den=den_awarded, duration=gen_time)
                return True

            # 'stale_no_payout' / 'duplicate': terminally closed, NOT a paid job —
            # no success DONE, no den, no metrics. Worker ack den=0; return True so
            # the caller acks the queue (the work is done, just not paid).
            logger.warning(f"Media job {job_id} closed without payout (settle={settle_result})")
            if settle_result == "stale_no_payout":
                await token_stream.publish_error(job_id, "Job could not be settled (already closed); please retry.")
            await ws.send_json({"type": "ack", "id": job_id, "den": 0})
            return True

        elif msg_type == "pong":
            continue

        elif msg_type == "error":
            logger.error(f"Worker error on media job {job_id}: {msg.get('message')}")
            await token_stream.publish_error(job_id, msg.get("message", "Worker error"))
            record_job_failed()
            await credits.release_job(job_id)  # terminal failure → refund the hold
            return True


async def _handle_raw_passthrough(ws: WebSocket, job: dict, selected_model: str, worker_id: str, worker_info: dict) -> bool:
    """Tunnel a raw Anthropic / OpenAI-Responses job to the worker and relay it.

    The worker forwards the client's request to the matching backend endpoint
    (/v1/messages or /v1/responses) and streams back the upstream events
    VERBATIM as `raw` messages (or a single `done` with `full_json` for
    non-streaming). The grid relays them untouched and only TEES the
    backend-reported `usage` for den — true faithful passthrough.

    Returns True when finished (success or surfaced failure) so the caller acks.
    """
    import time as _time

    job_id = job["job_id"]
    payload = job["payload"]

    await ws.send_json(
        {
            "type": "job",
            "id": job_id,
            "model": selected_model,
            "payload": payload,
        },
    )

    gen_start = _time.time()
    accumulated: list[str] = []  # raw data strings, for the result hash
    usage = None
    ttft: float | None = None
    cancel_sent = False
    last_cancel_poll = gen_start

    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=300)
        mtype = msg.get("type")

        # Forward a client-initiated cancel to the worker (once), throttled.
        if not cancel_sent:
            _now = _time.time()
            if _now - last_cancel_poll >= 0.3:
                last_cancel_poll = _now
                if await token_stream.is_cancelled(job_id):
                    await ws.send_json({"type": "cancel", "id": job_id})
                    cancel_sent = True
                    logger.info(f"Forwarded client cancel to worker for raw job {job_id}")

        if mtype == "raw":
            if ttft is None:
                ttft = _time.time() - gen_start
            data = msg.get("data", "")
            accumulated.append(data)
            await token_stream.publish_raw_event(job_id, msg.get("event"), data)

        elif mtype == "done":
            gen_time = _time.time() - gen_start
            usage = msg.get("usage") or usage
            full_json = msg.get("full_json")
            # DONE is published AFTER settlement commits (see below) so the client
            # never gets a success terminator on an unsettled job.

            # Metering: trust the backend-reported usage (we tee it), but cap the
            # output at the job's requested max and the prompt at the worker's
            # advertised context so a self-dealer can't inflate den.
            out_tokens = in_tokens = 0
            if isinstance(usage, dict):
                out_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                in_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            requested_max = int(payload.get("max_length", 4096) or 4096)
            effective_tokens = min(out_tokens, requested_max) if out_tokens else 0
            ctx_cap = int(worker_info.get("max_context_length", 2048) or 2048)
            prompt_tokens = min(in_tokens, ctx_cap)

            den_awarded = calculate_den(
                output_tokens=effective_tokens,
                prompt_tokens=prompt_tokens,
                model_name=selected_model,
                generation_time_seconds=gen_time,
            )
            await _clear_strikes(worker_id)
            result_src = "".join(accumulated) if accumulated else (json.dumps(full_json, sort_keys=True) if full_json else "")

            # ATOMIC terminal: worker-payout row + demand settlement in one txn.
            # Bill on a GRID count of the relayed/assembled output, never the
            # backend's `usage`. Reuses the reservation opened atomically at
            # reserve time; no-op in dry-run / for jobs without a reservation.
            from ._passthrough import completion_tokens as _pt_completion

            api_format = payload.get("api_format", "openai-chat")
            settle_result = await credits.record_and_settle(
                ledger_values=dict(
                    job_id=job_id,
                    worker_id=worker_id,
                    wallet=worker_info.get("wallet_address", ""),
                    model=selected_model,
                    job_type="text",
                    den=den_awarded,
                    output_units=effective_tokens,
                    duration=gen_time,
                    ttft=ttft,
                    prompt_hash=ledger_svc.text_hash(json.dumps(payload.get("request", {}), sort_keys=True)[:20000]),
                    result_hash=ledger_svc.content_hash(result_src[:20000]),
                ),
                completion_tokens=_pt_completion(api_format, accumulated, full_json),
            )
            if settle_result == "error":
                # Terminal didn't commit → surface an error and DON'T ack (return
                # False), so the caller leaves the queue message for stale-reclaim
                # instead of acking an unsettled job.
                logger.critical(f"Terminal settlement failed for raw job {job_id} — not acking")
                await token_stream.publish_error(job_id, "Settlement failed; please retry.")
                return False

            if settle_result in _PAID_SETTLE:
                # Committed → finalize: tell the client DONE, ack the worker, metrics.
                await token_stream.publish_done(job_id, usage=usage, full_json=full_json)
                await ws.send_json({"type": "ack", "id": job_id, "den": den_awarded})
                record_job_complete(tokens=effective_tokens, den=den_awarded, duration=gen_time)
                return True

            # 'stale_no_payout' / 'duplicate': terminally closed, NOT paid — no
            # success DONE, no den, no metrics. Worker ack den=0; return True so the
            # caller acks the queue.
            logger.warning(f"Raw job {job_id} closed without payout (settle={settle_result})")
            if settle_result == "stale_no_payout":
                await token_stream.publish_error(job_id, "Job could not be settled (already closed); please retry.")
            await ws.send_json({"type": "ack", "id": job_id, "den": 0})
            return True

        elif mtype == "pong":
            continue

        elif mtype == "error":
            message = msg.get("message", "Worker error")
            if msg.get("client_error"):
                logger.info(f"Client error on raw job {job_id}: {message[:200]}")
                await token_stream.publish_error(job_id, message, code=400)
            else:
                logger.error(f"Worker error on raw job {job_id}: {message}")
                await token_stream.publish_error(job_id, message, code=502)
            record_job_failed()
            await credits.release_job(job_id)  # terminal failure → refund the hold
            return True


def _merge_tool_call_deltas(acc: dict, deltas: list):
    """Accumulate streamed OpenAI tool_call fragments into full tool calls.

    Tool calls arrive split across deltas: the first carries the index + id +
    function name and a partial `arguments` string; later deltas append more
    `arguments`. We merge by `index` so the assembled non-streaming response
    has complete, parseable tool calls (the stream itself still relays each raw
    fragment to the client — this is only for the collected/DONE view + hashing).
    """
    for tc in deltas or []:
        idx = tc.get("index", 0)
        slot = acc.setdefault(idx, {"index": idx, "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


# record_and_settle outcomes that mean a PAID success (publish DONE, pay den).
# Everything else is either a retry ('error') or a terminally-closed-but-unpaid
# job ('stale_no_payout' / 'duplicate') that must NOT look like a paid completion.
_PAID_SETTLE = ("settled", "no_reservation")


def _gen_result(
    *,
    full_text="",
    full_reasoning="",
    tool_calls=None,
    usage=None,
    finish_reason="stop",
    grid_meta=None,
    metered=0,
    failed=False,
    client_error=None,
    ttft=None,
    worker_sig=None,
) -> dict:
    """Structured result of one worker generation. On SUCCESS the caller publishes
    DONE only AFTER settlement commits (so the client's completion signal, the
    queue ack, and the worker ack all gate on a committed terminal).

    `worker_sig` is the OPTIONAL signature the worker reported in its `done`
    frame — the caller verifies it against the worker's payout wallet before
    persisting (unverified/absent → the row is stored unsigned)."""
    return {
        "full_text": full_text,
        "full_reasoning": full_reasoning,
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
        "grid_meta": grid_meta,
        "metered": metered,
        "failed": failed,
        "client_error": client_error,
        "ttft": ttft,
        "worker_sig": worker_sig,
    }


async def _handle_worker_generation(ws: WebSocket, job: dict, worker_info: dict) -> dict:
    """Receive the worker's stream, relay it faithfully, and tee a copy.

    OBSERVE-mode passthrough: each `token` message carries the inference
    backend's raw `delta` (content / reasoning_content / tool_calls), which we
    republish UNTOUCHED for SSE clients. We simultaneously *tee* the stream —
    accumulating content/reasoning, assembling tool_calls, and capturing
    authoritative `usage` — so the grid can meter den, build the non-streaming
    reply, and hash the result without ever mutating what the client receives.

    Legacy workers (no `delta`, just `text`+`reasoning`) are still handled.

    Returns (full_text, token_count, failed, client_error, ttft). `token_count`
    prefers the backend's reported completion_tokens when available (more
    accurate than counting deltas). `ttft` is the time-to-first-token in seconds
    (None if no token arrived). On FAILURE (explicit error, or zero output)
    it publishes NOTHING and returns failed=True. `client_error` is a non-None
    message when the failure was the CALLER's fault (e.g. a 4xx from the backend
    over a malformed request) — the caller surfaces it to the client and skips
    the requeue/strike machinery (retrying would fail identically everywhere).
    """
    import time as _time

    job_id = job["job_id"]
    full_text = ""
    full_reasoning = ""
    tool_acc: dict = {}
    token_count = 0
    usage = None
    last_finish = None
    recv_start = _time.time()
    ttft: float | None = None
    cancel_sent = False
    last_cancel_poll = recv_start

    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=300)
        msg_type = msg.get("type")

        # Forward a client-initiated cancel to the worker (once), throttled so we
        # don't hit Redis on every token. The worker aborts its backend request
        # and sends a normal `done` with the partial output, which the branch
        # below settles as a (partial) success — no special cancel settlement.
        if not cancel_sent:
            _now = _time.time()
            if _now - last_cancel_poll >= 0.3:
                last_cancel_poll = _now
                if await token_stream.is_cancelled(job_id):
                    await ws.send_json({"type": "cancel", "id": job_id})
                    cancel_sent = True
                    logger.info(f"Forwarded client cancel to worker for job {job_id}")

        if msg_type == "token":
            if ttft is None:
                ttft = _time.time() - recv_start
            delta = msg.get("delta")
            if delta is not None:
                # Faithful path — accumulate a copy, relay the raw delta as-is.
                if delta.get("content"):
                    full_text += delta["content"]
                if delta.get("reasoning_content"):
                    full_reasoning += delta["reasoning_content"]
                if delta.get("tool_calls"):
                    _merge_tool_call_deltas(tool_acc, delta["tool_calls"])
                if msg.get("finish_reason"):
                    last_finish = msg["finish_reason"]
                token_count += 1
                await token_stream.publish_token(job_id, delta=delta, finish_reason=msg.get("finish_reason"))
            else:
                # Legacy path — separate text/reasoning channels.
                text = msg.get("text", "")
                is_reasoning = bool(msg.get("reasoning", False))
                if is_reasoning:
                    full_reasoning += text
                else:
                    full_text += text
                token_count += 1
                await token_stream.publish_token(job_id, text, reasoning=is_reasoning)

        elif msg_type == "done":
            # `cancelled` means the worker aborted its backend request because we
            # forwarded a client cancel. The partial output (possibly empty) is a
            # legitimate terminal — settle it as a partial success, never a worker
            # failure (no strike, no requeue).
            was_cancelled = bool(msg.get("cancelled"))
            # Use the worker's final full_text ONLY when it's non-empty. A worker
            # that streamed deltas but sends an empty full_text in `done` must not
            # wipe the grid-witnessed stream we accumulated (that's what produced
            # the misleading sha256("") result hashes). Non-streaming workers send
            # the whole output here (truthy → used); partial/cancelled output stays
            # whatever we actually relayed.
            full_text = msg.get("full_text") or full_text
            full_reasoning = msg.get("full_reasoning") or full_reasoning
            usage = msg.get("usage") or usage
            # OPTIONAL worker signature over the output commitment (Part B). Just
            # carried here; verified against the payout wallet at settle time.
            reported_worker_sig = msg.get("worker_sig")
            tool_calls = [tool_acc[i] for i in sorted(tool_acc)] if tool_acc else None
            finish_reason = msg.get("finish_reason") or last_finish or ("tool_calls" if tool_calls else "stop")

            # An empty completion (no content, no reasoning, no tool calls) is a
            # silent backend failure, not a success — don't pay for it or hand
            # the client a blank reply. A cancelled job is exempt: stopping early
            # with little/no output is expected, not a fault.
            produced_output = bool((full_text or "").strip() or (full_reasoning or "").strip() or tool_calls or token_count)
            if not produced_output and not was_cancelled:
                logger.warning(f"Worker returned EMPTY completion for job {job_id} — treating as failure")
                return _gen_result(full_text=full_text, metered=0, failed=True, ttft=ttft)

            # Provenance the API surfaces with the reply: who ran it + how fast.
            # tokens_per_s is DECODE throughput (output tokens / generation time
            # AFTER the first token) — i.e. it excludes TTFT/prefill, which is the
            # comparable industry number. Dividing by full wall-clock (incl. ttft)
            # understates t/s badly on prompts with long prefill / short outputs.
            gen_elapsed = _time.time() - recv_start
            decode_s = gen_elapsed - (ttft or 0.0)  # first-token → last-token
            if decode_s <= 0:
                decode_s = gen_elapsed  # 1-token / sub-tick fallback
            metered_now = (
                usage.get("completion_tokens") if usage and isinstance(usage.get("completion_tokens"), int) else token_count
            ) or 0
            grid_meta = {
                "worker": worker_info.get("name", ""),
                "gen_time": round(gen_elapsed, 2),
                "ttft": round(ttft, 2) if ttft is not None else None,
                "tokens_per_s": round(metered_now / decode_s, 1) if decode_s > 0 and metered_now else None,
            }
            # NOTE: DONE is published by the CALLER, only after settlement commits —
            # so the client never gets a success terminator on an unsettled job.

            # Prefer the backend's authoritative completion_tokens for metering;
            # fall back to the delta count. (The den path still caps this against
            # a server-side tiktoken count + requested max, so a worker can't
            # inflate it.)
            metered = token_count
            if usage and isinstance(usage.get("completion_tokens"), int):
                metered = usage["completion_tokens"]
            return _gen_result(
                full_text=full_text,
                full_reasoning=full_reasoning,
                tool_calls=tool_calls,
                usage=usage,
                finish_reason=finish_reason,
                grid_meta=grid_meta,
                metered=metered,
                failed=False,
                ttft=ttft,
                worker_sig=reported_worker_sig,
            )

        elif msg_type == "pong":
            continue

        elif msg_type == "error":
            message = msg.get("message", "Worker error")
            if msg.get("client_error"):
                # Deterministic caller fault (bad request); not the worker's fault.
                logger.info(f"Client error on job {job_id}: {message[:200]}")
                return _gen_result(full_text=full_text, metered=token_count, failed=True, client_error=message, ttft=ttft)
            logger.error(f"Worker error on job {job_id}: {message}")
            return _gen_result(full_text=full_text, metered=token_count, failed=True, ttft=ttft)
