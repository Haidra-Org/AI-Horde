# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only set-based views for moderator operational review."""

from __future__ import annotations

import re
import string
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import TypedDict

from sqlalchemy.orm import selectinload

from horde.classes.base.kudos import get_kudos_trust_threshold
from horde.classes.base.user import PromotionStatus, User, UserSuspicions
from horde.classes.base.worker import WorkerTemplate
from horde.flask import db
from horde.suspicions import SUSPICION_LOGS, Suspicions


class SuspicionReasonDetails(TypedDict):
    """Describe one active suspicion reason and its occurrence count."""

    id: int
    """The numeric suspicion reason code."""
    name: str
    """The reason's ``Suspicions`` name, or ``UNKNOWN_<id>`` for a code this version does not define."""
    description: str
    """The reason's log template with its placeholders removed."""
    count: int
    """The number of active reports for this reason."""


class PromotionReviewUser(TypedDict):
    """Describe a user who currently satisfies the automatic-promotion prerequisites."""

    id: int
    """The account ID."""
    username: str
    """The account's unique alias, ``name#id``."""
    last_active: datetime
    """The naive UTC time the account was last active."""
    account_age: int
    """Seconds since the account was created."""
    kudos: float
    """The account's spendable kudos."""
    evaluating_kudos: float
    """The kudos the account earned while its trust is evaluated."""
    suspicious: int
    """The number of active suspicion reports on the account."""
    suspicion_reasons: list[SuspicionReasonDetails]
    """The active suspicion reasons, by code."""
    flagged: bool
    """Whether a moderator flagged the account."""
    deleted: bool
    """Whether the account is marked for deletion."""
    vpn: bool
    """Whether the account has the VPN role."""
    worker_count: int
    """The number of workers the account owns."""
    paused_worker_count: int
    """The number of the account's workers that are paused."""
    worker_kudos: float
    """The kudos the account's workers earned."""
    worker_fulfilments: int
    """The number of jobs the account's workers fulfilled."""
    contact: str | None
    """The contact the account supplied, if any."""
    admin_comment: str | None
    """The moderators' comment on the account, if any."""


class ModerationOverview(TypedDict):
    """Group operational exceptions into independently limited review queues."""

    promotion_threshold: float | None
    """The evaluating kudos above which automatic promotion applies, or None when it has no configured threshold."""
    suspicion_threshold: int
    """The report count at which an account counts as suspicious."""
    worker_suspicion_threshold: int
    """The report count at which a worker counts as suspicious."""
    promotion_blocked_users: list[PromotionReviewUser]
    """Accounts that meet every promotion criterion but suspicion, highest evaluating kudos first."""
    promotion_eligible_users: list[PromotionReviewUser]
    """Accounts automatic promotion will trust, highest evaluating kudos first."""


def suspicion_description(reason: Suspicions) -> str:
    """Return the reason's log template with its placeholders removed.

    ``SUSPICION_LOGS`` holds ``str.format`` templates that are filled per event. An overview aggregates events by
    reason, so there is no single value to fill; dropping the fields keeps the templates the one source of wording.
    """
    literal = "".join(text for text, *_ in string.Formatter().parse(SUSPICION_LOGS[reason]))
    # A placeholder wrapped in brackets leaves the brackets behind once removed.
    literal = re.sub(r"\(\s*\)|\[\s*\]", "", literal)
    return " ".join(literal.split()).rstrip(" :;,-")


def _suspicion_name(suspicion_id: int) -> str:
    """Return the reason's enum name, or a placeholder for a code this version does not define."""
    try:
        return Suspicions(suspicion_id).name
    except ValueError:
        return f"UNKNOWN_{suspicion_id}"


def _suspicion_details(
    suspicions: Iterable[UserSuspicions],
) -> list[SuspicionReasonDetails]:
    counts: dict[int, int] = {}
    for suspicion in suspicions:
        counts[suspicion.suspicion_id] = counts.get(suspicion.suspicion_id, 0) + 1
    details: list[SuspicionReasonDetails] = []
    for suspicion_id, count in sorted(counts.items()):
        try:
            reason = Suspicions(suspicion_id)
        except ValueError:
            name, description = f"UNKNOWN_{suspicion_id}", "Unknown suspicion reason"
        else:
            name, description = reason.name, suspicion_description(reason)
        details.append(
            {
                "id": suspicion_id,
                "name": name,
                "description": description,
                "count": count,
            },
        )
    return details


def _promotion_user_details(user: User) -> PromotionReviewUser:
    workers = list(user.workers)
    return {
        "id": user.id,
        "username": user.get_unique_alias(),
        "last_active": user.last_active,
        "account_age": int((datetime.utcnow() - user.created).total_seconds()),
        "kudos": user.kudos,
        "evaluating_kudos": user.evaluating_kudos,
        "suspicious": len(user.suspicions),
        "suspicion_reasons": _suspicion_details(user.suspicions),
        "flagged": user.flagged,
        "deleted": user.deleted,
        "vpn": user.vpn,
        "worker_count": len(workers),
        "paused_worker_count": sum(worker.paused for worker in workers),
        "worker_kudos": sum(worker.kudos for worker in workers),
        "worker_fulfilments": sum(worker.fulfilments for worker in workers),
        "contact": user.contact,
        "admin_comment": user.admin_comment,
    }


def _promotion_queue(*, threshold: Decimal, status: PromotionStatus, limit: int) -> list[User]:
    """Return up to ``limit`` accounts whose promotion status is ``status``, highest evaluating kudos first.

    The query is bounded by ``limit`` and walks ``ix_users_evaluating_kudos_rank`` from
    the top, so its cost follows the population above the threshold, not the table.
    The relationships ``_promotion_user_details`` reads are loaded for the returned
    rows only, in one extra query each.
    """
    return (
        db.session.query(User)
        .options(
            selectinload(User.roles),
            selectinload(User.suspicions),
            selectinload(User.workers),
        )
        .filter(User.promotion_queue_criteria(threshold, status))
        .order_by(User.evaluating_kudos.desc(), User.id.asc())
        .limit(limit)
        .all()
    )


def _promotion_queues(limit: int) -> tuple[float | None, list[PromotionReviewUser], list[PromotionReviewUser]]:
    """Return the blocked and eligible promotion queues, each at most ``limit`` long.

    The threshold is None and both queues are empty when automatic promotion has no configured threshold.
    """
    threshold = get_kudos_trust_threshold()
    if threshold is None:
        return None, [], []
    blocked = _promotion_queue(threshold=threshold, status=PromotionStatus.BLOCKED_BY_SUSPICION, limit=limit)
    eligible = _promotion_queue(threshold=threshold, status=PromotionStatus.PROMOTE, limit=limit)
    return (
        float(threshold),
        [_promotion_user_details(user) for user in blocked],
        [_promotion_user_details(user) for user in eligible],
    )


def get_moderation_overview(*, limit: int = 100) -> ModerationOverview:
    """Return promotion exceptions needing moderator review.

    Args:
        limit: Maximum number of rows returned in each independent review queue.

    Returns:
        Current promotion review queues.
    """
    promotion_threshold, blocked_users, eligible_users = _promotion_queues(limit)
    return {
        "promotion_threshold": promotion_threshold,
        "suspicion_threshold": User.SUSPICION_THRESHOLD,
        "worker_suspicion_threshold": WorkerTemplate.suspicion_threshold,
        "promotion_blocked_users": blocked_users,
        "promotion_eligible_users": eligible_users,
    }
