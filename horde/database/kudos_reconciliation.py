# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Online snapshots and non-destructive kudos projection reconciliation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast

from loguru import logger
from sqlalchemy import CursorResult, Subquery, func, insert, literal, or_, select

from horde.classes.base.kudos import (
    KudosBalanceSnapshot,
    KudosIdempotencyKey,
    KudosLedger,
    KudosLedgerCheckpoint,
    KudosRetentionUnit,
    KudosSnapshotInvalidation,
    emit_kudos_ledger_entry,
    kudos_event,
    kudos_event_id,
)
from horde.classes.base.user import User
from horde.database import kudos_retention
from horde.database.kudos_db import acquire_reconciliation_lock, begin_repeatable_read
from horde.enums import KudosAuditDetail, KudosEntryType, KudosIdempotencyKind, KudosRetentionDataset, KudosRetentionState
from horde.flask import db


def _begin_repeatable_read() -> None:
    """Give the operational command ownership of a clean consistent snapshot."""
    begin_repeatable_read()


def _lock_reconciliation() -> None:
    """Serialize repair emitters while leaving ordinary writers online."""
    acquire_reconciliation_lock()


@dataclass(frozen=True)
class KudosDrift:
    """One materialized user balance that differs from its snapshot replay."""

    user_id: int
    expected_balance: Decimal
    actual_balance: Decimal
    expected_escrow: Decimal
    actual_escrow: Decimal


STALE_CHECKPOINT_REASONS: Final[frozenset[kudos_retention.RetentionBlockReason]] = frozenset(
    {kudos_retention.RetentionBlockReason.LATE_ROWS, kudos_retention.RetentionBlockReason.ONLINE_DRIFT},
)
"""The block reasons of a currency unit whose rows changed after its checkpoint, so that checkpoint and every later one
are stale until ``retention unblock --recheckpoint`` deletes and recomputes them."""


@dataclass(frozen=True, kw_only=True)
class StaleCheckpointUnit:
    """Represents a blocked currency unit whose checkpoint is known to be stale."""

    unit_id: int
    partition_name: str
    boundary: datetime
    reason: str


class StaleCheckpointsError(ValueError):
    """A checkpoint or saved snapshot baseline is unsafe for accounting."""


def stale_checkpoint_units() -> list[StaleCheckpointUnit]:
    """Return the blocked currency units whose checkpoints are stale, in boundary order.

    Reconciliation reads the latest checkpoint as the total of every posting below its boundary. A unit blocked with
    one of ``STALE_CHECKPOINT_REASONS`` holds rows that its checkpoint and every later checkpoint omit, so drift
    measured against them is not real.
    """
    rows = db.session.scalars(
        select(KudosRetentionUnit)
        .where(
            KudosRetentionUnit.dataset == KudosRetentionDataset.CURRENCY,
            KudosRetentionUnit.state == KudosRetentionState.BLOCKED,
            KudosRetentionUnit.error.in_([str(reason) for reason in STALE_CHECKPOINT_REASONS]),
        )
        .order_by(KudosRetentionUnit.unit_upper),
    ).all()
    return [
        StaleCheckpointUnit(unit_id=row.id, partition_name=row.partition_name, boundary=row.unit_upper, reason=str(row.error))
        for row in rows
    ]


def _stale_checkpoints_message(stale_units: list[StaleCheckpointUnit]) -> str:
    described = ", ".join(f"{stale_unit.unit_id} ({stale_unit.partition_name}, {stale_unit.reason})" for stale_unit in stale_units)
    return (
        f"Kudos ledger checkpoints are stale: currency retention units {described} are blocked. "
        f"Run retention unblock --recheckpoint {stale_units[0].unit_id} before taking snapshots or reconciling"
    )


def _applied_totals_subquery() -> Subquery:
    """Aggregate each user's all-time applied ledger movements, split by target balance.

    Retention removes postings older than the online window after summing them into
    ``kudos_ledger_checkpoints``, so the all-time total is the latest checkpoint plus the
    applied postings at or above its boundary. With no checkpoint the tail is every applied
    posting. The boundary is read once, so the tail predicate is a constant that prunes the
    scan to the partitions at or above it.

    ``escrow`` is a per-row boolean, so one grouped scan cannot emit the
    spendable and escrow sums as separate columns without conditional
    aggregation. The ``FILTER`` clauses partition each user's applied rows into
    the two totals the snapshot baseline stores independently. Reconciliation
    postings are excluded so replay measures the projection against its inputs
    rather than against prior repairs; checkpoints exclude them the same way.
    """
    boundary: datetime | None = db.session.execute(select(func.max(KudosLedgerCheckpoint.boundary))).scalar_one()
    tail_filters = [KudosLedger.applied.is_(True), KudosLedger.entry_type != KudosEntryType.RECONCILIATION]
    if boundary is not None:
        tail_filters.append(KudosLedger.created >= boundary)
    tail = (
        select(
            KudosLedger.user_id.label("user_id"),
            func.coalesce(
                func.sum(KudosLedger.amount).filter(KudosLedger.escrow.is_(False)),
                Decimal("0"),
            ).label("balance_total"),
            func.coalesce(
                func.sum(KudosLedger.amount).filter(KudosLedger.escrow.is_(True)),
                Decimal("0"),
            ).label("escrow_total"),
        )
        .where(*tail_filters)
        .group_by(KudosLedger.user_id)
        .subquery()
    )
    if boundary is None:
        return tail
    checkpoint = (
        select(KudosLedgerCheckpoint.user_id, KudosLedgerCheckpoint.balance_total, KudosLedgerCheckpoint.escrow_total)
        .where(KudosLedgerCheckpoint.boundary == boundary)
        .subquery()
    )
    # A user can appear only in the checkpoint (no recent postings) or only in the tail (first
    # posting after the boundary), so both sides are kept.
    return (
        select(
            func.coalesce(checkpoint.c.user_id, tail.c.user_id).label("user_id"),
            (func.coalesce(checkpoint.c.balance_total, Decimal("0")) + func.coalesce(tail.c.balance_total, Decimal("0"))).label(
                "balance_total",
            ),
            (func.coalesce(checkpoint.c.escrow_total, Decimal("0")) + func.coalesce(tail.c.escrow_total, Decimal("0"))).label(
                "escrow_total",
            ),
        )
        .select_from(checkpoint.outerjoin(tail, checkpoint.c.user_id == tail.c.user_id, full=True))
        .subquery()
    )


def _refuse_snapshot_older_than_key_lifetime(snapshot_id: uuid.UUID) -> None:
    """Raise when a repair from ``snapshot_id`` could outlive its idempotency key.

    Repair keys expire after the key lifetime. A repair from an older snapshot could find its key
    gone and post a second compensation for drift it already repaired.

    Raises:
        ValueError: The snapshot is older than the key lifetime, or does not exist.
    """
    key_lifetime: timedelta = kudos_retention.KUDOS_RETENTION_POLICY.key_lifetime
    snapshot_created: datetime | None = db.session.execute(
        select(func.min(KudosBalanceSnapshot.created)).where(KudosBalanceSnapshot.snapshot_id == snapshot_id),
    ).scalar_one()
    if snapshot_created is None:
        raise ValueError(f"Kudos balance snapshot {snapshot_id} does not exist")
    if snapshot_created < datetime.utcnow() - key_lifetime:
        raise ValueError(
            f"Kudos balance snapshot {snapshot_id} is older than the repair key lifetime of {key_lifetime.days} days; "
            "take a new snapshot to repair",
        )


@kudos_retention.operator_retention_lock()
def create_balance_snapshot(now: datetime | None = None) -> uuid.UUID:
    """Capture one transaction-consistent online replay baseline under the retention lock.

    Raises:
        StaleCheckpointsError: A checkpoint is known to be stale. Recover it before taking a baseline.
        kudos_retention.RetentionLockBusyError: A retention tick or recovery is running. Retry after it finishes.
    """
    _begin_repeatable_read()
    stale_units = stale_checkpoint_units()
    if stale_units:
        db.session.rollback()
        raise StaleCheckpointsError(_stale_checkpoints_message(stale_units))
    snapshot_id = uuid.uuid4()
    created = now or datetime.utcnow()
    applied_totals = _applied_totals_subquery()
    # LEFT JOIN so a user with no applied ledger rows still gets a baseline with
    # zero totals; the outer coalesce supplies that zero for the missing side.
    snapshot_rows = (
        select(
            literal(snapshot_id, KudosBalanceSnapshot.snapshot_id.type),
            User.id,
            User.kudos,
            User.evaluating_kudos,
            func.coalesce(applied_totals.c.balance_total, Decimal("0")),
            func.coalesce(applied_totals.c.escrow_total, Decimal("0")),
            literal(created, KudosBalanceSnapshot.created.type),
        )
        .select_from(User)
        .outerjoin(applied_totals, applied_totals.c.user_id == User.id)
    )
    insert_stmt = insert(KudosBalanceSnapshot).from_select(
        [
            "snapshot_id",
            "user_id",
            "balance",
            "escrow",
            "applied_balance_total",
            "applied_escrow_total",
            "created",
        ],
        snapshot_rows,
    )
    result = cast("CursorResult[Any]", db.session.execute(insert_stmt))
    user_count = result.rowcount
    db.session.commit()
    logger.info(f"Kudos balance snapshot {snapshot_id} captured {user_count} users")
    return snapshot_id


def reconcile_balances(snapshot_id: uuid.UUID, *, apply_repairs: bool = False) -> list[KudosDrift]:
    """Compare projections with snapshot-plus-ledger totals and optionally compensate.

    Repairs are new audit-visible postings; this function never overwrites a
    balance, deletes history, or changes an old posting.

    While ``stale_checkpoint_units`` reports a unit, the checkpoints omit rows, so repairs are refused and a read-only
    run logs a warning that its drift is not reliable.

    Raises:
        StaleCheckpointsError: The snapshot baseline was invalidated, or repairs were requested with stale checkpoints.
        ValueError: ``apply_repairs`` is set and the snapshot is older than the key lifetime or does not exist.
    """
    _begin_repeatable_read()
    invalid_baseline = db.session.execute(
        select(KudosBalanceSnapshot.id)
        .join(
            KudosSnapshotInvalidation,
            (KudosBalanceSnapshot.created >= KudosSnapshotInvalidation.invalid_from)
            & (KudosBalanceSnapshot.created <= KudosSnapshotInvalidation.invalid_until),
        )
        .where(KudosBalanceSnapshot.snapshot_id == snapshot_id)
        .limit(1),
    ).first()
    if invalid_baseline is not None:
        db.session.rollback()
        raise StaleCheckpointsError(
            f"Snapshot {snapshot_id} may contain rebuilt checkpoint totals and cannot be used for reconciliation. "
            "Its rows are kept as evidence. Take a new snapshot after the checkpoint recovery",
        )
    stale_units = stale_checkpoint_units()
    if stale_units and apply_repairs:
        db.session.rollback()
        raise StaleCheckpointsError(_stale_checkpoints_message(stale_units))
    if stale_units:
        logger.warning(f"{_stale_checkpoints_message(stale_units)}. This read-only reconciliation reports drift that may not exist")
    if apply_repairs:
        try:
            _refuse_snapshot_older_than_key_lifetime(snapshot_id)
        except ValueError:
            db.session.rollback()
            raise
        _lock_reconciliation()
    applied_totals = _applied_totals_subquery()
    # expected = snapshot balance + movements applied since the snapshot; the
    # snapshot's own applied total is subtracted so already-folded movements are
    # not double counted. The outer coalesce zeroes users the LEFT JOIN misses.
    expected_balance = (
        KudosBalanceSnapshot.balance
        + func.coalesce(applied_totals.c.balance_total, Decimal("0"))
        - KudosBalanceSnapshot.applied_balance_total
    )
    expected_escrow = (
        KudosBalanceSnapshot.escrow + func.coalesce(applied_totals.c.escrow_total, Decimal("0")) - KudosBalanceSnapshot.applied_escrow_total
    )
    drift_rows = db.session.execute(
        select(
            KudosBalanceSnapshot.user_id.label("user_id"),
            expected_balance.label("expected_balance"),
            User.kudos.label("actual_balance"),
            expected_escrow.label("expected_escrow"),
            User.evaluating_kudos.label("actual_escrow"),
        )
        .select_from(KudosBalanceSnapshot)
        .join(User, User.id == KudosBalanceSnapshot.user_id)
        .outerjoin(applied_totals, applied_totals.c.user_id == KudosBalanceSnapshot.user_id)
        .where(KudosBalanceSnapshot.snapshot_id == snapshot_id)
        .where(or_(expected_balance != User.kudos, expected_escrow != User.evaluating_kudos))
        .order_by(KudosBalanceSnapshot.user_id.asc()),
    ).all()
    drifts: list[KudosDrift] = [
        KudosDrift(
            row.user_id,
            row.expected_balance,
            Decimal(row.actual_balance),
            row.expected_escrow,
            Decimal(row.actual_escrow),
        )
        for row in drift_rows
    ]
    repaired = 0
    if apply_repairs:
        for drift in drifts:
            repair_key = f"reconcile:{snapshot_id}:{drift.user_id}"
            event_id = kudos_event_id(repair_key)
            if db.session.get(KudosIdempotencyKey, event_id) is not None:
                continue
            repaired += 1
            with kudos_event(idempotency_key=repair_key):
                if drift.expected_balance != drift.actual_balance:
                    emit_kudos_ledger_entry(
                        KudosEntryType.RECONCILIATION,
                        drift.expected_balance - drift.actual_balance,
                        user_id=drift.user_id,
                        detail={
                            KudosAuditDetail.REASON: "reconciliation",
                            KudosAuditDetail.SNAPSHOT_ID: str(snapshot_id),
                        },
                        force_projection=True,
                    )
                if drift.expected_escrow != drift.actual_escrow:
                    emit_kudos_ledger_entry(
                        KudosEntryType.RECONCILIATION,
                        drift.expected_escrow - drift.actual_escrow,
                        user_id=drift.user_id,
                        escrow=True,
                        detail={
                            KudosAuditDetail.REASON: "reconciliation",
                            KudosAuditDetail.SNAPSHOT_ID: str(snapshot_id),
                        },
                        force_projection=True,
                    )
            # Committed with the postings below, so a second run finds the key and skips this user.
            db.session.add(
                KudosIdempotencyKey(
                    event_id=event_id,
                    kind=KudosIdempotencyKind.REPAIR,
                    source_user_id=None,
                    dest_user_id=drift.user_id,
                    amount=(drift.expected_balance - drift.actual_balance) + (drift.expected_escrow - drift.actual_escrow),
                    outcome="OK",
                ),
            )
    total_absolute_drift = sum(
        (abs(drift.expected_balance - drift.actual_balance) + abs(drift.expected_escrow - drift.actual_escrow) for drift in drifts),
        Decimal("0"),
    )
    if apply_repairs:
        db.session.commit()
        logger.info(f"Kudos reconciliation repaired {repaired} users; total absolute drift {total_absolute_drift}")
    else:
        db.session.rollback()
        if drifts:
            logger.warning(
                f"Kudos read-only reconciliation detected drift for {len(drifts)} users; total absolute drift {total_absolute_drift}",
            )
    return drifts
