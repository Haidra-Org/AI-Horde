# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Request-time enforcement of the shared sampler constraints, and the document that publishes them.

The rules come from horde_sdk, which reads them from the image backend's own solver implementations.
They are refused rather than adjusted because each is a case where the backend would otherwise produce
something the request did not ask for: a knob a solver does not declare is dropped in silence, a
schedule with no sigma table for the model cannot be built, and one sampler/schedule pairing returns
colour noise at every step count. A regression here is not a loud failure, which is the whole reason
these are checked before a job is ever queued.
"""

from __future__ import annotations

import json

import pytest
from horde_sdk.generation_parameters.image.constraints_document import (
    PublishedSamplerRecord,
    SamplerConstraintsDocument,
)
from horde_sdk.generation_parameters.image.consts import KNOWN_IMAGE_SAMPLERS
from horde_sdk.generation_parameters.image.sampler_work import (
    AdaptiveSamplerWorkProfile,
    FixedRateSamplerWorkProfile,
    get_sampler_work_profile,
)

from horde import exceptions as e
from horde.apis.models.stable_v2 import inline_json_schema_definitions
from horde.consts import (
    KNOWN_SAMPLERS,
    KNOWN_SCHEDULERS,
    SOLVER_KNOB_SAMPLERS,
    baseline_for_constraints,
)
from horde.sampler_constraints import compile_sampler_constraints
from horde.validation import ParamValidator

pytestmark = pytest.mark.unit

# A model name the reference resolves to a Flux baseline without needing the reference to be populated.
FLUX_MODEL = "SomeModel [Flux]"
# Resolves to the default baseline, which is the SD1 family.
SD1_MODEL = "Deliberate"


def validate(params, models=None):
    """Run the sampler constraint check and return the validator, or raise as the API would."""
    validator = ParamValidator(prompt="a prompt", models=models or [SD1_MODEL], params=params, user=None)
    validator.validate_sampler_constraints()
    return validator


def rejection_code(params, models=None):
    """Return the return code the constraint check rejects these params with, or None if it accepts."""
    try:
        validate(params, models)
    except e.BadRequest as rejected:
        return rejected.rc
    return None


class TestKnobApplicability:
    def test_a_knob_the_solver_does_not_declare_is_rejected(self):
        # k_euler takes a churn window, not an eta. The backend would drop the eta without saying so.
        assert rejection_code({"sampler_name": "k_euler", "sampler_eta": 0.5}) == "SamplerKnobInapplicable"

    def test_the_same_knob_is_accepted_where_the_solver_declares_it(self):
        assert rejection_code({"sampler_name": "k_euler_a", "sampler_eta": 0.5}) is None

    def test_a_solver_taking_no_knobs_at_all_rejects_every_one(self):
        # dpm_fast is wrapped by the backend in a closure that forwards no options whatsoever.
        for field_name in ("sampler_eta", "sampler_s_noise", "sampler_s_churn", "sampler_order"):
            assert rejection_code({"sampler_name": "k_dpm_fast", field_name: 1}) == "SamplerKnobInapplicable", field_name

    def test_unset_knobs_are_never_a_violation(self):
        # Leaving a knob out means the solver's own default applies, which is always renderable.
        for sampler in KNOWN_SAMPLERS:
            assert rejection_code({"sampler_name": sampler}) is None, sampler

    def test_an_explicit_none_is_treated_as_unset(self):
        # The request models default every knob to None, so an unset knob arrives as a present null.
        assert rejection_code({"sampler_name": "k_euler", "sampler_eta": None}) is None


class TestKnobRanges:
    def test_a_value_above_the_samplers_range_is_rejected(self):
        assert rejection_code({"sampler_name": "k_dpm_adaptive", "sampler_order": 4}) == "SamplerKnobOutOfRange"

    def test_a_value_inside_the_samplers_range_is_accepted(self):
        assert rejection_code({"sampler_name": "k_dpm_adaptive", "sampler_order": 3}) is None

    def test_the_multistep_solvers_accept_an_order(self):
        # These spell it `max_order` in the backend, which is the same concept as `order`. Reading the
        # spelling difference as an absence refused a setting that genuinely reaches them.
        for sampler in ("deis", "ipndm", "ipndm_v"):
            assert rejection_code({"sampler_name": sampler, "sampler_order": 3}) is None, sampler

    def test_the_multistep_order_floor_is_enforced(self):
        # At 1 these index their history buffer before anything is in it, raising inside the sampling loop.
        for sampler in ("deis", "ipndm", "ipndm_v"):
            assert rejection_code({"sampler_name": sampler, "sampler_order": 1}) == "SamplerKnobOutOfRange", sampler

    def test_the_multistep_order_ceiling_is_enforced(self):
        for sampler in ("deis", "ipndm", "ipndm_v"):
            assert rejection_code({"sampler_name": sampler, "sampler_order": 5}) == "SamplerKnobOutOfRange", sampler

    def test_the_sa_solver_family_still_refuses_an_order(self):
        # Its predictor and corrector orders are a different concept, not another spelling of this one.
        for sampler in ("sa_solver", "sa_solver_pece"):
            assert rejection_code({"sampler_name": sampler, "sampler_order": 3}) == "SamplerKnobInapplicable", sampler

    def test_res_multistep_still_refuses_an_order(self):
        # Despite the name, it takes no order argument of either spelling.
        params = {"sampler_name": "res_multistep", "sampler_order": 3}
        assert rejection_code(params) == "SamplerKnobInapplicable"

    def test_the_range_is_per_sampler_rather_than_global(self):
        # The same order is legal on one solver and not on another, which is why a single global clamp
        # cannot express this.
        assert rejection_code({"sampler_name": "k_lms", "sampler_order": 40}) is None
        assert rejection_code({"sampler_name": "k_dpm_adaptive", "sampler_order": 40}) == "SamplerKnobOutOfRange"

    def test_a_fractional_value_is_rejected_for_an_integer_knob(self):
        assert rejection_code({"sampler_name": "k_lms", "sampler_order": 2.5}) == "SamplerKnobOutOfRange"


class TestSolverType:
    def test_a_foreign_solver_type_is_rejected(self):
        # phi_1 belongs to a different solver family; the backend would raise inside the render graph.
        params = {"sampler_name": "dpmpp_2m_sde", "sampler_solver_type": "phi_1"}
        assert rejection_code(params) == "SamplerSolverTypeUnsupported"

    def test_the_samplers_own_vocabulary_is_accepted(self):
        for solver_type in ("midpoint", "heun"):
            params = {"sampler_name": "dpmpp_2m_sde", "sampler_solver_type": solver_type}
            assert rejection_code(params) is None, solver_type

    def test_a_solver_type_on_a_sampler_that_has_none_is_rejected(self):
        params = {"sampler_name": "k_euler", "sampler_solver_type": "heun"}
        assert rejection_code(params) == "SamplerKnobInapplicable"


class TestSamplerSchedulerPairing:
    def test_the_divergent_pairing_is_rejected(self):
        params = {"sampler_name": "dpmpp_3m_sde", "scheduler": "normal"}
        assert rejection_code(params) == "SamplerSchedulerMismatch"

    def test_the_same_sampler_is_accepted_on_another_schedule(self):
        assert rejection_code({"sampler_name": "dpmpp_3m_sde", "scheduler": "karras"}) is None

    def test_the_pairing_is_caught_through_the_legacy_flag_too(self):
        # `karras: false` resolves to `normal`, so a request can reach the divergent pairing without ever
        # naming a schedule.
        params = {"sampler_name": "dpmpp_3m_sde", "karras": False}
        assert rejection_code(params) == "SamplerSchedulerMismatch"


class TestSchedulerBaselineApplicability:
    def test_a_sigma_generator_schedule_is_rejected_on_an_undefined_baseline(self):
        params = {"sampler_name": "k_euler", "scheduler": "align_your_steps"}
        assert rejection_code(params, models=[FLUX_MODEL]) == "SchedulerBaselineMismatch"

    def test_a_sigma_generator_schedule_is_accepted_on_a_defined_baseline(self):
        for schedule in ("align_your_steps", "gits"):
            params = {"sampler_name": "k_euler", "scheduler": schedule}
            assert rejection_code(params, models=[SD1_MODEL]) is None, schedule

    def test_an_ordinary_schedule_is_unrestricted_by_baseline(self):
        params = {"sampler_name": "k_euler", "scheduler": "karras"}
        assert rejection_code(params, models=[FLUX_MODEL]) is None

    def test_the_restriction_applies_to_every_requested_model(self):
        # A multi-model request can be dispatched for any of them, so all have to be renderable.
        params = {"sampler_name": "k_euler", "scheduler": "gits"}
        assert rejection_code(params, models=[SD1_MODEL, FLUX_MODEL]) == "SchedulerBaselineMismatch"

    def test_a_baseline_outside_the_shared_vocabulary_leaves_it_unenforced(self):
        # Rejecting a model over a spelling would strand it entirely; the check simply does not apply.
        assert baseline_for_constraints("something the reference has never heard of") is None


class TestFlowShift:
    @pytest.mark.parametrize("value", [0.0, 1.1, 100.0])
    def test_flow_shift_is_accepted_on_a_supported_baseline(self, value: float):
        assert rejection_code({"flow_shift": value}, models=[FLUX_MODEL]) is None

    @pytest.mark.parametrize("value", [-0.01, 100.01])
    def test_flow_shift_outside_the_backend_range_is_rejected(self, value: float):
        assert rejection_code({"flow_shift": value}, models=[FLUX_MODEL]) == "FlowShiftOutOfRange"

    def test_flow_shift_is_rejected_when_the_backend_graph_would_ignore_it(self):
        assert rejection_code({"flow_shift": 1.1}, models=[SD1_MODEL]) == "FlowShiftInapplicable"

    def test_every_model_in_a_multi_model_request_must_support_flow_shift(self):
        assert rejection_code({"flow_shift": 1.1}, models=[FLUX_MODEL, SD1_MODEL]) == "FlowShiftInapplicable"

    def test_an_unknown_baseline_fails_closed(self):
        assert rejection_code({"flow_shift": 1.1}, models=["unrecognized custom model"]) == "FlowShiftInapplicable"


class TestCfgPPAdvisory:
    def test_a_high_cfg_scale_warns_rather_than_rejecting(self):
        from horde.enums import WarningMessage

        validator = validate({"sampler_name": "euler_cfg_pp", "cfg_scale": 7.5})
        assert WarningMessage.CfgPPScaleTooLarge in validator.warnings

    def test_a_low_cfg_scale_does_not_warn(self):
        validator = validate({"sampler_name": "euler_cfg_pp", "cfg_scale": 1.5})
        assert validator.warnings == set()

    def test_a_non_cfg_pp_sampler_never_warns_about_it(self):
        from horde.enums import WarningMessage

        validator = validate({"sampler_name": "k_euler", "cfg_scale": 20.0})
        assert WarningMessage.CfgPPScaleTooLarge not in validator.warnings


def marginal_work_rate(sampler_name: str) -> int | None:
    """Return a fixed sampler's marginal work rate, or none for adaptive/unknown names."""
    try:
        profile = get_sampler_work_profile(KNOWN_IMAGE_SAMPLERS(sampler_name))
    except ValueError:
        return None
    if isinstance(profile, FixedRateSamplerWorkProfile):
        return profile.marginal_work_units_per_trajectory_step
    assert isinstance(profile, AdaptiveSamplerWorkProfile)
    return None


class TestWorkProfiles:
    def test_the_classic_second_order_samplers_have_two_work_units_per_step(self):
        for sampler in ("k_heun", "k_dpm_2", "k_dpm_2_a", "k_dpmpp_2s_a", "k_dpmpp_sde"):
            assert marginal_work_rate(sampler) == 2, sampler

    def test_the_three_evaluation_samplers_are_priced_as_such(self):
        for sampler in ("heunpp2", "seeds_3"):
            assert marginal_work_rate(sampler) == 3, sampler

    def test_multistep_solvers_cost_one_evaluation(self):
        for sampler in ("k_dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "deis", "ipndm", "res_multistep"):
            assert marginal_work_rate(sampler) == 1, sampler

    def test_an_unknown_sampler_is_priced_as_first_order(self):
        # Read for payloads already stored, whose sampler was validated when the request was made.
        assert marginal_work_rate("not_a_real_sampler_xyz") is None

    def test_every_accepted_sampler_has_a_count(self):
        for sampler in KNOWN_SAMPLERS:
            assert get_sampler_work_profile(KNOWN_IMAGE_SAMPLERS(sampler)) is not None


class TestPublishedConstraints:
    @pytest.fixture(scope="class")
    def document(self):
        # The compiler returns the typed model; this is the rendering of it the endpoint serves.
        return compile_sampler_constraints().model_dump(mode="json")

    def test_the_document_is_strict_json(self, document):
        # Infinity is not valid JSON, and an unbounded knob maximum is the obvious way to emit one.
        rendered = json.dumps(document, allow_nan=False)
        assert "Infinity" not in rendered

    def test_every_accepted_sampler_is_published(self, document):
        assert set(document["samplers"]) == set(KNOWN_SAMPLERS)

    def test_no_unrequestable_sampler_is_published(self, document):
        # Publishing a name the request models reject would have clients offering it.
        for sampler in document["samplers"]:
            assert sampler in KNOWN_SAMPLERS, sampler

    def test_each_sampler_carries_its_work_profile_and_vocabulary(self, document):
        for sampler, entry in document["samplers"].items():
            assert entry["name"] == sampler
            assert entry["work_profile"]["kind"] in {"fixed_rate", "adaptive"}
            assert isinstance(entry["accepted_settings"], dict)
            assert isinstance(entry["solver_type_choices"], list)

    def test_settings_are_published_under_their_request_field_names(self, document):
        # A client reads this to build a request, so the keys have to be the field names it will send.
        churn_settings = document["samplers"]["k_euler"]["accepted_settings"]
        assert "sampler_s_churn" in churn_settings
        assert "s_churn" not in churn_settings

    def test_an_unbounded_maximum_publishes_as_null(self, document):
        tmax = document["samplers"]["k_euler"]["accepted_settings"]["sampler_s_tmax"]
        assert tmax["maximum"] is None
        assert tmax["default"] is None

    def test_the_integer_only_knobs_are_flagged(self, document):
        assert document["samplers"]["k_lms"]["accepted_settings"]["sampler_order"]["integer_only"] is True

    def test_the_hard_constraints_mirror_the_rejections(self, document):
        hard = document["hard_constraints"]
        assert {"sampler": "dpmpp_3m_sde", "scheduler": "normal"} in hard["rejected_sampler_scheduler_pairings"]
        for schedule in ("align_your_steps", "gits"):
            assert set(hard["scheduler_baseline_applicability"][schedule]) == {
                "stable_diffusion_1",
                "stable_diffusion_xl",
            }

    def test_published_pairings_name_only_accepted_values(self, document):
        for pairing in document["hard_constraints"]["rejected_sampler_scheduler_pairings"]:
            assert pairing["sampler"] in KNOWN_SAMPLERS
            assert pairing["scheduler"] in KNOWN_SCHEDULERS

    def test_every_recommendation_states_its_provenance(self, document):
        # These range from the backend author's own statements to third-party folklore, and a client
        # cannot weigh them without knowing which is which.
        assert document["recommendations"]
        for recommendation in document["recommendations"]:
            assert recommendation["provenance"] in {"upstream_author", "community", "measured", "ai_horde_devs"}
            assert recommendation["source"]
            assert recommendation["summary"]

    def test_third_party_folklore_is_never_labelled_as_upstream(self, document):
        for recommendation in document["recommendations"]:
            if "comfyui.dev" in recommendation["source"]:
                assert recommendation["provenance"] == "community"

    def test_every_sampler_carries_a_presentation_tier(self, document):
        for sampler, entry in document["samplers"].items():
            assert entry["presentation_tier"] in {"recommended", "advanced"}, sampler

    def test_the_recommended_tier_is_published_and_agrees_per_sampler(self, document):
        published = set(document["presentation_tiers"]["recommended"])

        assert published == {
            "k_euler",
            "k_euler_a",
            "k_dpmpp_2m",
            "dpmpp_2m_sde",
            "k_dpmpp_sde",
            "lcm",
            "uni_pc",
            "DDIM",
        }
        for sampler, entry in document["samplers"].items():
            expected = "recommended" if sampler in published else "advanced"
            assert entry["presentation_tier"] == expected, sampler

    def test_the_tier_note_says_it_restricts_nothing(self, document):
        # A client must not read the tier as a capability difference.
        assert "accepted, priced and dispatched identically" in document["presentation_tiers"]["note"]

    def test_the_measured_cost_ratios_are_labelled_and_traceable(self, document):
        # Published as bare numbers they read like prices. They are evidence about cost, and a client
        # needs to be told what produced them and which field a request is actually charged on.
        work_accounting = document["work_accounting"]

        assert work_accounting["measured_cost_ratio_provenance"] == "measured"
        assert work_accounting["authoritative_field"] == "work_profile"
        assert work_accounting["measured_cost_ratio_source"].endswith(".json")
        assert "fixed-rate work profiles" in work_accounting["measured_cost_ratio_note"]

    def test_each_measured_figure_states_the_model_it_was_taken_on(self, document):
        # The two differ systematically, so a figure read without its resolution is misleading.
        work_accounting = document["work_accounting"]

        assert "1024x1024" in work_accounting["measured_cost_ratio_sdxl_note"]
        assert "512x512" in work_accounting["measured_cost_ratio_sd15_note"]
        assert "biases the one-evaluation samplers upwards" in work_accounting["measured_cost_ratio_sd15_note"]

    def test_both_measured_figures_are_served_for_every_sampler_that_has_them(self, document):
        for sampler, entry in document["samplers"].items():
            if sampler == "k_dpm_adaptive":
                continue

            assert entry["measured_cost_ratio_sd15"] > 0, sampler
            assert entry["measured_cost_ratio_sdxl"] > 0, sampler

    def test_the_adaptive_sampler_serves_no_measured_ratio(self, document):
        # It chooses its own step count, so a cost per requested step would be a number about nothing.
        adaptive = document["samplers"]["k_dpm_adaptive"]

        assert adaptive["measured_cost_ratio_sd15"] is None
        assert adaptive["measured_cost_ratio_sdxl"] is None

    def test_the_large_model_figures_agree_with_the_evaluation_counts(self, document):
        # This is the claim the measurement supports: the published evaluation families are real.
        for sampler, entry in document["samplers"].items():
            ratio = entry["measured_cost_ratio_sdxl"]
            if ratio is None:
                continue

            expected = entry["work_profile"]["marginal_work_units_per_trajectory_step"]
            assert abs(ratio - expected) <= 0.2 * expected, sampler

    def test_the_ruled_recommendations_are_served_as_ruled(self, document):
        ruled = [rec for rec in document["recommendations"] if rec["provenance"] == "ai_horde_devs"]

        assert ruled
        summaries = " ".join(rec["summary"] for rec in ruled)
        assert "karras is not the safe choice at low step counts" in summaries
        assert "align_your_steps and gits are recommended for low step counts" in summaries
        assert "CFG++" in summaries

    def test_the_cfg_pp_samplers_are_flagged_with_their_advisory(self, document):
        assert document["advisories"]["cfg_pp_advised_max_cfg_scale"] == 2.0
        for sampler in SOLVER_KNOB_SAMPLERS:
            expected = sampler.endswith("_cfg_pp")
            assert document["samplers"][sampler]["applies_cfg_pp"] is expected, sampler


class TestTheDocumentAndItsPublishedTypeStayInLockstep:
    """The document, its type in horde_sdk and its swagger schema are one definition rather than three.

    A python client parses the response back into the horde_sdk model and every other client builds
    against the swagger schema, so either drifting from what is served misdescribes this API. The
    compiler returning the model closes the first gap by construction; deriving the swagger schema from
    the same model closes the second, and these pin both.
    """

    @pytest.fixture(scope="class")
    def document(self):
        return compile_sampler_constraints().model_dump(mode="json")

    @pytest.fixture(scope="class")
    def swagger_schema(self):
        # Read off the live api namespace, which is the schema the served documentation actually shows.
        from horde.apis.v2.stable import models

        return models.response_model_sampler_constraints.__schema__

    def test_the_served_keys_are_exactly_the_models_fields(self, document):
        # An addition to either side that the other does not know about fails here rather than silently.
        assert set(document) == set(SamplerConstraintsDocument.model_fields)

    def test_each_sampler_entry_carries_exactly_the_records_fields(self, document):
        for sampler, entry in document["samplers"].items():
            assert set(entry) == set(PublishedSamplerRecord.model_fields), sampler

    def test_the_document_round_trips_through_its_own_type(self, document):
        assert SamplerConstraintsDocument.model_validate(document).model_dump(mode="json") == document

    def test_the_swagger_schema_is_derived_from_the_model(self, swagger_schema):
        assert swagger_schema == inline_json_schema_definitions(SamplerConstraintsDocument.model_json_schema())

    def test_the_swagger_schema_leaves_no_dangling_reference(self, swagger_schema):
        # Swagger 2.0 has no `$defs`, so a surviving reference would document nothing.
        rendered = json.dumps(swagger_schema)

        assert "$ref" not in rendered
        assert "$defs" not in rendered

    def test_the_swagger_schema_uses_only_swagger_2_equivalents(self, swagger_schema):
        rendered = json.dumps(swagger_schema)

        assert '"anyOf"' not in rendered
        assert '"const"' not in rendered
        assert '"propertyNames"' not in rendered
        assert rendered.count('"x-nullable"') == 4
        assert swagger_schema["properties"]["work_accounting"]["properties"]["authoritative_field"]["enum"] == [
            "work_profile",
        ]

    def test_the_swagger_schema_describes_the_served_top_level(self, swagger_schema, document):
        assert set(swagger_schema["properties"]) == set(document)


class TestStylesCarryTheSolverSettings:
    """A style stores a params payload, so it has to be able to name every setting a request can.

    Style params inherit from the same request model, which is what makes this automatic rather than a
    second whitelist to maintain. The test exists because that inheritance is easy to break by adding a
    field to the wrong model.
    """

    @pytest.fixture(scope="class")
    def style_param_fields(self):
        # The models are registered against the live api namespace on import, so this reads the same
        # definition the endpoint validates against rather than a rebuilt copy.
        from horde.apis.v2.stable import models

        # `.resolved` merges the inherited fields; a model's own keys() shows only what it adds.
        return set(models.input_model_style_params.resolved.keys())

    def test_the_scheduler_is_a_style_setting(self, style_param_fields):
        assert "scheduler" in style_param_fields

    def test_every_solver_knob_is_a_style_setting(self, style_param_fields):
        from horde.consts import SOLVER_OPTION_PARAMS

        for field_name in SOLVER_OPTION_PARAMS:
            assert field_name in style_param_fields, field_name

    def test_a_style_is_validated_against_the_models_it_will_run_on(self):
        # A style replaces both the params and the model list, so the constraint check has to read the
        # style's models. Reading the request's own list would check the style against models it is not
        # going to use.
        import inspect

        from horde.apis.v2 import stable

        source = inspect.getsource(stable.ImageAsyncGenerate.validate)
        assert "models=self.models" in source
        assert "models=self.args.models" not in source
