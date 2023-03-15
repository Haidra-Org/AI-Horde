import uuid

from datetime import datetime
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy import JSON, func, or_

from horde.logger import logger
from horde.flask import db, SQLITE_MODE
from horde import vars as hv
from horde.utils import is_profane, get_db_uuid, get_expiry_date, get_db_uuid

from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.kobold.processing_generation import TextProcessingGeneration

procgen_classes = {
    "template": ProcessingGeneration,
    "image": ImageProcessingGeneration,
    "text": TextProcessingGeneration,
}

json_column_type = JSONB if not SQLITE_MODE else JSON
uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)

class WPAllowedWorkers(db.Model):
    __tablename__ = "wp_allowed_workers"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    worker = db.relationship(f"Worker")
    wp_id = db.Column(uuid_column_type(), db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"), nullable=False)
    wp = db.relationship(f"WaitingPrompt", back_populates="workers")


class WPTrickedWorkers(db.Model):
    __tablename__ = "wp_tricked_workers"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    worker = db.relationship(f"Worker")
    wp_id = db.Column(uuid_column_type(), db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"), nullable=False)
    wp = db.relationship(f"WaitingPrompt", back_populates="tricked_workers")


class WPModels(db.Model):
    __tablename__ = "wp_models"
    id = db.Column(db.Integer, primary_key=True)
    wp_id = db.Column(uuid_column_type(), db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"), nullable=False)
    wp = db.relationship(f"WaitingPrompt", back_populates="models")
    model = db.Column(db.String(255), nullable=False)


class WaitingPrompt(db.Model):
    """For storing waiting prompts in the DB"""
    __tablename__ = "waiting_prompts"
    __mapper_args__ = {
        "polymorphic_identity": "template",
        "polymorphic_on": "wp_type",
    }    
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    wp_type = db.Column(db.String(30), nullable=False, index=True)
    prompt = db.Column(db.Text, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="waiting_prompts")

    params = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)
    gen_payload = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)
    nsfw = db.Column(db.Boolean, default=False, nullable=False)
    ipaddr = db.Column(db.String(39))  # ipv6
    safe_ip = db.Column(db.Boolean, default=False, nullable=False)
    trusted_workers = db.Column(db.Boolean, default=False, nullable=False, index=True)
    slow_workers = db.Column(db.Boolean, default=True, nullable=False, index=True)
    faulted = db.Column(db.Boolean, default=False, nullable=False, index=True)
    active = db.Column(db.Boolean, default=False, nullable=False, index=True)
    consumed_kudos = db.Column(db.Integer, default=0, nullable=False)
    # The amount of jobs still to do
    n = db.Column(db.Integer, default=0, nullable=False, index=True)
    # This stores the original amount of jobs requested
    jobs = db.Column(db.Integer, default=0, nullable=False)
    things = db.Column(db.BigInteger, default=0, nullable=False)
    total_usage = db.Column(db.Float, default=0, nullable=False)
    extra_priority = db.Column(db.Integer, default=0, nullable=False, index=True)
    job_ttl = db.Column(db.Integer, default=150, nullable=False)
    client_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False)

    tricked_workers = db.relationship("WPTrickedWorkers", back_populates="wp", passive_deletes=True, cascade="all, delete-orphan")
    workers = db.relationship("WPAllowedWorkers", back_populates="wp", passive_deletes=True, cascade="all, delete-orphan")
    models = db.relationship("WPModels", back_populates="wp", cascade="all, delete-orphan")

    expiry = db.Column(db.DateTime, default=get_expiry_date, index=True)

    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)

    def __init__(self, worker_ids, models, *args, **kwargs):
        super().__init__(*args, **kwargs)
        db.session.add(self)
        db.session.commit()
        self.set_workers(worker_ids)
        self.set_models(models)
        self.extract_params()

    def set_workers(self, worker_ids = None):
        if not worker_ids: worker_ids = []
        # We don't allow more workers to claim they can server more than 50 models atm (to prevent abuse)
        for wid in worker_ids:
            worker_entry = WPAllowedWorkers(worker_id=wid,wp_id=self.id)
            db.session.add(worker_entry)

    def set_models(self, model_names = None):
        if not model_names: model_names = []
        # We don't allow more workers to claim they can server more than 50 models atm (to prevent abuse)
        logger.debug(model_names)
        for model in model_names:
            model_entry = WPModels(model=model,wp_id=self.id)
            db.session.add(model_entry)

    def activate(self):
        '''We separate the activation from __init__ as often we want to check if there's a valid worker for it
        Before we add it to the queue
        '''
        self.active = True
        if self.user.flagged and self.user.kudos > 10:
            self.extra_priority = round(self.user.kudos / 100) 
        else:    
            self.extra_priority = self.user.kudos
        db.session.commit()

    def get_model_names(self):
        # Could also do this based on self.models, but no need
        return [m.model for m in self.models]

    # These are typically horde-specific so they will be defined in the specific class for this horde type
    def extract_params(self):
        logger.debug(self.params)
        self.n = self.params.pop('n', 1)
        # We store the original amount of jobs requested as well
        self.jobs = self.n 
        # This specific per horde so it should be set in the extended class
        self.things = 0
        self.total_usage = round(self.things * self.n, 2)
        self.prepare_job_payload()
        self.set_job_ttl()
        db.session.commit()

    def prepare_job_payload(self):
        # This is what we send to the worker
        self.gen_payload = self.params
        db.session.commit()
    
    def get_job_payload(self, procgen):
        return(self.gen_payload)

    def needs_gen(self):
        return self.n > 0

    def start_generation(self, worker):
        # We have to do this to lock the row for updates, to ensure we don't have racing conditions on who is picking up requests
        myself_refresh = db.session.query(type(self)).filter(type(self).id == self.id, type(self).n > 0).with_for_update().first()
        if not myself_refresh:
            return None
        myself_refresh.n -= 1
        db.session.commit()
        procgen_class = procgen_classes[self.wp_type]
        new_gen = procgen_class(wp_id=self.id, worker_id=worker.id)
        self.n = myself_refresh.n
        logger.audit(f"Procgen with ID {new_gen.id} popped from WP {self.id} by worker {worker.id} ('{worker.name}' / {worker.ipaddr}) - {self.n} gens left")
        return self.get_pop_payload(new_gen)

    def fake_generation(self, worker):
        procgen_class = procgen_classes[self.wp_type]
        new_gen = procgen_class(
            wp_id=self.id, 
            worker_id=worker.id,
            fake=True)
        new_trick = WPTrickedWorkers(wp_id=self.id, worker_id=worker.id)
        db.session.add(new_trick)
        db.session.commit()
        logger.audit(f"FAKE Procgen with ID {new_gen.id} popped from WP {self.id} by worker {worker.id} ('{worker.name}' / {worker.ipaddr}) - {self.n} gens left")
        return self.get_pop_payload(new_gen)
    
    def tricked_worker(self, worker):
        return worker.id in [w.worker_id for w in self.tricked_workers]

    def get_pop_payload(self, procgen):
        prompt_payload = {
            "payload": self.get_job_payload(procgen),
            "id": procgen.id,
            "model": procgen.model,
        }
        return(prompt_payload)

    def is_completed(self):
        if self.faulted:
            return True
        if self.needs_gen():
            return False
        procgen_class = procgen_classes[self.wp_type]
        finished_procgens = db.session.query(
            procgen_class.wp_id
        ).filter(
            procgen_class.wp_id == self.id,
            procgen_class.fake == False,
            or_(
                procgen_class.faulted == True,
                procgen_class.generation != None,
            )
        ).count()
        if finished_procgens < self.jobs:
            return False
        return True

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
            "restarted": 0,
        }
        for procgen in self.processing_gens:
            if procgen.fake:
                continue
            if procgen.is_completed():
                ret_dict["finished"] += 1
            elif procgen.is_faulted():
                ret_dict["restarted"] += 1
            else:
                ret_dict["processing"] += 1
        return ret_dict

    # FIXME: Looks like this is not used anywhere
    # def get_queued_things(self):
    #     '''The things still queued to be generated for this waiting prompt'''
    #     return round(self.things * self.n ,2)

    def get_status(
            self, 
            request_avg, 
            active_worker_count, 
            has_valid_workers, 
            wp_queue_stats, 
            lite = False
        ):
        active_worker_thread_count = active_worker_count[1]
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = max(self.n, 0)
        # This might still happen due to a race condition on parallel requests. Not sure how to avoid it.
        if self.n < 0:
            logger.error("Request was popped more times than requested!")

        ret_dict["done"] = self.is_completed()
        ret_dict["faulted"] = self.faulted
        # Lite mode does not include the generations, to spare me download size
        if not lite:
            ret_dict["generations"] = []
            for procgen in self.processing_gens:
                if procgen.fake:
                    continue
                if procgen.is_completed():
                    ret_dict["generations"].append(procgen.get_details())

        queue_pos, queued_things, queued_n = wp_queue_stats
        # We increment the priority by 1, because it starts at -1
        # This means when all our requests are currently processing or done, with nothing else in the queue, we'll show queue position 0 which is appropriate.
        ret_dict["queue_position"] = queue_pos + 1
        # If there's fewer requests than the number of active workers
        # Then we need to adjust the parallelization accordingly
        if queued_n < active_worker_thread_count:
            active_worker_thread_count = queued_n
        avg_things_per_sec = (request_avg / hv.thing_divisors[self.wp_type]) * active_worker_thread_count
        # Is this is 0, it means one of two things:
        # 1. This horde hasn't had any requests yet. So we'll initiate it to 1 avg_things_per_sec
        # 2. All gens for this WP are being currently processed, so we'll just set it to 1 to avoid a div by zero, but it's not used anyway as it will just divide 0/1
        if avg_things_per_sec == 0:
            avg_things_per_sec = 1
        wait_time = queued_things / avg_things_per_sec
        # We add the expected running time of our processing gens
        highest_expected_time_left = 0
        for procgen in self.processing_gens:
            expected_time_left = procgen.get_expected_time_left()
            if expected_time_left > highest_expected_time_left:
                highest_expected_time_left = expected_time_left
        wait_time += highest_expected_time_left
        ret_dict["wait_time"] = round(wait_time)
        ret_dict["kudos"] = self.consumed_kudos
        ret_dict["is_possible"] = has_valid_workers
        return(ret_dict)

    def get_lite_status(self, **kwargs):
        '''Same as get_status(), but without the images to avoid unnecessary size'''
        ret_dict = self.get_status(lite=True, **kwargs)
        return(ret_dict)

    def record_usage(self, raw_things, kudos, usage_type):
        '''Record that we received a requested generation and how much kudos it costs us
        We use 'thing' here as we do not care what type of thing we're recording at this point
        This avoids me having to extend this just to change a var name
        '''
        self.user.record_usage(raw_things, kudos, usage_type)
        self.consumed_kudos = round(self.consumed_kudos + kudos,2)
        self.refresh()

    def log_faulted_prompt(self):
        '''Extendable function to log why a request was aborted'''
        logger.warning(f"Faulting waiting prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")

    def delete(self):
        for gen in self.processing_gens:
            if not self.faulted and not gen.fake:
                gen.cancel()
            gen.delete()
        for worker in self.workers:
            db.session.delete(worker)
        for tricked_worker in self.tricked_workers:
            db.session.delete(tricked_worker)
        for model in self.models:
            db.session.delete(model)
        db.session.delete(self)
        db.session.commit()

    def abort_for_maintenance(self):
        '''sets all waiting requests to 0, so that all clients pick them up once the client gen is completed'''
        if self.is_completed():
            return
        self.n = 0
        db.session.commit()

    def refresh(self):
        self.expiry = get_expiry_date()
        db.session.commit()

    def is_stale(self):
        if datetime.utcnow() > self.expiry:
            return(True)
        return(False)

    def get_priority(self):
        return(self.extra_priority)

    def set_job_ttl(self):
        '''Returns how many seconds each job request should stay waiting before considering it stale and cancelling it
        This function should be overriden by the invididual hordes depending on how the calculating ttl
        '''
        self.job_ttl = 150
        db.session.commit()
    
    def get_worker_ids(self):
        return [worker.id for worker in self.workers]