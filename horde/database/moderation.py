# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only set-based views for moderator operational review."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Literal, TypedDict

from sqlalchemy.orm import selectinload

from horde.classes.base.kudos import get_kudos_trust_threshold
from horde.classes.base.user import PromotionStatus, User, UserSuspicions
from horde.classes.base.worker import WorkerModel, WorkerSuspicionEvent, WorkerSuspicions, WorkerTemplate
from horde.flask import db
from horde.suspicions import SUSPICION_LOGS, Suspicions

PROMOTION_CANDIDATE_PAGE = 500


class SuspicionReasonDetails(TypedDict):
    """Describe one active suspicion reason and its occurrence count."""

    id: int
    name: str
    description: str
    count: int


class PromotionReviewUser(TypedDict):
    """Describe a user who currently satisfies the automatic-promotion prerequisites."""

    id: int
    username: str
    created: datetime
    last_active: datetime
    account_age: int
    kudos: float
    evaluating_kudos: float
    promotion_threshold: float
    threshold_excess: float
    suspicious: int
    suspicion_threshold: int
    suspicion_reasons: list[SuspicionReasonDetails]
    blockers: list[Literal["suspicion"]]
    flagged: bool
    deleted: bool
    vpn: bool
    worker_count: int
    paused_worker_count: int
    worker_kudos: float
    worker_fulfilments: int
    contact: str | None
    admin_comment: str | None


class PausedWorkerReview(TypedDict):
    """Describe a paused worker and the state relevant to moderation."""

    id: str
    name: str
    type: str
    owner_id: int
    owner: str
    created: datetime
    last_check_in: datetime
    online: bool
    paused: bool
    maintenance_mode: bool
    maintenance_msg: str | None
    suspicious: int
    suspicion_threshold: int
    suspicion_reasons: list[SuspicionReasonDetails]
    owner_suspicion: int
    owner_trusted: bool
    owner_flagged: bool
    kudos_rewards: float
    kudos_details: dict[str, int]
    requests_fulfilled: int
    uncompleted_jobs: int
    aborted_jobs: int
    contributions: float
    uptime: int
    threads: int
    bridge_agent: str
    models: list[str]
    ipaddr: str | None
    contact: str | None


class ModerationOverview(TypedDict):
    """Group operational exceptions into independently limited review queues."""

    promotion_enabled: bool
    promotion_threshold: float | None
    promotion_blocked_users: list[PromotionReviewUser]
    promotion_eligible_users: list[PromotionReviewUser]
    paused_workers: list[PausedWorkerReview]


class WorkerSuspicionEventDetails(TypedDict):
    """Describe one immutable worker suspicion audit event."""

    id: int
    created: datetime
    worker_id: str
    worker_name: str
    user_id: int
    suspicion_id: int
    reason: str
    amount: int
    detail: str


class WorkerSuspicionEventsPage(TypedDict):
    """Represent a cursor-paginated page of worker suspicion events."""

    events: list[WorkerSuspicionEventDetails]
    next_cursor: int | None


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
            name = reason.name
            description = SUSPICION_LOGS[reason]
        except ValueError:
            name = f"UNKNOWN_{suspicion_id}"
            description = "Unknown suspicion reason"
        details.append(
            {
                "id": suspicion_id,
                "name": name,
                "description": description,
                "count": count,
            },
        )
    return details


def _promotion_user_details(user: User, threshold: float) -> PromotionReviewUser:
    suspicion_count = len(user.suspicions)
    blockers: list[Literal["suspicion"]] = []
    if suspicion_count >= User.SUSPICION_THRESHOLD:
        blockers.append("suspicion")
    workers = list(user.workers)
    return {
        "id": user.id,
        "username": user.get_unique_alias(),
        "created": user.created,
        "last_active": user.last_active,
        "account_age": int((datetime.utcnow() - user.created).total_seconds()),
        "kudos": user.kudos,
        "evaluating_kudos": user.evaluating_kudos,
        "promotion_threshold": threshold,
        "threshold_excess": float(user.evaluating_kudos) - threshold,
        "suspicious": suspicion_count,
        "suspicion_threshold": User.SUSPICION_THRESHOLD,
        "suspicion_reasons": _suspicion_details(user.suspicions),
        "blockers": blockers,
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


def _paused_worker_details(worker: WorkerTemplate, models: list[str]) -> PausedWorkerReview:
    return {
        "id": str(worker.id),
        "name": worker.name,
        "type": worker.wtype,
        "owner_id": worker.user_id,
        "owner": worker.user.get_unique_alias(),
        "created": worker.created,
        "last_check_in": worker.last_check_in,
        "online": not worker.is_stale(),
        "paused": worker.paused,
        "maintenance_mode": worker.maintenance,
        "maintenance_msg": worker.maintenance_msg,
        "suspicious": len(worker.suspicions),
        "suspicion_threshold": worker.suspicion_threshold,
        "suspicion_reasons": _suspicion_details(worker.suspicions),
        "owner_suspicion": len(worker.user.suspicions),
        "owner_trusted": worker.user.trusted,
        "owner_flagged": worker.user.flagged,
        "kudos_rewards": worker.kudos,
        "kudos_details": worker.get_kudos_details(),
        "requests_fulfilled": worker.fulfilments,
        "uncompleted_jobs": worker.uncompleted_jobs,
        "aborted_jobs": worker.aborted_jobs,
        "contributions": worker.contributions,
        "uptime": worker.uptime,
        "threads": worker.threads,
        "bridge_agent": worker.bridge_agent,
        "models": models,
        "ipaddr": worker.ipaddr,
        "contact": worker.user.contact,
    }


def get_moderation_overview(*, limit: int = 100) -> ModerationOverview:
    """Return promotion exceptions and paused workers needing moderator review.

    Args:
        limit: Maximum number of rows returned in each independent review queue.

    Returns:
        Current promotion and worker moderation queues.
    """
    threshold_value = get_kudos_trust_threshold()
    blocked_promotion_users: list[User] = []
    eligible_promotion_users: list[User] = []
    if threshold_value is not None:
        # The query only bounds the population: untrusted accounts above the threshold.
        # It must stay a superset of what promotion_status accepts, never a restatement
        # of it, so the decision has exactly one implementation. Candidates are read in
        # pages, newest-kudos first, until both queues are full.
        candidates = (
            db.session.query(User)
            .options(
                selectinload(User.roles),
                selectinload(User.suspicions),
                selectinload(User.workers),
            )
            .filter(~User.trusted, User.evaluating_kudos > threshold_value)
            .order_by(User.evaluating_kudos.desc(), User.id.asc())
        )
        offset = 0
        while len(blocked_promotion_users) < limit or len(eligible_promotion_users) < limit:
            page = candidates.offset(offset).limit(PROMOTION_CANDIDATE_PAGE).all()
            for user in page:
                status = user.promotion_status(threshold_value)
                if status is PromotionStatus.PROMOTE and len(eligible_promotion_users) < limit:
                    eligible_promotion_users.append(user)
                elif status is PromotionStatus.BLOCKED_BY_SUSPICION and len(blocked_promotion_users) < limit:
                    blocked_promotion_users.append(user)
            if len(page) < PROMOTION_CANDIDATE_PAGE:
                break
            offset += PROMOTION_CANDIDATE_PAGE

    threshold = float(threshold_value) if threshold_value is not None else None
    blocked_users = [_promotion_user_details(user, threshold) for user in blocked_promotion_users] if threshold is not None else []
    eligible_users = [_promotion_user_details(user, threshold) for user in eligible_promotion_users] if threshold is not None else []

    paused_workers = (
        db.session.query(WorkerTemplate)
        .options(
            selectinload(WorkerTemplate.user).selectinload(User.roles),
            selectinload(WorkerTemplate.user).selectinload(User.suspicions),
            selectinload(WorkerTemplate.suspicions),
            selectinload(WorkerTemplate.stats),
        )
        .filter(WorkerTemplate.paused.is_(True))
        .order_by(WorkerTemplate.last_check_in.desc(), WorkerTemplate.id.asc())
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
        "promotion_enabled": threshold is not None,
        "promotion_threshold": threshold,
        "promotion_blocked_users": blocked_users,
        "promotion_eligible_users": eligible_users,
        "paused_workers": [_paused_worker_details(worker, sorted(models_by_worker.get(worker.id, []))) for worker in paused_workers],
    }


def get_worker_suspicion_events(
    *,
    limit: int = 100,
    before_id: int | None = None,
    worker_id: str | None = None,
    user_id: int | None = None,
    suspicion_id: int | None = None,
) -> WorkerSuspicionEventsPage:
    """Return an immutable, newest-first page of worker suspicion events.

    Args:
        limit: Maximum number of events in the page.
        before_id: Return events with an ID below this cursor.
        worker_id: Restrict events to one worker UUID.
        user_id: Restrict events to workers owned by this user at event time.
        suspicion_id: Restrict events to one suspicion reason.

    Returns:
        The matching event page and a cursor when more events are available.
    """
    query = db.session.query(WorkerSuspicionEvent)
    if before_id is not None:
        query = query.filter(WorkerSuspicionEvent.id < before_id)
    if worker_id is not None:
        query = query.filter(WorkerSuspicionEvent.worker_id == worker_id)
    if user_id is not None:
        query = query.filter(WorkerSuspicionEvent.user_id == user_id)
    if suspicion_id is not None:
        query = query.filter(WorkerSuspicionEvent.suspicion_id == suspicion_id)
    rows = query.order_by(WorkerSuspicionEvent.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    events: list[WorkerSuspicionEventDetails] = []
    for row in rows:
        try:
            reason_name = Suspicions(row.suspicion_id).name
        except ValueError:
            reason_name = f"UNKNOWN_{row.suspicion_id}"
        events.append(
            {
                "id": row.id,
                "created": row.created,
                "worker_id": str(row.worker_id),
                "worker_name": row.worker_name,
                "user_id": row.user_id,
                "suspicion_id": row.suspicion_id,
                "reason": reason_name,
                "amount": row.amount,
                "detail": row.detail,
            },
        )
    return {
        "events": events,
        "next_cursor": rows[-1].id if has_more and rows else None,
    }
