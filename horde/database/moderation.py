# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only set-based views for moderator operational review."""

from __future__ import annotations

import re
import string
import uuid
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from horde.classes.base.kudos import get_kudos_trust_threshold
from horde.classes.base.user import PromotionStatus, User, UserSuspicions
from horde.classes.base.worker import WORKER_ONLINE_SECONDS, WorkerModel, WorkerSuspicionEvent, WorkerSuspicions, WorkerTemplate
from horde.flask import db
from horde.suspicions import SUSPICION_LOGS, Suspicions

WORKER_TYPE_IDENTITIES: dict[str, str] = {
    "image": "stable_worker",
    "text": "text_worker",
    "interrogation": "interrogation_worker",
}
"""The API worker types, mapped to the polymorphic identities stored in ``workers.worker_type``."""


class PausedWorkerSort(StrEnum):
    """Represent the order of the paused-worker queue."""

    SUSPICION = "suspicion"
    """Most active suspicion reports first, then latest check-in."""
    LAST_CHECK_IN = "last_check_in"
    """Latest check-in first."""
    NAME = "name"
    """Worker name, ascending."""
    OWNER = "owner"
    """Owner username, then worker name, ascending."""


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


class PausedWorkerReview(TypedDict):
    """Describe a paused worker and the state relevant to moderation."""

    id: str
    """The worker ID."""
    name: str
    """The worker name."""
    type: str
    """The worker type: image, text or interrogation."""
    owner_id: int
    """The owning account's ID."""
    owner: str
    """The owning account's unique alias, ``name#id``."""
    last_check_in: datetime
    """The naive UTC time the worker last checked in."""
    online: bool
    """Whether the worker checked in within ``WORKER_ONLINE_SECONDS``."""
    maintenance_mode: bool
    """Whether the worker is in maintenance mode."""
    maintenance_msg: str
    """The maintenance message."""
    suspicious: int
    """The number of active suspicion reports on the worker."""
    suspicion_reasons: list[SuspicionReasonDetails]
    """The worker's active suspicion reasons, by code."""
    owner_suspicion: int
    """The number of active suspicion reports on the owning account."""
    owner_trusted: bool
    """Whether the owning account is trusted."""
    owner_flagged: bool
    """Whether a moderator flagged the owning account."""
    requests_fulfilled: int
    """The number of jobs the worker fulfilled."""
    aborted_jobs: int
    """The number of jobs the worker aborted."""
    bridge_agent: str
    """The worker's reported bridge agent."""
    models: list[str]
    """The models the worker serves, sorted."""
    contact: str | None
    """The owning account's contact, if any."""


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
    paused_workers: list[PausedWorkerReview]
    """Paused workers matching the filters, in the requested order."""
    paused_workers_total: int
    """The number of paused workers matching the filters, before the limit."""


class WorkerSuspicionEventDetails(TypedDict):
    """Describe one retained worker suspicion event."""

    id: int
    """The record identifier, also the cursor."""
    created: datetime
    """The naive UTC time the suspicion was reported."""
    worker_id: str
    """The UUID of the worker the suspicion was raised against."""
    worker_name: str
    """The worker's name when the suspicion was reported."""
    user_id: int
    """The account that owned the worker when the suspicion was reported."""
    suspicion_id: int
    """The numeric suspicion reason code."""
    reason: str
    """The reason's ``Suspicions`` name, or ``UNKNOWN_<id>`` for a code this version does not define."""
    detail: str
    """The suspicion's diagnostic text."""


class WorkerSuspicionEventsPage(TypedDict):
    """Represent a cursor-paginated page of worker suspicion events."""

    events: list[WorkerSuspicionEventDetails]
    """The page's records, newest first."""
    next_cursor: int | None
    """The ``before_id`` of the next page, or None on the last page."""


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
    suspicions: Iterable[UserSuspicions | WorkerSuspicions],
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


def _paused_worker_details(worker: WorkerTemplate, models: list[str]) -> PausedWorkerReview:
    return {
        "id": str(worker.id),
        "name": worker.name,
        "type": worker.wtype,
        "owner_id": worker.user_id,
        "owner": worker.user.get_unique_alias(),
        "last_check_in": worker.last_check_in,
        "online": not worker.is_stale(),
        "maintenance_mode": worker.maintenance,
        "maintenance_msg": worker.maintenance_msg,
        "suspicious": len(worker.suspicions),
        "suspicion_reasons": _suspicion_details(worker.suspicions),
        "owner_suspicion": len(worker.user.suspicions),
        "owner_trusted": worker.user.trusted,
        "owner_flagged": worker.user.flagged,
        "requests_fulfilled": worker.fulfilments,
        "aborted_jobs": worker.aborted_jobs,
        "bridge_agent": worker.bridge_agent,
        "models": models,
        "contact": worker.user.contact,
    }


def _promotion_queues(limit: int) -> tuple[float | None, list[PromotionReviewUser], list[PromotionReviewUser]]:
    """Return the promotion threshold and the blocked and eligible promotion queues, each at most ``limit`` long.

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


def get_moderation_overview(
    *,
    limit: int = 100,
    worker_types: Collection[str] | None = None,
    suspicion_ids: Collection[int] | None = None,
    online: bool | None = None,
    sort: PausedWorkerSort = PausedWorkerSort.LAST_CHECK_IN,
) -> ModerationOverview:
    """Return promotion exceptions and paused workers needing moderator review.

    The paused-worker filters narrow that queue alone; the promotion queues are unaffected.

    Args:
        limit: Maximum number of rows returned in each independent review queue.
        worker_types: Keep paused workers of these API types (image, text, interrogation).
        suspicion_ids: Keep paused workers currently carrying any of these suspicion reasons.
        online: Keep paused workers that checked in within the online window (True) or not (False).
        sort: Paused-worker order: most suspicion reports, latest check-in, name, or owner username.

    Returns:
        Current promotion and worker moderation queues.
    """
    promotion_threshold, blocked_users, eligible_users = _promotion_queues(limit)

    paused_query = db.session.query(WorkerTemplate).filter(WorkerTemplate.paused.is_(True))
    if worker_types:
        paused_query = paused_query.filter(
            WorkerTemplate.worker_type.in_([WORKER_TYPE_IDENTITIES[worker_type] for worker_type in worker_types]),
        )
    if suspicion_ids:
        paused_query = paused_query.filter(
            WorkerTemplate.suspicions.any(WorkerSuspicions.suspicion_id.in_(list(suspicion_ids))),
        )
    if online is not None:
        recent = WorkerTemplate.last_check_in > datetime.utcnow() - timedelta(seconds=WORKER_ONLINE_SECONDS)
        paused_query = paused_query.filter(recent if online else ~recent)
    paused_total = paused_query.order_by(None).count()
    if sort == PausedWorkerSort.SUSPICION:
        report_count = select(func.count(WorkerSuspicions.id)).where(WorkerSuspicions.worker_id == WorkerTemplate.id).scalar_subquery()
        ordering = [
            report_count.desc(),
            WorkerTemplate.last_check_in.desc(),
        ]
    elif sort == PausedWorkerSort.NAME:
        ordering = [WorkerTemplate.name.asc()]
    elif sort == PausedWorkerSort.OWNER:
        paused_query = paused_query.join(User, User.id == WorkerTemplate.user_id)
        ordering = [
            User.username.asc(),
            WorkerTemplate.name.asc(),
        ]
    else:
        ordering = [WorkerTemplate.last_check_in.desc()]
    paused_workers = (
        paused_query.options(
            selectinload(WorkerTemplate.user).selectinload(User.roles),
            selectinload(WorkerTemplate.user).selectinload(User.suspicions),
            selectinload(WorkerTemplate.suspicions),
        )
        .order_by(*ordering, WorkerTemplate.id.asc())
        .limit(limit)
        .all()
    )
    worker_ids = [worker.id for worker in paused_workers]
    model_rows = (
        db.session.query(WorkerModel.worker_id, WorkerModel.model).filter(WorkerModel.worker_id.in_(worker_ids)).all() if worker_ids else []
    )
    models_by_worker: dict[uuid.UUID | str, list[str]] = {}
    for worker_id, model in model_rows:
        models_by_worker.setdefault(worker_id, []).append(model)

    return {
        "promotion_threshold": promotion_threshold,
        "suspicion_threshold": User.SUSPICION_THRESHOLD,
        "worker_suspicion_threshold": WorkerTemplate.suspicion_threshold,
        "promotion_blocked_users": blocked_users,
        "promotion_eligible_users": eligible_users,
        "paused_workers": [_paused_worker_details(worker, sorted(models_by_worker.get(worker.id, []))) for worker in paused_workers],
        "paused_workers_total": paused_total,
    }


def get_worker_suspicion_events(
    *,
    limit: int = 100,
    before_id: int | None = None,
    worker_id: str | None = None,
    user_id: int | None = None,
) -> WorkerSuspicionEventsPage:
    """Return a newest-first page of history, retained after worker deletion or suspicion reset."""
    query = db.session.query(WorkerSuspicionEvent)
    if before_id is not None:
        query = query.filter(WorkerSuspicionEvent.id < before_id)
    if worker_id is not None:
        query = query.filter(WorkerSuspicionEvent.worker_id == worker_id)
    if user_id is not None:
        query = query.filter(WorkerSuspicionEvent.user_id == user_id)
    rows = query.order_by(WorkerSuspicionEvent.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    events: list[WorkerSuspicionEventDetails] = [
        {
            "id": row.id,
            "created": row.created,
            "worker_id": str(row.worker_id),
            "worker_name": row.worker_name,
            "user_id": row.user_id,
            "suspicion_id": row.suspicion_id,
            "reason": _suspicion_name(row.suspicion_id),
            "detail": row.detail,
        }
        for row in rows
    ]
    return {
        "events": events,
        "next_cursor": rows[-1].id if has_more else None,
    }
