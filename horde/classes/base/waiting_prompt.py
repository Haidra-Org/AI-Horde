
from datetime import datetime
import uuid

from horde.logger import logger
from horde.flask import db
from horde.classes import ProcessingGeneration
from horde.classes import stats
from horde.classes import database


class WPAllowedWorkers(db.Model):
    __tablename__ = "wp_allowed_workers"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.String(32), db.ForeignKey("workers.id"))
    worker = db.relationship(f"WorkerExtended")
    wp_id = db.Column(db.Integer, db.ForeignKey("waiting_prompts.id"))
    wp = db.relationship(f"WaitingPromptExtended", back_populates="workers")


class WPTrickedWorkers(db.Model):
    __tablename__ = "wp_tricked_workers"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(db.String(32), db.ForeignKey("workers.id"))
    worker = db.relationship(f"WorkerExtended")
    wp_id = db.Column(db.Integer, db.ForeignKey("waiting_prompts.id"))
    wp = db.relationship(f"WaitingPromptExtended", back_populates="tricked_workers")


class WPModels(db.Model):
    __tablename__ = "wp_models"
    id = db.Column(db.Integer, primary_key=True)
    wp_id = db.Column(db.Integer, db.ForeignKey("waiting_prompts.id"))
    wp = db.relationship(f"WaitingPromptExtended", back_populates="models")
    model = db.Column(db.String(20), primary_key=False)


# TODO why is this line here?
logger.debug(ProcessingGeneration)
class WaitingPrompt(db.Model):
    """For storing waiting prompts in the DB"""
    __tablename__ = "waiting_prompts"
    STALE_TIME = 1200
    id = db.Column(db.String(36), primary_key=True, default=str(uuid.uuid4()))  # Whilst using sqlite use this, as it has no uuid type
    # id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # Then move to this
    prompt = db.Column(db.Text, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    user = db.relationship("User", back_populates="waiting_prompts")

    
    params = db.Column(db.JSON, default={}, nullable=False)
    gen_payload = db.Column(db.JSON, default={}, nullable=False)
    nsfw = db.Column(db.Boolean, default=False, nullable=False)
    ipaddr = db.Column(db.String(39))  # ipv6
    safe_ip = db.Column(db.Boolean, default=False, nullable=False)
    trusted_workers = db.Column(db.Boolean, default=False, nullable=False)
    last_process_time = db.Column(db.DateTime, default=datetime.utcnow())
    faulted = db.Column(db.Boolean, default=False, nullable=False)
    consumed_kudos = db.Column(db.Integer, default=0, nullable=False)
    # The amount of jobs still to do
    n = db.Column(db.Integer, default=0, nullable=False)
    # This stores the original amount of jobs requested
    jobs = db.Column(db.Integer, default=0, nullable=False)
    things = db.Column(db.Integer, default=0, nullable=False)
    total_usage = db.Column(db.Float, default=0, nullable=False)
    extra_priority = db.Column(db.Integer, default=0, nullable=False)
    job_ttl = db.Column(db.Integer, default=150, nullable=False)

    processing_gens = db.relationship("ProcessingGenerationExtended", back_populates="wp")
    tricked_workers = db.relationship("WPTrickedWorkers", back_populates="wp")
    workers = db.relationship("WPAllowedWorkers", back_populates="wp")
    models = db.relationship("WPModels", back_populates="wp")

    ttl = db.Column(db.Integer, default=1200, nullable=False)

    updated = db.Column(
        db.DateTime(timezone=False), nullable=True, onupdate=datetime.utcnow
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extract_params(**kwargs)

    def activate(self):
        '''We separate the activation from __init__ as often we want to check if there's a valid worker for it
        Before we add it to the queue
        '''
        db.session.add(self)
        db.session.commit()

    def get_model_names(self):
        return set([m.model for m in self.models])

    # These are typically horde-specific so they will be defined in the specific class for this horde type
    def extract_params(self, **kwargs):
        logger.debug(self.params)
        self.n = self.params.pop('n', 1)
        # We store the original amount of jobs requested as well
        self.jobs = self.n 
        # This specific per horde so it should be set in the extended class
        self.things = 0
        self.store_models(kwargs.get("models", ['ReadOnly']))

        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.prepare_job_payload()
        db.session.commit()

    def store_models(self, model_names):
        for model in model_names:
            model_entry = WPModels(wp_id=self.id, model=model)
            db.session.add(model_entry)
        db.session.commit()

    def prepare_job_payload(self):
        # This is what we send to the worker
        self.gen_payload = self.params
        db.session.commit()
    
    def get_job_payload(self,procgen):
        return(self.gen_payload)

    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, worker):
        if self.n <= 0:
            return
        new_gen = self.new_procgen(worker)
        self.n -= 1
        self.refresh()
        logger.audit(f"Procgen with ID {new_gen.id} popped from WP {self.id} by worker {worker.id} ('{worker.name}' / {worker.ipaddr})")
        return self.get_pop_payload(new_gen)

    def fake_generation(self, worker):
        new_gen = self.new_procgen(worker)
        new_gen.fake = True
        new_trick = WPTrickedWorkers(wp_id=self.id, worker_id=worker.id)
        db.session.add(new_trick)
        db.session.commit()
        return self.get_pop_payload(new_gen)
    
    def tricked_worker(self, worker):
        return worker in self.tricked_workers

    def get_pop_payload(self, procgen):
        prompt_payload = {
            "payload": self.get_job_payload(procgen),
            "id": procgen.id,
            "model": procgen.model,
        }
        return(prompt_payload)

    # Using this function so that I can extend it to have it grab the correct extended class
    def new_procgen(self, worker):
        return(ProcessingGeneration(wp_id=self.id, worker_id=worker.id))

    def is_completed(self):
        if self.faulted:
            return(True)
        if self.needs_gen():
            return(False)
        for procgen in self.processing_gens:
            if not procgen.is_completed() and not procgen.is_faulted():
                return(False)
        return(True)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
            "restarted": 0,
        }
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["finished"] += 1
            elif procgen.is_faulted():
                ret_dict["restarted"] += 1
            else:
                ret_dict["processing"] += 1
        return(ret_dict)

    def get_queued_things(self):
        '''The things still queued to be generated for this waiting prompt'''
        return(round(self.things * self.n/thing_divisor,2))

    def get_status(self, lite = False):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        # This might still happen due to a race condition on parallel requests. Not sure how to avoid it.
        if ret_dict["waiting"] < 0:
            logger.error("Request was popped more times than requested!")
            ret_dict["waiting"] = 0
        ret_dict["done"] = self.is_completed()
        ret_dict["faulted"] = self.faulted
        # Lite mode does not include the generations, to spare me download size
        if not lite:
            ret_dict["generations"] = []
            for procgen in self.processing_gens:
                if procgen.is_completed():
                    ret_dict["generations"].append(procgen.get_details())
        queue_pos, queued_things, queued_n = self.get_own_queue_stats()
        # We increment the priority by 1, because it starts at -1
        # This means when all our requests are currently processing or done, with nothing else in the queue, we'll show queue position 0 which is appropriate.
        ret_dict["queue_position"] = queue_pos + 1
        active_workers = database.count_active_workers()
        # If there's less requests than the number of active workers
        # Then we need to adjust the parallelization accordingly
        if queued_n < active_workers:
            active_workers = queued_n
        avg_things_per_sec = (stats.get_request_avg() / thing_divisor) * active_workers
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
        ret_dict["is_possible"] = self.has_valid_workers()
        return(ret_dict)

    def get_lite_status(self):
        '''Same as get_status(), but without the images to avoid unnecessary size'''
        ret_dict = self.get_status(True)
        return(ret_dict)

    def get_own_queue_stats(self):
        '''Get out position in the working prompts queue sorted by kudos
        If this gen is completed, we return (-1,-1) which represents this, to avoid doing operations.
        '''
        if self.needs_gen():
            return(database.get_wp_queue_stats(self))
        return(-1,0,0)

    def record_usage(self, raw_things, kudos):
        '''Record that we received a requested generation and how much kudos it costs us
        We use 'thing' here as we do not care what type of thing we're recording at this point
        This avoids me having to extend this just to change a var name
        '''
        self.user.record_usage(raw_things, kudos)
        self.consumed_kudos = round(self.consumed_kudos + kudos,2)
        self.refresh()

    def log_faulted_job(self):
        '''Extendable function to log why a request was aborted'''
        logger.warning(f"Faulting waiting prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")

    def delete(self):
        for gen in self.processing_gens:
            if not self.faulted and not gen.fake:
                gen.cancel()
            gen.delete()
        db.session.delete(self)
        db.session.commit()

    def abort_for_maintenance(self):
        '''sets all waiting requests to 0, so that all clients pick them up once the client gen is completed'''
        if self.is_completed():
            return
        self.n = 0
        db.session.commit()

    def refresh(self):
        self.last_process_time = datetime.utcnow()
        db.session.commit()

    def is_stale(self):
        if (datetime.utcnow() - self.last_process_time).seconds > self.STALE_TIME:
            return(True)
        return(False)

    def get_priority(self):
        return(self.user.kudos + self.extra_priority)

    def set_job_ttl(self):
        '''Returns how many seconds each job request should stay waiting before considering it stale and cancelling it
        This function should be overriden by the invididual hordes depending on how the calculating ttl
        '''
        self.job_ttl = 150
        db.session.commit()

    def has_valid_workers(self):
        worker_found = False
        for worker in database.get_active_workers():
            if len(self.workers) and worker not in self.workers:
                continue
            if worker.can_generate(self)[0]:
                worker_found = True
                break
        return(worker_found)