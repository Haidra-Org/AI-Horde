# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pricing-model invariants for ``horde.classes.stable.kudos.KudosModel``.

The kudos pricer is a small frozen MLP loaded from NumPy weights converted from
``kudos-v21-206.ckpt``. Two failure modes we want CI to catch loudly:

1. **Silent model drift**: someone swaps the .npz file, or the
   feature ordering in ``payload_to_vector`` changes. Either silently shifts
   kudos pricing for every job.
2. **Post-inference arithmetic regression**: ``basis_adjustment`` and
   ``basis_scale`` are pure arithmetic on the model output. Easy to break
   without noticing.

Strategy: design-intent invariants (BASIS_PAYLOAD ≈ KUDOS_BASIS, monotonicity)
+ exact arithmetic on the post-inference math. Specific golden floats are
recorded inline below; if the model is intentionally retrained, regenerate
them via ``python -m horde.classes.stable.kudos <npz>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from horde.classes.stable.kudos import KudosModel

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def kudos_model() -> KudosModel:
    """Load the singleton ``KudosModel`` once for the module."""
    # Import inside the fixture so the module-level KudosModel() in
    # horde.classes.stable.kudos doesn't fire during collection-only runs.
    from horde.classes.stable.kudos import KudosModel

    return KudosModel()


@pytest.fixture
def basis_payload() -> dict[str, Any]:
    """Return a fresh copy of BASIS_PAYLOAD per test (KudosModel mutates payloads in place via .get)."""
    from horde.classes.stable.kudos import KudosModel

    return dict(KudosModel.BASIS_PAYLOAD)


class TestModelLoad:
    """Constructing the model yields the shared singleton with a computed time basis."""

    def test_singleton_returned_on_repeat_construction(self, kudos_model: KudosModel) -> None:
        """Repeated construction returns the same singleton instance."""
        from horde.classes.stable.kudos import KudosModel

        assert KudosModel() is kudos_model
        assert KudosModel() is KudosModel()

    def test_time_basis_was_calculated(self, kudos_model: KudosModel) -> None:
        """The singleton's time basis is computed during construction."""
        # calculate_basis_time runs in __init__; a zero here means the
        # singleton init didn't complete.
        assert kudos_model.time_basis > 0


class TestDesignIntent:
    """The model should approximately honour its design contract."""

    def test_golden_payload_outputs_match_converted_checkpoint(
        self,
        kudos_model: KudosModel,
        basis_payload: dict[str, Any],
    ) -> None:
        """Representative payloads price to the golden times and kudos of the converted checkpoint."""
        payloads = {
            "basis": basis_payload,
            "large": dict(basis_payload, width=1024, height=1024),
            "control": dict(
                basis_payload,
                width=768,
                height=512,
                steps=30,
                cfg_scale=8.5,
                source_image=True,
                source_processing="img2img",
                control_type="canny",
                control_strength=0.72,
                denoising_strength=0.42,
                sampler_name="k_dpmpp_2m",
            ),
            "post": dict(
                basis_payload,
                steps=35,
                post_processing=["GFPGAN", "RealESRGAN_x4plus"],
                sampler_name="uni_pc",
            ),
        }

        expected = {
            "basis": (6.45, 11.0),
            "large": (17.02, 29.03),
            "control": (8.87, 15.13),
            "post": (14.93, 25.46),
        }

        for name, payload in payloads.items():
            expected_time, expected_kudos = expected[name]
            assert kudos_model.payload_to_time(payload) == expected_time
            assert kudos_model.calculate_kudos(payload) == expected_kudos

    def test_basis_payload_is_close_to_kudos_basis(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """A 50-step 512×512 generation costs approximately KUDOS_BASIS (10) kudos.

        This is the model's documented design intent (see ``KudosModel.KUDOS_BASIS``
        and the ``BASIS_PAYLOAD`` docstring). A wide tolerance is fine - we're
        catching gross drift, not precision regressions.
        """
        kudos = kudos_model.calculate_kudos(basis_payload)
        assert kudos == pytest.approx(10.0, abs=1.0), (
            f"Basis payload should price near KUDOS_BASIS=10.0; got {kudos}. "
            f"Probable cause: checkpoint swap or feature-ordering change in "
            f"payload_to_vector."
        )

    def test_doubling_steps_roughly_scales_kudos(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """Doubling the step count roughly scales kudos within a 1.4-2.5x band."""
        baseline = kudos_model.calculate_kudos(basis_payload)
        doubled = dict(basis_payload, steps=basis_payload["steps"] * 2)
        doubled_kudos = kudos_model.calculate_kudos(doubled)
        # Diffusion sampling is roughly linear in step count; allow a wide
        # band (×1.4 .. ×2.5) so this catches direction-of-change failures
        # without being brittle to model-specific non-linearities.
        ratio = doubled_kudos / baseline
        assert 1.4 <= ratio <= 2.5, f"Doubling steps changed kudos by ratio {ratio:.2f}; expected 1.4..2.5"

    def test_doubling_resolution_increases_kudos(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """Quadrupling the pixel count strictly increases kudos."""
        baseline = kudos_model.calculate_kudos(basis_payload)
        bigger = dict(basis_payload, width=1024, height=1024)
        bigger_kudos = kudos_model.calculate_kudos(bigger)
        # Strictly greater, pixel count quadrupled.
        assert bigger_kudos > baseline, f"1024×1024 ({bigger_kudos}) should cost more than 512×512 ({baseline})"


class TestPostInferenceArithmetic:
    """``basis_adjustment`` and ``basis_scale`` are deterministic arithmetic.

    The implementation is::

        kudos = (KUDOS_BASIS + basis_adjustment) * basis_scale * job_ratio

    Note ``basis_adjustment`` defaults to **1**, not 0 - these tests pass it
    explicitly so the arithmetic relationships are unambiguous.
    """

    def test_basis_adjustment_adds_before_scaling(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """``basis_adjustment`` is added to KUDOS_BASIS before scaling."""
        # Clean baseline: adjustment=0 isolates the model's job_ratio * 10 path.
        baseline_unadjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        adjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=5)
        # adjusted / baseline_unadjusted == (10+5)/10 == 1.5
        assert adjusted == pytest.approx(baseline_unadjusted * 1.5, rel=0.01)

    def test_basis_scale_multiplies(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """``basis_scale`` multiplies the post-adjustment kudos."""
        baseline = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        scaled = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0, basis_scale=1.25)
        assert scaled == pytest.approx(baseline * 1.25, rel=0.01)

    def test_zero_scale_zeroes_kudos(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """A zero ``basis_scale`` zeroes the kudos price."""
        assert kudos_model.calculate_kudos(basis_payload, basis_scale=0) == 0.0

    def test_combined_adjustment_and_scale(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """Adjustment and scale compose as ``(basis + adjustment) * scale``."""
        baseline_unadjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        # (10 + 5) * 1.25 / 10 == 1.875
        combined = kudos_model.calculate_kudos(basis_payload, basis_adjustment=5, basis_scale=1.25)
        assert combined == pytest.approx(baseline_unadjusted * 1.875, rel=0.01)

    def test_default_adjustment_is_one(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """The default ``basis_adjustment`` of 1 returns 110% of the model's raw output.

        Locked in here because changing the default would silently re-price every
        job in the system.
        """
        unadjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        defaulted = kudos_model.calculate_kudos(basis_payload)
        assert defaulted == pytest.approx(unadjusted * 11.0 / 10.0, rel=0.01)


class TestUnknownInputsHandled:
    """Unknown samplers / control types should not crash, they get sane defaults."""

    def test_unknown_sampler_falls_back_to_k_euler(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """An unknown sampler name falls back to a default and still prices positively."""
        unknown = dict(basis_payload, sampler_name="not_a_real_sampler_xyz")
        # Should not raise, and should produce a finite kudos value.
        kudos = kudos_model.calculate_kudos(unknown)
        assert kudos > 0

    def test_remix_source_processing_treated_as_img2img(self, kudos_model: KudosModel, basis_payload: dict[str, Any]) -> None:
        """A ``remix`` source_processing prices identically to ``img2img``."""
        # See the "Little hack until new model is out" comment in
        # payload_to_vector: source_processing="remix" is mapped to "img2img".
        remix = dict(basis_payload, source_processing="remix", source_image=True)
        img2img = dict(basis_payload, source_processing="img2img", source_image=True)
        # Same arithmetic path → same kudos.
        assert kudos_model.calculate_kudos(remix) == kudos_model.calculate_kudos(img2img)


class TestControlTypeCanonicalMapping:
    """The kudos checkpoint only has one-hot slots for the classic control types.

    Unified control types are collapsed onto their closest classic slot before
    the lookup so the trained input vector keeps its length. These tests lock the
    vector-length invariant and the specific slot each new type lands on.

    Comparisons use ``(a == b).all()`` rather than a framework-specific equality
    so they hold whether the feature vector is a torch tensor or a numpy array,
    and ``payload_to_tensor`` is the compatibility alias both backends keep.
    """

    def test_new_type_preserves_vector_length(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        classic = KudosModel.payload_to_tensor(dict(basis_payload, control_type="canny"))
        new_type = KudosModel.payload_to_tensor(dict(basis_payload, control_type="lineart"))
        # A changed width here means a new slot leaked into the one-hot, which the
        # frozen checkpoint cannot consume.
        assert new_type.shape == classic.shape

    def test_lineart_family_lands_on_hed_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        for lineart_type in ("lineart", "teed", "pidinet", "standard_lineart"):
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, control_type=lineart_type))
            hed = KudosModel.payload_to_tensor(dict(basis_payload, control_type="hed"))
            assert bool((mapped == hed).all()), lineart_type

    def test_cheap_preprocessors_land_on_canny_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        for canny_type in ("pyracanny", "tile", "recolor_luminance", "shuffle"):
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, control_type=canny_type))
            canny = KudosModel.payload_to_tensor(dict(basis_payload, control_type="canny"))
            assert bool((mapped == canny).all()), canny_type

    def test_depth_family_lands_on_depth_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        for depth_type in ("midas_depth", "zoe_depth", "depth_anything_v2"):
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, control_type=depth_type))
            depth = KudosModel.payload_to_tensor(dict(basis_payload, control_type="depth"))
            assert bool((mapped == depth).all()), depth_type

    def test_mlsd_maps_to_legacy_hough_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        mlsd = KudosModel.payload_to_tensor(dict(basis_payload, control_type="mlsd"))
        hough = KudosModel.payload_to_tensor(dict(basis_payload, control_type="hough"))
        assert bool((mlsd == hough).all())

    def test_unknown_type_falls_back_to_none_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        # An unmapped, unknown type must not raise on the one-hot index lookup.
        unknown = KudosModel.payload_to_tensor(dict(basis_payload, control_type="not_a_real_detector_xyz"))
        neutral = KudosModel.payload_to_tensor(dict(basis_payload, control_type="None"))
        assert bool((unknown == neutral).all())

    def test_new_type_prices_without_crashing(self, kudos_model, basis_payload):
        priced = kudos_model.calculate_kudos(dict(basis_payload, control_type="teed"))
        assert priced > 0


class TestSamplerCanonicalMapping:
    """The same frozen one-hot problem applies to ``sampler_name``.

    Extended solvers are collapsed onto the trained slot with the same cost per
    step. These tests lock the vector width and the slot each name lands on, and
    guard the two samplers that already own trained slots against being remapped.
    """

    def test_extended_sampler_preserves_vector_length(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        classic = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_euler"))
        extended = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="deis"))
        assert extended.shape == classic.shape

    def test_multistep_solvers_land_on_dpmpp_2m_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        reference = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_dpmpp_2m"))
        for sampler in ("deis", "ipndm", "res_multistep", "sa_solver", "dpmpp_2m_sde", "dpmpp_3m_sde"):
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=sampler))
            assert bool((mapped == reference).all()), sampler

    def test_first_order_solvers_land_on_euler_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        reference = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_euler"))
        for sampler in ("ddpm", "gradient_estimation", "er_sde"):
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=sampler))
            assert bool((mapped == reference).all()), sampler

    def test_heunpp2_lands_on_heun_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="heunpp2"))
        heun = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_heun"))
        assert bool((mapped == heun).all())

    def test_unipc_keeps_its_own_trained_slots(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        # These are in the trained vocabulary already, so they must not be collapsed onto anything.
        euler = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_euler"))
        for sampler in ("uni_pc", "uni_pc_bh2"):
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=sampler))
            assert not bool((mapped == euler).all()), sampler

    def test_unipc_variants_are_distinct_from_each_other(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        first = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="uni_pc"))
        second = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="uni_pc_bh2"))
        assert not bool((first == second).all())

    def test_uppercase_ddim_reaches_its_trained_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        # The API accepts `DDIM`; the model was trained on `ddim`. Before the canonical map the
        # uppercase spelling missed its own slot and was priced as the euler fallback.
        upper = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="DDIM"))
        lower = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="ddim"))
        euler = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_euler"))
        assert bool((upper == lower).all())
        assert not bool((upper == euler).all())

    def test_dpmsolver_prices_as_what_the_backend_runs(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="dpmsolver"))
        reference = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_dpmpp_2m"))
        assert bool((mapped == reference).all())

    def test_unknown_sampler_falls_back_to_euler(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        unknown = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="not_a_real_sampler_xyz"))
        euler = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_euler"))
        assert bool((unknown == euler).all())

    def test_every_accepted_sampler_prices_without_crashing(self, kudos_model, basis_payload):
        from horde.consts import KNOWN_SAMPLERS

        for sampler in KNOWN_SAMPLERS:
            priced = kudos_model.calculate_kudos(dict(basis_payload, sampler_name=sampler))
            assert priced > 0, sampler


class TestSchedulerPricing:
    """The trained vector has one karras on/off float and no schedule slot.

    The requirement is continuity: introducing the field must not reprice any request that was already
    being served. Every schedule therefore lands on whichever side of that float its legacy equivalent
    did, which also means the field is not a pricing lever a requester can pull.
    """

    def test_karras_schedule_prices_as_the_legacy_flag_did(self, kudos_model, basis_payload):
        assert kudos_model.calculate_kudos(dict(basis_payload, scheduler="karras")) == kudos_model.calculate_kudos(
            dict(basis_payload, karras=True),
        )

    def test_normal_schedule_prices_as_karras_false_did(self, kudos_model, basis_payload):
        assert kudos_model.calculate_kudos(dict(basis_payload, scheduler="normal")) == kudos_model.calculate_kudos(
            dict(basis_payload, karras=False),
        )

    def test_extended_schedules_price_on_the_non_karras_side(self, kudos_model, basis_payload):
        # The model only ever saw `karras` as the feature being on, so everything else takes the off side
        # rather than being guessed onto it.
        reference = kudos_model.calculate_kudos(dict(basis_payload, karras=False))
        for schedule in ("simple", "sgm_uniform", "exponential", "ddim_uniform", "beta", "linear_quadratic", "kl_optimal"):
            assert kudos_model.calculate_kudos(dict(basis_payload, scheduler=schedule)) == reference, schedule

    def test_field_overrides_the_flag_for_pricing_too(self, kudos_model, basis_payload):
        # Otherwise a request could be priced on one schedule and rendered on another.
        priced = kudos_model.calculate_kudos(dict(basis_payload, scheduler="karras", karras=False))
        assert priced == kudos_model.calculate_kudos(dict(basis_payload, karras=True))

    def test_every_known_schedule_prices_without_crashing(self, kudos_model, basis_payload):
        from horde.consts import KNOWN_SCHEDULERS

        for schedule in KNOWN_SCHEDULERS:
            assert kudos_model.calculate_kudos(dict(basis_payload, scheduler=schedule)) > 0, schedule


class TestSolverKnobSamplerSlotting:
    """The solver-knob tier has no trained slots, so each name is collapsed onto one that has.

    The grouping is by marginal sampler-work rate, read from horde_sdk, and then by whether the solver is
    deterministic or stochastic. A sampler landing on the wrong slot is priced as the wrong workload, and
    nothing else in the system would notice.
    """

    def test_one_evaluation_solvers_land_on_their_uncorrected_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        expected_slots = {
            "euler_cfg_pp": "k_euler",
            "euler_ancestral_cfg_pp": "k_euler_a",
            "dpmpp_2m_cfg_pp": "k_dpmpp_2m",
            "dpmpp_2m_sde_heun": "k_dpmpp_2m",
            "ipndm_v": "k_dpmpp_2m",
            "res_multistep_cfg_pp": "k_dpmpp_2m",
            "res_multistep_ancestral": "k_dpmpp_2m",
            "res_multistep_ancestral_cfg_pp": "k_dpmpp_2m",
            "gradient_estimation_cfg_pp": "k_euler",
        }
        for sampler, slot in expected_slots.items():
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=sampler))
            reference = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=slot))
            assert bool((mapped == reference).all()), sampler

    def test_two_evaluation_solvers_land_on_a_second_order_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        expected_slots = {
            "exp_heun_2_x0": "k_heun",
            "exp_heun_2_x0_sde": "k_dpmpp_sde",
            "dpmpp_2s_ancestral_cfg_pp": "k_dpmpp_2s_a",
            "seeds_2": "k_dpmpp_sde",
            "sa_solver_pece": "k_dpmpp_sde",
        }
        for sampler, slot in expected_slots.items():
            mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=slot))
            reference = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name=sampler))
            assert bool((mapped == reference).all()), sampler

    def test_three_evaluation_solver_lands_on_the_heun_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import KudosModel

        mapped = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="seeds_3"))
        heun = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="k_heun"))
        assert bool((mapped == heun).all())

    def test_pece_costs_more_than_the_plain_sa_solver(self, kudos_model, basis_payload):
        # The corrector is a second model evaluation, so the two must not share a slot.
        from horde.classes.stable.kudos import KudosModel

        plain = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="sa_solver"))
        pece = KudosModel.payload_to_tensor(dict(basis_payload, sampler_name="sa_solver_pece"))
        assert not bool((plain == pece).all())

    def test_every_solver_knob_sampler_has_a_slot(self, kudos_model, basis_payload):
        from horde.classes.stable.kudos import CANONICAL_KUDOS_SAMPLERS, KudosModel
        from horde.consts import SOLVER_KNOB_SAMPLERS

        for sampler in SOLVER_KNOB_SAMPLERS:
            assert sampler in CANONICAL_KUDOS_SAMPLERS, f"'{sampler}' would silently price as the euler fallback"
            assert CANONICAL_KUDOS_SAMPLERS[sampler] in KudosModel.KNOWN_SAMPLERS, sampler

    def test_slotting_matches_the_shared_work_rates_where_a_slot_exists(self, kudos_model, basis_payload):
        # The slot a sampler takes has to cost what the sampler costs, or the grouping is decorative.
        # The exception is the three-evaluation solvers: the trained vocabulary tops out at two
        # evaluations per step, so they take the most expensive slot available and are under-priced by
        # roughly a third. That predates this tier (heunpp2 has always sat there) and cannot be fixed
        # without retraining, which is why it is asserted rather than left to be discovered.
        from horde_sdk.generation_parameters.image.consts import KNOWN_IMAGE_SAMPLERS
        from horde_sdk.generation_parameters.image.sampler_work import (
            FixedRateSamplerWorkProfile,
            get_sampler_work_profile,
        )

        from horde.classes.stable.kudos import CANONICAL_KUDOS_SAMPLERS, KudosModel
        from horde.consts import SOLVER_KNOB_SAMPLERS

        def fixed_rate(sampler_name):
            legacy_aliases = {
                "ddim": KNOWN_IMAGE_SAMPLERS.DDIM,
                "plms": KNOWN_IMAGE_SAMPLERS.k_lms,
            }
            sdk_sampler = legacy_aliases[sampler_name] if sampler_name in legacy_aliases else KNOWN_IMAGE_SAMPLERS(sampler_name)
            profile = get_sampler_work_profile(sdk_sampler)
            assert isinstance(profile, FixedRateSamplerWorkProfile)
            return profile.marginal_work_units_per_trajectory_step

        fixed_trained_slots = [slot for slot in KudosModel.KNOWN_SAMPLERS if slot != "k_dpm_adaptive"]
        most_expensive_trained_slot = max(fixed_rate(slot) for slot in fixed_trained_slots)

        for sampler in SOLVER_KNOB_SAMPLERS:
            if sampler == "k_dpm_adaptive":
                continue
            slot = CANONICAL_KUDOS_SAMPLERS[sampler]
            sampler_cost = fixed_rate(sampler)
            slot_cost = fixed_rate(slot)
            expected_cost = min(sampler_cost, most_expensive_trained_slot)
            assert slot_cost == expected_cost, (
                f"'{sampler}' has work rate {sampler_cost} but is priced as '{slot}', which has rate {slot_cost}"
            )

    def test_the_trained_vocabulary_cannot_express_a_three_unit_work_rate(self, kudos_model, basis_payload):
        # Stated outright so the under-pricing above is a known quantity rather than an accident: if a
        # retrained checkpoint ever adds a costlier slot, this fails and the slotting should be revisited.
        from horde_sdk.generation_parameters.image.consts import KNOWN_IMAGE_SAMPLERS
        from horde_sdk.generation_parameters.image.sampler_work import (
            FixedRateSamplerWorkProfile,
            get_sampler_work_profile,
        )

        from horde.classes.stable.kudos import KudosModel

        rates = []
        for slot in KudosModel.KNOWN_SAMPLERS:
            legacy_aliases = {
                "ddim": KNOWN_IMAGE_SAMPLERS.DDIM,
                "plms": KNOWN_IMAGE_SAMPLERS.k_lms,
            }
            sdk_sampler = legacy_aliases[slot] if slot in legacy_aliases else KNOWN_IMAGE_SAMPLERS(slot)
            profile = get_sampler_work_profile(sdk_sampler)
            if isinstance(profile, FixedRateSamplerWorkProfile):
                rates.append(profile.marginal_work_units_per_trajectory_step)
        assert max(rates) == 2


class TestExistingRequestsAreNotRepriced:
    """Adding the new tier must leave every price that was already being charged exactly where it was.

    The samplers below all own trained slots or were already canonically mapped, so none of them touches
    the new entries. A change here means the wave repriced requests it was not supposed to touch.
    """

    def test_pre_existing_samplers_keep_their_exact_prices(self, kudos_model, basis_payload):
        # Measured from the pricing path with the solver-knob tier absent, then re-measured with it
        # present; every value below was identical across the two.
        expected_kudos = {
            "k_euler": 11.0,
            "k_euler_a": 10.98,
            "k_heun": 19.02,
            "k_dpm_2": 16.82,
            "k_dpm_2_a": 17.55,
            "k_dpm_fast": 11.48,
            "k_dpm_adaptive": 16.2,
            "k_dpmpp_2s_a": 16.41,
            "k_dpmpp_2m": 10.64,
            "k_dpmpp_sde": 19.53,
            "k_lms": 11.19,
            "dpmsolver": 10.64,
            "DDIM": 15.02,
            "lcm": 11.0,
            "uni_pc": 15.04,
            "uni_pc_bh2": 14.5,
            "dpmpp_2m_sde": 10.64,
            "dpmpp_3m_sde": 10.64,
            "ddpm": 11.0,
            "deis": 10.64,
            "ipndm": 10.64,
            "res_multistep": 10.64,
            "gradient_estimation": 11.0,
            "heunpp2": 19.02,
            "er_sde": 11.0,
            "sa_solver": 10.64,
        }
        for sampler, expected in expected_kudos.items():
            assert kudos_model.calculate_kudos(dict(basis_payload, sampler_name=sampler)) == expected, sampler


class TestControlStrengthPricing:
    """A request that leaves the guidance weight unset prices exactly as it did before the field existed.

    The pricer reads ``control_strength`` and falls back to ``denoising_strength`` when it is absent, so
    the API must leave the field out of the payload rather than writing the worker-side default of 1.0
    into it. Doing that would reprice every controlnet request that never asked for the setting.
    """

    @pytest.fixture
    def controlnet_payload(self, basis_payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(basis_payload)
        payload.update(
            {
                "source_image": True,
                "source_processing": "img2img",
                "control_type": "canny",
                "denoising_strength": 0.4,
            },
        )
        del payload["control_strength"]
        return payload

    def test_an_absent_field_prices_as_the_denoising_strength(
        self,
        kudos_model: KudosModel,
        controlnet_payload: dict[str, Any],
    ) -> None:
        absent = kudos_model.calculate_kudos(dict(controlnet_payload))
        matching = kudos_model.calculate_kudos({**controlnet_payload, "control_strength": 0.4})
        assert absent == matching

    def test_a_supplied_field_replaces_the_fallback(
        self,
        kudos_model: KudosModel,
        controlnet_payload: dict[str, Any],
    ) -> None:
        absent = kudos_model.calculate_kudos(dict(controlnet_payload))
        supplied = kudos_model.calculate_kudos({**controlnet_payload, "control_strength": 1.6})
        assert supplied != absent


class TestQrCodeWorkflowPricing:
    """The QR workflow weights a control map of its own, so its guidance weight has to reach the pricer.

    The workflow builds its control map from the extra texts rather than from a source image, so the
    payload names neither a ``source_image`` nor a ``control_type``. A request that leaves the weight
    unset still prices as it did before the field existed.
    """

    @pytest.fixture
    def qr_code_payload(self, basis_payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(basis_payload, workflow="qr_code")
        del payload["control_strength"]
        return payload

    def test_an_absent_field_prices_as_a_plain_generation(
        self,
        kudos_model: KudosModel,
        basis_payload: dict[str, Any],
        qr_code_payload: dict[str, Any],
    ) -> None:
        absent = kudos_model.calculate_kudos(dict(qr_code_payload))
        assert absent == kudos_model.calculate_kudos(dict(basis_payload))

    def test_a_supplied_field_changes_the_price(
        self,
        kudos_model: KudosModel,
        qr_code_payload: dict[str, Any],
    ) -> None:
        absent = kudos_model.calculate_kudos(dict(qr_code_payload))
        supplied = kudos_model.calculate_kudos({**qr_code_payload, "control_strength": 1.6})
        assert supplied != absent
