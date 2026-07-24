# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validator assignment, attestation, and scorecard storage.

This module is the boundary between preview validator evidence and
assignment-bound evidence. It deliberately does not route production traffic,
reward validators, slash workers, move credits, or write worker payout ledger
rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import validator_assignments as assignments_t
from ..v2.schema import validator_attestations as attestations_t
from ..v2.schema import workers as workers_t

logger = logging.getLogger("grid_api.validators")

VALID_VERDICTS = {"healthy", "slow", "failed"}
VERDICT_SCORE = {"healthy": 1.0, "slow": 0.75, "failed": 0.0}
VALID_AUTHORITY = {"all", "preview", "authoritative"}
MAX_PAYLOAD_BYTES = 64 * 1024
ASSIGNMENT_TTL_SECONDS = int(os.getenv("VALIDATOR_ASSIGNMENT_TTL_SECONDS", "900") or 900)
PROBE_TIMEOUT_SECONDS = int(os.getenv("VALIDATOR_PROBE_TIMEOUT_SECONDS", "180") or 180)
PROBE_LATENCY_BUDGET_SECONDS = int(os.getenv("VALIDATOR_PROBE_LATENCY_BUDGET_SECONDS", "30") or 30)
QUORUM_MIN = max(1, int(os.getenv("VALIDATOR_QUORUM_MIN", "1") or 1))
# Canary answers are short, but reasoning models (gpt-oss/qwen3/Gemma) spend
# tokens "thinking" before the final answer. A tight cap (was 32) gets fully
# consumed by the reasoning phase → empty answer → probe returns no text →
# the model can't be scored. Give enough room to think AND answer. Overridable.
PROBE_MAX_TOKENS = max(32, int(os.getenv("VALIDATOR_PROBE_MAX_TOKENS", "512") or 512))

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIG_RE = re.compile(r"^(0x)?[0-9a-fA-F]{130}$")


class AttestationError(ValueError):
    """Raised when a submitted attestation is malformed or unverifiable."""


class AssignmentError(ValueError):
    """Raised when an assignment/probe request is invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_obj(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _attestation_hash(payload: dict[str, Any], signature: str | None) -> str:
    body = _canonical({"payload": payload, "signature": signature or None})
    return hashlib.sha256(body.encode()).hexdigest()


def _payload_size(payload: dict[str, Any]) -> int:
    return len(_canonical(payload).encode())


def _string(payload: dict[str, Any], key: str, max_len: int) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if len(value) > max_len:
        raise AttestationError(f"payload.{key} is too long")
    return value


def _int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError) as exc:
        raise AttestationError(f"payload.{key} must be an integer") from exc
    if ivalue < 0:
        raise AttestationError(f"payload.{key} must be non-negative")
    return ivalue


def _float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AttestationError(f"payload.{key} must be a number") from exc


def _normalize_signature(signature: str | None) -> str | None:
    if signature is None or signature == "":
        return None
    if not isinstance(signature, str):
        raise AttestationError("signature must be a hex string")
    sig = signature.strip()
    if not _SIG_RE.match(sig):
        raise AttestationError("signature must be a 65-byte hex signature")
    return sig if sig.startswith("0x") else f"0x{sig}"


def _validator_wallet(payload: dict[str, Any]) -> str | None:
    wallet = _string(payload, "validator", 42) or _string(payload, "validator_wallet", 42)
    if not wallet:
        return None
    if not _ADDR_RE.match(wallet):
        raise AttestationError("payload.validator must be a 20-byte 0x hex address")
    return wallet.lower()


def _signature_status(payload: dict[str, Any], signature: str | None) -> str:
    if not signature:
        return "unsigned"

    wallet = _validator_wallet(payload)
    if not wallet:
        raise AttestationError("signature requires payload.validator")

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:  # pragma: no cover - dependency exists in production requirements
        return "unverified:dependency"

    try:
        recovered = Account.recover_message(
            encode_defunct(text=_canonical(payload)),
            signature=signature,
        )
    except Exception as exc:
        raise AttestationError("signature verification failed") from exc
    if recovered.lower() != wallet.lower():
        raise AttestationError("signature does not match validator wallet")
    return "verified"


def _make_text_challenge(round_index: int) -> dict[str, Any]:
    if round_index % 2 == 0:
        token = secrets.token_hex(8).upper()
        prompt = f"Reply with exactly this token and nothing else: {token}"
        expected = token
        kind = "echo"
    else:
        a = secrets.randbelow(80) + 11
        b = secrets.randbelow(80) + 11
        if secrets.randbelow(2):
            prompt = f"What is {a} + {b}? Reply with only the number."
            expected = str(a + b)
            kind = "math.add"
        else:
            prompt = f"What is {a} * {b}? Reply with only the number."
            expected = str(a * b)
            kind = "math.mul"
    return {
        "kind": kind,
        "prompt": prompt,
        "expected": expected,
        "expected_hash": _hash_text(expected),
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
    }


def _strip_think(text: str) -> str:
    return re.sub(
        r"<think(?:ing)?>.*?</think(?:ing)?>",
        "",
        text or "",
        flags=re.DOTALL,
    ).strip()


def _strip_wrapping_quotes(text: str) -> str:
    answer = (text or "").strip()
    wrappers = (("`", "`"), ('"', '"'), ("'", "'"))
    changed = True
    while changed and len(answer) >= 2:
        changed = False
        for left, right in wrappers:
            if answer.startswith(left) and answer.endswith(right):
                answer = answer[1:-1].strip()
                changed = True
                break
    return answer


def _contains_expected(answer: str, expected: str) -> bool:
    if re.fullmatch(r"-?\d+", expected):
        return re.search(rf"(?<![a-z0-9-]){re.escape(expected)}(?![a-z0-9])", answer) is not None
    return expected.lower() in answer.lower()


def _score_text_challenge(challenge: dict[str, Any], text: str, latency_ms: int) -> str:
    expected = str(challenge.get("expected") or challenge.get("expect") or "")
    if not expected:
        return "failed"
    answer = _strip_think(text)
    if not answer:
        return "failed"
    if challenge.get("kind") == "echo":
        correct = _strip_wrapping_quotes(answer).lower() == expected.lower()
    else:
        correct = _contains_expected(answer.lower(), expected.lower())
    if not correct:
        return "failed"
    if latency_ms > PROBE_LATENCY_BUDGET_SECONDS * 1000:
        return "slow"
    return "healthy"


def _assignment_to_dict(
    row,
    *,
    include_challenge: bool = True,
    include_grid_nonce: bool = True,
) -> dict[str, Any]:
    out = {
        "assignment_id": row["id"],
        "target_worker_id": row["target_worker_id"],
        "target_worker_name": row["target_worker_name"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "scoring_policy_id": row["scoring_policy_id"],
        "status": row["status"],
        "quorum_status": row["quorum_status"],
        "quorum_outcome": row["quorum_outcome"],
        "probe_status": row["probe_status"],
        "probe_job_id": row["probe_job_id"],
        "created": row["created"].isoformat() if row["created"] else None,
        "expires": row["expires"].isoformat() if row["expires"] else None,
        "probed": row["probed"].isoformat() if row["probed"] else None,
        "finalized": row["finalized"].isoformat() if row["finalized"] else None,
    }
    if include_grid_nonce:
        out["grid_nonce"] = row["grid_nonce"]
    if include_challenge:
        out["challenge"] = row["challenge"]
    return out


async def _finalize_due_assignments(session) -> None:
    now = _now()
    rows = (
        await session.execute(
            sa.select(
                assignments_t.c.id,
                assignments_t.c.quorum_status,
                assignments_t.c.quorum_outcome,
            ).where(
                assignments_t.c.expires < now,
                assignments_t.c.quorum_status != "finalized",
            )
        )
    ).mappings().all()
    for row in rows:
        outcome = row["quorum_outcome"] or (
            row["quorum_status"] if row["quorum_status"] in ("accepted", "disputed") else "no_evidence"
        )
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == row["id"])
            .values(
                status="finalized",
                quorum_status="finalized",
                quorum_outcome=outcome,
                finalized=now,
            )
        )


async def issue_assignments(
    *,
    account_id,
    validator_wallet: str | None,
    active_workers: list[dict[str, Any]],
    limit: int = 5,
    modality: str = "text",
) -> dict[str, Any]:
    """Return pending assignments for this validator, creating more if needed."""
    safe_limit = max(1, min(int(limit), 25))
    if modality != "text":
        raise AssignmentError("only text assignments are enabled in this rollout")

    now = _now()
    expires = now + timedelta(seconds=ASSIGNMENT_TTL_SECONDS)
    wallet = validator_wallet.lower() if validator_wallet and _ADDR_RE.match(validator_wallet) else None

    async with await new_session() as session:
        await _finalize_due_assignments(session)

        own_worker_rows = (
            await session.execute(
                sa.select(workers_t.c.id).where(workers_t.c.account_id == account_id)
            )
        ).all()
        own_worker_ids = {str(r[0]) for r in own_worker_rows}

        existing = (
            await session.execute(
                sa.select(assignments_t)
                .where(
                    assignments_t.c.account_id == account_id,
                    assignments_t.c.quorum_status == "pending",
                    assignments_t.c.expires >= now,
                )
                .order_by(assignments_t.c.created.asc())
                .limit(safe_limit)
            )
        ).mappings().all()
        existing_keys = {(r["target_worker_id"], r["model"]) for r in existing}
        rows = list(existing)

        for worker in active_workers:
            if len(rows) >= safe_limit:
                break
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            worker_name = str(worker.get("name") or "")
            if not worker_id or not worker_name or worker_id in own_worker_ids:
                continue
            if modality not in (worker.get("job_types") or ["text"]):
                continue
            models = [m for m in (worker.get("models") or []) if isinstance(m, str) and m]
            if not models:
                continue
            model = models[0]
            if (worker_id, model) in existing_keys:
                continue
            challenge = _make_text_challenge(len(rows))
            assignment_id = f"asg_{uuid4().hex}"
            grid_nonce = secrets.token_urlsafe(24)
            values = {
                "id": assignment_id,
                "account_id": account_id,
                "validator_wallet": wallet,
                "grid_nonce": grid_nonce,
                "target_worker_id": worker_id,
                "target_worker_name": worker_name,
                "model": model,
                "modality": "text",
                "capability": "text.basic.v1",
                "canary_kind": challenge["kind"],
                "scoring_policy_id": "text.basic.generated.v1",
                "challenge": challenge,
                "status": "pending",
                "quorum_status": "pending",
                "quorum_outcome": None,
                "probe_job_id": None,
                "probe_status": "not_started",
                "created": now,
                "expires": expires,
                "probed": None,
                "finalized": None,
            }
            await session.execute(sa.insert(assignments_t).values(**values))
            rows.append(values)
            existing_keys.add((worker_id, model))

        await session.commit()

    return {
        "assignments": [_assignment_to_dict(r) for r in rows[:safe_limit]],
        "count": min(len(rows), safe_limit),
        "targeted_probe_enabled": True,
        "quorum": await assignment_health(account_id=account_id),
        "economic_effect": "none",
    }


async def assignment_health(*, account_id=None, limit: int = 25) -> dict[str, Any]:
    """Return assignment/quorum health without exposing raw evidence."""
    safe_limit = max(1, min(int(limit), 100))
    async with await new_session() as session:
        await _finalize_due_assignments(session)
        await session.commit()
        base = sa.select(assignments_t.c.quorum_status, sa.func.count().label("count")).group_by(
            assignments_t.c.quorum_status
        )
        if account_id is not None:
            base = base.where(assignments_t.c.account_id == account_id)
        quorum_counts = {
            row["quorum_status"]: int(row["count"])
            for row in (await session.execute(base)).mappings().all()
        }
        probe_q = sa.select(assignments_t.c.probe_status, sa.func.count().label("count")).group_by(
            assignments_t.c.probe_status
        )
        if account_id is not None:
            probe_q = probe_q.where(assignments_t.c.account_id == account_id)
        probe_counts = {
            row["probe_status"]: int(row["count"])
            for row in (await session.execute(probe_q)).mappings().all()
        }
        recent_q = (
            sa.select(assignments_t)
            .order_by(assignments_t.c.created.desc())
            .limit(safe_limit)
        )
        if account_id is not None:
            recent_q = recent_q.where(assignments_t.c.account_id == account_id)
        recent = (await session.execute(recent_q)).mappings().all()
    return {
        "quorum": {
            "pending": quorum_counts.get("pending", 0),
            "accepted": quorum_counts.get("accepted", 0),
            "disputed": quorum_counts.get("disputed", 0),
            "finalized": quorum_counts.get("finalized", 0),
        },
        "probe": probe_counts,
        "recent": [
            _assignment_to_dict(r, include_challenge=False, include_grid_nonce=False)
            for r in recent
        ],
        "economic_effect": "none",
    }


def _normalize(payload: dict[str, Any], signature: str | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AttestationError("payload must be an object")
    if _payload_size(payload) > MAX_PAYLOAD_BYTES:
        raise AttestationError("payload is too large")

    verdict = _string(payload, "verdict", 16)
    if verdict not in VALID_VERDICTS:
        raise AttestationError("payload.verdict must be healthy, slow, or failed")

    sig = _normalize_signature(signature)
    validator_wallet = _validator_wallet(payload)
    assignment_source = _string(payload, "assignment_source", 32)
    wants_authority = assignment_source == "grid" or bool(_string(payload, "grid_nonce", 128))
    if assignment_source == "grid" and not _string(payload, "assignment_id", 96):
        raise AttestationError("grid assignment evidence requires payload.assignment_id")
    if wants_authority and not _string(payload, "grid_nonce", 128):
        raise AttestationError("authoritative evidence requires payload.grid_nonce")
    if wants_authority and not _string(payload, "evidence_hash", 64):
        raise AttestationError("authoritative evidence requires payload.evidence_hash")

    return {
        "attestation_hash": _attestation_hash(payload, sig),
        "validator_wallet": validator_wallet,
        "assignment_id": _string(payload, "assignment_id", 96) if wants_authority else None,
        "grid_nonce": _string(payload, "grid_nonce", 128) if wants_authority else None,
        "evidence_hash": _string(payload, "evidence_hash", 64) if wants_authority else None,
        "authority": "authoritative" if wants_authority else "preview",
        "quorum_status": "pending",
        "worker_id": _string(payload, "worker_id", 64) or _string(payload, "worker", 64),
        "model": _string(payload, "model", 255),
        "modality": _string(payload, "modality", 16),
        "capability": _string(payload, "capability", 128),
        "canary_kind": _string(payload, "canary_kind", 64) or _string(payload, "challenge_type", 64),
        "nonce": _string(payload, "nonce", 128),
        "verdict": verdict,
        "score": _float(payload, "score"),
        "latency_ms": _int(payload, "latency_ms"),
        "epoch": _string(payload, "epoch", 64),
        "signature": sig,
        "signature_status": _signature_status(payload, sig),
        "payload": payload,
    }


async def _verify_assignment_in_session(session, *, account_id, row: dict[str, Any]) -> dict[str, Any]:
    assignment_id = row.get("assignment_id")
    grid_nonce = row.get("grid_nonce")
    assignment = (
        await session.execute(
            sa.select(assignments_t).where(assignments_t.c.id == assignment_id)
        )
    ).mappings().first()
    if not assignment:
        raise AttestationError("assignment_id is not a Grid-issued assignment")
    if assignment["account_id"] != account_id:
        raise AttestationError("assignment does not belong to this validator account")
    if assignment["grid_nonce"] != grid_nonce:
        raise AttestationError("grid_nonce does not match assignment")
    if assignment["expires"] and _aware(assignment["expires"]) < _now():
        raise AttestationError("assignment has expired")
    if assignment["probe_status"] != "completed":
        raise AttestationError("assignment probe has not completed")
    if not assignment["probe_evidence_hash"]:
        raise AttestationError("assignment is missing probe evidence")
    if not assignment["probe_verdict"]:
        raise AttestationError("assignment is missing probe verdict")
    if row.get("evidence_hash") != assignment["probe_evidence_hash"]:
        raise AttestationError("payload.evidence_hash does not match assignment probe")
    checks = {
        "worker_id": assignment["target_worker_id"],
        "model": assignment["model"],
        "modality": assignment["modality"],
        "capability": assignment["capability"],
        "canary_kind": assignment["canary_kind"],
    }
    for key, expected in checks.items():
        if row.get(key) and row[key] != expected:
            raise AttestationError(f"payload.{key} does not match assignment")
        row[key] = expected
    row["score"] = VERDICT_SCORE[row["verdict"]]
    return assignment


async def _update_quorum_in_session(session, assignment_id: str) -> str:
    assignment = (
        await session.execute(
            sa.select(assignments_t.c.probe_verdict).where(assignments_t.c.id == assignment_id)
        )
    ).mappings().first()
    probe_verdict = assignment["probe_verdict"] if assignment else None
    rows = (
        await session.execute(
            sa.select(attestations_t.c.verdict, sa.func.count().label("count"))
            .where(
                attestations_t.c.assignment_id == assignment_id,
                attestations_t.c.authority == "authoritative",
            )
            .group_by(attestations_t.c.verdict)
        )
    ).mappings().all()
    total = sum(int(r["count"]) for r in rows)
    if total <= 0:
        status = "pending"
        outcome = None
    elif probe_verdict and any(r["verdict"] != probe_verdict for r in rows):
        status = "disputed"
        outcome = "disputed"
    elif total >= QUORUM_MIN:
        status = "accepted"
        outcome = probe_verdict or rows[0]["verdict"]
    else:
        status = "pending"
        outcome = None
    await session.execute(
        sa.update(assignments_t)
        .where(assignments_t.c.id == assignment_id)
        .values(status=status, quorum_status=status, quorum_outcome=outcome)
    )
    await session.execute(
        sa.update(attestations_t)
        .where(attestations_t.c.assignment_id == assignment_id)
        .values(quorum_status=status)
    )
    return status


async def record_attestation(
    *,
    account_id,
    payload: dict[str, Any],
    signature: str | None = None,
) -> dict[str, Any]:
    """Store one validator attestation idempotently.

    Preview attestations are preserved for rollout/debugging. Authoritative
    attestations require a verified Grid assignment id + nonce and update the
    assignment quorum state. No route/reward/slash side effects happen here.
    """
    row = _normalize(payload, signature)
    row["account_id"] = account_id
    row["created"] = _now()

    async with await new_session() as session:
        if row["authority"] == "authoritative":
            await _verify_assignment_in_session(session, account_id=account_id, row=row)
        try:
            result = await session.execute(sa.insert(attestations_t).values(**row))
            attestation_id = result.inserted_primary_key[0] if result.inserted_primary_key else None
            status = "accepted"
        except IntegrityError:
            await session.rollback()
            existing = (
                await session.execute(
                    sa.select(
                        attestations_t.c.id,
                        attestations_t.c.assignment_id,
                        attestations_t.c.authority,
                    ).where(attestations_t.c.attestation_hash == row["attestation_hash"])
                )
            ).first()
            attestation_id = existing[0] if existing else None
            row["assignment_id"] = existing[1] if existing else row.get("assignment_id")
            row["authority"] = existing[2] if existing else row.get("authority")
            status = "duplicate"
        quorum_status = "preview"
        if row["authority"] == "authoritative" and row.get("assignment_id"):
            quorum_status = await _update_quorum_in_session(session, row["assignment_id"])
        await session.commit()

    logger.info(
        "validator attestation %s account=%s authority=%s verdict=%s model=%s assignment=%s",
        status,
        account_id,
        row["authority"],
        row["verdict"],
        row["model"] or "-",
        row.get("assignment_id") or "-",
    )
    return {
        "status": status,
        "id": attestation_id,
        "attestation_hash": row["attestation_hash"],
        "signature_status": row["signature_status"],
        "authority": row["authority"],
        "assignment_id": row.get("assignment_id"),
        "quorum_status": quorum_status,
    }


async def scorecards(
    *,
    limit: int = 100,
    since_hours: int = 168,
    worker_id: str | None = None,
    model: str | None = None,
    authority: str = "all",
) -> dict[str, Any]:
    """Return aggregate validator evidence without economic side effects."""
    safe_limit = max(1, min(int(limit), 500))
    safe_since = max(1, min(int(since_hours), 24 * 90))
    mode = authority if authority in VALID_AUTHORITY else "all"
    cutoff = _now() - timedelta(hours=safe_since)

    healthy = sa.func.sum(sa.case((attestations_t.c.verdict == "healthy", 1), else_=0))
    slow = sa.func.sum(sa.case((attestations_t.c.verdict == "slow", 1), else_=0))
    failed = sa.func.sum(sa.case((attestations_t.c.verdict == "failed", 1), else_=0))

    q = (
        sa.select(
            attestations_t.c.authority,
            attestations_t.c.quorum_status,
            attestations_t.c.worker_id,
            attestations_t.c.model,
            attestations_t.c.modality,
            attestations_t.c.capability,
            sa.func.count().label("total"),
            healthy.label("healthy"),
            slow.label("slow"),
            failed.label("failed"),
            sa.func.avg(attestations_t.c.latency_ms).label("avg_latency_ms"),
            sa.func.avg(attestations_t.c.score).label("avg_score"),
            sa.func.min(attestations_t.c.created).label("first_seen"),
            sa.func.max(attestations_t.c.created).label("last_seen"),
        )
        .where(attestations_t.c.created >= cutoff)
        .group_by(
            attestations_t.c.authority,
            attestations_t.c.quorum_status,
            attestations_t.c.worker_id,
            attestations_t.c.model,
            attestations_t.c.modality,
            attestations_t.c.capability,
        )
        .order_by(sa.func.max(attestations_t.c.created).desc())
        .limit(safe_limit)
    )
    if mode != "all":
        q = q.where(attestations_t.c.authority == mode)
    if worker_id:
        q = q.where(attestations_t.c.worker_id == worker_id)
    if model:
        q = q.where(attestations_t.c.model == model)

    async with await new_session() as session:
        rows = (await session.execute(q)).mappings().all()

    items = []
    for row in rows:
        total = int(row["total"] or 0)
        failures = int(row["failed"] or 0)
        healthy_count = int(row["healthy"] or 0)
        slow_count = int(row["slow"] or 0)
        subject_type = "worker" if row["worker_id"] else "model"
        subject_id = row["worker_id"] or row["model"] or "unknown"
        items.append({
            "subject_type": subject_type,
            "subject_id": subject_id,
            "worker_id": row["worker_id"],
            "model": row["model"],
            "modality": row["modality"],
            "capability": row["capability"],
            "authority": row["authority"],
            "quorum_status": row["quorum_status"],
            "total": total,
            "healthy": healthy_count,
            "slow": slow_count,
            "failed": failures,
            "healthy_rate": (healthy_count / total) if total else 0.0,
            "slow_rate": (slow_count / total) if total else 0.0,
            "failed_rate": (failures / total) if total else 0.0,
            "avg_latency_ms": (
                float(row["avg_latency_ms"]) if row["avg_latency_ms"] is not None else None
            ),
            "avg_score": float(row["avg_score"]) if row["avg_score"] is not None else None,
            "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
        })

    return {
        "items": items,
        "count": len(items),
        "window_hours": safe_since,
        "limit": safe_limit,
        "authority": mode,
        "filters": {
            "worker_id": worker_id,
            "model": model,
        },
        "economic_effect": "none",
    }


async def probe_assignment(*, account_id, assignment_id: str) -> dict[str, Any]:
    """Run a stored assignment against exactly its target worker.

    This queues a hard-targeted validator probe job and waits for the worker
    response. It never reserves credits, writes ledger rows, pays den, strikes,
    or slashes. The caller can use the returned hashes in a signed attestation.
    """
    from . import job_queue, token_stream

    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(assignments_t).where(assignments_t.c.id == assignment_id)
            )
        ).mappings().first()
        if not row:
            raise AssignmentError("assignment not found")
        if row["account_id"] != account_id:
            raise AssignmentError("assignment does not belong to this validator account")
        if row["expires"] and _aware(row["expires"]) < _now():
            raise AssignmentError("assignment has expired")
        if row["modality"] != "text":
            raise AssignmentError("only text probes are enabled in this rollout")

        challenge = row["challenge"] or {}
        prompt = str(challenge.get("prompt") or "")
        if not prompt:
            raise AssignmentError("assignment has no prompt")
        job_id = f"validator:{assignment_id}:{uuid4().hex}"
        payload = {
            "request": {
                "model": row["model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(challenge.get("max_tokens") or 32),
                "temperature": float(challenge.get("temperature") or 0),
                "stream": True,
            },
            "api_format": "openai-chat",
            "prompt": prompt,
            "max_length": int(challenge.get("max_tokens") or 32),
            "temperature": float(challenge.get("temperature") or 0),
            "_validator_probe": True,
            "_validator_assignment_id": assignment_id,
            "_validator_grid_nonce": row["grid_nonce"],
        }
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == assignment_id)
            .values(probe_job_id=job_id, probe_status="running", probed=_now())
        )
        await session.commit()

    started = _now()
    try:
        await job_queue.submit_job(
            job_id,
            payload,
            [row["model"]],
            job_type="text",
            preferred_worker=row["target_worker_name"],
            hard_target_worker=row["target_worker_name"],
        )
    except TypeError:
        # Tests or old monkeypatches may not yet accept the new keyword.
        await job_queue.submit_job(
            job_id,
            payload,
            [row["model"]],
            job_type="text",
            preferred_worker=row["target_worker_name"],
        )

    chunks: list[str] = []
    full_text = ""
    full_reasoning = ""
    grid_meta = None
    usage = None
    try:
        async for event in token_stream.subscribe_tokens(job_id, timeout=PROBE_TIMEOUT_SECONDS):
            if event.get("error"):
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": event.get("error", "probe failed"),
                    "code": event.get("code", 502),
                }
            if event.get("text") == token_stream.DONE_SENTINEL:
                full_text = event.get("full_text") or "".join(chunks)
                full_reasoning = event.get("full_reasoning") or ""
                usage = event.get("usage")
                grid_meta = event.get("grid")
                break
            chunks.append(token_stream.event_content_text(event))
        else:
            await _mark_probe(job_id, "timeout")
            return {
                "status": "error",
                "assignment_id": assignment_id,
                "job_id": job_id,
                "message": "probe timed out",
                "code": 504,
            }
    except Exception:
        await _mark_probe(job_id, "failed")
        logger.error("validator probe failed assignment=%s job=%s", assignment_id, job_id, exc_info=True)
        raise

    evidence = {
        "assignment_id": assignment_id,
        "grid_nonce": row["grid_nonce"],
        "worker_id": row["target_worker_id"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "prompt_hash": _hash_text(str((row["challenge"] or {}).get("prompt") or "")),
        "response_hash": _hash_text(full_text),
    }
    evidence["evidence_hash"] = _hash_obj(evidence)
    latency_ms = int((_now() - started).total_seconds() * 1000)
    probe_verdict = _score_text_challenge(row["challenge"] or {}, full_text, latency_ms)
    await _mark_probe(
        job_id,
        "completed",
        prompt_hash=evidence["prompt_hash"],
        response_hash=evidence["response_hash"],
        evidence_hash=evidence["evidence_hash"],
        verdict=probe_verdict,
        latency_ms=latency_ms,
    )
    return {
        "status": "completed",
        "assignment_id": assignment_id,
        "job_id": job_id,
        "grid_nonce": row["grid_nonce"],
        "target_worker_id": row["target_worker_id"],
        "target_worker_name": row["target_worker_name"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "output_text": full_text,
        "output_reasoning": full_reasoning,
        "usage": usage,
        "grid": grid_meta,
        "probe_verdict": probe_verdict,
        "probe_score": VERDICT_SCORE[probe_verdict],
        "probe_latency_ms": latency_ms,
        **evidence,
        "economic_effect": "none",
    }


async def _mark_probe(
    job_id: str,
    status: str,
    *,
    prompt_hash: str | None = None,
    response_hash: str | None = None,
    evidence_hash: str | None = None,
    verdict: str | None = None,
    latency_ms: int | None = None,
) -> None:
    try:
        async with await new_session() as session:
            values: dict[str, Any] = {"probe_status": status}
            if prompt_hash is not None:
                values["probe_prompt_hash"] = prompt_hash
            if response_hash is not None:
                values["probe_response_hash"] = response_hash
            if evidence_hash is not None:
                values["probe_evidence_hash"] = evidence_hash
            if verdict is not None:
                values["probe_verdict"] = verdict
            if latency_ms is not None:
                values["probe_latency_ms"] = latency_ms
            await session.execute(
                sa.update(assignments_t)
                .where(assignments_t.c.probe_job_id == job_id)
                .values(**values)
            )
            await session.commit()
    except Exception:
        logger.warning("failed to mark validator probe %s as %s", job_id, status, exc_info=True)
