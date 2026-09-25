# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store prompt evidence separately from short-lived requests and mutable review state.

Account/request identifiers deliberately have no foreign keys: rejected requests
have no waiting row, and evidence must survive request expiry or account deletion.
Only the review references evidence, so retention deletes both together.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy.orm import Mapped

from horde.flask import db


class PromptModerationReason(StrEnum):
    """Represent bounded reasons for actionable prompt evidence, not filter matches."""

    FILTER_REJECTION = "filter_rejection"
    MODEL_REJECTION = "model_rejection"
    WORKER_CSAM = "worker_csam"


class PromptReviewStatus(StrEnum):
    """Represent moderator disposition; pending is the absence of a review row."""

    PENDING = "pending"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"


class PromptModerationEvent(db.Model):
    """Represent retained evidence for one rejection or worker-reported problem job."""

    __tablename__ = "prompt_moderation_events"
    __table_args__ = (
        db.Index("ix_prompt_moderation_created", "created"),
        db.Index("ix_prompt_moderation_user_id", "user_id", "id"),
        db.Index("ix_prompt_moderation_reason_id", "reason", "id"),
    )

    id: Mapped[int] = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    created: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id: Mapped[int] = db.Column(db.Integer, nullable=False)
    request_id: Mapped[str | None] = db.Column(db.String(36))
    job_id: Mapped[str | None] = db.Column(db.String(36), unique=True)
    worker_id: Mapped[str | None] = db.Column(db.String(36))
    proxied_account: Mapped[str | None] = db.Column(db.Text)
    reason: Mapped[str] = db.Column(db.String(32), nullable=False)
    outcome: Mapped[str] = db.Column(db.String(16), nullable=False)
    submitted_prompt: Mapped[str | None] = db.Column(db.Text)
    moderation_prompt: Mapped[str | None] = db.Column(db.Text)
    effective_prompt: Mapped[str | None] = db.Column(db.Text)
    text_truncated: Mapped[bool] = db.Column(db.Boolean, nullable=False, default=False)


class PromptModerationReview(db.Model):
    """Represent the latest disposition without modifying captured evidence."""

    __tablename__ = "prompt_moderation_reviews"

    event_id: Mapped[int] = db.Column(
        db.BigInteger,
        db.ForeignKey("prompt_moderation_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = db.Column(db.String(16), nullable=False)
    reviewer_id: Mapped[int] = db.Column(db.Integer, nullable=False)
    reviewed_at: Mapped[datetime] = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    note: Mapped[str] = db.Column(db.String(2000), nullable=False, default="")
