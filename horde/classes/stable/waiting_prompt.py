import random

from horde.logger import logger
from horde.vars import thing_divisor
from horde.flask import db
from horde.classes.base.waiting_prompt import WaitingPrompt


class WaitingPromptExtended(WaitingPrompt):
    source_image = db.Column(db.Text, default=None)
    source_processing = db.Column(db.String(10), default='img2img', nullable=False)
    source_mask = db.Column(db.Text, default=None)
    censor_nsfw = db.Column(db.Boolean, default=False, nullable=False)
    seed = db.Column(db.BigInteger, default=None, nullable=True)
    seed_variation = db.Column(db.Integer, default=None)
    kudos = db.Column(db.Float, default=0, nullable=False)

    @logger.catch(reraise=True)
    def extract_params(self):
        self.n = self.params.pop('n', 1)
        self.jobs = self.n 
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        # Silent change
        if self.get_model_names() == ["stable_diffusion_2.0"]:
            self.params['sampler_name'] = "dpmsolver"
        # The total amount of to pixelsteps requested.
        if self.params['seed'] == '':
            self.seed = None
        if 'seed' in self.params and self.params['seed'] is not None:
            # logger.warning([self,'seed' in params, params])
            self.seed = self.seed_to_int(self.params.pop('seed'))
        if "seed_variation" in self.params:
            self.seed_variation = self.params.pop("seed_variation")
        logger.message(self.params)
        logger.message([self.prompt,self.params['width'],self.params['sampler_name']])
        self.things = self.params.get('width',512) * self.params.get('height',512) * self.get_accurate_steps()
        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.prepare_job_payload(self.params)
        self.calculate_kudos()
        # Commit will happen in prepare_job_payload()

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict = {}):
        '''Prepares the default job payload. This might be further adjusted per job in get_job_payload()'''
        self.gen_payload = initial_dict.copy()
        self.gen_payload["prompt"] = self.prompt
        # We always send only 1 iteration to Stable Diffusion
        self.gen_payload["batch_size"] = 1
        self.gen_payload["ddim_steps"] = self.params['steps']
        self.gen_payload["seed"] = self.seed
        del self.gen_payload["steps"]
        db.session.commit()

    @logger.catch(reraise=True)
    def get_job_payload(self,procgen):
        if self.seed_variation and self.jobs - self.n == 0:
            self.gen_payload["seed"] += self.seed_variation
            while self.gen_payload["seed"] >= 2**32:
                self.gen_payload["seed"] = self.gen_payload["seed"] >> 32
        else:
            self.gen_payload["seed"] = self.seed_to_int(self.seed)
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

    def get_pop_payload(self, procgen):
        # This prevents from sending a payload with an ID when there has been an exception inside get_job_payload()
        payload = self.get_job_payload(procgen)
        if payload:
            prompt_payload = {
                "payload": payload,
                "id": procgen.id,
                "model": procgen.model,
            }
            if self.source_image and procgen.worker.bridge_version > 2:
                prompt_payload["source_image"] = self.source_image
            if procgen.worker.bridge_version > 3:
                prompt_payload["source_processing"] = self.source_processing
                if self.source_mask:
                    prompt_payload["source_mask"] = self.source_mask
        else:
            prompt_payload = {}
            self.faulted = True
            db.session.commit()
        # logger.debug([payload,prompt_payload])
        return(prompt_payload)

    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate()
        prompt_type = "txt2img"
        if self.source_image:
            prompt_type = self.source_processing
        logger.info(
            f"New {prompt_type} prompt with ID {self.id} by {self.user.get_unique_alias()} ({self.ipaddr}): "
            f"w:{self.params['width']} * h:{self.params['height']} * s:{self.params['steps']} * n:{self.n} == {self.total_usage} Total MPs"
        )

    def seed_to_int(self, s = None):
        if type(s) is int:
            return s
        if s is None or s == '':
            return random.randint(0, 2**32 - 1)
        n = abs(int(s) if s.isdigit() else int.from_bytes(s.encode(), 'little'))
        while n >= 2**32:
            n = n >> 32
        # logger.debug([s,n])
        return n

    def record_usage(self, raw_things, kudos):
        '''I have to extend this function for the stable cost, to add an extra cost when it's an img2img
        img2img burns more kudos than it generates, due to the extra bandwidth costs to the horde.
        Also extra cost when upscaling
        '''
        if self.source_image:
            kudos = kudos * 1.5
        if 'RealESRGAN_x4plus' in self.gen_payload.get('post_processing', []):
            kudos = kudos * 1.3
        super().record_usage(raw_things, kudos)

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
        db.session.commit()


    def requires_upfront_kudos(self, counted_totals):
        '''Returns True if this wp requires that the user already has the required kudos to fulfil it
        else returns False
        '''
        queue = counted_totals["queued_requests"]
        max_res = 1124 - round(queue * 0.9)
        if max_res < 576:
            max_res = 576
            # SD 2.0 requires at least 768 to do its thing
            if max_res < 768 and len(self.models) > 1 and "stable_diffusion_2.0" in self.models:
                max_res = 768
        if max_res > 1024:
            max_res = 1024
        if self.get_accurate_steps() > 50:
            return(True,max_res)
        if self.params['width'] * self.params['height'] > max_res*max_res:
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
        if self.params['width'] * self.height > 2048*2048:
            self.job_ttl = 800
        elif self.params['width'] * self.height > 1024*1024:
            self.job_ttl = 400
        elif self.params['width'] * self.height > 728*728:
            self.job_ttl = 260
        elif self.params['width'] * self.height >= 512*512:
            self.job_ttl = 150
        db.session.commit()

    def log_faulted_job(self):
        source_processing = 'txt2img'
        if self.source_image:
            source_processing = self.source_processing
        logger.warning(f"Faulting waiting {source_processing} prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")

