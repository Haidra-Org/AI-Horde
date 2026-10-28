# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the text (kobold) ``TextWaitingPrompt`` decision logic.

The stable/image line has dedicated tests; the text line had none. The kudos and
upfront-requirement methods here are effectively pure given their inputs. They
read a handful of attributes off ``self`` and an env var, so we exercise the
branch matrix by binding each method to a lightweight stub rather than building a
full persisted ORM graph (``WaitingPrompt.__init__`` commits to the DB). This
keeps the test hermetic and fast while still locking the real arithmetic.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from horde.classes.base.waiting_prompt import estimate_queue_wait_seconds
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt, compute_free_output_allowance
from horde.config import Config


def _wp(slow_workers=True, max_length=80, max_context_length=2048, workers=(), models=()):
    return SimpleNamespace(
        slow_workers=slow_workers,
        max_length=max_length,
        max_context_length=max_context_length,
        workers=list(workers),
        models=list(models),
    )


FREE_OUTPUT_CEILING = 4096
"""The default ceiling of the free output allowance, which the curve tests pass explicitly."""

FREE_OUTPUT_FLOOR = 512
"""The default floor of the free output allowance."""

FULL_WAIT_SECONDS = 60.0
"""The default longest estimated wait that still gives the ceiling."""

FLOOR_WAIT_SECONDS = 300.0
"""The default estimated wait from which the allowance is the floor."""


def _allowance(estimated_wait_seconds: float | None) -> int:
    return compute_free_output_allowance(
        estimated_wait_seconds=estimated_wait_seconds,
        ceiling=FREE_OUTPUT_CEILING,
        floor=FREE_OUTPUT_FLOOR,
        full_wait_seconds=FULL_WAIT_SECONDS,
        floor_wait_seconds=FLOOR_WAIT_SECONDS,
    )


class TestComputeFreeOutputAllowance:
    def test_no_wait_gives_the_ceiling(self):
        assert _allowance(0.0) == FREE_OUTPUT_CEILING

    def test_a_wait_up_to_the_full_threshold_gives_the_ceiling(self):
        assert _allowance(FULL_WAIT_SECONDS) == FREE_OUTPUT_CEILING

    def test_the_allowance_falls_linearly_between_the_thresholds(self):
        """Halfway between the thresholds the allowance is halfway between the ceiling and the floor."""
        halfway_wait = (FULL_WAIT_SECONDS + FLOOR_WAIT_SECONDS) / 2
        assert _allowance(halfway_wait) == (FREE_OUTPUT_CEILING + FREE_OUTPUT_FLOOR) / 2
        assert _allowance(120.0) == 3200
        assert FREE_OUTPUT_FLOOR < _allowance(FULL_WAIT_SECONDS + 1) < FREE_OUTPUT_CEILING

    def test_a_wait_at_the_floor_threshold_gives_the_floor(self):
        assert _allowance(FLOOR_WAIT_SECONDS) == FREE_OUTPUT_FLOOR

    def test_a_wait_beyond_the_floor_threshold_gives_the_floor(self):
        assert _allowance(FLOOR_WAIT_SECONDS * 10) == FREE_OUTPUT_FLOOR

    def test_no_eligible_worker_gives_the_floor(self):
        assert _allowance(None) == FREE_OUTPUT_FLOOR


class TestEstimateQueueWaitSeconds:
    def test_the_wait_is_the_queued_work_over_the_combined_throughput(self):
        assert estimate_queue_wait_seconds(queued_things=1200, queued_jobs=6, worker_threads=3, things_per_second=60) == 20

    def test_fewer_jobs_than_threads_count_only_the_threads_they_occupy(self):
        """One job on a three-thread pool runs at one thread's share of the combined throughput."""
        assert estimate_queue_wait_seconds(queued_things=200, queued_jobs=1, worker_threads=3, things_per_second=60) == 10

    def test_an_empty_queue_has_no_wait(self):
        assert estimate_queue_wait_seconds(queued_things=0, queued_jobs=0, worker_threads=3, things_per_second=60) == 0

    def test_no_throughput_divides_the_work_by_one(self):
        assert estimate_queue_wait_seconds(queued_things=80, queued_jobs=1, worker_threads=0, things_per_second=0) == 80


class TestRequireUpfrontKudos:
    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", FREE_OUTPUT_CEILING)
        monkeypatch.setattr(Config, "text_free_output_floor", FREE_OUTPUT_FLOOR)
        monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", FULL_WAIT_SECONDS)
        monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", FLOOR_WAIT_SECONDS)
        monkeypatch.delenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", raising=False)

    def test_a_short_wait_serves_up_to_the_ceiling_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=FREE_OUTPUT_CEILING), estimated_wait_seconds=0.0)
        assert result == (False, FREE_OUTPUT_CEILING, False)

    def test_over_the_allowance_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=FREE_OUTPUT_CEILING + 1), estimated_wait_seconds=0.0)
        assert result == (True, FREE_OUTPUT_CEILING, False)

    def test_a_long_wait_serves_only_the_floor_free(self):
        within_floor = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
        )
        over_floor = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR + 1),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
        )
        assert within_floor == (False, FREE_OUTPUT_FLOOR, False)
        assert over_floor == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_eligible_worker_serves_only_the_floor_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=FREE_OUTPUT_FLOOR + 1), estimated_wait_seconds=None)
        assert result == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_slow_workers_always_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(slow_workers=False, max_length=80), estimated_wait_seconds=0.0)
        assert result == (True, FREE_OUTPUT_CEILING, False)

    def test_configured_ceiling_floor_and_thresholds_apply(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", 1024)
        monkeypatch.setattr(Config, "text_free_output_floor", 256)
        monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", 10.0)
        monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", 20.0)
        idle = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), estimated_wait_seconds=10.0)
        halfway = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), estimated_wait_seconds=15.0)
        busy = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), estimated_wait_seconds=20.0)
        assert idle == (False, 1024, False)
        assert halfway == (True, 640, False)
        assert busy == (True, 256, False)

    def test_workerlist_env_forces_upfront_and_blocks_downgrade(self, monkeypatch):
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=200, workers=["w"]),
            estimated_wait_seconds=0.0,
        )
        assert result == (True, FREE_OUTPUT_CEILING, True)


class TestKudos:
    def test_extra_kudos_burn_adds_one(self):
        assert TextWaitingPrompt.calculate_extra_kudos_burn(SimpleNamespace(), 10) == 11

    def test_calculate_kudos_empty_model_list_uses_13b_assumption(self):
        # context_multiplier = 1.2 + 2.2**log2(2048/1024) = 3.4
        # round(80 * 13 * 3.4 / 100, 2) = 35.36
        assert TextWaitingPrompt.calculate_kudos(_wp(max_length=80, max_context_length=2048)) == pytest.approx(35.36)

    def test_calculate_kudos_matched_targeting_env_floors_to_min(self, monkeypatch):
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")
        wp = _wp(workers=["w"])
        assert TextWaitingPrompt.calculate_kudos(wp) == 0.1


class TestCalculateKudos:
    def test_unknown_model_quotes_from_the_prompt_itself(self, monkeypatch):
        """The unknown-model branch prices the request from its own max_length; the object has no ``wp``."""
        from horde.classes.kobold import waiting_prompt as text_wp_module

        monkeypatch.setattr(text_wp_module.model_reference, "is_known_text_model", lambda model_name: False)
        wp = _wp(max_length=100, max_context_length=1024, models=(SimpleNamespace(model="not-in-reference"),))

        kudos = TextWaitingPrompt.calculate_kudos(wp)

        context_multiplier = 1.2 + (2.2**0)
        assert kudos == pytest.approx(100 * (2.7 / 100) * context_multiplier)
