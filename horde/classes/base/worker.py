import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime

from horde.classes.base.waiting_prompt import WPModels
from horde.logger import logger
from horde.argparser import raid
from horde.flask import db, SQLITE_MODE
from horde.vars import thing_name, thing_divisor, things_per_sec_suspicion_threshold
from horde.suspicions import SUSPICION_LOGS, Suspicions
from horde.utils import is_profane, get_db_uuid, sanitize_string


uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)

class WorkerStats(db.Model):
    __tablename__ = "worker_stats"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"WorkerExtended", back_populates="stats")
    action = db.Column(db.String(20), nullable=False, index=True)
    value = db.Column(db.BigInteger, default=0, nullable=False)

class WorkerPerformance(db.Model):
    __tablename__ = "worker_performances"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"WorkerExtended", back_populates="performance")
    performance = db.Column(db.Float, primary_key=False)
    created = db.Column(db.DateTime, default=datetime.utcnow) # TODO maybe index here, but I'm not sure how big this table is

class WorkerBlackList(db.Model):
    __tablename__ = "worker_blacklists"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"WorkerExtended", back_populates="blacklist")
    word = db.Column(db.String(20), primary_key=False)

class WorkerSuspicions(db.Model):
    __tablename__ = "worker_suspicions"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"WorkerExtended", back_populates="suspicions")
    suspicion_id = db.Column(db.Integer, primary_key=False)

class WorkerModel(db.Model):
    __tablename__ = "worker_models"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"WorkerExtended", back_populates="models")
    model = db.Column(db.String(50))  # TODO model should be a foreign key to a model table

class WorkerTemplate(db.Model):
    __tablename__ = "workers"
    __mapper_args__ = {
        "polymorphic_identity": "worker_template",
        "polymorphic_on": "worker_type",
    }    
    suspicion_threshold = 3
    # Every how many seconds does this worker get a kudos reward
    uptime_reward_threshold = 600
    default_maintenance_msg = "This worker has been put into maintenance mode by its owner"

    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    worker_type = db.Column(db.String(30), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="workers")
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    info = db.Column(db.String(1000))
    ipaddr = db.Column(db.String(15))
    created = db.Column(db.DateTime, default=datetime.utcnow)

    last_check_in = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_aborted_job = db.Column(db.DateTime, default=datetime.utcnow)

    kudos = db.Column(db.BigInteger, default=0, nullable=False)
    contributions = db.Column(db.BigInteger, default=0, nullable=False, index=True)
    fulfilments = db.Column(db.Integer, default=0, nullable=False)
    aborted_jobs = db.Column(db.Integer, default=0, nullable=False)
    uncompleted_jobs = db.Column(db.Integer, default=0, nullable=False)
    uptime = db.Column(db.BigInteger, default=0, nullable=False)
    threads = db.Column(db.Integer, default=1, nullable=False)
    bridge_version = db.Column(db.Integer, default=1, nullable=False)
    last_reward_uptime = db.Column(db.BigInteger, default=0, nullable=False)

    paused = db.Column(db.Boolean, default=False, nullable=False)
    maintenance = db.Column(db.Boolean, default=False, nullable=False)
    maintenance_msg = db.Column(db.String(300), unique=False, default=default_maintenance_msg, nullable=False)
    team_id = db.Column(uuid_column_type(), db.ForeignKey("teams.id"), default=None)
    team = db.relationship("Team", back_populates="workers")

    allow_unsafe_ipaddr = db.Column(db.Boolean, default=True, nullable=False)

    stats = db.relationship("WorkerStats", back_populates="worker", cascade="all, delete-orphan")
    performance = db.relationship("WorkerPerformance", back_populates="worker", cascade="all, delete-orphan")
    suspicions = db.relationship("WorkerSuspicions", back_populates="worker", cascade="all, delete-orphan")

    require_upfront_kudos = False
    prioritized_users = []

    def create(self, **kwargs):
        self.check_for_bad_actor()
        db.session.add(self)
        db.session.commit()
        if self.is_suspicious():
            pass
            # TODO: Doesn't work
            # db.delete(self)

    def check_for_bad_actor(self):
        # Each worker starts at the suspicion level of its user
        if len(self.name) > 100:
            if len(self.name) > 200:
                self.report_suspicion(reason = Suspicions.WORKER_NAME_EXTREMELY_LONG)
            self.name = self.name[:100]
            self.report_suspicion(reason = Suspicions.WORKER_NAME_LONG)
        if is_profane(self.name):
            self.report_suspicion(reason = Suspicions.WORKER_PROFANITY, formats = [self.name])

    def report_suspicion(self, amount = 1, reason = Suspicions.WORKER_PROFANITY, formats = None):
        if not formats: formats = []
        # Unreasonable Fast can be added multiple times and it increases suspicion each time
        if int(reason) in self.suspicions and reason not in [Suspicions.UNREASONABLY_FAST,Suspicions.TOO_MANY_JOBS_ABORTED]:
            return
        new_suspicion = WorkerSuspicions(worker_id=self.id, suspicion_id=int(reason))
        self.user.report_suspicion(amount, reason, formats)
        if reason:
            reason_log = SUSPICION_LOGS[reason].format(*formats)
            logger.warning(f"Worker '{self.id}' suspicion increased. Reason: {reason_log}")
        if self.is_suspicious():
            self.paused = True
        db.session.commit()

    def reset_suspicion(self):
        '''Clears the worker's suspicion and resets their reasons'''
        db.session.query(WorkerSuspicions).filter_by(worker_id=self.id).delete()
        db.session.commit()   

    def get_suspicion(self):
        return(len(self.suspicions))

    def is_suspicious(self):
        # Trusted users are never suspicious
        if self.user.trusted:
            return(False)       
        if self.get_suspicion() >= self.suspicion_threshold:
            return(True)
        return(False)

    def set_name(self,new_name):
        if self.name == new_name:
            return("OK")        
        if is_profane(new_name):
            return("Profanity")
        if len(new_name) > 100:
            return("Too Long")
        self.name = sanitize_string(new_name)
        db.session.commit()   
        return("OK")

    def set_info(self,new_info):
        if self.info == new_info:
            return("OK")
        if is_profane(new_info):
            return("Profanity")
        if len(new_info) > 1000:
            return("Too Long")
        self.info = sanitize_string(new_info)
        db.session.commit()   
        return("OK")

    def set_team(self,new_team):
        self.team_id = new_team.id
        db.session.commit()   
        return("OK")

    # This should be overwriten by each specific horde
    def calculate_uptime_reward(self):
        return(100)

    def toggle_maintenance(self, is_maintenance_active, maintenance_msg = None):
        self.maintenance = is_maintenance_active
        self.maintenance_msg = self.default_maintenance_msg
        if self.maintenance and maintenance_msg is not None:
            self.maintenance_msg = sanitize_string(maintenance_msg)
        db.session.commit()   

    def toggle_paused(self, is_paused_active):
        self.paused = is_paused_active
        db.session.commit()   

    # This should be extended by each worker type
    def check_in(self, **kwargs):
        self.ipaddr = kwargs.get("ipaddr", None)
        self.bridge_version = kwargs.get("bridge_version", 1)
        self.threads = kwargs.get("threads", 1)
        self.require_upfront_kudos = kwargs.get('require_upfront_kudos', False)
        self.allow_unsafe_ipaddr = kwargs.get('allow_unsafe_ipaddr', True)
        # If's OK to provide an empty list here as we don't actually modify this var
        # We only check it in can_generate
        self.prioritized_users = kwargs.get('prioritized_users', [])
        if not kwargs.get("safe_ip", True):
            if not self.user.trusted:
                self.report_suspicion(reason = Suspicions.UNSAFE_IP)
        if not self.is_stale() and not self.paused and not self.maintenance:
            self.uptime += (datetime.utcnow() - self.last_check_in).seconds
            # Every 10 minutes of uptime gets 100 kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                if self.team:
                    self.team.record_uptime(self.uptime_reward_threshold)
                kudos = self.calculate_uptime_reward()
                self.modify_kudos(kudos,'uptime')
                self.user.record_uptime(kudos)
                logger.debug(f"Worker '{self.name}' received {kudos} kudos for uptime of {self.uptime_reward_threshold} seconds.")
                self.last_reward_uptime = self.uptime
        else:
            # If the worker comes back from being stale, we just reset their last_reward_uptime
            # So that they have to stay up at least 10 mins to get uptime kudos
            self.last_reward_uptime = self.uptime
        self.last_check_in = datetime.utcnow()
        db.session.commit()

    def get_human_readable_uptime(self):
        if self.uptime < 60:
            return(f"{self.uptime} seconds")
        elif self.uptime < 60*60:
            return(f"{round(self.uptime/60,2)} minutes")
        elif self.uptime < 60*60*24:
            return(f"{round(self.uptime/60/60,2)} hours")
        else:
            return(f"{round(self.uptime/60/60/24,2)} days")

    # We split it to its own function to make it extendable
    def convert_contribution(self,raw_things):
        converted = round(raw_things/thing_divisor,2)
        self.contributions = round(self.contributions + converted,2)
        # We reurn the converted amount as well in case we need it
        return(converted)

    @logger.catch(reraise=True)
    def record_contribution(self, raw_things, kudos, things_per_sec):
        '''We record the servers newest contribution
        We do not need to know what type the contribution is, to avoid unnecessarily extending this method
        '''
        self.user.record_contributions(raw_things = raw_things, kudos = kudos)
        self.modify_kudos(kudos,'generated')
        converted_amount = self.convert_contribution(raw_things)
        self.fulfilments += 1
        if self.team:
            self.team.record_contribution(converted_amount, kudos)
        performances = db.session.query(WorkerPerformance).filter_by(worker_id=self.id).order_by(WorkerPerformance.created.asc())
        if performances.count() >= 20:
            db.session.delete(performances.first())
        new_performance = WorkerPerformance(worker_id=self.id, performance=things_per_sec)
        db.session.add(new_performance)
        db.session.commit()
        if things_per_sec / thing_divisor > things_per_sec_suspicion_threshold:
            self.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[round(things_per_sec / thing_divisor,2)])

    def modify_kudos(self, kudos, action = 'generated'):
        self.kudos = round(self.kudos + kudos, 2)
        kudos_details = db.session.query(WorkerStats).filter_by(worker_id=self.id).filter_by(action=action).first()
        if not kudos_details:
            kudos_details = WorkerStats(worker_id=self.id,action=action, value=round(kudos, 2))
            db.session.add(kudos_details)
            db.session.commit()
        else:
            kudos_details.value = round(kudos_details.value + kudos, 2)
            db.session.commit()
        logger.trace([kudos_details,kudos_details.value])

    def log_aborted_job(self):
        # We count the number of jobs aborted in an 1 hour period. So we only log the new timer each time an hour expires.
        if (datetime.utcnow() - self.last_aborted_job).seconds > 3600:
            self.aborted_jobs = 0
            self.last_aborted_job = datetime.utcnow()
        self.aborted_jobs += 1
        # These are accumulating too fast at 5. Increasing to 20
        dropped_job_threshold = 20
        if raid.active:
            dropped_job_threshold = 10
        # Avoiding putting into maintenance interrogation workers due to crashes from the model
        # TODO: Remove once crashes are fixed
        if self.worker_type == "interrogation_worker":
            dropped_job_threshold = 5000
        if self.aborted_jobs > dropped_job_threshold:
            # if a worker drops too many jobs in an hour, we put them in maintenance
            # except during a raid, as we don't want them to know we detected them.
            if not raid.active:
                self.toggle_maintenance(
                    True, 
                    "Maintenance mode activated because worker is dropping too many jobs."
                    "Please investigate if your performance has been impacted and consider reducing your max_power or your max_threads"
                )
            self.report_suspicion(reason = Suspicions.TOO_MANY_JOBS_ABORTED)
            self.aborted_jobs = 0
        self.uncompleted_jobs += 1
        db.session.commit()

    def get_performance_average(self):
        performances = [p.performance for p in self.performance]
        if len(performances):
            ret_num = sum(performances) / len(performances)
        else:
            # Always sending at least 1 thing per second, to avoid divisions by zero
            ret_num = 1
        return(ret_num)

    def get_performance(self):
        performances = [p.performance for p in self.performance]
        if len(performances):
            ret_str = f'{round(sum(performances) / len(performances) / thing_divisor,1)} {thing_name} per second'
        else:
            ret_str = f'No requests fulfilled yet'
        return(ret_str)

    def is_stale(self):
        try:
            if (datetime.utcnow() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new worker, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    def delete(self):
        for stat in self.stats:
            db.session.delete(stat)    
        for performance in self.performance:
            db.session.delete(performance)
        for suspicion in self.suspicions:
            db.session.delete(suspicion)
        db.session.delete(self)
        db.session.commit()

    def get_kudos_details(self):
        kudos_details = db.session.query(WorkerStats).filter_by(worker_id=self.id).all()
        ret_dict = {}
        for kd in kudos_details:
            ret_dict[kd.action] = kd.value
        return ret_dict

    def import_kudos_details(self, kudos_details):
        for key in kudos_details:
            new_kd = WorkerStats(worker_id=self.id, action=key, value=kudos_details[key])
            db.session.add(new_kd)
        db.session.commit()

    def import_performances(self, performances):
        for p in performances:
            new_kd = WorkerPerformance(worker_id=self.id, performance=p)
            db.session.add(new_kd)
        db.session.commit()

    def import_suspicions(self, suspicions):
        for s in suspicions:
            new_suspicion = WorkerSuspicions(worker_id=self.id, suspicion_id=int(s))
            db.session.add(new_suspicion)
        db.session.commit()

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        ret_dict = {
            "name": self.name,
            "id": str(self.id),
            "type": self.worker_type,
            "requests_fulfilled": self.fulfilments,
            "uncompleted_jobs": self.uncompleted_jobs,
            "kudos_rewards": self.kudos,
            "kudos_details": self.get_kudos_details(),
            "performance": self.get_performance(),
            "threads": self.threads,
            "uptime": self.uptime,
            "maintenance_mode": self.maintenance,
            "info": self.info,
            "trusted": self.user.trusted,
            "online": not self.is_stale(),
            "team": {"id": str(self.team.id),"name": self.team.name} if self.team else 'None',
        }
        if details_privilege >= 2:
            ret_dict['paused'] = self.paused
            ret_dict['suspicious'] = len(self.suspicions)
        if details_privilege >= 1 or self.user.public_workers:
            ret_dict['owner'] = self.user.get_unique_alias()
            ret_dict['contact'] = self.user.contact
        return(ret_dict)


class Worker(WorkerTemplate):
    '''A worker is meant to receive a text prompt and pass it though a generative model'''
    __mapper_args__ = {
        "polymorphic_identity": "worker",
    }    
    nsfw = db.Column(db.Boolean, default=False, nullable=False)
    
    blacklist = db.relationship("WorkerBlackList", back_populates="worker", cascade="all, delete-orphan")
    models = db.relationship("WorkerModel", back_populates="worker", cascade="all, delete-orphan")
    processing_gens = db.relationship("ProcessingGenerationExtended", back_populates="worker")

    # This should be extended by each specific horde
    def check_in(self, **kwargs):
        super().check_in(**kwargs)
        self.set_models(kwargs.get("models"))
        self.nsfw = kwargs.get("nsfw", True)
        self.set_blacklist(kwargs.get("blacklist", []))
        db.session.commit()    

    def set_blacklist(self, blacklist):
        # We don't allow more workers to claim they can server more than 50 models atm (to prevent abuse)
        blacklist = [sanitize_string(word) for word in blacklist]
        del blacklist[200:]
        blacklist = set(blacklist)
        existing_blacklist = db.session.query(WorkerBlackList).filter_by(worker_id=self.id)
        existing_blacklist_words = set([b.word for b in existing_blacklist.all()])
        if existing_blacklist_words == blacklist:
            return
        existing_blacklist.delete()
        for word in blacklist:
            blacklisted_word = WorkerBlackList(worker_id=self.id,word=word[0:15])
            db.session.add(blacklisted_word)
        db.session.commit()

    def get_model_names(self):
        model_names = db.session.query(func.distinct(WorkerModel.model).label('name')).filter(WorkerModel.worker_id == self.id).all()
        return [m.name for m in model_names]

    def set_models(self, models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        models = [sanitize_string(model_name[0:100]) for model_name in models]
        del models[100:]
        models = set(models)
        existing_models = db.session.query(WorkerModel).filter_by(worker_id=self.id)
        existing_model_names = set([m.model for m in existing_models.all()])
        if existing_model_names == models:
            return
        existing_models.delete()
        for model_name in models:
            model = WorkerModel(worker_id=self.id,model=model_name)
            db.session.add(model)
        db.session.commit()


    def can_generate(self, waiting_prompt):
        '''Takes as an argument a WaitingPrompt class and checks if this worker is valid for generating it'''
        # Workers in maintenance are still allowed to generate for their owner
        if self.maintenance and waiting_prompt.user != self.user:
            return [False, None]
        #logger.warning(datetime.utcnow())
        if self.is_stale():
            # We don't consider stale workers in the request, so we don't need to report a reason
            return [False, None]
        #logger.warning(datetime.utcnow())
        if waiting_prompt.nsfw and not self.nsfw:
            return [False, 'nsfw']
        #logger.warning(datetime.utcnow())
        if waiting_prompt.trusted_workers and not self.user.trusted:
            return [False, 'untrusted']
        # If the worker has been tricked once by this prompt, we don't want to resend it it
        # as it may give up the jig
        #logger.warning(datetime.utcnow())
        if waiting_prompt.tricked_worker(self):
            return [False, 'secret']
        #logger.warning(datetime.utcnow())
        if any(b.word.lower() in waiting_prompt.prompt.lower() for b in self.blacklist):
            return [False, 'blacklist']
        # Skips working prompts which require a specific worker from a list, and our ID is not in that list
        if len(waiting_prompt.workers) and self.id not in [wref.worker_id for wref in waiting_prompt.workers]:
            return [False, 'worker_id']
        #logger.warning(datetime.utcnow())

        my_model_names = self.get_model_names()
        wp_model_names = waiting_prompt.get_model_names()
        if len(wp_model_names) > 0:
            found_matching_model = False
            for model_name in my_model_names:
                if model_name in wp_model_names:
                    found_matching_model = True
                    break
            if not found_matching_model:
                return [False, 'model']

        # # I removed this for now as I think it might be blocking requests from generating. I will revisit later again
        # # If the worker is slower than average, and we're on the last quarter of the request, we try to utilize only fast workers
        # if self.get_performance_average() < self.db.stats.get_request_avg() and waiting_prompt.n <= waiting_prompt.jobs/4:
        #   return [False, 'performance']
        return [True,None]

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        ret_dict = super().get_details(details_privilege)
        ret_dict["nsfw"] = self.nsfw
        ret_dict["models"] = self.get_model_names()
        return(ret_dict)

    def delete(self):
        for procgen in self.processing_gens:
            procgen.abort()
        for word in self.blacklist:
            db.session.delete(word)
        for model in self.models:
            db.session.delete(model)
        super().delete()
