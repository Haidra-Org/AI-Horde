# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat mix's native client: drives the Horde's chat routes where they differ from the front.

It polls a chat request to the end and checks its structured reply, reuses an operation key unchanged and changed,
asks for a dry run, reads the declaration gap from the status, and sends text requests to a chat worker's model and to
a chat-only worker's model. Every task waits for the persona it needs to be online. An unexpected status is a contract
violation unless it reports a deployment condition, which fails as an environment condition.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from .chat_checks import CONTRACT_VIOLATION_PREFIX, BenignRefusal, classify_benign_refusal, classify_environment_condition, error_code_of
from .chat_mix_settings import (
    back_off,
    chat_mix_settings,
    fail_contract,
    fail_environment,
    fire_check_failure,
    fire_row,
    record_benign,
    record_measurement,
)
from .chat_worker_script import ChatPersonaRole, nonce_marker
from .helpers import _headers, _pick_requestor_key, _safe_json

__all__ = ["ChatMixNativeClient"]

CHAT_TAG: str = "chat"
"""The tag every chat mix task carries."""

NATIVE_CHAT_ASYNC_PATH: str = "/api/v2/generate/chat/async"
"""The native route a chat request is queued on."""

NATIVE_CHAT_STATUS_PATH: str = "/api/v2/generate/chat/status"
"""The native chat status and cancel route, followed by the request id."""

TEXT_ASYNC_PATH: str = "/api/v2/generate/text/async"
"""The native text request route."""

TEXT_STATUS_PATH: str = "/api/v2/generate/text/status"
"""The native text status and cancel route, followed by the request id."""

NATIVE_STATUS_POLL_SECONDS: float = 7.0
"""The pause between two native status reads; the status routes allow 10 reads a minute per path."""

NATIVE_COMPLETION_DEADLINE_SECONDS: float = 300.0
"""How long a native client polls before it gives up and cancels."""

NATIVE_GAP_POLLS: int = 3
"""How many status reads a native client makes looking for the declaration gap before it cancels."""

NATIVE_SUBMIT_SPACING_SECONDS: float = 0.6
"""The pause between back-to-back native submits; the generate routes allow 2 a second per key."""

EXCERPT_CHARACTERS: int = 200
"""How much of a body a failure quotes."""


class ChatMixNativeClient(HttpUser):
    """Drive the native chat routes where they differ from the front: polling, operation keys, dry runs, the
    declaration gap on the status, and text requests beside chat workers."""

    weight = 1
    fixed_count = 0
    wait_time = between(2, 5)

    def on_start(self) -> None:
        """Take a requestor key; the client talks to the run's ``--host``."""
        self.api_key = _pick_requestor_key()

    @tag(CHAT_TAG, "chat-native")
    @task(4)
    def native_round_trip(self) -> None:
        """Queue a chat request, poll its status to the end, and check the structured reply and its accounting."""
        if not chat_mix_settings.is_persona_online(ChatPersonaRole.CONFORMANT):
            return
        scenario_name = "native_round_trip"
        nonce = uuid.uuid4().hex[:12]
        body = {
            "model": chat_mix_settings.models.conformant,
            "messages": [{"role": "user", "content": f"Say hello. {nonce_marker(nonce)}"}],
            "max_tokens": 64,
        }
        submitted_at = time.time()
        request_id = self._submit_chat(scenario_name, body, expected_status=202)
        if request_id is None:
            return
        self._check_route_separation(request_id)
        status_body = self._poll_until_finished(NATIVE_CHAT_STATUS_PATH, request_id, scenario_name)
        if status_body is None:
            return
        record_measurement(self.environment, "[submit-to-done] native chat", time.time() - submitted_at)
        generations = status_body.get("generations") or []
        if generations:
            record_measurement(self.environment, "[queue_wait_seconds] native chat", generations[0].get("queue_wait_seconds"))
        violations = find_finished_chat_violations(request_id, status_body, nonce=nonce)
        if violations:
            self._fail_check(scenario_name, violations)

    @tag(CHAT_TAG, "chat-native")
    @task(2)
    def native_operation_key(self) -> None:
        """Submit with an operation key, repeat it unchanged (same request, 200, nothing new queued) and changed
        (``OperationKeyConflict``), then cancel."""
        if not chat_mix_settings.is_persona_online(ChatPersonaRole.CONFORMANT):
            return
        scenario_name = "native_operation_key"
        operation_key = str(uuid.uuid4())
        body = {
            "model": chat_mix_settings.models.conformant,
            "messages": [{"role": "user", "content": f"Say hello. {nonce_marker(operation_key[:8])}"}],
            "max_tokens": 64,
            "horde": {"operation_key": operation_key},
        }
        request_id = self._submit_chat(scenario_name, body, expected_status=202)
        if request_id is None:
            return
        time.sleep(NATIVE_SUBMIT_SPACING_SECONDS)
        repeated_id = self._submit_chat(f"{scenario_name} repeat", body, expected_status=200)
        if repeated_id is not None and repeated_id != request_id:
            self._fail_check(f"{scenario_name} repeat", [f"the repeated operation key returned request {repeated_id}, not {request_id}"])
        time.sleep(NATIVE_SUBMIT_SPACING_SECONDS)
        self._submit_expecting_refusal(f"{scenario_name} changed", {**body, "max_tokens": 65}, expected_rc="OperationKeyConflict")
        self._cancel(NATIVE_CHAT_STATUS_PATH, request_id, scenario_name)

    @tag(CHAT_TAG, "chat-native")
    @task(1)
    def native_dry_run(self) -> None:
        """Ask for a quote: 200 with the kudos and no request id, so nothing is queued. The model has to be known, which
        an unlisted model is only while its worker is online."""
        if not chat_mix_settings.is_persona_online(ChatPersonaRole.CONFORMANT):
            return
        name = f"{NATIVE_CHAT_ASYNC_PATH} [native_dry_run]"
        body = {
            "model": chat_mix_settings.models.conformant,
            "messages": [{"role": "user", "content": "Quote this."}],
            "max_tokens": 64,
            "horde": {"dry_run": True},
        }
        with self.client.post(NATIVE_CHAT_ASYNC_PATH, json=body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if self._handled_as_benign_or_environment(resp, name, "native_dry_run"):
                return
            quote = _safe_json(resp) or {}
            if resp.status_code != 200:
                fail_contract(resp, "native_dry_run", [f"a dry run answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"])
                return
            if "id" in quote or not isinstance(quote.get("kudos"), int | float):
                fail_contract(
                    resp, "native_dry_run", [f"a dry run returned {json.dumps(quote)[:EXCERPT_CHARACTERS]}, expected kudos and no id"]
                )
                return
            resp.success()

    @tag(CHAT_TAG, "chat-native")
    @task(1)
    def native_declaration_gap(self) -> None:
        """Queue a forced function on the limited model under ``strict``: the status reports the declaration gap
        naming ``tool_choice_enforced``. Then cancel."""
        if not chat_mix_settings.is_persona_online(ChatPersonaRole.LIMITED):
            return
        scenario_name = "native_declaration_gap"
        body = {
            "model": chat_mix_settings.models.limited,
            "messages": [{"role": "user", "content": f"Hello. {nonce_marker(uuid.uuid4().hex[:12])}"}],
            "max_tokens": 64,
            "temperature": 0.2,
            "tools": [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object", "properties": {}}}}],
            "tool_choice": {"type": "function", "function": {"name": "get_time"}},
        }
        request_id = self._submit_chat(scenario_name, body, expected_status=202)
        if request_id is None:
            return
        name = f"{NATIVE_CHAT_STATUS_PATH}/[id] [{scenario_name}]"
        found_gap = False
        last_reason: Any = None
        for _poll in range(NATIVE_GAP_POLLS):
            time.sleep(NATIVE_STATUS_POLL_SECONDS)
            status_body = self._read_status(NATIVE_CHAT_STATUS_PATH, request_id, name)
            if status_body is None:
                continue
            last_reason = status_body.get("impossible_reason")
            if _reports_tool_choice_gap(last_reason):
                found_gap = status_body.get("is_possible") is False
                break
        if not found_gap:
            self._fail_check(
                scenario_name,
                [f"the status never reported the declaration gap; last impossible_reason {json.dumps(last_reason)[:EXCERPT_CHARACTERS]}"],
            )
        self._cancel(NATIVE_CHAT_STATUS_PATH, request_id, scenario_name)

    @tag(CHAT_TAG, "chat-native", "text")
    @task(1)
    def native_text_beside_chat(self) -> None:
        """Queue a text request on the limited model: its chat worker also accepts text jobs, pops it without a
        ``chat`` key and submits plain generation text, so text is unaffected by chat."""
        if not chat_mix_settings.is_persona_online(ChatPersonaRole.LIMITED):
            return
        scenario_name = "native_text_beside_chat"
        request_id = self._submit_text(scenario_name, chat_mix_settings.models.limited)
        if request_id is None:
            return
        status_body = self._poll_until_finished(TEXT_STATUS_PATH, request_id, scenario_name)
        if status_body is None:
            return
        generations = status_body.get("generations") or []
        if status_body.get("faulted") or not generations or not generations[0].get("text"):
            self._fail_check(
                scenario_name, [f"the text request finished without a generation: {json.dumps(status_body)[:EXCERPT_CHARACTERS]}"]
            )
        elif "message" in generations[0]:
            self._fail_check(scenario_name, ["a text generation carries a chat message"])

    @tag(CHAT_TAG, "chat-native", "text")
    @task(1)
    def native_text_to_chat_only_model(self) -> None:
        """Queue a text request on the conformant model, whose only worker sets ``prompt_jobs: false``: no worker can
        serve it, so the status reports it impossible. Then cancel."""
        if not chat_mix_settings.is_persona_online(ChatPersonaRole.CONFORMANT):
            return
        scenario_name = "native_text_to_chat_only_model"
        request_id = self._submit_text(scenario_name, chat_mix_settings.models.conformant)
        if request_id is None:
            return
        time.sleep(NATIVE_STATUS_POLL_SECONDS)
        status_body = self._read_status(TEXT_STATUS_PATH, request_id, f"{TEXT_STATUS_PATH}/[id] [{scenario_name}]")
        if status_body is not None and (status_body.get("is_possible") is not False or status_body.get("done")):
            reported = f"is_possible={status_body.get('is_possible')!r} done={status_body.get('done')!r}"
            self._fail_check(scenario_name, [f"a text request for a model only a chat-only worker serves reports {reported}"])
        self._cancel(TEXT_STATUS_PATH, request_id, scenario_name)

    def _submit_chat(self, scenario_name: str, body: dict[str, Any], *, expected_status: int) -> str | None:
        """Submit a chat request and return its id, or None after a benign refusal or a failure."""
        name = f"{NATIVE_CHAT_ASYNC_PATH} [{scenario_name}]"
        with self.client.post(NATIVE_CHAT_ASYNC_PATH, json=body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if self._handled_as_benign_or_environment(resp, name, scenario_name):
                return None
            submit_body = _safe_json(resp) or {}
            request_id = submit_body.get("id")
            if resp.status_code != expected_status:
                fail_contract(
                    resp,
                    scenario_name,
                    [f"the submit answered {resp.status_code}, expected {expected_status}: {resp.text[:EXCERPT_CHARACTERS]}"],
                )
                return None
            if not request_id or not isinstance(submit_body.get("kudos"), int | float):
                fail_contract(
                    resp, scenario_name, [f"the submit returned {json.dumps(submit_body)[:EXCERPT_CHARACTERS]}, expected an id and kudos"]
                )
                return None
            resp.success()
            return str(request_id)

    def _submit_expecting_refusal(self, scenario_name: str, body: dict[str, Any], *, expected_rc: str) -> None:
        """Submit a chat request the Horde has to refuse with ``expected_rc``."""
        name = f"{NATIVE_CHAT_ASYNC_PATH} [{scenario_name}]"
        with self.client.post(NATIVE_CHAT_ASYNC_PATH, json=body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if self._handled_as_benign_or_environment(resp, name, scenario_name):
                return
            if resp.status_code != 400 or error_code_of(_safe_json(resp)) != expected_rc:
                fail_contract(
                    resp, scenario_name, [f"expected 400 {expected_rc}, got {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"]
                )
                return
            resp.success()

    def _submit_text(self, scenario_name: str, model: str) -> str | None:
        """Submit a short text request for ``model`` and return its id, or None after a refusal."""
        name = f"{TEXT_ASYNC_PATH} [{scenario_name}]"
        body = {
            "prompt": f"Once upon a time ({uuid.uuid4().hex[:12]})",
            "params": {"max_length": 32, "max_context_length": 1024},
            "models": [model],
        }
        with self.client.post(TEXT_ASYNC_PATH, json=body, headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if self._handled_as_benign_or_environment(resp, name, scenario_name):
                return None
            request_id = (_safe_json(resp) or {}).get("id")
            if resp.status_code != 202 or not request_id:
                fail_contract(resp, scenario_name, [f"the text submit answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"])
                return None
            resp.success()
            return str(request_id)

    def _check_route_separation(self, chat_request_id: str) -> None:
        """Check a chat request's id is not found on the text status route."""
        name = f"{TEXT_STATUS_PATH}/[id] [chat id]"
        with self.client.get(
            f"{TEXT_STATUS_PATH}/{chat_request_id}", headers=_headers(self.api_key), catch_response=True, name=name
        ) as resp:
            if self._handled_as_benign_or_environment(resp, name, "route separation", back_off_after_rate_limit=False):
                return
            if resp.status_code != 404:
                fail_contract(resp, "route separation", [f"a chat id on the text status route answered {resp.status_code}, expected 404"])
                return
            resp.success()

    def _poll_until_finished(self, status_path: str, request_id: str, scenario_name: str) -> dict[str, Any] | None:
        """Poll a status until it is done or faulted and return it; cancel and return None at the deadline."""
        name = f"{status_path}/[id] [{scenario_name}]"
        deadline = time.time() + NATIVE_COMPLETION_DEADLINE_SECONDS
        while time.time() < deadline:
            time.sleep(NATIVE_STATUS_POLL_SECONDS)
            status_body = self._read_status(status_path, request_id, name)
            if status_body is not None and (status_body.get("done") or status_body.get("faulted")):
                return status_body
        fire_row(
            self.environment,
            request_type="GET",
            name=f"{name} [benign gave up]",
            response_time_ms=NATIVE_COMPLETION_DEADLINE_SECONDS * 1000,
        )
        self._cancel(status_path, request_id, scenario_name)
        return None

    def _read_status(self, status_path: str, request_id: str, name: str) -> dict[str, Any] | None:
        """Read one status and return it, or None after a rate limit or a failure."""
        with self.client.get(f"{status_path}/{request_id}", headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if self._handled_as_benign_or_environment(resp, name, name, back_off_after_rate_limit=False):
                return None
            if resp.status_code != 200:
                fail_contract(resp, name, [f"the status answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"])
                return None
            resp.success()
            return _safe_json(resp)

    def _cancel(self, status_path: str, request_id: str, scenario_name: str) -> None:
        """Cancel a request; a request already gone (404) is fine."""
        name = f"{status_path}/[id] [cancel {scenario_name}]"
        with self.client.delete(f"{status_path}/{request_id}", headers=_headers(self.api_key), catch_response=True, name=name) as resp:
            if self._handled_as_benign_or_environment(resp, name, f"cancel {scenario_name}", back_off_after_rate_limit=False):
                return
            if resp.status_code not in (200, 404):
                fail_contract(
                    resp, f"cancel {scenario_name}", [f"the cancel answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"]
                )
                return
            resp.success()

    def _fail_check(self, scenario_name: str, violations: list[str]) -> None:
        """Record a contract violation found across several requests on a row of its own."""
        fire_check_failure(
            self.environment,
            name=f"[native] {scenario_name}",
            message=f"{CONTRACT_VIOLATION_PREFIX} [{scenario_name}]: {'; '.join(violations[:4])}",
        )

    def _handled_as_benign_or_environment(self, resp: Any, name: str, subject: str, *, back_off_after_rate_limit: bool = True) -> bool:
        """Return True after recording a transport failure, a deployment condition or a benign refusal for ``resp``.

        Raises:
            RescheduleTask: After a rate limit's back-off, when ``back_off_after_rate_limit`` is set.
        """
        if resp.status_code == 0:
            resp.failure(f"Transport error: {resp.error}")
            return True
        body = _safe_json(resp)
        error_code = error_code_of(body)
        condition = classify_environment_condition(resp.status_code, error_code, has_envelope=isinstance(body, dict))
        if condition is not None:
            fail_environment(resp, condition, subject)
            return True
        benign_refusal = classify_benign_refusal(resp.status_code, error_code)
        if benign_refusal is None:
            return False
        record_benign(self.environment, resp, name, benign_refusal.value)
        if back_off_after_rate_limit and benign_refusal == BenignRefusal.RATE_LIMITED:
            back_off(resp)
            raise RescheduleTask()
        return True


def _reports_tool_choice_gap(impossible_reason: Any) -> bool:
    """Return whether an ``impossible_reason`` is a declaration gap naming ``tool_choice_enforced``."""
    if not isinstance(impossible_reason, dict) or impossible_reason.get("kind") != "declaration_gap":
        return False
    return "tool_choice_enforced" in (impossible_reason.get("missing_declarations") or [])


def find_finished_chat_violations(request_id: str, status_body: dict[str, Any], *, nonce: str) -> list[str]:
    """Return how a finished native chat status breaks the contract: one generation whose structured reply answers this
    request, finished with ``stop``, charged on the output estimate, with its queue wait.

    Args:
        request_id: The request's id, for the message.
        status_body: The finished status.
        nonce: The request's nonce, which the reply has to carry.
    """
    generations = status_body.get("generations") or []
    if status_body.get("faulted") or len(generations) != 1:
        return [f"request {request_id} finished faulted={status_body.get('faulted')!r} with {len(generations)} generations"]
    generation = generations[0]
    message = generation.get("message") or {}
    violations: list[str] = []
    content = message.get("content")
    if message.get("role") != "assistant" or not isinstance(content, str) or nonce not in content:
        violations.append(f"the generation's message is not this request's reply: {json.dumps(message)[:EXCERPT_CHARACTERS]}")
    if generation.get("finish_reason") != "stop":
        violations.append(f"the generation's finish_reason is {generation.get('finish_reason')!r}")
    if generation.get("charge_basis") != "output_estimate":
        violations.append(f"the generation's charge_basis is {generation.get('charge_basis')!r}")
    queue_wait = generation.get("queue_wait_seconds")
    if not isinstance(queue_wait, int | float) or queue_wait < 0:
        violations.append(f"the generation's queue_wait_seconds is {queue_wait!r}")
    return violations
