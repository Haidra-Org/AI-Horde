# SPDX-FileCopyrightText: 2024 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from loguru import logger

from horde import exceptions as e
from horde.classes.base.user import User
from horde.consts import KNOWN_POST_PROCESSORS, KNOWN_UPSCALERS
from horde.enums import WarningMessage
from horde.model_reference import model_reference


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

    def validate_base_params(self):
        pass

    def validate_text_params(self):
        self.validate_base_params()
        if self.params.get("max_context_length", 1024) < self.params.get("max_length", 80):
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
            # FIXME: Scheduler workaround until we support multiple schedulers
            scheduler = "karras"
            if not self.params.get("karras", True):
                scheduler = "simple"
            if "schedulers" in model_req_dict and scheduler not in model_req_dict["schedulers"]:
                self.warnings.add(WarningMessage.SchedulerMismatch)
        if any(model_reference.get_model_baseline(model_name).startswith("flux_1") for model_name in self.models):
            if self.params.get("hires_fix", False) is True:
                raise e.BadRequest("HiRes Fix does not work with Flux currently.", rc="HiResMismatch")
        if "loras" in self.params:
            if len(self.params["loras"]) > 5:
                raise e.BadRequest("You cannot request more than 5 loras per generation.", rc="TooManyLoras")
            for lora in self.params["loras"]:
                if lora.get("is_version") and not lora["name"].isdigit():
                    raise e.BadRequest("explicit LoRa version requests have to be a version ID (i.e integer).", rc="BadLoraVersion")
        if "tis" in self.params and len(self.params["tis"]) > 20:
            raise e.BadRequest("You cannot request more than 20 Textual Inversions per generation.", rc="TooManyTIs")
        if self.params.get("transparent", False) is True:
            if any(
                model_reference.get_model_baseline(model_name) not in ["stable_diffusion_xl", "stable diffusion 1"]
                for model_name in self.models
            ):
                raise e.BadRequest(
                    "Generating Transparent images is only possible for Stable Diffusion 1.5 and XL models.",
                    rc="InvalidTransparencyModel",
                )
        if self.params.get("workflow") == "qr_code":
            if not all(
                model_reference.get_model_baseline(model_name) in ["stable diffusion 1", "stable_diffusion_xl"]
                for model_name in self.models
            ):
                raise e.BadRequest("QR Code controlnet only works with SD 1.5 and SDXL models currently", rc="ControlNetMismatch.")
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
