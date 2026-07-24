# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Online snapshots and non-destructive kudos projection reconciliation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from loguru import logger
from sqlalchemy import func

from horde.classes.base.kudos import (
    KudosBalanceSnapshot,
    KudosLedger,
    emit_kudos_ledger_entry,
    kudos_event,
    kudos_event_id,
)
from horde.classes.base.user import User
from horde.database.kudos_db import acquire_reconciliation_lock, begin_repeatable_read
from horde.enums import KudosAuditDetail, KudosEntryType
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


def _applied_totals() -> dict[tuple[int, bool], Decimal]:
    rows = (
        db.session.query(KudosLedger.user_id, KudosLedger.escrow, func.sum(KudosLedger.amount))
        .filter(
            KudosLedger.applied.is_(True),
            KudosLedger.entry_type != KudosEntryType.RECONCILIATION,
        )
        .group_by(KudosLedger.user_id, KudosLedger.escrow)
        .all()
    )
    return {(user_id, escrow): Decimal(total) for user_id, escrow, total in rows}


def create_balance_snapshot(now: datetime | None = None) -> uuid.UUID:
    """Capture one transaction-consistent online replay baseline."""
    _begin_repeatable_read()
    snapshot_id = uuid.uuid4()
    created = now or datetime.utcnow()
    totals = _applied_totals()
    user_count = 0
    for user in db.session.query(User).order_by(User.id.asc()).yield_per(1000):
        db.session.add(
            KudosBalanceSnapshot(
                snapshot_id=snapshot_id,
                user_id=user.id,
                balance=user.kudos,
                escrow=user.evaluating_kudos,
                applied_balance_total=totals.get((user.id, False), Decimal("0")),
                applied_escrow_total=totals.get((user.id, True), Decimal("0")),
                created=created,
            ),
        )
        user_count += 1
    db.session.commit()
    logger.info(f"Kudos balance snapshot {snapshot_id} captured {user_count} users")
    return snapshot_id


def reconcile_balances(snapshot_id: uuid.UUID, *, apply_repairs: bool = False) -> list[KudosDrift]:
    """Compare projections with snapshot-plus-ledger totals and optionally compensate.

    Repairs are new audit-visible postings; this function never overwrites a
    balance, deletes history, or changes an old posting.
    """
    _begin_repeatable_read()
    if apply_repairs:
        _lock_reconciliation()
    totals = _applied_totals()
    drifts: list[KudosDrift] = []
    repaired = 0
    snapshots = db.session.query(KudosBalanceSnapshot).filter_by(snapshot_id=snapshot_id).order_by(KudosBalanceSnapshot.user_id.asc()).all()
    users = {user.id: user for user in db.session.query(User).filter(User.id.in_([row.user_id for row in snapshots])).all()}
    for snapshot in snapshots:
        user = users.get(snapshot.user_id)
        if user is None:
            continue
        expected_balance = snapshot.balance + totals.get((user.id, False), Decimal("0")) - snapshot.applied_balance_total
        expected_escrow = snapshot.escrow + totals.get((user.id, True), Decimal("0")) - snapshot.applied_escrow_total
        actual_balance = Decimal(user.kudos)
        actual_escrow = Decimal(user.evaluating_kudos)
        if expected_balance == actual_balance and expected_escrow == actual_escrow:
            continue
        drift = KudosDrift(user.id, expected_balance, actual_balance, expected_escrow, actual_escrow)
        drifts.append(drift)
        if apply_repairs:
            repair_key = f"reconcile:{snapshot_id}:{user.id}"
            event_id = kudos_event_id(repair_key)
            already_emitted = db.session.query(KudosLedger.id).filter(KudosLedger.event_id == event_id).first()
            if already_emitted is not None:
                continue
            repaired += 1
            with kudos_event(idempotency_key=repair_key):
                if expected_balance != actual_balance:
                    emit_kudos_ledger_entry(
                        KudosEntryType.RECONCILIATION,
                        expected_balance - actual_balance,
                        user_id=user.id,
                        detail={
                            KudosAuditDetail.REASON: "reconciliation",
                            KudosAuditDetail.SNAPSHOT_ID: str(snapshot_id),
                        },
                        force_projection=True,
                    )
                if expected_escrow != actual_escrow:
                    emit_kudos_ledger_entry(
                        KudosEntryType.RECONCILIATION,
                        expected_escrow - actual_escrow,
                        user_id=user.id,
                        escrow=True,
                        detail={
                            KudosAuditDetail.REASON: "reconciliation",
                            KudosAuditDetail.SNAPSHOT_ID: str(snapshot_id),
                        },
                        force_projection=True,
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
