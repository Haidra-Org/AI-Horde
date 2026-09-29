# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store prompt evidence as events with moderator notes.

Account/request identifiers deliberately have no foreign keys: rejected requests
have no waiting row, and evidence must survive request expiry or account deletion.
Only notes reference evidence, so deleting an event removes its notes.

Captured values never change. Retention removes text, address and identity on schedule and records each removal in
a flag, so a null value reads as removed rather than never known.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import column, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped

from horde.classes.base.sql_constructs import UtcNow
from horde.flask import db

MAX_NOTE_CHARACTERS: int = 2000
"""The most characters one moderator note holds, in the note column and in the API that writes it.

A note records a moderator's reasoning about one event. 2,000 characters hold several paragraphs while bounding what
one write can store.
"""
IP_SUBJECT_KEY_CHARACTERS: int = 64
"""The length of an address pseudonym, a hexadecimal HMAC-SHA256 digest (``CounterMeasures.ip_subject_key``)."""
MAX_ORIGIN_TEXT_CHARACTERS: int = 255
"""The most characters of a request origin with no IP subject that evidence keeps.

The origin can be a trusted proxy's ``Proxied-For`` header value, which the proxy controls, so it is bounded so
garbage cannot grow the row.
"""


class PromptModerationReason(StrEnum):
    """Represent bounded reasons for actionable prompt evidence, not filter matches."""

    FILTER_REJECTION = "filter_rejection"
    """The prompt filter refused the submission and no replacement produced a usable prompt."""
    MODEL_REJECTION = "model_rejection"
    """The NSFW-model or flagged-account replacement emptied the prompt, so the submission was refused."""
    WORKER_CSAM = "worker_csam"
    """A worker reported its generation as suspected child sexual abuse material and censored it."""


class PromptModerationOutcome(StrEnum):
    """Represent what happened to the request an event records."""

    REJECTED = "rejected"
    """The submission was refused before any worker received it."""
    CENSORED = "censored"
    """A worker generated the request and censored the result."""


class PromptModerationEvent(db.Model):
    """Represent retained evidence for one rejection or worker-reported problem job."""

    __tablename__ = "prompt_moderation_events"
    __table_args__ = (
        # One partial index per retention step, so each pass reads only events that step has not finished.
        db.Index("ix_prompt_moderation_text_pending", "created", "id", postgresql_where=text("NOT text_redacted")),
        db.Index("ix_prompt_moderation_ipaddr_pending", "created", "id", postgresql_where=text("NOT ipaddr_redacted")),
        db.Index("ix_prompt_moderation_anonymize_pending", "created", "id", postgresql_where=text("NOT anonymized")),
        # Serves time-bounded browsing (since/until), which the partial indexes cannot once events finish a step.
        db.Index("ix_prompt_moderation_created", "created"),
        db.Index("ix_prompt_moderation_user_id", "user_id", "id"),
        # Serves a proxied-account filter with or without an account; only service accounts supply one.
        db.Index(
            "ix_prompt_moderation_proxied_account_id",
            "proxied_account",
            "id",
            postgresql_where=column("proxied_account").is_not(None),
        ),
        # The address filter matches the pseudonym, and the address only on events without one. Every event captured
        # under a usable secret has a pseudonym, so the address index holds the remaining events alone.
        db.Index(
            "ix_prompt_moderation_ip_subject_key_id",
            "ip_subject_key",
            "id",
            postgresql_where=column("ip_subject_key").is_not(None),
        ),
        db.Index(
            "ix_prompt_moderation_unkeyed_ipaddr_id",
            "ipaddr",
            "id",
            postgresql_where=column("ip_subject_key").is_(None),
        ),
        # Only worker reports carry a worker; the rest of the table stays out of the index.
        db.Index(
            "ix_prompt_moderation_worker_id",
            "worker_id",
            "id",
            postgresql_where=column("worker_id").is_not(None),
        ),
    )

    id: Mapped[int] = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    """The event identifier, also the listing's descending cursor."""
    created: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=UtcNow())
    """The naive UTC capture time, from which every retention window counts.

    The server default repeats the migration's, so a schema built from the models matches production.
    """
    user_id: Mapped[int | None] = db.Column(db.Integer)
    """The submitting account; null once the event is anonymized."""
    request_id: Mapped[str | None] = db.Column(db.String(36))
    """The waiting request; null for a rejected submission, which never became one."""
    job_id: Mapped[str | None] = db.Column(db.String(36), unique=True)
    """The reported job; unique, so a repeated worker report returns the existing event."""
    worker_id: Mapped[str | None] = db.Column(db.String(36))
    """The worker that reported the job. It identifies the reporter, not the subject of the evidence."""
    proxied_account: Mapped[str | None] = db.Column(db.Text)
    """The account a service account submitted for, when it supplied one."""
    ipaddr: Mapped[str | None] = db.Column(db.Text)
    """The request's IP subject (``CounterMeasures.ip_subject``), such as an IPv4 address or an IPv6 /64 network."""
    ip_subject_key: Mapped[str | None] = db.Column(db.String(IP_SUBJECT_KEY_CHARACTERS))
    """The address pseudonym of ``ipaddr`` (``CounterMeasures.ip_subject_key``).

    It outlives the address, so the address filter keeps matching the event until it is anonymized. It is null when
    the origin was not an IP subject or the deployment had no usable secret at capture.
    """
    origin_text: Mapped[str | None] = db.Column(db.String(MAX_ORIGIN_TEXT_CHARACTERS))
    """The request origin as a trusted proxy reported it, when it was not blank and had no IP subject.

    It is null when the origin had a subject, which ``ipaddr`` holds instead. It is clipped to
    ``MAX_ORIGIN_TEXT_CHARACTERS``.
    """
    models: Mapped[list[str] | None] = db.Column(
        db.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
    )
    """The model names the request asked for.

    ``none_as_null`` stores a missing list as SQL NULL, never as a JSON null the array functions reject.
    """
    reason: Mapped[str] = db.Column(db.String(32), nullable=False)
    """Why the evidence was retained, a ``PromptModerationReason`` value."""
    outcome: Mapped[str] = db.Column(db.String(16), nullable=False)
    """What happened to the request, a ``PromptModerationOutcome`` value."""
    submitted_prompt: Mapped[str | None] = db.Column(db.Text)
    """The original submission."""
    moderation_prompt: Mapped[str | None] = db.Column(db.Text)
    """The submission after style expansion, as moderation evaluated it."""
    effective_prompt: Mapped[str | None] = db.Column(db.Text)
    """The prompt a worker received; null for a rejection."""
    text_truncated: Mapped[bool] = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    """Whether a prompt stage exceeded the evidence length limit and was clipped."""
    text_redacted: Mapped[bool] = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    """Whether retention ran its text step on the event, past the text window or at the ceiling.

    It is set whether or not the event had prompt text to remove.
    """
    ipaddr_redacted: Mapped[bool] = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    """Whether retention ran its address step on the event, past the address window or at the ceiling.

    It is set whether or not the event had an address or origin text to remove.
    """
    anonymized: Mapped[bool] = db.Column(db.Boolean, nullable=False, default=False, server_default=false())
    """Whether retention removed every identifying value at the ceiling."""


class PromptModerationNote(db.Model):
    """Represent a moderator's note on one event; notes never modify the captured evidence.

    Retention and account wipes never delete a note, and a note exempts its event from retention.
    """

    __tablename__ = "prompt_moderation_notes"
    __table_args__ = (db.Index("ix_prompt_moderation_notes_event_id", "event_id", "id"),)

    id: Mapped[int] = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    """The note identifier; an event's notes list oldest first by it."""
    event_id: Mapped[int] = db.Column(
        db.BigInteger().with_variant(db.Integer, "sqlite"),
        db.ForeignKey("prompt_moderation_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    """The event the note is about; deleting the event deletes the note."""
    author_id: Mapped[int] = db.Column(db.Integer, nullable=False)
    """The moderator who wrote the note."""
    note: Mapped[str] = db.Column(db.String(MAX_NOTE_CHARACTERS), nullable=False)
    """The note text."""
    created: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, server_default=UtcNow())
    """The naive UTC time the note was written."""
