# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of the worker uptime reward.

A worker that stays online accrues uptime, and each time its accrued uptime
crosses the reward threshold both the worker and the worker's owner are credited
the uptime reward. The owner's share follows the same trust routing as any
credit: a trusted (or anonymous) owner receives it on the spendable balance,
while an untrusted owner's share is held in the evaluation escrow.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from horde.classes.base.user import User
from horde.classes.base.worker import WorkerTemplate
from horde.enums import UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole

# The base uptime reward for a generic worker (WorkerTemplate.calculate_uptime_reward).
BASE_UPTIME_REWARD: int = 100


@pytest.fixture(autouse=True)
def _trust_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # record_uptime consults the trust threshold for untrusted owners; pin it high
    # so escrow accrual is observed without triggering an automatic promotion.
    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")


def _make_worker(db_session: Session, owner: User, *, primed_to_cross: bool = False) -> WorkerTemplate:
    """Create a persisted worker, optionally primed to cross the uptime reward threshold on the next check-in."""
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:12]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    if primed_to_cross:
        # A recent check-in (past the 30s debounce, within the 300s staleness
        # window) whose accrued uptime lands just under the reward threshold, so
        # this check-in tips it over.
        worker.last_check_in = datetime.utcnow() - timedelta(seconds=40)
        worker.uptime = worker.uptime_reward_threshold - 10
        worker.last_reward_uptime = 0
    db_session.flush()
    return worker


class TestUptimeRewardCrossing:
    """Crossing the uptime threshold credits both the worker and its owner."""

    def test_trusted_owner_credited_on_balance(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """A trusted owner is credited the reward on the spendable balance."""
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner, primed_to_cross=True)

        worker.check_in(ipaddr="10.0.0.1")

        assert worker.kudos == BASE_UPTIME_REWARD
        assert owner.kudos == 1000 + BASE_UPTIME_REWARD
        assert owner.evaluating_kudos == 0

    def test_untrusted_owner_share_goes_to_escrow(self, db_session: Session, make_user: MakeUser) -> None:
        """An untrusted owner's reward is held in escrow while the worker is still credited."""
        owner = make_user(kudos=1000)
        worker = _make_worker(db_session, owner, primed_to_cross=True)

        worker.check_in(ipaddr="10.0.0.1")

        # The worker is always credited, but the untrusted owner's share is held
        # for evaluation rather than reaching the spendable balance.
        assert worker.kudos == BASE_UPTIME_REWARD
        assert owner.kudos == 1000
        assert owner.evaluating_kudos == BASE_UPTIME_REWARD

    def test_no_reward_before_threshold_is_crossed(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """Uptime short of the threshold credits neither the worker nor the owner."""
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.last_check_in = datetime.utcnow() - timedelta(seconds=40)
        worker.uptime = 0
        worker.last_reward_uptime = 0
        db_session.flush()

        worker.check_in(ipaddr="10.0.0.1")

        assert worker.kudos == 0
        assert owner.kudos == 1000


class TestUptimeOwnerRouting:
    """``record_uptime`` routes the owner credit by trust state."""

    def test_trusted_owner_credited_on_balance(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """A trusted owner's uptime credit lands on the spendable balance."""
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        owner.record_uptime(100)
        assert owner.kudos == 1100
        assert owner.evaluating_kudos == 0

    def test_untrusted_owner_credited_to_escrow(self, db_session: Session, make_user: MakeUser) -> None:
        """An untrusted owner's uptime credit lands in the evaluation escrow."""
        owner = make_user(kudos=1000)
        owner.record_uptime(100)
        assert owner.kudos == 1000
        assert owner.evaluating_kudos == 100

    def test_anonymous_owner_credited_on_balance(self, db_session: Session, make_user: MakeUser) -> None:
        """An anonymous owner's uptime credit lands on the spendable balance."""
        anon = make_user(username="Anonymous", oauth_id="anon", kudos=1000)
        anon.record_uptime(100)
        assert anon.kudos == 1100
        assert anon.evaluating_kudos == 0

    def test_bypass_credits_balance_despite_untrusted(self, db_session: Session, make_user: MakeUser) -> None:
        """The evaluation bypass credits the balance even for an untrusted owner."""
        owner = make_user(kudos=1000)
        owner.record_uptime(100, bypass_eval=True)
        assert owner.kudos == 1100
        assert owner.evaluating_kudos == 0
