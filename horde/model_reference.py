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

    def call_function(self):
        '''Retrieves to nataili and text model reference and stores in it a var'''
        # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
        # We don't want to report on any random model name a client might request
        for iter in range(10):
            try:
                self.reference = requests.get("https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/main/stable_diffusion.json", timeout=2).json()
                diffusers = requests.get("https://raw.githubusercontent.com/Haidra-Org/AI-Horde-image-model-reference/main/diffusers.json", timeout=2).json()
                self.reference.update(diffusers)
                # logger.debug(self.reference)
                self.stable_diffusion_names = set()
                for model in self.reference:
                    if self.reference[model].get("baseline") in {"stable diffusion 1","stable diffusion 2", "stable diffusion 2 512", "stable_diffusion_xl"}:
                        self.stable_diffusion_names.add(model)
                        if self.reference[model].get("nsfw"):
                            self.nsfw_models.add(model)
                        if self.reference[model].get("type") == "controlnet":
                            self.controlnet_models.add(model)
                break            
            except Exception as e:
                logger.error(f"Error when downloading nataili models list: {e}")
        for iter in range(10):
            try:
                self.text_reference = requests.get("https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json", timeout=2).json()
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
        return model_details.get("baseline", "stable diffusion 1")

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
        return int(self.text_reference[model_name]["parameters"]) / 1000000000

    def has_inpainting_models(self, model_names):
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            if model_details.get("style") == "inpainting":
                return True
        return False

    def has_only_inpainting_models(self, model_names):
        if len(model_names) == 0:
            return False
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            if model_details.get("style") != "inpainting":
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

model_reference = ModelReference(3600, None)
model_reference.call_function()
