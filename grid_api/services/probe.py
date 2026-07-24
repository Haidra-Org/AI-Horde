# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verification probes — coordinator-run canary quality checks (Phase 1).

See docs/VERIFICATION_PROBES.md. The coordinator ("validator zero") periodically
sends a known-answer canary to an online model through the NORMAL worker path,
grades the reply, and records the verdict to grid_validator_attestations as
EVIDENCE ONLY — no routing / reward / strike / slash effect whatsoever. Dark by
default (GRID_PROBE_ENABLED=0). The scoring engine here is who-agnostic: a future
staked validator node calls the identical make_canary()/grade()/record against
the identical table, just decentralized and signed.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from uuid import uuid4

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import utcnow, validator_attestations
from . import job_queue, token_stream

logger = logging.getLogger("grid_api.probe")

# Dark-mode flag — mirrors GRID_CHARGING_ENABLED. OFF = the loop is dormant; ON =
# it collects EVIDENCE ONLY (verdicts still have zero economic/routing effect).
PROBE_ENABLED = os.getenv("GRID_PROBE_ENABLED", "0").lower() in ("1", "true", "yes", "on")
PROBE_INTERVAL = int(os.getenv("GRID_PROBE_INTERVAL", "300"))   # seconds between probes
PROBE_MAX_TOKENS = int(os.getenv("GRID_PROBE_MAX_TOKENS", "24"))
PROBE_TIMEOUT = int(os.getenv("GRID_PROBE_TIMEOUT", "60"))       # idle timeout for one probe

# Coordinator ("validator zero") signing key — makes each attestation
# tamper-evident + attributable. Unset → attestations are recorded unsigned
# (graceful). Future staked validators sign with their OWN keys against the same
# canonical digest, so scorecards can weight by signer once quorum exists.
_SIGNING_KEY = os.getenv("GRID_PROBE_SIGNING_KEY", "").strip()


def _sign(digest: dict) -> tuple[str | None, str | None, str]:
    """Sign a canonical attestation digest. Returns (signature, signer_address,
    status). ECDSA/EIP-191 over the sorted-JSON digest."""
    if not _SIGNING_KEY:
        return None, None, "unsigned"
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        canonical = json.dumps(digest, sort_keys=True, default=str)
        acct = Account.from_key(_SIGNING_KEY)
        sig = Account.sign_message(encode_defunct(text=canonical), acct.key).signature.hex()
        return ("0x" + sig.removeprefix("0x")), acct.address, "signed"
    except Exception:
        logger.warning("attestation signing failed", exc_info=True)
        return None, None, "unsigned"


# ── Canary bank ──────────────────────────────────────────────────────────────
# Each canary embeds a fresh random tag in the prompt (defeats caching / canned
# answers) and grades deterministically. Answers must be unambiguous at temp=0.

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _c_arithmetic() -> dict:
    a, b = secrets.randbelow(900) + 100, secrets.randbelow(90) + 10
    ans = str(a + b)
    prompt = (f"Verification check {secrets.token_hex(4)}. "
              f"What is {a} + {b}? Reply with ONLY the number, nothing else.")

    def grade(text: str):
        digits = re.findall(r"-?\d+", text or "")
        if not digits:
            return ("inconclusive", 0.0)
        return ("pass", 1.0) if digits[0] == ans else ("fail", 0.0)

    return {"kind": "arithmetic", "prompt": prompt, "expected": ans, "grade": grade}


def _c_capital() -> dict:
    pairs = [("France", "paris"), ("Japan", "tokyo"), ("Canada", "ottawa"),
             ("Egypt", "cairo"), ("Brazil", "brasilia"), ("Kenya", "nairobi"),
             ("Norway", "oslo"), ("Peru", "lima")]
    country, cap = pairs[secrets.randbelow(len(pairs))]
    prompt = (f"Verification check {secrets.token_hex(4)}. "
              f"What is the capital city of {country}? Reply with ONLY the city name.")

    def grade(text: str):
        if not (text or "").strip():
            return ("inconclusive", 0.0)
        return ("pass", 1.0) if cap in _norm(text) else ("fail", 0.0)

    return {"kind": "capital", "prompt": prompt, "expected": cap, "grade": grade}


def _c_hard_arithmetic() -> dict:
    """Capability-tiered canary: 2-digit × 2-digit multiplication. Small/cheap
    models routinely botch multi-digit multiplication while the larger models a
    worker CLAIMS to run get it right — so a `fail` here is a signal the worker
    may have swapped in a smaller model than advertised (a model-downgrade
    cheat), not just a bad sample. Evidence only in V0."""
    a, b = secrets.randbelow(90) + 10, secrets.randbelow(90) + 10
    ans = str(a * b)
    prompt = (f"Verification check {secrets.token_hex(4)}. "
              f"What is {a} multiplied by {b}? Reply with ONLY the number, nothing else.")

    def grade(text: str):
        digits = re.findall(r"-?\d+", text or "")
        if not digits:
            return ("inconclusive", 0.0)
        return ("pass", 1.0) if digits[-1] == ans else ("fail", 0.0)

    return {"kind": "hard_arithmetic", "prompt": prompt, "expected": ans, "grade": grade}


_CANARIES = [_c_arithmetic, _c_capital, _c_hard_arithmetic]


def make_canary() -> dict:
    """A fresh, nonce-tagged canary with a deterministic grader."""
    return _CANARIES[secrets.randbelow(len(_CANARIES))]()


# ── Dispatch one probe through the NORMAL worker path ────────────────────────

async def _run_job(model: str, prompt: str) -> tuple[str, str | None]:
    """Send one probe prompt as a normal text job; return (reply_text, worker_id)."""
    job_id = str(uuid4())
    request_body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,          # deterministic so grading is stable
        "stream": True,            # workers always stream
        "seed": secrets.randbelow(2**53),
    }
    payload = {
        "request": request_body,
        "api_format": "openai-chat",
        "prompt": prompt,
        "max_length": PROBE_MAX_TOKENS,
        "temperature": 0,
        "top_p": 1.0,
    }
    await job_queue.submit_job(job_id, payload, [model])

    content = ""
    worker_id = None
    async for data in token_stream.subscribe_tokens(job_id, timeout=PROBE_TIMEOUT):
        if data.get("text") == token_stream.DONE_SENTINEL:
            if data.get("error"):
                raise RuntimeError(str(data["error"])[:200])
            content = data.get("full_text") or content
            worker_id = (data.get("grid") or {}).get("worker")
            break
        delta = data.get("delta")
        if delta is not None:
            content += delta.get("content") or ""
        elif not data.get("reasoning"):
            content += data.get("text", "")
    return content, worker_id


async def _record(model: str, worker_id: str | None, canary: dict,
                  verdict: str, score: float, latency_ms: float, got: str) -> None:
    """Write ONE attestation. validator_wallet=None + signature_status='unsigned'
    marks it as coordinator ('validator zero') evidence — no economic weight."""
    rec = {
        "account_id": None,
        "validator_wallet": None,
        "worker_id": (worker_id or "")[:64] or None,
        "model": (model or "")[:255],
        "modality": "text",
        "capability": None,
        "canary_kind": canary["kind"],
        "nonce": None,
        "verdict": verdict,
        "score": score,
        "latency_ms": int(latency_ms),
        "epoch": None,
        "signature": None,
        "signature_status": "unsigned",
        "payload": {"prompt": canary["prompt"], "expected": canary["expected"],
                    "got": (got or "")[:500]},
        "created": utcnow(),
    }
    # Idempotency hash — the nonce-tagged prompt makes every canary unique.
    canonical = json.dumps(
        {k: rec[k] for k in ("worker_id", "model", "canary_kind", "verdict", "payload")},
        sort_keys=True, default=str,
    )
    rec["attestation_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    # Sign the attestation (tamper-evident + attributable to validator zero).
    signature, signer, status = _sign(
        {"hash": rec["attestation_hash"], "worker_id": rec["worker_id"],
         "model": rec["model"], "verdict": rec["verdict"], "score": rec["score"]}
    )
    rec["signature"] = signature
    rec["validator_wallet"] = signer
    rec["signature_status"] = status
    async with await new_session() as session:
        await session.execute(sa.insert(validator_attestations).values(**rec))
        await session.commit()


async def run_once() -> str | None:
    """One probe cycle: pick an online text model, canary it, grade, record.
    Returns the verdict (or None if no model was available). Never raises."""
    # Lazy import avoids a routers<->services import cycle at module load.
    from ..routers.worker_ws import get_available_models
    models = await get_available_models(job_type="text")
    if not models:
        return None
    model = models[secrets.randbelow(len(models))]
    canary = make_canary()
    t0 = time.monotonic()
    worker_id = None
    got = ""
    try:
        got, worker_id = await _run_job(model, canary["prompt"])
        verdict, score = canary["grade"](got)
    except Exception as e:
        logger.info("probe dispatch failed model=%s: %s", model, e)
        verdict, score = "inconclusive", 0.0
    latency_ms = (time.monotonic() - t0) * 1000
    try:
        await _record(model, worker_id, canary, verdict, score, latency_ms, got)
    except Exception:
        logger.warning("probe attestation write failed (table missing?)", exc_info=True)
    logger.info("probe model=%s worker=%s kind=%s verdict=%s score=%.2f %dms",
                model, worker_id, canary["kind"], verdict, score, int(latency_ms))
    return verdict


async def _acquire_leader(ttl: int) -> bool:
    """Redis leader gate so ONLY ONE uvicorn worker probes per interval window
    (the loop runs in every worker under `--workers N`; without this, N probes
    fire each interval). SET NX + EX ttl: whoever grabs the key this window runs;
    the others skip until it expires (~next interval). Fails CLOSED (skip) on a
    Redis error to avoid the N-way duplication it exists to prevent."""
    from ..redis_client import get_redis
    try:
        return bool(await get_redis().set("grid:probe:leader", "1", nx=True, ex=max(ttl - 5, 30)))
    except Exception:
        return False


async def probe_loop() -> None:
    """Background sampling loop. Dormant unless GRID_PROBE_ENABLED. Evidence only."""
    if not PROBE_ENABLED:
        logger.info("verification probes disabled (GRID_PROBE_ENABLED=0) — dormant")
        return
    logger.info("verification probes ENABLED (dark / evidence-only) interval=%ds", PROBE_INTERVAL)
    # Small initial delay so a restart storm of probes doesn't hit workers at once.
    await asyncio.sleep(min(30, PROBE_INTERVAL))
    while True:
        try:
            if await _acquire_leader(PROBE_INTERVAL):
                await run_once()
            # else: another uvicorn worker holds the probe lease this window.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("probe loop iteration error", exc_info=True)
        await asyncio.sleep(PROBE_INTERVAL)
