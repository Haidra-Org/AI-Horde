import uuid
import json

from datetime import datetime, timedelta
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy import Enum, JSON, func, or_

from horde.logger import logger
from horde.flask import db, SQLITE_MODE
from horde import vars as hv
from horde.utils import get_expiry_date, get_interrogation_form_expiry_date, get_db_uuid
from horde.enums import State
from horde.horde_redis import horde_r


uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)
json_column_type = JSONB if not SQLITE_MODE else JSON


class InterrogationForms(db.Model):
    """For storing the details of each image interrogation form"""
    __tablename__ = "interrogation_forms"
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid) 
    i_id = db.Column(uuid_column_type(), db.ForeignKey("interrogations.id", ondelete="CASCADE"), nullable=False)
    interrogation = db.relationship(f"Interrogation", back_populates="forms")
    name = db.Column(db.String(30), nullable=False)
    state = db.Column(Enum(State), default=State.WAITING, nullable=False, index=True) 
    payload = db.Column(json_column_type, default=None)
    result = db.Column(json_column_type, default=None)
    kudos = db.Column(db.Float, default=1, nullable=False)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), default=None, nullable=True)
    worker = db.relationship("InterrogationWorker", back_populates="processing_forms")
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    initiated =  db.Column(db.DateTime, default=None)
    expiry = db.Column(db.DateTime, default=None, index=True)
    abort_count = db.Column(db.Integer, default=0, nullable=False)

    def pop(self, worker):
        myself_refresh = db.session.query(
            InterrogationForms
        ).filter(
            InterrogationForms.id == self.id, 
            InterrogationForms.state == State.WAITING
        ).with_for_update().first()
        if not myself_refresh:
            return None
        myself_refresh.state = State.PROCESSING
        db.session.commit()
        self.expiry = get_interrogation_form_expiry_date()
        self.initiated = datetime.utcnow()
        self.worker_id = worker.id
        # This also commits
        self.interrogation.refresh()
        return {
            "id": self.id,
            "form": self.name,
            "payload": self.payload,
            "source_image": self.interrogation.source_image,
        }
    
    def deliver(self, result, state):
        if self.state != State.PROCESSING:
            return(0)
        if state == "faulted":
            self.abort()
            return(-1)
        # If the image was not sent as b64, we cache its origin url and result so we save on compute
        if not self.interrogation.r2stored:
            horde_r.setex(f'{self.name}_{self.interrogation.source_image}', timedelta(days=5), json.dumps(result))
        self.result = result
        self.state = State.DONE
        self.record(self.kudos)
        db.session.commit()
        return(self.kudos)

    def cancel(self):
        if self.state != State.DONE:
            self.result = None
            self.state = State.CANCELLED
        if self.state == State.PROCESSING:
            self.record(self.kudos)
        db.session.commit()
        return(self.kudos)

    def record(self, kudos):
        cancel_txt = ""
        if self.state == State.CANCELLED:
            cancel_txt = " CANCELLED"
        self.worker.record_interrogation(kudos = self.kudos, seconds_taken = (datetime.utcnow() - self.initiated).seconds)
        self.interrogation.record_usage(kudos = self.kudos + 1)
        logger.info(f"New{cancel_txt} Form {self.id} ({self.name}) worth {self.kudos} kudos, delivered by worker: {self.worker.name} for interrogation {self.interrogation.id}")


    def abort(self):
        '''Called when this request needs to be stopped without rewarding kudos. Say because it timed out due to a worker crash'''
        if self.state != State.PROCESSING:
            return
        self.worker.log_aborted_job()
        self.log_aborted_interrogation()
        # If it aborted 3 or more times, we consider there's something wrong with its payload and permanently fault it
        if self.abort_count > 2:
            self.state = State.FAULTED
        else:
            # We return it to WAITING to let another worker pick it up
            self.expiry = None
            self.state = State.WAITING
            self.abort_count += 1
        db.session.commit()
        
    def log_aborted_interrogation(self):
        logger.info(f"Aborted Stale Interrogation {self.id} ({self.name}) from by worker: {self.worker.name} ({self.worker.id})")

    def is_completed(self):
        return self.state == State.DONE

    def is_faulted(self):
        return self.state == State.FAULTED

    def is_waiting(self):
        return self.state == State.WAITING

    def is_stale(self, ttl):
        if self.state in [State.FAULTED, State.CANCELLED, State.DONE]:
            return False
        return datetime.utcnow() > self.expiry

    def delete(self):
        db.session.delete(self)
        db.session.commit()


class Interrogation(db.Model):
    """For storing the request for interrogating an image"""
    __tablename__ = "interrogations"
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid) 
    source_image = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User", back_populates="interrogations")
    ipaddr = db.Column(db.String(39))  # ipv6
    safe_ip = db.Column(db.Boolean, default=False, nullable=False)
    trusted_workers = db.Column(db.Boolean, default=False, nullable=False)
    # This is used so I know to delete up the image 30 mins after this request expires
    r2stored = db.Column(db.Boolean, default=False, nullable=False)
    expiry = db.Column(db.DateTime, default=get_expiry_date, index=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    extra_priority = db.Column(db.Integer, default=0, nullable=False, index=True)
    forms = db.relationship("InterrogationForms", back_populates="interrogation", cascade="all, delete-orphan")


    def __init__(self, forms, *args, **kwargs):
        super().__init__(*args, **kwargs)
        db.session.add(self)
        db.session.commit()
        self.extra_priority = self.user.kudos
        self.set_forms(forms)


    def set_source_image(self, source_image, r2stored):
        self.source_image = source_image
        self.r2stored = r2stored
        if not r2stored:
            for form in self.forms:
                self.check_cache(form, source_image)
        db.session.commit()

    def check_cache(self, form, source_image):
        '''Checks if the image is already in the redis cache. 
        If it is, it sets the cached forms to DONE and sets the cached value as its result
        '''
        cached_result = horde_r.get(f'{form.name}_{source_image}')
        # The entry might be False, so we need to check explicitly against None
        if cached_result != None:
            form.result = json.loads(cached_result)
            form.state = State.DONE

    def refresh(self):
        self.expiry = get_expiry_date()
        db.session.commit()

    def is_stale(self):
        if datetime.utcnow() > self.expiry:
            return(True)
        return(False)

    def set_forms(self, forms = None):
        if not forms: forms = []
        seen_names = []
        for form in forms:
            # We don't allow the same interrogation type twice
            if form["name"] in seen_names:
                continue
            kudos = 1
            # Interrogations are more intensive so they reward better
            if form["name"] == "interrogation":
                kudos = 3
            form_entry = InterrogationForms(
                name=form["name"],
                payload=form.get("payload"),
                i_id=self.id,
                kudos = kudos, #TODO: Adjust the kudos cost per interrogation
            )
            db.session.add(form_entry)
        db.session.commit()

    def get_form_names(self):
        return [f.name for f in self.forms]

    def start_interrogation(self, worker):
        # We have to do this to lock the row for updates, to ensure we don't have racing conditions on who is picking up requests
        myself_refresh = db.session.query(Interrogation).filter(Interrogation.id == self.id, Interrogation.n > 0).with_for_update().first()
        if not myself_refresh:
            return None
        myself_refresh.n -= 1
        db.session.commit()
        worker_id = worker.id
        self.refresh()
        logger.audit(f"Interrogation with ID {self.id} popped by worker {worker.id} ('{worker.name}' / {worker.ipaddr})")
        return self.get_pop_payload()


    def get_pop_payload(self):
        interrogation_payload = {
            "id": self.id,
            "source_image": self.source_image,
            "forms": self.get_form_names(),
        }
        return(interrogation_payload)

    def needs_interrogation(self):
        return any(not form.is_completed() for form in self.forms)

    def is_completed(self):
        if self.FAULTED:
            return True
        if self.needs_interrogation():
            return False
        return True


    def get_status(
            self, 
        ):
        ret_dict = {
            "state": State.PARTIAL.name.lower(),
            "forms": [],
        }
        all_faulted = True
        all_done = True
        processing = False
        found_waiting = False
        for form in self.forms:
            form_dict = {
                "form": form.name,
                "state": form.state.name.lower(),
                "result": form.result,
            }
            ret_dict["forms"].append(form_dict)
            if form.state != State.FAULTED:
                all_faulted = False
            if form.state != State.DONE:
                all_done = False
            if form.state == State.PROCESSING:
                processing = True
            if form.state == State.WAITING:
                found_waiting = True
        if all_faulted:
            ret_dict["state"] = State.FAULTED.name.lower()
        elif all_done:
            ret_dict["state"] = State.DONE.name.lower()
        elif processing:
            ret_dict["state"] = State.PROCESSING.name.lower()
        elif found_waiting:
            ret_dict["state"] = State.WAITING.name.lower()
        return(ret_dict)

    def record_usage(self, kudos):
        '''Record that we received a requested interrogation and how much kudos it costs us
        '''
        self.user.record_usage(0, kudos, "interrogation")
        self.refresh()
    
    def cancel(self):
        for form in self.forms:
            form.cancel()
        
    def delete(self):
        if not self.is_completed():
            self.cancel()
        db.session.delete(self)
        db.session.commit()
