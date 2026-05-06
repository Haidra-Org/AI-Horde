# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import random
import time
from datetime import datetime

import logfire
import requests
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import expression

from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.utils import get_db_uuid

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731
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
    gen_metadata = db.Column(json_column_type, nullable=True)

    model = db.Column(db.String(255), default="", nullable=False)
    seed = db.Column(db.BigInteger, default=0, nullable=False)
    start_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cancelled = db.Column(db.Boolean, default=False, nullable=False)
    faulted = db.Column(db.Boolean, default=False, nullable=False)
    fake = db.Column(db.Boolean, default=False, nullable=False)
    censored = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        server_default=expression.literal(False),
    )
    job_ttl = db.Column(db.Integer, default=150, nullable=False, index=True)

    wp_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If there has been no explicit model requested by the user, we just choose the first available from the worker
        db.session.add(self)
        db.session.commit()
        if kwargs.get("model") is None:
            worker_models = list(self.worker.get_model_names())
            # Under load, cache/session staleness can return an empty model list right
            # after check-in updates. Fall back to a direct DB read before giving up.
            if len(worker_models) == 0:
                from horde.classes.base.worker import WorkerModel

                worker_models = [
                    row.model for row in db.session.query(WorkerModel.model).filter(WorkerModel.worker_id == self.worker_id).all()
                ]
            # If we reached this point, it means there is at least 1 matching model between worker and client
            # so we pick the first one.
            wp_models = list(self.wp.get_model_names())
            matching_models = worker_models.copy()
            if len(wp_models) != 0:
                matching_models = [model for model in wp_models if model in worker_models]
            if len(matching_models) == 0:
                logger.warning(
                    f"Unexpectedly No models matched between worker and request!: Worker Models: {worker_models}. "
                    f"Request Models: {wp_models}. Will use random worker model.",
                )
                # If worker model metadata is missing, prefer the explicit request model over crashing.
                matching_models = wp_models if len(wp_models) != 0 else worker_models
            if len(matching_models) == 0:
                logger.warning(
                    f"No models available for generation {self.id}. "
                    f"Worker Models: {worker_models}. Request Models: {wp_models}. Using empty model string.",
                )
                self.model = ""
            else:
                random.shuffle(matching_models)
                self.model = matching_models[0]
        else:
            self.model = kwargs["model"]
        self.set_job_ttl()
        db.session.commit()

    def set_generation(self, generation, things_per_sec, **kwargs):
        from horde.metrics import submit_record_duration, submit_webhook_call_duration, submit_commit_duration

        # Use an atomic compare-and-set update so exactly one concurrent submit
        # can transition this procgen from pending -> completed.
        sanitized_generation = generation.replace("\x00", "\ufffd")
        seed = kwargs.get("seed", self.seed)
        gen_metadata = kwargs.get("gen_metadata", self.gen_metadata)
        kudos = self.get_gen_kudos()

        updated_rows = (
            db.session.query(type(self))
            .filter(
                type(self).id == self.id,
                type(self).generation.is_(None),
                type(self).faulted.is_(False),
            )
            .update(
                {
                    type(self).generation: sanitized_generation,
                    type(self).seed: seed,
                    type(self).gen_metadata: gen_metadata,
                    type(self).cancelled: False,
                },
                synchronize_session=False,
            )
        )
        if updated_rows == 0:
            current_procgen = db.session.query(type(self)).filter(type(self).id == self.id).populate_existing().first()
            if current_procgen is None:
                return -1
            if current_procgen.is_faulted():
                return -1
            if current_procgen.is_completed():
                return 0
            return -1
        # Sanitize NUL char away from string literal we store in the DB
        self.generation = generation.replace("\x00", "\ufffd")
        # Support for two typical properties
        self.seed = kwargs.get("seed", None)
        self.gen_metadata = kwargs.get("gen_metadata", None)
        kudos = self.get_gen_kudos()
        self.cancelled = False
        _t = time.monotonic()
        self.record(things_per_sec, kudos)
        submit_record_duration.record(time.monotonic() - _t)
        _t = time.monotonic()
        db.session.commit()
        submit_commit_duration.record(time.monotonic() - _t)
        # Send webhook after commit so external I/O does not hold DB locks.
        _t = time.monotonic()
        self.send_webhook(kudos)
        submit_webhook_call_duration.record(time.monotonic() - _t)
        return kudos

    def cancel(self):
        """Cancelling requests in progress still rewards/burns the relevant amount of kudos"""
        if self.is_completed() or self.is_faulted():
            return None
        self.faulted = True
        # We  don't want cancelled requests to raise suspicion
        things_per_sec = self.worker.speed
        kudos = self.get_gen_kudos()
        self.cancelled = True
        self.record(things_per_sec, kudos)
        db.session.commit()
        return kudos * self.worker.get_bridge_kudos_multiplier()

    def record(self, things_per_sec, kudos):
        from horde.metrics import submit_worker_contrib_duration, submit_wp_record_usage_duration

        cancel_txt = ""
        if self.cancelled:
            cancel_txt = " Cancelled"
        if self.fake and self.worker.user == self.wp.user:
            # We do not record usage for paused workers, unless the requestor was the same owner as the worker
            _t = time.monotonic()
            self.worker.record_contribution(raw_things=self.wp.things, kudos=kudos, things_per_sec=things_per_sec)
            submit_worker_contrib_duration.record(time.monotonic() - _t)
            logger.info(
                f"Fake{cancel_txt} Generation {self.id} worth {self.kudos} kudos, delivered by worker: "
                f"{self.worker.name} for wp {self.wp.id}",
            )
        else:
            _t = time.monotonic()
            self.worker.record_contribution(raw_things=self.wp.things, kudos=kudos, things_per_sec=things_per_sec)
            submit_worker_contrib_duration.record(time.monotonic() - _t)
            _t = time.monotonic()
            self.wp.record_usage(raw_things=self.wp.things, kudos=self.adjust_user_kudos(kudos), commit=False)
            submit_wp_record_usage_duration.record(time.monotonic() - _t)
            log_string = (
                f"New{cancel_txt} Generation {self.id} worth {kudos} kudos, delivered by worker: {self.worker.name} for wp {self.wp.id} "
            )
            log_string += f" (requesting user {self.wp.user.get_unique_alias()} [{self.wp.ipaddr}])"
            logger.info(log_string)

    def adjust_user_kudos(self, kudos):
        if self.censored:
            return 0
        return kudos

    def abort(self):
        """Called when this request needs to be stopped without rewarding kudos. Say because it timed out due to a worker crash"""
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
            return True
        return False

    def is_faulted(self):
        return self.faulted

    def is_stale(self):
        if self.is_completed() or self.is_faulted():
            return False
        return (datetime.utcnow() - self.start_time).total_seconds() > self.job_ttl

    def delete(self):
        db.session.delete(self)
        db.session.commit()

    def get_seconds_needed(self):
        return self.wp.things / self.worker.speed

    def get_expected_time_left(self):
        if self.is_completed():
            return 0
        seconds_needed = self.get_seconds_needed()
        seconds_elapsed = (datetime.utcnow() - self.start_time).total_seconds()
        expected_time = seconds_needed - seconds_elapsed
        # In case we run into a slow request
        if expected_time < 0:
            expected_time = 0
        return expected_time

    # This should be extended by every horde type
    def get_details(self):
        """Returns a dictionary with details about this processing generation"""
        ret_dict = {
            "gen": self.generation,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "gen_metadata": self.gen_metadata if self.gen_metadata is not None else [],
        }
        return ret_dict

    # Extendable function to be able to dynamically adjust the amount of things
    # based on what the worker actually returned.
    # Typically needed for LLMs using EOS tokens etc
    def get_things_count(self, generation):
        return self.wp.things

    def send_webhook(self, kudos):
        if not self.wp.webhook:
            return
        with logfire.span("horde.webhook.send", wp_id=str(self.wp.id), procgen_id=str(self.id)) as span:
            from horde.metrics import webhook_duration, webhook_outcomes

            data = self.get_details()
            data["request"] = str(self.wp.id)
            data["id"] = str(self.id)
            data["kudos"] = kudos
            data["worker_id"] = str(data["worker_id"])
            outcome = "giveup"
            attempts = 0
            import time as _time

            for riter in range(3):
                attempts += 1
                t0 = _time.monotonic()
                status_code = None
                attempt_outcome = "exception"
                try:
                    req = requests.post(self.wp.webhook, json=data, timeout=3)
                    status_code = req.status_code
                    if not req.ok:
                        attempt_outcome = "http_error"
                        webhook_duration.record(
                            _time.monotonic() - t0,
                            {"attempt": riter, "outcome": attempt_outcome, "status_code": status_code},
                        )
                        logger.debug(
                            f"Something went wrong when sending generation webhook: {req.status_code} - {req.text}. "
                            f"Will retry {3 - riter - 1} more times...",
                        )
                        continue
                    attempt_outcome = "ok"
                    outcome = "ok"
                    webhook_duration.record(
                        _time.monotonic() - t0,
                        {"attempt": riter, "outcome": attempt_outcome, "status_code": status_code},
                    )
                    break
                except Exception as err:
                    webhook_duration.record(
                        _time.monotonic() - t0,
                        {"attempt": riter, "outcome": attempt_outcome},
                    )
                    logger.debug(f"Exception when sending generation webhook: {err}. Will retry {3 - riter - 1} more times...")
            webhook_outcomes.add(1, {"outcome": outcome})
            span.set_attribute("horde.webhook.outcome", outcome)
            span.set_attribute("horde.webhook.attempts", attempts)

    def set_job_ttl(self):
        """Returns how many seconds each job request should stay waiting before considering it stale and cancelling it
        This function should be overriden by the invididual hordes depending on how the calculating ttl
        """
        self.job_ttl = 150
        db.session.commit()
