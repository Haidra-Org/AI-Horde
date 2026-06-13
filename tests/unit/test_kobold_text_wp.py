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

from horde.classes.kobold.waiting_prompt import TextWaitingPrompt


def _wp(slow_workers=True, max_length=80, max_context_length=2048, workers=(), models=()):
    return SimpleNamespace(
        slow_workers=slow_workers,
        max_length=max_length,
        max_context_length=max_context_length,
        workers=list(workers),
        models=list(models),
    )


class TestRequireUpfrontKudos:
    def test_no_slow_workers_requires_upfront_with_unclamped_budget(self):
        # When slow workers are disallowed the clamp is skipped, so the returned
        # token budget is the raw formula (512 + threads*5 - round(queue*0.9)).
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=False),
            {"queued_text_requests": 0},
            100,
        )
        assert result == (True, 1012, False)

    def test_within_budget_no_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=400),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (False, 512, False)

    def test_over_budget_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=600),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (True, 512, False)

    def test_queue_pressure_clamps_budget_to_floor(self):
        # queue=300 -> 512 - 270 = 242, clamped up to the 256 floor; 300 > 256.
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=300),
            {"queued_text_requests": 300},
            0,
        )
        assert result == (True, 256, False)

    def test_workerlist_env_forces_upfront_and_blocks_downgrade(self, monkeypatch):
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=200, workers=["w"]),
            {"queued_text_requests": 0},
            0,
        )
        assert result == (True, 512, True)


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
