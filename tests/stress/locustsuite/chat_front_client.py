# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat mix's front client: sends the scenarios in [`chat_scenarios`][tests.stress.locustsuite.chat_scenarios] to
the OpenAI-compatible front and records each response as the contract judges it.

A response that breaks its scenario's expected outcome fails as a contract violation; a deployment condition fails as
an environment condition; an operating condition (rate limit, full front, too little kudos) is a ``[benign ...]``
success; the expected outcome, a refusal included, is a plain success of the scenario's row.

Two shares change how a request is sent. Under ``--chat-mix-fail-fast-share`` a queued scenario asks
``horde.availability_mode: fail_fast``, and its 503 ``queue_unavailable`` is a benign outcome labelled by the reason the
front states. Under ``--chat-mix-stream-abandon-share`` a streamed request's client closes the connection after
``--chat-mix-stream-abandon-chunks`` content chunks, which makes the front cancel the native request; the row is
``<row> [abandoned]`` and its response length is the bytes received before the close.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import time
import uuid
from collections.abc import Iterator
from typing import Any

import requests
from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from .chat_checks import (
    COMPLETION_ID_PREFIX,
    BenignRefusal,
    CompletionExpectation,
    ErrorExpectation,
    FrontVerdict,
    StreamTranscript,
    classify_environment_condition,
    error_code_of,
    find_abandoned_stream_violations,
    find_cors_violations,
    judge_front_response,
    queue_unavailable_reason,
)
from .chat_mix_settings import (
    back_off,
    chat_mix_settings,
    fail_contract,
    fail_environment,
    record_benign,
    record_measurement,
)
from .chat_scenarios import (
    ANONYMOUS_SHARE_GROUPS,
    FAIL_FAST_SHARE_GROUPS,
    LITE_ORIGIN,
    STRESS_CLIENT_TITLE,
    AuthorizationMode,
    ChatScenario,
    ScenarioContext,
    ScenarioGroup,
    StreamMode,
    as_anonymous,
    as_fail_fast,
    build_authorization_headers,
    pick_scenario_builder,
)
from .chat_wire import CHAT_COMPLETIONS_PATH, CONNECT_TIMEOUT_SECONDS, STREAM_READ_TIMEOUT_SECONDS
from .chat_worker_script import DEFAULT_PERSONA_MAX_LENGTH, ChatPersonaRole, build_persona, find_request_nonce
from .config import _config
from .helpers import _headers, _pick_requestor_key, _safe_json

__all__ = ["ChatMixFrontClient", "build_request_body"]

logger = logging.getLogger(__name__)

CHAT_TAG: str = "chat"
"""The tag every chat mix task carries, so ``--tags chat`` runs only chat and ``--exclude-tags chat`` leaves it out."""

MODELS_PATH: str = "/v1/models"
"""The front's model list."""

NATIVE_CHAT_STATUS_PATH: str = "/api/v2/generate/chat/status"
"""The native chat status route, followed by the request id."""

NONSTREAM_READ_TIMEOUT_SECONDS: float = 330.0
"""How long a non-streamed client waits: the front's default 300 second deadline plus a margin, so its 408 comes first."""

MODEL_CARD_GRACE_SECONDS: float = 60.0
"""How long after a persona's first pop its model card is checked: the model cache and the model list cache lag."""

UPLOAD_RESET_BEFORE_413: str = "upload reset before 413"
"""The benign label of an oversized upload the front cut off before the client read its 413."""

EXCERPT_CHARACTERS: int = 200
"""How much of a body a failure quotes."""

ABANDONED_LABEL: str = "abandoned"
"""The row label of a stream its client closed after the configured content chunks."""

STREAM_READ_CHUNK_BYTES: int = 512
"""The most bytes one read of an abandoning client's stream takes, the size ``requests.iter_lines`` reads by default, so
a line is seen as soon as it arrives."""

CONFORMANT_CARD_PARAMETERS: frozenset[str] = frozenset(
    {
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
    },
)
"""The ``supported_parameters`` a model served by the conformant or flaky persona lists."""

LIMITED_CARD_PARAMETERS: frozenset[str] = frozenset({"max_tokens", "temperature", "top_p", "stop", "seed", "tools"})
"""The ``supported_parameters`` a model served only by the limited persona lists, and no more."""


class ChatMixFrontClient(HttpUser):
    """Send the chat scenarios to the OpenAI-compatible front and check each response against its promised outcome."""

    weight = 1
    fixed_count = 0
    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Point the client at the front and take a requestor key."""
        self.client.base_url = chat_mix_settings.front_host
        self.api_key = _pick_requestor_key()
        self.rng = random.Random()

    @tag(CHAT_TAG, "chat-front")
    @task(10)
    def run_scenario(self) -> None:
        """Send one scenario from a group picked by the run's weights, skipping one whose worker is not online or whose
        budget is spent."""
        weighted_groups = [(group, weight) for group, weight in chat_mix_settings.group_weights.items() if weight > 0]
        if not weighted_groups:
            return
        groups, weights = zip(*weighted_groups, strict=True)
        context = ScenarioContext(
            models=chat_mix_settings.models, worker_max_length=DEFAULT_PERSONA_MAX_LENGTH, nonce=uuid.uuid4().hex[:12]
        )
        scenario = pick_scenario_builder(self.rng.choices(groups, weights=weights)[0], rng=self.rng)(context)
        if self._sends_anonymously(scenario):
            scenario = as_anonymous(scenario)
        if self._sends_fail_fast(scenario):
            scenario = as_fail_fast(scenario)
        if not chat_mix_settings.is_persona_online(scenario.persona):
            return
        if scenario.aborts_worker_jobs and not chat_mix_settings.aborting_budget.try_take():
            return
        if scenario.oversized_body and not chat_mix_settings.oversized_budget.try_take():
            return
        self.send_scenario(scenario)

    def _sends_anonymously(self, scenario: ChatScenario) -> bool:
        """Return whether this request of an ordinary or Lite scenario goes without ``Authorization``, at the run's
        anonymous share."""
        is_eligible = scenario.group in ANONYMOUS_SHARE_GROUPS and scenario.authorization == AuthorizationMode.REQUESTOR_KEY
        return is_eligible and self.rng.random() < chat_mix_settings.anonymous_share

    def _sends_fail_fast(self, scenario: ChatScenario) -> bool:
        """Return whether this request of a queued scenario asks ``horde.availability_mode: fail_fast``, at the run's
        fail-fast share."""
        if chat_mix_settings.fail_fast_share <= 0 or scenario.group not in FAIL_FAST_SHARE_GROUPS:
            return False
        return self.rng.random() < chat_mix_settings.fail_fast_share

    @tag(CHAT_TAG, "chat-front")
    @task(1)
    def check_model_cards(self) -> None:
        """Read the model list as a browser would and check each online persona's card against its declaration.

        The check runs once per ``MODEL_CARD_CHECK_INTERVAL_SECONDS`` per process, however many front clients it runs,
        so the model list sees a fixed rate of reads rather than one growing with the users.
        """
        if not chat_mix_settings.model_card_checks.try_open():
            return
        name = f"{MODELS_PATH} [cards]"
        headers = {**self._headers_for(AuthorizationMode.REQUESTOR_KEY), "Origin": LITE_ORIGIN}
        with self.client.get(MODELS_PATH, headers=headers, catch_response=True, name=name) as resp:
            if resp.status_code == 0:
                resp.failure(f"Transport error: {resp.error}")
                return
            if resp.status_code != 200:
                self._fail_unexpected_status(resp, "model cards")
                return
            violations = find_cors_violations(resp.headers)
            cards = {card.get("id"): card for card in (_safe_json(resp) or {}).get("data") or [] if isinstance(card, dict)}
            for persona, online_since in chat_mix_settings.persona_online_since().items():
                if time.time() - online_since < MODEL_CARD_GRACE_SECONDS:
                    continue
                violations.extend(find_card_violations(persona, cards.get(chat_mix_settings.model_of(persona))))
            if violations:
                fail_contract(resp, "model cards", violations)
                return
            resp.success()

    @tag(CHAT_TAG, "chat-front")
    @task(1)
    def lite_preflight(self) -> None:
        """Send the CORS preflight a browser sends before Lite's POST, and check it allows the call."""
        if chat_mix_settings.group_weights.get(ScenarioGroup.LITE, 0) <= 0:
            return
        preflight_headers = {
            "Origin": LITE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        }
        name = f"{CHAT_COMPLETIONS_PATH} [lite preflight]"
        with self.client.options(CHAT_COMPLETIONS_PATH, headers=preflight_headers, catch_response=True, name=name) as resp:
            if resp.status_code == 0:
                resp.failure(f"Transport error: {resp.error}")
                return
            violations = [] if resp.status_code in (200, 204) else [f"the preflight answered {resp.status_code}"]
            violations.extend(find_cors_violations(resp.headers))
            allowed_headers = (resp.headers.get("access-control-allow-headers") or "").lower()
            if "authorization" not in allowed_headers and allowed_headers != "*":
                violations.append(f"the preflight does not allow the authorization header: {allowed_headers!r}")
            if violations:
                fail_contract(resp, "lite preflight", violations)
                return
            resp.success()

    def send_scenario(self, scenario: ChatScenario) -> None:
        """Send one scenario, judge the response and record the outcome.

        Raises:
            RescheduleTask: After a back-off for a rate limit or a full front, so the client starts afresh.
        """
        streamed = self._decides_to_stream(scenario)
        include_usage = (
            streamed and scenario.stream_mode == StreamMode.EITHER and self.rng.random() < chat_mix_settings.include_usage_chance
        )
        abandon_after = self._abandon_after(scenario, streamed=streamed)
        request_body = build_request_body(scenario, streamed=streamed, include_usage=include_usage)
        headers = {**self._headers_for(scenario.authorization), **scenario.extra_headers, "Content-Type": "application/json"}
        name = f"{CHAT_COMPLETIONS_PATH} [{scenario.name}] [{'stream' if streamed else 'non-stream'}]"
        payload_bytes = chat_mix_settings.oversized_body() if scenario.oversized_body else json.dumps(request_body).encode()
        read_timeout = STREAM_READ_TIMEOUT_SECONDS if streamed else NONSTREAM_READ_TIMEOUT_SECONDS
        chat_mix_settings.coverage.group_attempts[scenario.group.value] += 1
        sent_at = time.time()
        verdict: FrontVerdict | None = None
        with self.client.post(
            CHAT_COMPLETIONS_PATH,
            data=payload_bytes,
            headers=headers,
            catch_response=True,
            name=name,
            stream=streamed,
            timeout=(CONNECT_TIMEOUT_SECONDS, read_timeout),
        ) as resp:
            if resp.status_code == 0:
                self._record_transport_error(resp, scenario, name)
                return
            if abandon_after is not None and resp.status_code == 200 and isinstance(scenario.expectation, CompletionExpectation):
                transcript, received_bytes = self._read_until_abandoned(resp, abandon_after)
                body = None
                if transcript is None:
                    return
                if received_bytes is not None:
                    self._record_abandoned(resp, scenario, name, scenario.expectation, transcript, received_bytes=received_bytes)
                    return
            else:
                transcript, body = self._read_response(resp, streamed=streamed)
                if transcript is None and streamed and resp.status_code == 200:
                    return
            verdict = judge_front_response(
                scenario.expectation,
                scenario.alternative_error,
                nonce=find_request_nonce(scenario.body.get("messages") or []),
                kudos_upfront_benign=scenario.kudos_upfront_benign,
                status_code=resp.status_code,
                headers=resp.headers,
                body=body,
                transcript=transcript,
                streamed=streamed,
                include_usage=include_usage,
                cors_expected="Origin" in scenario.extra_headers,
                fail_fast=scenario.fail_fast,
            )
            self._record_verdict(resp, scenario, name, verdict, sent_at=sent_at, transcript=transcript, body=body)
        if verdict.benign_refusal in (BenignRefusal.RATE_LIMITED, BenignRefusal.FRONT_AT_CAPACITY):
            back_off(resp)
            raise RescheduleTask()
        if (
            not verdict.violations
            and verdict.matched_error is not None
            and verdict.matched_error.rc is not None
            and verdict.error_envelope is not None
        ):
            self._cross_check_fault(scenario, verdict.matched_error, verdict.error_envelope)

    def _record_verdict(
        self,
        resp: Any,
        scenario: ChatScenario,
        name: str,
        verdict: FrontVerdict,
        *,
        sent_at: float,
        transcript: StreamTranscript | None,
        body: Any,
    ) -> None:
        """Mark the response by its verdict: a violation, an environment condition, a benign refusal, or the outcome."""
        if verdict.violations:
            fail_contract(resp, scenario.name, verdict.violations)
            return
        if verdict.environment_condition is not None:
            fail_environment(resp, verdict.environment_condition, scenario.name)
            return
        if verdict.benign_refusal is not None:
            record_benign(self.environment, resp, name, _benign_label(verdict.benign_refusal, verdict.error_envelope))
            return
        resp.success()
        if scenario.group != ScenarioGroup.MISUSE:
            chat_mix_settings.coverage.completed_front_scenarios += 1
        self._record_timings(scenario, sent_at=sent_at, transcript=transcript, body=body)

    def _record_transport_error(self, resp: Any, scenario: ChatScenario, name: str) -> None:
        """Record a request that got no HTTP response.

        An oversized upload is the exception: the front answers 413 from the Content-Length and closes the connection
        without reading the body, so the client can be reset while it is still sending and never read the 413. That is
        the expected end of the scenario, so it is a benign outcome of its own; any other transport error is a crash.
        """
        if scenario.oversized_body and isinstance(resp.error, requests.ConnectionError):
            record_benign(self.environment, resp, name, UPLOAD_RESET_BEFORE_413)
            return
        resp.failure(f"Transport error: {resp.error}")

    def _record_abandoned(
        self,
        resp: Any,
        scenario: ChatScenario,
        name: str,
        expectation: CompletionExpectation,
        transcript: StreamTranscript,
        *,
        received_bytes: int,
    ) -> None:
        """Record a stream this client closed mid-relay under ``<name> [abandoned]``, with the bytes received as the
        row's response length.

        Only what the client read is checked here. What the Horde settled the cancelled generation on is read from its
        log and database, so the request id and the content characters received are logged for that comparison.
        """
        resp.request_meta["name"] = f"{name} [{ABANDONED_LABEL}]"
        resp.request_meta["response_length"] = received_bytes
        violations = find_abandoned_stream_violations(transcript, expectation)
        if violations:
            fail_contract(resp, scenario.name, violations)
            return
        resp.success()
        chat_mix_settings.coverage.completed_front_scenarios += 1
        completion_id = str(transcript.chunks[0].get("id") or "")
        logger.info(
            "Abandoned the stream of chat request %s after %d content chunks: %d content characters, %d bytes received",
            completion_id.removeprefix(COMPLETION_ID_PREFIX),
            transcript.reply_chunks,
            transcript.content_characters,
            received_bytes,
        )

    def _abandon_after(self, scenario: ChatScenario, *, streamed: bool) -> int | None:
        """Return after how many content chunks this request's client closes the stream, at the run's abandon share, or
        None when it reads the stream to its end. Only a streamed request that expects a reply has content to abandon."""
        if not streamed or chat_mix_settings.stream_abandon_share <= 0:
            return None
        if not isinstance(scenario.expectation, CompletionExpectation):
            return None
        if self.rng.random() >= chat_mix_settings.stream_abandon_share:
            return None
        return chat_mix_settings.stream_abandon_chunks

    def _decides_to_stream(self, scenario: ChatScenario) -> bool:
        """Return whether this request of the scenario is streamed."""
        if scenario.stream_mode == StreamMode.LITE_STREAM:
            return True
        if scenario.stream_mode == StreamMode.NEVER:
            return False
        return self.rng.random() < chat_mix_settings.stream_chance

    def _headers_for(self, authorization: AuthorizationMode) -> dict[str, str]:
        """Return the authorization and attribution headers of a request."""
        headers = {"X-Title": STRESS_CLIENT_TITLE, "User-Agent": _config.get("client_agent", "aihorde_stress_test")}
        return {**headers, **build_authorization_headers(authorization, api_key=self.api_key)}

    def _read_response(self, resp: Any, *, streamed: bool) -> tuple[StreamTranscript | None, Any]:
        """Return the stream transcript of a 200 stream, or the parsed body of any other response.

        A stream that fails mid-read fails the response as a transport error and returns ``(None, None)``.
        """
        if not (streamed and resp.status_code == 200):
            return None, _safe_json(resp)
        transcript = StreamTranscript()
        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                transcript.feed_line(raw_line or "", time.time())
        except requests.RequestException as error:
            resp.failure(f"Stream read failed: {error}")
            return None, None
        finally:
            resp.close()
        return transcript, None

    def _read_until_abandoned(self, resp: Any, abandon_after: int) -> tuple[StreamTranscript | None, int | None]:
        """Read a 200 stream until ``abandon_after`` chunks carrying a reply arrived, then close the connection.

        The close is what the front reacts to: it sees the client disconnect and cancels the native request, which
        settles a generation in progress on the text its worker posted. A response closed before its body is consumed
        closes its socket rather than returning it to the pool. A client that only stopped reading would hold the
        connection open, and the front would relay the rest of the reply into it.

        Returns:
            The transcript and the body bytes received before the close; the whole transcript and None when the stream
            ended or carried an error first; ``(None, None)`` after a failed read, which fails the response.
        """
        transcript = StreamTranscript()
        try:
            for line, received_bytes in _iter_stream_lines(resp):
                transcript.feed_line(line, time.time())
                if transcript.done or transcript.error_envelope is not None:
                    continue
                if transcript.reply_chunks >= abandon_after:
                    return transcript, received_bytes
        except requests.RequestException as error:
            resp.failure(f"Stream read failed: {error}")
            return None, None
        finally:
            resp.close()
        return transcript, None

    def _fail_unexpected_status(self, resp: Any, subject: str) -> None:
        """Fail a response with an unexpected status: an environment condition when it reports one, else a contract
        violation."""
        body = _safe_json(resp)
        condition = classify_environment_condition(resp.status_code, error_code_of(body), has_envelope=isinstance(body, dict))
        if condition is not None:
            fail_environment(resp, condition, subject)
            return
        fail_contract(resp, subject, [f"answered {resp.status_code}: {resp.text[:EXCERPT_CHARACTERS]}"])

    def _record_timings(self, scenario: ChatScenario, *, sent_at: float, transcript: StreamTranscript | None, body: Any) -> None:
        """Record the queue wait the Horde reports and, for a stream, the time to the first keepalive and content."""
        x_horde = body.get("x_horde") if isinstance(body, dict) else None
        if transcript is not None:
            record_measurement(self.environment, "[time-to-first-keepalive] stream", _elapsed(sent_at, transcript.first_keepalive_at))
            record_measurement(self.environment, "[time-to-first-content] stream", _elapsed(sent_at, transcript.first_content_at))
            finish_chunks = [chunk for chunk in transcript.chunks if chunk.get("x_horde")]
            x_horde = finish_chunks[-1]["x_horde"] if finish_chunks else None
        if isinstance(x_horde, dict) and scenario.persona is not None:
            # Anonymous requests rank last by kudos, so their queue wait is recorded apart.
            requester = " anonymous" if scenario.authorization == AuthorizationMode.ANONYMOUS else ""
            record_measurement(
                self.environment, f"[queue_wait_seconds] {scenario.persona.value}{requester}", x_horde.get("queue_wait_seconds")
            )

    def _cross_check_fault(self, scenario: ChatScenario, matched_error: ErrorExpectation, envelope: dict[str, Any]) -> None:
        """Read the native status of a request the front reported faulted and check it reports the same fault."""
        if not chat_mix_settings.verify_faults:
            return
        request_id = (envelope["error"].get("metadata") or {}).get("request_id")
        if not request_id:
            return
        name = f"{NATIVE_CHAT_STATUS_PATH}/[id] [fault cross-check]"
        with self.client.get(
            f"{chat_mix_settings.horde_host}{NATIVE_CHAT_STATUS_PATH}/{request_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            if resp.status_code == 0:
                resp.failure(f"Transport error: {resp.error}")
                return
            if resp.status_code == 429:
                record_benign(self.environment, resp, name, BenignRefusal.RATE_LIMITED.value)
                return
            if resp.status_code != 200:
                self._fail_unexpected_status(resp, f"{scenario.name} fault cross-check")
                return
            status_body = _safe_json(resp) or {}
            if status_body.get("faulted") is not True or status_body.get("fault_rc") != matched_error.rc:
                reported = f"faulted={status_body.get('faulted')!r} fault_rc={status_body.get('fault_rc')!r}"
                fail_contract(
                    resp,
                    f"{scenario.name} fault cross-check",
                    [f"the native status reports {reported}, expected fault_rc {matched_error.rc!r}"],
                )
                return
            resp.success()


def build_request_body(scenario: ChatScenario, *, streamed: bool, include_usage: bool) -> dict[str, Any]:
    """Return the body sent for one request of a scenario: a copy of the scenario's body, deep enough that the stream
    keys never touch it, plus ``stream`` and ``stream_options`` when streamed.

    Args:
        scenario: The scenario.
        streamed: Whether the request is streamed.
        include_usage: Whether a streamed request asks for the usage chunk.
    """
    request_body = copy.deepcopy(scenario.body)
    if streamed:
        request_body["stream"] = True
    if include_usage:
        request_body["stream_options"] = {"include_usage": True}
    return request_body


def find_card_violations(persona: ChatPersonaRole, card: Any) -> list[str]:
    """Return how a persona's model card breaks what its declaration advertises.

    Args:
        persona: The persona whose worker serves the model.
        card: The model's entry in ``/v1/models``, or None when it is missing.
    """
    model = chat_mix_settings.model_of(persona)
    if not isinstance(card, dict):
        return [f"no model card for {model}, though its {persona.value} worker has been online for a minute"]
    violations: list[str] = []
    declared_max_length = build_persona(persona, vision=chat_mix_settings.vision).chat_max_length
    card_max_length = (card.get("top_provider") or {}).get("max_completion_tokens")
    if card_max_length != declared_max_length:
        violations.append(f"{model} card max_completion_tokens is {card_max_length!r}, declared {declared_max_length}")
    supported = set(card.get("supported_parameters") or [])
    if persona == ChatPersonaRole.LIMITED and supported != LIMITED_CARD_PARAMETERS:
        violations.append(f"{model} card supported_parameters is {sorted(supported)}, expected {sorted(LIMITED_CARD_PARAMETERS)}")
    if persona != ChatPersonaRole.LIMITED and not CONFORMANT_CARD_PARAMETERS <= supported:
        violations.append(f"{model} card lacks {sorted(CONFORMANT_CARD_PARAMETERS - supported)}")
    advertises_image = "image" in ((card.get("architecture") or {}).get("input_modalities") or [])
    declares_vision = persona == ChatPersonaRole.CONFORMANT and chat_mix_settings.vision
    if advertises_image != declares_vision:
        violations.append(f"{model} card input_modalities image={advertises_image}, declared vision={declares_vision}")
    return violations


def _benign_label(benign_refusal: BenignRefusal, error_envelope: dict[str, Any] | None) -> str:
    """Return the row label of a benign outcome. A ``queue_unavailable`` refusal carries the reason its message states,
    so a run counts the refusals for zero eligible threads apart from those for a wait over the budget."""
    if benign_refusal == BenignRefusal.QUEUE_UNAVAILABLE:
        reason = queue_unavailable_reason(error_envelope)
        if reason is not None:
            return f"{benign_refusal.value} {reason}"
    return benign_refusal.value


def _iter_stream_lines(resp: Any) -> Iterator[tuple[str, int]]:
    """Yield each line of a streamed body, without its line ending, with the body bytes received up to it.

    The lines split as ``requests.iter_lines`` splits them; the byte count is why the body is read here instead.
    """
    received_bytes = 0
    pending = b""
    for data in resp.iter_content(chunk_size=STREAM_READ_CHUNK_BYTES):
        received_bytes += len(data)
        lines = (pending + data).splitlines(keepends=True)
        pending = lines.pop() if lines and not lines[-1].endswith((b"\n", b"\r")) else b""
        for line in lines:
            yield line.rstrip(b"\r\n").decode("utf-8", errors="replace"), received_bytes
    if pending:
        yield pending.decode("utf-8", errors="replace"), received_bytes


def _elapsed(started_at: float, ended_at: float | None) -> float | None:
    """Return the seconds between two times, or None when the second never happened."""
    if ended_at is None:
        return None
    return ended_at - started_at
