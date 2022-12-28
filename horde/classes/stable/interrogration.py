import uuid

from datetime import datetime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import func, or_
from enum import Enum

from horde.logger import logger
from horde.flask import db, SQLITE_MODE
from horde.vars import thing_divisor
from horde.utils import get_expiry_date, get_interrogation_form_expiry_date


uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)
json_column_type = JSONB if not SQLITE_MODE else JSON

class State(Enum):
    Waiting = 0
    Processing = 1
    Done = 2
    Faulted = 3



class InterrogationsForms(db.Model):
    """For storing the details of each image interrogation form"""
    __tablename__ = "interrogation_forms"
    id = db.Column(db.Integer, primary_key=True)
    i_id = db.Column(uuid_column_type(), db.ForeignKey("interrogations.id", ondelete="CASCADE"), nullable=False)
    interrogation = db.relationship(f"Interrogation", back_populates="forms")
    name = db.Column(db.String(30), nullable=False)
    state = db.Column(Enum(State), default=0, nullable=False) 
    payload = db.Column(json_column_type, default=None)
    result = db.Column(json_column_type, default=None)
    worker_id = db.Column(db.Integer, db.ForeignKey("workers.id"))
    worker = db.relationship("WorkerExtended", back_populates="interrogation_forms")
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expiry = db.Column(db.DateTime, default=None, index=True)

    def pop(self):
        self.expiry = get_interrogation_form_expiry_date()
        db.session.commit()


class Interrogation(db.Model):
    """For storing the request for interrogating an image"""
    __tablename__ = "interrogations"
    id = db.Column(uuid_column_type(), primary_key=True, default=uuid.uuid4) 
    source_image = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="interrogations")
    ipaddr = db.Column(db.String(39))  # ipv6
    safe_ip = db.Column(db.Boolean, default=False, nullable=False)
    trusted_workers = db.Column(db.Boolean, default=False, nullable=False)
    r2stored = db.Column(db.Boolean, default=False, nullable=False)
    expiry = db.Column(db.DateTime, default=get_expiry_date, index=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    forms = db.relationship("InterrogationsForms", back_populates="interrogation", cascade="all, delete-orphan")


    def __init__(self, forms, *args, **kwargs):
        super().__init__(*args, **kwargs)
        db.session.add(self)
        db.session.commit()
        self.set_forms(forms)


    def set_source_image(self, source_image):
        self.source_image = source_image
        db.session.commit()


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
            form_entry = InterrogationsForms(
                name=form["name"],
                payload=form.get("payload"),
                i_id=self.id
            )
            db.session.add(form_entry)

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
        return any(form.result == None for form in self.form)

    def is_completed(self):
        if self.faulted:
            return True
        if self.needs_interrogation():
            return False
        return True


    def get_status(
            self, 
        ):
        ret_dict = {
            "state": State.Waiting,
            "forms": {},
        }
        all_faulted = True
        all_done = True
        processing = False
        for form in self.forms:
            ret_dict["forms"][form.name] = form.result
            if form.state != State.Faulted:
                all_faulted = False
            if form.state != State.Done:
                all_done = False
            if form.state == State.Processing:
                processing = True
            ret_dict["forms"][form.state] = ret_dict.get(form.state,0) + 1
        if all_faulted:
            ret_dict["state"] = State.Faulted
        elif all_done:
            ret_dict["state"] = State.Done
        elif processing:
            ret_dict["state"] = State.Processing
        return(ret_dict)
