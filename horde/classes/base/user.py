import uuid
import os

import dateutil.relativedelta
from datetime import datetime
from sqlalchemy import Enum, UniqueConstraint
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.dialects.postgresql import UUID

from horde.logger import logger
from horde.flask import db, SQLITE_MODE
from horde.vars import thing_name, text_thing_divisor, text_thing_name
from horde import vars as hv
from horde.suspicions import Suspicions, SUSPICION_LOGS
from horde.utils import is_profane, sanitize_string, generate_client_id
from horde.patreon import patrons
from horde.enums import UserRecordTypes, UserRoleTypes
from horde.utils import get_db_uuid

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)


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


class UserRecords(db.Model):
    __tablename__ = "user_records"
    __table_args__ = (UniqueConstraint('user_id', 'record_type', 'record', name='user_records_user_id_record_type_record_key'),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User", back_populates="records")
    # contribution, usage, fulfillment, request
    record_type = db.Column(Enum(UserRecordTypes), nullable=False, index=True)
    record = db.Column(db.String(30), nullable=False)
    value = db.Column(db.Float, default=0, nullable=False)

class UserRole(db.Model):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint('user_id', 'user_role', name='user_id_role'),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user = db.relationship("User", back_populates="roles")
    user_role = db.Column(Enum(UserRoleTypes), nullable=False)
    value = db.Column(db.Boolean, default=False, nullable=False)

class KudosTransferLog(db.Model):
    __tablename__ = "kudos_transfers"
    # Decided to add one row per
    # __table_args__ = (UniqueConstraint('source_id', 'dest_id', name='source_dest'),)
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    dest_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kudos = db.Column(db.BigInteger, default=0, nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class UserSharedKey(db.Model):
    __tablename__ = "user_sharedkeys"
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user = db.relationship("User", back_populates="sharedkeys")
    kudos = db.Column(db.BigInteger, default=5000, nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expiry = db.Column(db.DateTime, index=True)
    name = db.Column(db.String(255), nullable=True)
    utilized = db.Column(db.BigInteger, default=0, nullable=False)
    waiting_prompts = db.relationship("WaitingPrompt", back_populates="sharedkey", passive_deletes=True, cascade="all, delete-orphan")

    @logger.catch(reraise=True)
    def get_details(self):
        ret_dict = {
            "username": self.user.get_unique_alias(),
            "id": self.id,
            "kudos": self.kudos,
            "expiry": self.expiry,
            "utilized": self.utilized,
        }
        return ret_dict

    def consume_kudos(self, kudos):
        if self.kudos == 0:
            return
        if self.kudos != -1:      
            self.kudos = round(self.kudos - kudos, 2)
            if self.kudos < 0:
                self.kudos = 0
        self.utilized = round(self.utilized + kudos, 2)
        logger.debug(f"Utilized {kudos} from shared key {self.id}. {self.kudos} remaining.")
        db.session.commit()

    def is_valid(self):
        if self.kudos == 0:
            return False,"This shared key has run out of kudos."
        if self.expiry is not None and self.expiry < datetime.utcnow():
            return False,"This shared key has expired"
        else:
            return True, None

class User(db.Model):
    __tablename__ = "users"
    SUSPICION_THRESHOLD = 5
    SAME_IP_WORKER_THRESHOLD = 3
    SAME_IP_TRUSTED_WORKER_THRESHOLD = 20

    id = db.Column(db.Integer, primary_key=True) 
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

    worker_invited = db.Column(db.Integer, default=0, nullable=False)
    public_workers = db.Column(db.Boolean, default=False, nullable=False)
    concurrency = db.Column(db.Integer, default=30, nullable=False)

    workers = db.relationship(f"Worker", back_populates="user", cascade="all, delete-orphan")
    teams = db.relationship(f"Team", back_populates="owner", cascade="all, delete-orphan")
    sharedkeys = db.relationship(f"UserSharedKey", back_populates="user", cascade="all, delete-orphan")
    suspicions = db.relationship("UserSuspicions", back_populates="user", cascade="all, delete-orphan")
    records = db.relationship("UserRecords", back_populates="user", cascade="all, delete-orphan")
    roles = db.relationship("UserRole", back_populates="user", cascade="all, delete-orphan")
    stats = db.relationship("UserStats", back_populates="user", cascade="all, delete-orphan")
    waiting_prompts = db.relationship("WaitingPrompt", back_populates="user", cascade="all, delete-orphan")
    interrogations = db.relationship("Interrogation", back_populates="user", cascade="all, delete-orphan")
    filters = db.relationship("Filter", back_populates="user")

    ## TODO: Figure out how to make the below work
    # def get_role_expr(cls, role):
    #     subquery = db.session.query(UserRole.user_id
    #         ).filter(
    #             UserRole.user_role == UserRoleTypes.MODERATOR,
    #             UserRole.value == True,
    #             UserRole.user_id == cls.id
    #         ).correlate(
    #             cls
    #         ).as_scalar()
    #     return cls.id == subquery

    @hybrid_property
    def trusted(self) -> bool:
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=UserRoleTypes.TRUSTED
        ).first()
        return user_role is not None and user_role.value

    @trusted.expression
    def trusted(cls):
        subquery = db.session.query(UserRole.user_id
            ).filter(
                UserRole.user_role == UserRoleTypes.TRUSTED,
                UserRole.value == True,
                UserRole.user_id == cls.id
            ).correlate(
                cls
            ).as_scalar()
        return cls.id == subquery

    @hybrid_property
    def flagged(self) -> bool:
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=UserRoleTypes.FLAGGED
        ).first()
        return user_role is not None and user_role.value


    @flagged.expression
    def flagged(cls):
        subquery = db.session.query(UserRole.user_id
            ).filter(
                UserRole.user_role == UserRoleTypes.FLAGGED,
                UserRole.value == True,
                UserRole.user_id == cls.id
            ).correlate(
                cls
            ).as_scalar()
        return cls.id == subquery

    @hybrid_property
    def moderator(self) -> bool:
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=UserRoleTypes.MODERATOR
        ).first()
        return user_role is not None and user_role.value

    @moderator.expression
    def moderator(cls):
        subquery = db.session.query(UserRole.user_id
            ).filter(
                UserRole.user_role == UserRoleTypes.MODERATOR,
                UserRole.value == True,
                UserRole.user_id == cls.id
            ).correlate(
                cls
            ).as_scalar()
        return cls.id == subquery

    @hybrid_property
    def customizer(self) -> bool:
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=UserRoleTypes.CUSTOMIZER
        ).first()
        return user_role is not None and user_role.value

    @customizer.expression
    def customizer(cls):
        subquery = db.session.query(UserRole.user_id
            ).filter(
                UserRole.user_role == UserRoleTypes.CUSTOMIZER,
                UserRole.value == True,
                UserRole.user_id == cls.id
            ).correlate(
                cls
            ).as_scalar()
        return cls.id == subquery

    @hybrid_property
    def vpn(self) -> bool:
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=UserRoleTypes.VPN
        ).first()
        return user_role is not None and user_role.value

    @vpn.expression
    def vpn(cls):
        subquery = db.session.query(UserRole.user_id
            ).filter(
                UserRole.user_role == UserRoleTypes.VPN,
                UserRole.value == True,
                UserRole.user_id == cls.id
            ).correlate(
                cls
            ).as_scalar()
        return cls.id == subquery

    @hybrid_property
    def special(self) -> bool:
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=UserRoleTypes.SPECIAL
        ).first()
        return user_role is not None and user_role.value

    @special.expression
    def special(cls):
        subquery = db.session.query(UserRole.user_id
            ).filter(
                UserRole.user_role == UserRoleTypes.SPECIAL,
                UserRole.value == True,
                UserRole.user_id == cls.id
            ).correlate(
                cls
            ).as_scalar()
        return cls.id == subquery

    def create(self):
        self.check_for_bad_actor()
        db.session.add(self)
        db.session.commit()
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

    def check_key(self, api_key):
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

    def set_user_role(self, role, value):
        user_role = UserRole.query.filter_by(
            user_id=self.id, 
            user_role=role,
        ).first()
        if value is False:
            if user_role is None:
                return
            else:
                # No entry means false
                db.session.delete(user_role)
                db.session.commit()
                return 
        if user_role is None:
            new_role = UserRole(
                user_id=self.id, 
                user_role=role,
                value=value
            )
            db.session.add(new_role)
            db.session.commit()
            return
        logger.debug(user_role)
        if user_role.value is False:
            user_role.value = True
            db.session.commit()

    def set_trusted(self,is_trusted):
        # Anonymous can never be trusted
        if self.is_anon():
            return
        self.set_user_role(UserRoleTypes.TRUSTED, is_trusted)
        if self.trusted:
            for worker in self.workers:
                worker.paused = False

    def set_flagged(self, is_flagged):
        # Anonymous can never be flagged
        if self.is_anon():
            return
        self.set_user_role(UserRoleTypes.FLAGGED, is_flagged)

    def set_moderator(self,is_moderator):
        if self.is_anon():
            return
        self.set_user_role(UserRoleTypes.MODERATOR, is_moderator)
        if self.moderator:
            logger.warning(f"{self.username} Set as moderator")
            self.set_trusted(True)

    def set_customizer(self, is_customizer):
        if self.is_anon():
            return
        self.set_user_role(UserRoleTypes.CUSTOMIZER, is_customizer)

    def set_vpn(self, is_vpn):
        if self.is_anon():
            return
        self.set_user_role(UserRoleTypes.VPN, is_vpn)

    def set_special(self, is_special):
        if self.is_anon():
            return
        self.set_user_role(UserRoleTypes.SPECIAL, is_special)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def update_user_record(self, record_type, record, increment_value):
        record_details = db.session.query(
            UserRecords
        ).filter_by(
            user_id=self.id,
            record_type=record_type,
            record=record
        ).first()
        if not record_details:
            record_details = UserRecords(
                user_id=self.id, 
                record_type=record_type, 
                record=record, 
                value=round(increment_value,2)
            )
            db.session.add(record_details)
        else:
            # The value is always added to the existing value
            record_details.value = round(record_details.value + increment_value, 2)
        db.session.commit()


    def record_usage(self, raw_things, kudos, usage_type):
        self.last_active = datetime.utcnow()
        self.modify_kudos(-kudos,"accumulated")
        self.update_user_record(
            record_type=UserRecordTypes.REQUEST, 
            record=usage_type, 
            increment_value=1
        )
        self.update_user_record(
            record_type=UserRecordTypes.USAGE, 
            record=usage_type, 
            increment_value=raw_things * self.usage_multiplier / hv.thing_divisors[usage_type]
        )

    def record_contributions(self, raw_things, kudos, contrib_type):
        self.last_active = datetime.utcnow()
        self.update_user_record(
            record_type=UserRecordTypes.FULFILLMENT, 
            record=contrib_type, 
            increment_value=1
        )
        # While a worker is untrusted, half of all generated kudos go for evaluation
        if not self.trusted and not self.is_anon():
            kudos_eval = round(kudos / 2)
            kudos -= kudos_eval
            self.evaluating_kudos += kudos_eval
            self.modify_kudos(kudos,"accumulated")
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")
        self.update_user_record(
            record_type=UserRecordTypes.CONTRIBUTION, 
            record=contrib_type, 
            increment_value=raw_things/hv.thing_divisors[contrib_type]
        )
        

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
        if self.evaluating_kudos <= int(os.getenv("KUDOS_TRUST_THRESHOLD")):
            return
        if self.is_suspicious():
            return
        if self.is_anon():
            return
        # An account has to exist for at least 1 week to become trusted automatically
        if (datetime.utcnow() - self.created).total_seconds() < 86400 * 7:
            return
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

    def receive_monthly_kudos(self, force=False):
        kudos_amount = self.calculate_monthly_kudos()
        if kudos_amount == 0:
            return
        if force:
            has_month_passed = True
        elif self.monthly_kudos_last_received:
            has_month_passed = datetime.utcnow() > self.monthly_kudos_last_received + dateutil.relativedelta.relativedelta(months=+1)
        else:
            # If the user is supposed to receive Kudos, but doesn't have a last received date, it means it is a moderator who hasn't received it the first time
            has_month_passed = True
        if has_month_passed:
            # Not committing as it'll happen in modify_kudos() anyway
            if not self.monthly_kudos_last_received:
                self.monthly_kudos_last_received = datetime.utcnow() + dateutil.relativedelta.relativedelta(months=+1)
            else:
                self.monthly_kudos_last_received = self.monthly_kudos_last_received + dateutil.relativedelta.relativedelta(months=+1)
            self.modify_kudos(kudos_amount, "recurring")
            logger.info(f"User {self.get_unique_alias()} received their {kudos_amount} monthly Kudos")

    def calculate_monthly_kudos(self):
        base_amount = self.monthly_kudos
        if self.moderator:
            base_amount += 300000
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
        if reason not in [Suspicions.UNREASONABLY_FAST, Suspicions.TOO_MANY_JOBS_ABORTED] and int(reason) in self.get_suspicion_reasons():
            return
        new_suspicion = UserSuspicions(user_id=self.id, suspicion_id=int(reason))
        db.session.add(new_suspicion)
        db.session.commit()
        if reason:
            reason_log = SUSPICION_LOGS[reason].format(*formats)
            logger.warning(f"User '{self.id}' suspicion increased to {len(self.suspicions)}. Reason: {reason}")

    def get_suspicion_reasons(self):
        return set([s.suspicion_id for s in self.suspicions])

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

    def count_sharedkeys(self):
        return(len(self.sharedkeys))

    def max_sharedkeys(self):
        if self.trusted:
            return 10
        return 3

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
        if self.trusted:
            if ipcount > self.SAME_IP_TRUSTED_WORKER_THRESHOLD and ipcount > self.worker_invited:
                return(True)
        elif ipcount > self.SAME_IP_WORKER_THRESHOLD and ipcount > self.worker_invited:
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

    def compile_records_details(self):
        records_dict = {}
        for r in self.records:
            rtype = r.record_type.name.lower()
            if rtype not in records_dict:
                records_dict[rtype] = {}
            record_key = r.record
            if r.record_type in {UserRecordTypes.USAGE, UserRecordTypes.CONTRIBUTION} and r.record in hv.thing_names:
                record_key = hv.thing_names[r.record]
            records_dict[rtype][record_key] = r.value
        return records_dict

    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        ret_dict = {
            "username": self.get_unique_alias(),
            "id": self.id,
            "kudos": self.kudos,
            "kudos_details": self.compile_kudos_details(),
            "usage": {}, # Obsolete in favor or records
            "contributions": {}, # Obsolete in favor or records
            "records": self.compile_records_details(),
            "concurrency": self.concurrency,
            "worker_invited": self.worker_invited,
            "moderator": self.moderator,
            "trusted": self.trusted,
            "flagged": self.flagged,
            "pseudonymous": self.is_pseudonymous(),
            "worker_count": self.count_workers(),
            "account_age": (datetime.utcnow() - self.created).total_seconds(),
            # unnecessary information, since the workers themselves wil be visible
            # "public_workers": self.public_workers,
        }
        if self.public_workers or details_privilege >= 1:
            workers_array = []
            for worker in self.workers:
                workers_array.append(str(worker.id))
            ret_dict["worker_ids"] = workers_array
            ret_dict['contact'] = self.contact
            ret_dict['vpn'] = self.vpn
            ret_dict['special'] = self.special
        if details_privilege >= 1:
            sharedkeys_array = []
            for sk in self.sharedkeys:
                sharedkeys_array.append(str(sk.id))
            ret_dict["sharedkey_ids"] = sharedkeys_array
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