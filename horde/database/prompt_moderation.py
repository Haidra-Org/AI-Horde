# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capture actionable prompt evidence, list it for moderators, and decay it on schedule.

Evidence writes use a separate transaction, with no foreign-key dependency on
the submitting transaction. A rejected submission rollback cannot erase them.
Database failures are logged without prompt text and do not turn a rejection
into acceptance. This is operational evidence, not a guaranteed audit ledger.

Captured values never change. Each retention pass first anonymizes events past the ceiling, removing every identifying
value, then removes prompt text past the text window, then the address past the address window; it never deletes an
event by age. The worker report records in ``user_problem_jobs`` follow the same ceiling and address window: the pass
deletes them past the ceiling, then removes their address and origin text. Every step skips rows under moderation action
(``_under_moderation_action``), which are kept without limit. Retention never deletes a note, and wiping an account
leaves its events and worker report records in place.

``MODERATION_RETENTION_POLICY`` is read from the environment at import, so an invalid setting fails startup; the
retention pass and the privacy document read it when they run. ``EVENT_COLUMN_RETENTION`` assigns every event column a
``RetentionFate``, and the retention steps clear columns from it; a column without a fate fails import.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, auto
from types import MappingProxyType
from typing import Any, TypedDict

from sqlalchemy import ColumnElement, Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from horde.classes.base.prompt_moderation import (
    MAX_ORIGIN_TEXT_CHARACTERS,
    PromptModerationEvent,
    PromptModerationNote,
    PromptModerationOutcome,
    PromptModerationReason,
)
from horde.classes.base.user import User, UserProblemJobs, UserRole, UserSuspicions
from horde.countermeasures import CounterMeasures
from horde.enums import UserRoleTypes
from horde.flask import db
from horde.logger import logger

TEXT_RETENTION_ENV: str = "HORDE_MODERATION_TEXT_RETENTION_DAYS"
"""The environment variable for text retention."""
IPADDR_RETENTION_ENV: str = "HORDE_MODERATION_IPADDR_RETENTION_DAYS"
"""The environment variable for IP address retention."""
EVIDENCE_CEILING_ENV: str = "HORDE_MODERATION_EVIDENCE_CEILING_DAYS"
"""The environment variable for the evidence ceiling."""

DEFAULT_TEXT_RETENTION_DAYS: int | None = None
"""The default text retention window in days, or None to keep text until the ceiling.

Text is kept until the ceiling by default so that evidence no moderator has reviewed yet stays reviewable for the whole
evidence period. An operator can set a shorter window.
"""
DEFAULT_IPADDR_RETENTION_DAYS: int = 30
"""The default IP address retention window in days.

The raw address serves the review of recent activity. After the window the address pseudonym still links the event
to the same IP subject, so the address itself is not kept.
"""
DEFAULT_EVIDENCE_CEILING_DAYS: int = 365
"""The default evidence ceiling in days, the longest ``MAX_EVIDENCE_CEILING_DAYS`` permits.

The ceiling anonymizes only events not under moderation action.
"""

MAX_EVIDENCE_CEILING_DAYS: int = 365
"""The maximum evidence ceiling in days.

Identifying data on an event that no moderation action has touched is kept for at most a year. Events under
moderation action are exempt from the ceiling, so it bounds only records that serve no enforcement purpose.
"""

NO_SEPARATE_WINDOW: str = "none"
"""Indicate that there is no separate retention window for this type of data."""

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

CLEANUP_BATCH_SIZE: int = 1000
"""The most rows each retention step changes or deletes in one pass.

The pass runs every hour, so each step can drain 24,000 rows a day, above the daily intake of events and worker
reports, while one batch keeps each step's transaction and its row locks short. Rows under moderation action never get
a flag, so every pass rescans them; running hourly with a bounded batch keeps that rescan cheap.
"""


@dataclass(frozen=True, kw_only=True)
class RetentionPolicy:
    """Represent how long each part of prompt evidence is kept, measured from capture.

    Every window and the ceiling apply only to events not under moderation action; those events and every note are
    kept without limit.

    Raises:
        ValueError: A window is not a positive number of days, a window exceeds the ceiling, or the ceiling exceeds
            ``MAX_EVIDENCE_CEILING_DAYS``.
    """

    text: timedelta | None
    """The text retention window, or None to keep text until the ceiling."""
    ipaddr: timedelta | None
    """The IP address retention window, or None to keep IP addresses until the ceiling."""
    ceiling: timedelta
    """The anonymization ceiling, after which an event not under moderation action keeps no identifying data."""

    def __post_init__(self) -> None:
        # Messages name the environment variables because operators set the policy through them.
        if self.ceiling <= timedelta(0) or self.ceiling > timedelta(days=MAX_EVIDENCE_CEILING_DAYS):
            raise ValueError(f"{EVIDENCE_CEILING_ENV} must be at most {MAX_EVIDENCE_CEILING_DAYS} days, got {self.ceiling}")
        for variable, window in ((TEXT_RETENTION_ENV, self.text), (IPADDR_RETENTION_ENV, self.ipaddr)):
            if window is None:
                continue
            if window <= timedelta(0):
                raise ValueError(f"{variable} must be a positive whole number of days, got {window}")
            if window > self.ceiling:
                raise ValueError(f"{variable} ({window.days} days) exceeds {EVIDENCE_CEILING_ENV} ({self.ceiling.days} days)")

    @property
    def text_days(self) -> int | None:
        """Return the text window in whole days, or None when text is kept until the ceiling."""
        return self.text.days if self.text is not None else None

    @property
    def ipaddr_days(self) -> int | None:
        """Return the address window in whole days, or None when the address is kept until the ceiling."""
        return self.ipaddr.days if self.ipaddr is not None else None

    @property
    def ceiling_days(self) -> int:
        """Return the anonymization ceiling in whole days."""
        return self.ceiling.days


def _parse_positive_days(variable: str, raw: str) -> int:
    """Parse a positive whole number of days from an environment value.

    Raises:
        ValueError: The value is not a positive integer.
    """
    try:
        days = int(raw.strip())
    except ValueError as err:
        raise ValueError(f"{variable} must be a positive whole number of days, got {raw!r}") from err
    if days < 1:
        raise ValueError(f"{variable} must be a positive whole number of days, got {raw!r}")
    return days


def _parse_window(environ: Mapping[str, str], variable: str, default_days: int | None) -> timedelta | None:
    """Parse an optional window: absent is the default, ``none`` is no separate window.

    Raises:
        ValueError: The value is neither ``none`` nor a positive integer.
    """
    raw = environ.get(variable)
    if raw is None:
        return timedelta(days=default_days) if default_days is not None else None
    if raw.strip().lower() == NO_SEPARATE_WINDOW:
        return None
    return timedelta(days=_parse_positive_days(variable, raw))


def load_retention_policy(environ: Mapping[str, str] = os.environ) -> RetentionPolicy:
    """Create the retention policy from the operator's environment.

    Args:
        environ: Variables to read; the process environment by default.

    Returns:
        The validated policy.

    Raises:
        ValueError: A variable is malformed, the ceiling is disabled or above ``MAX_EVIDENCE_CEILING_DAYS``, or a
            window exceeds the ceiling.
    """
    raw_ceiling = environ.get(EVIDENCE_CEILING_ENV)
    if raw_ceiling is None:
        ceiling_days = DEFAULT_EVIDENCE_CEILING_DAYS
    elif raw_ceiling.strip().lower() == NO_SEPARATE_WINDOW:
        raise ValueError(f"{EVIDENCE_CEILING_ENV} cannot be disabled; set at most {MAX_EVIDENCE_CEILING_DAYS} days")
    else:
        ceiling_days = _parse_positive_days(EVIDENCE_CEILING_ENV, raw_ceiling)
    return RetentionPolicy(
        text=_parse_window(environ, TEXT_RETENTION_ENV, DEFAULT_TEXT_RETENTION_DAYS),
        ipaddr=_parse_window(environ, IPADDR_RETENTION_ENV, DEFAULT_IPADDR_RETENTION_DAYS),
        ceiling=timedelta(days=ceiling_days),
    )


MODERATION_RETENTION_POLICY: RetentionPolicy = load_retention_policy()
"""The retention policy used for moderation evidence, loaded from the environment."""


class RetentionFate(StrEnum):
    """Represent what retention does to one event column."""

    KEPT = auto()
    """Never changed by retention."""
    TEXT = auto()
    """Cleared past the text window, and at the ceiling."""
    IPADDR = auto()
    """Cleared past the address window, and at the ceiling."""
    IDENTITY = auto()
    """Cleared at the ceiling only, and only on an event not under moderation action."""
    RETENTION_FLAG = auto()
    """Set once retention clears the columns it records, so a null reads as removed rather than never known."""


EVENT_COLUMN_RETENTION: Mapping[str, RetentionFate] = MappingProxyType(
    {
        "id": RetentionFate.KEPT,
        "created": RetentionFate.KEPT,
        "reason": RetentionFate.KEPT,
        "outcome": RetentionFate.KEPT,
        "models": RetentionFate.KEPT,
        "text_truncated": RetentionFate.KEPT,
        # The reporting worker, not the subject of the evidence, so counts by worker stay derivable.
        "worker_id": RetentionFate.KEPT,
        "submitted_prompt": RetentionFate.TEXT,
        "moderation_prompt": RetentionFate.TEXT,
        "effective_prompt": RetentionFate.TEXT,
        "ipaddr": RetentionFate.IPADDR,
        "origin_text": RetentionFate.IPADDR,
        # Outlives the address, so address filters keep matching the event until the ceiling.
        "ip_subject_key": RetentionFate.IDENTITY,
        "user_id": RetentionFate.IDENTITY,
        "proxied_account": RetentionFate.IDENTITY,
        "request_id": RetentionFate.IDENTITY,
        # Clearing it also releases the unique key, so a later report of the same job records a new event.
        "job_id": RetentionFate.IDENTITY,
        "text_redacted": RetentionFate.RETENTION_FLAG,
        "ipaddr_redacted": RetentionFate.RETENTION_FLAG,
        "anonymized": RetentionFate.RETENTION_FLAG,
    }
)
"""The retention fate of every ``PromptModerationEvent`` column.

The retention steps build their updates from it, so it is the single decision of what each step clears. No step clears
a column of an event under moderation action, and notes are separate rows no step deletes. Import validation refuses
a column without a fate, so a new column cannot be retained or cleared by accident.
"""

REMOVAL_FLAGS: Mapping[RetentionFate, str] = MappingProxyType(
    {
        RetentionFate.TEXT: "text_redacted",
        RetentionFate.IPADDR: "ipaddr_redacted",
        RetentionFate.IDENTITY: "anonymized",
    }
)
"""The flag column retention sets when it clears the columns of each removable fate.

The flag keeps a cleared value distinguishable from one never captured, and excludes the event from that step's
partial index.
"""


def validate_column_retention(
    column_retention: Mapping[str, RetentionFate],
    removal_flags: Mapping[RetentionFate, str],
    column_names: Collection[str],
) -> None:
    """Validate that every column has one fate and every removable fate has one flag column.

    Args:
        column_retention: Fate of each column, by column name.
        removal_flags: Flag column of each removable fate.
        column_names: Every column of the event table.

    Raises:
        ValueError: A column has no fate, a fate names no column, or the flags do not match the flag columns.
    """
    missing = set(column_names) - set(column_retention)
    unknown = set(column_retention) - set(column_names)
    if missing or unknown:
        raise ValueError(f"Event column retention has no fate for {sorted(missing)} and no column for {sorted(unknown)}")
    flag_columns = {column for column, fate in column_retention.items() if fate == RetentionFate.RETENTION_FLAG}
    removable_fates = set(RetentionFate) - {RetentionFate.KEPT, RetentionFate.RETENTION_FLAG}
    if set(removal_flags) != removable_fates or set(removal_flags.values()) != flag_columns:
        raise ValueError(f"Removal flags {dict(removal_flags)} must map each removable fate to one of {sorted(flag_columns)}")


validate_column_retention(EVENT_COLUMN_RETENTION, REMOVAL_FLAGS, PromptModerationEvent.__table__.columns.keys())


def _removal_values(*fates: RetentionFate) -> dict[str, Any]:
    """Return update values that clear every column of the given fates and set their flags."""
    values: dict[str, Any] = {column: None for column, fate in EVENT_COLUMN_RETENTION.items() if fate in fates}
    values.update(dict.fromkeys((REMOVAL_FLAGS[fate] for fate in fates), True))
    return values


@dataclass(frozen=True, kw_only=True)
class RetentionPassResult:
    """Represent how many rows each retention step changed or deleted in one pass.

    Rows under moderation action are never counted, since no step selects them, and notes are never deleted.
    """

    anonymized: int
    """The number of events anonymized."""
    text_redacted: int
    """The number of events with text redacted."""
    ipaddr_redacted: int
    """The number of events with IP addresses redacted."""
    problem_jobs_deleted: int
    """The number of worker report records deleted at the ceiling."""
    problem_job_ipaddr_redacted: int
    """The number of worker report records with IP addresses redacted."""

    @property
    def total(self) -> int:
        """Return the number of rows changed or deleted across all steps."""
        return self.anonymized + self.text_redacted + self.ipaddr_redacted + self.problem_jobs_deleted + self.problem_job_ipaddr_redacted


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


class NoteRecord(TypedDict):
    """Represent a moderator note as the listing and the note endpoint return it."""

    id: int
    """The ID of the note record."""
    author_id: int
    """The ID of the author of the note."""
    note: str
    """The content of the note."""
    created: datetime
    """The timestamp when the note was created."""


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


def _ip_subject_criterion(ip_subject: str) -> ColumnElement[bool]:
    """Return the filter selecting the events of one IP subject.

    An event with an address pseudonym matches by it alone. An event without one, captured while the deployment had no
    usable secret, matches by its address. Each branch has its own partial index.
    """
    unkeyed_match = and_(PromptModerationEvent.ip_subject_key.is_(None), PromptModerationEvent.ipaddr == ip_subject)
    subject_key = CounterMeasures.ip_subject_key(ip_subject)
    if subject_key is None:
        return unkeyed_match
    return or_(PromptModerationEvent.ip_subject_key == subject_key, unkeyed_match)


def get_prompt_events(
    *,
    limit: int,
    before_id: int | None = None,
    event_id: int | None = None,
    user_id: int | None = None,
    proxied_account: str | None = None,
    ip_subject: str | None = None,
    ip_subject_key: str | None = None,
    worker_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Return a bounded, newest-first page of evidence with its notes.

    Args:
        limit: Maximum number of events in the page.
        before_id: Exclusive descending cursor; only events with a lower ID are returned.
        event_id: Restrict to one event.
        user_id: Restrict to one account.
        proxied_account: Restrict to one proxied account.
        ip_subject: Restrict to one IP subject (``CounterMeasures.parse_ip_subject``). It matches by address
            pseudonym, so an event stays selected after its address is removed.
        ip_subject_key: Restrict to one address pseudonym, matched exactly.
        worker_id: Restrict to one reporting worker.
        since: Inclusive naive UTC lower bound on the capture time.
        until: Exclusive naive UTC upper bound on the capture time.

    Returns:
        ``events``, each with its notes oldest first, and ``next_cursor``, the ``before_id`` of the next page or None.
    """
    statement = select(PromptModerationEvent)
    if before_id is not None:
        statement = statement.where(PromptModerationEvent.id < before_id)
    if event_id is not None:
        statement = statement.where(PromptModerationEvent.id == event_id)
    if user_id is not None:
        statement = statement.where(PromptModerationEvent.user_id == user_id)
    if proxied_account is not None:
        statement = statement.where(PromptModerationEvent.proxied_account == proxied_account)
    if ip_subject is not None:
        statement = statement.where(_ip_subject_criterion(ip_subject))
    if ip_subject_key is not None:
        statement = statement.where(PromptModerationEvent.ip_subject_key == ip_subject_key)
    if worker_id is not None:
        statement = statement.where(PromptModerationEvent.worker_id == worker_id)
    if since is not None:
        statement = statement.where(PromptModerationEvent.created >= since)
    if until is not None:
        statement = statement.where(PromptModerationEvent.created < until)
    statement = statement.order_by(PromptModerationEvent.id.desc()).limit(limit + 1)
    rows = list(db.session.execute(statement).scalars())
    notes: dict[int, list[NoteRecord]] = defaultdict(list)
    if rows[:limit]:
        for note in db.session.execute(
            select(PromptModerationNote)
            .where(PromptModerationNote.event_id.in_([evidence.id for evidence in rows[:limit]]))
            .order_by(PromptModerationNote.id),
        ).scalars():
            notes[note.event_id].append(_note_record(note))
    events = []
    for evidence in rows[:limit]:
        # Deliberate allowlist: never serialize ORM __dict__ or arbitrary relationships.
        events.append(
            {
                "id": evidence.id,
                "created": evidence.created,
                "user_id": evidence.user_id,
                "request_id": evidence.request_id,
                "worker_id": evidence.worker_id,
                "proxied_account": evidence.proxied_account,
                "ipaddr": evidence.ipaddr,
                "ip_subject_key": evidence.ip_subject_key,
                "origin_text": evidence.origin_text,
                "models": evidence.models,
                "reason": evidence.reason,
                "outcome": evidence.outcome,
                "submitted_prompt": evidence.submitted_prompt,
                "moderation_prompt": evidence.moderation_prompt,
                "effective_prompt": evidence.effective_prompt,
                "text_truncated": evidence.text_truncated,
                "text_redacted": evidence.text_redacted,
                "ipaddr_redacted": evidence.ipaddr_redacted,
                "anonymized": evidence.anonymized,
                "notes": notes.get(evidence.id, []),
            }
        )
    return {
        "events": events,
        "next_cursor": events[-1]["id"] if len(rows) > limit else None,
    }


def _note_record(note: PromptModerationNote) -> NoteRecord:
    return {
        "id": note.id,
        "author_id": note.author_id,
        "note": note.note,
        "created": note.created,
    }


def add_prompt_note(*, event_id: int, author_id: int, note: str) -> NoteRecord | None:
    """Attach a note to an event.

    An anonymized event takes notes too, and they are stored. Postgres checks the note's foreign key with a key-share
    lock on the event row, held until commit, which orders the note against ``_anonymize_batch``.

    Args:
        event_id: Event the note is about.
        author_id: Moderator writing the note.
        note: Validated note text.

    Returns:
        The stored note, or None when the event does not exist.
    """
    # Own this transaction explicitly; unrelated ORM mutations must not be committed here.
    # Own this transaction explicitly; unrelated ORM mutations must not be committed here. Events are never deleted,
    # so an event that exists here still exists when the note is inserted.
    with Session(db.engine, expire_on_commit=False) as session, session.begin():
        event = session.execute(
            select(PromptModerationEvent.id).where(PromptModerationEvent.id == event_id),
        ).scalar_one_or_none()
        if event is None:
            return None
        record = PromptModerationNote(event_id=event_id, author_id=author_id, note=note)
        session.add(record)
    return _note_record(record)


def _account_restricted(user_id: ColumnElement[int]) -> ColumnElement[bool]:
    """Return whether the account ``user_id`` identifies is flagged, or suspicious as ``User.is_suspicious`` judges it.

    A null ``user_id`` matches no account and is never restricted.
    """
    flagged = exists().where(
        UserRole.user_id == user_id,
        UserRole.user_role == UserRoleTypes.FLAGGED,
        UserRole.value.is_(True),
    )
    trusted = exists().where(
        UserRole.user_id == user_id,
        UserRole.user_role == UserRoleTypes.TRUSTED,
        UserRole.value.is_(True),
    )
    suspicions = select(func.count(UserSuspicions.id)).where(UserSuspicions.user_id == user_id).scalar_subquery()
    return or_(flagged, and_(~trusted, suspicions >= User.SUSPICION_THRESHOLD))


def _under_moderation_action() -> ColumnElement[bool]:
    """Return whether an event is under moderation action: it has a note, or its account is flagged or suspicious.

    The evidence is kept to identify accounts attempting to generate illegal content and to enforce the restrictions
    that follow, and a restriction cannot be enforced against an account whose record is gone, so no retention step
    touches these events.
    """
    has_note = exists().where(PromptModerationNote.event_id == PromptModerationEvent.id)
    return or_(has_note, _account_restricted(PromptModerationEvent.user_id))


# Each predicate is the single definition of which rows a retention step selects. The negated flag, or the non-null
# address, matches the step's partial index predicate, so the planner reads only rows the step has not finished; the
# moderation-action exemption then filters those rows. Each step applies its predicate twice: in the subquery that
# picks the oldest due ids, and again in the WHERE of the UPDATE or DELETE itself. Under READ COMMITTED, when a picked
# row was changed by a concurrent transaction while the statement waited for its lock, Postgres re-checks that WHERE
# against the committed row, so a row another pass already finished is not changed twice.
def _anonymization_due(cutoff: datetime) -> ColumnElement[bool]:
    return and_(PromptModerationEvent.created < cutoff, ~PromptModerationEvent.anonymized, ~_under_moderation_action())


def _text_redaction_due(cutoff: datetime) -> ColumnElement[bool]:
    return and_(PromptModerationEvent.created < cutoff, ~PromptModerationEvent.text_redacted, ~_under_moderation_action())


def _ipaddr_redaction_due(cutoff: datetime) -> ColumnElement[bool]:
    return and_(PromptModerationEvent.created < cutoff, ~PromptModerationEvent.ipaddr_redacted, ~_under_moderation_action())


def _problem_job_deletion_due(cutoff: datetime) -> ColumnElement[bool]:
    return and_(UserProblemJobs.created < cutoff, ~_account_restricted(UserProblemJobs.user_id))


def _problem_job_ipaddr_redaction_due(cutoff: datetime) -> ColumnElement[bool]:
    return and_(
        UserProblemJobs.created < cutoff,
        or_(UserProblemJobs.ipaddr.is_not(None), UserProblemJobs.origin_text.is_not(None)),
        ~_account_restricted(UserProblemJobs.user_id),
    )


def _oldest_due(
    model: type[PromptModerationEvent] | type[UserProblemJobs],
    predicate: ColumnElement[bool],
    batch_size: int,
) -> Select[tuple[int]]:
    return select(model.id).where(predicate).order_by(model.created, model.id).limit(batch_size)


def _anonymize_batch(cutoff: datetime, batch_size: int) -> int:
    """Remove identity, address and text from the oldest events past the ceiling; their notes are kept."""
    due = _anonymization_due(cutoff)
    with db.engine.begin() as connection:
        # FOR UPDATE conflicts with the key-share lock a note insert takes on its event through the foreign key, so
        # this select waits for a note transaction in progress on a picked event, and a note begun after it waits for
        # this transaction. The UPDATE is a later statement, which under READ COMMITTED sees every note committed
        # while the select waited, and it re-checks the due predicate, so such a note exempts its event. A note that
        # waited on this transaction is stored on the anonymized event.
        event_ids = list(connection.execute(_oldest_due(PromptModerationEvent, due, batch_size).with_for_update()).scalars())
        if not event_ids:
            return 0
        result = connection.execute(
            update(PromptModerationEvent)
            .where(PromptModerationEvent.id.in_(event_ids), due)
            .values(_removal_values(RetentionFate.TEXT, RetentionFate.IPADDR, RetentionFate.IDENTITY)),
        )
    return result.rowcount


def _redact_text_batch(cutoff: datetime, batch_size: int) -> int:
    """Remove the prompt stages from the oldest events past the text window."""
    due = _text_redaction_due(cutoff)
    with db.engine.begin() as connection:
        result = connection.execute(
            update(PromptModerationEvent)
            .where(PromptModerationEvent.id.in_(_oldest_due(PromptModerationEvent, due, batch_size)), due)
            .values(_removal_values(RetentionFate.TEXT)),
        )
    return result.rowcount


def _redact_ipaddr_batch(cutoff: datetime, batch_size: int) -> int:
    """Remove the address from the oldest events past the address window."""
    due = _ipaddr_redaction_due(cutoff)
    with db.engine.begin() as connection:
        result = connection.execute(
            update(PromptModerationEvent)
            .where(PromptModerationEvent.id.in_(_oldest_due(PromptModerationEvent, due, batch_size)), due)
            .values(_removal_values(RetentionFate.IPADDR)),
        )
    return result.rowcount


def _delete_problem_jobs_batch(cutoff: datetime, batch_size: int) -> int:
    """Delete the oldest worker report records past the ceiling whose account is not under moderation action."""
    due = _problem_job_deletion_due(cutoff)
    with db.engine.begin() as connection:
        result = connection.execute(
            delete(UserProblemJobs).where(UserProblemJobs.id.in_(_oldest_due(UserProblemJobs, due, batch_size)), due),
        )
    return result.rowcount


def _redact_problem_job_ipaddr_batch(cutoff: datetime, batch_size: int) -> int:
    """Remove the address from the oldest worker report records past the address window, except under moderation action.

    The problem-job alerts count records by address over the last hour or day only, so a removed address changes
    no alert.
    """
    due = _problem_job_ipaddr_redaction_due(cutoff)
    with db.engine.begin() as connection:
        result = connection.execute(
            update(UserProblemJobs)
            .where(UserProblemJobs.id.in_(_oldest_due(UserProblemJobs, due, batch_size)), due)
            .values(ipaddr=None, origin_text=None),
        )
    return result.rowcount


def apply_evidence_retention(
    policy: RetentionPolicy | None = None,
    *,
    batch_size: int = CLEANUP_BATCH_SIZE,
) -> RetentionPassResult:
    """Run one bounded retention pass: anonymize past the ceiling, then redact text and addresses past their windows.

    Evidence events and notes are never deleted. Each step changes at most ``batch_size`` events, oldest first, in
    its own transaction, and skips events it already finished, so repeated passes drain a backlog and then do nothing.
    Anonymizing first leaves the later steps nothing to do for those events. The worker report records follow the
    same ceiling and address window: a step deletes at most ``batch_size`` of them past the ceiling, then another
    removes the address from at most ``batch_size`` past the address window. A step whose window is None does not
    run.

    Every step skips rows under moderation action (``_under_moderation_action``, or ``_account_restricted`` for a
    worker report record or worker suspicion history row), so the ceiling bounds only records that serve no enforcement
    purpose, and actioned records are kept without limit.

    Run periodically in an application context.

    Args:
        policy: Windows to apply. By default ``MODERATION_RETENTION_POLICY`` as it stands at the call, the policy
            the privacy document renders.
        batch_size: Most rows each step changes or deletes in this pass.

    Returns:
        Number of rows each step changed or deleted.
    """
    if policy is None:
        policy = MODERATION_RETENTION_POLICY
    now = datetime.utcnow()
    anonymized = _anonymize_batch(now - policy.ceiling, batch_size)
    text_redacted = _redact_text_batch(now - policy.text, batch_size) if policy.text is not None else 0
    ipaddr_redacted = _redact_ipaddr_batch(now - policy.ipaddr, batch_size) if policy.ipaddr is not None else 0
    problem_jobs_deleted = _delete_problem_jobs_batch(now - policy.ceiling, batch_size)
    problem_job_ipaddr_redacted = 0
    if policy.ipaddr is not None:
        problem_job_ipaddr_redacted = _redact_problem_job_ipaddr_batch(now - policy.ipaddr, batch_size)
    return RetentionPassResult(
        anonymized=anonymized,
        text_redacted=text_redacted,
        ipaddr_redacted=ipaddr_redacted,
        problem_jobs_deleted=problem_jobs_deleted,
        problem_job_ipaddr_redacted=problem_job_ipaddr_redacted,
    )
