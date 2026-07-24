# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compatibility projection used only during the kudos shadow period.

All feature-mode branching for legacy inline writes lives here. The business
methods always emit the new ledger/stat events and call one narrowly named
compatibility helper. Removing shadow mode therefore means deleting this module
and its call sites, without untangling conditionals from accounting logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from horde.classes.base.kudos import KudosAmount, kudos_projection_is_async
from horde.enums import KudosEntryType, UserRecordTypes


class LegacyUser(Protocol):
    """User attributes required by the temporary compatibility projector."""

    id: int
    kudos: float
    evaluating_kudos: float
    last_active: datetime

    def get_min_kudos(self) -> int:
        """Return the account's allowed minimum balance."""
        ...

    def modify_evaluating_kudos(self, kudos: KudosAmount, entry_type: KudosEntryType, commit: bool = False) -> None:
        """Emit and project an evaluation-escrow movement."""
        ...

    def modify_kudos(
        self,
        kudos: KudosAmount,
        action: str = "accumulated",
        commit: bool = True,
        entry_type: KudosEntryType = ...,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Emit and project a spendable-balance movement."""
        ...


class LegacyTeam(Protocol):
    """Team attributes mutated by the temporary compatibility projector."""

    contributions: float
    fulfilments: int
    kudos: float


class LegacyWorker(Protocol):
    """Worker attributes required by the temporary compatibility projector."""

    id: uuid.UUID
    user_id: int
    wtype: str
    kudos: float
    contributions: float
    fulfilments: int
    team: LegacyTeam


def project_user_record(
    user: LegacyUser,
    *,
    record_type: UserRecordTypes,
    record: str,
    increment: float,
    touch_activity: bool,
) -> None:
    """Apply the pre-ledger user-record mutation while shadowing."""
    if kudos_projection_is_async():
        return
    from horde.classes.base.user import UserRecords
    from horde.database.kudos_counters import increment_counter

    increment_counter(
        UserRecords,
        {"user_id": user.id, "record_type": record_type, "record": record},
        increment,
    )
    if touch_activity:
        user.last_active = datetime.utcnow()


def consume_user_reservation(business_id: str, amount: KudosAmount) -> None:
    """Consume a debit hold inline only while legacy projection owns writes."""
    if kudos_projection_is_async():
        return
    from horde.database.kudos_reservations import consume_reservation

    consume_reservation(business_id, amount)


def touch_user_activity(user: LegacyUser) -> None:
    """Apply the legacy inline last-active timestamp."""
    if not kudos_projection_is_async():
        user.last_active = datetime.utcnow()


def project_user_balance(user: LegacyUser, amount: KudosAmount, action: str) -> Decimal:
    """Apply a shadow balance/stat mutation and return its floor adjustment."""
    if kudos_projection_is_async():
        return Decimal("0.00")
    from horde.classes.base.user import UserStats
    from horde.database.kudos_counters import increment_counter

    original_balance = Decimal(str(user.kudos))
    amount_decimal = Decimal(str(amount))
    user.kudos = float((original_balance + amount_decimal).quantize(Decimal("0.01")))
    user.kudos = max(user.kudos, user.get_min_kudos())
    increment_counter(UserStats, {"user_id": user.id, "action": action}, amount_decimal)
    return (Decimal(str(user.kudos)) - original_balance - amount_decimal).quantize(Decimal("0.01"))


def project_user_escrow(user: LegacyUser, amount: KudosAmount) -> None:
    """Apply the pre-ledger evaluation-escrow mutation while shadowing."""
    if not kudos_projection_is_async():
        user.evaluating_kudos = round(user.evaluating_kudos + float(amount), 2)


def project_trust_promotion(user: LegacyUser) -> None:
    """Drain escrow with the historical inline path while shadowing."""
    if kudos_projection_is_async() or user.evaluating_kudos <= 0:
        return
    from horde.classes.base.kudos import kudos_event
    from horde.enums import KudosEntryType
    from horde.flask import db

    amount = user.evaluating_kudos
    with kudos_event():
        user.modify_evaluating_kudos(-amount, KudosEntryType.EVALUATION_PROMOTION)
        user.modify_kudos(
            amount,
            "accumulated",
            commit=False,
            entry_type=KudosEntryType.EVALUATION_PROMOTION,
        )
    db.session.commit()


def project_worker_contribution(worker: LegacyWorker, amount: float) -> None:
    """Apply the legacy worker contribution aggregate while shadowing."""
    if not kudos_projection_is_async():
        worker.contributions = round(worker.contributions + amount, 2)


def project_worker_fulfilment(
    worker: LegacyWorker,
    *,
    team_id: uuid.UUID | None,
    raw_things: float,
    kudos: float,
) -> None:
    """Apply legacy worker/team settlement aggregates while shadowing."""
    if kudos_projection_is_async():
        return
    worker.fulfilments += 1
    if team_id is None:
        return
    from horde import vars as hv

    worker.team.contributions = round(worker.team.contributions + raw_things / hv.thing_divisors[worker.wtype], 2)
    worker.team.fulfilments += 1
    worker.team.kudos = round(worker.team.kudos + kudos, 2)


def project_worker_kudos(worker: LegacyWorker, amount: float, action: str) -> None:
    """Apply legacy worker balance/stat mutations while shadowing."""
    if kudos_projection_is_async():
        return
    from horde.classes.base.worker import WorkerStats
    from horde.database.kudos_counters import increment_counter

    worker.kudos = round(worker.kudos + amount, 2)
    increment_counter(WorkerStats, {"worker_id": worker.id, "action": action}, amount)
