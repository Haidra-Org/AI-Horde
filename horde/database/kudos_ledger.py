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

import os
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, TypedDict, cast

from loguru import logger
from sqlalchemy import DateTime, Integer, Numeric, case, column, func, update, values
from sqlalchemy import cast as sql_cast

from horde.classes.base.kudos import (
    KUDOS_STAT_ACTION_MAX_LENGTH,
    KUDOS_STAT_RECORD_MAX_LENGTH,
    KudosLedger,
    KudosLedgerApplierState,
    KudosReservation,
    KudosStatEvent,
    emit_kudos_ledger_entry,
    emit_kudos_stat_event,
    get_kudos_ledger_mode,
    get_kudos_trust_threshold,
    kudos_event,
)
from horde.classes.base.team import Team
from horde.classes.base.user import User, UserRecords, UserRole, UserStats, UserSuspicions
from horde.classes.base.worker import WorkerStats, WorkerTemplate
from horde.database.kudos_counters import increment_counters
from horde.database.kudos_db import try_acquire_applier_lock
from horde.database.kudos_reservations import consume_reservation, release_event_reservations
from horde.enums import (
    KudosAggregate,
    KudosAuditDetail,
    KudosEntryType,
    KudosLedgerMode,
    KudosStatEventQuarantineReason,
    KudosStatRecord,
    KudosUnit,
    UserRecordTypes,
    UserRoleTypes,
)
from horde.flask import db
from horde.metrics import (
    kudos_applier_folded,
    kudos_applier_phase_duration,
    kudos_applier_quarantined,
    kudos_applier_quarantined_by_reason,
    kudos_floor_adjustments,
    kudos_floor_adjustments_created,
)

type _MaterializedKudosAmount = int | Decimal

# Cap how many rows from each queue one cycle folds so that catching up after
# applier downtime cannot load an unbounded tail into memory. Rows folded this
# cycle are marked applied, so the next cycle continues with whatever remains
# unapplied. Production can raise this after measuring its database; keeping the
# default conservative preserves smaller deployments' existing lock/memory
# footprint.
KUDOS_APPLIER_BATCH_SIZE = max(int(os.getenv("KUDOS_APPLIER_BATCH_SIZE", "1000")), 1)
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


_USER_RECORD_UNITS: Final[Mapping[str, KudosUnit]] = {
    UserRecordTypes.CONTRIBUTION.name: KudosUnit.THINGS,
    UserRecordTypes.USAGE.name: KudosUnit.THINGS,
    UserRecordTypes.FULFILLMENT.name: KudosUnit.COUNT,
    UserRecordTypes.REQUEST.name: KudosUnit.COUNT,
    UserRecordTypes.STYLE.name: KudosUnit.COUNT,
}


class KudosApplierHealth(TypedDict):
    """Represents projector health fields returned to probes and operators."""

    pending_rows: int
    oldest_pending_seconds: float | None
    ledger_pending_rows: int
    stat_pending_rows: int
    oldest_ledger_pending_seconds: float | None
    oldest_stat_pending_seconds: float | None
    quarantined_rows: int
    oldest_quarantined_seconds: float | None
    newest_quarantined_seconds: float | None
    heartbeat_seconds: float | None
    active_reservations: int
    oldest_reservation_seconds: float | None


def _stat_event_quarantine_reason(
    row: KudosStatEvent,
    existing_user_ids: set[int],
) -> KudosStatEventQuarantineReason | None:
    """Return a bounded reason code when a stat event cannot be projected safely.

    Validation happens before any materialized counter write. Deterministic bad
    data is retained as immutable audit history but removed from the hot queue;
    infrastructure failures still raise and roll the whole cycle back.
    """
    action = row.stat_action
    record = row.record
    entry_type = row.entry_type
    unit = row.unit

    if row.user_id is not None and row.user_id not in existing_user_ids:
        return KudosStatEventQuarantineReason.MISSING_USER
    if action is not None and len(action) > KUDOS_STAT_ACTION_MAX_LENGTH:
        return KudosStatEventQuarantineReason.STAT_ACTION_TOO_LONG
    if record is not None and len(record) > KUDOS_STAT_RECORD_MAX_LENGTH:
        return KudosStatEventQuarantineReason.RECORD_TOO_LONG

    if record == KudosStatRecord.USER_KUDOS.value:
        if row.user_id is None or action is None or unit != KudosUnit.KUDOS:
            return KudosStatEventQuarantineReason.INVALID_USER_KUDOS
        return None
    if record == KudosStatRecord.WORKER_KUDOS.value:
        if row.worker_id is None or action is None or unit != KudosUnit.KUDOS:
            return KudosStatEventQuarantineReason.INVALID_WORKER_KUDOS
        return None
    if entry_type == KudosEntryType.STAT_RECORD:
        if row.user_id is None or action is None or record is None:
            return KudosStatEventQuarantineReason.INVALID_STAT_RECORD
        if action not in _USER_RECORD_UNITS:
            return KudosStatEventQuarantineReason.INVALID_STAT_RECORD
        if unit != _USER_RECORD_UNITS[action]:
            return KudosStatEventQuarantineReason.INVALID_STAT_RECORD_UNIT
        return None
    if entry_type == KudosEntryType.STAT_CONTRIBUTION:
        if row.worker_id is None or action is None:
            return KudosStatEventQuarantineReason.INVALID_STAT_CONTRIBUTION
        expected_unit: KudosUnit | None = {
            KudosAggregate.CONTRIBUTIONS.value: KudosUnit.THINGS,
            KudosAggregate.FULFILMENTS.value: KudosUnit.COUNT,
        }.get(action)
        if expected_unit is None or unit != expected_unit:
            return KudosStatEventQuarantineReason.INVALID_STAT_CONTRIBUTION
        return None
    if entry_type == KudosEntryType.STAT_ACTIVITY:
        if (
            row.user_id is None
            or record != KudosStatRecord.LAST_ACTIVE.value
            or unit != KudosUnit.COUNT
            or not row.detail
            or not row.detail.get(KudosAuditDetail.TOUCH_LAST_ACTIVE)
        ):
            return KudosStatEventQuarantineReason.INVALID_STAT_ACTIVITY
        return None
    return KudosStatEventQuarantineReason.UNKNOWN_PROJECTION


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
        batch_size: Maximum rows settled in this cycle; the next cycle continues
            with whatever remains drainable and unapplied.

    Returns:
        The number of rows folded or quarantined this cycle plus any
        promotion-drain postings emitted (see :func:`_drain_trusted_escrow`). A
        return of 0 means no drainable unapplied rows remain and no trusted
        escrow needs draining, so a caller folding to quiescence can stop.
    """
    if now is None:
        now = datetime.utcnow()
    if not lock_already_held and not _acquire_applier_lock():
        # Do not leave an idle transaction open merely because another replica
        # owns the projector.  The owner will process the queue.
        db.session.rollback()
        return 0
    state = get_applier_state()
    phase_t = time.monotonic()
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
        .filter(
            KudosStatEvent.applied.is_(False),
            KudosStatEvent.quarantined.is_(False),
        )
        .order_by(KudosStatEvent.id.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .all()
    )
    claimed_stat_count = len(stat_rows)
    kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "claim"})

    user_target_ids = {row.user_id for row in stat_rows if row.user_id is not None}
    existing_user_ids = {
        user_id
        for (user_id,) in (db.session.query(User.id).filter(User.id.in_(user_target_ids)).with_for_update(read=True, key_share=True).all())
    }
    valid_stat_rows: list[KudosStatEvent] = []
    quarantined_rows: list[KudosStatEvent] = []
    quarantine_counts: dict[KudosStatEventQuarantineReason, int] = defaultdict(int)
    invalid_event_reasons: dict[uuid.UUID, KudosStatEventQuarantineReason] = {}
    for row in stat_rows:
        reason = _stat_event_quarantine_reason(row, existing_user_ids)
        if reason is not None:
            invalid_event_reasons.setdefault(row.event_id, reason)
    for row in stat_rows:
        reason = invalid_event_reasons.get(row.event_id)
        if reason is None:
            valid_stat_rows.append(row)
            continue
        # Quarantine the claimed portion of one business event together. A bad
        # dimension must not leave its valid peer counters partially projected
        # merely because they shared a batch with unrelated healthy events.
        row.quarantined = True
        row.quarantine_reason = reason
        row.quarantined_at = datetime.utcnow()
        quarantined_rows.append(row)
        quarantine_counts[reason] += 1
    stat_rows = valid_stat_rows

    # Worker and team folds are executed at the end of the cycle (see below), so
    # their delta maps live at cycle scope.
    worker_deltas: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    worker_contribution_deltas: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    worker_fulfilment_deltas: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    # Team aggregates are derived from the worker's own postings stamped with a
    # team_id: kudos from the balance-credit posting, contributions/fulfilments
    # from the worker STAT_CONTRIBUTION postings. team_id is read independently
    # of the balance target, so a stamped worker posting feeds both.
    team_kudos_deltas: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    team_contribution_deltas: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    team_fulfilment_deltas: dict[uuid.UUID, Decimal] = defaultdict(Decimal)

    if rows or stat_rows:
        user_balance_deltas: dict[int, Decimal] = defaultdict(Decimal)
        user_escrow_deltas: dict[int, Decimal] = defaultdict(Decimal)
        user_last_active: dict[int, datetime] = {}
        # Counter folds ride the same claimed batch and the same transaction as the
        # balance fold, so one cycle materializes balances and every derived counter
        # atomically. Each counter reconstructs its row by grouping the batch on the
        # dimension the posting carries.
        user_stats_deltas: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
        worker_stats_deltas: dict[tuple[uuid.UUID, str], Decimal] = defaultdict(Decimal)
        user_record_deltas: dict[tuple[int, str, str], Decimal] = defaultdict(Decimal)
        reservation_consumptions: dict[str, Decimal] = defaultdict(Decimal)
        folded_ids = [row.id for row in rows]
        folded_stat_ids = [row.id for row in stat_rows]
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

        # The workers and teams row folds do not happen here: worker check_in
        # updates the same rows on the pop hot path, and a row lock taken this
        # early would be held across everything below plus the settling scans,
        # queueing pops behind the fold transaction. They run at the end of the
        # cycle instead.
        phase_t = time.monotonic()
        _apply_user_deltas(user_balance_deltas, user_escrow_deltas, user_last_active)
        kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "user_fold"})
        phase_t = time.monotonic()
        _apply_user_stats_deltas(user_stats_deltas)
        _apply_worker_stats_deltas(worker_stats_deltas)
        _apply_user_record_deltas(user_record_deltas)
        kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "counter_fold"})
        phase_t = time.monotonic()
        if folded_ids:
            _mark_applied(folded_ids)
        if folded_stat_ids:
            _mark_stat_events_applied(folded_stat_ids)
        kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "mark_applied"})
        phase_t = time.monotonic()
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
        kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "reservations"})
        # The balance folds above write through single bulk statements that
        # bypass the identity map, and the session keeps attributes across
        # commits (expire_on_commit=False), so any instance the session holds
        # still shows pre-fold values. Expire everything once per cycle, after
        # the last read of the claimed row instances, so subsequent reads
        # observe the folded values. The flush first persists pending instance
        # state (the floor corrections' applied flag), which expiry would
        # otherwise discard.
        phase_t = time.monotonic()
        db.session.flush()
        db.session.expire_all()
        kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "flush_expire"})

    # A trusted user's escrow always drains to their spendable balance; the
    # applier owns that movement so promotion timing cannot strand an escrow
    # posting. The emitted pairs are folded by a subsequent cycle. Promotion and
    # the drain mutate balances, which in shadow mode belong to the inline legacy
    # projection (project_trust_promotion); only the ledger-owned projector may run
    # them, so both are gated on the mode pinned in this applier transaction. The
    # heartbeat is stamped every cycle regardless (even folding nothing) so the lag
    # metric tracks applier staleness rather than a quiet period.
    #
    # The scans run only on a settling cycle (one whose claims came back short,
    # meaning the queue drained this cycle). They examine population state, not
    # the claimed batch, so repeating them on every full-batch cycle of a
    # catch-up burst does identical work while multiplying the burst's duration
    # by their cost. At steady state every cycle settles, so scan cadence there
    # is unchanged; under sustained saturation the scans wait for the backlog to
    # clear, which only defers promotion/drain, never loses it.
    # A zero-size claim folds nothing by construction and settles trivially,
    # which keeps scan-only invocations working.
    claims_settled = batch_size == 0 or (len(rows) < batch_size and claimed_stat_count < batch_size)
    drained = 0
    if claims_settled and get_kudos_ledger_mode() == KudosLedgerMode.LEDGER:
        phase_t = time.monotonic()
        _promote_eligible_users(now)
        drained = _drain_trusted_escrow()
        kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "settle_scans"})
    # Folding workers and teams last minimizes the wall time this transaction
    # holds their row locks: worker check_in updates the same rows on the pop
    # hot path and queues behind the fold until it commits. The delta folds are
    # relative DB-side increments and nothing between the delta computation and
    # this point reads the folded aggregates, so the deferral does not change
    # what this cycle observes. Empty maps make each call a no-op.
    phase_t = time.monotonic()
    _apply_worker_deltas(worker_deltas)
    _apply_worker_contribution_deltas(worker_contribution_deltas, worker_fulfilment_deltas)
    _apply_team_deltas(team_contribution_deltas, team_fulfilment_deltas, team_kudos_deltas)
    kudos_applier_phase_duration.record(time.monotonic() - phase_t, {"horde.kudos.phase": "worker_team_fold"})
    state.applied_at = now
    if commit:
        db.session.commit()
        # Success counters must follow the commit. Recording them before the
        # projection transaction commits makes a poison row look like useful
        # throughput every time the same batch rolls back and retries.
        if rows:
            kudos_applier_folded.add(len(rows), {"horde.kudos.row_type": "currency"})
        if stat_rows:
            kudos_applier_folded.add(len(stat_rows), {"horde.kudos.row_type": "stat"})
        if quarantined_rows:
            kudos_applier_quarantined.add(len(quarantined_rows))
        for reason, count in sorted(quarantine_counts.items()):
            kudos_applier_quarantined_by_reason.add(count, {"horde.kudos.reason": reason})
        if quarantined_rows:
            logger.error(
                "Kudos applier quarantined {} invalid stat events: {}",
                len(quarantined_rows),
                ", ".join(f"{reason}={count}" for reason, count in sorted(quarantine_counts.items())),
            )
    else:
        db.session.flush()
    # Quarantine is durable queue progress too. Including it keeps catch-up,
    # explicit drain, and ledger->shadow transition loops moving when a claimed
    # batch consists entirely of poison events.
    return len(rows) + len(stat_rows) + len(quarantined_rows) + drained


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
            # The inline promotion path routed the released escrow through
            # modify_kudos(amount, "accumulated"), so the user's per-action
            # "accumulated" statistic includes promoted escrow. Emit the same
            # statistic movement here so a ledger-mode promotion keeps that
            # meaning; the currency pair alone would leave user_stats
            # understating the balance's provenance by the drained amount.
            emit_kudos_stat_event(
                KudosEntryType.EVALUATION_PROMOTION,
                amount,
                user_id=user_id,
                unit=KudosUnit.KUDOS,
                stat_action="accumulated",
                record=KudosStatRecord.USER_KUDOS,
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
    # Ineligibility is filtered in SQL rather than per instance. An anon or
    # suspicious account above the threshold never promotes, so a Python-side
    # skip would refetch it on every scan forever, and the instance-level
    # ``is_suspicious`` check lazy-loads each candidate's suspicions
    # individually. The count comparison reproduces ``User.is_suspicious`` for
    # a non-trusted account (the candidate filter already excludes trusted).
    # The comparison only distinguishes "fewer than SUSPICION_THRESHOLD" from
    # "at least that many", so the inner scan stops at the threshold. Counting a
    # heavily suspicious account's full history instead inflates the subquery's
    # estimated cost enough to push the whole candidate scan over the JIT
    # threshold and to turn the trusted-role anti-join into a materialized nested
    # loop over every trusted role.
    bounded_suspicions = (
        db.session.query(UserSuspicions.id)
        .filter(UserSuspicions.user_id == User.id)
        .correlate(User)
        .limit(User.SUSPICION_THRESHOLD)
        .subquery()
    )
    suspicion_count = db.session.query(func.count()).select_from(bounded_suspicions).scalar_subquery()
    candidates = (
        db.session.query(User)
        .filter(
            ~trusted_role_exists,
            User.evaluating_kudos > threshold,
            User.created <= now - timedelta(days=7),
            User.oauth_id != "anon",
            suspicion_count < User.SUSPICION_THRESHOLD,
        )
        .order_by(User.id.asc())
        .all()
    )
    promoted_user_ids: list[int] = []
    for user in candidates:
        role = db.session.query(UserRole).filter_by(user_id=user.id, user_role=UserRoleTypes.TRUSTED).first()
        if role is None:
            role = UserRole(user_id=user.id, user_role=UserRoleTypes.TRUSTED, value=True)
            db.session.add(role)
        else:
            role.value = True
        for worker in cast(list[WorkerTemplate], user.workers):
            worker.paused = False
        # Per-user detail stays at debug: a promotion is durably auditable via
        # the EVALUATION_PROMOTION ledger pair the drain emits, and a promotion
        # wave (hundreds of users in one tick at cutover) must not stretch the
        # fold tick with per-row log writes.
        logger.debug(f"Kudos applier promoted user {user.id} to trusted")
        promoted_user_ids.append(user.id)
    if promoted_user_ids:
        logger.info(f"Kudos applier promoted {len(promoted_user_ids)} users to trusted this tick")
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
    floor_adjustment_count = 0
    floor_adjustment_total = Decimal("0")
    # The applier's advisory lock makes it the only writer of these columns, so
    # absolute values computed from the rows read in this same transaction
    # cannot lose a concurrent update. All of them are then written back with a
    # single statement: the fold transaction holds every touched row's lock
    # until commit, so the write count, and with it the lock-hold window other
    # writers queue behind, must not scale with the batch's account count.
    update_rows: list[tuple[int, _MaterializedKudosAmount, _MaterializedKudosAmount, datetime | None]] = []
    for user in users:
        new_balance: _MaterializedKudosAmount = user.kudos
        if user.id in balance_deltas:
            requested_balance = round(user.kudos + balance_deltas[user.id], 2)
            floor = user.get_min_kudos()
            new_balance = floor if requested_balance < floor else requested_balance
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
                # The FLOOR_ADJUSTMENT posting is the durable per-user audit
                # record. Avoid a redundant log line here: a catch-up batch can
                # floor hundreds of users, and formatting/writing one line per
                # row materially stretches the projection transaction. Aggregate
                # logging and metrics are emitted after the fold below.
                floor_adjustment_count += 1
                floor_adjustment_total += created
                # Anon rides its floor continuously by design (unlimited
                # anonymous consumption against a fixed overdraft), so the
                # account class is exported to let alerting watch registered
                # accounts without that structural baseline drowning them out.
                account_class = "anon" if user.is_anon() else ("pseudonymous" if user.is_pseudonymous() else "registered")
                kudos_floor_adjustments.add(1, {"horde.account_class": account_class})
                kudos_floor_adjustments_created.add(float(created), {"horde.account_class": account_class})
        new_escrow: _MaterializedKudosAmount = user.evaluating_kudos
        if user.id in escrow_deltas:
            new_escrow = round(user.evaluating_kudos + escrow_deltas[user.id], 2)
        new_last_active = user.last_active
        if user.id in activity and (user.last_active is None or activity[user.id] > user.last_active):
            new_last_active = activity[user.id]
        update_rows.append((user.id, new_balance, new_escrow, new_last_active))
    written = values(
        column("id", Integer),
        column("kudos", Numeric),
        column("evaluating_kudos", Numeric),
        column("last_active", DateTime),
        name="user_balance_updates",
    ).data(update_rows)
    users_table = User.__table__
    db.session.execute(
        update(users_table)
        .where(users_table.c.id == written.c.id)
        .values(
            kudos=written.c.kudos,
            evaluating_kudos=written.c.evaluating_kudos,
            last_active=written.c.last_active,
        ),
    )
    if floor_adjustment_count:
        logger.info(
            f"Kudos floor adjustments created {floor_adjustment_total} kudos across {floor_adjustment_count} users this batch",
        )


def _apply_worker_deltas(worker_deltas: dict[uuid.UUID, Decimal]) -> None:
    if not worker_deltas:
        return
    # A pure relative increment needs no read at all: one statement adjusts
    # every touched worker row. Worker rows are concurrently written by
    # check-in and performance writers, so keeping the fold's lock-hold window
    # to a single statement matters more here than anywhere else.
    workers_table = WorkerTemplate.__table__
    deltas = values(
        column("id", workers_table.c.id.type),
        column("kudos_delta", Numeric),
        name="worker_kudos_deltas",
    ).data(sorted(worker_deltas.items(), key=lambda item: str(item[0])))
    db.session.execute(
        update(workers_table)
        .where(workers_table.c.id == deltas.c.id)
        .values(kudos=func.round(sql_cast(workers_table.c.kudos + deltas.c.kudos_delta, Numeric), 2)),
    )


def _apply_user_stats_deltas(deltas: dict[tuple[int, str], Decimal]) -> None:
    # One upsert statement folds every dimension: a round-then-sum increment on
    # the existing row, or a rounded insert when none exists, matching the
    # historical request-path semantics. Single-writer applier ownership removes
    # the first-insert race the request path had to guard against.
    increment_counters(
        UserStats,
        [({"user_id": user_id, "action": action}, delta) for (user_id, action), delta in sorted(deltas.items())],
    )


def _apply_worker_stats_deltas(deltas: dict[tuple[uuid.UUID, str], Decimal]) -> None:
    if not deltas:
        return
    worker_ids = {worker_id for worker_id, _action in deltas}
    existing_worker_ids = {
        worker_id
        for (worker_id,) in (
            db.session.query(WorkerTemplate.id)
            .filter(WorkerTemplate.id.in_(worker_ids))
            # Prevent a worker deletion between existence validation and the
            # worker_stats FK insert. FOR KEY SHARE permits ordinary check-in
            # updates but makes deletion wait for this projection transaction.
            .with_for_update(read=True, key_share=True)
            .all()
        )
    }
    missing_worker_ids = worker_ids - existing_worker_ids
    if missing_worker_ids:
        # KudosStatEvent deliberately has no worker FK because it is immutable
        # audit history and workers are hard-deleted.  Historical events may
        # therefore reach the projector after their display target disappeared.
        # Their owner currency/user counters still fold normally; only the
        # deleted worker's worker_stats row has nowhere to go.
        logger.warning(
            "Skipping worker_stats projection for deleted workers: {}",
            ", ".join(sorted(map(str, missing_worker_ids))),
        )
    increment_counters(
        WorkerStats,
        [
            ({"worker_id": worker_id, "action": action}, delta)
            for (worker_id, action), delta in sorted(deltas.items(), key=lambda item: (str(item[0][0]), item[0][1]))
            if worker_id in existing_worker_ids
        ],
    )


def _apply_user_record_deltas(deltas: dict[tuple[int, str, str], Decimal]) -> None:
    increment_counters(
        UserRecords,
        [
            ({"user_id": user_id, "record_type": UserRecordTypes[record_type_name], "record": record}, delta)
            for (user_id, record_type_name, record), delta in sorted(deltas.items())
        ],
    )


def _apply_worker_contribution_deltas(
    contribution_deltas: dict[uuid.UUID, Decimal],
    fulfilment_deltas: dict[uuid.UUID, Decimal],
) -> None:
    worker_ids = set(contribution_deltas) | set(fulfilment_deltas)
    if not worker_ids:
        return
    workers_table = WorkerTemplate.__table__
    deltas = values(
        column("id", workers_table.c.id.type),
        column("contributions_delta", Numeric),
        column("fulfilments_delta", Integer),
        name="worker_aggregate_deltas",
    ).data(
        [
            (
                worker_id,
                contribution_deltas.get(worker_id, Decimal("0")),
                int(fulfilment_deltas.get(worker_id, Decimal("0"))),
            )
            for worker_id in sorted(worker_ids, key=str)
        ],
    )
    db.session.execute(
        update(workers_table)
        .where(workers_table.c.id == deltas.c.id)
        .values(
            # A zero delta leaves the stored value bit-for-bit untouched, as the
            # per-column skip it replaces did, instead of re-quantizing it.
            contributions=case(
                (deltas.c.contributions_delta == 0, workers_table.c.contributions),
                else_=func.round(sql_cast(workers_table.c.contributions + deltas.c.contributions_delta, Numeric), 2),
            ),
            fulfilments=workers_table.c.fulfilments + deltas.c.fulfilments_delta,
        ),
    )


def _apply_team_deltas(
    contribution_deltas: dict[uuid.UUID, Decimal],
    fulfilment_deltas: dict[uuid.UUID, Decimal],
    kudos_deltas: dict[uuid.UUID, Decimal],
) -> None:
    team_ids = set(contribution_deltas) | set(fulfilment_deltas) | set(kudos_deltas)
    if not team_ids:
        return
    teams_table = Team.__table__
    deltas = values(
        column("id", teams_table.c.id.type),
        column("contributions_delta", Numeric),
        column("fulfilments_delta", Integer),
        column("kudos_delta", Numeric),
        name="team_aggregate_deltas",
    ).data(
        [
            (
                team_id,
                contribution_deltas.get(team_id, Decimal("0")),
                int(fulfilment_deltas.get(team_id, Decimal("0"))),
                kudos_deltas.get(team_id, Decimal("0")),
            )
            for team_id in sorted(team_ids, key=str)
        ],
    )
    db.session.execute(
        update(teams_table)
        .where(teams_table.c.id == deltas.c.id)
        .values(
            # A zero delta leaves the stored value bit-for-bit untouched, as the
            # per-column skip it replaces did, instead of re-quantizing it.
            contributions=case(
                (deltas.c.contributions_delta == 0, teams_table.c.contributions),
                else_=func.round(sql_cast(teams_table.c.contributions + deltas.c.contributions_delta, Numeric), 2),
            ),
            fulfilments=teams_table.c.fulfilments + deltas.c.fulfilments_delta,
            kudos=case(
                (deltas.c.kudos_delta == 0, teams_table.c.kudos),
                else_=func.round(sql_cast(teams_table.c.kudos + deltas.c.kudos_delta, Numeric), 2),
            ),
        ),
    )


def prune_applied_kudos_ledger(
    now: datetime | None = None,
    retention: timedelta = KUDOS_LEDGER_RETENTION,
    batch_size: int = KUDOS_PRUNE_BATCH_SIZE,
) -> int:
    """Retain the permanent ledger archive (compatibility no-op).

    The function and its parameters exist so a scheduled caller of the former
    pruning job stays harmless; the archive itself is never pruned.
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


def kudos_applier_health(now: datetime | None = None) -> KudosApplierHealth:
    """Return heartbeat and real queue-lag health for probes and operators."""
    reference = now or datetime.utcnow()
    ledger_pending_count, ledger_oldest_created = (
        db.session.query(func.count(KudosLedger.id), func.min(KudosLedger.created)).filter(KudosLedger.applied.is_(False)).one()
    )
    stat_pending_count, stat_oldest_created = (
        db.session.query(func.count(KudosStatEvent.id), func.min(KudosStatEvent.created))
        .filter(KudosStatEvent.applied.is_(False), KudosStatEvent.quarantined.is_(False))
        .one()
    )
    quarantined_count, quarantined_oldest_created, quarantined_newest_at = (
        db.session.query(
            func.count(KudosStatEvent.id),
            func.min(KudosStatEvent.created),
            func.max(KudosStatEvent.quarantined_at),
        )
        .filter(KudosStatEvent.quarantined.is_(True))
        .one()
    )
    ledger_oldest_age = None if ledger_oldest_created is None else max((reference - ledger_oldest_created).total_seconds(), 0.0)
    stat_oldest_age = None if stat_oldest_created is None else max((reference - stat_oldest_created).total_seconds(), 0.0)
    oldest_candidates = [created for created in (ledger_oldest_created, stat_oldest_created) if created is not None]
    oldest_created = min(oldest_candidates) if oldest_candidates else None
    oldest_age = None if oldest_created is None else max((reference - oldest_created).total_seconds(), 0.0)
    oldest_quarantined_age = (
        None if quarantined_oldest_created is None else max((reference - quarantined_oldest_created).total_seconds(), 0.0)
    )
    newest_quarantined_age = None if quarantined_newest_at is None else max((reference - quarantined_newest_at).total_seconds(), 0.0)
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
        "ledger_pending_rows": int(ledger_pending_count),
        "stat_pending_rows": int(stat_pending_count),
        "oldest_ledger_pending_seconds": ledger_oldest_age,
        "oldest_stat_pending_seconds": stat_oldest_age,
        "quarantined_rows": int(quarantined_count),
        "oldest_quarantined_seconds": oldest_quarantined_age,
        "newest_quarantined_seconds": newest_quarantined_age,
        "heartbeat_seconds": kudos_applier_lag(reference),
        "active_reservations": int(active_reservations),
        "oldest_reservation_seconds": oldest_reservation_age,
    }
