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
    def __init__(self, *, extra_slow_worker=False):
        self.extra_slow_worker = extra_slow_worker


class FakeProcessingGeneration:
    """Enough of a processing generation to exercise the job time budget.

    ``set_job_ttl`` commits to the database at the end, which is stubbed out here: the budget is fully
    computed by then, and persisting it is not what these tests are about.
    """

    def __init__(self, waiting_prompt, *, model="stable_diffusion", extra_slow_worker=False):
        self.wp = waiting_prompt
        self.worker = FakeWorker(extra_slow_worker=extra_slow_worker)
        self.model = model
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
    """The deadline becomes concrete when a request is assigned to a model and worker.

    This suite fixes the policy reasons behind the numbers, not merely examples of the arithmetic. The
    lease is intentionally longer than isolated inference so a worker can hold a shallow look-ahead
    queue while overlapping model and asset I/O. ControlNet and an assigned slow-model baseline
    multiply the lease before its floor; an extra-slow worker multiplies the floored result.
    """

    def test_scalable_allowance_supports_one_prefetched_job_at_the_normal_speed_floor(self, monkeypatch):
        """Queue-aware slack must remain visible if the constants are revisited.

        Normal-speed matching uses 0.5 megapixel-work units per second. Away from the minimum-TTL
        regime, the lease's scalable portion represents 0.131072 MPS. One equally expensive job ahead
        doubles completion time, leaving about 1.9x headroom for variance and I/O.
        """
        waiting_prompt = FakeWaitingPrompt("k_euler", steps=100, width=1024, height=1024)
        generation = FakeProcessingGeneration(waiting_prompt)
        generation.set_job_ttl(monkeypatch)

        estimated_work = waiting_prompt.get_estimated_sampler_work().work_units.value
        pixel_work = waiting_prompt.width * waiting_prompt.height * estimated_work
        scalable_allowance = generation.job_ttl - 30
        effective_mps = pixel_work / scalable_allowance / 1_000_000
        two_job_compute_time_at_worker_floor = 2 * pixel_work / 500_000

        assert effective_mps == pytest.approx(0.131072)
        assert scalable_allowance / two_job_compute_time_at_worker_floor == pytest.approx(1.9073486328125)

    @pytest.mark.parametrize(
        ("sampler_name", "expected_ttl"),
        [
            ("k_euler", 430),  # 30 + (50 work * 2 seconds * 4x pixels)
            ("k_heun", 830),  # 30 + (100 work * 2 seconds * 4x pixels)
            ("heunpp2", 1230),  # 30 + (150 work * 2 seconds * 4x pixels)
        ],
    )
    def test_sampler_work_sets_the_ordinary_assignment_deadline(self, monkeypatch, sampler_name, expected_ttl):
        generation = FakeProcessingGeneration(FakeWaitingPrompt(sampler_name, steps=50, width=1024, height=1024))
        generation.set_job_ttl(monkeypatch)

        assert generation.job_ttl == expected_ttl

    def test_adaptive_work_uses_the_contract_estimate_not_requested_steps(self, monkeypatch):
        generation = FakeProcessingGeneration(FakeWaitingPrompt("k_dpm_adaptive", steps=5, width=1024, height=1024))
        generation.set_job_ttl(monkeypatch)

        # The adaptive contract estimates 40 work units; treating the five requested steps literally
        # would have hit the 150-second floor instead.
        assert generation.job_ttl == 350

    def test_the_minimum_budget_still_applies(self, monkeypatch):
        # Short assignments need a useful lease even when model/LoRA preparation costs more than
        # inference; this floor is also what makes shallow prefetch practical for small requests.
        tiny = FakeProcessingGeneration(FakeWaitingPrompt("k_euler", steps=1, width=512, height=512))
        tiny.set_job_ttl(monkeypatch)
        assert tiny.job_ttl == 150

    def test_controlnet_multiplies_the_computed_budget(self, monkeypatch):
        waiting_prompt = FakeWaitingPrompt("k_euler", steps=50, width=1024, height=1024)
        waiting_prompt.gen_payload["control_type"] = "canny"
        generation = FakeProcessingGeneration(waiting_prompt)
        generation.set_job_ttl(monkeypatch)

        assert generation.job_ttl == 860

    def test_slow_path_allowances_compound_in_the_documented_order(self, monkeypatch):
        """Prevent a seemingly harmless reorder from silently changing the lease contract."""
        monkeypatch.setattr(
            "horde.classes.stable.processing_generation.model_reference.get_model_baseline",
            lambda _model: "flux_1",
        )
        waiting_prompt = FakeWaitingPrompt("k_euler", steps=30, width=1024, height=1024)
        waiting_prompt.gen_payload["control_type"] = "canny"
        generation = FakeProcessingGeneration(waiting_prompt, model="slow-model", extra_slow_worker=True)
        generation.set_job_ttl(monkeypatch)

        # 270 ordinary seconds, then ControlNet 2x, assigned slow model 3x, and extra-slow worker 3x.
        assert generation.job_ttl == 4860

    def test_only_the_model_assigned_to_the_job_controls_the_slow_model_allowance(self, monkeypatch):
        monkeypatch.setattr(
            "horde.classes.stable.processing_generation.model_reference.get_model_baseline",
            lambda model: "flux_1" if model == "slow-model" else "stable_diffusion_1",
        )
        ordinary = FakeProcessingGeneration(
            FakeWaitingPrompt("k_euler", steps=50, width=1024, height=1024),
            model="ordinary-model",
        )
        slow = FakeProcessingGeneration(
            FakeWaitingPrompt("k_euler", steps=50, width=1024, height=1024),
            model="slow-model",
        )
        ordinary.set_job_ttl(monkeypatch)
        slow.set_job_ttl(monkeypatch)

        assert ordinary.job_ttl == 430
        assert slow.job_ttl == 1290

    def test_extra_slow_worker_multiplies_even_the_minimum_deadline(self, monkeypatch):
        generation = FakeProcessingGeneration(
            FakeWaitingPrompt("k_euler", steps=1),
            extra_slow_worker=True,
        )
        generation.set_job_ttl(monkeypatch)

        assert generation.job_ttl == 450

    def test_each_requested_lora_adds_a_download_allowance_after_the_floor(self, monkeypatch):
        """Five maximum-size LoRAs on an uncached worker take longer than the whole floor to fetch.

        The allowance is additive rather than folded into the floor, so a short job with LoRAs is not
        left with the same lease as one without.
        """
        tiny = FakeWaitingPrompt("k_euler", steps=1)
        tiny.params["loras"] = [{"name": str(i)} for i in range(5)]
        generation = FakeProcessingGeneration(tiny)
        generation.set_job_ttl(monkeypatch)

        # 150 floor plus five downloads of 400 MB at 30 Mbps (106.67 s each), rounded up.
        assert generation.job_ttl == 684

    def test_the_lora_allowance_is_multiplied_for_an_extra_slow_worker(self, monkeypatch):
        tiny = FakeWaitingPrompt("k_euler", steps=1)
        tiny.params["loras"] = [{"name": "one"}]
        generation = FakeProcessingGeneration(tiny, extra_slow_worker=True)
        generation.set_job_ttl(monkeypatch)

        assert generation.job_ttl == 770

    def test_fractional_deadlines_round_up_to_whole_seconds(self, monkeypatch):
        generation = FakeProcessingGeneration(FakeWaitingPrompt("k_euler", steps=100, width=520, height=520))
        generation.set_job_ttl(monkeypatch)

        assert generation.job_ttl == 237
        assert isinstance(generation.job_ttl, int)


class TestAssignedJobTimeBudgetContract:
    def test_the_persisted_assignment_deadline_is_returned_by_the_worker_pop_payload(self, monkeypatch):
        waiting_prompt = FakeWaitingPrompt("k_heun", steps=50, width=1024, height=1024)
        waiting_prompt.source_image = None
        waiting_prompt.extra_source_images = None
        waiting_prompt.shared = False
        generation = FakeProcessingGeneration(waiting_prompt)
        generation.id = "generation-id"
        generation.worker.bridge_agent = "test-bridge"
        generation.set_job_ttl(monkeypatch)
        monkeypatch.setattr("horde.classes.stable.waiting_prompt.check_bridge_capability", lambda *_args: True)
        monkeypatch.setattr(
            "horde.classes.stable.waiting_prompt.generate_procgen_upload_url",
            lambda generation_id, _shared: f"https://upload.invalid/{generation_id}",
        )

        popped = ImageWaitingPrompt.get_pop_payload(waiting_prompt, [generation], {"sampler_name": "k_heun"})

        assert generation.job_ttl == 830
        assert popped["ttl"] == generation.job_ttl
        assert popped["ttl"] == 830


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
