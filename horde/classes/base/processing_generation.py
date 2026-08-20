# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import queue
import random
import threading
import time
from datetime import datetime
from typing import Any

import logfire
import requests
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import expression

from horde.classes.base.kudos import kudos_event
from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.utils import get_db_uuid

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731
json_column_type = JSONB if not SQLITE_MODE else JSON

# Generation webhooks deliver through this bounded queue instead of on the
# submit request path: a slow subscriber endpoint (up to three 3-second
# attempts) would otherwise hold the worker's submit response open for
# seconds. Delivery is best-effort either way (a failed delivery only logs
# after the final attempt), so a subscriber observes the same guarantees.
# The bound keeps one unreachable endpoint from accumulating deliveries
# without limit; overflow drops the delivery and counts it as an outcome.
WEBHOOK_QUEUE_MAXSIZE = 256
_webhook_queue: queue.Queue[tuple[str, dict[str, Any], str, str]] = queue.Queue(maxsize=WEBHOOK_QUEUE_MAXSIZE)
_webhook_sender_lock = threading.Lock()
_webhook_sender: threading.Thread | None = None


def _ensure_webhook_sender() -> None:
    # Started lazily on first use so every serving process owns a live thread;
    # a thread started at import time would not survive a post-import fork.
    global _webhook_sender
    if _webhook_sender is not None and _webhook_sender.is_alive():
        return
    with _webhook_sender_lock:
        if _webhook_sender is None or not _webhook_sender.is_alive():
            _webhook_sender = threading.Thread(target=_webhook_sender_loop, name="webhook-sender", daemon=True)
            _webhook_sender.start()


def _webhook_sender_loop() -> None:
    while True:
        url, data, wp_id, procgen_id = _webhook_queue.get()
        try:
            _deliver_webhook(url, data, wp_id, procgen_id)
        except Exception as err:
            logger.warning(f"Unexpected error delivering generation webhook for procgen {procgen_id}: {err}")


def _deliver_webhook(url: str, data: dict[str, Any], wp_id: str, procgen_id: str) -> None:
    with logfire.span("horde.webhook.send", wp_id=wp_id, procgen_id=procgen_id) as span:
        from horde.metrics import webhook_duration, webhook_outcomes

        outcome = "giveup"
        attempts = 0
        for riter in range(3):
            attempts += 1
            t0 = time.monotonic()
            status_code = None
            attempt_outcome = "exception"
            try:
                req = requests.post(url, json=data, timeout=3)
                status_code = req.status_code
                if not req.ok:
                    attempt_outcome = "http_error"
                    webhook_duration.record(
                        time.monotonic() - t0,
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
                    time.monotonic() - t0,
                    {"attempt": riter, "outcome": attempt_outcome, "status_code": status_code},
                )
                break
            except Exception as err:
                webhook_duration.record(
                    time.monotonic() - t0,
                    {"attempt": riter, "outcome": attempt_outcome},
                )
                logger.debug(f"Exception when sending generation webhook: {err}. Will retry {3 - riter - 1} more times...")
        webhook_outcomes.add(1, {"outcome": outcome})
        span.set_attribute("horde.webhook.outcome", outcome)
        span.set_attribute("horde.webhook.attempts", attempts)


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
        db.session.add(self)
        db.session.commit()
        if kwargs.get("model") is None:
            # Callers on the pop path pass an explicit model selected against the
            # worker's declared list. This branch serves the remaining callers
            # (e.g. fake generations) that construct without one, deriving the
            # model from the intersection of the worker's hosted models and the
            # WP's requested models.
            worker_models = list(self.worker.get_model_names())
            # Under load, cache/session staleness can return an empty model list right
            # after check-in updates. Fall back to a direct DB read before giving up.
            if len(worker_models) == 0:
                from horde.classes.base.worker import WorkerModel

                worker_models = [
                    row.model for row in db.session.query(WorkerModel.model).filter(WorkerModel.worker_id == self.worker_id).all()
                ]
            wp_models = list(self.wp.get_model_names())
            if len(wp_models) != 0:
                matching_models = [model for model in wp_models if model in worker_models]
            else:
                matching_models = worker_models.copy()
            if len(matching_models) == 0:
                # An empty intersection still has to name a model: workers reject a nameless job
                # outright and fault it back, which defeats this path's remaining caller (the fake
                # generation handed to a paused worker). Prefer a model the worker hosts, since it
                # can actually run it; fall back to the WP's own list; go blank only when neither
                # side names anything.
                matching_models = worker_models.copy() or wp_models.copy()
            if len(matching_models) == 0:
                logger.warning(
                    f"No models matched between worker and request for generation {self.id}. "
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

    def set_generation(self, generation: str, things_per_sec: float, **kwargs: object) -> float | int:
        from horde.metrics import (
            submit_claim_duration,
            submit_commit_duration,
            submit_gen_kudos_duration,
            submit_record_duration,
            submit_record_performance_duration,
            submit_webhook_call_duration,
            submit_wp_completion_duration,
        )

        gentype_label = {"horde.gentype": self.procgen_type}
        # Use an atomic compare-and-set update so exactly one concurrent submit
        # can transition this procgen from pending -> completed.
        sanitized_generation = generation.replace("\x00", "\ufffd")
        seed = kwargs.get("seed", self.seed)
        gen_metadata = kwargs.get("gen_metadata", self.gen_metadata)

        _t = time.monotonic()
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
        submit_claim_duration.record(time.monotonic() - _t, gentype_label)
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
        # The reward is derived from what the worker returned (text kudos scale
        # with the delivered token count), so it is computed once the generation
        # fields above are populated.
        _t = time.monotonic()
        kudos = self.get_gen_kudos()
        submit_gen_kudos_duration.record(time.monotonic() - _t, gentype_label)
        self.cancelled = False
        _t = time.monotonic()
        self.record(things_per_sec, kudos)
        submit_record_duration.record(time.monotonic() - _t)
        _t = time.monotonic()
        db.session.commit()
        submit_commit_duration.record(time.monotonic() - _t)
        # Persist the worker performance sample AFTER the main commit so its INSERT
        # on worker_performances does not extend the time the hot `users` row locks
        # are held above. Performance samples are telemetry, so a separate follow-up
        # transaction is safe. Retention and the workers.speed average are folded
        # off the request path by threads.refresh_worker_speeds.
        _t = time.monotonic()
        self.worker.record_performance(things_per_sec)
        submit_record_performance_duration.record(time.monotonic() - _t, gentype_label)
        _t = time.monotonic()
        if self.wp.is_completed():
            from horde.database.kudos_reservations import release_reservation

            release_reservation(f"upfront:{self.wp.id}")
            db.session.commit()
        submit_wp_completion_duration.record(time.monotonic() - _t, gentype_label)
        # Queue the webhook after commit; delivery runs on the background
        # sender thread, so this only measures payload build and enqueue.
        _t = time.monotonic()
        self.send_webhook(kudos)
        submit_webhook_call_duration.record(time.monotonic() - _t)
        return kudos

    def cancel(self) -> float | None:
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
        # See set_generation: keep the performance write out of the locked window.
        self.worker.record_performance(things_per_sec)
        if self.wp.is_completed():
            from horde.database.kudos_reservations import release_reservation

            release_reservation(f"upfront:{self.wp.id}")
            db.session.commit()
        return kudos * self.worker.get_bridge_kudos_multiplier()

    def record(self, things_per_sec, kudos):
        from horde.metrics import submit_worker_contrib_duration, submit_wp_record_usage_duration

        cancel_txt = ""
        if self.cancelled:
            cancel_txt = " Cancelled"
        if self.fake:
            # A tricked worker's submission is never delivered and its job is
            # fulfilled (and settled) by another worker, so a fake generation
            # credits no contribution and debits no usage.
            logger.info(
                f"Fake{cancel_txt} Generation {self.id} discarded without kudos, submitted by worker: "
                f"{self.worker.name} for wp {self.wp.id}",
            )
            return
        # The worker-owner credit and the requester debit are now recorded as
        # ledger postings (see kudos.py) rather than in-place `users` row UPDATEs,
        # so this settlement no longer takes any users-row lock and cannot form the
        # activate/submit deadlock cycle the old FOR NO KEY UPDATE ordering guarded
        # against. Grouping every posting of this settlement under one event id.
        with kudos_event(job_id=self.id, wp_type=self.wp.wp_type):
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

    def send_webhook(self, kudos: float) -> None:
        """Hand the generation webhook to the background sender.

        The payload is materialized here because it reads session-bound ORM
        state; the sender thread performs only HTTP I/O on plain data.
        """
        if not self.wp.webhook:
            return
        from horde.metrics import webhook_outcomes

        data = self.get_details()
        data["request"] = str(self.wp.id)
        data["id"] = str(self.id)
        data["kudos"] = kudos
        data["worker_id"] = str(data["worker_id"])
        _ensure_webhook_sender()
        try:
            _webhook_queue.put_nowait((self.wp.webhook, data, str(self.wp.id), str(self.id)))
        except queue.Full:
            webhook_outcomes.add(1, {"outcome": "dropped"})
            logger.warning(f"Webhook queue full; dropping delivery for procgen {self.id}")

    def set_job_ttl(self):
        """Returns how many seconds each job request should stay waiting before considering it stale and cancelling it
        This function should be overriden by the invididual hordes depending on how the calculating ttl
        """
        self.job_ttl = 150
        db.session.commit()
