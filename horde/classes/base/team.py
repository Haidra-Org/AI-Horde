import uuid

from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB, UUID

from horde.logger import logger
from horde.flask import db, SQLITE_MODE
from horde.vars import thing_divisor
from horde.utils import is_profane, get_db_uuid, sanitize_string, get_db_uuid

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)

class Team(db.Model):
    __tablename__ = "teams"
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    info = db.Column(db.String(1000), default='')
    name = db.Column(db.String(100), default='', unique=True, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    owner = db.relationship(f"User", back_populates="teams")

    contributions = db.Column(db.BigInteger, default=0, nullable=False)
    fulfilments = db.Column(db.Integer, default=0, nullable=False)
    kudos = db.Column(db.BigInteger, default=0, nullable=False)
    uptime = db.Column(db.BigInteger, default=0, nullable=False)

    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_active = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    workers = db.relationship("Worker", back_populates="team")

    def create(self):
        db.session.add(self)
        db.session.commit()

    def get_performance(self):
        all_performances = []
        for worker in self.workers:
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
        for worker in self.workers:
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
        self.name = sanitize_string(new_name)
        existing_team = find_team_by_name(self.name)
        if existing_team and existing_team != self:
            return("Already Exists")
        db.session.commit()
        return("OK")


    def set_info(self, new_info):
        if self.info == new_info:
            return("OK")
        if is_profane(new_info):
            return("Profanity")
        self.info = sanitize_string(new_info)
        db.session.commit()
        return("OK")

    def set_owner(self, new_owner):
        self.user_id = new_owner.id
        db.session.commit()

    def delete(self):
        db.session.delete(self)
        for worker in self.workers:
            worker.set_team(None)
        db.session.commit()

    def record_uptime(self, seconds):
        self.uptime += seconds
        self.last_active = datetime.utcnow()
        db.session.commit()
    
    def record_contribution(self, contributions, kudos):
        self.contributions = round(self.contributions + contributions, 2)
        self.fulfilments += 1
        self.kudos = round(self.kudos + kudos, 2)
        self.last_active = datetime.utcnow()
        db.session.commit()

   # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        worker_list = [{"id": worker.id, "name":worker.name, "online": not worker.is_stale()} for worker in self.workers]
        perf_avg, perf_total = self.get_performance()
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "creator": self.owner.get_unique_alias(),
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