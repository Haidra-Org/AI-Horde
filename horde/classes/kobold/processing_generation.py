# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import enum
import math
import os
import time
from datetime import UTC, datetime

from sqlalchemy.sql import expression

from horde import vars as hv
from horde.bridge_reference import (
    is_backed_validated,
    parse_bridge_agent,
)
from horde.chat_progress import (
    PostedProgress,
    StreamLease,
    end_progress_stream,
    extend_stream_lease,
    release_stream_lease,
    write_stream_lease,
)
from horde.chat_signal import ChatEvent, publish_chat_event
from horde.classes.base.processing_generation import ProcessingGeneration, json_column_type
from horde.classes.kobold.chat_request import (
    CHAT_SUBMIT_NONCONFORMANT_RC,
    MAX_CHAT_REFUSAL_RC_LENGTH,
    ChargeBasis,
    chat_reply_characters,
    streams_progress,
)
from horde.classes.kobold.genstats import record_text_statistic
from horde.classes.kobold.worker import (
    TEXT_JOB_LEASE_SECONDS,
    TextWorker,
    get_effective_text_worker_speed,
    get_text_worker_lease_capacity,
)
from horde.flask import db
from horde.logger import logger
from horde.metrics import (
    TEXT_LENGTH_BUCKETS,
    TextRequester,
    submit_genstats_record_duration,
    submit_state_handling_duration,
    text_empty_generations,
    text_gate_shadow_assignments,
    text_gate_shadow_outcomes,
    text_gate_shadow_service_seconds,
    text_lease_renewals,
    text_queue_wait_seconds,
    text_stale_aborts,
)
from horde.model_reference import model_reference
from horde.suspicions import Suspicions

UNKNOWN_ATTRIBUTE_VALUE = "unknown"
"""Placeholder for a metric attribute the submission did not include."""

TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS = 600
"""The longest a trusted worker's chat job lease runs from pop once progress posts extend it. A trusted worker has a
record of completing jobs, so a slow reply that keeps adding output is waited for longer."""

UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS = 200
"""The longest an untrusted worker's chat job lease runs from pop once progress posts extend it. A worker that posts
a trickle of output holds the job from other workers until the cap, so the cap stays close to the base lease."""


class LeaseRenewal(enum.StrEnum):
    """What a progress post that added output did to its job's lease."""

    GRANTED = "granted"
    """The lease was extended."""

    AT_CAP = "at_cap"
    """The lease already reached its cap and was not extended."""

    UNCHANGED = "unchanged"
    """The lease already ends at least as late as the post would extend it to."""

    LAPSED = "lapsed"
    """The lease ended before the post; the stale-generation thread aborts the job."""


class GateShadowOutcome(enum.StrEnum):
    """How a legacy text job assigned over its worker's lease capacity ended."""

    COMPLETED = "completed"
    """The worker submitted a generation that was stored."""

    FAULTED = "faulted"
    """The worker submitted a fault."""

    ABORTED = "aborted"
    """The lease lapsed before a submit and the stale-generation thread aborted the job."""


def text_job_lease_cap(trusted: bool) -> int:
    """Return the longest a text job lease may run from pop once progress extends it.

    Args:
        trusted: Whether the owner of the worker holding the job is trusted.

    Returns:
        The cap in seconds.
    """
    if trusted:
        return TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS
    return UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS


def renew_text_job_ttl(elapsed_seconds: float, job_ttl: int, lease_cap: int) -> tuple[LeaseRenewal, int]:
    """Decide the ``job_ttl`` after a progress post that added output.

    The lease is extended to end [`TEXT_JOB_LEASE_SECONDS`][horde.classes.kobold.worker.TEXT_JOB_LEASE_SECONDS] after
    the post, never past ``lease_cap`` from pop.

    Args:
        elapsed_seconds: Seconds from the generation's start to the post.
        job_ttl: The generation's current ``job_ttl``.
        lease_cap: The longest ``job_ttl`` the lease may reach.

    Returns:
        The renewal outcome and the ``job_ttl`` after it.
    """
    if elapsed_seconds > job_ttl:
        return LeaseRenewal.LAPSED, job_ttl
    # Whole seconds, as the column and the pop's ``ttl`` are; rounding up keeps the full allowance after the post.
    requested_ttl = math.ceil(elapsed_seconds) + TEXT_JOB_LEASE_SECONDS
    renewed_ttl = min(requested_ttl, lease_cap)
    if renewed_ttl > job_ttl:
        return LeaseRenewal.GRANTED, renewed_ttl
    if requested_ttl > lease_cap:
        return LeaseRenewal.AT_CAP, job_ttl
    return LeaseRenewal.UNCHANGED, job_ttl


def renew_text_job_lease(lease: StreamLease, now: float | None = None) -> LeaseRenewal | None:
    """Extend a chat job's lease after its worker posted progress that added output.

    The stream lease carries the job lease's start, ``job_ttl`` and cap, so a post that does not extend the lease does
    not touch Postgres. An extension is written to the generation only while it is pending, and then to the stream
    lease, which outlives the job lease by its grace.

    Args:
        lease: The generation's stream lease, as the progress route read it before the append.
        now: The post's time as Unix seconds; the current time when omitted.

    Returns:
        The renewal outcome, or None when the lease predates the job lease fields.
    """
    if lease.started_at is None or lease.job_ttl is None or lease.lease_cap is None:
        return None
    if now is None:
        now = time.time()
    outcome, job_ttl = renew_text_job_ttl(now - lease.started_at, lease.job_ttl, lease.lease_cap)
    trust_attributes = {"horde.worker_trusted": str(lease.trusted).lower()}
    if outcome is LeaseRenewal.AT_CAP:
        text_lease_renewals.add(1, {"horde.outcome": outcome.value, **trust_attributes})
    if outcome is not LeaseRenewal.GRANTED:
        return outcome
    # A compare-and-set on the row: only a pending generation is extended, and only upward, so a submit, fault or a
    # concurrent post that already extended further is not overwritten. For a target job_ttl of 180:
    #
    # id | generation | faulted | job_ttl | rows updated
    # G1 | NULL       | false   | 150     | 1 (job_ttl becomes 180)
    # G2 | "reply"    | false   | 150     | 0
    # G3 | NULL       | false   | 190     | 0
    extended_rows = (
        db.session.query(TextProcessingGeneration)
        .filter(
            TextProcessingGeneration.id == lease.generation_id,
            TextProcessingGeneration.generation.is_(None),
            TextProcessingGeneration.faulted.is_(False),
            TextProcessingGeneration.job_ttl < job_ttl,
        )
        .update({TextProcessingGeneration.job_ttl: job_ttl}, synchronize_session=False)
    )
    db.session.commit()
    if extended_rows != 1:
        # Submitted, faulted or extended further by a concurrent post since the lease was read.
        return LeaseRenewal.UNCHANGED
    text_lease_renewals.add(1, {"horde.outcome": outcome.value, **trust_attributes})
    extend_stream_lease(lease.generation_id, job_ttl, math.ceil(lease.started_at + job_ttl - now))
    return outcome


def is_empty_text_generation(generation: str | None) -> bool:
    """Report whether a submitted text generation has no visible characters.

    Args:
        generation: The text the worker submitted, if any.

    Returns:
        True when the submission is missing, empty, or whitespace only.
    """
    if generation is None:
        return True
    return not generation.strip()


def is_empty_submission(generation: str | None, message: object) -> bool:
    """Report whether a submission is an empty generation for the empty-generation counter.

    A chat reply that calls tools is complete without any text, so it is never empty.

    Args:
        generation: The text the worker submitted, or a chat reply's content.
        message: The chat reply as stored, or None for a text submission.

    Returns:
        True when the text has no visible characters and the reply carries no tool calls.
    """
    calls_tools = isinstance(message, dict) and bool(message.get("tool_calls"))
    return is_empty_text_generation(generation) and not calls_tools


def build_empty_generation_attributes(model: str | None, bridge_agent: str | None, state: str) -> dict[str, str]:
    """Build the metric attributes for an empty text generation.

    The bridge agent is reduced to its name so the counter is not split across
    every version and worker URL of the same bridge.

    Args:
        model: The model the worker reported serving.
        bridge_agent: The worker's raw agent string, e.g. ``"KoboldCppEmbedWorker:2:https://..."``.
        state: The state the worker submitted the generation under.

    Returns:
        The attribute mapping to pass to the empty-generation counter.
    """
    bridge_name = UNKNOWN_ATTRIBUTE_VALUE
    if bridge_agent is not None:
        bridge_name, _ = parse_bridge_agent(bridge_agent)
    return {
        "model": model if model is not None else UNKNOWN_ATTRIBUTE_VALUE,
        "bridge_agent": bridge_name,
        "state": state,
    }


class TextProcessingGeneration(ProcessingGeneration):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }
    wp = db.relationship("TextWaitingPrompt", back_populates="processing_gens")
    worker = db.relationship("TextWorker", back_populates="processing_gens")
    message = db.Column(json_column_type(none_as_null=True), nullable=True)
    """The assistant message a chat worker submitted, ``{role, content}`` with ``tool_calls`` when the model called
    tools. NULL on text generations. ``generation`` holds the same content, or an empty string when the content is
    null, so every predicate that reads ``generation`` treats chat and text alike."""
    finish_reason = db.Column(db.String(32), nullable=True)
    """Why a chat worker stopped generating, a ``ChatFinishReason`` value. NULL on text generations."""
    native_finish_reason = db.Column(db.String(32), nullable=True)
    """The backend's own name for why it stopped, as a chat worker reported it. NULL when not reported."""
    prompt_tokens = db.Column(db.Integer, nullable=True)
    """The prompt token count a chat worker reported. NULL when not reported, which a reported 0 is not."""
    completion_tokens = db.Column(db.Integer, nullable=True)
    """The generated token count a chat worker reported. NULL when not reported, which a reported 0 is not."""
    reasoning_tokens = db.Column(db.Integer, nullable=True)
    """How many of the generated tokens a chat worker reported as reasoning. NULL when not reported. Shown and never
    charged or rewarded: the charge basis is the reply the Horde measures."""
    charge_basis = db.Column(db.String(16), nullable=True)
    """The ``ChargeBasis`` the generation was charged and rewarded on, ``output_estimate`` for every chat generation.
    NULL on text generations, which are charged on the same basis without recording it."""
    assigned_model = db.Column(db.String, nullable=True)
    """The model a chat worker declared when the generation was assigned; its submit has to name the same one."""
    assigned_max_context_length = db.Column(db.Integer, nullable=True)
    """The chat context length the worker declared when the generation was assigned."""
    assigned_max_length = db.Column(db.Integer, nullable=True)
    """The chat output length the worker declared when the generation was assigned."""
    image_transport = db.Column(db.String(16), nullable=True)
    """How the generation's worker receives the request's messages and images, a ``ChatImageTransport`` value chosen
    from its declaration at assignment: ``messages_url`` when the messages were offloaded and the worker declared it,
    else the image transport. NULL when the messages are sent inline without images and on text generations."""
    chat_refusal_rc = db.Column(db.String(MAX_CHAT_REFUSAL_RC_LENGTH), nullable=True)
    """The return code the Horde last refused this generation's reply with, written at the refusal. The worker's
    fault submit that follows is counted by it, so a worker cannot claim the exemption a nonconformant reply has from
    dropped jobs. NULL while no reply was refused and on text generations."""
    settled_character_count = db.Column(db.Integer, nullable=True)
    """The posted character count a cancelled streamed generation was priced on, written by
    [`settle_cancelled`][horde.classes.kobold.processing_generation.TextProcessingGeneration.settle_cancelled] and
    read by ``get_things_count`` in place of a stored reply. The posted text is not retained and a stored
    ``generation`` would count the faulted generation as a delivered result, so this count is the record cancels are
    analysed by. NULL on every other generation."""
    over_capacity_at_assignment = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        server_default=expression.false(),
    )
    """Whether a legacy text job's ``max_length`` exceeded the worker's lease capacity when it was assigned. The speed
    gate is enforced for chat requests only, so the outcome of such an assignment is recorded against this marker
    rather than against the worker's speed at submit, which moves between pop and submit. False on chat and fake
    generations."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        over_capacity = self._assignment_exceeds_lease_capacity(kwargs)
        if over_capacity:
            # Passed as a column value, so the base constructor's commit of the new row writes the marker.
            kwargs["over_capacity_at_assignment"] = True
        super().__init__(*args, **kwargs)
        self._write_stream_lease()
        if over_capacity:
            self._record_over_capacity_assignment()
        self._record_queue_wait()

    @classmethod
    def _assignment_exceeds_lease_capacity(cls, assignment: dict[str, object]) -> bool:
        """Return whether an assignment gives a legacy text job to a worker whose speed does not finish it in the base lease.

        The speed gate refuses such a job to a chat request only; for a legacy request the assignment proceeds and is
        marked, so the effect the gate would have is measurable. A fake generation is not a real assignment and is
        not marked.

        Args:
            assignment: The constructor's keyword arguments, which carry the request and worker ids.
        """
        if assignment.get("fake"):
            return False
        # Imported here because the request module imports this one.
        from horde.classes.kobold.waiting_prompt import TextWaitingPrompt

        # The pop passes ids rather than rows. The request and worker rows it locked and loaded are in the session, so
        # get() takes them from the identity map.
        waiting_prompt = db.session.get(TextWaitingPrompt, assignment["wp_id"])
        worker = db.session.get(TextWorker, assignment["worker_id"])
        if waiting_prompt is None or worker is None or waiting_prompt.is_chat:
            return False
        return waiting_prompt.max_length > get_text_worker_lease_capacity(worker.speed)

    def _record_queue_wait(self) -> None:
        """Record the time from the request's creation to this assignment; a fake generation is not an assignment."""
        if self.fake:
            return
        requester = TextRequester.from_request(
            anonymous=self.wp.user.is_anon(),
            shared_key=self.wp.sharedkey_id is not None,
        )
        text_queue_wait_seconds.record(
            max((self.start_time - self.wp.created).total_seconds(), 0),
            {
                "horde.requester": requester.value,
                "horde.chat": bool(self.wp.is_chat),
                "horde.requested_bucket": TEXT_LENGTH_BUCKETS.label(self.wp.max_length),
            },
        )

    def _gate_shadow_attributes(self) -> dict[str, str]:
        """Return the metric attributes of a gate shadow record: the worker owner's trust and the request's size."""
        return {
            "horde.worker_trusted": str(bool(self.worker.user.trusted)).lower(),
            "horde.size_bucket": TEXT_LENGTH_BUCKETS.label(self.wp.max_length),
        }

    def _record_over_capacity_assignment(self) -> None:
        """Count and log a legacy text job assigned over its worker's lease capacity, once its row has an id.

        Side Effects:
            Increments ``horde.text.gate_shadow.assignments`` and logs the assignment with its ids.
        """
        lease_capacity = get_text_worker_lease_capacity(self.worker.speed)
        text_gate_shadow_assignments.add(1, self._gate_shadow_attributes())
        logger.info(
            f"Legacy text generation {self.id} of wp {self.wp_id} assigned over the lease capacity of worker "
            f"{self.worker_id}: max_length={self.wp.max_length} capacity={lease_capacity} "
            f"speed={get_effective_text_worker_speed(self.worker.speed)}",
        )

    def _record_gate_shadow_outcome(self, outcome: GateShadowOutcome) -> None:
        """Count how a legacy text job assigned over its worker's lease capacity ended; other jobs are not counted.

        Args:
            outcome: How the job ended.
        """
        if not self.over_capacity_at_assignment:
            return
        text_gate_shadow_outcomes.add(
            1,
            {
                **self._gate_shadow_attributes(),
                "horde.outcome": outcome.value,
            },
        )

    def set_job_ttl(self) -> None:
        """Persist the base lease, the deadline from pop to submission offered to the worker for this assignment.

        The lease does not grow with ``max_length``, since a worker that pops a job and never submits holds it for
        the whole lease. The pop offers a job only to a worker whose speed finishes its ``max_length`` inside this
        lease, and a chat job's progress posts extend it up to
        [`text_job_lease_cap`][horde.classes.kobold.processing_generation.text_job_lease_cap].
        """
        self.job_ttl = TEXT_JOB_LEASE_SECONDS
        db.session.commit()

    def _write_stream_lease(self) -> None:
        """Write the stream lease of a chat generation whose client streams the reply.

        Runs once the generation and its TTL are committed, so the lease lives exactly as long as the job may. The
        request row and its extras are the ones the pop already loaded to build the job payload. A fake generation
        gets a lease too, so its worker's posts are answered as a real generation's would be.
        """
        if not self.wp.is_chat or not streams_progress(self.wp.chat_extras):
            return
        owner = self.worker.user
        trusted = bool(owner.trusted)
        write_stream_lease(
            str(self.id),
            request_id=str(self.wp_id),
            worker_id=str(self.worker_id),
            model=self.model,
            owner_id=owner.id,
            owner_api_key=owner.api_key,
            max_length=self.wp.max_length,
            job_ttl=self.job_ttl,
            started_at=self.start_time.replace(tzinfo=UTC).timestamp(),
            lease_cap=text_job_lease_cap(trusted),
            trusted=trusted,
            fake=bool(self.fake),
        )

    def record_stale_abort(self) -> None:
        """Count this generation's abort for a lapsed lease, split by whether its worker's owner is trusted.

        A legacy text job assigned over its worker's lease capacity is also counted as an aborted gate shadow outcome.
        """
        text_stale_aborts.add(1, {"horde.worker_trusted": str(bool(self.worker.user.trusted)).lower()})
        self._record_gate_shadow_outcome(GateShadowOutcome.ABORTED)

    def end_progress_after_fault(self) -> None:
        """Append the ended marker for a faulted chat generation that holds a stream lease, and delete the lease.

        Reads the lease rather than the request's extras, so the stale-generation thread does not load a deferred column.
        """
        if self.wp.is_chat:
            end_progress_stream(str(self.id), str(self.wp_id))

    def _censorship_state(self) -> str:
        """Determine the state string for this procgen based on its censorship flags."""
        if not self.censored:
            return "ok"
        gen_metadata = self.gen_metadata if self.gen_metadata is not None else []
        for meta in gen_metadata:
            if isinstance(meta, dict) and meta.get("type") == "censorship" and meta.get("value") == "csam":
                return "csam"
        return "censored"

    def get_details(self):
        """Returns a dictionary with details about this processing generation"""
        ret_dict = {
            "text": self.generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "id": self.id,
            "state": self._censorship_state(),
            "gen_metadata": self.gen_metadata if self.gen_metadata is not None else [],
        }
        # Only the chat status model declares these, and text webhooks send this dict as it is.
        if self.wp.is_chat:
            ret_dict["message"] = self.message
            ret_dict["finish_reason"] = self.finish_reason
            ret_dict["native_finish_reason"] = self.native_finish_reason
            ret_dict["prompt_tokens"] = self.prompt_tokens
            ret_dict["completion_tokens"] = self.completion_tokens
            ret_dict["reasoning_tokens"] = self.reasoning_tokens
            ret_dict["charge_basis"] = self.charge_basis
            ret_dict["queue_wait_seconds"] = max((self.start_time - self.wp.created).total_seconds(), 0)
        return ret_dict

    def get_charge_basis(self) -> ChargeBasis:
        """Return what this generation is charged and rewarded on.

        Every text generation, chat or not, is priced by the text kudos formula, whose token count is
        ``get_things_count``: the returned characters divided by 4, rounded up, at least 20 and at most the
        requested ``max_length``. Reported token counts are stored and shown and never change the charge.

        Returns:
            [`ChargeBasis.OUTPUT_ESTIMATE`][horde.classes.kobold.chat_request.ChargeBasis].
        """
        return ChargeBasis.OUTPUT_ESTIMATE

    def get_submission_columns(self, **kwargs: object) -> dict[str, object]:
        """Return the structured reply a chat worker submitted, written in the same update as ``generation``.

        Args:
            **kwargs: The submission's keyword arguments, which for a chat job carry ``message``,
                ``finish_reason``, ``native_finish_reason``, ``prompt_tokens``, ``completion_tokens`` and
                ``reasoning_tokens``.

        Returns:
            Those values and the charge basis for a chat generation; nothing for a text generation.
        """
        if not self.wp.is_chat:
            return {}
        return {
            "message": kwargs.get("message"),
            "finish_reason": kwargs.get("finish_reason"),
            "native_finish_reason": kwargs.get("native_finish_reason"),
            "prompt_tokens": kwargs.get("prompt_tokens"),
            "completion_tokens": kwargs.get("completion_tokens"),
            "reasoning_tokens": kwargs.get("reasoning_tokens"),
            "charge_basis": self.get_charge_basis().value,
        }

    def get_gen_kudos(self):
        if os.getenv("HORDE_REQUIRE_MATCHED_TARGETING", "0") == "1" and len(self.wp.workers) > 0:
            return 0.1
        # This formula creates an exponential increase on the kudos consumption, based on the context requested
        # 1024 context is considered the base.
        # The reason is that higher context has exponential VRAM requirements
        actual_context_length = self.get_things_count(self.wp.prompt)
        context_multiplier = 1.2 + (2.2 ** (math.log2(actual_context_length / 1024)))
        # Prevent shenanigans
        if context_multiplier > 30:
            context_multiplier = 30
        if context_multiplier < 0.1:
            context_multiplier = 0.1
        # If a worker serves an unknown model, they only get 1 kudos, unless they're trusted in which case they get 20
        if not model_reference.is_known_text_model(self.model):
            if not self.worker.user.trusted:
                return context_multiplier
            # Trusted users with an unknown model are considered as running a 2.7B model
            return self.get_things_count() * context_multiplier * (2.7 / 100)
        # This is the approximate reward for generating with a 2.7 model at 4bit
        model_multiplier = model_reference.get_text_model_multiplier(self.model)
        parameter_bonus = (max(model_multiplier, 13) / 13) ** 0.20
        kudos = self.get_things_count() * parameter_bonus * model_multiplier / 125
        # Unvalidated backends have their rewards cut to 30%
        if not is_backed_validated(self.worker.bridge_agent):
            kudos *= 0.3
        return round(kudos * context_multiplier, 2)

    def log_worker_abort(self) -> None:
        """Count the abort on the worker, as a nonconformant reply when the Horde refused the reply as one.

        The worker generated a well-formed reply that broke a ``strict`` request's contract, which the model failed
        rather than the worker, so it does not count toward the dropped jobs that put a worker into maintenance.
        """
        if self.chat_refusal_rc == CHAT_SUBMIT_NONCONFORMANT_RC:
            self.worker.log_nonconformant_reply()
            return
        super().log_worker_abort()

    def log_aborted_generation(self):
        record_text_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} of wp {str(self.wp_id)} "
            f"(for {self.get_things_count()} tokens and {self.wp.max_context_length} content length) "
            f" from by worker: {self.worker.name} ({self.worker.id})",
        )

    def _record_empty_generation(self, state: str, things_per_sec: float, kudos: float) -> None:
        """Log and count a text generation that arrived with no visible text.

        The submission is still stored and still paid for; this only records the
        event so the behaviour can be measured before any payment rule changes.

        Args:
            state: The state the worker submitted the generation under.
            things_per_sec: The generation speed the worker reported.
            kudos: The kudos the submission was awarded.
        """
        prompt_characters = len(self.wp.prompt) if self.wp.prompt is not None else 0
        logger.warning(
            f"Empty text generation submitted: procgen {self.id} of wp {self.wp_id} "
            f"by worker {self.worker.name} ({self.worker.id}) "
            f"agent '{self.worker.bridge_agent}' model '{self.model}' state '{state}' "
            f"max_length={self.wp.max_length} max_context_length={self.wp.max_context_length} "
            f"prompt_characters={prompt_characters} things_per_sec={things_per_sec} kudos={kudos}",
        )
        text_empty_generations.add(
            1,
            build_empty_generation_attributes(self.model, self.worker.bridge_agent, state),
        )

    def set_generation(self, generation, things_per_sec, **kwargs):
        # We don't check the state in the super() function as image gen sets it early here
        # as well, so it can abort before doing R2 operations
        state_t0 = time.monotonic()
        state = kwargs.get("state", "ok")
        if state == "faulted":
            # Counted only by the submit that faulted the job, so a repeated fault submit is not counted again.
            if self.fault_and_restart():
                self._record_gate_shadow_outcome(GateShadowOutcome.FAULTED)
        elif state in ("censored", "csam"):
            self.censored = True
            db.session.commit()

        # Detect csam from gen_metadata (mirrors the image-side approach).
        # Also inject a csam metadata entry when state == "csam" so the
        # authoritative source (gen_metadata) is always consistent.
        gen_metadata = list(kwargs.get("gen_metadata", self.gen_metadata or []))
        has_csam_meta = any(isinstance(m, dict) and m.get("type") == "censorship" and m.get("value") == "csam" for m in gen_metadata)
        if not has_csam_meta and state == "csam":
            gen_metadata.append({"type": "censorship", "value": "csam"})
            kwargs = {**kwargs, "gen_metadata": gen_metadata}
        elif has_csam_meta:
            self.censored = True
            db.session.commit()
        submit_state_handling_duration.record(time.monotonic() - state_t0, {"horde.gentype": "text"})

        # Checked before super() so the raw submission is examined, but reported
        # after it so the log can include the kudos awarded.
        submission_is_empty = is_empty_submission(generation, kwargs.get("message"))

        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        if submission_is_empty:
            self._record_empty_generation(state, things_per_sec, kudos)
        genstats_t0 = time.monotonic()
        record_text_statistic(self)
        submit_genstats_record_duration.record(time.monotonic() - genstats_t0, {"horde.gentype": "text"})
        # The base returns -1 for a generation that was already faulted and 0 for one already submitted;
        # neither stores this submission.
        if kudos in (-1, 0):
            return kudos
        if self.over_capacity_at_assignment:
            self._record_gate_shadow_outcome(GateShadowOutcome.COMPLETED)
            text_gate_shadow_service_seconds.record(
                max((datetime.utcnow() - self.start_time).total_seconds(), 0),
                self._gate_shadow_attributes(),
            )
        if self.wp.is_chat:
            # The submit route verified the reply against the extras, so reading the hint does not load anything more.
            if streams_progress(self.wp.chat_extras):
                release_stream_lease(str(self.id))
            self._publish_chat_progress()
        return kudos

    def _publish_chat_progress(self) -> None:
        """Signal a waiting chat client that a generation landed, and that the request is done when it is.

        Runs after the submission committed, so a client that reads the status on the signal finds the generation.
        """
        request_id = str(self.wp.id)
        generation_count = self.wp.count_stored_generations()
        publish_chat_event(request_id, ChatEvent.GENERATION, generation_count)
        if self.wp.is_completed():
            publish_chat_event(request_id, ChatEvent.DONE, generation_count)

    def settle_cancelled(self, posted: PostedProgress) -> float | None:
        """Settle a pending streamed generation whose request was cancelled on the content its worker posted.

        The posted content is measured by the server, so it is priced by the output estimate like a submitted reply:
        the worker is paid for what it turned in and the requesting user is charged the same, and the generation is
        faulted as cancelled, without a dropped-job penalty. The row is locked and reloaded first, so a submit or fault
        that lands while the cancel runs settles the generation once: whichever commits first wins, and the other finds
        it finished or faulted.

        Args:
            posted: The content the generation posted, from
                [`read_posted_progress`][horde.chat_progress.read_posted_progress].

        Returns:
            The kudos the worker was paid, or None when the generation had already finished or faulted.
        """
        db.session.refresh(self, with_for_update=True)
        self.settled_character_count = posted.chars
        kudos = self.cancel()
        if kudos is None:
            # A generation that a submit or fault settled first does not keep the count. cancel commits only when it
            # settles, so this commit releases the row lock the refresh took.
            self.settled_character_count = None
            db.session.commit()
        return kudos

    def get_things_count(self, generation=None):
        """Return the output estimate the generation is charged on: its characters divided by 4, rounded up, at least
        20 and at most the requested ``max_length``.

        Args:
            generation: Text to estimate in place of the stored generation.

        Returns:
            The estimate. A stored chat reply is counted by
            [`chat_reply_characters`][horde.classes.kobold.chat_request.chat_reply_characters], so tool calls are
            charged as output. A cancelled streamed generation is counted by the posted characters it was settled on.
        """
        if generation is not None:
            character_count = len(generation)
        elif self.message is not None:
            character_count = chat_reply_characters(self.message)
        elif self.generation is not None:
            character_count = len(self.generation)
        elif self.settled_character_count is not None:
            character_count = self.settled_character_count
        else:
            return 0
        quick_token_count = math.ceil(character_count / 4)
        if quick_token_count < 20:
            quick_token_count = 20
        if self.wp.things > quick_token_count:
            # logger.debug([self.wp.things,quick_token_count])
            return quick_token_count
        return self.wp.things

    def record(self, things_per_sec, kudos):
        # Extended function to try and catch workers using unreasonable
        # speeds at higher params
        # This only affects untrusted workers running known models
        super().record(things_per_sec, kudos)
        if not model_reference.is_known_text_model(self.model):
            return
        if self.worker.user.trusted:
            return
        param_multiplier = model_reference.get_text_model_multiplier(self.model)
        unreasonable_speed = hv.suspicion_thresholds["text"]

        # max_speed_per_multiplier = {
        # 70: 12,
        # 40: 22,
        # 20: 35,
        # 13: 50,
        # 7: 70,
        # }

        # Once upon a time, before batching and other optimizations, these were the speeds we considered unreasonable
        # but new paradigms, backends and breakthroughs have made these numbers increasingly inaccurate or irrelevant.

        max_speed_per_multiplier = {
            70: 30,
            14: 100,
            8: 150,
        }

        for params_count in max_speed_per_multiplier:
            if param_multiplier >= params_count:
                unreasonable_speed = max_speed_per_multiplier[params_count]
                break
        # This handles the 8x7 and 8x22 models which are generally faster than their size implies.
        if "8x" in self.model:
            unreasonable_speed = unreasonable_speed * 3
        if things_per_sec > unreasonable_speed:
            self.worker.report_suspicion(reason=Suspicions.UNREASONABLY_FAST, formats=[f"{things_per_sec} > {unreasonable_speed}"])
