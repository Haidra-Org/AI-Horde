# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the per-model eta arithmetic behind ``/v2/status/models``.

``compute_model_eta`` is the extracted, DB-free core of what
``get_available_models`` reports as a model's ``eta``. Exercising it directly locks
the clamp, the sparse-history fallback and the no-capacity sentinel without
standing up the queue and worker tables the surrounding query walks.
"""

from __future__ import annotations

import pytest

from horde.database.functions import MODEL_ETA_NO_CAPACITY, compute_model_eta


def _legacy_eta(things_queued, worker_count, model_avg_perf):
    """The formula this replaced, kept to prove the common backlog case is unchanged."""
    total_performance_on_model = worker_count * model_avg_perf
    if total_performance_on_model > 0:
        return int(things_queued / total_performance_on_model)
    return MODEL_ETA_NO_CAPACITY


class TestNoCapacity:
    def test_no_workers_returns_sentinel(self):
        assert (
            compute_model_eta(
                things_queued=50000000,
                jobs_queued=10,
                worker_count=0,
                model_avg_perf=200000,
                global_avg_perf=150000,
            )
            == MODEL_ETA_NO_CAPACITY
        )

    def test_no_known_speed_at_all_returns_sentinel(self):
        assert (
            compute_model_eta(
                things_queued=50000000,
                jobs_queued=10,
                worker_count=4,
                model_avg_perf=0,
                global_avg_perf=0,
            )
            == MODEL_ETA_NO_CAPACITY
        )


class TestNoQueuedWork:
    @pytest.mark.parametrize("model_avg_perf", [0, 200000])
    def test_empty_queue_is_immediate(self, model_avg_perf):
        assert (
            compute_model_eta(
                things_queued=0,
                jobs_queued=0,
                worker_count=4,
                model_avg_perf=model_avg_perf,
                global_avg_perf=150000,
            )
            == 0
        )

    def test_things_without_jobs_is_immediate(self):
        # Defensive: a jobs count of zero must never reach the division.
        assert (
            compute_model_eta(
                things_queued=50000000,
                jobs_queued=0,
                worker_count=4,
                model_avg_perf=200000,
                global_avg_perf=150000,
            )
            == 0
        )


class TestDemandClamp:
    def test_clamp_not_engaged_matches_legacy_formula(self):
        # Backlog case: fewer threads than queued jobs, so every thread has work and
        # the reported eta must be exactly what the pre-clamp formula gave.
        things_queued = 50000000
        jobs_queued = 20
        worker_count = 4
        model_avg_perf = 200000
        assert compute_model_eta(
            things_queued=things_queued,
            jobs_queued=jobs_queued,
            worker_count=worker_count,
            model_avg_perf=model_avg_perf,
            global_avg_perf=150000,
        ) == _legacy_eta(things_queued, worker_count, model_avg_perf)

    def test_threads_equal_to_jobs_matches_legacy_formula(self):
        things_queued = 12000000
        worker_count = 8
        model_avg_perf = 250000
        assert compute_model_eta(
            things_queued=things_queued,
            jobs_queued=worker_count,
            worker_count=worker_count,
            model_avg_perf=model_avg_perf,
            global_avg_perf=150000,
        ) == _legacy_eta(things_queued, worker_count, model_avg_perf)

    def test_idle_capacity_is_clamped_to_demand(self):
        # One job queued against a hundred threads: only one thread can serve it, so the
        # eta is the time that single thread needs, not a hundredth of it.
        eta = compute_model_eta(
            things_queued=1000000,
            jobs_queued=1,
            worker_count=100,
            model_avg_perf=100000,
            global_avg_perf=150000,
        )
        assert eta == 10
        assert eta != _legacy_eta(1000000, 100, 100000)


class TestSparseHistoryFallback:
    def test_unknown_model_speed_falls_back_to_horde_average(self):
        # A model with workers but no recorded fulfilments used to report the sentinel.
        eta = compute_model_eta(
            things_queued=6000000,
            jobs_queued=6,
            worker_count=3,
            model_avg_perf=0,
            global_avg_perf=200000,
        )
        assert eta == 10
        assert eta != MODEL_ETA_NO_CAPACITY

    def test_known_model_speed_ignores_horde_average(self):
        things_queued = 6000000
        worker_count = 3
        model_avg_perf = 100000
        assert compute_model_eta(
            things_queued=things_queued,
            jobs_queued=6,
            worker_count=worker_count,
            model_avg_perf=model_avg_perf,
            global_avg_perf=999999999,
        ) == _legacy_eta(things_queued, worker_count, model_avg_perf)
