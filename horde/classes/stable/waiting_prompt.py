import random
from sqlalchemy.sql import expression

from horde.logger import logger
from horde import vars as hv
from horde.flask import db
from horde.utils import get_random_seed, count_parentheses
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.r2 import generate_procgen_upload_url, download_source_image, download_source_mask
from horde.image import convert_pil_to_b64
from horde.bridge_reference import check_bridge_capability

class ImageWaitingPrompt(WaitingPrompt):
    __mapper_args__ = {
        "polymorphic_identity": "image",
    }
    #TODO: Find a way to index width*height
    width = db.Column(db.Integer, default=512, nullable=False, server_default=expression.literal(512))
    height = db.Column(db.Integer, default=512, nullable=False, server_default=expression.literal(512))
    source_image = db.Column(db.Text, default=None)
    source_processing = db.Column(db.String(10), default='img2img', nullable=False, server_default="img2img")
    source_mask = db.Column(db.Text, default=None)
    censor_nsfw = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.literal(False))
    seed = db.Column(db.BigInteger, default=None)
    seed_variation = db.Column(db.Integer, default=None)
    kudos = db.Column(db.Float, default=0, nullable=False, server_default=expression.literal(0))
    r2 = db.Column(db.Boolean, default=False, nullable=False, index=True, server_default=expression.literal(False))
    shared = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.literal(False))
    processing_gens = db.relationship("ImageProcessingGeneration", back_populates="wp", passive_deletes=True, cascade="all, delete-orphan")

    @logger.catch(reraise=True)
    def extract_params(self):
        self.n = self.params.pop('n', 1)
        self.jobs = self.n 
        # We store width and height individually in the DB to allow us to index them easier
        if "width" not in self.params:
            self.params["width"] = 512
        if "height" not in self.params:
            self.params["height"] = 512
        if "steps" not in self.params:
            if self.params.get('control_type'):
                self.params["steps"] = 20
            else:
                self.params["steps"] = 30
        elif self.params.get('control_type') and self.params["steps"] > 40:
            # I transpaently limit CN max steps to 40
            self.params["steps"] = 40
        if "sampler_name" not in self.params:
            self.params["sampler_name"] = "k_euler_a"
        if "cfg_scale" not in self.params:
            self.params["cfg_scale"] = 5.0
        if "karras" not in self.params:
            self.params["karras"] = True
        self.width = self.params["width"]
        self.height = self.params["height"]
        # Silent change
        # if any(model_name.startswith("stable_diffusion_2") for model_name in self.get_model_names()):
        #     self.params['sampler_name'] = "dpmsolver"
        # The total amount of to pixelsteps requested.
        if self.params.get('seed') == '':
            self.seed = None
        elif self.params.get('seed') is not None:
            # logger.warning([self,'seed' in params, params])
            self.seed = self.seed_to_int(self.params.pop('seed'))
        if "seed_variation" in self.params:
            self.seed_variation = self.params.pop("seed_variation")
            # I set the seed_to_int now, because it's anyway going to be incremented by the seed_variation
            # I am not doing it in get_job_payload() because there seems to be a race condition in where even though I set self.gen_payload["seed"] to seed_to_int()
            # It then crashes in self.gen_payload["seed"] += self.seed_variation trying to None + Int
            if self.seed is None:
                self.seed = self.seed_to_int(self.seed)
        # logger.debug(self.params)
        # logger.debug([self.prompt,self.params['width'],self.params['sampler_name']])
        self.things = self.width * self.height * self.get_accurate_steps()
        self.total_usage = round(self.things * self.n / hv.thing_divisors["image"],2)
        self.prepare_job_payload(self.params)
        self.calculate_kudos()
        self.set_job_ttl()
        # Commit will happen in prepare_job_payload()

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict = None):
        '''Prepares the default job payload. This might be further adjusted per job in get_job_payload()'''
        if not initial_dict: initial_dict = {}
        self.gen_payload = initial_dict.copy()
        self.gen_payload["prompt"] = self.prompt
        # We always send only 1 iteration to Stable Diffusion
        self.gen_payload["batch_size"] = 1
        self.gen_payload["ddim_steps"] = self.params['steps']
        self.gen_payload["seed"] = self.seed
        del self.gen_payload["steps"]
        db.session.commit()

    @logger.catch(reraise=True)
    def get_job_payload(self, procgen):
        # If self.seed is None, we randomize the seed we send to the worker each time.
        if self.seed is None:
            self.gen_payload["seed"] = self.seed_to_int(self.seed)
        if self.seed_variation and self.jobs - self.n > 1:
            self.gen_payload["seed"] += self.seed_variation
            while self.gen_payload["seed"] >= 2**32:
                self.gen_payload["seed"] = self.gen_payload["seed"] >> 32
        # logger.debug([self.gen_payload["seed"],self.seed_variation])
        if procgen.worker.bridge_version >= 2:
            if not self.nsfw and self.censor_nsfw:
                self.gen_payload["use_nsfw_censor"] = True
        else:
            # These parameters are not used in bridge v1
            for v2_param in ["use_gfpgan","use_real_esrgan","use_ldsr","use_upscaling"]:
                if v2_param in self.gen_payload:
                    del self.gen_payload[v2_param]
            if not self.nsfw and self.censor_nsfw:
                if "toggles" not in self.gen_payload:
                    self.gen_payload["toggles"] = [1, 4, 8]
                elif 8 not in self.gen_payload["toggles"]:
                    self.gen_payload["toggles"].append(8)
            if "denoising_strength" in self.gen_payload:
                del self.gen_payload["denoising_strength"]
        db.session.commit()
        return(self.gen_payload)

    def get_share_metadata(self):
        '''This is uploaded along with the image to the shared R2, when this WP shared'''
        ret_dict = {
            "prompt": self.prompt,
            "width": self.params["width"],
            "height": self.params["height"],
            "steps": self.params["steps"],
            "sampler": self.params["sampler_name"],
            "cfg": self.params["cfg_scale"],
            "karras": self.params.get("karras", True),
            "pp": self.params.get("post_processing", []),
            "set": str(self.id),
            "user_type": "oauth",
        }
        if self.user.is_anon():
            ret_dict["user_type"] = "anon"
        elif self.user.is_pseudonymous():
            ret_dict["user_type"] = "pseudonymous"
        return ret_dict

    def get_pop_payload(self, procgen):
        # This prevents from sending a payload with an ID when there has been an exception inside get_job_payload()
        payload = self.get_job_payload(procgen)
        if payload:
            prompt_payload = {
                "payload": payload,
                "id": procgen.id,
                "model": procgen.model,
            }
            if self.source_image and check_bridge_capability("img2img", procgen.worker.bridge_agent):
                if check_bridge_capability("r2_source", procgen.worker.bridge_agent):
                    prompt_payload["source_image"] = self.source_image
                else:    
                    src_img = download_source_image(self.id)
                    if src_img:
                        prompt_payload["source_image"] = convert_pil_to_b64(src_img, 50)
                prompt_payload["source_processing"] = self.source_processing
                if self.source_mask:
                    if check_bridge_capability("r2_source", procgen.worker.bridge_agent):
                        prompt_payload["source_mask"] = self.source_mask
                    else:
                        src_msk = download_source_mask(self.id)
                        if src_msk:
                            prompt_payload["source_mask"] = convert_pil_to_b64(src_msk, 50)
            # We always ask the workers to upload the generation to R2 instead of sending it back as b64
            # If they send it back as b64 anyway, we upload it outselves
            prompt_payload["r2_upload"] = generate_procgen_upload_url(str(procgen.id), self.shared)
        else:
            prompt_payload = {}
            self.faulted = True
            db.session.commit()
        # logger.debug([payload,prompt_payload])
        return(prompt_payload)

    def activate(self, source_image = None, source_mask = None):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate()
        if source_image or source_mask:
            self.source_image = source_image
            self.source_mask = source_mask
            db.session.commit()
        prompt_type = "txt2img"
        if self.source_image:
            prompt_type = self.source_processing
        logger.info(
            f"New {prompt_type} prompt with ID {self.id} by {self.user.get_unique_alias()} ({self.ipaddr}): "
            f"w:{self.width} * h:{self.height} * s:{self.params['steps']} * n:{self.n} == {self.total_usage} Total MPs"
        )


    def seed_to_int(self, s = None):
        if type(s) is int:
            return s
        if s is None or s == '':
            return get_random_seed(self.n)
        n = abs(int(s) if s.isdigit() else int.from_bytes(s.encode(), 'little'))
        while n >= 2**32:
            n = n >> 32
        # logger.debug([s,n])
        return n

    def record_usage(self, raw_things, kudos, usage_type = "image"):
        '''I have to extend this function for the stable cost, to add an extra cost when it's an img2img
        img2img burns more kudos than it generates, due to the extra bandwidth costs to the horde.
        Also extra cost when upscaling
        '''
        if self.source_image:
            kudos = kudos * 1.3
        if 'RealESRGAN_x4plus' in self.gen_payload.get('post_processing', []):
            kudos = kudos * 1.3
        if 'RealESRGAN_x4plus_anime_6B' in self.gen_payload.get('post_processing', []):
            kudos = kudos * 1.3
        # Codeformers are expensive to calculate, so we increase the kudos burn
        if 'CodeFormers' in self.gen_payload.get('post_processing', []):
            kudos = kudos * 1.3
        if 'strip_background' in self.gen_payload.get('post_processing', []):
            kudos = kudos * 1.2
        # This represents the cost of using the resources of the horde
        horde_tax = 3
        # Sharing images reduces the rax
        if self.shared:
            horde_tax = 1
        if kudos < 10:
            horde_tax -= 1
        kudos += horde_tax
        if not self.slow_workers:
            kudos = kudos * 1.2
        super().record_usage(raw_things, kudos, usage_type)

    # We can calculate the kudos in advance as they model doesn't affect them
    def calculate_kudos(self):
        result = pow((self.params.get('width', 512) * self.params.get('height', 512)) - (64*64), 1.75) / pow((1024*1024) - (64*64), 1.75)
        # We need to calculate the steps, without affecting the actual steps requested
        # because some samplers are effectively doubling their steps
        steps = self.get_accurate_steps()
        self.kudos = round((0.1232 * steps) + result * (0.1232 * steps * 8.75),2)
        # For each post processor in requested, we increase the cost by 20%
        for post_processor in self.gen_payload.get('post_processing', []):
            self.kudos = round(self.kudos * 1.2,2)
        if self.gen_payload.get('control_type') and not self.gen_payload.get('return_control_map', False):
            self.kudos = round(self.kudos * 3,2)
        weights_count = count_parentheses(self.prompt)
        # we increase the kudos cost per weight
        self.kudos += weights_count
        db.session.commit()


    def require_upfront_kudos(self, counted_totals, total_threads):
        '''Returns True if this wp requires that the user already has the required kudos to fulfil it
        else returns False
        '''
        queue = counted_totals["queued_requests"]
        max_res = 1024 + (total_threads*10) - round(queue * 0.9)
        if not self.slow_workers:
            return(True,max_res) 
        if max_res < 576:
            max_res = 576
            # SD 2.0 requires at least 768 to do its thing
            if max_res < 768 and len(self.models) >= 1 and "stable_diffusion_2." in self.models:
                max_res = 768
        if max_res > 1024:
            max_res = 1024
        if self.get_accurate_steps() > 50:
            return(True,max_res)
        if self.width * self.height > max_res*max_res:
            return(True,max_res)
        if self.params.get('control_type') and self.get_accurate_steps() > 20:
            return(True,max_res)
        # 10 or more weights, require upfront kudos
        if count_parentheses(self.prompt) > 12:
            return(True,max_res)
        # haven't decided yet if this is a good idea.
        # if 'RealESRGAN_x4plus' in self.gen_payload.get('post_processing', []):
        #     return(True,max_res)
        return(False,max_res)

    def get_accurate_steps(self):
        if self.params.get('sampler_name', 'k_euler_a') in ['k_dpm_adaptive']:
            # This sampler chooses the steps amount automatically 
            # and disregards the steps value from the user
            # so we just calculate it as an average 50 steps
            return(50)
        steps = self.params['steps']
        if self.params.get('sampler_name', 'k_euler_a') in ['k_heun', "k_dpm_2", "k_dpm_2_a", "k_dpmpp_2s_a"]:
            # These samplerS do double steps per iteration, so they're at half the speed
            # So we adjust the things to take that into account
            steps *= 2
        if self.source_image and self.source_processing == "img2img":
            # 0.8 is the default on nataili
            steps *= self.gen_payload.get("denoising_strength",0.8)
        return(steps)

    def set_job_ttl(self):
        # default is 2 minutes. Then we scale up based on resolution.
        # This will be more accurate with a newer formula
        self.job_ttl = 120
        if self.width * self.height > 2048*2048:
            self.job_ttl = 800
        elif self.width * self.height > 1024*1024:
            self.job_ttl = 400
        elif self.width * self.height > 728*728:
            self.job_ttl = 260
        elif self.width * self.height >= 512*512:
            self.job_ttl = 150
        # CN is 3 times slower
        if self.gen_payload.get('control_type'):
            self.job_ttl = self.job_ttl * 3
        weights_count = count_parentheses(self.prompt)
        self.job_ttl += 3*weights_count
        # logger.info([weights_count,self.job_ttl])
        db.session.commit()

    def log_faulted_prompt(self):
        source_processing = 'txt2img'
        if self.source_image:
            source_processing = self.source_processing
        logger.warning(f"Faulting waiting {source_processing} prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")

    def get_status(self, **kwargs):
        ret_dict = super().get_status(**kwargs)
        ret_dict["shared"] = self.shared
        return ret_dict
