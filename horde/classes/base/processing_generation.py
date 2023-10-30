import uuid

from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy import JSON
from sqlalchemy.sql import expression
from horde.utils import get_db_uuid
from horde.logger import logger
from horde.flask import db, SQLITE_MODE

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)
json_column_type = JSONB if not SQLITE_MODE else JSON

class ProcessingGeneration(db.Model):
    """For storing processing generations in the DB"""
    __tablename__ = "processing_gens"
    __mapper_args__ = {
        "polymorphic_identity": "template",
        "polymorphic_on": "procgen_type",
    }    
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    procgen_type = db.Column(db.String(30), nullable=False, index=True)
    generation = db.Column(db.Text)
    gen_metadata = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)

    model = db.Column(db.String(255), default='', nullable=False)
    seed = db.Column(db.BigInteger, default=0, nullable=False)
    start_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cancelled = db.Column(db.Boolean, default=False, nullable=False)
    faulted = db.Column(db.Boolean, default=False, nullable=False)
    fake = db.Column(db.Boolean, default=False, nullable=False)
    censored = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.literal(False))

    wp_id = db.Column(uuid_column_type(), db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"), nullable=False)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If there has been no explicit model requested by the user, we just choose the first available from the worker
        db.session.add(self)
        db.session.commit()
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

    def set_generation(self, generation, things_per_sec, **kwargs):
        if self.is_completed():
            return(0)
        # We return -1 to know to send a different error
        if self.is_faulted():
            return(-1)
        self.generation = generation
        # Support for two typical properties 
        self.seed = kwargs.get('seed', None)
        self.gen_metadata = kwargs.get('metadata', None)
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
        things_per_sec = self.worker.speed
        kudos = self.get_gen_kudos()
        self.cancelled = True
        self.record(things_per_sec,kudos)
        db.session.commit()
        return(kudos * self.worker.get_bridge_kudos_multiplier())
    
    def record(self, things_per_sec, kudos):
        cancel_txt = ""
        if self.cancelled:
            cancel_txt = " Cancelled"
        if self.fake and self.worker.user == self.wp.user:
            # We do not record usage for paused workers, unless the requestor was the same owner as the worker
            self.worker.record_contribution(raw_things = self.wp.things, kudos = kudos, things_per_sec = things_per_sec)
            logger.info(f"Fake{cancel_txt} Generation {self.id} worth {self.kudos} kudos, delivered by worker: {self.worker.name} for wp {self.wp.id}")
        else:
            self.worker.record_contribution(raw_things = self.wp.things, kudos = kudos, things_per_sec = things_per_sec)
            self.wp.record_usage(raw_things = self.wp.things, kudos = self.adjust_user_kudos(kudos))
            logger.info(f"New{cancel_txt} Generation {self.id} worth {kudos} kudos, delivered by worker: {self.worker.name} for wp {self.wp.id}")

    def adjust_user_kudos(self, kudos):
        if self.censored:
            return 0
        return kudos

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
        if self.generation is not None:
            return(True)
        return(False)

    def is_faulted(self):
        return self.faulted

    def is_stale(self, ttl):
        if self.is_completed() or self.is_faulted():
            return False
        return (datetime.utcnow() - self.start_time).total_seconds() > ttl

    def delete(self):
        db.session.delete(self)
        db.session.commit()

    def get_seconds_needed(self):
        return(self.wp.things / self.worker.speed)

    def get_expected_time_left(self):
        if self.is_completed():
            return(0)
        seconds_needed = self.get_seconds_needed()
        seconds_elapsed = (datetime.utcnow() - self.start_time).total_seconds()
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
            "metadata": self.metadata,
        }
        return(ret_dict)

    # Extendable function to be able to dynamically adjust the amount of things
    # based on what the worker actually returned. 
    # Typically needed for LLMs using EOS tokens etc
    def get_things_count(self, generation):
        return self.wp.things
