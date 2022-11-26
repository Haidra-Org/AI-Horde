import datetime
import uuid
import time
import bleach

from horde import logger
from horde.flask import db
from horde.vars import thing_divisor
from horde.utils import is_profane
from horde.classes.base import User
from horde.classes.base.database import find_workers_by_team


class Team(db.Model):
    __tablename__ = "teams"
    id = db.Column(db.String(36), primary_key=True, default=uuid.uuid4)
    # id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # Then move to this
    info = db.Column(db.String(1000), default='')
    name = db.Column(db.String(100), default='', is_nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    owner = db.relationship("User", backref=db.backref("owner_id", lazy="dynamic"))

    contributions = db.Column(db.Integer, unique=False)
    fulfilments = db.Column(db.Integer, unique=False)
    kudos = db.Column(db.Integer, unique=False)
    uptime = db.Column(db.Integer, unique=False)

    creation_date = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    last_active = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


    def create(self, user):
        self.set_owner(user)
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        db.session.add(self)
        db.session.commit()

    def get_performance(self):
        all_performances = []
        for worker in find_workers_by_team(self):
            if worker.is_stale():
                continue
            all_performances.append(worker.get_performance_average())
        if len(all_performances):
            perf_avg = round(sum(all_performances) / len(all_performances) / thing_divisor,1)
            perf_total = round(sum(all_performances) / thing_divisor,1)
        else:
            perf_avg = 0
            perf_total = 0
        return(perf_avg,perf_total)

    def get_all_models(self):
        all_models = {}
        for worker in find_workers_by_team(self):
            for model_name in worker.get_model_names():
                all_models[model_name] = all_models.get(model_name,0) + 1
        model_list = []
        for model in all_models:
            minfo = {
                "name": model,
                "count": all_models[model]
            }
            model_list.append(minfo)
        return(model_list)

    def set_name(self,new_name):
        if self.name == new_name:
            return("OK")        
        if is_profane(new_name):
            return("Profanity")
        self.name = bleach.clean(new_name)
        existing_team = find_team_by_name(self.name)
        if existing_team and existing_team != self:
            return("Already Exists")
        return("OK")
        db.session.commit()


    def set_info(self, new_info):
        if self.info == new_info:
            return("OK")
        if is_profane(new_info):
            return("Profanity")
        self.info = bleach.clean(new_info)
        return("OK")
        db.session.commit()

    def set_owner(self, new_owner):
        self.user_id = new_owner.id
        db.session.commit()

    def get_owner(self, new_owner):
        return(db.session.query(User).filter_by(user_id=self.user_id).first())

    def delete(self):
        db.session.delete(self)
        for worker in find_workers_by_team(self):
            worker.set_team(None)
        db.session.commit()

    def record_uptime(self, seconds):
        self.uptime += seconds
        self.last_active = datetime.now()
        db.session.commit()
    
    def record_contribution(self, contributions, kudos):
        self.contributions = round(self.contributions + contributions, 2)
        self.fulfilments += 1
        self.kudos = round(self.kudos + kudos, 2)
        self.last_active = datetime.now()
        db.session.commit()

   # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        worker_list = [{"id": worker.id, "name":worker.name, "online": not worker.is_stale()} for worker in find_workers_by_team(self)]
        perf_avg, perf_total = self.get_performance()
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "creator": self.get_owner().get_unique_alias(),
            "contributions": self.contributions,
            "requests_fulfilled": self.fulfilments,
            "kudos": self.kudos,
            "performance": perf_avg,
            "speed": perf_total,
            "uptime": self.uptime,
            "info": self.info,
            "worker_count": len(worker_list),
            "workers": worker_list,
            "models": self.get_all_models(),
        }
        return(ret_dict)