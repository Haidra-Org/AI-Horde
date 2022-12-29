from datetime import datetime
from horde.logger import logger
from horde.flask import db
from horde.classes.base.worker import Worker, uuid_column_type
from horde.suspicions import Suspicions

class InterrogationPerformance(db.Model):
    __tablename__ = "worker_interrogation_performances"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"WorkerExtended", back_populates="interrogation_performance")
    performance = db.Column(db.Float, primary_key=False)
    created = db.Column(db.DateTime, default=datetime.utcnow) # TODO maybe index here, but I'm not sure how big this table is

class WorkerExtended(Worker):
    max_pixels = db.Column(db.Integer, default=512 * 512, nullable=False)
    allow_img2img = db.Column(db.Boolean, default=True, nullable=False)
    allow_painting = db.Column(db.Boolean, default=True, nullable=False)
    allow_unsafe_ipaddr = db.Column(db.Boolean, default=True, nullable=False)
    allow_post_processing = True
    interrogation_performance = db.relationship("InterrogationPerformance", back_populates="worker", cascade="all, delete-orphan")

    def check_in(self, max_pixels, **kwargs):
        super().check_in(**kwargs)
        if kwargs.get("max_pixels", 512 * 512) > 2048 * 2048:
            if not self.user.trusted:
                self.report_suspicion(reason=Suspicions.EXTREME_MAX_PIXELS)
        self.max_pixels = max_pixels
        self.allow_img2img = kwargs.get('allow_img2img', True)
        self.allow_painting = kwargs.get('allow_painting', True)
        self.allow_unsafe_ipaddr = kwargs.get('allow_unsafe_ipaddr', True)
        self.allow_post_processing = kwargs.get('allow_post_processing', True)
        if len(self.get_model_names()) == 0:
            self.set_models(['stable_diffusion'])
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        db.session.commit()
        logger.trace(f"{paused_string}Worker {self.name} checked-in, offering models {self.get_model_names()} at {self.max_pixels} max pixels")

    def calculate_uptime_reward(self):
        return 50

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        is_matching = can_generate[0]
        skipped_reason = can_generate[1]
        if not is_matching:
            return [is_matching, skipped_reason]
        #logger.warning(datetime.utcnow())
        if self.max_pixels < waiting_prompt.params.get('width', 512) * waiting_prompt.params.get('height', 512):
            is_matching = False
            skipped_reason = 'max_pixels'
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_image and self.bridge_version < 2:
            is_matching = False
            skipped_reason = 'img2img'
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img':
            if self.bridge_version < 4:
                is_matching = False
                skipped_reason = 'painting'
            if "stable_diffusion_inpainting" not in self.get_model_names():
                is_matching = False
                skipped_reason = 'models'
        # If the only model loaded is the inpainting one, we skip the worker when this kind of work is not required
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing not in ['inpainting', 'outpainting'] and self.get_model_names() == ["stable_diffusion_inpainting"]:
            is_matching = False
            skipped_reason = 'models'
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img' and self.bridge_version < 4:
            is_matching = False
            skipped_reason = 'painting'
        # These samplers are currently crashing nataili. Disabling them from these workers until we can figure it out
        #logger.warning(datetime.utcnow())
        if waiting_prompt.gen_payload.get('sampler_name', 'k_euler_a') in ["k_dpm_fast", "k_dpm_adaptive", "k_dpmpp_2s_a", "k_dpmpp_2m"] and self.bridge_version < 5:
            is_matching = False
            skipped_reason = 'bridge_version'
        #logger.warning(datetime.utcnow())
        if waiting_prompt.gen_payload.get('karras', False) and self.bridge_version < 6:
            is_matching = False
            skipped_reason = 'bridge_version'
        #logger.warning(datetime.utcnow())
        if len(waiting_prompt.gen_payload.get('post_processing', [])) >= 1 and self.bridge_version < 7:
            is_matching = False
            skipped_reason = 'bridge_version'
        if "CodeFormers" in waiting_prompt.gen_payload.get('post_processing', []) and self.bridge_version < 9:
            is_matching = False
            skipped_reason = 'bridge_version'
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_image and not self.allow_img2img:
            is_matching = False
            skipped_reason = 'img2img'
        # Prevent txt2img requests being sent to "stable_diffusion_inpainting" workers
        #logger.warning(datetime.utcnow())
        if not waiting_prompt.source_image and (self.models == ["stable_diffusion_inpainting"] or waiting_prompt.models == ["stable_diffusion_inpainting"]):
            is_matching = False
            skipped_reason = 'models'
        #logger.warning(datetime.utcnow())
        if waiting_prompt.source_processing != 'img2img' and not self.allow_painting:
            is_matching = False
            skipped_reason = 'painting'
        #logger.warning(datetime.utcnow())
        if not waiting_prompt.safe_ip and not self.allow_unsafe_ipaddr:
            is_matching = False
            skipped_reason = 'unsafe_ip'
        # We do not give untrusted workers anon or VPN generations, to avoid anything slipping by and spooking them.
        #logger.warning(datetime.utcnow())
        if not self.user.trusted:
            # if waiting_prompt.user.is_anon():
            #     is_matching = False
            #     skipped_reason = 'untrusted'
            if not waiting_prompt.safe_ip and not waiting_prompt.user.trusted:
                is_matching = False
                skipped_reason = 'untrusted'
        if not self.allow_post_processing and len(waiting_prompt.gen_payload.get('post_processing', [])) >= 1:
            is_matching = False
            skipped_reason = 'post-processing'
        return [is_matching, skipped_reason]

    def get_details(self, is_privileged=False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_pixels"] = self.max_pixels
        ret_dict["megapixelsteps_generated"] = self.contributions
        allow_img2img = self.allow_img2img
        if self.bridge_version < 3: allow_img2img = False
        ret_dict["img2img"] = allow_img2img
        allow_painting = self.allow_painting
        if self.bridge_version < 4: allow_painting = False
        ret_dict["painting"] = allow_painting
        return ret_dict


    @logger.catch(reraise=True)
    def record_interrogation(self, kudos, seconds_taken):
        '''We record the servers newest interrogation contribution
        '''
        self.user.record_contributions(raw_things = 0, kudos = kudos)
        self.modify_kudos(kudos,'interrogated')
        converted_amount = self.convert_contribution(raw_things)
        self.fulfilments += 1
        performances = db.session.query(InterrogationPerformance).filter_by(worker_id=self.id).order_by(InterrogationPerformance.created.asc())
        if performances.count() >= 20:
            db.session.delete(performances.first())
        new_performance = InterrogationPerformance(worker_id=self.id, performance=seconds_taken)
        db.session.add(new_performance)
        db.session.commit()
        # if things_per_sec / thing_divisor > things_per_sec_suspicion_threshold:
        #     self.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[round(things_per_sec / thing_divisor,2)])