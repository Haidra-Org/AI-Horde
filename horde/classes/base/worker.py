# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from datetime import datetime, timedelta
from typing import Any

import logfire
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID

from horde import vars as hv
from horde.classes.base import settings
from horde.discord import send_pause_notification
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.suspicions import SUSPICION_LOGS, Suspicions
from horde.utils import get_db_uuid, get_message_expiry_date, is_profane, sanitize_string

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731

# The throughput a worker is credited with before it has recorded any performance
# sample, expressed in "things" (megapixelsteps for image, tokens for text) per second.
# It is scaled into the raw units the speed column stores by the worker type's divisor.
# Kept as a named baseline so a fresh worker's speed reproduces the historical
# substitution the old speed expression applied whenever the sample average was NULL.
SPEED_BASELINE_THINGS_PER_SEC = 1


class WorkerStats(db.Model):
    __tablename__ = "worker_stats"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("Worker", back_populates="stats")
    action = db.Column(db.String(20), nullable=False, index=True)
    value = db.Column(db.BigInteger, default=0, nullable=False)


class WorkerPerformance(db.Model):
    __tablename__ = "worker_performances"
    id = db.Column(db.Integer, primary_key=True)
    # Indexed because ``Worker.record_performance`` filters this table by ``worker_id``
    # twice per completed job (once to prune older samples, once to recompute the
    # average that maintains ``Worker.speed``). PostgreSQL does not index foreign key
    # columns automatically, so without this both are sequential scans of the full
    # sample table on every job completion.
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    worker = db.relationship("Worker", back_populates="performance")
    performance = db.Column(db.Float, primary_key=False)
    created = db.Column(
        db.DateTime,
        default=datetime.utcnow,
    )  # TODO maybe index here, but I'm not sure how big this table is


class WorkerBlackList(db.Model):
    __tablename__ = "worker_blacklists"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("Worker", back_populates="blacklist")
    word = db.Column(db.String(20), primary_key=False)


class WorkerSuspicions(db.Model):
    __tablename__ = "worker_suspicions"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("Worker", back_populates="suspicions")
    suspicion_id = db.Column(db.Integer, primary_key=False)


class WorkerModel(db.Model):
    __tablename__ = "worker_models"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("Worker", back_populates="models")
    model = db.Column(db.String(255))  # TODO model should be a foreign key to a model table


class WorkerMessage(db.Model):
    __tablename__ = "worker_messages"
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=True,
    )
    worker = db.relationship("Worker", back_populates="messages")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="worker_messages")
    message = db.Column(db.Text)
    origin = db.Column(db.String(255))
    expiry = db.Column(db.DateTime, default=get_message_expiry_date, index=True)
    created = db.Column(db.DateTime, default=datetime.utcnow())


class WorkerTemplate(db.Model):
    __tablename__ = "workers"
    __mapper_args__ = {
        "polymorphic_identity": "worker_template",
        "polymorphic_on": "worker_type",
    }
    suspicion_threshold = 5
    # Every how many seconds does this worker get a kudos reward
    uptime_reward_threshold = 600
    default_maintenance_msg = "This worker has been put into maintenance mode by its owner"

    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    worker_type = db.Column(db.String(30), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="workers")
    name = db.Column(db.String(100), unique=True, nullable=False, index=True)
    info = db.Column(db.String(1000))
    ipaddr = db.Column(db.String(39))
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
    bridge_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False, index=True)
    last_reward_uptime = db.Column(db.BigInteger, default=0, nullable=False)
    # Used by all workers to record how much they can pick up to generate
    # The value of this column is dfferent per worker type
    max_power = db.Column(db.Integer, default=20, nullable=False)
    extra_slow_worker = db.Column(db.Boolean, default=False, nullable=False, index=True)

    paused = db.Column(db.Boolean, default=False, nullable=False)
    maintenance = db.Column(db.Boolean, default=False, nullable=False)
    maintenance_msg = db.Column(db.String(300), unique=False, default=default_maintenance_msg, nullable=False)
    team_id = db.Column(uuid_column_type(), db.ForeignKey("teams.id"), default=None)
    team = db.relationship("Team", back_populates="workers")

    allow_unsafe_ipaddr = db.Column(db.Boolean, default=True, nullable=False)

    stats = db.relationship("WorkerStats", back_populates="worker", cascade="all, delete-orphan")
    performance = db.relationship("WorkerPerformance", back_populates="worker", cascade="all, delete-orphan")
    suspicions = db.relationship("WorkerSuspicions", back_populates="worker", cascade="all, delete-orphan")
    problem_jobs = db.relationship("UserProblemJobs", back_populates="worker", cascade="all, delete-orphan")
    messages = db.relationship("WorkerMessage", back_populates="worker", cascade="all, delete-orphan")

    require_upfront_kudos = False
    prioritized_users = []
    # Because I didn't use worker_type correctly. I should have called them "text" and "image"
    # TODO: Normalize this to the standard
    wtype = "image"

    # ``speed`` is the worker's rolling-average throughput (raw things per second),
    # materialized on the row rather than derived on read. It was previously a
    # hybrid_property whose SQL form embedded a correlated
    # ``avg(worker_performances.performance)`` subquery, re-evaluated for every candidate
    # worker in every pop candidate filter: the single largest cumulative-time query on
    # the database. It is now maintained at the one write site (``record_performance``)
    # and seeded on construction (``__init__``), so every reader becomes a plain column
    # access.
    #
    # A worker with no samples stores the per-type baseline (see ``_baseline_speed``),
    # reproducing the historical CASE expression that substituted that baseline whenever
    # the average was NULL. This preserves the exact pop-filter outcomes for fresh
    # workers: image workers clear the ``>= 500000`` threshold, while text and
    # interrogation workers stay below theirs until real samples arrive.
    #
    # The stored value falls back to the baseline on a zero average as well as on a NULL
    # one, matching the former Python getter's truthiness check rather than the former
    # SQL expression's NULL-only CASE. The two forms disagreed, and the Python form is
    # the one that must be preserved: ``speed`` is a divisor in
    # ``ProcessingGeneration.get_seconds_needed``, so a stored zero raises
    # ZeroDivisionError there. The substitution is invisible to every pop filter, since
    # zero and the baseline fall on the same side of all four thresholds
    # (``>= 500000``, ``>= 2``, ``>= slow_speed`` where slow_speed is 3 to 5, and
    # ``< 10``).
    speed = db.Column(db.Float, nullable=False, index=True)

    def __init__(self, **kwargs: Any) -> None:
        """Seed the materialized ``speed`` to the per-type baseline on construction.

        ``speed`` is NOT NULL and is maintained thereafter by ``record_performance``.
        Seeding in ``__init__`` (the single construction chokepoint shared by every
        worker subclass) rather than in ``create`` guarantees the column is populated on
        every path that persists a worker, including callers that build and commit a
        worker without going through ``create``. A worker with no performance samples
        therefore reports the same baseline throughput it did when speed was derived on
        read.
        """
        super().__init__(**kwargs)
        if self.speed is None:
            self.speed = self._baseline_speed()

    def _baseline_speed(self) -> float:
        """Return the throughput a worker reports before it has any performance samples.

        Mirrors the historical speed expression, which substituted one thing per second
        (scaled into raw units by the worker type's divisor) whenever the performance
        average was NULL, keeping fresh workers on the same side of the pop-filter
        thresholds as when speed was derived on read.
        """
        return SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors[self.wtype]

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
                self.report_suspicion(reason=Suspicions.WORKER_NAME_EXTREME)
            self.name = self.name[:100]
            self.report_suspicion(reason=Suspicions.WORKER_NAME_LONG)
        if is_profane(self.name):
            self.report_suspicion(reason=Suspicions.WORKER_PROFANITY, formats=[self.name])

    def report_suspicion(self, amount=1, reason=Suspicions.WORKER_PROFANITY, formats=None):
        if not formats:
            formats = []
        # Unreasonable Fast can be added multiple times and it increases suspicion each time
        if reason not in [Suspicions.UNREASONABLY_FAST, Suspicions.TOO_MANY_JOBS_ABORTED] and int(reason) in self.get_suspicion_reasons():
            return
        new_suspicion = WorkerSuspicions(worker_id=self.id, suspicion_id=int(reason))
        db.session.add(new_suspicion)
        self.user.report_suspicion(amount, reason, formats)
        if reason:
            reason_log = SUSPICION_LOGS[reason].format(*formats)
            logger.warning(f"Worker '{self.id}' suspicion increased. Reason: {reason_log}")
        if self.is_suspicious() and not self.paused:
            self.paused = True
            send_pause_notification(
                f"Worker {self.name} ({self.id}) automatically set to paused.\n"
                f"Last suspicion log: {reason.name}.\n"
                f"Total Suspicion {self.get_suspicion()}",
            )
        db.session.flush()

    def get_suspicion_reasons(self):
        return set([s.suspicion_id for s in self.suspicions])

    def reset_suspicion(self):
        """Clears the worker's suspicion and resets their reasons"""
        db.session.query(WorkerSuspicions).filter_by(worker_id=self.id).delete()
        db.session.commit()

    def get_suspicion(self):
        return len(self.suspicions)

    def is_suspicious(self):
        # Trusted users are never suspicious
        if self.user.trusted:
            return False
        if self.get_suspicion() >= self.suspicion_threshold:
            return True
        return False

    def set_name(self, new_name):
        if self.name == new_name:
            return "OK"
        if is_profane(new_name):
            return "Profanity"
        if len(new_name) > 100:
            return "Too Long"
        new_name = sanitize_string(new_name)
        # Worker.name carries a unique constraint (ix_workers_name). Detect the
        # collision here and report it, rather than letting the commit raise an
        # IntegrityError (a 500 that also leaves the session needing a rollback).
        name_taken = db.session.query(WorkerTemplate.id).filter(WorkerTemplate.name == new_name, WorkerTemplate.id != self.id).first()
        if name_taken:
            return "Already Exists"
        self.name = new_name
        db.session.commit()
        return "OK"

    def set_info(self, new_info):
        if self.info == new_info:
            return "OK"
        if is_profane(new_info):
            return "Profanity"
        if len(new_info) > 1000:
            return "Too Long"
        self.info = sanitize_string(new_info)
        db.session.commit()
        return "OK"

    def set_team(self, new_team):
        self.team_id = new_team.id if new_team else None
        db.session.commit()
        return "OK"

    # This should be overwriten by each specific horde
    def calculate_uptime_reward(self):
        return 100

    def toggle_maintenance(self, is_maintenance_active, maintenance_msg=None):
        self.maintenance = is_maintenance_active
        self.maintenance_msg = self.default_maintenance_msg
        if self.maintenance and maintenance_msg not in [None, ""]:
            self.maintenance_msg = sanitize_string(maintenance_msg)
        db.session.commit()

    def toggle_paused(self, is_paused_active):
        self.paused = is_paused_active
        db.session.commit()

    # This should be extended by each worker type
    def check_in(self, **kwargs):
        # To avoid excessive commits and UPDATE churn under heavy pop load,
        # we only update the worker on check_in every 30 seconds (after the first 30s of life).
        # Returns True if the check_in actually performed work, False if it was debounced.
        # Subclasses should also short-circuit when this returns False.
        now = datetime.utcnow()
        if (now - self.last_check_in).total_seconds() < 30 and (now - self.created).total_seconds() > 30:
            return False
        self.ipaddr = kwargs.get("ipaddr", None)
        self.bridge_agent = sanitize_string(kwargs.get("bridge_agent", "unknown:0:unknown"))
        self.threads = kwargs.get("threads", 1)
        self.require_upfront_kudos = kwargs.get("require_upfront_kudos", False)
        self.allow_unsafe_ipaddr = kwargs.get("allow_unsafe_ipaddr", True)
        # If's OK to provide an empty list here as we don't actually modify this var
        # We only check it in can_generate
        self.prioritized_users = kwargs.get("prioritized_users", [])
        if not kwargs.get("safe_ip", True) and not self.user.trusted:
            self.report_suspicion(reason=Suspicions.UNSAFE_IP)
        if not self.is_stale() and not self.paused and not self.maintenance:
            self.uptime += (now - self.last_check_in).total_seconds()
            # Every 10 minutes of uptime gets 100 kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                if self.team:
                    self.team.record_uptime(self.uptime_reward_threshold)
                kudos = self.calculate_uptime_reward()
                self.modify_kudos(kudos, "uptime")
                self.user.record_uptime(kudos)
                logger.debug(
                    f"Worker '{self.name}' received {kudos} kudos for uptime of {self.uptime_reward_threshold} seconds.",
                )
                self.last_reward_uptime = self.uptime
        else:
            # If the worker comes back from being stale, we just reset their last_reward_uptime
            # So that they have to stay up at least 10 mins to get uptime kudos
            self.last_reward_uptime = self.uptime
        self.last_check_in = now
        return True

    def get_human_readable_uptime(self):
        if self.uptime < 60:
            return f"{self.uptime} seconds"
        elif self.uptime < 60 * 60:
            return f"{round(self.uptime / 60, 2)} minutes"
        elif self.uptime < 60 * 60 * 24:
            return f"{round(self.uptime / 60 / 60, 2)} hours"
        else:
            return f"{round(self.uptime / 60 / 60 / 24, 2)} days"

    # We split it to its own function to make it extendable
    def convert_contribution(self, raw_things):
        converted = round(raw_things / hv.thing_divisors[self.wtype], 2)
        self.contributions = round(self.contributions + converted, 2)
        # We reurn the converted amount as well in case we need it
        return converted

    def get_bridge_kudos_multiplier(self):
        """To override in case we want to adjust the worker reward based on their bridge version"""
        return 1

    def record_contribution(self, raw_things, kudos, things_per_sec):
        with logfire.span("horde.worker.record_contribution", worker_id=str(self.id), kudos=kudos):
            self._record_contribution(raw_things, kudos, things_per_sec)

    @logger.catch(reraise=True)
    def _record_contribution(self, raw_things, kudos, things_per_sec):
        """We record the servers newest contribution
        We do not need to know what type the contribution is, to avoid unnecessarily extending this method
        """
        kudos = kudos * self.get_bridge_kudos_multiplier()
        self.user.record_contributions(raw_things=raw_things, kudos=kudos, contrib_type=self.wtype, commit=False)
        self.modify_kudos(kudos, "generated", commit=False)
        converted_amount = self.convert_contribution(raw_things)
        self.fulfilments += 1
        if self.team and self.wtype == "image":
            self.team.record_contribution(converted_amount, kudos)
        # Note: deferred commit; caller (procgen.set_generation) commits once at the end.
        # The worker_performances prune+insert is intentionally NOT done here; it is
        # persisted separately via record_performance() after the main commit so it
        # does not lengthen the hot `users` row lock hold. See record_performance().
        if things_per_sec / hv.thing_divisors[self.wtype] > hv.suspicion_thresholds[self.wtype]:
            self.report_suspicion(
                reason=Suspicions.UNREASONABLY_FAST,
                formats=[round(things_per_sec / hv.thing_divisors[self.wtype], 2)],
            )

    def record_performance(self, things_per_sec, commit=True):
        """Persist a worker performance sample, pruning to the 20 most recent.

        Split out of _record_contribution so it runs in its own transaction after
        the submission's main commit, keeping the worker_performances count/prune/
        insert out of the window where the hot `users` rows are locked. Performance
        samples are telemetry, so persisting them separately (and losing at most one
        sample on a crash between commits) is safe.
        """
        performances = db.session.query(WorkerPerformance).filter_by(worker_id=self.id)
        if performances.count() >= 20:
            # Keep only the 20 most recent performance records
            keep_ids = (
                db.session.query(WorkerPerformance.id)
                .filter_by(worker_id=self.id)
                .order_by(WorkerPerformance.created.desc())
                .limit(20)
            )
            db.session.query(WorkerPerformance).filter_by(worker_id=self.id).filter(
                WorkerPerformance.id.not_in(keep_ids),
            ).delete(synchronize_session=False)
        new_performance = WorkerPerformance(worker_id=self.id, performance=things_per_sec)
        db.session.add(new_performance)
        # Refresh the materialized rolling average from the retained samples. The
        # aggregate autoflushes the pending insert and the prune above, so it reflects
        # exactly the rows a read-time subquery would have seen; recomputing from the
        # samples (rather than incrementally) also makes this write self-healing if an
        # earlier sample write was lost.
        retained_average = db.session.query(func.avg(WorkerPerformance.performance)).filter_by(worker_id=self.id).scalar()
        # A zero average falls back to the baseline alongside a NULL one; see the
        # ``speed`` column comment for why a stored zero is not a permissible value.
        self.speed = retained_average if retained_average else self._baseline_speed()
        if commit:
            db.session.commit()

    def modify_kudos(self, kudos, action="generated", commit=True):
        self.kudos = round(self.kudos + kudos, 2)
        kudos_details = db.session.query(WorkerStats).filter_by(worker_id=self.id).filter_by(action=action).first()
        if not kudos_details:
            kudos_details = WorkerStats(worker_id=self.id, action=action, value=round(kudos, 2))
            db.session.add(kudos_details)
            if commit:
                db.session.commit()
        else:
            kudos_details.value = round(kudos_details.value + kudos, 2)
            if commit:
                db.session.commit()
        logger.trace([kudos_details, kudos_details.value])

    def log_aborted_job(self):
        # We count the number of jobs aborted in an 1 hour period. So we only log the new timer each time an hour expires.
        if (datetime.utcnow() - self.last_aborted_job).total_seconds() > 3600:
            self.aborted_jobs = 0
            self.last_aborted_job = datetime.utcnow()
        self.aborted_jobs += 1
        # These are accumulating too fast at 5. Increasing to 20
        dropped_job_threshold = 20
        if settings.mode_raid():
            dropped_job_threshold = 10
        # Avoid putting stability.ai into maintenance until I figure out why I'm getting wrong payloads
        if self.user.id == 6901:
            dropped_job_threshold = 100
        # Avoiding putting into maintenance interrogation workers due to crashes from the model
        # TODO: Remove once crashes are fixed
        if self.worker_type == "interrogation_worker":
            dropped_job_threshold = 100
        if self.aborted_jobs > dropped_job_threshold:
            # if a worker drops too many jobs in an hour, we put them in maintenance
            # except during a raid, as we don't want them to know we detected them.
            if not settings.mode_raid():
                self.toggle_maintenance(
                    True,
                    "Maintenance mode activated because worker is dropping too many jobs."
                    "Please investigate if your performance has been impacted and consider reducing your max_power or your max_threads",
                )
            self.report_suspicion(reason=Suspicions.TOO_MANY_JOBS_ABORTED)
            self.aborted_jobs = 0
        self.uncompleted_jobs += 1
        db.session.commit()

    # def is_slow(self):

    def get_performance(self):
        return f"{round(self.speed / hv.thing_divisors[self.wtype], 1)} {hv.thing_names[self.wtype]} per second"
        # #TODO: Need to figure how to handle this using self.speed
        return "No requests fulfilled yet"

    def is_stale(self):
        try:
            if (datetime.utcnow() - self.last_check_in).total_seconds() > 300:
                return True
        # If the last_check_in isn't set, it's a new worker, so it's stale by default
        except AttributeError:
            return True
        return False

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

    def get_active_messages(self):
        return [m for m in self.messages if m.expiry > datetime.utcnow()]

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege=0):
        """We display these in the workers list json"""
        ret_dict = {
            "name": self.name,
            "id": str(self.id),
            "type": self.wtype,
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
            "flagged": self.user.flagged,
            "online": not self.is_stale(),
            "team": {"id": str(self.team.id), "name": self.team.name} if self.team else "None",
            "bridge_agent": self.bridge_agent,
        }
        if details_privilege >= 2:
            ret_dict["paused"] = self.paused
            ret_dict["suspicious"] = len(self.suspicions)
        if details_privilege >= 1 or self.user.public_workers:
            ret_dict["owner"] = self.user.get_unique_alias()
            msgs = []
            for m in self.get_active_messages():
                msgs.append(
                    {
                        "worker_id": str(self.id),
                        "user_id": m.user_id,
                        "message": m.message,
                        "origin": m.origin,
                        "created": m.created,
                        "expiry": m.expiry,
                    },
                )
            ret_dict["messages"] = msgs
        if details_privilege >= 1:
            ret_dict["ipaddr"] = self.ipaddr
            ret_dict["contact"] = self.user.contact
        return ret_dict

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_lite_details(self):
        """We display these in the workers list json"""
        ret_dict = {
            "name": self.name,
            "id": str(self.id),
            "type": self.wtype,
            "online": not self.is_stale(),
        }
        return ret_dict


class Worker(WorkerTemplate):
    """A worker is meant to receive a text prompt and pass it though a generative model"""

    __mapper_args__ = {
        "polymorphic_identity": "worker",
    }
    nsfw = db.Column(db.Boolean, default=False, nullable=False)

    blacklist = db.relationship("WorkerBlackList", back_populates="worker", cascade="all, delete-orphan")
    models = db.relationship("WorkerModel", back_populates="worker", cascade="all, delete-orphan")
    processing_gens = db.relationship("ImageProcessingGeneration", back_populates="worker", lazy="raise")

    # This should be extended by each specific horde
    def check_in(self, **kwargs):
        if not super().check_in(**kwargs):
            return False
        self.set_models(kwargs.get("models"))
        self.nsfw = kwargs.get("nsfw", True)
        self.set_blacklist(kwargs.get("blacklist", []))
        self.extra_slow_worker = kwargs.get("extra_slow_worker", False)
        # Commit should happen on calling extensions
        return True

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
            blacklisted_word = WorkerBlackList(worker_id=self.id, word=word[0:15])
            db.session.add(blacklisted_word)
        db.session.flush()

    def refresh_model_cache(self) -> list[str]:
        # Read the authoritative rows straight from worker_models rather than the
        # self.models relationship collection. Under expire_on_commit=False that
        # collection can remain loaded with pre-change rows across a commit, which
        # would republish a stale list. A direct query always reflects what was written.
        models_list = [row.model for row in db.session.query(WorkerModel.model).filter_by(worker_id=self.id).all()]
        try:
            hr.horde_r_setex(
                f"worker_{self.id}_model_cache",
                timedelta(seconds=600),
                json.dumps(models_list),
            )
        except Exception as err:
            logger.debug(f"Error when trying to set models cache: {err}. Retrieving from DB.")
        return models_list

    def get_model_names(self):
        if hr.horde_r is None:
            return [m.model for m in self.models]
        model_cache = hr.horde_r_get(f"worker_{self.id}_model_cache")
        if not model_cache:
            return self.refresh_model_cache()
        try:
            models_ret = json.loads(model_cache)
        except TypeError:
            logger.error(f"Model cache could not be loaded: {model_cache}")
            return self.refresh_model_cache()
        if models_ret is None:
            return self.refresh_model_cache()
        return models_ret

    def set_models(self, models):
        models = self.parse_models(models)
        existing_model_names = set(self.get_model_names())
        if existing_model_names == models:
            return
        # logger.debug([existing_model_names,models, existing_model_names == models])
        db.session.query(WorkerModel).filter_by(worker_id=self.id).delete()
        db.session.flush()
        for model_name in models:
            model = WorkerModel(worker_id=self.id, model=model_name)
            db.session.add(model)
        db.session.commit()
        self.refresh_model_cache()

    def parse_models(self, models):
        """Parses the models provided by the worker into a set
        Using an extra function to allow override by different types of worker
        """
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        models = [sanitize_string(model_name[0:100]) for model_name in models]
        del models[200:]
        return set(models)

    def can_generate(self, waiting_prompt):
        """Takes as an argument a WaitingPrompt class and checks if this worker is valid for generating it"""
        # Workers in maintenance are still allowed to generate for their owner
        if self.maintenance and waiting_prompt.user != self.user:
            return [False, None]
        # logger.warning(datetime.utcnow())
        if self.is_stale():
            # We don't consider stale workers in the request, so we don't need to report a reason
            return [False, None]
        # logger.warning(datetime.utcnow())
        if waiting_prompt.nsfw and not self.nsfw:
            return [False, "nsfw"]
        # logger.warning(datetime.utcnow())
        if waiting_prompt.trusted_workers and not self.user.trusted:
            return [False, "untrusted"]
        # If the worker has been tricked once by this prompt, we don't want to resend it it
        # as it may give up the jig
        # logger.warning(datetime.utcnow())
        if waiting_prompt.tricked_worker(self):
            return [False, "secret"]
        # logger.warning(datetime.utcnow())
        if self.blacklist:
            prompt_lower = waiting_prompt.prompt.lower()
            if any(b.word.lower() in prompt_lower for b in self.blacklist):
                return [False, "blacklist"]
        # Skips working prompts which require a specific worker from a list, and our ID is not in that list
        if waiting_prompt.worker_blacklist:
            if len(waiting_prompt.workers) and self.id in waiting_prompt.get_worker_ids():
                return [False, "worker_id"]
        else:
            if len(waiting_prompt.workers) and self.id not in waiting_prompt.get_worker_ids():
                return [False, "worker_id"]
        # logger.warning(datetime.utcnow())

        # my_model_names = self.get_model_names()
        # wp_model_names = waiting_prompt.get_model_names()
        # if len(wp_model_names) > 0:
        #     found_matching_model = False
        #     for model_name in my_model_names:
        #         if model_name in wp_model_names:
        #             found_matching_model = True
        #             break
        #     if not found_matching_model:
        #         return [False, 'model']

        # # I removed this for now as I think it might be blocking requests from generating. I will revisit later again
        # # If the worker is slower than average, and we're on the last quarter of the request, we try to utilize only fast workers
        # if self.get_performance_average() < self.db.stats.get_request_avg() and waiting_prompt.n <= waiting_prompt.jobs/4:
        #   return [False, 'performance']
        return [True, None]

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege=0):
        """We display these in the workers list json"""
        ret_dict = super().get_details(details_privilege)
        ret_dict["nsfw"] = self.nsfw
        ret_dict["models"] = self.get_model_names()
        return ret_dict

    def delete(self):
        for word in self.blacklist:
            db.session.delete(word)
        for model in self.models:
            db.session.delete(model)
        super().delete()

    # To override
    def get_safe_amount(self, amount, wp):
        return amount
