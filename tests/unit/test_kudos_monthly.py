# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of monthly kudos entitlements.

Users with a monthly entitlement (moderators, patrons, or an admin-granted
allowance) are credited that entitlement once per calendar month. Granting or
changing an entitlement credits the difference to the balance immediately and
never lets the stored entitlement go negative. The recurring award advances a
per-user cursor by exactly one month so it cannot be claimed twice in the same
period.

The patreon and stripe integrations are external services outside the kudos
contract under test; they are pinned to contribute nothing so the in-database
entitlement behaviour is observed deterministically.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import dateutil.relativedelta
import pytest
from sqlalchemy.orm import Session

from horde.classes.base import user as user_module
from horde.enums import UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole

MODERATOR_MONTHLY_BONUS: int = 300000


@pytest.fixture(autouse=True)
def _no_external_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_module.patrons, "get_monthly_kudos", lambda user_id: 0)
    monkeypatch.setattr(user_module.stripe_subs, "get_monthly_kudos", lambda user_id: 0)


class TestModifyMonthlyEntitlement:
    """Granting an entitlement credits the difference immediately."""

    def test_positive_grant_credits_balance_and_stores_entitlement(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A positive grant credits the balance and stores the entitlement."""
        user = make_user(kudos=1000)
        user.modify_monthly_kudos(500)
        settle_kudos()
        assert user.kudos == 1500
        assert user.monthly_kudos == 500
        assert user.monthly_kudos_last_received is not None

    def test_increasing_entitlement_credits_only_the_difference(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Raising an entitlement credits only the increase and leaves the received-date cursor untouched."""
        user = make_user(kudos=1000)
        user.modify_monthly_kudos(500)
        first_received = user.monthly_kudos_last_received
        user.modify_monthly_kudos(200)
        settle_kudos()
        assert user.kudos == 1700
        assert user.monthly_kudos == 700
        assert user.monthly_kudos_last_received == first_received

    def test_entitlement_never_goes_negative_and_debits_nothing(
        self,
        db_session: Session,
        make_user: MakeUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A reduction clamps the stored entitlement at zero and never debits the balance."""
        user = make_user(kudos=1000)
        user.modify_monthly_kudos(100)
        user.modify_monthly_kudos(-500)
        settle_kudos()
        assert user.monthly_kudos == 0
        assert user.kudos == 1100


class TestReceiveMonthlyKudos:
    """The recurring award credits the entitlement once per month."""

    def test_entitlement_credited_on_first_receipt(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """The first receipt credits the stored entitlement and stamps the received-date cursor."""
        user = make_user(kudos=1000)
        user.monthly_kudos = 500
        db_session.flush()
        user.receive_monthly_kudos()
        settle_kudos()
        assert user.kudos == 1500
        assert user.monthly_kudos_last_received is not None

    def test_moderator_bonus_is_credited(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A moderator receives the monthly bonus on top of any entitlement."""
        user = make_user(kudos=1000)
        make_user_role(user, UserRoleTypes.MODERATOR)
        db_session.flush()
        user.receive_monthly_kudos()
        settle_kudos()
        assert user.kudos == 1000 + MODERATOR_MONTHLY_BONUS

    def test_cursor_advances_by_one_month(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """Receipt advances the received-date cursor exactly one month from its prior value."""
        user = make_user(kudos=1000)
        previous = datetime.utcnow() - timedelta(days=40)
        user.monthly_kudos = 500
        user.monthly_kudos_last_received = previous
        db_session.flush()

        user.receive_monthly_kudos()
        settle_kudos()

        assert user.kudos == 1500
        expected = previous + dateutil.relativedelta.relativedelta(months=+1)
        assert user.monthly_kudos_last_received == expected

    def test_no_second_award_within_the_same_period(self, db_session: Session, make_user: MakeUser) -> None:
        """A second receipt within the same month credits nothing and leaves the cursor untouched."""
        user = make_user(kudos=1000)
        just_received = datetime.utcnow()
        user.monthly_kudos = 500
        user.monthly_kudos_last_received = just_received
        db_session.flush()

        user.receive_monthly_kudos()

        assert user.kudos == 1000
        assert user.monthly_kudos_last_received == just_received


class TestCalculateMonthlyKudos:
    """The awarded amount sums the stored entitlement and the moderator bonus."""

    def test_plain_entitlement(self, db_session: Session, make_user: MakeUser) -> None:
        """A plain user's calculated award equals the stored entitlement."""
        user = make_user(kudos=1000)
        user.monthly_kudos = 250
        db_session.flush()
        assert user.calculate_monthly_kudos() == 250

    def test_moderator_adds_bonus(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """A moderator's calculated award adds the bonus to the stored entitlement."""
        user = make_user(kudos=1000)
        user.monthly_kudos = 250
        make_user_role(user, UserRoleTypes.MODERATOR)
        db_session.flush()
        assert user.calculate_monthly_kudos() == 250 + MODERATOR_MONTHLY_BONUS
