from horde.flask import db
from horde.classes.base.worker import Worker

class WorkerExtended(Worker):

    max_pixels = db.Column(db.Integer, default=512*512)
    allow_img2img = db.Column(db.Boolean, default=True)
    allow_painting = db.Column(db.Boolean, default=True)
    allow_unsafe_ipaddr = db.Column(db.Boolean, default=True)

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        if kwargs.get("max_pixels", 512*512) > 2048 * 2048:
            if not self.user.trusted:
                self.report_suspicion(reason = Suspicions.EXTREME_MAX_PIXELS)
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get('allow_img2img', True)
        self.allow_painting = kwargs.get('allow_painting', True)
        self.allow_unsafe_ipaddr = kwargs.get('allow_unsafe_ipaddr', True)
        if len(self.get_model_names()) == 0:
            self.set_models(['stable_diffusion'])
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        db.session.commit() 
        logger.debug(f"{paused_string}Worker {self.name} checked-in, offering models {self.models} at {self.max_pixels} max pixels")

    def calculate_uptime_reward(self):
        return(50)

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        is_matching = can_generate[0]
        skipped_reason = can_generate[1]
        if not is_matching:
            return([is_matching,skipped_reason])
        if self.max_pixels < waiting_prompt.width * waiting_prompt.height:
            is_matching = False
            skipped_reason = 'max_pixels'
        if waiting_prompt.source_image and self.bridge_version < 2:
            is_matching = False
            skipped_reason = 'img2img'
        if waiting_prompt.source_processing != 'img2img':
            if self.bridge_version < 4:
                is_matching = False
                skipped_reason = 'painting'
            if "stable_diffusion_inpainting" not in self.models:
                is_matching = False
                skipped_reason = 'models'
        # If the only model loaded is the inpainting one, we skip the worker when this kind of work is not required
        if waiting_prompt.source_processing not in ['inpainting','outpainting'] and self.models == ["stable_diffusion_inpainting"]:
                is_matching = False
                skipped_reason = 'models'
        if waiting_prompt.source_processing != 'img2img' and self.bridge_version < 4:
            is_matching = False
            skipped_reason = 'painting'
        # These samplers are currently crashing nataili. Disabling them from these workers until we can figure it out
        if waiting_prompt.gen_payload.get('sampler_name', 'k_euler_a') in ["k_dpm_fast", "k_dpm_adaptive", "k_dpmpp_2s_a", "k_dpmpp_2m"] and self.bridge_version < 5:
            is_matching = False
            skipped_reason = 'bridge_version'
        if waiting_prompt.gen_payload.get('karras', False) and self.bridge_version < 6:
            is_matching = False
            skipped_reason = 'bridge_version'
        if len(waiting_prompt.gen_payload.get('post_processing', [])) >= 1 and self.bridge_version < 7:
            is_matching = False
            skipped_reason = 'bridge_version'
        if waiting_prompt.source_image and not self.allow_img2img:
            is_matching = False
            skipped_reason = 'img2img'
        # Prevent txt2img requests being sent to "stable_diffusion_inpainting" workers
        if not waiting_prompt.source_image and (self.models == ["stable_diffusion_inpainting"] or waiting_prompt.models == ["stable_diffusion_inpainting"]):
            is_matching = False
            skipped_reason = 'models'
        if waiting_prompt.source_processing != 'img2img' and not self.allow_painting:
            is_matching = False
            skipped_reason = 'painting'
        if not waiting_prompt.safe_ip and not self.allow_unsafe_ipaddr:
            is_matching = False
            skipped_reason = 'unsafe_ip'
        # We do not give untrusted workers anon or VPN generations, to avoid anything slipping by and spooking them.
        if not self.user.trusted:
            # if waiting_prompt.user.is_anon():
            #     is_matching = False
            #     skipped_reason = 'untrusted'
            if not waiting_prompt.safe_ip and not waiting_prompt.user.trusted:
                is_matching = False
                skipped_reason = 'untrusted'
        return([is_matching,skipped_reason])

    def get_details(self, is_privileged = False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        allow_img2img = self.allow_img2img
        if self.bridge_version < 3: allow_img2img = False
        ret_dict["img2img"] = allow_img2img
        allow_painting = self.allow_painting
        if self.bridge_version < 4: allow_painting = False
        ret_dict["painting"] = allow_painting
        return(ret_dict)