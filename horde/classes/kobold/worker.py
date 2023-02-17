from datetime import datetime
from horde.logger import logger
from horde.flask import db
from horde.classes.base.worker import Worker
from horde.suspicions import Suspicions
from horde.bridge_reference import check_bridge_capability, check_sampler_capability
from horde.model_reference import model_reference
from horde import exceptions as e
from horde.utils import sanitize_string

class TextWorkerSoftprompts(db.Model):
    __tablename__ = "text_worker_softprompts"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"TextWorker", back_populates="softprompts")
    softprompt = db.Column(db.String(255)) 

class TextWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "text_worker",
    }    
    max_length = db.Column(db.Integer, default=80, nullable=False)
    max_content_length = db.Column(db.Integer, default=1024, nullable=False)
    allow_post_processing = db.Column(db.Boolean, default=True, nullable=False)
    
    softprompts = db.relationship("TextWorkerSoftprompts", back_populates="worker", cascade="all, delete-orphan")

    def check_in(self, max_length, max_content_length, softprompts, **kwargs):
        super().check_in(**kwargs)
        self.max_length = max_length
        self.max_content_length = max_content_length
        self.set_softprompts(softprompts)
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        logger.trace(f"{paused_string}Text Worker {self.name} checked-in, offering models {self.models} at {self.max_length} max tokens and {self.max_content_length} max content length.")

    def refresh_softprompt_cache(self):
        softprompts_list = [s.softprompt for s in self.softprompts]
        try:
            horde_r.setex(f'worker_{self.id}_softprompts_cache', timedelta(seconds=600), json.dumps(softprompts_list))
        except Exception as err:
            logger.debug(f"Error when trying to set softprompts cache: {e}. Retrieving from DB.")
        return softprompts_list

    def get_softprompt_names(self):
        if horde_r is None:
            return [s.softprompt for s in self.softprompts]
        softprompts_cache = horde_r.get(f'worker_{self.id}_softprompts_cache')
        if not softprompts_cache:
            return self.refresh_softprompt_cache()
        try:
            softprompts_ret = json.loads(softprompts_cache)
        except TypeError as e:
            logger.error(f"Softprompts cache could not be loaded: {softprompts_cache}")
            return self.refresh_softprompt_cache()
        if softprompts_ret is None:
            return self.refresh_softprompt_cache()
        return softprompts_ret

    def set_softprompts(self, softprompts):
        softprompts = [sanitize_string(softprompt_name[0:100]) for softprompt_name in softprompts]
        del softprompts[200:]
        softprompts = set(softprompts)
        existing_softprompts_names = set(self.get_softprompt_names())
        if existing_softprompts_names == softprompts:
            return
        logger.debug([existing_softprompts_names,softprompts, existing_softprompts_names == softprompts])
        db.session.query(TextWorkerSoftprompts).filter_by(worker_id=self.id).delete()
        db.session.commit()
        for softprompt_name in softprompts:
            softprompt = WorkerModel(worker_id=self.id,softprompt=softprompt_name)
            db.session.add(softprompt)
        db.session.commit()
        self.refresh_softprompt_cache()


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
            if model in model_reference.text_model_names:
                models.add(model)
        if len(models) == 0:
            raise e.BadRequest("Unfortunately we cannot accept workers serving unrecognised models at this time")
        return models