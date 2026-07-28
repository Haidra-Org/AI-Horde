# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behaviour of the materialized ``Worker.speed`` column.

``speed`` stores each worker's rolling-average throughput (raw things per second)
on the ``workers`` row. ``Worker.record_performance`` appends a sample,
``horde.database.threads.refresh_worker_speeds`` folds the retained samples into
the column, and construction seeds a per-type baseline so a worker that has never
submitted a generation reports a stable speed rather than deriving one on read.

The contracts exercised here are:

- A submit appends a performance sample.
- The refresh sets ``speed`` to the average of the retained performance samples.
- The refresh prunes ``worker_performances`` to the most recent samples, and dropped
  samples no longer influence the average.
- A worker with no samples, or whose samples average to zero, reports the per-type
  baseline, both when read as a Python attribute and when compared inside a
  pop-candidate-filter-shaped query. The baseline places image workers above, and
  text workers below, the speed thresholds the pop filters apply.
- A worker with no new samples is left untouched by a refresh.
- Status and team readers that consume ``speed`` keep working across both states.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from horde import vars as hv
from horde.classes.base.team import Team
from horde.classes.base.worker import SPEED_BASELINE_THINGS_PER_SEC, WorkerPerformance, WorkerTemplate
from horde.classes.kobold.worker import TextWorker
from horde.classes.stable.worker import ImageWorker
from horde.database.threads import WORKER_SPEED_SAMPLE_LIMIT
from horde.flask import db

pytestmark = pytest.mark.unit


@pytest.fixture
def refresh_speeds(db_session: Any) -> Callable[[], None]:
    """Return a helper that folds pending performance samples into ``workers.speed``.

    ``speed`` is refresh-maintained: a submit only appends a sample, and the periodic
    refresh prunes to the retained window and rewrites the column. Call the returned
    helper before asserting on ``speed`` so the assertion observes the folded result.
    Sample values and the averaging rule are unchanged; only the observation point is.

    The refresh runs in its own app context, and therefore its own session, exactly as
    the scheduled thread does. Its bulk UPDATE bypasses this test's identity map, so
    expire the test session afterwards to read the committed column.
    """
    from horde.database.threads import refresh_worker_speeds

    def _refresh() -> None:
        refresh_worker_speeds()
        db_session.expire_all()

    return _refresh


# The lower speed bounds the image and text pop-candidate filters apply. Mirrored from
# the production predicates (``horde/database/functions.py`` and
# ``horde/database/text_functions.py``) so the tests assert the same inclusion boundary
# a pop evaluates against the materialized column.
IMAGE_POP_SPEED_THRESHOLD = 500000
TEXT_POP_SPEED_THRESHOLD = 2


def _make_image_worker(db_session: Any, user: Any, *, name: str) -> ImageWorker:
    worker = ImageWorker(user_id=user.id, name=name)
    db_session.add(worker)
    db_session.commit()
    return worker


def _make_text_worker(db_session: Any, user: Any, *, name: str) -> TextWorker:
    worker = TextWorker(user_id=user.id, name=name)
    db_session.add(worker)
    db_session.commit()
    return worker


def _retained_performances(worker_id: Any) -> list[float]:
    rows = db.session.query(WorkerPerformance.performance).filter_by(worker_id=worker_id).all()
    return [row.performance for row in rows]


class TestBaselineSpeedWithoutSamples:
    """A worker with no performance samples reports the per-type baseline speed."""

    def test_fresh_image_worker_reports_image_baseline(self, db_session, make_user):
        worker = _make_image_worker(db_session, make_user(), name="speed_fresh_image")

        assert worker.speed == SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors["image"]

    def test_fresh_text_worker_reports_text_baseline(self, db_session, make_user):
        worker = _make_text_worker(db_session, make_user(), name="speed_fresh_text")

        assert worker.speed == SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors["text"]


class TestBaselineSpeedAgainstPopFilter:
    """Baseline speed keeps fresh workers on the pop filter's expected side.

    The pop candidate filters compare ``speed`` against a per-type threshold. A fresh
    image worker's baseline clears the image threshold (so it can be offered work
    immediately), while a fresh text worker's baseline falls below the text threshold
    (so it is treated as a slow worker until it records real samples).
    """

    def test_fresh_image_worker_passes_image_speed_filter(self, db_session, make_user):
        worker = _make_image_worker(db_session, make_user(), name="speed_filter_image")

        matched = (
            db.session.query(WorkerTemplate.id)
            .filter(WorkerTemplate.id == worker.id, WorkerTemplate.speed >= IMAGE_POP_SPEED_THRESHOLD)
            .first()
        )

        assert matched is not None

    def test_fresh_text_worker_excluded_by_text_speed_filter(self, db_session, make_user):
        worker = _make_text_worker(db_session, make_user(), name="speed_filter_text")

        matched = (
            db.session.query(WorkerTemplate.id)
            .filter(WorkerTemplate.id == worker.id, WorkerTemplate.speed >= TEXT_POP_SPEED_THRESHOLD)
            .first()
        )

        assert matched is None


class TestRecordPerformanceAppendsSamples:
    """``record_performance`` persists the sample the submit path hands it."""

    def test_recording_a_performance_appends_a_sample(self, db_session, make_user):
        worker = _make_image_worker(db_session, make_user(), name="speed_sample_append")

        worker.record_performance(100.0)

        assert _retained_performances(worker.id) == [100.0]

    def test_recording_leaves_speed_for_the_refresh_to_write(self, db_session, make_user):
        worker = _make_image_worker(db_session, make_user(), name="speed_append_only")

        worker.record_performance(100.0)

        assert worker.speed == SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors["image"]


class TestRefreshMaintainsAverage:
    """The refresh keeps ``speed`` equal to the average of retained samples."""

    def test_speed_tracks_running_average(self, db_session, make_user, refresh_speeds):
        worker = _make_image_worker(db_session, make_user(), name="speed_running_avg")

        worker.record_performance(100.0)
        refresh_speeds()
        assert worker.speed == pytest.approx(100.0)

        worker.record_performance(200.0)
        refresh_speeds()
        assert worker.speed == pytest.approx(150.0)

    def test_speed_reflects_only_retained_samples_after_pruning(self, db_session, make_user, frozen_time, refresh_speeds):
        worker = _make_image_worker(db_session, make_user(), name="speed_pruned_avg")
        recorded_values = [float(i) for i in range(1, 26)]

        with frozen_time("2026-01-01 00:00:00") as frozen:
            for value in recorded_values:
                worker.record_performance(value)
                frozen.tick(timedelta(seconds=1))
            refresh_speeds()

        retained = _retained_performances(worker.id)
        assert len(retained) < len(recorded_values)
        assert min(retained) > recorded_values[0]
        assert worker.speed == pytest.approx(sum(retained) / len(retained))

    def test_pruning_keeps_exactly_the_most_recent_samples(self, db_session, make_user, frozen_time, refresh_speeds):
        worker = _make_image_worker(db_session, make_user(), name="speed_prune_window")
        recorded_values = [float(i) for i in range(1, 26)]

        with frozen_time("2026-01-01 00:00:00") as frozen:
            for value in recorded_values:
                worker.record_performance(value)
                frozen.tick(timedelta(seconds=1))
            refresh_speeds()

        assert sorted(_retained_performances(worker.id)) == recorded_values[-WORKER_SPEED_SAMPLE_LIMIT:]

    def test_worker_without_new_samples_is_left_untouched(self, db_session, make_user, refresh_speeds):
        worker = _make_image_worker(db_session, make_user(), name="speed_quiet_worker")
        db_session.add(
            WorkerPerformance(
                worker_id=worker.id,
                performance=100.0,
                created=datetime.utcnow() - timedelta(days=1),
            ),
        )
        db_session.commit()

        # A hand-written value stands in for whatever the refresh would otherwise
        # compute: a worker whose samples all predate the refresh window must not be
        # rewritten, and its samples must survive.
        worker.speed = 12345.0
        db_session.commit()
        refresh_speeds()

        assert worker.speed == pytest.approx(12345.0)
        assert _retained_performances(worker.id) == [100.0]


class TestZeroAverageFallsBackToBaseline:
    """Samples averaging zero leave ``speed`` at the baseline rather than storing zero.

    ``speed`` is a divisor in ``ProcessingGeneration.get_seconds_needed``, so a stored
    zero makes that reader raise. A worker whose retained samples average to zero is
    therefore treated the same as one with no samples at all.
    """

    def test_zero_sample_leaves_speed_at_baseline(self, db_session, make_user, refresh_speeds):
        worker = _make_text_worker(db_session, make_user(), name="speed_zero_sample")

        worker.record_performance(0.0)
        refresh_speeds()

        assert worker.speed == SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors["text"]

    def test_zero_sample_leaves_image_speed_at_image_baseline(self, db_session, make_user, refresh_speeds):
        worker = _make_image_worker(db_session, make_user(), name="speed_zero_sample_image")

        worker.record_performance(0.0)
        refresh_speeds()

        assert worker.speed == SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors["image"]

    def test_speed_stays_nonzero_for_division_by_reading_callers(self, db_session, make_user, refresh_speeds):
        worker = _make_text_worker(db_session, make_user(), name="speed_zero_divisor")

        worker.record_performance(0.0)
        refresh_speeds()

        assert worker.speed != 0
        assert 100 / worker.speed > 0

    def test_nonzero_sample_after_zero_restores_the_average(self, db_session, make_user, refresh_speeds):
        worker = _make_text_worker(db_session, make_user(), name="speed_zero_then_real")

        worker.record_performance(0.0)
        worker.record_performance(10.0)
        refresh_speeds()

        assert worker.speed == pytest.approx(5.0)


class TestSpeedReaders:
    """Readers that consume ``speed`` work in both the baseline and sampled states."""

    def test_worker_performance_string_uses_speed(self, db_session, make_user, refresh_speeds):
        worker = _make_image_worker(db_session, make_user(), name="speed_reader_worker")

        baseline_description = worker.get_performance()
        assert "per second" in baseline_description

        worker.record_performance(250000.0)
        refresh_speeds()
        assert worker.get_performance() != baseline_description

    def test_team_performance_reads_member_speed(self, db_session, make_user):
        user = make_user()
        team = Team(name="speed_reader_team", owner_id=user.id)
        db_session.add(team)
        db_session.commit()
        worker = _make_image_worker(db_session, user, name="speed_team_member")
        worker.team_id = team.id
        db_session.commit()

        perf_avg, perf_total = team.get_performance()

        expected = round(worker.speed / hv.thing_divisors["image"], 1)
        assert perf_total == expected
        assert perf_avg == expected
