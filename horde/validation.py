# SPDX-FileCopyrightText: 2024 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from collections.abc import Collection, Mapping
from typing import Any

from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_sdk.generation_parameters.image.constraints import (
    CFG_PP_SAMPLERS,
    CONSTRAINT_VIOLATION_KIND,
    SAMPLER_SOLVER_KNOB,
    list_constraint_violations,
)
from loguru import logger

from horde import exceptions as e
from horde.baseline_policy import UNSUPPORTED_MODEL_RETURN_CODE, baseline_violation
from horde.classes.base.user import User
from horde.consts import (
    CONTROL_STRENGTH_MAX,
    CONTROL_STRENGTH_MIN,
    CONTROL_STRENGTH_PARAM,
    FLOW_SHIFT_MAX,
    FLOW_SHIFT_MIN,
    FLOW_SHIFT_PARAM,
    KNOWN_POST_PROCESSORS,
    KNOWN_UPSCALERS,
    baseline_for_constraints,
    scheduler_for_request,
)
from horde.enums import BaselineFeature, WarningMessage
from horde.model_reference import model_reference

# The request field carrying each solver knob, keyed by the knob the shared constraints table names. The
# field names carry a `sampler_` prefix the backend's own keyword names do not.
SOLVER_KNOB_REQUEST_FIELDS = {
    SAMPLER_SOLVER_KNOB.eta: "sampler_eta",
    SAMPLER_SOLVER_KNOB.s_noise: "sampler_s_noise",
    SAMPLER_SOLVER_KNOB.s_churn: "sampler_s_churn",
    SAMPLER_SOLVER_KNOB.s_tmin: "sampler_s_tmin",
    SAMPLER_SOLVER_KNOB.s_tmax: "sampler_s_tmax",
    SAMPLER_SOLVER_KNOB.order: "sampler_order",
}

# One return code per violation class, so a client can tell "this sampler has no such setting" from
# "that value is out of range" without parsing the message.
CONSTRAINT_VIOLATION_RETURN_CODES = {
    CONSTRAINT_VIOLATION_KIND.knob_inapplicable: "SamplerKnobInapplicable",
    CONSTRAINT_VIOLATION_KIND.knob_out_of_range: "SamplerKnobOutOfRange",
    CONSTRAINT_VIOLATION_KIND.solver_type_unsupported: "SamplerSolverTypeUnsupported",
    CONSTRAINT_VIOLATION_KIND.sampler_scheduler_rejected: "SamplerSchedulerMismatch",
    CONSTRAINT_VIOLATION_KIND.scheduler_baseline_unsupported: "SchedulerBaselineMismatch",
}

# Above this, the CFG++ correction the `*_cfg_pp` solvers apply oversaturates rather than improving
# adherence. Advisory rather than enforced: it is a quality expectation, and the image still renders.
CFG_PP_ADVISED_MAX_CFG_SCALE = 2.0


def raise_model_policy_violations(
    baselines: Collection[KNOWN_IMAGE_GENERATION_BASELINE | str],
    params: Mapping[str, Any],
    features: Collection[BaselineFeature],
    *,
    source_processing: str | None = None,
) -> None:
    """Raise the first policy rejection the request draws for these baselines, if any.

    Features are passed per call site so the non-baseline checks around them keep their place in the
    order a request is rejected in.

    Args:
        baselines: The baselines of the models the job may run on.
        params: The generation payload.
        features: The features this call site owns.
        source_processing: The request's source processing mode, read by the remix feature.

    Raises:
        UnsupportedModel: The rejection describes a model the request named.
        BadRequest: The rejection describes a field the request set.
    """
    violation = baseline_violation(baselines, params=params, source_processing=source_processing, features=features)
    if violation is None:
        return
    return_code, detail = violation
    # This one describes a model the request named rather than a field it set, so it keeps the
    # unsupported-model status it has always carried.
    if return_code == UNSUPPORTED_MODEL_RETURN_CODE:
        raise e.UnsupportedModel(detail, rc=return_code)
    raise e.BadRequest(detail, rc=return_code)


class ParamValidator:
    prompt: str
    models: list
    params: dict
    user: User
    warnings = set()

    def __init__(self, prompt, models, params, user):
        self.prompt = prompt
        self.models = models
        self.params = params
        self.user = user
        self.warnings = set()

    def validate_base_params(self):
        pass

    def validate_text_params(self):
        self.validate_base_params()
        if self.params.get("max_context_length", 2048) < self.params.get("max_length", 80):
            raise e.BadRequest("You cannot request more tokens than your context length.", rc="TokenOverflow")
        if "sampler_order" in self.params and len(set(self.params["sampler_order"])) < 7:
            raise e.BadRequest(
                "When sending a custom sampler order, you need to specify all possible samplers in the order",
                rc="MissingFullSamplerOrder",
            )
        if "stop_sequence" in self.params:
            stop_seqs = set(self.params["stop_sequence"])
            if len(stop_seqs) > 128:
                raise e.BadRequest("Too many stop sequences specified (max allowed is 128).", rc="TooManyStopSequences")
            total_stop_seq_len = 0
            for seq in stop_seqs:
                total_stop_seq_len += len(seq)
            if total_stop_seq_len > 2000:
                raise e.BadRequest("Your total stop sequence length exceeds the allowed limit (2000 chars).", rc="ExcessiveStopSequence")

    def get_set_solver_knobs(self):
        """Returns the solver knobs this request set, keyed as the shared constraints table names them.

        A knob left unset is absent rather than present-and-None, because leaving a knob out means the
        solver's own default applies, which is never a constraint violation.
        """
        return {
            knob: self.params[field_name]
            for knob, field_name in SOLVER_KNOB_REQUEST_FIELDS.items()
            if self.params.get(field_name) is not None
        }

    def validate_sampler_constraints(self):
        """Rejects requests the image backend cannot render as asked, and warns about the rest.

        The rules come from horde_sdk, which reads them from the backend's own solver implementations.
        They are refused rather than adjusted because every one of them is a case where the backend
        would otherwise produce something the request did not ask for: a knob a solver does not declare
        is dropped in silence, a schedule with no sigmas for the model cannot be built, and one
        sampler/schedule pairing returns colour noise at every step count.
        """
        sampler_name = self.params.get("sampler_name", "k_euler_a")
        scheduler = scheduler_for_request(self.params.get("scheduler"), self.params.get("karras", True))
        solver_type = self.params.get("sampler_solver_type")
        set_knobs = self.get_set_solver_knobs()

        # A request naming several models is checked against each of their baselines: the schedule has to
        # be renderable on whichever one the job is eventually dispatched for.
        model_baselines = {model_reference.get_model_baseline(model_name) for model_name in self.models}
        flow_shift = self.params.get(FLOW_SHIFT_PARAM)
        if flow_shift is not None:
            if not FLOW_SHIFT_MIN <= flow_shift <= FLOW_SHIFT_MAX:
                raise e.BadRequest(
                    f"flow_shift must be between {FLOW_SHIFT_MIN:g} and {FLOW_SHIFT_MAX:g}, inclusive.",
                    rc="FlowShiftOutOfRange",
                )
            raise_model_policy_violations(model_baselines, self.params, [BaselineFeature.FLOW_SHIFT])
        # The sampler constraints are keyed by the shared vocabulary, which a baseline the reference
        # publishes ahead of this deployment is not in; None leaves them unenforced for it.
        baselines = {baseline_for_constraints(baseline) for baseline in model_baselines}
        for baseline in baselines or {None}:
            violations = list_constraint_violations(
                sampler=sampler_name,
                scheduler=scheduler,
                baseline=baseline,
                numeric_knobs=set_knobs,
                solver_type=solver_type,
            )
            if violations:
                violation = violations[0]
                raise e.BadRequest(violation.detail, rc=CONSTRAINT_VIOLATION_RETURN_CODES[violation.kind])

        if sampler_name in CFG_PP_SAMPLERS and self.params.get("cfg_scale", 7.5) > CFG_PP_ADVISED_MAX_CFG_SCALE:
            self.warnings.add(WarningMessage.CfgPPScaleTooLarge)

    def validate_control_strength(self) -> None:
        """Reject a control strength the backend has nothing to apply it to, or cannot apply as asked.

        The field weights the ControlNet guidance, so a request that sets it with neither a `control_type`
        nor the qr_code workflow has asked for something the backend has no control map to weight: it
        would render a plain generation with no sign that the setting had been dropped. The qr_code
        workflow builds its own control map from the extra texts and scales it by this field, so it
        weights the guidance without naming a control type. The range is checked here as well as in the
        schema so that a payload reaching the validator by any other route is held to the same bounds,
        which is also what gives the rejection a return code a client can branch on.
        """
        control_strength = self.params.get(CONTROL_STRENGTH_PARAM)
        if control_strength is None:
            return
        has_control_map = self.params.get("control_type") is not None or self.params.get("workflow") == "qr_code"
        if not has_control_map:
            raise e.BadRequest(
                "control_strength only applies alongside a control_type or the qr_code workflow.",
                rc="ControlStrengthWithoutControlType",
            )
        if not CONTROL_STRENGTH_MIN <= control_strength <= CONTROL_STRENGTH_MAX:
            raise e.BadRequest(
                f"control_strength must be between {CONTROL_STRENGTH_MIN:g} and {CONTROL_STRENGTH_MAX:g}, inclusive.",
                rc="ControlStrengthOutOfRange",
            )

    def validate_image_params(self):
        self.validate_base_params()
        for model_req_dict in [model_reference.get_model_requirements(m) for m in self.models]:
            if "clip_skip" in model_req_dict and model_req_dict["clip_skip"] != self.params.get("clip_skip", 1):
                self.warnings.add(WarningMessage.ClipSkipMismatch)
            if "min_steps" in model_req_dict and model_req_dict["min_steps"] > self.params.get("steps", 30):
                self.warnings.add(WarningMessage.StepsTooFew)
            if "max_steps" in model_req_dict and model_req_dict["max_steps"] < self.params.get("steps", 30):
                self.warnings.add(WarningMessage.StepsTooMany)
            if "cfg_scale" in model_req_dict and model_req_dict["cfg_scale"] != self.params.get("cfg_scale", 7.5):
                self.warnings.add(WarningMessage.CfgScaleMismatch)
            if "min_cfg_scale" in model_req_dict and model_req_dict["min_cfg_scale"] > self.params.get("cfg_scale", 7.5):
                self.warnings.add(WarningMessage.CfgScaleTooSmall)
            if "max_cfg_scale" in model_req_dict and model_req_dict["max_cfg_scale"] < self.params.get("cfg_scale", 7.5):
                self.warnings.add(WarningMessage.CfgScaleTooLarge)
            if "samplers" in model_req_dict and self.params.get("sampler_name", "k_euler_a") not in model_req_dict["samplers"]:
                self.warnings.add(WarningMessage.SamplerMismatch)
            scheduler = scheduler_for_request(self.params.get("scheduler"), self.params.get("karras", True))
            if "schedulers" in model_req_dict and scheduler not in model_req_dict["schedulers"]:
                self.warnings.add(WarningMessage.SchedulerMismatch)
        self.validate_sampler_constraints()
        self.validate_control_strength()
        model_baselines = model_reference.get_all_model_baselines(self.models)
        raise_model_policy_violations(model_baselines, self.params, [BaselineFeature.HIRES_FIX])
        if "loras" in self.params:
            if len(self.params["loras"]) > 5:
                raise e.BadRequest("You cannot request more than 5 loras per generation.", rc="TooManyLoras")
            for lora in self.params["loras"]:
                if lora.get("is_version") and not lora["name"].isdigit():
                    raise e.BadRequest("explicit LoRa version requests have to be a version ID (i.e integer).", rc="BadLoraVersion")
        if "tis" in self.params and len(self.params["tis"]) > 20:
            raise e.BadRequest("You cannot request more than 20 Textual Inversions per generation.", rc="TooManyTIs")
        raise_model_policy_violations(
            model_baselines,
            self.params,
            [BaselineFeature.TRANSPARENT, BaselineFeature.QR_CODE],
        )
        if len(self.prompt.split()) > 7500:
            raise e.InvalidPromptSize()
        if any(model_name in KNOWN_POST_PROCESSORS for model_name in self.models):
            raise e.UnsupportedModel(rc="UnexpectedModelName")
        upscaler_count = len([pp for pp in self.params.get("post_processing", []) if pp in KNOWN_UPSCALERS])
        if upscaler_count > 1:
            raise e.BadRequest("Cannot use more than 1 upscaler at a time.", rc="TooManyUpscalers")
        cfg_scale = self.params.get("cfg_scale")
        if cfg_scale is not None:
            try:
                rounded_cfg_scale = round(cfg_scale, 2)
                if rounded_cfg_scale != cfg_scale:
                    raise e.BadRequest("cfg_scale must be rounded to 2 decimal places", rc="BadCFGDecimals")
            except (TypeError, ValueError):
                logger.warning(
                    f"Invalid cfg_scale: {cfg_scale} for when it should be already validated.",
                )
                raise e.BadRequest("cfg_scale must be a valid number", rc="BadCFGNumber")

        return self.warnings

    def check_for_special(self):
        if not self.user and self.params.get("special"):
            raise e.BadRequest("Only special users can send a special field.", "SpecialFieldNeedsSpecialUser")
        for model in self.models:
            if "horde_special" in model:
                if not self.user.special:
                    raise e.Forbidden("Only special users can request a special model.", "SpecialModelNeedsSpecialUser")
                usermodel = model.split("::")
                if len(usermodel) == 1:
                    raise e.BadRequest(
                        "Special models must always include the username, in the form of 'horde_special::user#id'",
                        rc="SpecialMissingUsername",
                    )
                user_alias = usermodel[1]
                if self.user.get_unique_alias() != user_alias:
                    raise e.Forbidden(f"This model can only be requested by {user_alias}", "SpecialForbidden")
                if not self.params.get("special"):
                    raise e.BadRequest("Special models have to include a special payload", rc="SpecialMissingPayload")

    def validate_image_prompt(self, prompt):
        if "{p}" not in prompt:
            raise e.BadRequest(
                "A style prompt must include a dedicated spot where the user's positive prompt will be added, signified with '{p}'",
                "StylePromptMissingVars",
            )
        if "{np}" not in prompt:
            raise e.BadRequest(
                "A style prompt must include a dedicated spot where the user's negative prompt will be added, signified with '{np}'",
                "StylePromptMissingVars",
            )

    def validate_text_prompt(self, prompt):
        if "{p}" not in prompt:
            raise e.BadRequest(
                "A style prompt must include a dedicated spot where the user's positive prompt will be added, signified with '{p}'",
                "StylePromptMissingVars",
            )
