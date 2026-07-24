# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of the inline kudos *counter* mutation sites.

The kudos balance columns are applier-maintained (phase 1). The per-action stats
totals, per-user records, worker/team aggregates, and shared-key budgets are
still written directly and are the subject of the phase-2 fold. These tests pin
their current observable semantics so the fold can be designed against a fixed
contract.

Covered sites: user_stats action buckets, user_records, worker_stats action
buckets, worker contribution/fulfilment totals, worker uptime accrual and its
reward cursor, worker abort counters, team aggregates, the dead Style counter
methods, and shared-key budget consumption. The style-collection ``use_count``
double-count is not pinned here; it is an endpoint-tier behavior awaiting its
fix.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from horde.classes.base.style import Style
from horde.classes.base.team import Team
from horde.classes.base.user import User, UserRecords, UserSharedKey, UserStats
from horde.classes.base.worker import WorkerStats, WorkerTemplate
from horde.enums import UserRecordTypes, UserRoleTypes


def _make_worker(db_session: Any, owner: User) -> WorkerTemplate:
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:12]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    return worker


def _seed_settings(db_session: Any) -> None:
    """Ensure a HordeSettings row exists so ``settings.mode_raid`` can be read.

    ``log_aborted_job`` consults raid mode, which queries the single settings
    row; without it the query returns ``None`` and dereferences it.
    """
    from horde.classes.base.settings import HordeSettings

    if db_session.query(HordeSettings).first() is None:
        db_session.add(HordeSettings())
        db_session.flush()


def _make_team(db_session: Any, owner: User) -> Team:
    team = Team(name=f"team_{uuid.uuid4().hex[:12]}", owner_id=owner.id)
    db_session.add(team)
    db_session.flush()
    return team


def _user_stat(db_session: Any, user_id: int, action: str) -> float | None:
    row = db_session.query(UserStats).filter_by(user_id=user_id, action=action).first()
    return None if row is None else row.value


def _worker_stat(db_session: Any, worker_id: Any, action: str) -> float | None:
    row = db_session.query(WorkerStats).filter_by(worker_id=worker_id, action=action).first()
    return None if row is None else row.value


def _user_record(db_session: Any, user_id: int, record_type: UserRecordTypes, record: str) -> float | None:
    row = db_session.query(UserRecords).filter_by(user_id=user_id, record_type=record_type, record=record).first()
    return None if row is None else row.value


class TestUserRecords:
    """``update_user_record`` accumulates request counts and scaled thing totals.

    ``record_usage`` bumps a per-type REQUEST count by 1 and a USAGE total by the
    raw things scaled by the user's usage multiplier and the type divisor.
    ``record_contributions`` bumps a FULFILLMENT count by 1 and a CONTRIBUTION
    total by the raw things over the type divisor. ``record_style`` bumps a STYLE
    count by 1. These counts and thing totals are dimensioned by things and
    request counts, quantities that no kudos posting carries.
    """

    def test_record_usage_increments_request_count_and_scaled_usage(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        # image divisor is 1_000_000; usage_multiplier defaults to 1.0.
        user.record_usage(raw_things=2_000_000, kudos=50, usage_type="image")
        settle_kudos()
        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "image") == 1
        assert _user_record(db_session, user.id, UserRecordTypes.USAGE, "image") == 2.0

    def test_record_usage_respects_usage_multiplier(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000, usage_multiplier=1.5)
        user.record_usage(raw_things=2_000_000, kudos=50, usage_type="image")
        settle_kudos()
        # 2_000_000 * 1.5 / 1_000_000 == 3.0
        assert _user_record(db_session, user.id, UserRecordTypes.USAGE, "image") == 3.0

    def test_record_usage_request_count_accumulates(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        settle_kudos()
        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "image") == 2
        assert _user_record(db_session, user.id, UserRecordTypes.USAGE, "image") == 2.0

    def test_record_contributions_increments_fulfilment_and_contribution(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        owner.record_contributions(raw_things=3_000_000, kudos=100, contrib_type="image")
        settle_kudos()
        assert _user_record(db_session, owner.id, UserRecordTypes.FULFILLMENT, "image") == 1
        assert _user_record(db_session, owner.id, UserRecordTypes.CONTRIBUTION, "image") == 3.0

    def test_record_style_increments_style_count(self, db_session, make_user, settle_kudos):
        owner = make_user(kudos=0)
        owner.record_style(2, "image")
        settle_kudos()
        assert _user_record(db_session, owner.id, UserRecordTypes.STYLE, "image") == 1

    def test_distinct_record_types_are_tracked_separately(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        settle_kudos()
        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "image") == 1
        assert _user_record(db_session, user.id, UserRecordTypes.USAGE, "image") == 1.0
        assert _user_record(db_session, user.id, UserRecordTypes.CONTRIBUTION, "image") is None


class TestUserStatsActionBuckets:
    """``user_stats`` buckets by the free-form action, not by ledger entry type.

    The phase-2 fold cannot key user_stats off the posting's ``entry_type``: the
    ``accumulated`` bucket merges movements of different entry types. A request
    debit and a contribution credit are both GENERATION, and an uptime credit is
    UPTIME_REWARD, yet all three land in the single ``accumulated`` action bucket.
    """

    def test_accumulated_bucket_merges_debit_and_credit(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        # record_usage debits under "accumulated"; a direct credit under the same
        # action nets against it in the same bucket.
        user.record_usage(raw_things=0, kudos=40, usage_type="image")
        user.modify_kudos(10, "accumulated")
        settle_kudos()
        assert _user_stat(db_session, user.id, "accumulated") == -30

    def test_uptime_credit_lands_in_accumulated_bucket(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        owner.record_uptime(100)
        settle_kudos()
        # record_uptime credits via modify_kudos(action="accumulated"), so an
        # UPTIME_REWARD movement is indistinguishable from a GENERATION one in
        # user_stats.
        assert _user_stat(db_session, owner.id, "accumulated") == 100

    def test_styled_and_recurring_are_separate_buckets(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=0)
        user.record_style(2, "image")
        user.modify_monthly_kudos(500)
        settle_kudos()
        assert _user_stat(db_session, user.id, "styled") == 2
        assert _user_stat(db_session, user.id, "recurring") == 500


class TestWorkerStatsActionBuckets:
    """``worker_stats`` buckets by action; generation and uptime are distinct."""

    def test_generated_and_uptime_buckets_are_separate(self, db_session, make_user, settle_kudos):
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.modify_kudos(100, "generated")
        worker.modify_kudos(40, "uptime")
        settle_kudos()
        assert _worker_stat(db_session, worker.id, "generated") == 100
        assert _worker_stat(db_session, worker.id, "uptime") == 40


class TestWorkerContributionCounters:
    """A recorded contribution accrues worker things and a fulfilment count.

    ``record_contribution`` converts raw things to the worker's own contribution
    aggregate (raw over the type divisor) and increments the worker fulfilment
    count by one. These sit on the hot workers row and mirror the owner's
    user_records but scoped to the worker.
    """

    def test_contribution_accrues_things_and_fulfilment(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        settle_kudos()
        assert worker.contributions == 2.0
        assert worker.fulfilments == 1

    def test_repeated_contributions_accumulate(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.record_contribution(raw_things=1_000_000, kudos=50, things_per_sec=1)
        worker.record_contribution(raw_things=1_000_000, kudos=50, things_per_sec=1)
        settle_kudos()
        assert worker.contributions == 2.0
        assert worker.fulfilments == 2


class TestWorkerUptimeCounters:
    """Uptime seconds accrue every non-stale check-in; the cursor tracks rewards.

    ``check_in`` accrues wall-clock seconds since the previous check-in into the
    worker uptime on every non-stale check-in, independent of whether a reward
    crossing occurs. ``last_reward_uptime`` is set absolutely to the accrued
    uptime only when a reward is paid, so it is a cursor rather than an
    accumulator.
    """

    def test_uptime_accrues_on_check_in_without_reward(self, db_session, make_user):
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.last_check_in = datetime.utcnow() - timedelta(seconds=40)
        worker.uptime = 0
        worker.last_reward_uptime = 0
        db_session.flush()

        worker.check_in(ipaddr="10.0.0.1")

        # No reward (threshold not crossed) but the ~40s gap is accrued.
        assert worker.uptime >= 39
        assert worker.last_reward_uptime == 0

    def test_reward_crossing_advances_the_cursor_to_current_uptime(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        # A trusted owner keeps record_uptime out of the trust-evaluation branch,
        # which would otherwise read the KUDOS_TRUST_THRESHOLD env var.
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.last_check_in = datetime.utcnow() - timedelta(seconds=40)
        worker.uptime = worker.uptime_reward_threshold - 10
        worker.last_reward_uptime = 0
        db_session.flush()

        worker.check_in(ipaddr="10.0.0.1")
        settle_kudos()

        assert worker.uptime > worker.uptime_reward_threshold
        # The cursor is set absolutely to the accrued uptime at reward time.
        assert worker.last_reward_uptime == worker.uptime


class TestWorkerAbortCounters:
    """A logged aborted job bumps both abort counters; abort moves no kudos.

    ``log_aborted_job`` increments the hourly aborted-job count and the monotonic
    uncompleted-job count. The aborted count is order and time dependent: it
    resets to zero on an hourly rollover and on tripping the drop threshold.
    """

    def test_abort_increments_both_counters(self, db_session, make_user):
        _seed_settings(db_session)
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.aborted_jobs = 0
        worker.uncompleted_jobs = 0
        worker.last_aborted_job = datetime.utcnow()
        db_session.flush()

        worker.log_aborted_job()

        assert worker.aborted_jobs == 1
        assert worker.uncompleted_jobs == 1

    def test_hourly_rollover_resets_aborted_count(self, db_session, make_user):
        _seed_settings(db_session)
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.aborted_jobs = 5
        worker.uncompleted_jobs = 5
        # A last-abort timestamp older than an hour triggers the rollover reset.
        worker.last_aborted_job = datetime.utcnow() - timedelta(seconds=3601)
        db_session.flush()

        worker.log_aborted_job()

        # aborted_jobs is reset to 0 then incremented to 1; uncompleted keeps growing.
        assert worker.aborted_jobs == 1
        assert worker.uncompleted_jobs == 6


class TestTeamAggregates:
    """Team uptime accrues inline; the contribution aggregates are applier-maintained.

    ``record_uptime`` adds seconds to the team uptime inline. The contribution
    aggregates (things, fulfilment count, and the display kudos total) are
    applier-maintained: an image worker's submit stamps its team_id on its own
    postings, and the fold derives teams.contributions/fulfilments/kudos from
    them. The team kudos aggregate is a display total distinct from any balance
    the ledger maintains.
    """

    def test_record_uptime_accumulates_seconds(self, db_session, make_user):
        team = _make_team(db_session, make_user(kudos=0))
        team.record_uptime(600)
        team.record_uptime(600)
        assert team.uptime == 1200

    def test_worker_contribution_folds_into_its_team(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        team = _make_team(db_session, owner)
        worker = _make_worker(db_session, owner)
        worker.set_team(team)

        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        settle_kudos()

        # convert_contribution scales raw things to 2.0 and that converted amount
        # is what reaches the team aggregate (image workers only).
        assert team.contributions == 2.0
        assert team.fulfilments == 1
        assert team.kudos == 100


class TestSharedKeyBudget:
    """``consume_kudos`` debits the budget (clamped) and accrues utilization.

    A finite shared-key budget is debited by the consumed amount and floored at
    zero; the utilization total always grows by the full consumed amount. A
    budget of -1 is an unlimited sentinel: utilization still grows but the budget
    is not debited. A budget already at zero consumes nothing.
    """

    def _make_shared_key(self, db_session: Any, owner: User, *, kudos: int) -> UserSharedKey:
        key = UserSharedKey(user_id=owner.id, kudos=kudos)
        db_session.add(key)
        db_session.flush()
        return key

    def test_finite_budget_debits_and_accrues_utilization(self, db_session, make_user):
        key = self._make_shared_key(db_session, make_user(kudos=0), kudos=5000)
        key.consume_kudos(200)
        assert key.kudos == 4800
        assert key.utilized == 200

    def test_debit_is_floored_at_zero(self, db_session, make_user):
        key = self._make_shared_key(db_session, make_user(kudos=0), kudos=100)
        key.consume_kudos(250)
        assert key.kudos == 0
        # Utilization records the full requested consumption even past the floor.
        assert key.utilized == 250

    def test_unlimited_budget_is_not_debited_but_accrues_utilization(self, db_session, make_user):
        key = self._make_shared_key(db_session, make_user(kudos=0), kudos=-1)
        key.consume_kudos(200)
        assert key.kudos == -1
        assert key.utilized == 200

    def test_exhausted_budget_consumes_nothing(self, db_session, make_user):
        key = self._make_shared_key(db_session, make_user(kudos=0), kudos=0)
        key.consume_kudos(200)
        assert key.kudos == 0
        assert key.utilized == 0


class TestStyleDeadCode:
    """Style.record_usage / record_contribution reference absent columns.

    Both methods target attributes that are not columns on ``Style`` (which has
    only ``use_count`` and ``votes``) and have no callers; style usage is tracked
    by the inline ``use_count`` increment in ``apply_style``. Invoking either
    raises today. Pinned so a phase-2 cleanup that removes them is a deliberate,
    test-visible change rather than a silent deletion.
    """

    def _make_style(self, db_session: Any, owner: User) -> Style:
        style = Style(
            style_type="image",
            name=f"style_{uuid.uuid4().hex[:12]}",
            prompt="{p}",
            user_id=owner.id,
        )
        db_session.add(style)
        db_session.flush()
        return style

    def test_record_usage_raises_on_absent_attribute(self, db_session, make_user):
        style = self._make_style(db_session, make_user(kudos=0))
        with pytest.raises(AttributeError):
            style.record_usage()

    def test_record_contribution_raises_on_absent_attribute(self, db_session, make_user):
        style = self._make_style(db_session, make_user(kudos=0))
        with pytest.raises(AttributeError):
            style.record_contribution(1.0, 100)
