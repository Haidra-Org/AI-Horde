# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The kudos currency and statistics projectors.

The applier is the single writer of the materialized currency columns
(``users.kudos``, ``users.evaluating_kudos``) and of the derived statistical
rows (``workers.kudos``, ``user_stats``, ``worker_stats``, ``user_records``, and
the ``workers.contributions``/``workers.fulfilments`` aggregates) after cutover. It is
the consuming half of a single-consumer work queue (transactional-outbox
consumption with per-row state): each cycle claims the rows still flagged
unapplied, folds them into per-account sums and per-dimension counter totals,
writes one UPDATE per touched row, reproduces the historical balance floor on the
spendable balance, and flips those rows' ``applied`` flag in the same
transaction. Balances and counters therefore materialize atomically from one
claimed batch.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from loguru import logger
from sqlalchemy import func

from horde.classes.base.kudos import (
    KudosLedger,
    KudosLedgerApplierState,
    KudosReservation,
    KudosStatEvent,
    emit_kudos_ledger_entry,
    get_kudos_ledger_mode,
    get_kudos_trust_threshold,
    kudos_event,
)
from horde.classes.base.team import Team
from horde.classes.base.user import User, UserRecords, UserRole, UserStats
from horde.classes.base.worker import WorkerStats, WorkerTemplate
from horde.database.kudos_counters import increment_counter
from horde.database.kudos_db import try_acquire_applier_lock
from horde.database.kudos_reservations import consume_reservation, release_event_reservations
from horde.enums import (
    KudosAggregate,
    KudosAuditDetail,
    KudosEntryType,
    KudosLedgerMode,
    KudosStatRecord,
    UserRecordTypes,
    UserRoleTypes,
)
from horde.flask import db
from horde.metrics import (
    kudos_applier_folded,
    kudos_floor_adjustments,
    kudos_floor_adjustments_created,
)

# Cap how many rows one cycle folds so that catching up after applier downtime
# cannot load an unbounded tail into memory. Rows folded this cycle are marked
# applied, so the next cycle continues with whatever remains unapplied.
KUDOS_APPLIER_BATCH_SIZE = 1000
# One scheduler tick keeps folding while a cycle drains a full batch, up to this
# many cycles, so a backlog clears at many batches per tick instead of one. The
# bound keeps a tick from monopolizing the projector while each cycle remains its
# own small transaction.
KUDOS_APPLIER_MAX_CATCHUP_CYCLES = 10
# Applied rows are audit history; keep a rolling window and prune the rest.
KUDOS_LEDGER_RETENTION = timedelta(days=30)
KUDOS_PRUNE_BATCH_SIZE = 5000
_APPLIER_STATE_ID = 1
# A database-scoped ownership key. Redis quorum decides which replica should
# try the work; this lock decides which transaction is allowed to do it.


def _acquire_applier_lock() -> bool:
    """Acquire the transaction-scoped Postgres applier lock without waiting."""
    return try_acquire_applier_lock()


def get_applier_state() -> KudosLedgerApplierState:
    """Return the single applier-state row, creating it if absent."""
    state = db.session.query(KudosLedgerApplierState).filter_by(id=_APPLIER_STATE_ID).first()
    if state is None:
        state = KudosLedgerApplierState(id=_APPLIER_STATE_ID, applied_at=None)
        db.session.add(state)
        db.session.flush()
    return state


def apply_pending_kudos(
    now: datetime | None = None,
    batch_size: int = KUDOS_APPLIER_BATCH_SIZE,
    *,
    commit: bool = True,
    lock_already_held: bool = False,
) -> int:
    """Fold unapplied currency and statistics events and mark them applied.

    This is the consuming half of a single-consumer work queue (transactional-
    outbox consumption with per-row state). Each cycle claims up to ``batch_size``
    rows still flagged unapplied, ordered by id, sums them per account and
    balance, applies one UPDATE per touched account (clamping the spendable
    balance up to the per-class floor), and flips exactly those rows' ``applied``
    flag. The balance UPDATEs and the flag flip commit in one transaction, so
    folding is exactly-once: a crashed cycle commits nothing, leaves the rows
    unapplied, and re-reads them on restart. A row whose transaction commits late
    (a lower id becoming visible after higher ids were folded) is simply claimed
    in whatever later cycle first sees it unapplied, so there is no id/txid
    inversion loss mode.

    Args:
        now: Reference time for the lag heartbeat (injectable for tests).
            Defaults to ``utcnow``.
        batch_size: Maximum rows folded in this cycle; the next cycle continues
            with whatever remains unapplied.

    Returns:
        The number of ledger rows folded this cycle plus any promotion-drain
        postings emitted (see :func:`_drain_trusted_escrow`). A return of 0 means
        no unapplied rows remain and no trusted escrow needs draining, so a caller
        folding to quiescence can stop.
    """
    if now is None:
        now = datetime.utcnow()
    if not lock_already_held and not _acquire_applier_lock():
        # Do not leave an idle transaction open merely because another replica
        # owns the projector.  The owner will process the queue.
        db.session.rollback()
        return 0
    state = get_applier_state()
    rows = (
        db.session.query(KudosLedger)
        .filter(KudosLedger.applied.is_(False))
        .order_by(KudosLedger.id.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .all()
    )
    stat_rows = (
        db.session.query(KudosStatEvent)
        .filter(KudosStatEvent.applied.is_(False))
        .order_by(KudosStatEvent.id.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .all()
    )

    if rows or stat_rows:
        user_balance_deltas: dict[int, Decimal] = defaultdict(Decimal)
        user_escrow_deltas: dict[int, Decimal] = defaultdict(Decimal)
        user_last_active: dict[int, datetime] = {}
        worker_deltas: dict[object, Decimal] = defaultdict(Decimal)
        # Counter folds ride the same claimed batch and the same transaction as the
        # balance fold, so one cycle materializes balances and every derived counter
        # atomically. Each counter reconstructs its row by grouping the batch on the
        # dimension the posting carries.
        user_stats_deltas: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
        worker_stats_deltas: dict[tuple[object, str], Decimal] = defaultdict(Decimal)
        user_record_deltas: dict[tuple[int, str, str], Decimal] = defaultdict(Decimal)
        worker_contribution_deltas: dict[object, Decimal] = defaultdict(Decimal)
        worker_fulfilment_deltas: dict[object, Decimal] = defaultdict(Decimal)
        # Team aggregates are derived from the worker's own postings stamped with a
        # team_id: kudos from the balance-credit posting, contributions/fulfilments
        # from the worker STAT_CONTRIBUTION postings. team_id is read independently
        # of the balance target, so a stamped worker posting feeds both.
        team_kudos_deltas: dict[object, Decimal] = defaultdict(Decimal)
        team_contribution_deltas: dict[object, Decimal] = defaultdict(Decimal)
        team_fulfilment_deltas: dict[object, Decimal] = defaultdict(Decimal)
        reservation_consumptions: dict[str, Decimal] = defaultdict(Decimal)
        folded_ids = [row.id for row in rows]
        folded_stat_ids = [row.id for row in stat_rows]
        if folded_ids:
            kudos_applier_folded.add(len(folded_ids), {"horde.kudos.row_type": "currency"})
        if folded_stat_ids:
            kudos_applier_folded.add(len(folded_stat_ids), {"horde.kudos.row_type": "stat"})
        for row in rows:
            if row.escrow:
                user_escrow_deltas[row.user_id] += row.amount
                continue
            user_balance_deltas[row.user_id] += row.amount
            reservation_id = row.detail.get(KudosAuditDetail.RESERVATION_ID) if row.detail else None
            if row.amount < 0 and isinstance(reservation_id, str):
                reservation_consumptions[reservation_id] += -row.amount

        for row in stat_rows:
            if row.user_id is not None and row.detail and row.detail.get(KudosAuditDetail.TOUCH_LAST_ACTIVE):
                user_last_active[row.user_id] = max(user_last_active.get(row.user_id, row.created), row.created)
            if row.record == KudosStatRecord.USER_KUDOS:
                if row.user_id is None or row.stat_action is None:
                    continue
                user_stats_deltas[(row.user_id, row.stat_action)] += row.amount
            elif row.record == KudosStatRecord.WORKER_KUDOS:
                if row.worker_id is None or row.stat_action is None:
                    continue
                worker_deltas[row.worker_id] += row.amount
                worker_stats_deltas[(row.worker_id, row.stat_action)] += row.amount
                if row.team_id is not None:
                    team_kudos_deltas[row.team_id] += row.amount
            elif row.entry_type == KudosEntryType.STAT_RECORD:
                if row.user_id is None or row.stat_action is None or row.record is None:
                    continue
                user_record_deltas[(row.user_id, row.stat_action, row.record)] += row.amount
            elif row.entry_type == KudosEntryType.STAT_CONTRIBUTION:
                if row.worker_id is None:
                    continue
                if row.stat_action == KudosAggregate.CONTRIBUTIONS:
                    worker_contribution_deltas[row.worker_id] += row.amount
                    if row.team_id is not None:
                        team_contribution_deltas[row.team_id] += row.amount
                elif row.stat_action == KudosAggregate.FULFILMENTS:
                    worker_fulfilment_deltas[row.worker_id] += row.amount
                    if row.team_id is not None:
                        team_fulfilment_deltas[row.team_id] += row.amount

        _apply_user_deltas(user_balance_deltas, user_escrow_deltas, user_last_active)
        _apply_worker_deltas(worker_deltas)
        _apply_user_stats_deltas(user_stats_deltas)
        _apply_worker_stats_deltas(worker_stats_deltas)
        _apply_user_record_deltas(user_record_deltas)
        _apply_worker_contribution_deltas(worker_contribution_deltas, worker_fulfilment_deltas)
        _apply_team_deltas(team_contribution_deltas, team_fulfilment_deltas, team_kudos_deltas)
        if folded_ids:
            _mark_applied(folded_ids)
        if folded_stat_ids:
            _mark_stat_events_applied(folded_stat_ids)
        for business_id, amount in sorted(reservation_consumptions.items()):
            consume_reservation(business_id, amount)
        # Transfer holds remain active until the entire event has materialized.
        # A batch is allowed to split an event, so release only event ids with no
        # unapplied posting left after this batch's marker update.
        candidate_event_ids = {row.event_id for row in rows}
        incomplete_event_ids = {
            event_id
            for (event_id,) in (
                db.session.query(KudosLedger.event_id)
                .filter(
                    KudosLedger.event_id.in_(candidate_event_ids),
                    KudosLedger.applied.is_(False),
                )
                .distinct()
                .all()
            )
        }
        release_event_reservations(candidate_event_ids - incomplete_event_ids)

    # A trusted user's escrow always drains to their spendable balance; the
    # applier owns that movement so promotion timing cannot strand an escrow
    # posting. The emitted pairs are folded by a subsequent cycle. Promotion and
    # the drain mutate balances, which in shadow mode belong to the inline legacy
    # projection (project_trust_promotion); only the ledger-owned projector may run
    # them, so both are gated on the mode pinned in this applier transaction. The
    # heartbeat is stamped every cycle regardless (even folding nothing) so the lag
    # metric tracks applier staleness rather than a quiet period.
    if get_kudos_ledger_mode() == KudosLedgerMode.LEDGER:
        _promote_eligible_users(now)
        drained = _drain_trusted_escrow()
    else:
        drained = 0
    state.applied_at = now
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return len(rows) + len(stat_rows) + drained


def _mark_applied(folded_ids: list[int]) -> None:
    """Flag exactly the folded rows applied with one bulk UPDATE.

    Marking the exact folded ids (never an id range) is what keeps a
    late-committing lower id that was not part of this fold from being flagged
    applied without having been folded.
    """
    (db.session.query(KudosLedger).filter(KudosLedger.id.in_(folded_ids)).update({KudosLedger.applied: True}, synchronize_session=False))


def _mark_stat_events_applied(folded_ids: list[int]) -> None:
    """Flag exactly the folded statistics events applied."""
    (
        db.session.query(KudosStatEvent)
        .filter(KudosStatEvent.id.in_(folded_ids))
        .update({KudosStatEvent.applied: True}, synchronize_session=False)
    )


def _drain_trusted_escrow() -> int:
    """Emit EVALUATION_PROMOTION delta pairs draining trusted users' escrow.

    Each cycle scans for trusted users still carrying positive escrow and emits a
    delta pair (escrow debit, balance credit) for the full escrow amount under one
    event id; a subsequent cycle folds the pair, after which the escrow is zero
    and the scan stops finding the user. The scan self-heals a user promoted after
    their escrow was folded in an earlier cycle, and it subsumes the users touched
    by this cycle's own escrow fold (they surface as positive escrow once folded).

    A user with a drain pair already emitted but not yet folded is skipped. Until
    that pair folds, the materialized escrow is still positive, so without this
    guard a cycle that could not fold the pair (a crash or a batch bound before it
    is claimed) would emit a fresh pair, over-crediting the balance once the
    backlog folds. Counting the user's unapplied EVALUATION_PROMOTION postings is
    a reliable in-flight guard: the applier's own prior pairs are committed, so
    they are visible here whether or not they have been folded yet.

    Returns:
        The number of ledger postings emitted (two per drained user).
    """
    pending_drain_user_ids = {
        user_id
        for (user_id,) in (
            db.session.query(KudosLedger.user_id)
            .filter(
                KudosLedger.entry_type == KudosEntryType.EVALUATION_PROMOTION,
                KudosLedger.applied.is_(False),
            )
            .distinct()
            .all()
        )
    }
    # Read trust state and the drain amount straight from committed columns rather
    # than from ORM instances: set_trusted commits the TRUSTED role without
    # refreshing the user's in-memory role collection, so an instance attribute
    # can report a stale, pre-promotion trust state.
    drain_targets = db.session.query(User.id, User.evaluating_kudos).filter(User.trusted, User.evaluating_kudos > 0).all()
    emitted = 0
    for user_id, amount in drain_targets:
        if user_id in pending_drain_user_ids:
            continue
        with kudos_event():
            emit_kudos_ledger_entry(
                KudosEntryType.EVALUATION_PROMOTION,
                -amount,
                user_id=user_id,
                escrow=True,
                force_projection=True,
            )
            emit_kudos_ledger_entry(
                KudosEntryType.EVALUATION_PROMOTION,
                amount,
                user_id=user_id,
                force_projection=True,
            )
        emitted += 2
    return emitted


def _promote_eligible_users(now: datetime) -> None:
    """Promote every mature user whose newly projected escrow crossed threshold.

    Promotion belongs to the projector because request transactions cannot see
    their own still-unprojected escrow credit.  This guarantees that the final
    qualifying contribution promotes the user even if they never submit again.
    """
    threshold = get_kudos_trust_threshold()
    if threshold is None:
        return
    trusted_role_exists = (
        db.session.query(UserRole.id)
        .filter(
            UserRole.user_id == User.id,
            UserRole.user_role == UserRoleTypes.TRUSTED,
            UserRole.value.is_(True),
        )
        .exists()
    )
    candidates = (
        db.session.query(User)
        .filter(
            ~trusted_role_exists,
            User.evaluating_kudos > threshold,
            User.created <= now - timedelta(days=7),
        )
        .order_by(User.id.asc())
        .all()
    )
    for user in candidates:
        if user.is_anon() or user.is_suspicious():
            continue
        role = db.session.query(UserRole).filter_by(user_id=user.id, user_role=UserRoleTypes.TRUSTED).first()
        if role is None:
            role = UserRole(user_id=user.id, user_role=UserRoleTypes.TRUSTED, value=True)
            db.session.add(role)
        else:
            role.value = True
        for worker in cast(list[WorkerTemplate], user.workers):
            worker.paused = False
        logger.info(f"Kudos applier promoted user {user.id} to trusted")
    # Make the new roles visible to the SQL hybrid used by the drain query.
    db.session.flush()
    # record_contributions commonly loaded ``user.roles`` earlier in this same
    # session to decide whether to escrow. Expire that relationship after the
    # SQL-level role insert so subsequent instance-level ``user.trusted`` reads
    # cannot remain stuck on the pre-promotion collection.
    for user in candidates:
        db.session.expire(user, ["roles"])


def _apply_user_deltas(
    balance_deltas: dict[int, Decimal],
    escrow_deltas: dict[int, Decimal],
    last_active: dict[int, datetime] | None = None,
) -> None:
    activity = last_active or {}
    user_ids = set(balance_deltas) | set(escrow_deltas) | set(activity)
    if not user_ids:
        return
    users = db.session.query(User).filter(User.id.in_(user_ids)).order_by(User.id.asc()).all()
    for user in users:
        if user.id in balance_deltas:
            requested_balance = round(user.kudos + balance_deltas[user.id], 2)
            floor = user.get_min_kudos()
            user.kudos = floor if requested_balance < floor else requested_balance
            if requested_balance < floor:
                # Flooring is intentionally retained for compatibility, but it
                # creates currency. Record that creation explicitly so snapshot
                # replay remains linear and every forgiven debit is auditable.
                created = floor - requested_balance
                correction = emit_kudos_ledger_entry(
                    KudosEntryType.FLOOR_ADJUSTMENT,
                    created,
                    user_id=user.id,
                    detail={KudosAuditDetail.REASON: "minimum_balance_floor"},
                )
                correction.applied = True
                logger.info(f"Kudos floor adjustment created {created} kudos for user {user.id}")
                kudos_floor_adjustments.add(1)
                kudos_floor_adjustments_created.add(float(created))
        if user.id in escrow_deltas:
            user.evaluating_kudos = round(user.evaluating_kudos + escrow_deltas[user.id], 2)
        if user.id in activity and (user.last_active is None or activity[user.id] > user.last_active):
            user.last_active = activity[user.id]


def _apply_worker_deltas(worker_deltas: dict[object, Decimal]) -> None:
    if not worker_deltas:
        return
    workers = db.session.query(WorkerTemplate).filter(WorkerTemplate.id.in_(worker_deltas)).order_by(WorkerTemplate.id.asc()).all()
    for worker in workers:
        worker.kudos = round(worker.kudos + worker_deltas[worker.id], 2)


def _increment_or_insert(
    model: type,
    filters: dict[str, object],
    delta: Decimal,
    extra: dict[str, object] | None = None,
) -> None:
    """Fold ``delta`` into ``model``'s ``value`` for ``filters``, inserting if absent.

    Reproduces the historical update-then-insert the request path used for the
    stats and record rows: a round-then-sum increment on the existing row, or a
    rounded insert when none exists. Single-writer applier ownership removes the
    first-insert race the request path had to guard against, so no uniqueness or
    ON CONFLICT is needed here.
    """
    increment_counter(model, filters | (extra or {}), delta)


def _apply_user_stats_deltas(deltas: dict[tuple[int, str], Decimal]) -> None:
    for (user_id, action), delta in sorted(deltas.items()):
        _increment_or_insert(UserStats, {"user_id": user_id, "action": action}, delta)


def _apply_worker_stats_deltas(deltas: dict[tuple[object, str], Decimal]) -> None:
    for (worker_id, action), delta in sorted(deltas.items(), key=lambda item: (str(item[0][0]), item[0][1])):
        _increment_or_insert(WorkerStats, {"worker_id": worker_id, "action": action}, delta)


def _apply_user_record_deltas(deltas: dict[tuple[int, str, str], Decimal]) -> None:
    for (user_id, record_type_name, record), delta in sorted(deltas.items()):
        record_type = UserRecordTypes[record_type_name]
        _increment_or_insert(
            UserRecords,
            {"user_id": user_id, "record_type": record_type, "record": record},
            delta,
        )


def _apply_worker_contribution_deltas(
    contribution_deltas: dict[object, Decimal],
    fulfilment_deltas: dict[object, Decimal],
) -> None:
    worker_ids = set(contribution_deltas) | set(fulfilment_deltas)
    if not worker_ids:
        return
    workers = db.session.query(WorkerTemplate).filter(WorkerTemplate.id.in_(worker_ids)).order_by(WorkerTemplate.id.asc()).all()
    for worker in workers:
        if worker.id in contribution_deltas:
            worker.contributions = round(worker.contributions + contribution_deltas[worker.id], 2)
        if worker.id in fulfilment_deltas:
            worker.fulfilments = worker.fulfilments + int(fulfilment_deltas[worker.id])


def _apply_team_deltas(
    contribution_deltas: dict[object, Decimal],
    fulfilment_deltas: dict[object, Decimal],
    kudos_deltas: dict[object, Decimal],
) -> None:
    team_ids = set(contribution_deltas) | set(fulfilment_deltas) | set(kudos_deltas)
    if not team_ids:
        return
    teams = db.session.query(Team).filter(Team.id.in_(team_ids)).order_by(Team.id.asc()).all()
    for team in teams:
        if team.id in contribution_deltas:
            team.contributions = round(team.contributions + contribution_deltas[team.id], 2)
        if team.id in fulfilment_deltas:
            team.fulfilments = team.fulfilments + int(fulfilment_deltas[team.id])
        if team.id in kudos_deltas:
            team.kudos = round(team.kudos + kudos_deltas[team.id], 2)


def prune_applied_kudos_ledger(
    now: datetime | None = None,
    retention: timedelta = KUDOS_LEDGER_RETENTION,
    batch_size: int = KUDOS_PRUNE_BATCH_SIZE,
) -> int:
    """Retain the permanent ledger archive (compatibility no-op).

    The parameters and function remain for one compatibility release so an old
    scheduled caller is harmless during a rolling deployment.
    """
    del now, retention, batch_size
    return 0


def kudos_applier_lag(now: datetime | None = None) -> float | None:
    """Return seconds since the applier last folded, or ``None`` if it never has."""
    if now is None:
        now = datetime.utcnow()
    state = db.session.query(KudosLedgerApplierState).filter_by(id=_APPLIER_STATE_ID).first()
    if state is None or state.applied_at is None:
        return None
    return (now - state.applied_at).total_seconds()


def kudos_applier_health(now: datetime | None = None) -> dict[str, int | float | None]:
    """Return heartbeat and real queue-lag health for probes and operators."""
    reference = now or datetime.utcnow()
    ledger_pending_count, ledger_oldest_created = (
        db.session.query(func.count(KudosLedger.id), func.min(KudosLedger.created)).filter(KudosLedger.applied.is_(False)).one()
    )
    stat_pending_count, stat_oldest_created = (
        db.session.query(func.count(KudosStatEvent.id), func.min(KudosStatEvent.created)).filter(KudosStatEvent.applied.is_(False)).one()
    )
    oldest_candidates = [created for created in (ledger_oldest_created, stat_oldest_created) if created is not None]
    oldest_created = min(oldest_candidates) if oldest_candidates else None
    oldest_age = None if oldest_created is None else max((reference - oldest_created).total_seconds(), 0.0)
    active_reservations, oldest_reservation_created = (
        db.session.query(func.count(KudosReservation.id), func.min(KudosReservation.created))
        .filter(KudosReservation.released_at.is_(None), KudosReservation.remaining_amount > 0)
        .one()
    )
    oldest_reservation_age = (
        None if oldest_reservation_created is None else max((reference - oldest_reservation_created).total_seconds(), 0.0)
    )
    return {
        "pending_rows": int(ledger_pending_count) + int(stat_pending_count),
        "oldest_pending_seconds": oldest_age,
        "heartbeat_seconds": kudos_applier_lag(reference),
        "active_reservations": int(active_reservations),
        "oldest_reservation_seconds": oldest_reservation_age,
    }
