# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of the core kudos balance primitives.

Covers the per-account balance mutation (``User.modify_kudos`` and
``Worker.modify_kudos``) and the per-user-class balance floor. These are the
lowest-level accounting operations every kudos flow builds on: a signed delta is
added to the balance, the running per-action total is recorded, and a user
balance can never fall below the floor for that user's class. Worker balances
accrue independently and carry no floor.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from horde.classes.base.user import User, UserRecords, UserStats
from horde.classes.base.worker import WorkerStats, WorkerTemplate
from horde.enums import UserRecordTypes
from tests.fixture_types import MakeUser


def _stats_value(db_session: Session, user_id: int, action: str) -> int:
    """Return the recorded per-action total for a user, asserting the row exists."""
    row = db_session.query(UserStats).filter_by(user_id=user_id, action=action).first()
    assert row is not None, f"expected a UserStats row for user {user_id} action {action!r}"
    return row.value


def _make_worker(db_session: Session, owner: User) -> WorkerTemplate:
    """Create a persisted generic worker owned by the given user."""
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:12]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    return worker


class TestUserBalanceDelta:
    """A signed delta moves a user's balance and accrues a per-action total."""

    def test_positive_delta_raises_balance(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A positive delta increases the balance by that amount."""
        user = make_user(kudos=1000)
        user.modify_kudos(250, "award")
        settle_kudos()
        assert user.kudos == 1250

    def test_negative_delta_lowers_balance(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A negative delta decreases the balance by that amount."""
        user = make_user(kudos=1000)
        user.modify_kudos(-250, "accumulated")
        settle_kudos()
        assert user.kudos == 750

    def test_action_total_accumulates_across_calls(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """Repeated deltas for one action sum into that action's running total."""
        user = make_user(kudos=1000)
        user.modify_kudos(50, "award")
        user.modify_kudos(25, "award")
        settle_kudos()
        assert user.kudos == 1075
        assert _stats_value(db_session, user.id, "award") == 75

    def test_distinct_actions_are_tracked_separately(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Each action keeps its own independent running total."""
        user = make_user(kudos=1000)
        user.modify_kudos(40, "award")
        user.modify_kudos(-10, "accumulated")
        settle_kudos()
        assert _stats_value(db_session, user.id, "award") == 40
        assert _stats_value(db_session, user.id, "accumulated") == -10


class TestWorkerBalanceDelta:
    """Worker balances accrue per action independently and carry no floor."""

    def test_positive_delta_raises_worker_balance(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A positive delta increases a worker's balance."""
        owner = make_user(kudos=100)
        worker = _make_worker(db_session, owner)
        worker.modify_kudos(100, "generated")
        settle_kudos()
        assert worker.kudos == 100

    def test_worker_action_total_accumulates(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """Repeated deltas accumulate into the worker's per-action total."""
        owner = make_user(kudos=100)
        worker = _make_worker(db_session, owner)
        worker.modify_kudos(100, "generated")
        worker.modify_kudos(40, "generated")
        settle_kudos()
        assert worker.kudos == 140
        row = db_session.query(WorkerStats).filter_by(worker_id=worker.id, action="generated").first()
        assert row is not None
        assert row.value == 140

    def test_worker_balance_has_no_floor(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A worker balance may go arbitrarily negative."""
        owner = make_user(kudos=100)
        worker = _make_worker(db_session, owner)
        worker.modify_kudos(-500, "generated")
        settle_kudos()
        assert worker.kudos == -500


class TestAccumulatorComposition:
    """Repeated increments to one accumulator compose into a single running total.

    The per-action stats totals and per-user records are folded with in-database
    increments (so concurrent writers cannot lose an update). Two sequential
    increments to the same key must land as their sum on exactly one row.
    """

    def test_user_stats_increments_compose(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Two same-action deltas fold as their sum onto one user_stats row."""
        user = make_user(kudos=0)
        user.modify_kudos(3, "award")
        user.modify_kudos(4, "award")
        settle_kudos()
        rows = db_session.query(UserStats).filter_by(user_id=user.id, action="award").all()
        assert len(rows) == 1
        assert rows[0].value == 7

    def test_worker_stats_increments_compose(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Two same-action deltas fold as their sum onto one worker_stats row."""
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.modify_kudos(3, "generated")
        worker.modify_kudos(4, "generated")
        settle_kudos()
        rows = db_session.query(WorkerStats).filter_by(worker_id=worker.id, action="generated").all()
        assert len(rows) == 1
        assert rows[0].value == 7

    def test_user_record_increments_compose(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Two increments to one user-record key fold as their sum onto one row."""
        user = make_user(kudos=0)
        user.update_user_record(UserRecordTypes.USAGE, "image", 3)
        user.update_user_record(UserRecordTypes.USAGE, "image", 4)
        settle_kudos()
        rows = db_session.query(UserRecords).filter_by(user_id=user.id, record_type=UserRecordTypes.USAGE, record="image").all()
        assert len(rows) == 1
        assert rows[0].value == 7


class TestBalanceFloor:
    """A debit below a user-class floor clamps the balance to that floor."""

    def test_anonymous_floor_is_negative_fifty(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """The anonymous user's balance floor is -50 kudos."""
        anon = make_user(username="Anonymous", oauth_id="anon", kudos=10)
        assert anon.get_min_kudos() == -50
        anon.modify_kudos(-100, "accumulated")
        settle_kudos()
        assert anon.kudos == -50

    def test_named_floor_is_twenty_five(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A named user's balance floor is 25 kudos."""
        user = make_user(kudos=30)
        assert user.get_min_kudos() == 25
        user.modify_kudos(-100, "accumulated")
        settle_kudos()
        assert user.kudos == 25

    def test_pseudonymous_floor_is_fourteen(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A pseudonymous user's balance floor is 14 kudos."""
        user = make_user(oauth_id=str(uuid.uuid4()), kudos=20)
        assert user.get_min_kudos() == 14
        user.modify_kudos(-100, "accumulated")
        settle_kudos()
        assert user.kudos == 14

    def test_debit_at_floor_stays_at_floor(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A further debit applied at the floor leaves the balance at the floor."""
        anon = make_user(username="Anonymous", oauth_id="anon", kudos=10)
        anon.modify_kudos(-100, "accumulated")
        settle_kudos()
        assert anon.kudos == -50
        anon.modify_kudos(-5, "accumulated")
        settle_kudos()
        assert anon.kudos == -50

    def test_balance_above_floor_is_left_untouched(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A debit that stays above the floor is applied in full."""
        user = make_user(kudos=1000)
        user.modify_kudos(-100, "accumulated")
        settle_kudos()
        assert user.kudos == 900

    def test_floor_forgiveness_is_an_explicit_applied_posting(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """The non-linear clamp is represented so snapshot replay stays exact."""
        from horde.classes.base.kudos import KudosLedger
        from horde.enums import KudosEntryType

        user = make_user(kudos=30)
        user.modify_kudos(-100, "accumulated")
        settle_kudos()

        correction = db_session.query(KudosLedger).filter_by(entry_type=KudosEntryType.FLOOR_ADJUSTMENT).one()
        assert correction.amount == 95
        assert correction.applied is True
