# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capture actionable evidence and expose bounded moderator review queries.

Evidence writes use a separate transaction, with no foreign-key dependency on
the submitting transaction. A rejected submission rollback cannot erase them.
Database failures are logged without prompt text and do not turn a rejection
into acceptance. This is operational evidence, not a guaranteed audit ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from horde.classes.base.prompt_moderation import (
    PromptModerationEvent,
    PromptModerationReason,
    PromptModerationReview,
    PromptReviewStatus,
)
from horde.flask import db
from horde.logger import logger

PROMPT_EVIDENCE_RETENTION: timedelta = timedelta(days=30)
MAX_EVIDENCE_CHARACTERS: int = 16000
CLEANUP_BATCH_SIZE: int = 1000


@dataclass(frozen=True, kw_only=True)
class PromptEvidence:
    """Represent a snapshot of known prompt stages; unknown stages remain None."""

    user_id: int
    reason: PromptModerationReason
    submitted_prompt: str | None
    moderation_prompt: str | None
    effective_prompt: str | None
    request_id: str | None = None
    job_id: str | None = None
    worker_id: str | None = None
    proxied_account: str | None = None


def record_prompt_evidence(evidence: PromptEvidence) -> int | None:
    """Persist evidence independently of the caller's transaction.

    Args:
        evidence: Known prompt stages and correlation identifiers, never API keys.

    Returns:
        Event ID, or None after a logged database failure. A repeated report for a job
        that already has an event returns that event's ID.
    """
    prompts = (evidence.submitted_prompt, evidence.moderation_prompt, evidence.effective_prompt)
    clipped = [prompt[:MAX_EVIDENCE_CHARACTERS] if prompt is not None else None for prompt in prompts]
    # Use Core rather than a second ORM identity map; don't commit or roll back db.session.
    insert = sqlite_insert if db.engine.dialect.name == "sqlite" else postgres_insert
    statement = (
        insert(PromptModerationEvent)
        .values(
            user_id=evidence.user_id,
            reason=evidence.reason.value,
            outcome="censored" if evidence.reason == PromptModerationReason.WORKER_CSAM else "rejected",
            submitted_prompt=clipped[0],
            moderation_prompt=clipped[1],
            effective_prompt=clipped[2],
            text_truncated=any(prompt is not None and len(prompt) > MAX_EVIDENCE_CHARACTERS for prompt in prompts),
            request_id=evidence.request_id,
            job_id=evidence.job_id,
            worker_id=evidence.worker_id,
            proxied_account=evidence.proxied_account[:1000] if evidence.proxied_account else None,
        )
        .on_conflict_do_nothing(index_elements=["job_id"])
        .returning(PromptModerationEvent.id)
    )
    try:
        with db.engine.begin() as connection:
            event_id = connection.execute(statement).scalar_one_or_none()
            if event_id is None and evidence.job_id is not None:
                event_id = connection.execute(
                    select(PromptModerationEvent.id).where(PromptModerationEvent.job_id == evidence.job_id),
                ).scalar_one_or_none()
            return event_id
    except SQLAlchemyError as error:
        # SQLAlchemy's exception string can contain bound prompt text. Do not log it.
        logger.error("Prompt moderation evidence write failed ({})", type(error).__name__)
        return None


def get_prompt_events(
    *,
    limit: int,
    before_id: int | None = None,
    user_id: int | None = None,
    reason: PromptModerationReason | None = None,
    outcome: str | None = None,
    status: PromptReviewStatus | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Return a bounded, newest-first page of evidence and its current disposition.

    Args:
        limit: Page size, validated by the API to 1-100.
        before_id: Exclusive descending ID cursor.
        user_id: Optional account restriction.
        reason: Optional actionable-event reason.
        outcome: Optional rejected/censored restriction.
        status: Optional current review disposition.
        since: Inclusive UTC creation lower bound.
        until: Exclusive UTC creation upper bound.

    Returns:
        Materialized events and a next_cursor when another page exists.
    """
    statement = select(PromptModerationEvent, PromptModerationReview).outerjoin(PromptModerationReview)
    if before_id is not None:
        statement = statement.where(PromptModerationEvent.id < before_id)
    if user_id is not None:
        statement = statement.where(PromptModerationEvent.user_id == user_id)
    if reason is not None:
        statement = statement.where(PromptModerationEvent.reason == reason.value)
    if outcome is not None:
        statement = statement.where(PromptModerationEvent.outcome == outcome)
    if status is not None:
        statement = statement.where(func.coalesce(PromptModerationReview.status, "pending") == status.value)
    if since is not None:
        statement = statement.where(PromptModerationEvent.created >= since)
    if until is not None:
        statement = statement.where(PromptModerationEvent.created < until)
    statement = statement.order_by(PromptModerationEvent.id.desc()).limit(limit + 1)
    rows = db.session.execute(statement).all()
    events = []
    for evidence, review in rows[:limit]:
        # Deliberate allowlist: never serialize ORM __dict__ or arbitrary relationships.
        events.append(
            {
                "id": evidence.id,
                "created": evidence.created.isoformat(),
                "user_id": evidence.user_id,
                "request_id": evidence.request_id,
                "job_id": evidence.job_id,
                "worker_id": evidence.worker_id,
                "proxied_account": evidence.proxied_account,
                "reason": evidence.reason,
                "outcome": evidence.outcome,
                "submitted_prompt": evidence.submitted_prompt,
                "moderation_prompt": evidence.moderation_prompt,
                "effective_prompt": evidence.effective_prompt,
                "text_truncated": evidence.text_truncated,
                "status": review.status if review else PromptReviewStatus.PENDING.value,
                "reviewer_id": review.reviewer_id if review else None,
                "reviewed_at": review.reviewed_at.isoformat() if review else None,
                "note": review.note if review else None,
            }
        )
    return {"events": events, "next_cursor": events[-1]["id"] if len(rows) > limit else None}


def review_prompt_event(*, event_id: int, reviewer_id: int, status: PromptReviewStatus, note: str) -> bool:
    """Update disposition while locking the evidence against concurrent review/expiry.

    Args:
        event_id: Evidence to review.
        reviewer_id: Authenticated moderator ID.
        status: New disposition, including pending to reopen.
        note: Review note, validated by the API to at most 2000 characters.

    Returns:
        False if the evidence no longer exists; otherwise True.
    """
    # Own this transaction explicitly; unrelated ORM mutations must not be committed here.
    with Session(db.engine) as session, session.begin():
        evidence = session.execute(
            select(PromptModerationEvent.id).where(PromptModerationEvent.id == event_id).with_for_update(),
        ).scalar_one_or_none()
        if evidence is None:
            return False
        review = session.get(PromptModerationReview, event_id)
        if review is None:
            review = PromptModerationReview(event_id=event_id)
            session.add(review)
        review.status = status.value
        review.reviewer_id = reviewer_id
        review.reviewed_at = datetime.utcnow()
        review.note = note
    return True


def prune_moderation_evidence() -> int:
    """Delete a bounded batch of expired prompt evidence, cascading to its reviews.

    Returns:
        Number of evidence rows deleted. Run periodically in an application context.
    """
    with db.engine.begin() as connection:
        expired = (
            select(PromptModerationEvent.id)
            .where(PromptModerationEvent.created < datetime.utcnow() - PROMPT_EVIDENCE_RETENTION)
            .order_by(PromptModerationEvent.created, PromptModerationEvent.id)
            .limit(CLEANUP_BATCH_SIZE)
        )
        return connection.execute(
            delete(PromptModerationEvent).where(PromptModerationEvent.id.in_(expired)),
        ).rowcount
