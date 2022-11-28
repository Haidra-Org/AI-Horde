from datetime import datetime

from horde.utils import get_db_uuid
from horde.logger import logger
from horde.flask import db


class ProcessingGeneration(db.Model):
    """For storing processing generations in the DB"""
    __tablename__ = "processing_gens"
    # id = db.Column(db.String(36), primary_key=True, default=get_db_uuid)  # Whilst using sqlite use this, as it has no uuid type
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # Then move to this
    generation = db.Column(db.Text)

    model = db.Column(db.String(40), default='', nullable=False)
    seed = db.Column(db.Integer, default=0, nullable=False)
    start_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cancelled = db.Column(db.Boolean, default=False, nullable=False)
    faulted = db.Column(db.Boolean, default=False, nullable=False)
    fake = db.Column(db.Boolean, default=False, nullable=False)

    wp_id = db.Column(db.String(36), db.ForeignKey("waiting_prompts.id"), nullable=False)
    wp = db.relationship("WaitingPromptExtended", back_populates="processing_gens")
    worker_id = db.Column(db.String(36), db.ForeignKey("workers.id"), nullable=False)
    worker = db.relationship("WorkerExtended", back_populates="processing_gens")
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If there has been no explicit model requested by the user, we just choose the first available from the worker
        db.session.add(self)
        db.session.commit()
        logger.debug([kwargs['worker_id'],self.worker])
        worker_models = self.worker.get_model_names()
        if len(worker_models):
            self.model = worker_models[0]
        else:
            self.model = ''
        # If we reached this point, it means there is at least 1 matching model between worker and client
        # so we pick the first one.
        for model in self.wp.get_model_names():
            if model in worker_models:
                self.model = model
        db.session.commit()

    # We allow the seed to not be sent
    def set_generation(self, generation, things_per_sec, **kwargs):
        if self.is_completed() or self.is_faulted():
            return(0)
        self.generation = generation
        # Support for two typical properties 
        self.seed = kwargs.get('seed', None)
        kudos = self.get_gen_kudos()
        self.cancelled = False
        self.record(things_per_sec, kudos)
        db.session.commit()
        return(kudos)
        

    def cancel(self):
        '''Cancelling requests in progress still rewards/burns the relevant amount of kudos'''
        if self.is_completed() or self.is_faulted():
            return
        self.faulted = True
        # We  don't want cancelled requests to raise suspicion
        things_per_sec = self.worker.get_performance_average()
        kudos = self.get_gen_kudos()
        self.cancelled = True
        self.record(things_per_sec,kudos)
        db.session.commit()
        return(kudos)
    
    def record(self, things_per_sec, kudos):
        cancel_txt = ""
        if self.cancelled:
            cancel_txt = " Cancelled"
        if self.fake and self.worker.user == self.wp.user:
            # We do not record usage for paused workers, unless the requestor was the same owner as the worker
            self.worker.record_contribution(raw_things = self.wp.things, kudos = kudos, things_per_sec = things_per_sec)
            logger.info(f"Fake{cancel_txt} Generation worth {self.kudos} kudos, delivered by worker: {self.worker.name}")
        else:
            self.worker.record_contribution(raw_things = self.wp.things, kudos = kudos, things_per_sec = things_per_sec)
            self.wp.record_usage(raw_things = self.wp.things, kudos = kudos)
            logger.info(f"New{cancel_txt} Generation worth {kudos} kudos, delivered by worker: {self.worker.name}")

    def abort(self):
        '''Called when this request needs to be stopped without rewarding kudos. Say because it timed out due to a worker crash'''
        if self.is_completed() or self.is_faulted():
            return        
        self.faulted = True
        self.worker.log_aborted_job()
        self.log_aborted_generation()
        db.session.commit()
        
    def log_aborted_generation(self):
        logger.info(f"Aborted Stale Generation {self.id} from by worker: {self.worker.name} ({self.worker.id})")

    # Overridable function
    def get_gen_kudos(self):
        return self.wp.kudos
        # return(database.convert_things_to_kudos(self.wp.things, seed = self.seed, model_name = self.model))

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def is_faulted(self):
        if self.faulted:
            return(True)
        return(False)

    def is_stale(self, ttl):
        if self.is_completed() or self.is_faulted():
            return(False)
        if (datetime.utcnow() - self.start_time).seconds > ttl:
            return(True)
        return(False)

    def delete(self):
        db.session.delete(self)
        db.session.commit()

    def get_seconds_needed(self):
        return(self.wp.things / self.worker.get_performance_average())

    def get_expected_time_left(self):
        if self.is_completed():
            return(0)
        seconds_needed = self.get_seconds_needed()
        seconds_elapsed = (datetime.utcnow() - self.start_time).seconds
        expected_time = seconds_needed - seconds_elapsed
        # In case we run into a slow request
        if expected_time < 0:
            expected_time = 0
        return(expected_time)

    # This should be extended by every horde type
    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "gen": self.generation,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
        }
        return(ret_dict)
