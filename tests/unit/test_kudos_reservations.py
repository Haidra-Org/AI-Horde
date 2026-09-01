# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coverage for the batched reservation consumption the kudos applier uses.

``consume_reservations`` replaced one locked SELECT per hold per applier cycle; it must keep the per-hold
semantics of ``consume_reservation`` exactly (partial drains keep the hold open, a hold drained to zero is
stamped released, released or unknown holds consume nothing, over-consumption is capped at the remainder).
"""

from __future__ import annotations

from decimal import Decimal

from horde.classes.base.kudos import KudosReservation
from horde.database.kudos_reservations import consume_reservation, consume_reservations, release_reservation, reserve_kudos


def _reservations_by_business_id(db_session) -> dict[str, KudosReservation]:
    db_session.expire_all()
    return {reservation.business_id: reservation for reservation in db_session.query(KudosReservation).all()}


class TestConsumeReservations:
    def test_matches_per_hold_semantics(self, db_session, make_user):
        payer = make_user(kudos=10000)
        reserve_kudos(payer, 100, business_id="hold-drained")
        reserve_kudos(payer, 100, business_id="hold-partial")
        reserve_kudos(payer, 100, business_id="hold-capped")
        reserve_kudos(payer, 50, business_id="hold-released")
        release_reservation("hold-released")
        db_session.commit()

        total_consumed = consume_reservations(
            {
                "hold-drained": 100,
                "hold-partial": 40,
                "hold-capped": 500,
                "hold-released": 10,
                "hold-missing": 5,
            },
        )
        db_session.commit()

        assert total_consumed == Decimal("240.00")
        reservations = _reservations_by_business_id(db_session)
        assert reservations["hold-drained"].remaining_amount == Decimal("0.00")
        assert reservations["hold-drained"].released_at is not None
        assert reservations["hold-partial"].remaining_amount == Decimal("60.00")
        assert reservations["hold-partial"].released_at is None
        assert reservations["hold-capped"].remaining_amount == Decimal("0.00")
        assert reservations["hold-capped"].released_at is not None
        assert reservations["hold-released"].remaining_amount == Decimal("0.00")
        assert "hold-missing" not in reservations

    def test_agrees_with_consume_reservation(self, db_session, make_user):
        payer = make_user(kudos=10000)
        reserve_kudos(payer, 80, business_id="single")
        reserve_kudos(payer, 80, business_id="batched")
        db_session.commit()

        single_consumed = consume_reservation("single", 30)
        batched_consumed = consume_reservations({"batched": 30})
        db_session.commit()

        assert single_consumed == batched_consumed == Decimal("30.00")
        reservations = _reservations_by_business_id(db_session)
        assert reservations["single"].remaining_amount == reservations["batched"].remaining_amount == Decimal("50.00")
        assert reservations["single"].released_at is None and reservations["batched"].released_at is None

    def test_empty_batch_is_a_no_op(self, db_session):
        assert consume_reservations({}) == Decimal("0.00")
