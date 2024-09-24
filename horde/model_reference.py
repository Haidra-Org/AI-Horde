# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2024 ceruleandeep
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from datetime import datetime, timezone

import requests

from horde.logger import logger
from horde.threads import PrimaryTimedFunction


class KnownModelRef(dict):
    """
    Base class for a known model reference entry.

    Known model references need to be typed for RESTX to work properly,
    but they need to be dicts for everywhere else in the code.
    """


class KnownTextModelRef(KnownModelRef):
    """
    A known text model reference entry
    """


class KnownImageModelRef(KnownModelRef):
    """
    A known image model reference entry
    """


DEFAULT_HORDE_IMAGE_COMPVIS_REFERENCE = (
    "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/main/stable_diffusion.json"
)
DEFAULT_HORDE_IMAGE_LLM_REFERENCE = "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json"
DEFAULT_HORDE_IMAGE_DIFFUSERS_REFERENCE = "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/main/diffusers.json"

SD_BASELINES = {
    "stable diffusion 1",
    "stable diffusion 2",
    "stable diffusion 2 512",
    "stable_diffusion_xl",
    "stable_cascade",
    "flux_1",
}


class ModelReference(PrimaryTimedFunction):
    quorum = None
    reference: dict[str, KnownImageModelRef] = None
    text_reference: dict[str, KnownTextModelRef] = None
    stable_diffusion_names: set[str] = set()
    text_model_names: set[str] = set()
    nsfw_models: set[str] = set()
    controlnet_models: set[str] = set()

    # Workaround because users lacking customizer role are getting models not in the reference stripped away.
    # However due to a racing or caching issue, this causes them to still pick jobs using those models
    # Need to investigate more to remove this workaround
    testing_models = {}

    def call_function(self):
        """
        Retrieves image and text model references
        """
        for _riter in range(10):
            try:
                self._load_image_models()
                break
            except Exception as e:
                logger.error(f"Error when downloading image models list: {e}")

        for _riter in range(10):
            try:
                self._load_text_models()
                break
            except Exception as e:
                logger.error(f"Error when downloading text models list: {e}")

    def _load_text_models(self):
        text_ref_data = requests.get(self._llm_ref_url, timeout=2).json()
        self.text_reference = {name: KnownTextModelRef(text_ref_data[name]) for name in text_ref_data}
        self.text_model_names = set()
        for model in self.text_reference:
            self.text_model_names.add(model)
            if self.text_reference[model].get("nsfw"):
                self.nsfw_models.add(model)

    def _load_image_models(self):
        sd_ref_data = requests.get(self._compvis_ref_url, timeout=2).json()
        diffuser_ref_data = requests.get(self._diffusers_ref_url, timeout=2).json()
        self.reference = {name: KnownImageModelRef(sd_ref_data[name]) for name in sd_ref_data}
        self.reference.update({name: KnownImageModelRef(diffuser_ref_data[name]) for name in diffuser_ref_data})
        self.stable_diffusion_names = set()
        for model in self.reference:
            if self.reference[model].get("baseline") in SD_BASELINES:
                self.stable_diffusion_names.add(model)
                if self.reference[model].get("nsfw"):
                    self.nsfw_models.add(model)
                if self.reference[model].get("type") == "controlnet":
                    self.controlnet_models.add(model)

    def get_image_model_names(self):
        return set(self.reference.keys())

    def get_text_model_names(self):
        return set(self.text_reference.keys())

    def get_model_baseline(self, model_name):
        model_details = self.reference.get(model_name, {})
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
            return 1
        multiplier = int(self.text_reference[model_name]["parameters"]) / 1000000000
        # logger.debug(f"{model_name} param multiplier: {multiplier}")
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
        return model_name in self.get_text_model_names()

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

    @property
    def _compvis_ref_url(self):
        ref_json = DEFAULT_HORDE_IMAGE_COMPVIS_REFERENCE
        if datetime.now(timezone.utc) <= datetime(2024, 9, 30, tzinfo=timezone.utc):
            # Flux Beta
            # I don't understand how this hack works, but perhaps HORDE_IMAGE_COMPVIS_REFERENCE is unset in prod
            ref_json = "https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/refs/heads/flux/stable_diffusion.json"
            logger.debug("Using flux beta model reference...")
        return os.getenv("HORDE_IMAGE_COMPVIS_REFERENCE", ref_json)

    @property
    def _llm_ref_url(self):
        # it may not be necessary to constantly pull this from the environment
        # but the original code does that so I'm keeping it
        return os.getenv("HORDE_IMAGE_LLM_REFERENCE", DEFAULT_HORDE_IMAGE_LLM_REFERENCE)

    @property
    def _diffusers_ref_url(self):
        return os.getenv("HORDE_IMAGE_DIFFUSERS_REFERENCE", DEFAULT_HORDE_IMAGE_DIFFUSERS_REFERENCE)


model_reference = ModelReference(3600, None)
model_reference.call_function()
