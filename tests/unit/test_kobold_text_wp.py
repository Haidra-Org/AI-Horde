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

FREE_OUTPUT_SLOPE = 1792.0
"""The default slope: one competing job per eligible thread halves the headroom above the floor."""


def _allowance(*, competing_jobs_ahead: int, eligible_threads: int) -> int:
    return compute_free_output_allowance(
        competing_jobs_ahead=competing_jobs_ahead,
        eligible_threads=eligible_threads,
        ceiling=FREE_OUTPUT_CEILING,
        floor=FREE_OUTPUT_FLOOR,
        slope=FREE_OUTPUT_SLOPE,
    )


class TestComputeFreeOutputAllowance:
    def test_idle_eligible_workers_give_the_ceiling(self):
        assert _allowance(competing_jobs_ahead=0, eligible_threads=4) == FREE_OUTPUT_CEILING

    def test_one_competing_job_per_thread_halves_the_headroom(self):
        assert _allowance(competing_jobs_ahead=4, eligible_threads=4) == 2304

    def test_load_is_measured_per_eligible_thread(self):
        """Six jobs ahead weigh on four threads as three jobs ahead do on two."""
        assert _allowance(competing_jobs_ahead=6, eligible_threads=4) == _allowance(competing_jobs_ahead=3, eligible_threads=2)
        assert _allowance(competing_jobs_ahead=6, eligible_threads=4) == 1408
        assert _allowance(competing_jobs_ahead=1, eligible_threads=4) == 3648

    def test_saturated_eligible_workers_give_the_floor(self):
        assert _allowance(competing_jobs_ahead=2, eligible_threads=1) == FREE_OUTPUT_FLOOR
        assert _allowance(competing_jobs_ahead=500, eligible_threads=10) == FREE_OUTPUT_FLOOR

    def test_no_eligible_thread_gives_the_floor(self):
        assert _allowance(competing_jobs_ahead=0, eligible_threads=0) == FREE_OUTPUT_FLOOR


class TestRequireUpfrontKudos:
    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", FREE_OUTPUT_CEILING)
        monkeypatch.setattr(Config, "text_free_output_floor", FREE_OUTPUT_FLOOR)
        monkeypatch.setattr(Config, "text_free_output_slope", FREE_OUTPUT_SLOPE)
        monkeypatch.delenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", raising=False)

    def test_idle_fleet_serves_up_to_the_ceiling_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_CEILING),
            competing_jobs_ahead=0,
            eligible_threads=1,
        )
        assert result == (False, FREE_OUTPUT_CEILING, False)

    def test_over_the_allowance_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_CEILING + 1),
            competing_jobs_ahead=0,
            eligible_threads=1,
        )
        assert result == (True, FREE_OUTPUT_CEILING, False)

    def test_saturated_fleet_serves_only_the_floor_free(self):
        within_floor = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR),
            competing_jobs_ahead=40,
            eligible_threads=4,
        )
        over_floor = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR + 1),
            competing_jobs_ahead=40,
            eligible_threads=4,
        )
        assert within_floor == (False, FREE_OUTPUT_FLOOR, False)
        assert over_floor == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_eligible_thread_serves_only_the_floor_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR + 1),
            competing_jobs_ahead=0,
            eligible_threads=0,
        )
        assert result == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_slow_workers_always_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=False, max_length=80),
            competing_jobs_ahead=0,
            eligible_threads=1,
        )
        assert result == (True, FREE_OUTPUT_CEILING, False)

    def test_configured_ceiling_and_floor_apply(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", 1024)
        monkeypatch.setattr(Config, "text_free_output_floor", 256)
        idle = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), competing_jobs_ahead=0, eligible_threads=1)
        busy = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), competing_jobs_ahead=100, eligible_threads=1)
        assert idle == (False, 1024, False)
        assert busy == (True, 256, False)

    def test_workerlist_env_forces_upfront_and_blocks_downgrade(self, monkeypatch):
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=200, workers=["w"]),
            competing_jobs_ahead=0,
            eligible_threads=1,
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
