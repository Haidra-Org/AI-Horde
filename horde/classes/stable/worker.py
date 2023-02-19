from datetime import datetime
from horde.logger import logger
from horde.flask import db
from horde.classes.base.worker import Worker
from horde.suspicions import Suspicions
from horde.bridge_reference import check_bridge_capability, check_sampler_capability
from horde.model_reference import model_reference
from horde import exceptions as e
from horde.utils import sanitize_string

class ImageWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "stable_worker",
    }    
    max_pixels = db.Column(db.Integer, default=512 * 512, nullable=False)
    allow_img2img = db.Column(db.Boolean, default=True, nullable=False)
    allow_painting = db.Column(db.Boolean, default=True, nullable=False)
    allow_post_processing = db.Column(db.Boolean, default=True, nullable=False)
    wtype = "image"

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        if kwargs.get("max_pixels", 512 * 512) > 2048 * 2048:
            if not self.user.trusted:
                self.report_suspicion(reason=Suspicions.EXTREME_MAX_PIXELS)
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get('allow_img2img', True)
        self.allow_painting = kwargs.get('allow_painting', True)
        self.allow_post_processing = kwargs.get('allow_post_processing', True)
        if len(self.get_model_names()) == 0:
            self.set_models(['stable_diffusion'])
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        db.session.commit()
        logger.trace(f"{paused_string}Stable Worker {self.name} checked-in, offering models {self.get_model_names()} at {self.max_pixels} max pixels")

    def calculate_uptime_reward(self):
        return 50

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        if not can_generate[0]:
            return [can_generate[0], can_generate[1]]
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_image and not check_bridge_capability("img2img", self.bridge_agent):
            return [False, 'img2img']
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img':
            if self.bridge_version < 4:
                return [False, 'painting']
            if "stable_diffusion_inpainting" not in self.get_model_names():
                return [False, 'models']
        # If the only model loaded is the inpainting one, we skip the worker when this kind of work is not required
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing not in ['inpainting', 'outpainting'] and self.get_model_names() == ["stable_diffusion_inpainting"]:
            return [False, 'models']
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img' and not check_bridge_capability("img2img", self.bridge_agent):
            return [False, 'painting']
        # These samplers are currently crashing nataili. Disabling them from these workers until we can figure it out
        #logger.warning(datetime.utcnow())
        if not check_sampler_capability(
            waiting_prompt.gen_payload.get('sampler_name', 'k_euler_a'), 
            self.bridge_agent, 
            waiting_prompt.gen_payload.get('karras', False)
        ):
            return [False, 'bridge_version']
        #logger.warning(datetime.utcnow())
        if len(waiting_prompt.gen_payload.get('post_processing', [])) >= 1 and not check_bridge_capability("post-processing", self.bridge_agent):
            return [False, 'bridge_version']
        if "CodeFormers" in waiting_prompt.gen_payload.get('post_processing', []) and not check_bridge_capability("CodeFormers", self.bridge_agent):
            return [False, 'bridge_version']
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_image and not self.allow_img2img:
            return [False, 'img2img']
        # Prevent txt2img requests being sent to "stable_diffusion_inpainting" workers
        #logger.warning(datetime.utcnow())
        if not waiting_prompt.source_image and (self.models == ["stable_diffusion_inpainting"] or waiting_prompt.models == ["stable_diffusion_inpainting"]):
            return [False, 'models']
        if waiting_prompt.params.get('tiling') and not check_bridge_capability("tiling", self.bridge_agent):
            return [False, 'bridge_version']
        if waiting_prompt.params.get('hires_fix') and not check_bridge_capability("hires_fix", self.bridge_agent):
            return [False, 'bridge_version']
        if waiting_prompt.params.get('clip_skip', 1) > 1 and not check_bridge_capability("clip_skip", self.bridge_agent):
            return [False, 'bridge_version']
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img' and not self.allow_painting:
            return [False, 'painting']
        #logger.warning(datetime.utcnow())
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
        return ret_dict

    def parse_models(self, unchecked_models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        del unchecked_models[200:]
        models = set()
        for model in unchecked_models:
            if model in model_reference.stable_diffusion_names:
                models.add(model)
        if len(models) == 0:
            raise e.BadRequest("Unfortunately we cannot accept workers serving unrecognised models at this time")
        return models