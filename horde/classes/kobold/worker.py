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
    wtype = "text"

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
            return [can_generate[0],can_generate[1]]
        if self.max_content_length < waiting_prompt.max_content_length:
            return [False, 'max_content_length']
        if self.max_length < waiting_prompt.max_length:
            return [False, 'max_length']
        matching_softprompt = False
        for sp in waiting_prompt.softprompts:
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if sp == '':
                matching_softprompt = True
                break
            for sp_name in self.softprompts:
                if sp in sp_name:
                    matching_softprompt = True
                    break
        if not matching_softprompt:
            return [False, 'matching_softprompt']
        return [True, None]

    def get_details(self, is_privileged = False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_length"] = self.max_length
        ret_dict["max_content_length"] = self.max_content_length
        return(ret_dict)

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