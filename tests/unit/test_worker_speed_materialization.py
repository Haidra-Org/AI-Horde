# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behaviour of the materialized ``Worker.speed`` column.

``speed`` stores each worker's rolling-average throughput (raw things per second)
on the ``workers`` row. It is maintained by ``Worker.record_performance`` and
seeded to a per-type baseline on construction so a worker that has never submitted
a generation reports a stable speed rather than deriving one on read.

The contracts exercised here are:

- ``record_performance`` sets ``speed`` to the average of the retained performance
  samples.
- The pruning of ``worker_performances`` to the most recent samples is reflected in
  ``speed``: dropped samples no longer influence the average.
- A worker with no samples reports the per-type baseline, both when read as a Python
  attribute and when compared inside a pop-candidate-filter-shaped query. The
  baseline places image workers above, and text workers below, the speed thresholds
  the pop filters apply.
- Status and team readers that consume ``speed`` keep working across both states.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from horde import vars as hv
from horde.classes.base.team import Team
from horde.classes.base.worker import SPEED_BASELINE_THINGS_PER_SEC, WorkerPerformance, WorkerTemplate
from horde.classes.kobold.worker import TextWorker
from horde.classes.stable.worker import ImageWorker
from horde.flask import db

pytestmark = pytest.mark.unit

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


class TestRecordPerformanceMaintainsAverage:
    """``record_performance`` keeps ``speed`` equal to the average of retained samples."""

    def test_speed_tracks_running_average(self, db_session, make_user):
        worker = _make_image_worker(db_session, make_user(), name="speed_running_avg")

        worker.record_performance(100.0)
        assert worker.speed == pytest.approx(100.0)

        worker.record_performance(200.0)
        assert worker.speed == pytest.approx(150.0)

    def test_speed_reflects_only_retained_samples_after_pruning(self, db_session, make_user, frozen_time):
        worker = _make_image_worker(db_session, make_user(), name="speed_pruned_avg")
        recorded_values = [float(i) for i in range(1, 26)]

        with frozen_time("2026-01-01 00:00:00") as frozen:
            for value in recorded_values:
                worker.record_performance(value)
                frozen.tick(timedelta(seconds=1))

        retained = _retained_performances(worker.id)
        assert len(retained) < len(recorded_values)
        assert min(retained) > recorded_values[0]
        assert worker.speed == pytest.approx(sum(retained) / len(retained))


class TestSpeedReaders:
    """Readers that consume ``speed`` work in both the baseline and sampled states."""

    def test_worker_performance_string_uses_speed(self, db_session, make_user):
        worker = _make_image_worker(db_session, make_user(), name="speed_reader_worker")

        baseline_description = worker.get_performance()
        assert "per second" in baseline_description

        worker.record_performance(250000.0)
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
