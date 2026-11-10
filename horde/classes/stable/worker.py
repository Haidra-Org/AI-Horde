# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, override

from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from sqlalchemy.orm import Mapped, mapped_column

from horde import exceptions as e
from horde.bridge_reference import (
    bridge_supports,
    check_bridge_capability,
    check_sampler_capability,
    get_bridge_capabilities,
    is_latest_bridge_version,
    is_official_bridge_version,
)
from horde.classes.base.worker import EligibilitySkipReason, EligibilityVerdict, StoredWorkerDeclaration, Worker, WorkerEligibilityInputs
from horde.consts import (
    CONTROL_STRENGTH_PARAM,
    EXTENDED_SCHEDULERS,
    FLOW_SHIFT_PARAM,
    KNOWN_POST_PROCESSORS,
    LEGACY_IMAGE_CONTROL_TYPES,
    SIGMA_GENERATOR_SCHEDULERS,
    SOLVER_KNOB_PARAMS,
)
from horde.enums import BaselineFeature
from horde.flask import db
from horde.logger import logger
from horde.model_reference import model_reference
from horde.sampler_work_policy import (
    maximum_request_sampler_work,
    parse_sampler_execution_contract_version,
    sampler_work_request_from_payload,
)
from horde.suspicions import Suspicions

if TYPE_CHECKING:
    from horde.classes.base.waiting_prompt import RequestEligibilityInputs

MINIMUM_IMAGE_WORKER_SPEED: int = 500_000
"""The speed, in raw speed units (0.5 megapixelsteps per second), below which a worker gets only requests that allow
slow workers.

The pop query of ``get_sorted_wp_filtered_to_worker`` applies the same threshold.
"""

PAINTING_SOURCE_PROCESSING: frozenset[str] = frozenset(
    {
        "inpainting",
        "outpainting",
    },
)
"""The source processing kinds that need a bridge and a model able to paint."""


@dataclass(frozen=True)
class _ImageJudgement:
    """Represents the facts that one judgement of an image request reads, worked out once for all the image rules."""

    waiting_prompt: Any
    """The image request being judged."""
    request_inputs: "RequestEligibilityInputs"
    """The request's eligibility inputs."""
    worker_inputs: WorkerEligibilityInputs
    """The worker's eligibility inputs."""
    requested_models: tuple[str, ...]
    """The models the request accepts, or empty when it accepts any model."""
    model_baselines: frozenset[str]
    """The baselines of every model the worker advertises."""
    bridge_capabilities: Collection[str]
    """The capabilities of the worker's bridge release."""


class ImageWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "stable_worker",
    }
    # TODO: Switch to max_power
    max_pixels = db.Column(db.BigInteger, default=512 * 512, nullable=False)
    allow_img2img = db.Column(db.Boolean, default=True, nullable=False, index=True)
    allow_painting = db.Column(db.Boolean, default=True, nullable=False, index=True)
    allow_post_processing = db.Column(db.Boolean, default=True, nullable=False, index=True)
    allow_controlnet = db.Column(db.Boolean, default=False, nullable=False, index=True)
    allow_extended_controlnet = db.Column(db.Boolean, default=False, nullable=False, index=True)
    allow_sdxl_controlnet = db.Column(db.Boolean, default=False, nullable=False, index=True)
    allow_lora = db.Column(db.Boolean, default=False, nullable=False, index=True)
    limit_max_steps = db.Column(db.Boolean, default=False, nullable=False, index=True)
    sampler_execution_contract_version: Mapped[str | None] = mapped_column(db.String(32), nullable=True)
    wtype = "image"

    def check_in(
        self,
        *,
        max_pixels: int = 512 * 512,
        stored_declaration: StoredWorkerDeclaration | None = None,
        **kwargs: Any,
    ) -> bool:
        # The worker row is the recent capability snapshot shared by forecasting and this pop, and reGen
        # changes these fields between pops. They are updated even when the check-in bookkeeping is
        # debounced. The pop handler's unconditional commit persists a changed value.
        execution_contract_version = parse_sampler_execution_contract_version(
            kwargs.get("sampler_execution_contract_version"),
        )
        self.sampler_execution_contract_version = (
            execution_contract_version.value if execution_contract_version is not None else None
        )
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get("allow_img2img", True)
        self.allow_painting = kwargs.get("allow_painting", True)
        self.allow_post_processing = kwargs.get("allow_post_processing", True)
        self.allow_controlnet = kwargs.get("allow_controlnet", False)
        self.allow_extended_controlnet = kwargs.get("allow_extended_controlnet", False)
        self.allow_sdxl_controlnet = kwargs.get("allow_sdxl_controlnet", False)
        self.allow_lora = kwargs.get("allow_lora", False)
        self.limit_max_steps = kwargs.get("limit_max_steps", False)
        # The bookkeeping below reads the stored models, so they come from the pop's declaration (see
        # StoredWorkerDeclaration), or from the caches without it.
        if stored_declaration is None:
            stored_declaration = self.get_stored_declaration()
        if not super().check_in(stored_declaration=stored_declaration, **kwargs):
            return False
        # Reported with the debounced bookkeeping, so a worker that declares an extreme max_pixels is not reported on
        # every pop.
        if self.max_pixels > 3072 * 3072 and not self.user.trusted:
            self.report_suspicion(reason=Suspicions.EXTREME_MAX_PIXELS)
        if not stored_declaration.models and self.set_models_from_stored_declaration(["stable_diffusion"], stored_models=()):
            stored_declaration.models = tuple(self.get_model_names())
        paused_string = ""
        if self.paused:
            paused_string = "(Paused) "
        db.session.commit()
        logger.trace(
            f"{paused_string}Stable Worker {self.name} checked-in, offering models {stored_declaration.models} "
            f"at {self.max_pixels} max pixels",
        )
        return True

    def calculate_uptime_reward(self):
        baseline = 50 + (len(self.get_model_names()) * 2)
        if self.allow_lora:
            baseline += 30
        return baseline

    @override
    def get_eligibility_inputs_from_stored_declaration(
        self,
        stored_declaration: StoredWorkerDeclaration,
        *,
        maintenance_user_ids: Collection[int] = (),
    ) -> WorkerEligibilityInputs:
        """Return the worker-side eligibility inputs, including the bridge capabilities and the model baselines.

        Args:
            stored_declaration: The models stored for the worker.
            maintenance_user_ids: The users besides the owner whose requests the worker accepts while in maintenance.

        Returns:
            The worker's eligibility inputs.
        """
        eligibility_inputs = super().get_eligibility_inputs_from_stored_declaration(
            stored_declaration,
            maintenance_user_ids=maintenance_user_ids,
        )
        return replace(
            eligibility_inputs,
            bridge_capabilities=get_bridge_capabilities(self.bridge_agent),
            model_baselines={model_name: model_reference.get_model_baseline(model_name) for model_name in eligibility_inputs.model_names},
        )

    @override
    def can_generate_any(self, worker_inputs: WorkerEligibilityInputs) -> EligibilityVerdict:
        shared_verdict = super().can_generate_any(worker_inputs)
        if not shared_verdict.eligible:
            return shared_verdict
        model_baselines = self._resolve_model_baselines(worker_inputs, worker_inputs.model_names)
        if self._holds_baseline_its_bridge_cannot_run(model_baselines, self._resolve_bridge_capabilities(worker_inputs)):
            return EligibilityVerdict.refused(EligibilitySkipReason.BRIDGE_VERSION)
        return EligibilityVerdict.accepted()

    def _resolve_bridge_capabilities(self, worker_inputs: WorkerEligibilityInputs) -> Collection[str]:
        """Return the capabilities of the worker's bridge, taken from the eligibility inputs when they hold them.

        Args:
            worker_inputs: This worker's eligibility inputs.

        Returns:
            The capabilities of the worker's bridge release.
        """
        if worker_inputs.bridge_capabilities is not None:
            return worker_inputs.bridge_capabilities
        return get_bridge_capabilities(self.bridge_agent)

    @staticmethod
    def _resolve_model_baselines(worker_inputs: WorkerEligibilityInputs, model_names: Iterable[str]) -> frozenset[str]:
        """Return the baselines of these models, taken from the eligibility inputs where they hold the model.

        Args:
            worker_inputs: This worker's eligibility inputs.
            model_names: The models to resolve.

        Returns:
            The baselines the models resolve to.
        """
        return frozenset(
            worker_inputs.model_baselines[model_name]
            if model_name in worker_inputs.model_baselines
            else model_reference.get_model_baseline(model_name)
            for model_name in model_names
        )

    @staticmethod
    def _holds_baseline_its_bridge_cannot_run(model_baselines: Collection[str], bridge_capabilities: Collection[str]) -> bool:
        """Return whether the worker holds a model of a baseline that its bridge release cannot run.

        Support for such a baseline was added to the bridge in a later release. A worker on an older release cannot
        be sent any job, even though it holds the model.

        Args:
            model_baselines: The baselines of the worker's models.
            bridge_capabilities: The capabilities of the worker's bridge release.

        Returns:
            True when the worker holds such a model.
        """
        return KNOWN_IMAGE_GENERATION_BASELINE.flux_1 in model_baselines and "flux" not in bridge_capabilities

    @override
    def can_generate_with_eligibility_inputs(
        self,
        waiting_prompt: Any,
        request_inputs: "RequestEligibilityInputs",
        worker_inputs: WorkerEligibilityInputs,
    ) -> EligibilityVerdict:
        """Evaluate an image request from already loaded eligibility inputs.

        The shared rules run first, then the image rules in a fixed order. The first rule that refuses the worker
        gives the reason.

        Args:
            waiting_prompt: Image request being evaluated.
            request_inputs: The request's eligibility inputs.
            worker_inputs: This worker's eligibility inputs.

        Returns:
            The verdict, with the reason when the worker is refused.
        """
        shared_verdict = super().can_generate_with_eligibility_inputs(waiting_prompt, request_inputs, worker_inputs)
        if not shared_verdict.eligible:
            return shared_verdict
        judgement = _ImageJudgement(
            waiting_prompt=waiting_prompt,
            request_inputs=request_inputs,
            worker_inputs=worker_inputs,
            requested_models=request_inputs.model_names,
            model_baselines=self._resolve_model_baselines(worker_inputs, worker_inputs.model_names),
            bridge_capabilities=self._resolve_bridge_capabilities(worker_inputs),
        )
        image_rules = (
            self._capacity_refusal,
            self._source_processing_refusal,
            self._sampling_bridge_refusal,
            self._post_processing_bridge_refusal,
            self._source_image_refusal,
            self._control_refusal,
            self._model_family_bridge_refusal,
            self._extension_refusal,
            self._baseline_feature_refusal,
            self._worker_policy_refusal,
        )
        for image_rule in image_rules:
            skip_reason = image_rule(judgement)
            if skip_reason is not None:
                return EligibilityVerdict.refused(skip_reason)
        return EligibilityVerdict.accepted()

    def _capacity_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker's models, size or speed cannot serve the request, if they cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        waiting_prompt = judgement.waiting_prompt
        serves_no_requested_model = bool(judgement.requested_models) and judgement.request_inputs.model_name_set.isdisjoint(
            judgement.worker_inputs.model_names,
        )
        if serves_no_requested_model:
            return EligibilitySkipReason.MODELS
        if waiting_prompt.width * waiting_prompt.height > self.max_pixels:
            return EligibilitySkipReason.MAX_PIXELS
        too_slow_for_request = not waiting_prompt.slow_workers and self.speed < MINIMUM_IMAGE_WORKER_SPEED
        if too_slow_for_request:
            return EligibilitySkipReason.PERFORMANCE
        if self.extra_slow_worker and not waiting_prompt.extra_slow_workers:
            return EligibilitySkipReason.PERFORMANCE
        return None

    def _source_processing_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker cannot do the request's kind of source processing, if it cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        waiting_prompt = judgement.waiting_prompt
        model_names = judgement.worker_inputs.model_names
        if waiting_prompt.source_image and "img2img" not in judgement.bridge_capabilities:
            return EligibilitySkipReason.IMG2IMG
        is_painting_request = waiting_prompt.source_processing in PAINTING_SOURCE_PROCESSING
        if is_painting_request:
            if "inpainting" not in judgement.bridge_capabilities:
                return EligibilitySkipReason.PAINTING
            if not model_reference.has_inpainting_models(model_names):
                return EligibilitySkipReason.MODELS
            if not self.allow_painting:
                return EligibilitySkipReason.PAINTING
        # A worker that loaded only inpainting models is not sent work that does not need them.
        if not is_painting_request and model_reference.has_only_inpainting_models(model_names):
            return EligibilitySkipReason.MODELS
        return None

    def _sampling_bridge_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker's bridge cannot run the request's sampler settings, if it cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        gen_payload = judgement.waiting_prompt.gen_payload
        bridge_capabilities = judgement.bridge_capabilities
        supports_sampler = check_sampler_capability(
            gen_payload.get("sampler_name", "k_euler_a"),
            self.bridge_agent,
            gen_payload.get("karras", False),
        )
        if not supports_sampler:
            return EligibilitySkipReason.BRIDGE_VERSION
        requested_scheduler = gen_payload.get("scheduler")
        # A bridge that cannot read the `scheduler` field silently renders the flag's schedule instead of
        # the one requested, so a schedule the flag cannot express must not be dispatched to it.
        if requested_scheduler in EXTENDED_SCHEDULERS and "scheduler" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        if requested_scheduler in SIGMA_GENERATOR_SCHEDULERS and "sigma_generators" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        sets_solver_option = any(gen_payload.get(solver_param) is not None for solver_param in SOLVER_KNOB_PARAMS)
        if sets_solver_option and "solver_options" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        if gen_payload.get(FLOW_SHIFT_PARAM) is not None and "flow_shift" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        # A bridge that cannot read the field applies its own guidance weight instead of the requested
        # one, so the job would come back rendered to a strength nobody asked for.
        if gen_payload.get(CONTROL_STRENGTH_PARAM) is not None and "control_strength" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        return None

    def _post_processing_bridge_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker's bridge cannot run the request's post-processors, if it cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        requested_post_processors = judgement.waiting_prompt.gen_payload.get("post_processing", [])
        bridge_capabilities = judgement.bridge_capabilities
        if len(requested_post_processors) >= 1 and "post-processing" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        for post_processor in KNOWN_POST_PROCESSORS:
            if post_processor in requested_post_processors and post_processor not in bridge_capabilities:
                return EligibilitySkipReason.BRIDGE_VERSION
        return None

    def _source_image_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker does not take the request's source images, if it does not.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        waiting_prompt = judgement.waiting_prompt
        if waiting_prompt.source_image and not self.allow_img2img:
            return EligibilitySkipReason.IMG2IMG
        if waiting_prompt.extra_source_images is not None and "extra_source_images" not in judgement.bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        # Meant to keep txt2img requests away from "stable_diffusion_inpainting" workers. Both halves compare a
        # relationship of row objects with a list of model names, so the comparison is always false. The check is
        # kept unchanged.
        if not waiting_prompt.source_image and waiting_prompt.models == ["stable_diffusion_inpainting"]:
            return EligibilitySkipReason.MODELS
        return None

    def _control_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker cannot serve the request's tiling, control map or ControlNet options, if it cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        params = judgement.waiting_prompt.params
        bridge_capabilities = judgement.bridge_capabilities
        if params.get("tiling") and "tiling" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        if params.get("return_control_map") and "return_control_map" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        if params.get("control_type"):
            if "controlnet" not in bridge_capabilities or "image_is_control" not in bridge_capabilities:
                return EligibilitySkipReason.BRIDGE_VERSION
            if params["control_type"] not in LEGACY_IMAGE_CONTROL_TYPES:
                if "extended_controlnet" not in bridge_capabilities:
                    return EligibilitySkipReason.BRIDGE_VERSION
                if not self.allow_extended_controlnet:
                    return EligibilitySkipReason.CONTROLNET
            if not self.allow_controlnet:
                return EligibilitySkipReason.CONTROLNET
        if params.get("workflow") == "qr_code":
            if "controlnet" not in bridge_capabilities or "qr_code" not in bridge_capabilities:
                return EligibilitySkipReason.BRIDGE_VERSION
            holds_sdxl_model = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl in judgement.model_baselines
            if holds_sdxl_model and not self.allow_sdxl_controlnet:
                return EligibilitySkipReason.CONTROLNET
        return None

    def _model_family_bridge_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker's bridge cannot run the request on the worker's model families, if it cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        params = judgement.waiting_prompt.params
        bridge_capabilities = judgement.bridge_capabilities
        requests_hires_fix = bool(params.get("hires_fix"))
        if requests_hires_fix and "hires_fix" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        holds_stable_cascade_model = KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade in judgement.model_baselines
        if requests_hires_fix and holds_stable_cascade_model and "stable_cascade_2pass" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        if self._holds_baseline_its_bridge_cannot_run(judgement.model_baselines, bridge_capabilities):
            return EligibilitySkipReason.BRIDGE_VERSION
        if params.get("clip_skip", 1) > 1 and "clip_skip" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        return None

    def _extension_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker cannot use the request's LoRAs, textual inversions or transparency, if it cannot.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        params = judgement.waiting_prompt.params
        bridge_capabilities = judgement.bridge_capabilities
        if "loras" in params:
            if not self.allow_lora:
                return EligibilitySkipReason.LORA
            if "lora" not in bridge_capabilities:
                return EligibilitySkipReason.BRIDGE_VERSION
        if "tis" in params and "textual_inversion" not in bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        if params.get("transparent", False):
            if "layer_diffuse" not in bridge_capabilities:
                return EligibilitySkipReason.BRIDGE_VERSION
            if not self.allow_sdxl_controlnet:
                return EligibilitySkipReason.CONTROLNET
        return None

    def _baseline_feature_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker's bridge release cannot render a requested feature, if it cannot.

        A baseline feature must be rendered on the baseline the job would run on. The request was accepted because
        some release renders the feature there, which does not make this worker's release render it. Only the models
        that can serve this request are judged: a worker advertising many families must not lose an SD1 hires-fix job
        because it also holds a flux model. LoRA versions need a bridge release that reads them.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        params = judgement.waiting_prompt.params
        worker_model_names = set(judgement.worker_inputs.model_names)
        if judgement.requested_models:
            serving_model_names = set(judgement.requested_models) & worker_model_names
        else:
            serving_model_names = worker_model_names
        serving_baselines = self._resolve_model_baselines(judgement.worker_inputs, serving_model_names)
        requested_baseline_features = {
            BaselineFeature.HIRES_FIX: bool(params.get("hires_fix")),
            BaselineFeature.CONTROL_TYPE: "control_type" in params,
            BaselineFeature.FLOW_SHIFT: params.get(FLOW_SHIFT_PARAM) is not None,
            BaselineFeature.TRANSPARENT: bool(params.get("transparent", False)),
        }
        for feature, is_requested in requested_baseline_features.items():
            if not is_requested:
                continue
            if any(not bridge_supports(feature, str(baseline), self.bridge_agent) for baseline in serving_baselines):
                return EligibilitySkipReason.BRIDGE_VERSION
        requests_lora_versions = any(lora.get("is_version") for lora in params.get("loras", []))
        if requests_lora_versions and "lora_versions" not in judgement.bridge_capabilities:
            return EligibilitySkipReason.BRIDGE_VERSION
        return None

    def _worker_policy_refusal(self, judgement: _ImageJudgement) -> EligibilitySkipReason | None:
        """Return why the worker's policy refuses the request or its user, if it does.

        Args:
            judgement: The facts of this judgement.

        Returns:
            The skip reason, or None when the rules pass.
        """
        waiting_prompt = judgement.waiting_prompt
        if not waiting_prompt.safe_ip and not self.allow_unsafe_ipaddr:
            return EligibilitySkipReason.UNSAFE_IP
        if self.limit_max_steps and self._exceeds_step_limit(judgement):
            return EligibilitySkipReason.STEP_COUNT
        # An untrusted worker does not get an untrusted user's request from an unsafe IP, so nothing slips by and spooks it.
        # The requesting user is loaded only when the other two conditions hold.
        unsafe_ip_request_to_untrusted_worker = not judgement.worker_inputs.owner_trusted and not waiting_prompt.safe_ip
        if unsafe_ip_request_to_untrusted_worker and not waiting_prompt.user.trusted:
            return EligibilitySkipReason.UNTRUSTED
        requests_post_processing = len(waiting_prompt.gen_payload.get("post_processing", [])) >= 1
        if not self.allow_post_processing and requests_post_processing:
            return EligibilitySkipReason.POST_PROCESSING
        if self.require_upfront_kudos and self._requester_lacks_upfront_kudos(waiting_prompt):
            return EligibilitySkipReason.KUDOS
        return None

    def _exceeds_step_limit(self, judgement: _ImageJudgement) -> bool:
        """Return whether the request needs more sampler work than the worker's step limit allows.

        The limit for each model is the midpoint of the model's published step range. A request whose sampler work
        cannot be bounded under the worker's execution contract always exceeds it.

        Args:
            judgement: The facts of this judgement.

        Returns:
            True when the request exceeds the limit.
        """
        execution_contract_version = parse_sampler_execution_contract_version(self.sampler_execution_contract_version)
        sampler_work_ceiling = maximum_request_sampler_work(
            sampler_work_request_from_payload(judgement.waiting_prompt.params),
            execution_contract_version=execution_contract_version,
        )
        if sampler_work_ceiling is None:
            return True
        budget_model_names = judgement.requested_models if judgement.requested_models else judgement.worker_inputs.model_names
        for model_name in budget_model_names:
            requirements = model_reference.get_model_requirements(model_name)
            average_work_budget = int(requirements.get("min_steps", 20) + requirements.get("max_steps", 40)) // 2
            if sampler_work_ceiling.work_units.value > average_work_budget:
                return True
        return False

    def _requester_lacks_upfront_kudos(self, waiting_prompt: Any) -> bool:
        """Return whether the requesting user lacks the kudos this worker requires upfront.

        The user's minimum kudos is not counted as spendable. Trusted users and the worker's prioritized users are
        exempt.

        Args:
            waiting_prompt: The request being evaluated.

        Returns:
            True when the user does not have enough kudos and is not exempt.
        """
        requesting_user = waiting_prompt.user
        spendable_kudos = requesting_user.kudos
        if spendable_kudos > 0:
            spendable_kudos -= requesting_user.get_min_kudos()
        is_exempt = requesting_user.trusted or requesting_user.get_unique_alias() in self.prioritized_users
        return not is_exempt and spendable_kudos < waiting_prompt.kudos

    def get_details(self, details_privilege=0):
        ret_dict = super().get_details(details_privilege)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        ret_dict["img2img"] = self.allow_img2img if check_bridge_capability("img2img", self.bridge_agent) else False
        ret_dict["painting"] = self.allow_painting if check_bridge_capability("inpainting", self.bridge_agent) else False
        ret_dict["post-processing"] = self.allow_post_processing
        ret_dict["controlnet"] = self.allow_controlnet
        ret_dict["extended_controlnet"] = self.allow_extended_controlnet
        ret_dict["sdxl_controlnet"] = self.allow_sdxl_controlnet
        ret_dict["lora"] = self.allow_lora
        return ret_dict

    def parse_models(self, unchecked_models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        del unchecked_models[300:]
        models = set()
        # The role properties issue a user_roles lookup per access and this runs
        # on every check-in, so evaluate them once rather than per model name.
        user_is_special = self.user.special
        user_is_customizer = self.user.customizer
        for model in unchecked_models:
            usermodel = model.split("::")
            if user_is_special and len(usermodel) == 2:
                user_alias = usermodel[1]
                if self.user.get_unique_alias() != user_alias:
                    raise e.BadRequest(f"This model can only be hosted by {user_alias}")
                models.add(model)
            elif model in model_reference.stable_diffusion_names or user_is_customizer or model in model_reference.testing_models:
                models.add(model)
            else:
                logger.debug(f"Rejecting unknown model '{model}' from {self.name} ({self.id})")
        if len(models) == 0:
            raise e.BadRequest("Unfortunately we cannot accept workers serving unrecognised models at this time")
        return models

    def get_bridge_kudos_multiplier(self):
        if is_official_bridge_version(self.bridge_agent):
            # Obsolete hordelib workers get their kudos rewards reduced by 10%
            if not is_latest_bridge_version(self.bridge_agent):
                return 0.90
        # Non-hordelib workers gets their kudos rewards reduced by 25%
        # to incentivize switching to the latest version
        else:
            return 0.75
        return 1

    def get_safe_amount(self, amount, wp):
        safe_generations = (self.max_pixels / 3.5) * amount
        mps = wp.get_amount_calculation_things()
        # If the job has upscalers, we increase the amount of MPS in our calculations
        # As currently the upscaling happens serially on the worker
        pp_multiplier = 1 + (wp.count_pp() * 0.4)
        if wp.has_heavy_operations():
            pp_multiplier *= 1.8
        mps *= pp_multiplier
        mps *= wp.get_highest_model_batching_multiplier()
        safe_amount = round(safe_generations / mps)
        if safe_amount > amount:
            safe_amount = amount
        if safe_amount <= 0:
            safe_amount = 1
        return safe_amount
