# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Single-account kudos reservations for transfers and upfront admission."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import exists, func, select

from horde.classes.base.kudos import KudosLedger, KudosReservation
from horde.database.kudos_db import acquire_payer_lock
from horde.enums import KudosAuditDetail
from horde.flask import db
from horde.metrics import kudos_reservations_rejected


class KudosPayer(Protocol):
    """Account surface needed by reservation admission."""

    id: int
    kudos: float

    def get_min_kudos(self) -> int:
        """Return the account's allowed minimum balance."""
        ...


def _decimal(value: Decimal | int | float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def reserved_kudos(user_id: int) -> Decimal:
    """Return the payer's active reserved amount in the current transaction."""
    total = (
        db.session.query(func.coalesce(func.sum(KudosReservation.remaining_amount), 0))
        .filter(
            KudosReservation.user_id == user_id,
            KudosReservation.released_at.is_(None),
            KudosReservation.remaining_amount > 0,
        )
        .scalar()
    )
    return _decimal(total)


def available_kudos(user: KudosPayer) -> Decimal:
    """Return safely spendable kudos after floor, holds, and queued debits.

    A queued debit tagged with a reservation is skipped only while that
    reservation is still active, because its held share already covers it and
    subtracting both would double-count. Once the hold is released (for example
    an upfront hold released at prompt completion while its debit is still
    unprojected) the tagged debit is counted like any ordinary queued debit, so
    admission cannot re-spend money that has already left the balance in all but
    the applier's own bookkeeping. Queued credits are deliberately ignored until
    projected.

    Balance, active holds, and queued debits are read in one statement so they
    come from a single database snapshot: the applier commits a debit's fold and
    its hold consumption atomically, and a multi-statement read could otherwise
    interleave with that commit (or with an inline release) and observe the debit
    in no term at all.
    """
    from horde.classes.base.user import User

    balance_subquery = select(User.kudos).where(User.id == user.id).scalar_subquery()
    reserved_subquery = (
        select(func.coalesce(func.sum(KudosReservation.remaining_amount), 0))
        .where(
            KudosReservation.user_id == user.id,
            KudosReservation.released_at.is_(None),
            KudosReservation.remaining_amount > 0,
        )
        .scalar_subquery()
    )
    debit_hold_is_active = exists().where(
        KudosReservation.user_id == user.id,
        KudosReservation.business_id == KudosLedger.detail[KudosAuditDetail.RESERVATION_ID].as_string(),
        KudosReservation.released_at.is_(None),
        KudosReservation.remaining_amount > 0,
    )
    queued_debits_subquery = (
        select(func.coalesce(func.sum(-KudosLedger.amount), 0))
        .where(
            KudosLedger.user_id == user.id,
            KudosLedger.escrow.is_(False),
            KudosLedger.amount < 0,
            KudosLedger.applied.is_(False),
            ~debit_hold_is_active,
        )
        .scalar_subquery()
    )
    balance, reserved, queued_debits = db.session.execute(
        select(balance_subquery, reserved_subquery, queued_debits_subquery),
    ).one()
    if balance is None:
        balance = user.kudos
    return _decimal(balance) - _decimal(user.get_min_kudos()) - _decimal(reserved) - _decimal(queued_debits)


def effective_kudos(user: KudosPayer) -> Decimal:
    """Return the balance including every committed, unprojected currency delta."""
    pending_total = (
        db.session.query(func.coalesce(func.sum(KudosLedger.amount), 0))
        .filter(
            KudosLedger.user_id == user.id,
            KudosLedger.escrow.is_(False),
            KudosLedger.applied.is_(False),
        )
        .scalar()
    )
    return max(_decimal(user.kudos) + _decimal(pending_total), _decimal(user.get_min_kudos()))


def reserve_kudos(
    user: KudosPayer,
    amount: float | Decimal,
    *,
    business_id: str,
    event_id: uuid.UUID | None = None,
) -> KudosReservation | None:
    """Atomically reserve ``amount`` for one payer, returning ``None`` if short."""
    requested = _decimal(amount)
    if requested <= 0:
        raise ValueError("A kudos reservation must be positive")
    acquire_payer_lock(user.id)
    existing = db.session.query(KudosReservation).filter_by(business_id=business_id).first()
    if existing is not None:
        if existing.user_id != user.id:
            raise ValueError("A kudos reservation business id cannot change payer")
        if existing.released_at is None:
            return existing
        # A retryable job (for example an interrogation returned to WAITING after
        # a worker crash) reuses its stable business id. Reactivate the released
        # row under the same payer lock instead of weakening uniqueness or
        # silently proceeding without a hold.
        if available_kudos(user) < requested:
            kudos_reservations_rejected.add(1)
            return None
        existing.event_id = event_id
        existing.original_amount = requested
        existing.remaining_amount = requested
        existing.created = datetime.utcnow()
        existing.released_at = None
        db.session.flush()
        return existing
    if available_kudos(user) < requested:
        kudos_reservations_rejected.add(1)
        return None
    reservation = KudosReservation(
        business_id=business_id,
        user_id=user.id,
        event_id=event_id,
        original_amount=requested,
        remaining_amount=requested,
    )
    db.session.add(reservation)
    db.session.flush()
    return reservation


def consume_reservation(business_id: str, amount: float | Decimal) -> Decimal:
    """Release up to ``amount`` from a hold after its debit is projected."""
    reservation = db.session.query(KudosReservation).filter_by(business_id=business_id).with_for_update().first()
    if reservation is None or reservation.released_at is not None:
        return Decimal("0.00")
    consumed = min(reservation.remaining_amount, _decimal(amount))
    reservation.remaining_amount -= consumed
    if reservation.remaining_amount == 0:
        reservation.released_at = datetime.utcnow()
    return consumed


def release_reservation(business_id: str) -> Decimal:
    """Release the unused remainder of a reservation."""
    reservation = db.session.query(KudosReservation).filter_by(business_id=business_id).with_for_update().first()
    if reservation is None or reservation.released_at is not None:
        return Decimal("0.00")
    released = reservation.remaining_amount
    reservation.remaining_amount = Decimal("0.00")
    reservation.released_at = datetime.utcnow()
    return released


def release_reservations_for_business_ids(business_ids: Iterable[str]) -> int:
    """Release every active hold whose business id is in the given set (bulk).

    Cleanup passes that prune the owning request row with a set-based delete never
    run the ORM ``delete()`` that would otherwise release the upfront hold, so
    they call this to return the held kudos in the same transaction. Already
    released holds are skipped by the ``released_at`` predicate, so re-running the
    cleanup is harmless.

    Returns:
        The number of reservations released.
    """
    business_id_list = list(business_ids)
    if not business_id_list:
        return 0
    return (
        db.session.query(KudosReservation)
        .filter(
            KudosReservation.business_id.in_(business_id_list),
            KudosReservation.released_at.is_(None),
        )
        .update(
            {
                KudosReservation.remaining_amount: Decimal("0.00"),
                KudosReservation.released_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )


def release_event_reservations(event_ids: set[uuid.UUID]) -> None:
    """Release transfer-style holds once every posting in the event is folded."""
    if not event_ids:
        return
    reservations = (
        db.session.query(KudosReservation)
        .filter(
            KudosReservation.event_id.in_(event_ids),
            KudosReservation.released_at.is_(None),
        )
        .order_by(KudosReservation.user_id.asc())
        .with_for_update()
        .all()
    )
    for reservation in reservations:
        reservation.remaining_amount = Decimal("0.00")
        reservation.released_at = datetime.utcnow()
