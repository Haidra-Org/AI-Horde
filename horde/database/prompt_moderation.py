# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capture actionable prompt evidence for moderator review.

Evidence writes use a separate transaction, with no foreign-key dependency on
the submitting transaction. A rejected submission rollback cannot erase them.
Database failures are logged without prompt text and do not turn a rejection
into acceptance. This is operational evidence, not a guaranteed audit ledger.

Captured values never change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from horde.classes.base.prompt_moderation import (
    MAX_ORIGIN_TEXT_CHARACTERS,
    PromptModerationEvent,
    PromptModerationOutcome,
    PromptModerationReason,
)
from horde.countermeasures import CounterMeasures
from horde.flask import db
from horde.logger import logger

MAX_EVIDENCE_CHARACTERS: int = 16000
"""The most characters of each prompt stage an event keeps.

It bounds the size of an event row and of a listing page, which returns up to 100 events of three stages each; a
longer stage is clipped and the event marked ``text_truncated``.
"""


MAX_PROXIED_ACCOUNT_CHARACTERS: int = 1000
"""The most characters of a proxied account an event keeps.

The request parser does not bound the value, and an accepted request stores it in a 255-character column. A refused
request never reaches that column, so this keeps every value an accepted request could carry while bounding what a
refused one writes.
"""


@dataclass(frozen=True, kw_only=True)
class PromptEvidence:
    """Represent a snapshot of known prompt stages; unknown stages remain None.

    A stage is never filled from another stage. ``effective_prompt`` is the text a worker received or would receive,
    so it is None for a rejection, where no replacement produced text a worker could run.
    """

    user_id: int
    """The ID of the account that submitted the prompt."""
    reason: PromptModerationReason
    """The reason for the moderation action."""
    submitted_prompt: str | None
    """The prompt as submitted by the user."""
    moderation_prompt: str | None
    """The submission after style expansion, as moderation evaluated it."""
    effective_prompt: str | None
    """The prompt that was actually presented to the worker."""
    request_id: str | None = None
    """The ID of the request associated with the prompt."""
    job_id: str | None = None
    """The ID of the job associated with the prompt."""
    worker_id: str | None = None
    """The ID of the worker that reported the job."""
    proxied_account: str | None = None
    """The account that proxied the request, if any."""
    ipaddr: str | None = None
    """The IP address from which the request originated, as the request reported it."""
    models: Sequence[str] | None = None
    """The model names the request asked for."""


def subjectless_origin_text(origin: str | None, ip_subject: str | None) -> str | None:
    """Return the origin text evidence keeps for an origin that is not blank and has no IP subject.

    A trusted proxy's ``Proxied-For`` header can carry text that is not an address. It still records where the
    request came from, so it is kept, clipped to ``MAX_ORIGIN_TEXT_CHARACTERS``.

    Args:
        origin: Request origin as the request reported it, or None when unknown.
        ip_subject: The origin's subject (``CounterMeasures.ip_subject``).

    Returns:
        The clipped origin, or None when it is blank or has a subject.
    """
    if ip_subject is not None or origin is None or not origin.strip():
        return None
    return origin[:MAX_ORIGIN_TEXT_CHARACTERS]


def record_prompt_evidence(evidence: PromptEvidence) -> int | None:
    """Persist evidence independently of the caller's transaction.

    Args:
        evidence: Prompt stages and identifiers known at capture time.

    Returns:
        The event ID; for a job already reported, the existing event's ID; None when the write failed.
    """
    prompts = (evidence.submitted_prompt, evidence.moderation_prompt, evidence.effective_prompt)
    clipped = [prompt[:MAX_EVIDENCE_CHARACTERS] if prompt is not None else None for prompt in prompts]
    ip_subject = CounterMeasures.ip_subject(evidence.ipaddr)
    if evidence.reason == PromptModerationReason.WORKER_CSAM:
        outcome = PromptModerationOutcome.CENSORED
    else:
        outcome = PromptModerationOutcome.REJECTED
    # Use Core rather than a second ORM identity map; don't commit or roll back db.session.
    insert = sqlite_insert if db.engine.dialect.name == "sqlite" else postgres_insert
    statement = (
        insert(PromptModerationEvent)
        .values(
            user_id=evidence.user_id,
            reason=evidence.reason.value,
            outcome=outcome.value,
            submitted_prompt=clipped[0],
            moderation_prompt=clipped[1],
            effective_prompt=clipped[2],
            text_truncated=any(prompt is not None and len(prompt) > MAX_EVIDENCE_CHARACTERS for prompt in prompts),
            request_id=evidence.request_id,
            job_id=evidence.job_id,
            worker_id=evidence.worker_id,
            proxied_account=evidence.proxied_account[:MAX_PROXIED_ACCOUNT_CHARACTERS] if evidence.proxied_account else None,
            ipaddr=ip_subject,
            ip_subject_key=CounterMeasures.ip_subject_key(ip_subject),
            origin_text=subjectless_origin_text(evidence.ipaddr, ip_subject),
            models=list(evidence.models) if evidence.models is not None else None,
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
