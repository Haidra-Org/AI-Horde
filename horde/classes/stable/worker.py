from datetime import datetime
from horde.logger import logger
from horde.flask import db
from horde.classes.base.worker import Worker
from horde.suspicions import Suspicions
from horde.bridge_reference import check_bridge_capability, check_sampler_capability, parse_bridge_agent
from horde.model_reference import model_reference
from horde import exceptions as e
from horde.utils import sanitize_string
from horde.consts import KNOWN_POST_PROCESSORS

class ImageWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "stable_worker",
    }
    #TODO: Switch to max_power
    max_pixels = db.Column(db.BigInteger, default=512 * 512, nullable=False)
    allow_img2img = db.Column(db.Boolean, default=True, nullable=False)
    allow_painting = db.Column(db.Boolean, default=True, nullable=False)
    allow_post_processing = db.Column(db.Boolean, default=True, nullable=False)
    allow_controlnet = db.Column(db.Boolean, default=False, nullable=False)
    allow_lora = db.Column(db.Boolean, default=False, nullable=False)
    wtype = "image"

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        if kwargs.get("max_pixels", 512 * 512) > 3072 * 3072:
            if not self.user.trusted:
                self.report_suspicion(reason=Suspicions.EXTREME_MAX_PIXELS)
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get('allow_img2img', True)
        self.allow_painting = kwargs.get('allow_painting', True)
        self.allow_post_processing = kwargs.get('allow_post_processing', True)
        self.allow_controlnet = kwargs.get('allow_controlnet', False)
        self.allow_lora = kwargs.get('allow_lora', False)
        if len(self.get_model_names()) == 0:
            self.set_models(['stable_diffusion'])
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        db.session.commit()
        logger.trace(f"{paused_string}Stable Worker {self.name} checked-in, offering models {self.get_model_names()} at {self.max_pixels} max pixels")

    def calculate_uptime_reward(self):
        baseline = 50 + (len(self.get_model_names()) * 2)
        if self.allow_lora:
            baseline += 30
        return baseline

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        if not can_generate[0]:
            return [can_generate[0], can_generate[1]]
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_image and not check_bridge_capability("img2img", self.bridge_agent):
            return [False, 'img2img']
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img':
            if not check_bridge_capability("inpainting", self.bridge_agent):
                return [False, 'painting']
            if not model_reference.has_inpainting_models(self.get_model_names()):
                return [False, 'models']
        # If the only model loaded is the inpainting ones, we skip the worker when this kind of work is not required
        if waiting_prompt.source_processing not in ['inpainting', 'outpainting'] and model_reference.has_only_inpainting_models(self.get_model_names()):
            return [False, 'models']
        if not check_sampler_capability(
            waiting_prompt.gen_payload.get('sampler_name', 'k_euler_a'), 
            self.bridge_agent, 
            waiting_prompt.gen_payload.get('karras', False)
        ):
            return [False, 'bridge_version']
        #logger.warning(datetime.utcnow())
        if len(waiting_prompt.gen_payload.get('post_processing', [])) >= 1 and not check_bridge_capability("post-processing", self.bridge_agent):
            return [False, 'bridge_version']
        for pp in KNOWN_POST_PROCESSORS:
            if pp in waiting_prompt.gen_payload.get('post_processing', []) and not check_bridge_capability(pp, self.bridge_agent):
                return [False, 'bridge_version']
        if waiting_prompt.source_image and not self.allow_img2img:
            return [False, 'img2img']
        # Prevent txt2img requests being sent to "stable_diffusion_inpainting" workers
        if not waiting_prompt.source_image and (self.models == ["stable_diffusion_inpainting"] or waiting_prompt.models == ["stable_diffusion_inpainting"]):
            return [False, 'models']
        if waiting_prompt.params.get('tiling') and not check_bridge_capability("tiling", self.bridge_agent):
            return [False, 'bridge_version']
        if waiting_prompt.params.get('return_control_map') and not check_bridge_capability("return_control_map", self.bridge_agent):
            return [False, 'bridge_version']
        if waiting_prompt.params.get('control_type'):
            if not check_bridge_capability("controlnet", self.bridge_agent):
                return [False, 'bridge_version']
            if not self.allow_controlnet:
                return [False, 'bridge_version']
        if waiting_prompt.params.get('hires_fix') and not check_bridge_capability("hires_fix", self.bridge_agent):
            return [False, 'bridge_version']
        if waiting_prompt.params.get('clip_skip', 1) > 1 and not check_bridge_capability("clip_skip", self.bridge_agent):
            return [False, 'bridge_version']
        if waiting_prompt.source_processing != 'img2img' and not self.allow_painting:
            return [False, 'painting']
        if not waiting_prompt.safe_ip and not self.allow_unsafe_ipaddr:
            return [False, 'unsafe_ip']
        # We do not give untrusted workers anon or VPN generations, to avoid anything slipping by and spooking them.
        #logger.warning(datetime.utcnow())
        if not self.user.trusted:
            # if waiting_prompt.user.is_anon():
            #    return [False, 'untrusted']
            if not waiting_prompt.safe_ip and not waiting_prompt.user.trusted:
                return [False, 'untrusted']
        if not self.allow_post_processing and len(waiting_prompt.gen_payload.get('post_processing', [])) >= 1:
            return [False, 'post-processing']
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
                return [False, 'kudos']
        return [True, None]

    def get_details(self, details_privilege = 0):
        ret_dict = super().get_details(details_privilege)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        allow_img2img = self.allow_img2img
        if self.bridge_version < 3: allow_img2img = False
        ret_dict["img2img"] = allow_img2img
        allow_painting = self.allow_painting
        if self.bridge_version < 4: allow_painting = False
        ret_dict["painting"] = allow_painting
        ret_dict["post-processing"] = self.allow_post_processing        
        ret_dict["controlnet"] = self.allow_controlnet        
        ret_dict["lora"] = self.allow_lora
        return ret_dict

    def parse_models(self, unchecked_models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        del unchecked_models[300:]
        models = set()
        for model in unchecked_models:
            if model in model_reference.stable_diffusion_names:
                models.add(model)
            elif self.user.customizer:
                models.add(model)
            else:
                logger.debug(f"Rejecting unknown model '{model}' from {self.name} ({self.id})")
        if len(models) == 0:
            raise e.BadRequest("Unfortunately we cannot accept workers serving unrecognised models at this time")
        return models

    def get_bridge_kudos_multiplier(self):
        bridge_name, bridge_version = parse_bridge_agent(self.bridge_agent)
        # Non-hordelib workers gets their kudos rewards reduced by 25% 
        # to incentivize switching to the latest version
        if bridge_name != "AI Horde Worker" or bridge_version < 21:
            return 0.75
        return 1
