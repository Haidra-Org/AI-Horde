# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The split between requested trajectory progress and operational sampler work.

Trajectory steps answer "how far along the denoising path was requested". Estimated work answers how
much first-order-equivalent marginal inference the service should account for.

- A model's own step requirements describe where its output stops improving. That limit is a property
  of the trajectory and does not move because a solver evaluates the model twice per step.
- A time budget or a usage total scales with first-order-equivalent sampler work.

Conflating them is not a loud failure. It silently undercounts higher-order samplers in operational
gates or incorrectly treats adaptive accounting estimates as requested trajectory length.
"""

from __future__ import annotations

import pytest

from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt

pytestmark = pytest.mark.unit


class FakeWaitingPrompt:
    """The parts of a waiting prompt the step calculations read.

    The real class is a SQLAlchemy model and cannot be constructed without a database, but both
    calculations depend on nothing except ``params`` and the two dimensions.
    """

    def __init__(self, sampler_name="k_euler", steps=30, width=512, height=512):
        self.params = {"sampler_name": sampler_name, "steps": steps, "width": width, "height": height}
        self.gen_payload = {}
        self.width = width
        self.height = height
        self.n = 1
        self.slow_workers = False
        self.id = "fake"

    def get_model_names(self):
        return []

    def get_requested_trajectory_steps(self):
        return ImageWaitingPrompt.get_requested_trajectory_steps(self)

    def get_estimated_sampler_work(self):
        return ImageWaitingPrompt.get_estimated_sampler_work(self)

    def is_using_lcm(self):
        return False


class FakeWorker:
    """A worker with no time-budget adjustment of its own."""

    extra_slow_worker = False


class FakeProcessingGeneration:
    """Enough of a processing generation to exercise the job time budget.

    ``set_job_ttl`` commits to the database at the end, which is stubbed out here: the budget is fully
    computed by then, and persisting it is not what these tests are about.
    """

    def __init__(self, waiting_prompt):
        self.wp = waiting_prompt
        self.worker = FakeWorker()
        self.job_ttl = None

    def set_job_ttl(self, monkeypatch):
        from horde.classes.stable import processing_generation

        monkeypatch.setattr(processing_generation.db.session, "commit", lambda: None)
        return ImageProcessingGeneration.set_job_ttl(self)


class TestTrajectoryIsUnscaled:
    def test_a_first_order_sampler_reports_the_steps_requested(self):
        assert FakeWaitingPrompt("k_euler", steps=30).get_requested_trajectory_steps() == 30

    def test_a_second_order_sampler_reports_the_steps_requested(self):
        # A two-unit marginal work rate is a cost, not a longer trajectory.
        assert FakeWaitingPrompt("k_heun", steps=30).get_requested_trajectory_steps() == 30

    def test_a_three_unit_sampler_reports_the_steps_requested(self):
        assert FakeWaitingPrompt("heunpp2", steps=30).get_requested_trajectory_steps() == 30

    def test_the_adaptive_sampler_preserves_requested_trajectory_length(self):
        assert FakeWaitingPrompt("k_dpm_adaptive", steps=5).get_requested_trajectory_steps() == 5


class TestEstimatedWork:
    def test_a_first_order_sampler_costs_one_work_unit_per_step(self):
        assert FakeWaitingPrompt("k_euler", steps=30).get_estimated_sampler_work().work_units.value == 30

    def test_a_second_order_sampler_costs_two_work_units_per_step(self):
        assert FakeWaitingPrompt("k_heun", steps=30).get_estimated_sampler_work().work_units.value == 60

    def test_a_three_unit_sampler_costs_three_per_step(self):
        assert FakeWaitingPrompt("heunpp2", steps=30).get_estimated_sampler_work().work_units.value == 90

    def test_a_multistep_solver_costs_one_work_unit_per_step(self):
        # It reuses prior state, so the "2M" in the name is not a marginal work multiplier.
        assert FakeWaitingPrompt("dpmpp_2m_sde", steps=30).get_estimated_sampler_work().work_units.value == 30

    def test_an_unknown_sampler_uses_the_legacy_first_order_fallback(self):
        assert FakeWaitingPrompt("not_a_real_sampler_xyz", steps=30).get_estimated_sampler_work().work_units.value == 30

    def test_the_adaptive_sampler_costs_its_assumed_step_count(self):
        assert FakeWaitingPrompt("k_dpm_adaptive", steps=5).get_estimated_sampler_work().work_units.value == 40


class TestJobTimeBudget:
    def test_a_second_order_sampler_gets_twice_the_budget_of_a_first_order_one(self, monkeypatch):
        first_order = FakeProcessingGeneration(FakeWaitingPrompt("k_euler", steps=50, width=1024, height=1024))
        second_order = FakeProcessingGeneration(FakeWaitingPrompt("k_heun", steps=50, width=1024, height=1024))
        first_order.set_job_ttl(monkeypatch)
        second_order.set_job_ttl(monkeypatch)

        # The fixed 30 second model-loading allowance is outside the scaled part.
        assert (second_order.job_ttl - 30) == (first_order.job_ttl - 30) * 2

    def test_a_three_evaluation_sampler_gets_three_times_the_budget(self, monkeypatch):
        first_order = FakeProcessingGeneration(FakeWaitingPrompt("k_euler", steps=50, width=1024, height=1024))
        three_eval = FakeProcessingGeneration(FakeWaitingPrompt("heunpp2", steps=50, width=1024, height=1024))
        first_order.set_job_ttl(monkeypatch)
        three_eval.set_job_ttl(monkeypatch)

        assert (three_eval.job_ttl - 30) == (first_order.job_ttl - 30) * 3

    def test_the_minimum_budget_still_applies(self, monkeypatch):
        tiny = FakeProcessingGeneration(FakeWaitingPrompt("k_euler", steps=1, width=512, height=512))
        tiny.set_job_ttl(monkeypatch)
        assert tiny.job_ttl == 150


class TestUsageIsCountedInWorkUnits:
    def test_usage_scales_with_work_rather_than_trajectory_steps(self):
        # `things` is what the user's recorded usage is measured in, so it should reflect the work done.
        first_order = FakeWaitingPrompt("k_euler", steps=30, width=512, height=512)
        second_order = FakeWaitingPrompt("k_heun", steps=30, width=512, height=512)

        first_order_things = first_order.width * first_order.height * first_order.get_estimated_sampler_work().work_units.value
        second_order_things = second_order.width * second_order.height * second_order.get_estimated_sampler_work().work_units.value

        assert second_order_things == first_order_things * 2


class TestOperationalBudgetsReadWork:
    """Service workload gates compare explicit work units, not ambiguous sampler steps."""

    @pytest.mark.parametrize("sampler_name", ["k_euler", "k_heun", "heunpp2", "seeds_3"])
    def test_a_request_at_a_trajectory_limit_has_sampler_dependent_work(self, sampler_name):
        waiting_prompt = FakeWaitingPrompt(sampler_name, steps=30)
        expected = {"k_euler": 30, "k_heun": 60, "heunpp2": 90, "seeds_3": 90}[sampler_name]
        assert waiting_prompt.get_estimated_sampler_work().work_units.value == expected

    def test_a_request_above_the_models_limit_is_still_caught(self):
        waiting_prompt = FakeWaitingPrompt("k_heun", steps=31)
        assert waiting_prompt.get_estimated_sampler_work().work_units.value > 30

    def test_the_lcm_step_gate_reads_the_trajectory(self):
        # A 10-unit operational budget admits five second-order trajectory steps, not ten.
        waiting_prompt = FakeWaitingPrompt("k_heun", steps=10)
        assert waiting_prompt.get_estimated_sampler_work().work_units.value > 10


class TestDowngradePlanning:
    def test_over_budget_adaptive_request_is_left_entirely_unchanged(self):
        waiting_prompt = FakeWaitingPrompt("k_dpm_adaptive", steps=30, width=1024, height=1024)
        waiting_prompt.params["control_type"] = "canny"
        original = (dict(waiting_prompt.params), waiting_prompt.width, waiting_prompt.height, waiting_prompt.slow_workers)

        assert ImageWaitingPrompt.downgrade(waiting_prompt, 512) is False
        assert (waiting_prompt.params, waiting_prompt.width, waiting_prompt.height, waiting_prompt.slow_workers) == original
