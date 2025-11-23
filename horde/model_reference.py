# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from datetime import datetime

import regex as re
import requests

from horde.logger import logger
from horde.threads import PrimaryTimedFunction


class ModelReference(PrimaryTimedFunction):
    quorum = None
    reference = None
    text_reference = None
    stable_diffusion_names = set()
    text_model_names = set()
    nsfw_models = set()
    controlnet_models = set()
    # Workaround because users lacking customizer role are getting models not in the reference stripped away.
    # However due to a racing or caching issue, this causes them to still pick jobs using those models
    # Need to investigate more to remove this workaround
    testing_models = {}
    no_q_regex = re.compile(r"[.,-][a-zA-Z0-9]+?-?Q(-[Ii]nt)?[2-9]{1,2}([_.-][0-9a-zA-Z]+)*")

    def call_function(self):
        """Retrieves to image and text model reference and stores in it a var"""
        # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
        # We don't want to report on any random model name a client might request
        for _riter in range(10):
            try:
                ref_json = "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/main/stable_diffusion.json"
                if datetime.utcnow() <= datetime(2025, 12, 30):  # Qwen beta
                    ref_json = (
                        "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/refs/heads/qwen/stable_diffusion.json"
                    )
                    logger.debug("Using qwen beta model reference...")
                self.reference = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_COMPVIS_REFERENCE",
                        ref_json,
                    ),
                    timeout=2,
                ).json()
                diffusers = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_DIFFUSERS_REFERENCE",
                        "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/main/diffusers.json",
                    ),
                    timeout=2,
                ).json()
                self.reference.update(diffusers)
                # logger.debug(self.reference)
                self.stable_diffusion_names = set()
                for model in self.reference:
                    if self.reference[model].get("baseline") in {
                        "stable diffusion 1",
                        "stable diffusion 2",
                        "stable diffusion 2 512",
                        "stable_diffusion_xl",
                        "stable_cascade",
                        "flux_1",
                        "qwen_image",
                    }:
                        self.stable_diffusion_names.add(model)
                        if self.reference[model].get("nsfw"):
                            self.nsfw_models.add(model)
                        if self.reference[model].get("type") == "controlnet":
                            self.controlnet_models.add(model)

                break
            except Exception as e:
                logger.error(f"Error when downloading nataili models list: {e}")

        for _riter in range(10):
            try:
                self.text_reference = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_LLM_REFERENCE",
                        "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json",
                    ),
                    timeout=2,
                ).json()
                # logger.debug(self.reference)
                self.text_model_names = set()
                for model in self.text_reference:
                    self.text_model_names.add(model)
                    if self.text_reference[model].get("nsfw"):
                        self.nsfw_models.add(model)
                break
            except Exception as err:
                logger.error(f"Error when downloading known models list: {err}")

    def get_image_model_names(self):
        return set(self.reference.keys())

    def get_text_model_names(self):
        return set(self.text_reference.keys())

    def get_model_baseline(self, model_name):
        model_details = self.reference.get(model_name, {})
        if not model_details and "[SDXL]" in model_name:
            return "stable_diffusion_xl"
        if not model_details and "[Flux]" in model_name:
            return "flux_1"
        if not model_details and "[Qwen]" in model_name:
            return "qwen_image"
        return model_details.get("baseline", "stable diffusion 1")

    def get_all_model_baselines(self, model_names):
        baselines = set()
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            baselines.add(model_details.get("baseline", "stable diffusion 1"))
        return baselines

    def get_model_requirements(self, model_name):
        model_details = self.reference.get(model_name, {})
        return model_details.get("requirements", {})

    def get_model_csam_whitelist(self, model_name):
        model_details = self.reference.get(model_name, {})
        return set(model_details.get("csam_whitelist", []))

    def get_text_model_multiplier(self, model_name):
        # To avoid doing this calculations all the time
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        if not self.text_reference.get(model_name):
            model_name_no_q = self.no_q_regex.sub("", model_name)
            if model_name_no_q in self.get_text_model_names():
                model_name = model_name_no_q
            else:
                return 1
        multiplier = int(self.text_reference[model_name]["parameters"]) / 1_000_000_000
        logger.debug(f"{model_name} param multiplier: {multiplier}")
        return multiplier

    def has_inpainting_models(self, model_names):
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            if model_details.get("inpainting"):
                return True
        return False

    def has_only_inpainting_models(self, model_names):
        if len(model_names) == 0:
            return False
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            if not model_details.get("inpainting"):
                return False
        return True

    def is_known_image_model(self, model_name):
        return model_name in self.get_image_model_names()

    def is_known_text_model(self, model_name):
        # If it's a named model, we check if we can find it without the username
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        if model_name in self.get_text_model_names():
            return True
        model_name_no_q = self.no_q_regex.sub("", model_name)
        if model_name_no_q in self.get_text_model_names():
            return True
        return False

    def has_unknown_models(self, model_names):
        if len(model_names) == 0:
            return False
        if any(not self.is_known_image_model(m) for m in model_names):
            return True
        return False

    def has_nsfw_models(self, model_names):
        if len(model_names) == 0:
            return False
        if any(m in model_reference.nsfw_models for m in model_names):
            return True
        # if self.has_unknown_models(model_names):
        #     return True
        return False


model_reference = ModelReference(3600, None)
model_reference.call_function()
