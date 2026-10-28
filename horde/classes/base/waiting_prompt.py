# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import logfire
from sqlalchemy import JSON, case, func, inspect, or_, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, object_session, relationship
from sqlalchemy.sql import ColumnElement, expression, null

from horde import vars as hv
from horde.bridge_reference import check_bridge_capability
from horde.classes.base.kudos import StyleReward, kudos_event
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.database.kudos_legacy_projection import project_style_reward
from horde.enums import RequestTerminalOutcome
from horde.exceptions import is_deadlock_error
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.metrics import (
    request_time_to_first_start,
    wp_activate_base_commit_duration,
    wp_activate_base_record_usage_duration,
    wp_activate_duration,
    wp_activation_age,
)
from horde.request_scheduling import (
    DispatchObservation,
    build_worker_scheduling_state,
    calculate_scheduling_forecast,
    record_request_cancellation_forecast,
    record_request_start_forecast,
    record_worker_dispatch,
    store_scheduling_forecast,
)
from horde.utils import get_db_uuid, get_expiry_date, get_extra_slow_expiry_date

if TYPE_CHECKING:
    from horde.classes.base.user import User


class SubmittedPromptPurpose(StrEnum):
    """Represent the two approved consumers of original prompt provenance.

    The value is a declaration of intent by the caller, not a credential: it
    records which reviewed path is reading the column so a future consumer has
    to name itself rather than reach the text incidentally.
    """

    REQUEST_OWNER = "request_owner"
    MODERATION_EVIDENCE = "moderation_evidence"


procgen_classes = {
    "template": ProcessingGeneration,
    "image": ImageProcessingGeneration,
    "text": TextProcessingGeneration,
}

WP_ACTIVATION_MAX_ATTEMPTS = 4

EXTRA_SOURCE_IMAGE_HORDE_TAX = 5
"""The horde tax, in kudos, on each image a request stores beyond its prompt: an extra source image, or a chat
request's image. Each one adds storage and transfer the generation itself does not pay for."""

FAULTED_GENERATIONS_LIMIT = 2
"""How many faulted generations a request tolerates; one more faults the request. A request that fails on every
attempt, such as one no worker can build, is otherwise dispatched again after each fault, and every fault counts
toward the aborted-job threshold that puts the worker serving it into maintenance."""


def estimate_queue_wait_seconds(
    *,
    queued_things: float,
    queued_jobs: int,
    worker_threads: int,
    things_per_second: float,
) -> float:
    """Return the seconds that workers with ``things_per_second`` of throughput need to clear ``queued_things``.

    The text status reports this for the work up to and including a request, and the free output allowance reads it
    for the work ahead of a request being created, so both describe one estimate. Fewer queued jobs than threads
    leave the other threads idle, so the throughput is scaled down to the threads the jobs occupy.

    Args:
        queued_things: The queued work, in the request type's things (tokens for text).
        queued_jobs: The generations that work is split into.
        worker_threads: The threads ``things_per_second`` is measured across.
        things_per_second: The combined throughput of ``worker_threads`` when all of them are busy.

    Returns:
        The estimated seconds. Without any throughput the work is divided by one, which keeps the estimate defined
        for a horde with no measured throughput and is 0 when nothing is queued.
    """
    occupied_threads = min(queued_jobs, worker_threads)
    occupied_throughput = things_per_second * occupied_threads / worker_threads if worker_threads > 0 else 0.0
    if occupied_throughput <= 0:
        occupied_throughput = 1.0
    return queued_things / occupied_throughput


json_column_type = JSONB if not SQLITE_MODE else JSON
uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731


@dataclass(frozen=True)
class RequestCompletionSnapshot:
    """Represents what decides whether a request is completed, read from the database in one statement.

    Each statement sees the rows as committed when it starts, so values read by separate statements can mix states
    from before and after another session's commit, such as an ``n`` read before a pop and generation counts read
    after it.
    """

    n: int
    """How many jobs still wait for a worker, restarted ones included."""
    faulted: bool
    """Whether the request is faulted."""
    completed_jobs: int
    """The request's real generations that stored a result; faulted ones are not counted."""
    processing_jobs: int
    """The request's real generations a worker is still working on."""
    fault_reason: str | None
    """The message stored with the request's last refused reply or worker fault; None for requests that keep none."""
    fault_rc: str | None
    """The return code stored with ``fault_reason``."""
    cancelled: bool = False
    """Whether maintenance stopped dispatching jobs; work already in flight can still return results."""
    faulted_jobs: int = 0
    """The request's real generations that faulted without storing a result, restarted or not."""

    def is_completed(self, jobs: int) -> bool:
        """Return whether a request asking for ``jobs`` generations is completed in this snapshot.

        A request is completed when it is faulted, or when no job waits, none is processing and either maintenance
        cancelled its remaining jobs or as many generations stored a result as it asked for. Cancellation retains
        partial results and lets workers already processing finish. Faulted generations do not count: a third
        attempt (two faulted, one processing, none waiting) read as completed for the whole attempt.

        Args:
            jobs: How many generations the request asked for.
        """
        if self.faulted:
            return True
        return self.is_settled() and (self.cancelled or self.completed_jobs >= jobs)

    def is_settled(self) -> bool:
        """Return whether no job of the request waits for a worker and none is processing."""
        return self.n <= 0 and self.processing_jobs == 0


@dataclass(frozen=True)
class RequestEligibilityInputs:
    """Represents the request-side facts the worker eligibility rules read, computed once per request.

    Eligibility evaluates one request against many workers, so these are read from the request once rather than once
    per worker.
    """

    model_names: tuple[str, ...]
    """The models the request accepts, in the order ``get_model_names`` returns them; empty when it accepts any."""
    model_name_set: frozenset[str]
    """``model_names`` as a set, for membership and disjointness checks."""
    has_targeted_workers: bool
    """Whether the request lists workers, as an allow list or, with ``worker_blacklist``, a deny list."""
    targeted_worker_ids: frozenset[uuid.UUID]
    """The ids of the listed workers; empty when ``has_targeted_workers`` is false."""
    tricked_worker_ids: frozenset[uuid.UUID]
    """The ids of the workers this request has already tricked."""


class WPAllowedWorkers(db.Model):
    __tablename__ = "wp_allowed_workers"
    # The pop candidate query and the skipped-count query probe this table with
    # per-WP EXISTS subqueries; the composite index makes those probes
    # index-only and keeps the planner's cost estimate below the JIT threshold.
    __table_args__ = (db.Index("ix_wp_allowed_workers_wp_id_worker_id", "wp_id", "worker_id"),)
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    worker = db.relationship("Worker")
    wp_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"),
        nullable=False,
    )
    wp = db.relationship("WaitingPrompt", back_populates="workers")


class WPTrickedWorkers(db.Model):
    __tablename__ = "wp_tricked_workers"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    worker = db.relationship("Worker")
    wp_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"),
        nullable=False,
    )
    wp = db.relationship("WaitingPrompt", back_populates="tricked_workers")


class WPModels(db.Model):
    __tablename__ = "wp_models"
    # See WPAllowedWorkers: the pop candidate query and wp_has_valid_workers
    # probe wp_models per WP through EXISTS subqueries.
    __table_args__ = (db.Index("ix_wp_models_wp_id_model", "wp_id", "model"),)
    id = db.Column(db.BigInteger, primary_key=True)
    wp_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"),
        nullable=False,
    )
    wp = db.relationship("WaitingPrompt", back_populates="models")
    model = db.Column(db.String(255), nullable=False)


class WaitingPrompt(db.Model):
    """For storing waiting prompts in the DB"""

    __tablename__ = "waiting_prompts"
    # The pop candidate queries filter to the active unfilled queue and read it
    # in priority order; without this partial index they walk the full
    # extra_priority index and discard the expired majority row by row. The
    # index orders match the queries' ORDER BY exactly so the scan streams
    # presorted. The predicate is PostgreSQL-only and ignored on SQLite.
    __table_args__ = (
        db.Index(
            "ix_waiting_prompts_sharedkey_id",
            "sharedkey_id",
            postgresql_where=db.text("sharedkey_id IS NOT NULL"),
        ),
        db.Index(
            "ix_waiting_prompts_active_queue",
            "wp_type",
            db.text("extra_priority DESC"),
            db.text("created ASC"),
            postgresql_where=db.text("active AND NOT faulted AND n > 0"),
        ),
    )
    __mapper_args__ = {
        "polymorphic_identity": "template",
        "polymorphic_on": "wp_type",
    }
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    wp_type = db.Column(db.String(30), nullable=False, index=True)
    # Effective worker input. Never substitute submitted_prompt in generation consumers.
    prompt = db.Column(db.Text, nullable=False)
    # Submission provenance, before styles/moderation. NULL means unknown; it must
    # NEVER fall back to prompt. Deferred to keep queue scans lean.
    submitted_prompt: Mapped[str | None] = db.deferred(db.Column(db.Text, nullable=True), raiseload=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user: Mapped[User] = relationship("User", back_populates="waiting_prompts")

    params = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)
    gen_payload = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)
    extra_source_images = db.Column(MutableDict.as_mutable(json_column_type), nullable=True)
    nsfw = db.Column(db.Boolean, default=False, nullable=False)
    ipaddr = db.Column(db.String(39))  # ipv6
    safe_ip = db.Column(db.Boolean, default=False, nullable=False)
    trusted_workers = db.Column(db.Boolean, default=False, nullable=False, index=True)
    validated_backends = db.Column(db.Boolean, default=True, nullable=False, index=True)
    slow_workers = db.Column(db.Boolean, default=True, nullable=False, index=True)
    extra_slow_workers = db.Column(db.Boolean, default=False, nullable=False, index=True)
    worker_blacklist = db.Column(db.Boolean, default=False, nullable=False, index=True)
    faulted = db.Column(db.Boolean, default=False, nullable=False, index=True)
    active = db.Column(db.Boolean, default=False, nullable=False, index=True)
    consumed_kudos = db.Column(db.Integer, default=0, nullable=False)
    kudos = db.Column(db.Float, default=0, nullable=False, server_default=expression.literal(0))
    # The amount of jobs still to do
    n = db.Column(db.Integer, default=0, nullable=False, index=True)
    # This stores the original amount of jobs requested
    jobs = db.Column(db.Integer, default=0, nullable=False)
    things = db.Column(db.BigInteger, default=0, nullable=False)
    total_usage = db.Column(db.Float, default=0, nullable=False)
    # Kudos-derived (activation sets it to the requester's kudos), so it is as wide as users.kudos.
    extra_priority = db.Column(db.BigInteger, default=0, nullable=False, index=True)
    # TODO: Delete. Obsoleted.
    job_ttl = db.Column(db.Integer, default=150, nullable=False)
    disable_batching = db.Column(db.Boolean, default=False, nullable=False)
    webhook = db.Column(db.String(1024))
    traceparent = db.Column(db.String(55), nullable=True)

    client_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False)
    sharedkey_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("user_sharedkeys.id", ondelete="CASCADE"),
        nullable=True,
    )
    sharedkey = db.relationship("UserSharedKey", back_populates="waiting_prompts")
    # The style the request runs under, drawn from a collection when the request specified one. Copied to
    # the generation statistics on submit. No foreign key, so deleting a style leaves running requests alone.
    style_id = db.Column(uuid_column_type(), nullable=True)
    proxied_account = db.Column(db.String(255), nullable=True)

    tricked_workers: Mapped[list[WPTrickedWorkers]] = relationship(
        "WPTrickedWorkers",
        back_populates="wp",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )
    workers: Mapped[list[WPAllowedWorkers]] = relationship(
        "WPAllowedWorkers",
        back_populates="wp",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )
    models: Mapped[list[WPModels]] = relationship("WPModels", back_populates="wp", cascade="all, delete-orphan")

    expiry = db.Column(db.DateTime, default=get_expiry_date, index=True)

    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    terminal_outcome = db.Column(db.String(32), nullable=True)
    terminal_recorded_at = db.Column(db.DateTime(timezone=False), nullable=True)

    def __init__(self, worker_ids, models, *args, **kwargs):
        with logfire.span("horde.wp.init"):
            super().__init__(*args, **kwargs)
            db.session.add(self)
            db.session.commit()
            self.set_workers(worker_ids)
            self.set_models(models)
            self.extract_params()

    def set_workers(self, worker_ids=None):
        if not worker_ids:
            worker_ids = []
        else:
            # We only allow whitelisting up to 5 worker IDs
            worker_ids = worker_ids[0:5]
        # We don't allow more workers to claim they can server more than 50 models atm (to prevent abuse)
        for wid in worker_ids:
            worker_entry = WPAllowedWorkers(worker_id=wid, wp_id=self.id)
            db.session.add(worker_entry)

    def set_models(self, model_names=None):
        if not model_names:
            model_names = []
        # logger.debug(model_names)
        for model in model_names:
            model_entry = WPModels(model=model, wp_id=self.id)
            db.session.add(model_entry)

    def activate(
        self,
        downgrade_wp_priority: bool = False,
        extra_source_images: list[dict[str, object]] | None = None,
        kudos_adjustment: float = 0,
        *,
        style_reward: StyleReward | None = None,
    ) -> None:
        """Put the request on the queue and debit its horde tax.

        Args:
            downgrade_wp_priority: Queue the request at anonymous priority.
            extra_source_images: The request's extra source images, each of which adds to the horde tax.
            kudos_adjustment: An extra charge added to the horde tax.
            style_reward: The style surcharge ``kudos_adjustment`` includes and the style author it is owed to.
        """
        with logfire.span("horde.wp.activate", wp_id=str(self.id)):
            t0 = time.monotonic()
            wp_type = getattr(self, "wp_type", "unknown")
            created = getattr(self, "created", None)
            try:
                for attempt in range(1, WP_ACTIVATION_MAX_ATTEMPTS + 1):
                    try:
                        self._activate(
                            downgrade_wp_priority,
                            extra_source_images,
                            kudos_adjustment,
                            style_reward=style_reward,
                        )
                        break
                    except OperationalError as error:
                        if not is_deadlock_error(error) or attempt == WP_ACTIVATION_MAX_ATTEMPTS:
                            raise
                        # PostgreSQL has already aborted the victim transaction;
                        # reset SQLAlchemy before touching ORM state again. Jitter
                        # keeps concurrently retried activations from re-forming
                        # the same cycle.
                        db.session.rollback()
                        time.sleep(random.uniform(0.025, 0.1) * attempt)
            finally:
                wp_activate_duration.record(time.monotonic() - t0, {"horde.wp_type": wp_type})
                # Elapsed seconds between WP row insert and activation.
                # This surfaces "time spent validating/initiating" before the
                # WP becomes visible to workers, a key SLO signal.
                if created:
                    age = (datetime.utcnow() - created).total_seconds()
                    if age >= 0:
                        wp_activation_age.record(age, {"horde.wp_type": wp_type})

    def _activate(
        self,
        downgrade_wp_priority: bool = False,
        extra_source_images: list[dict[str, object]] | None = None,
        kudos_adjustment: float = 0,
        *,
        style_reward: StyleReward | None = None,
    ) -> None:
        """We separate the activation from __init__ as often we want to check if there's a valid worker for it
        Before we add it to the queue
        """
        # The up-front horde-tax debit is now a ledger posting rather than an
        # in-place `users` row UPDATE, removing that users-row lock from this path.
        # Activation still touches several related rows and therefore retains a
        # bounded retry for any other PostgreSQL deadlock cycle.
        self.active = True
        with logfire.span("horde.wp.activate.priority_calc"):
            if self.user.flagged and self.user.kudos > 10:
                self.extra_priority = round(self.user.kudos / 1000)
            elif self.user.flagged:
                self.extra_priority = -100
            elif downgrade_wp_priority:
                logger.debug("Started WP with downgraded priority to Anon")
                self.extra_priority = -50
            else:
                self.extra_priority = self.user.kudos
        # This is an extra cost for the operation as a whole, to represent the infrastructure costs
        # and rewarding requests which bundle multiple jobs into the same payload
        # Instead of splitting them into multiples.
        horde_tax = 1
        if extra_source_images is not None and len(extra_source_images) > 0:
            self.extra_source_images = {"esi": extra_source_images}
            # Extra source images add more infrastructure costs, which are represented with a kudos tax
            horde_tax += EXTRA_SOURCE_IMAGE_HORDE_TAX * len(extra_source_images)
        horde_tax += kudos_adjustment
        with logfire.span("horde.wp.activate.record_usage", horde_tax=horde_tax):
            _t_ru = time.monotonic()
            try:
                with kudos_event(job_id=self.id, wp_type=self.wp_type):
                    forgiven = self.record_usage(
                        raw_things=0,
                        kudos=horde_tax,
                        usage_type=self.wp_type,
                        avoid_burn=True,
                        style_reward=style_reward,
                    )
                    # The author is owed only what the debit collected. Shadow mode applies the floor
                    # inline and so knows the forgiven part here; ledger mode credits nothing here and
                    # the applier credits the author when it folds the debit.
                    if style_reward is not None:
                        project_style_reward(style_reward, forgiven=forgiven)
            finally:
                wp_activate_base_record_usage_duration.record(time.monotonic() - _t_ru, {})
        # logger.debug(f"wp {self.id} initiated and paying horde tax: {horde_tax}")
        with logfire.span("horde.wp.activate.commit"):
            _t_c = time.monotonic()
            try:
                db.session.commit()
            finally:
                wp_activate_base_commit_duration.record(time.monotonic() - _t_c, {})

    def get_model_names(self):
        return [m.model for m in self.models]

    def get_eligibility_inputs(self) -> RequestEligibilityInputs:
        """Return the request-side facts the worker eligibility rules read.

        The targeted worker ids are only looked up when the request lists workers, which skips the worker id cache
        read for the common request that does not.

        Returns:
            The request's eligibility inputs.
        """
        model_names = tuple(self.get_model_names())
        has_targeted_workers = len(self.workers) > 0
        return RequestEligibilityInputs(
            model_names=model_names,
            model_name_set=frozenset(model_names),
            has_targeted_workers=has_targeted_workers,
            targeted_worker_ids=frozenset(self.get_worker_ids()) if has_targeted_workers else frozenset(),
            tricked_worker_ids=frozenset(tricked.worker_id for tricked in self.tricked_workers),
        )

    def read_privileged_submitted_prompt(self, *, purpose: SubmittedPromptPurpose) -> str | None:
        """Return submission provenance explicitly without populating a deferred ORM attribute.

        Ordinary queries and joins omit this sensitive column; accidental lazy
        access raises. Only requester serialization and moderation evidence should
        call this method. A missing original never falls back to the worker prompt.

        Args:
            purpose: Which of the two approved consumers is reading.

        Returns:
            The submitted string, or None when provenance was not captured.

        Raises:
            RuntimeError: A detached instance requires a database read.
        """
        if "submitted_prompt" not in inspect(self).unloaded:
            return self.submitted_prompt
        session = object_session(self)
        if session is None:
            raise RuntimeError("Read submitted prompt before releasing the request's database session")
        return session.scalar(select(WaitingPrompt.submitted_prompt).where(WaitingPrompt.id == self.id))

    def get_submitted_request(self) -> dict[str, Any]:
        """Return the request as this row records it, shaped like the generation input model.

        The values are the normalized ones the horde acts on rather than the bytes the client sent: omitted
        parameters are filled with their defaults, but the prompt is the original submission, before
        styles or moderation. Legacy rows omit it. This is not an exact replay of styled requests.
        Submission-time controls that leave no trace on the row (dry_run, allow_downgrade,
        replacement_filter, style) are not reported. Subclasses add the fields of their own input model.

        Callers must authorize the requester first; assert_submitting_key is that check.

        Returns:
            Original prompt and normalized parameters for the authenticated requester.
        """
        params = dict(self.params)
        params["n"] = self.jobs
        return {
            "prompt": self.read_privileged_submitted_prompt(purpose=SubmittedPromptPurpose.REQUEST_OWNER),
            "params": params,
            "models": self.get_model_names(),
            "workers": [str(allowed.worker_id) for allowed in self.workers],
            "worker_blacklist": self.worker_blacklist,
            "trusted_workers": self.trusted_workers,
            "validated_backends": self.validated_backends,
            "slow_workers": self.slow_workers,
            "extra_slow_workers": self.extra_slow_workers,
            "disable_batching": self.disable_batching,
            "webhook": self.webhook,
            "proxied_account": self.proxied_account,
            "extra_source_images": (self.extra_source_images or {}).get("esi"),
        }

    # These are typically horde-specific so they will be defined in the specific class for this horde type
    def extract_params(self):
        # logger.debug(self.params)
        if self.user.education:
            self.nsfw = False
        self.n = self.params.pop("n", 1)
        # We store the original amount of jobs requested as well
        self.jobs = self.n
        # This specific per horde so it should be set in the extended class
        self.things = 0
        self.total_usage = round(self.things * self.n, 2)
        self.prepare_job_payload()
        db.session.commit()

    def prepare_job_payload(self):
        # This is what we send to the worker
        self.gen_payload = self.params
        db.session.commit()

    def get_job_payload(self, current_n):
        return self.gen_payload

    def get_generation_assignment(self, worker) -> dict[str, Any]:
        """Return extra columns a new generation for this request records about its worker.

        Subclass Integration:
            Override where a gentype snapshots part of the worker's declaration at assignment.

        Args:
            worker: The worker the generation is assigned to.

        Returns:
            Keyword arguments for the generation's constructor; none by default.
        """
        return {}

    def needs_gen(self):
        return self.n > 0

    def start_generation(
        self,
        worker,
        amount: int = 1,
        declared_models: list[str] | None = None,
        declared_softprompts: list[str] | None = None,
        *,
        selected_from_priority_queue: bool = False,
        priority_user_ids: Sequence[int | str] = (),
    ):
        """Assign up to ``amount`` of this request's waiting jobs to ``worker`` and return the pop payload.

        Concurrency:
            The taken jobs are committed with the first generation's row rather than on their own, so a status read
            never sees them taken without a generation. The request's row lock from the pop query is therefore held
            across the model selection and that INSERT.
        """
        with logfire.span(
            "horde.wp.start_generation",
            wp_id=str(self.id),
            worker_id=str(worker.id),
            amount=amount,
        ) as gen_span:
            return self._start_generation(
                worker,
                amount,
                gen_span,
                declared_models,
                declared_softprompts,
                selected_from_priority_queue=selected_from_priority_queue,
                priority_user_ids=priority_user_ids,
            )

    def _select_model(self, worker, declared_models: list[str] | None = None) -> str:
        """Choose the single model recorded for every procgen in a batch.

        ``declared_models`` is the model list the pop SQL matched this WP
        against (the worker's declared list in the pop payload). It is the set
        the worker committed to running, so the recorded model is drawn from it
        rather than from later-read worker state. When absent (callers outside
        the pop flow), the worker's currently hosted models stand in for it.
        """
        if declared_models is None:
            declared_models = list(worker.get_model_names())
            # A cache/session race right after check-in can momentarily return
            # an empty hosted-model list, so fall back to a direct DB read.
            if len(declared_models) == 0:
                from horde.classes.base.worker import WorkerModel

                declared_models = [
                    row.model for row in db.session.query(WorkerModel.model).filter(WorkerModel.worker_id == worker.id).all()
                ]
        wp_models = list(self.get_model_names())
        if len(wp_models) != 0:
            candidates = [model for model in wp_models if model in declared_models]
        else:
            candidates = list(declared_models)
        # The pop SQL matched this WP against ``declared_models``, so an empty
        # overlap means the WP carried no constraint the match narrowed on; any
        # declared model is one the worker will actually run.
        if len(candidates) == 0:
            candidates = list(declared_models)
        if len(candidates) == 0:
            # The worker declared no models in its pop, so there is no committed list to draw from.
            # The WP's own request list is the next-most-truthful record; a blank model is worse than
            # either, because workers reject a nameless job outright and fault it back for reissue.
            candidates = list(wp_models)
        if len(candidates) == 0:
            logger.warning(
                f"No models available to record for a generation on WP {self.id} by worker {worker.id}. "
                f"WP Models: {wp_models}. Declared Models: {declared_models}.",
            )
            return ""
        random.shuffle(candidates)
        return candidates[0]

    def _start_generation(
        self,
        worker,
        amount=1,
        gen_span=None,
        declared_models: list[str] | None = None,
        declared_softprompts: list[str] | None = None,
        *,
        selected_from_priority_queue: bool = False,
        priority_user_ids: Sequence[int | str] = (),
    ):
        # # We have to do this to lock the row for updates, to ensure we don't have racing conditions on who is picking up requests
        # myself_refresh = db.session.query(
        #     type(self)
        # ).filter(
        #     type(self).id == self.id,
        #     type(self).n > 0
        # ).with_for_update().populate_existing().first()
        # if not myself_refresh:
        #     return None
        # myself_refresh.n -= 1
        safe_amount = worker.get_safe_amount(amount, self)
        if safe_amount > self.n:
            safe_amount = self.n
        if self.disable_batching:
            safe_amount = 1
        # We use a local var to avoid touching the DB through self.n
        # due to all the commits clearing row lock,
        # can we can't ensure a race-condition won't have changed self.n between iterations
        current_n = self.n
        could_be_first_assignment = current_n == self.jobs
        self.n -= safe_amount
        payload = self.get_job_payload(current_n)
        # Not committed here: the first generation's constructor commits the taken jobs with it. Committed alone,
        # a restarted request would show no job waiting, none processing and its faulted ones finished, which reads
        # as completed.
        self.refresh(worker, commit=False)
        procgen_class = procgen_classes[self.wp_type]
        gens_list = []
        dispatch_batch_id = uuid.uuid4()
        # Select the batch's model once from the pop-declared list so every
        # procgen records the same model the worker committed to running.
        model = self._select_model(worker, declared_models)
        generation_assignment = self.get_generation_assignment(worker)
        while safe_amount >= 1:
            safe_amount -= 1
            current_n -= 1
            new_gen = procgen_class(
                wp_id=self.id,
                worker_id=worker.id,
                model=model,
                dispatch_batch_id=dispatch_batch_id,
                **generation_assignment,
            )
            logger.info(
                f"Procgen with ID {new_gen.id} popped from WP {self.id} by worker {worker.id} "
                f"('{worker.name}' / {worker.ipaddr}) - {current_n} gens left",
            )
            gens_list.append(new_gen)
            if self.faulted:
                break
        if not gens_list:
            # No generation constructor committed the expiry refresh.
            db.session.commit()
        if gen_span is not None:
            gen_span.set_attribute("horde.procgens_created", len(gens_list))
        if gens_list:
            model_names = declared_models if declared_models is not None else list(worker.get_model_names())
            softprompt_names = declared_softprompts or []
            record_worker_dispatch(
                DispatchObservation(
                    dispatch_id=str(dispatch_batch_id),
                    worker_id=str(worker.id),
                    worker_state=build_worker_scheduling_state(
                        gentype=self.wp_type,
                        model_names=model_names,
                        bridge_agent=worker.bridge_agent,
                        softprompt_names=softprompt_names,
                    ),
                    request_id=str(self.id),
                    request_created_at=self.created,
                    request_extra_priority=int(self.extra_priority),
                    selected_from_priority_queue=selected_from_priority_queue,
                    priority_user_ids=tuple(str(user_id) for user_id in priority_user_ids),
                    dispatched_at=gens_list[0].created,
                    assigned_work=self.things * len(gens_list) / hv.thing_divisors[self.wp_type],
                ),
            )
        if could_be_first_assignment and gens_list:
            first_real_generation = (
                db.session.query(procgen_class.id)
                .filter(
                    procgen_class.wp_id == self.id,
                    procgen_class.fake.is_(False),
                )
                .order_by(procgen_class.created.asc(), procgen_class.id.asc())
                .first()
            )
            if first_real_generation is not None and first_real_generation.id == gens_list[0].id:
                started_at = gens_list[0].created
                start_delay = max((started_at - self.created).total_seconds(), 0)
                request_time_to_first_start.record(start_delay, {"horde.gentype": self.wp_type})
                try:
                    record_request_start_forecast(
                        request_id=str(self.id),
                        gentype=self.wp_type,
                        started_at=started_at,
                        request_expires_at=self.expiry,
                    )
                except Exception as err:
                    logger.warning(f"Unable to validate scheduling forecast for request {self.id}: {err}")
        pop_payload = self.get_pop_payload(gens_list, payload)
        return pop_payload

    def fake_generation(self, worker):
        payload = self.get_job_payload(self.n)
        procgen_class = procgen_classes[self.wp_type]
        new_gen = procgen_class(wp_id=self.id, worker_id=worker.id, fake=True, **self.get_generation_assignment(worker))
        new_trick = WPTrickedWorkers(wp_id=self.id, worker_id=worker.id)
        db.session.add(new_trick)
        db.session.commit()
        logger.info(
            f"FAKE Procgen with ID {new_gen.id} popped from WP {self.id} by worker {worker.id} "
            f"('{worker.name}' / {worker.ipaddr}) - {self.n} gens left",
        )
        return self.get_pop_payload([new_gen], payload)

    def tricked_worker(self, worker):
        return worker.id in [w.worker_id for w in self.tricked_workers]

    def get_pop_payload(self, procgen_list, payload):
        prompt_payload = {
            "payload": payload,
            "id": procgen_list[0].id,
            "model": procgen_list[0].model,
            "ids": [g.id for g in procgen_list],
            "ttl": procgen_list[0].job_ttl,
        }
        if self.traceparent:
            prompt_payload["traceparent"] = self.traceparent
        if self.extra_source_images and check_bridge_capability("extra_source_images", procgen_list[0].worker.bridge_agent):
            prompt_payload["extra_source_images"] = self.extra_source_images["esi"]

        return prompt_payload

    def count_finished_jobs(self):
        procgen_class = procgen_classes[self.wp_type]
        return (
            db.session.query(procgen_class.wp_id)
            .filter(
                procgen_class.wp_id == self.id,
                procgen_class.fake.is_(False),
                or_(
                    procgen_class.faulted.is_(True),
                    procgen_class.generation != None,  # noqa E712
                ),
            )
            .count()
        )

    def count_processing_jobs(self):
        procgen_class = procgen_classes[self.wp_type]
        return (
            db.session.query(procgen_class.wp_id)
            .filter(
                procgen_class.wp_id == self.id,
                procgen_class.fake.is_(False),
                procgen_class.faulted.is_(False),
                procgen_class.generation.is_(None),
            )
            .count()
        )

    def read_completion_snapshot(self) -> RequestCompletionSnapshot:
        """Return the request's queued jobs, fault/cancellation state and generation counts in one statement.

        The values loaded on this instance are not used: they can predate a fault or a pop that the counts include.
        A request whose row was deleted at expiry after this instance loaded has no generations left, so its loaded
        ``n`` and fault flag decide.
        """
        procgen_class = procgen_classes[self.wp_type]
        # One grouped pass over the generation rows reads every count; a correlated subquery per count would scan them
        # once per count on every submit, status read and completion check. The derived table is still part of the same
        # statement as ``n``, so the counts stay consistent with it.
        unfinished = procgen_class.generation.is_(None)
        generation_counts = (
            select(
                procgen_class.wp_id.label("wp_id"),
                func.count().filter(procgen_class.faulted.is_(False), procgen_class.generation.is_not(None)).label("completed_jobs"),
                func.count().filter(procgen_class.faulted.is_(False), unfinished).label("processing_jobs"),
                func.count().filter(procgen_class.faulted.is_(True), unfinished).label("faulted_jobs"),
            )
            .where(
                procgen_class.wp_id == self.id,
                procgen_class.fake.is_(False),
            )
            .group_by(procgen_class.wp_id)
            .subquery()
        )
        fault_reason_column, fault_rc_column = self.fault_detail_columns()
        # The outer join keeps the request row when it has no generations; its counts then read null.
        completion_row = (
            db.session.query(
                WaitingPrompt.n,
                WaitingPrompt.faulted,
                func.coalesce(generation_counts.c.completed_jobs, 0),
                func.coalesce(generation_counts.c.processing_jobs, 0),
                func.coalesce(generation_counts.c.faulted_jobs, 0),
                fault_reason_column,
                fault_rc_column,
                WaitingPrompt.terminal_outcome,
            )
            .outerjoin(generation_counts, generation_counts.c.wp_id == WaitingPrompt.id)
            .filter(WaitingPrompt.id == self.id)
            .one_or_none()
        )
        if completion_row is None:
            return RequestCompletionSnapshot(
                n=self.n,
                faulted=self.faulted,
                completed_jobs=0,
                processing_jobs=0,
                fault_reason=None,
                fault_rc=None,
                cancelled=self.terminal_outcome == RequestTerminalOutcome.CANCELLED.value,
            )
        (
            stored_n,
            stored_faulted,
            completed_job_count,
            processing_job_count,
            faulted_job_count,
            stored_fault_reason,
            stored_fault_rc,
            terminal_outcome,
        ) = completion_row
        return RequestCompletionSnapshot(
            n=stored_n,
            faulted=stored_faulted,
            completed_jobs=completed_job_count,
            processing_jobs=processing_job_count,
            faulted_jobs=faulted_job_count,
            fault_reason=stored_fault_reason,
            fault_rc=stored_fault_rc,
            cancelled=terminal_outcome == RequestTerminalOutcome.CANCELLED.value,
        )

    def fault_detail_columns(self) -> tuple[ColumnElement[str | None], ColumnElement[str | None]]:
        """Return the columns the completion snapshot reads the request's fault message and return code from.

        Requests that store no fault details read two nulls.
        """
        return null(), null()

    def status_fault_details(self, completion: RequestCompletionSnapshot) -> dict[str, Any]:
        """Return the fault fields the status adds for ``completion``; none for requests that store no fault details."""
        return {}

    def is_completed(self) -> bool:
        """Return whether the request is faulted, or settled with all results stored or remaining jobs cancelled.

        Decided on one [`read_completion_snapshot`][horde.classes.base.waiting_prompt.WaitingPrompt.read_completion_snapshot].
        """
        return self.read_completion_snapshot().is_completed(self.jobs)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
            "restarted": 0,
        }
        for procgen in self.processing_gens:
            if procgen.fake:
                continue
            if procgen.is_completed():
                ret_dict["finished"] += 1
            elif procgen.is_faulted():
                ret_dict["restarted"] += 1
            else:
                ret_dict["processing"] += 1
        return ret_dict

    # FIXME: Looks like this is not used anywhere
    # def get_queued_things(self):
    #     '''The things still queued to be generated for this waiting prompt'''
    #     return round(self.things * self.n ,2)

    def get_generations(self):
        generations = []
        for procgen in self.processing_gens:
            if procgen.fake:
                continue
            if procgen.is_completed():
                generations.append(procgen.get_details())
        return generations

    def get_status(
        self,
        request_avg: float,
        active_worker_count: tuple[int, int],
        has_valid_workers: bool,
        wp_queue_stats: tuple[int, float, int],
        lite: bool = False,
        *,
        eligible_workers: int | None = None,
        eligible_worker_threads: int | None = None,
        might_stall: bool = False,
        forecast_queue_stats: tuple[int, float, int] | None = None,
        things_per_second: float | None = None,
    ) -> dict[str, Any]:
        """Return the request's current generation and queue status.

        The completion snapshot is the first read, and the generations are loaded after it, so every generation the
        snapshot counts as finished is in the list: a status is never done without the generation that completed it,
        nor done from a fault it does not report. The caller must therefore not load this request's
        ``processing_gens`` earlier in the session; an earlier load would keep generations as they were then. The
        status routes load only the request and the worker availability and queue figures before calling this.

        Args:
            request_avg: Horde-wide average generation throughput.
            active_worker_count: Counts of the workers and threads whose throughput the wait estimate assumes: the
                horde's active workers, or for a text request the workers eligible for it.
            has_valid_workers: Whether the request is currently feasible.
            wp_queue_stats: Queue position, and the work and generation counts up to and including this request: of
                the whole queue, or for a text request of the requests its eligible workers could take.
            lite: Whether to omit completed generation payloads.
            eligible_workers: Exact number of workers eligible for this request.
            eligible_worker_threads: Advertised threads across eligible workers.
            might_stall: Cached assignment-pressure advisory for this request.
            forecast_queue_stats: The horde-wide queue statistics the scheduling forecast reads, when
                ``wp_queue_stats`` counts only part of the queue; ``wp_queue_stats`` otherwise. The forecast keeps
                the global queue because compatible workers do not share one candidate queue.
            things_per_second: The combined measured throughput, in things per second, of the threads in
                ``active_worker_count``; None estimates it as ``request_avg`` for each of those threads.

        Returns:
            Materialized request status ready for API serialization.
        """
        completion = self.read_completion_snapshot()
        active_worker_thread_count = active_worker_count[1]
        ret_dict = {
            "finished": completion.completed_jobs,
            "processing": completion.processing_jobs,
            "restarted": completion.faulted_jobs,
        }
        # `n` can still count a slot that a generation has claimed: a pop writes
        # `n` from the value it loaded, so concurrent pops of one request can
        # leave a single decrement for two generations, reporting more
        # outstanding generations than the request asked for. The slot count
        # reconciles `waiting` against the counts actually reported. A faulted
        # generation returns its slot to `n` and is reported separately as
        # `restarted`, so it is not subtracted here. `jobs` is zero on rows
        # predating the column, where no reconciliation is possible.
        waiting = max(completion.n, 0)
        if self.jobs > 0:
            waiting = min(waiting, max(self.jobs - ret_dict["processing"] - ret_dict["finished"], 0))
        ret_dict["waiting"] = waiting
        # This might still happen due to a race condition on parallel requests. Not sure how to avoid it.
        if completion.n < 0:
            logger.error("Request was popped more times than requested!")

        ret_dict["done"] = completion.is_completed(self.jobs)
        ret_dict["faulted"] = completion.faulted
        ret_dict.update(self.status_fault_details(completion))
        # Lite mode does not include the generations, to spare me download size
        if not lite:
            ret_dict["generations"] = self.get_generations()

        queue_pos, queued_things, queued_n = wp_queue_stats
        # We increment the priority by 1, because it starts at -1
        # This means when all our requests are currently processing or done, with nothing else in the queue,
        # we'll show queue position 0 which is appropriate.
        ret_dict["queue_position"] = queue_pos + 1
        if things_per_second is None:
            things_per_second = (request_avg / hv.thing_divisors[self.wp_type]) * active_worker_thread_count
        wait_time = estimate_queue_wait_seconds(
            queued_things=queued_things,
            queued_jobs=queued_n,
            worker_threads=active_worker_thread_count,
            things_per_second=things_per_second,
        )
        # We add the expected running time of our processing gens
        highest_expected_time_left = 0
        for procgen in self.processing_gens:
            expected_time_left = procgen.get_expected_time_left()
            if expected_time_left > highest_expected_time_left:
                highest_expected_time_left = expected_time_left
        wait_time += highest_expected_time_left
        ret_dict["wait_time"] = round(wait_time)
        ret_dict["kudos"] = round(self.consumed_kudos)
        # The caller samples the validity verdict before these counts are taken, so
        # it can predate an in-flight generation and contradict the processing count
        # it is reported beside. An in-flight generation means a worker is serving
        # this request, which is the same conclusion `wp_has_valid_workers` reaches
        # from its own in-flight check.
        ret_dict["is_possible"] = has_valid_workers or ret_dict["processing"] > 0

        # These additive fields make the deliberately broad `is_possible`
        # verdict easier to interpret. A matching worker may be busy, and a
        # single matching thread can be starved by higher-priority work even
        # while the horde-wide queue looks healthy.
        availability_known = eligible_workers is not None and eligible_worker_threads is not None
        if not availability_known:
            # Preserve useful output for internal/legacy callers that only
            # provide the boolean verdict. API status routes pass exact counts.
            eligible_workers = int(bool(has_valid_workers))
            eligible_worker_threads = int(bool(has_valid_workers))
        eligible_workers = max(int(eligible_workers), 0)
        eligible_worker_threads = max(int(eligible_worker_threads), 0)
        ret_dict["eligible_workers"] = eligible_workers
        ret_dict["eligible_worker_threads"] = eligible_worker_threads

        status_time = datetime.utcnow()
        ret_dict["might_stall"] = False
        if availability_known and ret_dict["waiting"] > 0 and ret_dict["processing"] == 0:
            ret_dict["might_stall"] = might_stall
        if availability_known and ret_dict["waiting"] > 0 and ret_dict["processing"] == 0:
            thing_divisor = hv.thing_divisors[self.wp_type]
            own_queued_work = self.things * ret_dict["waiting"] / thing_divisor
            already_started = ret_dict["finished"] > 0 or ret_dict["restarted"] > 0 or completion.n < self.jobs
            _, forecast_queued_things, forecast_queued_n = forecast_queue_stats or wp_queue_stats
            if forecast_queued_things >= own_queued_work and forecast_queued_n >= ret_dict["waiting"]:
                try:
                    forecast = calculate_scheduling_forecast(
                        forecasted_at=status_time,
                        request_expires_at=self.expiry,
                        queued_work=forecast_queued_things,
                        own_work=own_queued_work,
                        queued_jobs=forecast_queued_n,
                        own_jobs=ret_dict["waiting"],
                        average_work_per_second=request_avg / thing_divisor,
                        eligible_worker_threads=eligible_worker_threads,
                    )
                    store_scheduling_forecast(
                        request_id=str(self.id),
                        forecast=forecast,
                        request_expires_at=self.expiry,
                        already_started=already_started,
                    )
                except Exception as err:
                    logger.warning(f"Unable to create scheduling forecast for request {self.id}: {err}")
        return ret_dict

    def get_lite_status(self, **kwargs: Any) -> dict[str, Any]:
        """Return the request status without completed generation payloads."""

        ret_dict = self.get_status(lite=True, **kwargs)
        return ret_dict

    # This should be overriden by each extending class
    def calculate_kudos(self):
        """Returns the expected kudos a worker will receive for this request"""
        # Dummy ammount
        return 10

    def calculate_extra_kudos_burn(self, kudos):
        """Extend this function to add extra kudos burn for the particular use
        Which represents the cost of each job to the horde infrastructure
        """
        return kudos

    def record_usage(
        self,
        raw_things: float,
        kudos: float,
        usage_type: str,
        avoid_burn: bool = False,
        commit: bool = True,
        *,
        style_reward: StyleReward | None = None,
    ) -> Decimal:
        """Record that we received a requested generation and how much kudos it costs us
        We use 'thing' here as we do not care what type of thing we're recording at this point
        This avoids me having to extend this just to change a var name

        Args:
            raw_things: The things generated.
            kudos: The kudos to debit, before any extra burn.
            usage_type: The record the usage counts under.
            avoid_burn: Debit ``kudos`` without the extra burn.
            commit: Commit the session afterwards.
            style_reward: The style surcharge ``kudos`` includes, carried to the requester's debit.

        Returns:
            The part of the debit the minimum-balance floor forgave inline (see ``User.record_usage``).
        """
        if not avoid_burn:
            kudos = self.calculate_extra_kudos_burn(kudos)
        if self.sharedkey_id is not None:
            self.sharedkey.consume_kudos(kudos, commit=False)
        forgiven = self.user.record_usage(
            raw_things,
            kudos,
            usage_type,
            commit=False,
            reservation_id=f"upfront:{self.id}",
            style_reward=style_reward,
        )
        self.consumed_kudos = round(self.consumed_kudos + kudos, 2)
        self.refresh(commit=commit)
        return forgiven

    def extrapolate_dry_run_kudos(self, extra_source_images_count=0, kudos_adjustment=0):
        kudos = self.calculate_kudos()
        # The horde tax has to match the one _activate() charges, or the quote underpromises
        # what the same request costs when it's not a dry run.
        horde_tax = 1 + (EXTRA_SOURCE_IMAGE_HORDE_TAX * extra_source_images_count) + kudos_adjustment
        return (self.calculate_extra_kudos_burn(kudos) * self.n) + horde_tax

    def log_faulted_prompt(self) -> None:
        """Extendable function to log why a request was aborted"""
        logger.warning(
            f"Faulting waiting prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs",
        )

    def mark_faulted_after_repeated_faults(self) -> bool:
        """Fault the request once more than `FAULTED_GENERATIONS_LIMIT` of its generations have faulted, uncommitted.

        The caller commits, so the fault that crossed the limit and the request's fault become visible together. The
        submit route applies the rule through
        [`fault_and_restart`][horde.classes.base.processing_generation.ProcessingGeneration.fault_and_restart]; the
        stale-generation thread applies the same limit to every request in its own pass.

        Returns:
            True when this call faulted the request.
        """
        procgen_class = procgen_classes[self.wp_type]
        faulted_generations = (
            db.session.query(procgen_class.id)
            .filter(
                procgen_class.wp_id == self.id,
                procgen_class.faulted.is_(True),
            )
            .count()
        )
        if faulted_generations <= FAULTED_GENERATIONS_LIMIT:
            return False
        return self._claim_fault()

    def mark_faulted_when_stranded(self) -> bool:
        """Fault the request, uncommitted, when no job waits or processes and fewer results are stored than it asked for.

        A faulted job that is not restarted, such as an image job once the request has as many finished or faulted
        jobs as it asked for, leaves such a request with nothing more to do; without the fault it would never complete.

        Returns:
            True when this call faulted the request.
        """
        completion = self.read_completion_snapshot()
        has_all_results = completion.completed_jobs >= self.jobs
        if completion.faulted or completion.cancelled or not completion.is_settled() or has_all_results:
            return False
        return self._claim_fault()

    def _claim_fault(self) -> bool:
        """Fault the request and claim its faulted terminal outcome, uncommitted, unless it is already faulted.

        Returns:
            True when this call faulted the request.
        """
        faulted_at = datetime.utcnow()
        # Conditional on the stored flag so a concurrent submit or the thread cannot fault and log the request twice.
        updated_rows = (
            db.session.query(WaitingPrompt)
            .filter(
                WaitingPrompt.id == self.id,
                WaitingPrompt.faulted.is_(False),
            )
            .update(
                {
                    WaitingPrompt.faulted: True,
                    WaitingPrompt.terminal_outcome: case(
                        (WaitingPrompt.terminal_outcome.is_(None), RequestTerminalOutcome.FAULTED.value),
                        else_=WaitingPrompt.terminal_outcome,
                    ),
                    WaitingPrompt.terminal_recorded_at: case(
                        (WaitingPrompt.terminal_outcome.is_(None), faulted_at),
                        else_=WaitingPrompt.terminal_recorded_at,
                    ),
                },
                synchronize_session="fetch",
            )
        )
        return updated_rows == 1

    def requeue_faulted_job(self) -> None:
        """Put one faulted job back as an SQL-side ``n = n + 1``, uncommitted, unless maintenance cancelled it.

        The increment is computed by the database, so it cannot overwrite a pop's decrement committed after this
        instance loaded ``n``.
        """
        db.session.query(WaitingPrompt).filter(
            WaitingPrompt.id == self.id,
            or_(WaitingPrompt.terminal_outcome.is_(None), WaitingPrompt.terminal_outcome != RequestTerminalOutcome.CANCELLED.value),
        ).update(
            {WaitingPrompt.n: WaitingPrompt.n + 1},
            synchronize_session=False,
        )

    def report_repeated_faults(self) -> None:
        """Report a request faulted for repeated faults, once its fault is committed."""
        self.log_faulted_prompt()

    def report_stranded_fault(self) -> None:
        """Report a request faulted because a fault left it with nothing to do and results missing, once committed.

        Only image requests can be left so: a chat or text fault always restarts its job.
        """
        logger.warning(f"Faulting waiting prompt {self.id}: a faulted job was not restarted and no other job remains")

    def delete(self):
        from horde.database.kudos_reservations import release_reservation

        release_reservation(f"upfront:{self.id}")
        for gen in self.processing_gens:
            if not self.faulted and not gen.fake:
                gen.cancel()
            gen.delete()
        for worker in self.workers:
            db.session.delete(worker)
        for tricked_worker in self.tricked_workers:
            db.session.delete(tricked_worker)
        for model in self.models:
            db.session.delete(model)
        db.session.delete(self)
        db.session.commit()

    def abort_for_maintenance(self):
        """Cancel queued jobs, retaining partial results and allowing in-flight jobs to settle before reporting done."""
        try:
            if self.is_completed():
                return
            cancelled_at = datetime.utcnow()
            cancellation_claimed = self.claim_terminal_outcome(
                RequestTerminalOutcome.CANCELLED,
                recorded_at=cancelled_at,
                commit=False,
            )
            self.n = 0
            db.session.commit()
            if cancellation_claimed:
                record_request_cancellation_forecast(request_id=str(self.id), cancelled_at=cancelled_at)
        except Exception as err:
            logger.warning(f"Error when aborting WP. Skipping: {err}")

    def refresh(self, worker=None, commit=True):
        if worker is not None and worker.extra_slow_worker is True:
            self.expiry = get_extra_slow_expiry_date()
        else:
            new_expiry = get_expiry_date()
            if self.expiry < new_expiry:
                self.expiry = new_expiry
        if commit:
            db.session.commit()

    def is_stale(self):
        if datetime.utcnow() > self.expiry:
            return True
        return False

    def claim_terminal_outcome(
        self,
        outcome: RequestTerminalOutcome,
        *,
        recorded_at: datetime | None = None,
        commit: bool = True,
    ) -> bool:
        """Claim the request's first terminal outcome.

        Args:
            outcome: Terminal state to claim.
            recorded_at: Observation time stored with the outcome.
            commit: Whether to commit the claim immediately.

        Returns:
            True when this call claimed the previously unset outcome.

        Concurrency:
            The conditional update arbitrates concurrent completion,
            cancellation, and expiry paths in PostgreSQL.
        """

        terminal_recorded_at = recorded_at or datetime.utcnow()
        updated_rows = (
            db.session.query(WaitingPrompt)
            .filter(
                WaitingPrompt.id == self.id,
                WaitingPrompt.terminal_outcome.is_(None),
            )
            .update(
                {
                    WaitingPrompt.terminal_outcome: outcome.value,
                    WaitingPrompt.terminal_recorded_at: terminal_recorded_at,
                },
                synchronize_session="fetch",
            )
        )
        if commit:
            db.session.commit()
        return updated_rows == 1

    def get_priority(self):
        return self.extra_priority

    def refresh_worker_cache(self):
        worker_ids = [worker.worker_id for worker in self.workers]
        worker_string_ids = [str(worker.worker_id) for worker in self.workers]
        try:
            hr.horde_r_setex(
                f"wp_{self.id}_worker_cache",
                timedelta(minutes=20),
                json.dumps(worker_string_ids),
            )
        except Exception as err:
            logger.debug(f"Error when trying to set workers cache: {err}. Retrieving from DB.")
        return worker_ids

    def get_worker_ids(self):
        if hr.horde_r is None:
            return [worker.worker_id for worker in self.workers]
        worker_cache = hr.horde_r_get(f"wp_{self.id}_worker_cache")
        if not worker_cache:
            return self.refresh_worker_cache()
        try:
            worker_cache = json.loads(worker_cache)
        except TypeError:
            logger.error(f"Worker cache could not be loaded: {worker_cache}")
            return self.refresh_worker_cache()
        if worker_cache is None:
            return self.refresh_worker_cache()
        return [uuid.UUID(wid) for wid in worker_cache]

    # To override
    def get_amount_calculation_things(self):
        return self.things
