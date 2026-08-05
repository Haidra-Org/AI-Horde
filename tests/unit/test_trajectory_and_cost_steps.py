# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The split between how far a request denoises and how much that costs.

``get_accurate_steps`` answers "how far along the denoising path does this go" and
``get_evaluation_steps`` answers "how many times does the model run". They differ for any solver that
evaluates the model more than once per step, and the two questions have different right answers:

- A model's own step requirements describe where its output stops improving. That limit is a property
  of the trajectory and does not move because a solver evaluates the model twice per step.
- A time budget or a usage total is spent per model evaluation, so it does scale.

Conflating them is not a loud failure. It silently pushes second-order samplers into upfront-kudos
gates and step-count downgrades they do not warrant, which is what these tests pin against.
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
        self.params = {"sampler_name": sampler_name, "steps": steps}
        self.gen_payload = {}
        self.width = width
        self.height = height

    def get_model_names(self):
        return []

    def get_accurate_steps(self):
        return ImageWaitingPrompt.get_accurate_steps(self)

    def get_evaluation_steps(self):
        return ImageWaitingPrompt.get_evaluation_steps(self)


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
        assert FakeWaitingPrompt("k_euler", steps=30).get_accurate_steps() == 30

    def test_a_second_order_sampler_reports_the_steps_requested(self):
        # Two model evaluations per step is a cost, not a longer trajectory.
        assert FakeWaitingPrompt("k_heun", steps=30).get_accurate_steps() == 30

    def test_a_three_evaluation_sampler_reports_the_steps_requested(self):
        assert FakeWaitingPrompt("heunpp2", steps=30).get_accurate_steps() == 30

    def test_the_adaptive_sampler_reports_its_own_step_count(self):
        # It chooses its own step size and disregards the requested value entirely.
        assert FakeWaitingPrompt("k_dpm_adaptive", steps=5).get_accurate_steps() == 40


class TestCostIsScaled:
    def test_a_first_order_sampler_costs_one_evaluation_per_step(self):
        assert FakeWaitingPrompt("k_euler", steps=30).get_evaluation_steps() == 30

    def test_a_second_order_sampler_costs_two_evaluations_per_step(self):
        assert FakeWaitingPrompt("k_heun", steps=30).get_evaluation_steps() == 60

    def test_a_three_evaluation_sampler_costs_three_per_step(self):
        assert FakeWaitingPrompt("heunpp2", steps=30).get_evaluation_steps() == 90

    def test_a_multistep_solver_costs_one_evaluation_per_step(self):
        # It reuses its previous evaluation, so the "2M" in the name is not a cost.
        assert FakeWaitingPrompt("dpmpp_2m_sde", steps=30).get_evaluation_steps() == 30

    def test_an_unknown_sampler_costs_one_evaluation_per_step(self):
        assert FakeWaitingPrompt("not_a_real_sampler_xyz", steps=30).get_evaluation_steps() == 30

    def test_the_adaptive_sampler_costs_its_assumed_step_count(self):
        assert FakeWaitingPrompt("k_dpm_adaptive", steps=5).get_evaluation_steps() == 40


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


class TestUsageIsCountedInEvaluations:
    def test_usage_scales_with_evaluations_rather_than_steps(self):
        # `things` is what the user's recorded usage is measured in, so it should reflect the work done.
        first_order = FakeWaitingPrompt("k_euler", steps=30, width=512, height=512)
        second_order = FakeWaitingPrompt("k_heun", steps=30, width=512, height=512)

        first_order_things = first_order.width * first_order.height * first_order.get_evaluation_steps()
        second_order_things = second_order.width * second_order.height * second_order.get_evaluation_steps()

        assert second_order_things == first_order_things * 2


class TestModelStepLimitsReadTheTrajectory:
    """The comparisons against a model's own step requirements must not scale with solver cost.

    These mirror the upfront-kudos gates, the downgrade loop and the worker step-count match, all of
    which compare against `max_steps` from the model reference.
    """

    @pytest.mark.parametrize("sampler_name", ["k_euler", "k_heun", "heunpp2", "seeds_3"])
    def test_a_request_at_the_models_limit_is_within_it_on_any_sampler(self, sampler_name):
        model_max_steps = 30
        waiting_prompt = FakeWaitingPrompt(sampler_name, steps=model_max_steps)

        # This is the comparison the downgrade loop and the upfront-kudos gates perform.
        assert waiting_prompt.get_accurate_steps() <= model_max_steps

    def test_a_request_above_the_models_limit_is_still_caught(self):
        waiting_prompt = FakeWaitingPrompt("k_heun", steps=31)
        assert waiting_prompt.get_accurate_steps() > 30

    def test_the_lcm_step_gate_reads_the_trajectory(self):
        # The LCM gate allows 10 steps; a solver's evaluation count must not consume that allowance.
        waiting_prompt = FakeWaitingPrompt("k_heun", steps=10)
        assert waiting_prompt.get_accurate_steps() <= 10
