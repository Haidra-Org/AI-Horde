# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat mix's simulated chat worker: pops as one persona, checks each payload against its declaration, and
replies as the client's reply marker asks.

The worker never takes a new identity to get past a refusal. A configured name, owned by ``--chat-mix-worker-api-key``,
backs off after a refused pop and stops after three refusals in a row; a generated name stops at the first identity
refusal, since minting a new worker per refusal would evade the Horde's anti-abuse limits. A worker that stops records
why, and the coverage gate fails the run on it.

A breaker protects the worker name: after three refusals of replies the worker meant to conform, it stops faulting
(each fault would count as an aborted job) and stops, so a Horde regression cannot drive a deployment's worker into
maintenance.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask, StopUser

from tests.integration._fake_chat_worker import build_chat_submit_body, build_faulted_chat_submit_body, build_pop_body

from .chat_checks import classify_environment_condition, error_code_of
from .chat_mix_settings import (
    chat_mix_settings,
    claim_worker_slot,
    fail_contract,
    fail_environment,
    read_worker_counters,
    record_benign,
)
from .chat_wire import TEXT_POP_PATH, TEXT_SUBMIT_PATH, WORKER_BRIDGE_AGENT
from .chat_worker_script import (
    ChatPersona,
    ChatPersonaRole,
    WorkerReply,
    build_misbehaving_reply,
    build_persona,
    build_scripted_reply,
    find_job_payload_violations,
    find_reply_script,
)
from .config import _EXPECTED_RC_RECOVER
from .helpers import _headers, _is_expected_rc, _is_too_many_workers, _pick_worker_key, _random_worker_name, _safe_json
from .users.chat import STREAM_PROGRESS_FIELD, post_progress_while_generating

__all__ = ["ChatMixWorker", "refusal_was_intended", "reply_posts_progress"]

logger = logging.getLogger(__name__)

CHAT_TAG: str = "chat"
"""The tag every chat mix task carries."""

STALE_JOB_RCS: frozenset[str] = frozenset({"InvalidJobID", "InvalidProcGen", "AbortedGen", "DuplicateGen"})
"""The return codes of a submit for a job that is no longer the worker's, such as one the client cancelled."""

WORKER_POLL_MIN_SECONDS: float = 0.5
"""The shortest pause between two pops."""

WORKER_POLL_MAX_SECONDS: float = 1.5
"""The longest pause between two pops."""

IDENTITY_REFUSAL_BACKOFF_SECONDS: float = 30.0
"""How long a configured name waits after its pop was refused, before trying again."""

IDENTITY_REFUSALS_BEFORE_STOP: int = 3
"""How many consecutive pop refusals a configured name takes before its worker stops."""

UNINTENDED_REFUSALS_BEFORE_STOP: int = 3
"""How many refusals of replies the worker meant to conform it takes before its breaker stops it."""

WORKER_TEXT_MAX_LENGTH: int = 512
"""The text output limit a simulated chat worker declares for text jobs."""

WORKER_TEXT_MAX_CONTEXT_LENGTH: int = 2048
"""The text context limit a simulated chat worker declares for text jobs."""

CHARACTERS_PER_TOKEN: int = 4
"""Characters per token in the simulated workers' reported counts."""

UNREPORTED_USAGE_CHANCE: float = 0.1
"""The chance a worker leaves out its token counts, so the front's usage estimate runs."""

TEXT_JOB_GENERATION: str = "Once upon a time there was a stress test that completed successfully."
"""The generation a simulated chat worker submits for a text job."""

REPLY_REFUSED_FAULT_REASON: str = "reply_refused"
"""The ``fault_reason`` a worker sends after the Horde refused its reply."""

EXCERPT_CHARACTERS: int = 200
"""How much of a body a failure quotes."""


class ChatMixWorker(HttpUser):
    """Pop chat jobs as one persona, check each payload against the declaration, and reply as scripted."""

    weight = 1
    fixed_count = 0
    wait_time = between(WORKER_POLL_MIN_SECONDS, WORKER_POLL_MAX_SECONDS)

    def on_start(self) -> None:
        """Take the next worker slot: a persona and a configured name with its key, or a generated name.

        In a distributed run the slot comes from the master, so a name is never popped by two processes.

        Raises:
            StopUser: The run has no slot left or the master did not grant one; the coverage gate fails the run on it.
        """
        self.has_slot: bool = False
        slot = claim_worker_slot(self.environment)
        if slot is None:
            reason = "a chat worker got no worker slot (more workers than --chat-mix-workers, or no answer from the master)"
            logger.error("Chat mix: %s; it stops at once.", reason)
            chat_mix_settings.coverage.stopped_workers.append(reason)
            raise StopUser()
        self.persona_role: ChatPersonaRole = slot.persona
        self.uses_configured_name: bool = slot.configured_name is not None
        self.worker_name: str = slot.configured_name or self._generate_name()
        self.api_key: str = slot.api_key or _pick_worker_key()
        self.persona: ChatPersona = build_persona(self.persona_role, vision=chat_mix_settings.vision)
        self.model: str = chat_mix_settings.model_of(self.persona_role)
        self.rng = random.Random()
        self.consecutive_pop_refusals: int = 0
        self.unintended_refusals: int = 0
        chat_mix_settings.mark_worker_started(self.worker_name, self.persona_role)
        self.has_slot = True
        _snapshot_worker_counters(self.worker_name)

    def on_stop(self) -> None:
        """Take the worker offline, so scenarios needing its persona pause."""
        if self.has_slot:
            chat_mix_settings.mark_worker_offline(self.worker_name)

    def _generate_name(self) -> str:
        """Create a worker name unlikely to collide with another simulated worker's."""
        return _random_worker_name("StressChatMix-", 6)

    @tag(CHAT_TAG, "chat-worker", "worker")
    @task
    def pop_and_serve(self) -> None:
        """Pop one job and serve it: a chat job as the persona and its script say, a text job with plain text.

        With ``--chat-mix-worker-progress``, a chat job whose client streams and whose reply conforms posts the reply
        text as content deltas while it generates, and the submit carries the same text. An acknowledgement reporting
        the request cancelled ends the job without a submit.
        """
        job = self._pop()
        if job is None:
            return
        generation_seconds = self.rng.uniform(chat_mix_settings.gen_time_min_seconds, chat_mix_settings.gen_time_max_seconds)
        if job.get("chat") is not True:
            time.sleep(generation_seconds)
            self._submit_text_job(job)
            return
        payload = job.get("payload") or {}
        reply = self._choose_reply(payload)
        if chat_mix_settings.worker_progress and reply_posts_progress(payload, reply):
            request_cancelled = post_progress_while_generating(
                self.client,
                api_key=self.api_key,
                generation_id=job["id"],
                reply_text=reply.content or "",
                generation_seconds=generation_seconds,
                interval_seconds=chat_mix_settings.worker_progress_interval_seconds,
            )
            if request_cancelled:
                return
        else:
            time.sleep(generation_seconds)
        if reply.fault_reason is not None:
            self._submit_fault(job, reply.fault_reason, name=f"{TEXT_SUBMIT_PATH} [chat fault scripted]")
            return
        self._submit_reply(job, reply)

    def _pop(self) -> dict[str, Any] | None:
        """Pop with the persona's declaration and return the job, or None when there is none or the pop was refused."""
        pop_body = build_pop_body(
            name=self.worker_name,
            models=[self.model],
            bridge_agent=WORKER_BRIDGE_AGENT,
            chat_jobs=True,
            prompt_jobs=self.persona.prompt_jobs,
            chat_features=list(self.persona.features),
            chat_guarantees=list(self.persona.guarantees),
            chat_max_context_length=self.persona.chat_max_context_length,
            chat_max_length=self.persona.chat_max_length,
            tools_max_temperature=self.persona.tools_max_temperature,
            max_context_length=WORKER_TEXT_MAX_CONTEXT_LENGTH,
            max_length=WORKER_TEXT_MAX_LENGTH,
            image_transports=list(self.persona.image_transports) or None,
        )
        pop_body.update({"nsfw": True, "softprompts": [], "threads": 1})
        name = f"{TEXT_POP_PATH} [chat {self.persona_role.value}]"
        with self.client.post(TEXT_POP_PATH, json=pop_body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if resp.status_code == 0:
                resp.failure(f"Transport error: {resp.error}")
                return None
            body = _safe_json(resp)
            if not resp.ok:
                self.handle_pop_refusal(resp, body, name)
                return None
            self.consecutive_pop_refusals = 0
            chat_mix_settings.mark_worker_online(self.worker_name, self.persona_role)
            job = body or {}
            if not job.get("id"):
                resp.success()
                return None
            if job.get("chat") is True:
                violations = find_job_payload_violations(job.get("payload") or {}, self.persona)
                if violations:
                    fail_contract(resp, f"{self.persona_role.value} worker payload", violations)
                    return job
            elif not self.persona.prompt_jobs:
                fail_contract(resp, f"{self.persona_role.value} worker", ["a worker that set prompt_jobs false popped a text job"])
                return job
            resp.success()
            return job

    def handle_pop_refusal(self, resp: Any, body: Any, name: str) -> None:
        """Record a refused pop and decide whether the worker waits, tries again or stops.

        A rate limit is benign. A refusal of the worker's identity (wrong owner, too many workers, maintenance, the
        other anti-abuse codes) takes the persona offline; a configured name waits and stops after three in a row, a
        generated name stops at once. A deployment condition fails as one; anything else is a contract violation.

        Raises:
            RescheduleTask: After a rate limit, or a configured name's refusal short of the limit.
            StopUser: When the worker gives up its identity.
        """
        return_code = error_code_of(body)
        if resp.status_code == 429:
            record_benign(self.environment, resp, name, "rate_limited")
            raise RescheduleTask()
        condition = classify_environment_condition(resp.status_code, return_code, has_envelope=isinstance(body, dict))
        if condition is not None:
            fail_environment(resp, condition, f"{self.persona_role.value} worker pop")
            raise RescheduleTask()
        refuses_identity = resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body))
        if not refuses_identity:
            fail_contract(
                resp, f"{self.persona_role.value} worker pop", [f"the pop answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"]
            )
            return
        record_benign(self.environment, resp, name, str(return_code))
        self.consecutive_pop_refusals += 1
        chat_mix_settings.mark_worker_offline(self.worker_name)
        logger.warning(
            "Chat mix: %s worker %s pop refused with %s (%d in a row)",
            self.persona_role.value,
            self.worker_name,
            return_code,
            self.consecutive_pop_refusals,
        )
        if not self.uses_configured_name or self.consecutive_pop_refusals >= IDENTITY_REFUSALS_BEFORE_STOP:
            refusals = f"{self.consecutive_pop_refusals} pop refusals ({return_code})"
            self._stop(f"the {self.persona_role.value} worker {self.worker_name} stopped after {refusals}")
        time.sleep(IDENTITY_REFUSAL_BACKOFF_SECONDS)
        raise RescheduleTask()

    def _stop(self, reason: str) -> None:
        """Record why the worker stops, for the coverage gate, and stop it.

        Raises:
            StopUser: Always.
        """
        logger.error("Chat mix: %s", reason)
        chat_mix_settings.coverage.stopped_workers.append(reason)
        chat_mix_settings.mark_worker_offline(self.worker_name)
        raise StopUser()

    def _choose_reply(self, payload: dict[str, Any]) -> WorkerReply:
        """Return the reply: the client's script when it wrote one, else the persona's own behaviour."""
        is_unscripted = find_reply_script(payload.get("messages") or []) is None
        if self.persona_role == ChatPersonaRole.FLAKY and is_unscripted and self.rng.random() < chat_mix_settings.flaky_rate:
            return build_misbehaving_reply(payload)
        return build_scripted_reply(payload)

    def _submit_reply(self, job: dict[str, Any], reply: WorkerReply) -> None:
        """Submit a structured reply and check the Horde accepts or refuses it as the contract says; fault a refused
        reply, unless the breaker trips."""
        reported_usage = self.rng.random() >= UNREPORTED_USAGE_CHANCE
        reply_characters = len(reply.content or "") + len(json.dumps(reply.tool_calls or []))
        submit_body = build_chat_submit_body(
            generation_id=job["id"],
            content=reply.content,
            model_id=job.get("model") or self.model,
            finish_reason=reply.finish_reason,
            prompt_tokens=20 if reported_usage else None,
            completion_tokens=max(1, reply_characters // CHARACTERS_PER_TOKEN) if reported_usage else None,
            tool_calls=reply.tool_calls,
        )
        name = f"{TEXT_SUBMIT_PATH} [chat {self.persona_role.value}]"
        with self.client.post(TEXT_SUBMIT_PATH, json=submit_body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if resp.status_code == 0:
                resp.failure(f"Transport error: {resp.error}")
                return
            return_code = error_code_of(_safe_json(resp))
            if resp.ok:
                if reply.breaks_every_contract:
                    fail_contract(
                        resp, f"{self.persona_role.value} worker submit", ["the Horde stored a reply every conformance mode refuses"]
                    )
                    return
                chat_mix_settings.ledger.accepted_replies[self.worker_name] += 1
                resp.success()
                return
            if resp.status_code in (404, 429) or return_code in STALE_JOB_RCS:
                record_benign(self.environment, resp, name, str(return_code or resp.status_code))
                return
            intended = refusal_was_intended(reply, return_code)
            if intended:
                record_benign(self.environment, resp, name, f"refused as intended {return_code}")
            else:
                fail_contract(
                    resp,
                    f"{self.persona_role.value} worker submit",
                    [f"a reply the worker meant to conform was refused: {resp.text[:EXCERPT_CHARACTERS]}"],
                )
        self._count_refusal(return_code)
        if not intended:
            self.unintended_refusals += 1
            if self.unintended_refusals >= UNINTENDED_REFUSALS_BEFORE_STOP:
                # Faulting would count one more aborted job against a worker name the run cannot replace.
                refusals = f"{self.unintended_refusals} replies it meant to conform were refused"
                self._stop(f"the {self.persona_role.value} worker {self.worker_name} stopped: {refusals}")
        # A refusal is definitive: fault the job, as koboldcpp does, so it goes back to the queue at once.
        self._submit_fault(job, REPLY_REFUSED_FAULT_REASON, name=f"{TEXT_SUBMIT_PATH} [chat fault after refusal]")

    def _count_refusal(self, return_code: str | None) -> None:
        """Count a refused reply in the ledger by its return code."""
        if return_code == "ChatSubmitNonconformant":
            chat_mix_settings.ledger.refused_nonconformant[self.worker_name] += 1
        elif return_code == "ChatSubmitInvalid":
            chat_mix_settings.ledger.refused_invalid[self.worker_name] += 1

    def _submit_fault(self, job: dict[str, Any], fault_reason: str, *, name: str) -> None:
        """Fault a chat job with a reason; the Horde answers with a reward of 0."""
        submit_body = build_faulted_chat_submit_body(
            generation_id=job["id"], model_id=job.get("model") or self.model, fault_reason=fault_reason
        )
        with self.client.post(TEXT_SUBMIT_PATH, json=submit_body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            self._judge_plain_submit(resp, name, "fault submit")
            if resp.ok:
                chat_mix_settings.ledger.faults_submitted[self.worker_name] += 1

    def _submit_text_job(self, job: dict[str, Any]) -> None:
        """Submit a text job the way a text worker submits it."""
        name = f"{TEXT_SUBMIT_PATH} [text on chat {self.persona_role.value}]"
        submit_body = {"id": job["id"], "generation": TEXT_JOB_GENERATION, "state": "ok", "seed": self.rng.randint(0, 999_999_999)}
        with self.client.post(TEXT_SUBMIT_PATH, json=submit_body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            self._judge_plain_submit(resp, name, "text submit")
            if resp.ok:
                chat_mix_settings.ledger.text_jobs[self.worker_name] += 1

    def _judge_plain_submit(self, resp: Any, name: str, subject: str) -> None:
        """Mark a fault or text submit: success, a stale job (benign), a deployment condition, or a contract violation."""
        if resp.status_code == 0:
            resp.failure(f"Transport error: {resp.error}")
            return
        body = _safe_json(resp)
        return_code = error_code_of(body)
        if resp.ok:
            resp.success()
            return
        if resp.status_code in (404, 429) or return_code in STALE_JOB_RCS:
            record_benign(self.environment, resp, name, str(return_code or resp.status_code))
            return
        condition = classify_environment_condition(resp.status_code, return_code, has_envelope=isinstance(body, dict))
        if condition is not None:
            fail_environment(resp, condition, f"{self.persona_role.value} worker {subject}")
            return
        fail_contract(
            resp, f"{self.persona_role.value} worker {subject}", [f"answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"]
        )


def reply_posts_progress(payload: dict[str, Any], reply: WorkerReply) -> bool:
    """Return whether a chat job's reply is posted as content deltas while it generates.

    Only a text reply the worker means to conform is posted, for a job whose client streams. A fault, a reply that
    breaks a contract and a reply with tool calls are submitted without progress; koboldcpp does not stream tool jobs
    either, since a streamed tool reply can differ from the submitted one.

    Args:
        payload: The popped chat job's payload.
        reply: The reply the worker will submit.
    """
    client_streams = payload.get(STREAM_PROGRESS_FIELD) is True
    is_conforming_text = bool(reply.content) and not reply.tool_calls and reply.fault_reason is None
    breaks_a_contract = reply.breaks_strict_contract or reply.breaks_every_contract
    return client_streams and is_conforming_text and not breaks_a_contract


def refusal_was_intended(reply: WorkerReply, return_code: str | None) -> bool:
    """Return whether the Horde refused a reply for the reason the worker broke it: nonconformant replies under
    ``strict``, invalid ones under every mode.

    Args:
        reply: The reply the worker submitted.
        return_code: The refusal's return code.
    """
    if return_code == "ChatSubmitNonconformant":
        return reply.breaks_strict_contract
    if return_code == "ChatSubmitInvalid":
        return reply.breaks_every_contract
    return False


def _snapshot_worker_counters(worker_name: str) -> None:
    """Keep a worker's counters at the start of the run, once per name."""
    if worker_name in chat_mix_settings.ledger.counters_at_start:
        return
    counters = read_worker_counters(worker_name)
    if counters is not None:
        chat_mix_settings.ledger.counters_at_start[worker_name] = counters
