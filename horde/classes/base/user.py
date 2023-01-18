import uuid
import os

import dateutil.relativedelta
from datetime import datetime

from horde.logger import logger
from horde.flask import db
from horde.vars import thing_name, thing_divisor
from horde.suspicions import Suspicions, SUSPICION_LOGS
from horde.utils import is_profane, sanitize_string, generate_client_id
from horde.patreon import patrons


class UserStats(db.Model):
    __tablename__ = "user_stats"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User", back_populates="stats")
    action = db.Column(db.String(20), nullable=False, index=True)
    value = db.Column(db.BigInteger, nullable=False)
    # updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class UserSuspicions(db.Model):
    __tablename__ = "user_suspicions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User", back_populates="suspicions")
    suspicion_id = db.Column(db.Integer, primary_key=False)


class User(db.Model):
    __tablename__ = "users"
    SUSPICION_THRESHOLD = 5
    SAME_IP_WORKER_THRESHOLD = 3

    id = db.Column(db.Integer, primary_key=True) 
    # id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # Then move to this
    username = db.Column(db.String(50), unique=False, nullable=False)
    oauth_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    api_key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    client_id = db.Column(db.String(50), unique=True, default=generate_client_id, nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_active = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    contact = db.Column(db.String(50), default=None)

    kudos = db.Column(db.BigInteger, default=0, nullable=False, index=True)
    monthly_kudos = db.Column(db.Integer, default=0, nullable=False)
    monthly_kudos_last_received = db.Column(db.DateTime, default=None)
    evaluating_kudos = db.Column(db.Integer, default=0, nullable=False)
    usage_multiplier = db.Column(db.Float, default=1.0, nullable=False)
    contributed_thing = db.Column(db.Float, default=0, nullable=False, index=True)
    contributed_fulfillments = db.Column(db.Integer, default=0, nullable=False)
    usage_thing = db.Column(db.Float, default=0, nullable=False)
    usage_requests = db.Column(db.Integer, default=0, nullable=False)

    worker_invited = db.Column(db.Integer, default=0, nullable=False)
    moderator = db.Column(db.Boolean, default=False, nullable=False)
    public_workers = db.Column(db.Boolean, default=False, nullable=False)
    trusted = db.Column(db.Boolean, default=False, nullable=False)
    concurrency = db.Column(db.Integer, default=30, nullable=False)

    workers = db.relationship(f"WorkerExtended", back_populates="user", cascade="all, delete-orphan")
    teams = db.relationship(f"Team", back_populates="owner", cascade="all, delete-orphan")
    suspicions = db.relationship("UserSuspicions", back_populates="user", cascade="all, delete-orphan")
    stats = db.relationship("UserStats", back_populates="user", cascade="all, delete-orphan")
    waiting_prompts = db.relationship("WaitingPromptExtended", back_populates="user", cascade="all, delete-orphan")
    interrogations = db.relationship("Interrogation", back_populates="user", cascade="all, delete-orphan")
    filters = db.relationship("Filter", back_populates="user")

    def create(self):
        self.check_for_bad_actor()
        logger.debug(self.api_key)
        db.session.add(self)
        db.session.commit()
        logger.debug(self.api_key)
        logger.info(f"New User Created {self.get_unique_alias()}")
        

    def get_min_kudos(self):
        if self.is_anon(): 
            return -50
        elif self.is_pseudonymous():
            return 14
        else:
            return 25

    def check_for_bad_actor(self):
        if len(self.username) > 30:
            self.username = self.username[:30]
            self.report_suspicion(reason = Suspicions.USERNAME_LONG)
        if is_profane(self.username):
            self.report_suspicion(reason = Suspicions.USERNAME_PROFANITY)

    def check_key(api_key):
        return(self.api_key == api_key)

    def set_username(self,new_username):
        if is_profane(new_username):
            return("Profanity")
        if len(new_username) > 30:
            return("Too Long")
        self.username = sanitize_string(new_username)
        db.session.commit()
        return("OK")

    def set_contact(self,new_contact):
        if self.contact == new_contact:
            return("OK")
        if is_profane(new_contact):
            return("Profanity")
        self.contact = sanitize_string(new_contact)
        db.session.commit()
        return("OK")

    def set_trusted(self,is_trusted):
        # Anonymous can never be trusted
        if self.is_anon():
            return
        self.trusted = is_trusted
        db.session.commit()
        if self.trusted:
            for worker in self.workers:
                worker.paused = False

    def set_moderator(self,is_moderator):
        if self.is_anon():
            return
        self.moderator = is_moderator
        db.session.commit()
        if self.moderator:
            logger.warning(f"{self.username} Set as moderator")
            self.set_trusted(True)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def record_usage(self, raw_things, kudos):
        self.last_active = datetime.utcnow()
        self.usage_requests += 1
        self.modify_kudos(-kudos,"accumulated")
        self.usage_thing = round(self.usage_thing + (raw_things * self.usage_multiplier / thing_divisor),2)
        db.session.commit()

    def record_contributions(self, raw_things, kudos):
        self.last_active = datetime.utcnow()
        self.contributed_fulfillments += 1
        # While a worker is untrusted, half of all generated kudos go for evaluation
        if not self.trusted and not self.is_anon():
            kudos_eval = round(kudos / 2)
            kudos -= kudos_eval
            self.evaluating_kudos += kudos_eval
            self.modify_kudos(kudos,"accumulated")
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")
        self.contributed_thing = round(self.contributed_thing + raw_things/thing_divisor,2)
        db.session.commit()

    def record_uptime(self, kudos):
        self.last_active = datetime.utcnow()
        # While a worker is untrusted, all uptime kudos go for evaluation
        if not self.trusted and not self.is_anon():
            self.evaluating_kudos += kudos
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")

    def check_for_trust(self):
        '''After a user passes the evaluation threshold (?? kudos)
        All the evaluating Kudos added to their total and they automatically become trusted
        Suspicious users do not automatically pass evaluation
        '''
        if self.evaluating_kudos >= int(os.getenv("KUDOS_TRUST_THRESHOLD")) and not self.is_suspicious() and not self.is_anon():
            self.modify_kudos(self.evaluating_kudos,"accumulated")
            self.evaluating_kudos = 0
            self.set_trusted(True)

    def modify_monthly_kudos(self, monthly_kudos):
        # We always give upfront the monthly kudos to the user once.
        # If they already had some, we give the difference but don't change the date
        if monthly_kudos > 0:
            self.modify_kudos(monthly_kudos, "recurring")
        if not self.monthly_kudos_last_received:
            self.monthly_kudos_last_received = datetime.utcnow()
        self.monthly_kudos += monthly_kudos
        if self.monthly_kudos < 0:
            self.monthly_kudos = 0
        db.session.commit()

    def receive_monthly_kudos(self):
        kudos_amount = self.calculate_monthly_kudos()
        if kudos_amount == 0:
            return
        if self.monthly_kudos_last_received:
            has_month_passed = datetime.utcnow() > self.monthly_kudos_last_received + dateutil.relativedelta.relativedelta(months=+1)
        else:
            # If the user is supposed to receive Kudos, but doesn't have a last received date, it means it is a moderator who hasn't received it the first time
            has_month_passed = True
        if has_month_passed:
            # Not committing as it'll happen in modify_kudos() anyway
            self.monthly_kudos_last_received = datetime.utcnow()
            self.modify_kudos(kudos_amount, "recurring")
            logger.info(f"User {self.get_unique_alias()} received their {kudos_amount} monthly Kudos")

    def calculate_monthly_kudos(self):
        base_amount = self.monthly_kudos
        if self.moderator:
            base_amount += 100000
        base_amount += patrons.get_monthly_kudos(self.id)
        return(base_amount)

    def modify_kudos(self, kudos, action = 'accumulated'):
        logger.debug(f"modifying existing {self.kudos} kudos of {self.get_unique_alias()} by {kudos} for {action}")
        self.kudos = round(self.kudos + kudos, 2)
        self.ensure_kudos_positive()
        kudos_details = db.session.query(UserStats).filter_by(user_id=self.id).filter_by(action=action).first()
        if not kudos_details:
            kudos_details = UserStats(user_id=self.id, action=action, value=round(kudos, 2))
            db.session.add(kudos_details)
            db.session.commit()
        else:
            kudos_details.value = round(kudos_details.value + kudos, 2)
            db.session.commit()

    def ensure_kudos_positive(self):
        if self.kudos < self.get_min_kudos():
            self.kudos = self.get_min_kudos()

    # def get_last_kudos_stat_time(action = "award"):
    #     return db.session.query(UserStats).filter_by(user_id=self.id).filter_by(action=action).first()

    def is_anon(self):
        if self.oauth_id == 'anon':
            return(True)
        return(False)

    def is_pseudonymous(self):
        try:
            uuid.UUID(str(self.oauth_id))
            return(True)
        except ValueError:
            return(False)

    def get_concurrency(self, models_requested = None, models_dict = None):
        if not models_requested: models_requested=[]
        if not models_dict: models_dict={}
        if not self.is_anon() or len(models_requested) == 0:
            return(self.concurrency)
        return self.concurrency # FIXME: For this to work, each model_dict needs to contain a list of worker ids in the "workers" key
        found_workers = []
        for model_name in models_requested:
            model_dict = models_dict.get(model_name)
            if model_dict:
                for worker in model_dict["workers"]:
                    if worker not in found_workers:
                        found_workers.append(worker)
        # We allow 10 concurrency per worker serving the models requested
        allowed_concurrency = len(found_workers) * 4
        # logger.debug([allowed_concurrency,models_dict.get(model_name,{"count":0})["count"]])
        return(allowed_concurrency)

    def report_suspicion(self, amount = 1, reason = Suspicions.USERNAME_PROFANITY, formats = None):
        if not formats: formats = []
        # Anon is never considered suspicious
        if self.is_anon():
            return
        if int(reason) in self.suspicions and reason not in [Suspicions.UNREASONABLY_FAST,Suspicions.TOO_MANY_JOBS_ABORTED]:
            return
        new_suspicion = UserSuspicions(user_id=self.id, suspicion_id=int(reason))
        db.session.add(new_suspicion)
        db.session.commit()
        if reason:
            reason_log = SUSPICION_LOGS[reason].format(*formats)
            logger.warning(f"User '{self.id}' suspicion increased to {len(self.suspicions)}. Reason: {reason}")

    def reset_suspicion(self):
        '''Clears the user's suspicion and resets their reasons'''
        if self.is_anon():
            return
        db.session.query(UserSuspicions).filter_by(user_id=self.id).delete()
        db.session.commit()
        for worker in self.workers:
            worker.reset_suspicion()

    def get_suspicion(self):
        return(db.session.query(UserSuspicions).filter_by(user_id=self.id).count())

    def count_workers(self):
        return(len(self.workers))

    def is_suspicious(self): 
        if self.trusted:
            return(False)
        if len(self.suspicions) >= self.SUSPICION_THRESHOLD:
            return(True)
        return(False)

    def exceeding_ipaddr_restrictions(self, ipaddr):
        '''Checks that the ipaddr of the new worker does not have too many other workers
        to prevent easy spamming of new workers with a script
        '''
        ipcount = 0
        for worker in self.workers:
            if worker.ipaddr == ipaddr:
                ipcount += 1
        if ipcount > self.SAME_IP_WORKER_THRESHOLD and ipcount > self.worker_invited:
            return(True)
        return(False)

    def is_stale(self):
        # Stale users have to be inactive for a month
        days_threshold = 30
        days_inactive = (datetime.utcnow() - self.last_active).days
        if days_inactive < days_threshold:
            return(False)
        # Stale user have to have little accumulated kudos. 
        # The longer a user account is inactive. the more kudos they need to have stored to not be deleted
        # logger.debug([days_inactive,self.kudos, 10 * (days_inactive - days_threshold)])
        if self.kudos > 10 * (days_inactive - days_threshold):
            return(False)
        # Anonymous cannot be stale
        if self.is_anon():
            return(False)
        if self.moderator:
            return(False)
        if self.trusted:
            return(False)
        logger.debug([days_inactive,self.kudos,10 * (days_inactive - days_threshold)])
        return(True)

    def compile_kudos_details(self):
        kudos_details_dict = {}
        for stat in self.stats:
            kudos_details_dict[stat.action] = stat.value
        return kudos_details_dict

    def compile_usage_details(self):
        usage_dict = {  
            thing_name: self.usage_thing,
            "requests": self.usage_requests
        }
        return usage_dict

    def compile_contribution_details(self):
        usage_dict = {
            thing_name: self.contributed_thing,
            "fulfillments": self.contributed_fulfillments
        }
        return usage_dict

    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        ret_dict = {
            "username": self.get_unique_alias(),
            "id": self.id,
            "kudos": self.kudos,
            "kudos_details": self.compile_kudos_details(),
            "usage": self.compile_usage_details(),
            "contributions": self.compile_contribution_details(),
            "concurrency": self.concurrency,
            "worker_invited": self.worker_invited,
            "moderator": self.moderator,
            "trusted": self.trusted,
            "pseudonymous": self.is_pseudonymous(),
            "worker_count": self.count_workers(),
            "account_age": (datetime.utcnow() - self.created).seconds,
            # unnecessary information, since the workers themselves wil be visible
            # "public_workers": self.public_workers,
        }
        if self.public_workers or details_privilege >= 1:
            workers_array = []
            for worker in self.workers:
                workers_array.append(worker.id)
            ret_dict["worker_ids"] = workers_array
            ret_dict['contact'] = self.contact
        if details_privilege >= 2:
            mk_dict = {
                "amount": self.calculate_monthly_kudos(),
                "last_received": self.monthly_kudos_last_received
            }
            ret_dict["evaluating_kudos"] = self.evaluating_kudos
            ret_dict["monthly_kudos"] = mk_dict
            ret_dict["suspicious"] = len(self.suspicions)
        return(ret_dict)


    def import_suspicions(self, suspicions):
        for s in suspicions:
            new_suspicion = UserSuspicions(user_id=self.id, suspicion_id=int(s))
            db.session.add(new_suspicion)
        db.session.commit()

    def import_kudos_details(self, kudos_details):
        for key in kudos_details:
            new_kd = UserStats(user_id=self.id, action=key, value=kudos_details[key])
            db.session.add(new_kd)
        db.session.commit()