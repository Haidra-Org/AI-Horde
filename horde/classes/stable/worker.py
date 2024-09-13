# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from horde import exceptions as e
from horde.bridge_reference import (
    check_bridge_capability,
    check_sampler_capability,
    is_latest_bridge_version,
    is_official_bridge_version,
)
from horde.classes.base.worker import Worker
from horde.consts import KNOWN_POST_PROCESSORS
from horde.flask import db
from horde.logger import logger
from horde.model_reference import model_reference
from horde.suspicions import Suspicions


class ImageWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "stable_worker",
    }
    # TODO: Switch to max_power
    max_pixels = db.Column(db.BigInteger, default=512 * 512, nullable=False)
    allow_img2img = db.Column(db.Boolean, default=True, nullable=False)
    allow_painting = db.Column(db.Boolean, default=True, nullable=False)
    allow_post_processing = db.Column(db.Boolean, default=True, nullable=False)
    allow_controlnet = db.Column(db.Boolean, default=False, nullable=False)
    allow_sdxl_controlnet = db.Column(db.Boolean, default=False, nullable=False)
    allow_lora = db.Column(db.Boolean, default=False, nullable=False)
    limit_max_steps = db.Column(db.Boolean, default=False, nullable=False)
    wtype = "image"

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        if kwargs.get("max_pixels", 512 * 512) > 3072 * 3072:  # FIXME #noqa SIM102
            if not self.user.trusted:
                self.report_suspicion(reason=Suspicions.EXTREME_MAX_PIXELS)
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get("allow_img2img", True)
        self.allow_painting = kwargs.get("allow_painting", True)
        self.allow_post_processing = kwargs.get("allow_post_processing", True)
        self.allow_controlnet = kwargs.get("allow_controlnet", False)
        self.allow_sdxl_controlnet = kwargs.get("allow_sdxl_controlnet", False)
        self.allow_lora = kwargs.get("allow_lora", False)
        self.limit_max_steps = kwargs.get("limit_max_steps", False)
        if len(self.get_model_names()) == 0:
            self.set_models(["stable_diffusion"])
        paused_string = ""
        if self.paused:
            paused_string = "(Paused) "
        db.session.commit()
        logger.trace(
            f"{paused_string}Stable Worker {self.name} checked-in, offering models {self.get_model_names()} "
            f"at {self.max_pixels} max pixels",
        )

    def calculate_uptime_reward(self):
        baseline = 50 + (len(self.get_model_names()) * 2)
        if self.allow_lora:
            baseline += 30
        return baseline

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        if not can_generate[0]:
            return [can_generate[0], can_generate[1]]
        # logger.warning(datetime.utcnow())
        if waiting_prompt.source_image and not check_bridge_capability("img2img", self.bridge_agent):
            return [False, "img2img"]
        # logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing in [
            "inpainting",
            "outpainting",
        ]:
            if not check_bridge_capability("inpainting", self.bridge_agent):
                return [False, "painting"]
            if not model_reference.has_inpainting_models(self.get_model_names()):
                return [False, "models"]
            if not self.allow_painting:
                return [False, "painting"]
        # If the only model loaded is the inpainting ones, we skip the worker when this kind of work is not required
        if waiting_prompt.source_processing not in [
            "inpainting",
            "outpainting",
        ] and model_reference.has_only_inpainting_models(self.get_model_names()):
            return [False, "models"]
        if not check_sampler_capability(
            waiting_prompt.gen_payload.get("sampler_name", "k_euler_a"),
            self.bridge_agent,
            waiting_prompt.gen_payload.get("karras", False),
        ):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        # logger.warning(datetime.utcnow())
        if len(waiting_prompt.gen_payload.get("post_processing", [])) >= 1 and not check_bridge_capability(
            "post-processing",
            self.bridge_agent,
        ):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        for pp in KNOWN_POST_PROCESSORS:
            if pp in waiting_prompt.gen_payload.get("post_processing", []) and not check_bridge_capability(
                pp,
                self.bridge_agent,
            ):
                logger.debug("bridge_version")
                return [False, "bridge_version"]
        if waiting_prompt.source_image and not self.allow_img2img:
            return [False, "img2img"]
        # Prevent txt2img requests being sent to "stable_diffusion_inpainting" workers
        if not waiting_prompt.source_image and (
            self.models == ["stable_diffusion_inpainting"] or waiting_prompt.models == ["stable_diffusion_inpainting"]
        ):
            return [False, "models"]
        if waiting_prompt.params.get("tiling") and not check_bridge_capability("tiling", self.bridge_agent):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        if waiting_prompt.params.get("return_control_map") and not check_bridge_capability(
            "return_control_map",
            self.bridge_agent,
        ):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        if waiting_prompt.params.get("control_type"):
            if not check_bridge_capability("controlnet", self.bridge_agent):
                logger.debug("bridge_version")
                return [False, "bridge_version"]
            if not check_bridge_capability("image_is_control", self.bridge_agent):
                logger.debug("bridge_version")
                return [False, "bridge_version"]
            if not self.allow_controlnet:
                logger.debug("bridge_version")
                return [False, "controlnet"]
        if waiting_prompt.params.get("workflow") == "qr_code":
            if not check_bridge_capability("controlnet", self.bridge_agent):
                logger.debug("bridge_version")
                return [False, "bridge_version"]
            if not check_bridge_capability("qr_code", self.bridge_agent):
                logger.debug("bridge_version")
                return [False, "bridge_version"]
            if "stable_diffusion_xl" in model_reference.get_all_model_baselines(self.get_model_names()) and not self.allow_sdxl_controlnet:
                return [False, "controlnet"]
        if waiting_prompt.params.get("hires_fix") and not check_bridge_capability("hires_fix", self.bridge_agent):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        if (
            waiting_prompt.params.get("hires_fix")
            and "stable_cascade" in model_reference.get_all_model_baselines(self.get_model_names())
            and not check_bridge_capability("stable_cascade_2pass", self.bridge_agent)
        ):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        if "flux_1" in model_reference.get_all_model_baselines(self.get_model_names()) and not check_bridge_capability(
            "flux", self.bridge_agent
        ):
            logger.debug(["bridge_version",self.bridge_agent])
            return [False, "bridge_version"]
        if waiting_prompt.params.get("clip_skip", 1) > 1 and not check_bridge_capability(
            "clip_skip",
            self.bridge_agent,
        ):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        if any(lora.get("is_version") for lora in waiting_prompt.params.get("loras", [])) and not check_bridge_capability(
            "lora_versions",
            self.bridge_agent,
        ):
            logger.debug("bridge_version")
            return [False, "bridge_version"]
        if not waiting_prompt.safe_ip and not self.allow_unsafe_ipaddr:
            return [False, "unsafe_ip"]
        if self.limit_max_steps:
            for mn in waiting_prompt.get_model_names():
                avg_steps = (
                    int(
                        model_reference.get_model_requirements(mn).get("min_steps", 20)
                        + model_reference.get_model_requirements(mn).get("max_steps", 40)
                    )
                    / 2
                )
                if waiting_prompt.get_accurate_steps() > avg_steps:
                    return [False, "step_count"]
        # We do not give untrusted workers anon or VPN generations, to avoid anything slipping by and spooking them.
        # logger.warning(datetime.utcnow())
        if not self.user.trusted:  # FIXME #noqa SIM102
            # if waiting_prompt.user.is_anon():
            #    return [False, 'untrusted']
            if not waiting_prompt.safe_ip and not waiting_prompt.user.trusted:
                return [False, "untrusted"]
        if not self.allow_post_processing and len(waiting_prompt.gen_payload.get("post_processing", [])) >= 1:
            return [False, "post-processing"]
        # When the worker requires upfront kudos, the user has to have the required kudos upfront
        # But we allowe prioritized and trusted users to bypass this
        if self.require_upfront_kudos:
            user_actual_kudos = waiting_prompt.user.kudos
            # We don't want to take into account minimum kudos
            if user_actual_kudos > 0:
                user_actual_kudos -= waiting_prompt.user.get_min_kudos()
            if (
                not waiting_prompt.user.trusted
                and waiting_prompt.user.get_unique_alias() not in self.prioritized_users
                and user_actual_kudos < waiting_prompt.kudos
            ):
                return [False, "kudos"]
        return [True, None]

    def get_details(self, details_privilege=0):
        ret_dict = super().get_details(details_privilege)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        ret_dict["img2img"] = self.allow_img2img if check_bridge_capability("img2img", self.bridge_agent) else False
        ret_dict["painting"] = self.allow_painting if check_bridge_capability("inpainting", self.bridge_agent) else False
        ret_dict["post-processing"] = self.allow_post_processing
        ret_dict["controlnet"] = self.allow_controlnet
        ret_dict["sdxl_controlnet"] = self.allow_sdxl_controlnet
        ret_dict["lora"] = self.allow_lora
        return ret_dict

    def parse_models(self, unchecked_models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        del unchecked_models[300:]
        models = set()
        for model in unchecked_models:
            usermodel = model.split("::")
            if self.user.special and len(usermodel) == 2:
                user_alias = usermodel[1]
                if self.user.get_unique_alias() != user_alias:
                    raise e.BadRequest(f"This model can only be hosted by {user_alias}")
                models.add(model)
            elif model in model_reference.stable_diffusion_names or self.user.customizer or model in model_reference.testing_models:
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
