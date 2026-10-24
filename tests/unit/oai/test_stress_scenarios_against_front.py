# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The stress suite's chat scenarios, response checks and front client against the front itself, run in process
against a mocked Horde.

Every refusal a scenario expects is the one the front sends, param and type included. Every request a scenario expects
queued is forwarded as sent, and the reply the suite's worker script gives for the forwarded body, delivered through
the front, meets the scenario's expectation. The front client's own judgement runs against a front served over HTTP, so
its stream goes through ``requests.iter_lines`` with the front's real keepalive comments.
"""

from __future__ import annotations

import copy
import json
import os
import random
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import fakeredis
import httpx
import pytest
import respx
import uvicorn
from opentelemetry.sdk.metrics import MeterProvider

os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from locust.env import Environment

from horde.oai.app import create_app
from horde.oai.metrics import METER_NAME
from horde.oai.settings import FrontSettings
from tests.stress.locustsuite.chat_checks import ErrorExpectation, FrontVerdict, StreamTranscript, judge_front_response
from tests.stress.locustsuite.chat_front_client import ChatMixFrontClient, build_request_body
from tests.stress.locustsuite.chat_mix_settings import RunnerRole, RunTopology, chat_mix_settings
from tests.stress.locustsuite.chat_scenarios import (
    SCENARIO_BUILDERS,
    ChatModelNames,
    ChatScenario,
    ScenarioContext,
    ScenarioGroup,
    build_authorization_headers,
    gap_forced_strict,
    nonconformant_schema_strict,
    oversized_body,
    plain_prompt,
    tools_auto,
)
from tests.stress.locustsuite.chat_worker_script import build_scripted_reply
from tests.unit.oai.conftest import (
    FAST_TEST_SETTINGS,
    HORDE_URL,
    MODEL_NAME,
    REQUEST_ID,
    FakeHorde,
    StartFront,
    done_status,
    waiting_status,
)
from tests.unit.test_stress_chat_mix import chat_mix_options

CONTEXT: ScenarioContext = ScenarioContext(
    models=ChatModelNames(conformant=MODEL_NAME, limited=MODEL_NAME, flaky=MODEL_NAME),
    worker_max_length=512,
    nonce="0123456789ab",
)
"""Every persona's model is the one the mocked Horde serves, so a completion names the requested model."""

API_KEY: str = "stress-test-key"
"""The requestor key the scenarios authenticate with."""

ANSWERED_BY_THE_HORDE: frozenset[str] = frozenset({"unknown_model", "unknown_api_key"})
"""Refusals the Horde, not the front, decides; the mocked Horde accepts everything, so they are left out here."""

ALL_SCENARIOS: list[ChatScenario] = [builder(CONTEXT) for builders in SCENARIO_BUILDERS.values() for builder in builders]
"""Every scenario of the catalogue."""

FRONT_REFUSED_SCENARIOS: list[ChatScenario] = [
    scenario
    for scenario in ALL_SCENARIOS
    if scenario.persona is None and not scenario.oversized_body and scenario.name not in ANSWERED_BY_THE_HORDE
]
"""The scenarios the front refuses before submitting."""

QUEUED_SCENARIOS: list[ChatScenario] = [scenario for scenario in ALL_SCENARIOS if scenario.persona is not None]
"""The scenarios the front submits to the Horde."""

NATIVE_REQUEST_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "seed",
        "reasoning_effort",
        "n",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "horde",
    },
)
"""The keys the native chat route accepts; nothing else may be forwarded."""

FORWARDED_AS_SENT: tuple[str, ...] = ("tools", "tool_choice", "parallel_tool_calls", "response_format", "seed", "stop", "temperature")
"""Keys the front forwards unchanged whenever the scenario sends them at a value that changes the answer."""

HORDE_JPEG_DATA_URL: str = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ=="
"""What the Horde hands a ``base64`` worker for an image: the stored JPEG as a data URL."""

FAST_GRACE_SETTINGS: FrontSettings = FAST_TEST_SETTINGS.model_copy(update={"fail_fast_grace_seconds": 0.2})
"""Test settings whose no-eligible-worker grace is short enough for a test to wait it out."""


def scenario_headers(scenario: ChatScenario) -> dict[str, str]:
    """Return the headers the front client sends with a scenario."""
    return {**build_authorization_headers(scenario.authorization, api_key=API_KEY), **scenario.extra_headers}


def streamed_body(scenario: ChatScenario, *, include_usage: bool) -> dict[str, Any]:
    """Return a scenario's body as a streamed request."""
    stream_options = {"stream_options": {"include_usage": True}} if include_usage else {}
    return {**scenario.body, "stream": True, **stream_options}


def read_stream(response: httpx.Response) -> StreamTranscript:
    """Return the transcript of a whole streamed response."""
    transcript = StreamTranscript()
    for line_index, line in enumerate(response.text.splitlines()):
        transcript.feed_line(line, float(line_index))
    return transcript


def judge(scenario: ChatScenario, response: httpx.Response, *, streamed: bool, include_usage: bool = False) -> FrontVerdict:
    """Return the suite's verdict on a front response to ``scenario``."""
    stream_started = streamed and response.status_code == 200
    return judge_front_response(
        scenario.expectation,
        scenario.alternative_error,
        status_code=response.status_code,
        headers=response.headers,
        body=None if stream_started else response.json(),
        transcript=read_stream(response) if stream_started else None,
        streamed=streamed,
        include_usage=include_usage,
        cors_expected="Origin" in scenario.extra_headers,
    )


def dispatch_like_the_horde(native_body: dict[str, Any]) -> dict[str, Any]:
    """Return the chat job payload the Horde builds from a forwarded body: tools narrowed for ``none`` and a function
    choice, and image parts replaced by the stored JPEG."""
    payload = {
        payload_key: native_body[payload_key]
        for payload_key in ("messages", "tools", "tool_choice", "parallel_tool_calls", "response_format")
        if payload_key in native_body
    }
    payload["messages"] = copy.deepcopy(payload["messages"])
    for message in payload["messages"]:
        for part in message.get("content") if isinstance(message.get("content"), list) else []:
            if part.get("type") == "image_url":
                part["image_url"] = {**part["image_url"], "url": HORDE_JPEG_DATA_URL}
    tool_choice = payload.get("tool_choice")
    if tool_choice == "none":
        for narrowed_key in ("tools", "tool_choice", "parallel_tool_calls"):
            payload.pop(narrowed_key, None)
    elif isinstance(tool_choice, dict):
        chosen_name = tool_choice["function"]["name"]
        payload["tools"] = [tool for tool in payload["tools"] if tool["function"]["name"] == chosen_name]
        payload["tool_choice"] = "required"
    return payload


def stored_generation(native_body: dict[str, Any]) -> dict[str, Any]:
    """Return the generation the Horde stores for the suite worker's reply to a forwarded body, as the native status
    shows it: a single call kept under ``strict`` ``parallel_tool_calls: false``, and ``stop`` for a forced function."""
    reply = build_scripted_reply(dispatch_like_the_horde(native_body))
    conformance = (native_body.get("horde") or {}).get("conformance", "strict")
    tool_calls = reply.tool_calls
    if tool_calls and native_body.get("parallel_tool_calls") is False and conformance == "strict":
        tool_calls = tool_calls[:1]
    finish_reason = reply.finish_reason
    if tool_calls and isinstance(native_body.get("tool_choice"), dict):
        finish_reason = "stop"
    message: dict[str, Any] = {"role": "assistant", "content": reply.content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    status = done_status(content=reply.content or "", finish_reason=finish_reason)
    status["generations"][0]["message"] = message
    return status


def refusal_status(expectation: ErrorExpectation) -> dict[str, Any]:
    """Return the native status that leads the front to the refusal a queued scenario expects."""
    if expectation.rc is not None:
        reason = " ".join(expectation.message_fragments) or "The reply broke the request."
        return {**waiting_status(), "faulted": True, "fault_reason": reason, "fault_rc": expectation.rc}
    if "no_worker_supports_features" in expectation.codes:
        (named,) = expectation.message_fragments
        exceeded = [named] if named == "tools_max_temperature" else []
        missing = [] if exceeded else [named]
        gap = {"kind": "declaration_gap", "missing_declarations": missing, "exceeded_limits": exceeded}
        return {**waiting_status(is_possible=False), "impossible_reason": gap}
    if "no_worker_supports_limit" in expectation.codes:
        gap = {"kind": "declaration_gap", "missing_declarations": [], "exceeded_limits": ["max_length"]}
        return {**waiting_status(is_possible=False), "impossible_reason": gap}
    return waiting_status(is_possible=False)


@pytest.mark.parametrize("scenario", FRONT_REFUSED_SCENARIOS, ids=lambda scenario: scenario.name)
@pytest.mark.asyncio
async def test_a_refusal_scenario_expects_the_refusal_the_front_sends(start_front: StartFront, scenario: ChatScenario) -> None:
    assert isinstance(scenario.expectation, ErrorExpectation)
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=scenario.body, headers=scenario_headers(scenario))

    assert judge(scenario, response, streamed=False).violations == []
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize("scenario", QUEUED_SCENARIOS, ids=lambda scenario: scenario.name)
@pytest.mark.asyncio
async def test_a_queued_scenario_is_forwarded_as_sent_and_its_reply_meets_the_expectation(
    start_front: StartFront,
    scenario: ChatScenario,
) -> None:
    async with start_front(FAST_GRACE_SETTINGS) as front:
        if isinstance(scenario.expectation, ErrorExpectation):
            front.horde.status_payload = refusal_status(scenario.expectation)
            response = await front.client.post("/v1/chat/completions", json=scenario.body, headers=scenario_headers(scenario))
        else:
            front.horde.status_payload = waiting_status()
            front.horde.submit_route.side_effect = lambda request: _answer_submit_with_reply(front.horde, request)
            response = await front.client.post("/v1/chat/completions", json=scenario.body, headers=scenario_headers(scenario))
        forwarded = front.horde.submitted_body()

    assert set(forwarded) <= NATIVE_REQUEST_KEYS, sorted(set(forwarded) - NATIVE_REQUEST_KEYS)
    assert forwarded["model"] == scenario.body["model"]
    assert len(forwarded["messages"]) == len(scenario.body["messages"])
    assert forwarded["horde"]["operation_key"]
    assert CONTEXT.nonce in json.dumps(forwarded["messages"])
    for request_key in FORWARDED_AS_SENT:
        if scenario.body.get(request_key) not in (None, [], {"type": "text"}):
            assert forwarded[request_key] == scenario.body[request_key], request_key
    assert judge(scenario, response, streamed=False).violations == []


CONFORMANCE_SCENARIOS: list[ChatScenario] = [scenario for scenario in QUEUED_SCENARIOS if scenario.group == ScenarioGroup.CONFORMANCE]
"""The scenarios that send a request under one conformance mode, some of them twins under the other."""


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("scenario", CONFORMANCE_SCENARIOS, ids=lambda scenario: scenario.name)
@pytest.mark.asyncio
async def test_the_front_forwards_the_scenarios_conformance_streamed_or_not(
    start_front: StartFront,
    scenario: ChatScenario,
    streamed: bool,
) -> None:
    body = build_request_body(scenario, streamed=streamed, include_usage=streamed)
    async with start_front(FAST_GRACE_SETTINGS) as front:
        front.horde.status_payload = (
            refusal_status(scenario.expectation) if isinstance(scenario.expectation, ErrorExpectation) else done_status()
        )
        await front.client.post("/v1/chat/completions", json=body, headers=scenario_headers(scenario))
        forwarded = front.horde.submitted_body()

    expected_conformance = (scenario.body.get("horde") or {}).get("conformance", "strict")
    assert forwarded["horde"].get("conformance", "strict") == expected_conformance


def _answer_submit_with_reply(horde: FakeHorde, request: httpx.Request) -> httpx.Response:
    """Answer a submit and make the status report the suite worker's reply to the submitted body."""
    horde.status_payload = stored_generation(json.loads(request.content))
    return httpx.Response(202, json={"id": REQUEST_ID, "kudos": 12.5})


@pytest.mark.parametrize(("streamed", "include_usage"), [(False, False), (True, False), (True, True)])
@pytest.mark.asyncio
async def test_the_checks_accept_the_fronts_text_completion(start_front: StartFront, streamed: bool, include_usage: bool) -> None:
    scenario = plain_prompt(CONTEXT)
    async with start_front() as front:
        front.horde.status_payload = done_status(content=f"Hello {CONTEXT.nonce}", finish_reason="stop")
        body = streamed_body(scenario, include_usage=include_usage) if streamed else scenario.body
        response = await front.client.post("/v1/chat/completions", json=body, headers=scenario_headers(scenario))

    assert judge(scenario, response, streamed=streamed, include_usage=include_usage).violations == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_the_checks_accept_the_fronts_tool_call_completion(start_front: StartFront, streamed: bool) -> None:
    scenario = tools_auto(CONTEXT)
    weather_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": json.dumps({"city": CONTEXT.nonce})},
    }
    status = done_status(content="", finish_reason="tool_calls")
    status["generations"][0]["message"] = {"role": "assistant", "content": None, "tool_calls": [weather_call]}
    async with start_front() as front:
        front.horde.status_payload = status
        body = streamed_body(scenario, include_usage=True) if streamed else scenario.body
        response = await front.client.post("/v1/chat/completions", json=body, headers=scenario_headers(scenario))

    assert judge(scenario, response, streamed=streamed, include_usage=streamed).violations == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_the_checks_match_the_fronts_fault_for_a_refused_reply(start_front: StartFront, streamed: bool) -> None:
    scenario = nonconformant_schema_strict(CONTEXT)
    async with start_front() as front:
        front.horde.status_payload = refusal_status(scenario.expectation)
        body = streamed_body(scenario, include_usage=False) if streamed else scenario.body
        response = await front.client.post("/v1/chat/completions", json=body, headers=scenario_headers(scenario))

    verdict = judge(scenario, response, streamed=streamed)
    assert verdict.violations == []
    assert verdict.matched_error is scenario.expectation
    assert verdict.error_envelope is not None
    assert verdict.error_envelope["error"]["metadata"]["request_id"] == REQUEST_ID


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.asyncio
async def test_the_checks_match_the_fronts_declaration_gap(start_front: StartFront, streamed: bool) -> None:
    scenario = gap_forced_strict(CONTEXT)
    async with start_front() as front:
        front.horde.status_payload = refusal_status(scenario.expectation)
        body = streamed_body(scenario, include_usage=False) if streamed else scenario.body
        response = await front.client.post("/v1/chat/completions", json=body, headers=scenario_headers(scenario))

    assert judge(scenario, response, streamed=streamed).violations == []


@pytest.mark.asyncio
async def test_the_checks_refuse_a_completion_where_a_refusal_was_promised(start_front: StartFront) -> None:
    scenario = nonconformant_schema_strict(CONTEXT)
    async with start_front() as front:
        front.horde.status_payload = done_status(finish_reason="stop")
        response = await front.client.post("/v1/chat/completions", json=scenario.body, headers=scenario_headers(scenario))

    violations = judge(scenario, response, streamed=False).violations
    assert violations and "got a completion" in violations[0]


SERVED_FRONT_SETTINGS: FrontSettings = FAST_TEST_SETTINGS.model_copy(update={"fallback_poll_seconds": 0.1, "max_body_bytes": 1024})
"""Settings for a front served over HTTP without Redis: a fast fallback poll replaces the completion signal, and a small
body limit lets the oversized scenario run with a small body."""


@dataclass
class ServedFront:
    """Represents a front served over HTTP on a local port, with its mocked Horde."""

    url: str
    """The front's base URL."""
    horde: FakeHorde
    """The mocked Horde."""


def _free_port() -> int:
    """Return a local port no one listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def serve_front() -> Iterator[ServedFront]:
    """Run a front with uvicorn on a thread against a mocked Horde, for a client that speaks real HTTP."""
    with respx.mock(assert_all_called=False) as router:
        horde = FakeHorde(router, fakeredis.FakeAsyncRedis())
        app = create_app(
            SERVED_FRONT_SETTINGS,
            http_client=httpx.AsyncClient(),
            cancel_http_client=httpx.AsyncClient(),
            redis_client=None,
            meter=MeterProvider().get_meter(METER_NAME),
        )
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "The served front did not start."
            time.sleep(0.02)
        try:
            yield ServedFront(url=f"http://127.0.0.1:{port}", horde=horde)
        finally:
            server.should_exit = True
            server_thread.join(timeout=10)


@pytest.fixture
def served_front() -> Iterator[ServedFront]:
    """Serve a front for one test and configure the chat mix to talk to it."""
    with serve_front() as front:
        chat_mix_settings.configure(
            chat_mix_options(front_host=front.url, front_clients=1), horde_host=HORDE_URL, topology=RunTopology(role=RunnerRole.LOCAL)
        )
        chat_mix_settings.verify_faults = False
        chat_mix_settings.oversized_body_bytes = 4096
        yield front


@dataclass
class RecordedClient:
    """Represents a front client and the request events it fired."""

    client: ChatMixFrontClient
    """The Locust user."""
    events: list[dict[str, Any]]
    """Every request event, in order."""

    def failures(self) -> list[str]:
        """Return the messages of the events that failed."""
        return [str(event["exception"]) for event in self.events if event["exception"] is not None]

    def names(self) -> list[str]:
        """Return the names of every event."""
        return [event["name"] for event in self.events]


def build_front_client(front: ServedFront) -> RecordedClient:
    """Create a front client for the served front, recording its events."""
    # Locust reads the host from the user class, which a run sets from --host.
    hosted_client_class = type("HostedChatMixFrontClient", (ChatMixFrontClient,), {"host": front.url})
    environment = Environment(user_classes=[hosted_client_class])
    events: list[dict[str, Any]] = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    front_client = hosted_client_class(environment)
    front_client.api_key = API_KEY
    front_client.rng = random.Random(7)
    return RecordedClient(client=front_client, events=events)


def served_scenario(builder: Any) -> ChatScenario:
    """Build a scenario for the mocked Horde's model."""
    return builder(CONTEXT)


def test_a_streamed_reply_read_through_iter_lines_with_keepalives_passes(served_front: ServedFront) -> None:
    chat_mix_settings.stream_chance = 1.0
    chat_mix_settings.include_usage_chance = 1.0
    recorded = build_front_client(served_front)
    finished = threading.Timer(
        0.4, lambda: setattr(served_front.horde, "status_payload", done_status(content=f"Hello {CONTEXT.nonce}", finish_reason="stop"))
    )
    finished.start()

    recorded.client.send_scenario(served_scenario(plain_prompt))
    finished.join()

    assert recorded.failures() == []
    assert "/v1/chat/completions [plain_prompt] [stream]" in recorded.names()
    assert "[time-to-first-keepalive] stream" in recorded.names()
    assert "[time-to-first-content] stream" in recorded.names()
    assert chat_mix_settings.coverage.completed_front_scenarios == 1


def test_a_reply_for_another_request_is_a_contract_violation(served_front: ServedFront) -> None:
    chat_mix_settings.stream_chance = 0.0
    served_front.horde.status_payload = done_status(content="Hello from another request", finish_reason="stop")
    recorded = build_front_client(served_front)

    recorded.client.send_scenario(served_scenario(plain_prompt))

    (failure,) = recorded.failures()
    assert "Contract violation [plain_prompt]" in failure
    assert "nonce" in failure


def test_an_expected_fault_is_a_success_of_the_scenario(served_front: ServedFront) -> None:
    chat_mix_settings.stream_chance = 0.0
    scenario = served_scenario(nonconformant_schema_strict)
    served_front.horde.status_payload = refusal_status(scenario.expectation)
    recorded = build_front_client(served_front)

    recorded.client.send_scenario(scenario)

    assert recorded.failures() == []
    assert recorded.names() == ["/v1/chat/completions [nonconformant_schema_strict] [non-stream]"]


def test_an_unreachable_horde_is_an_environment_condition(served_front: ServedFront) -> None:
    chat_mix_settings.stream_chance = 0.0
    served_front.horde.submit_route.side_effect = httpx.ConnectError("refused")
    recorded = build_front_client(served_front)

    recorded.client.send_scenario(served_scenario(plain_prompt))

    (failure,) = recorded.failures()
    assert "Environment condition [horde_unavailable]" in failure


def test_an_oversized_body_ends_in_a_413_or_a_benign_upload_reset(served_front: ServedFront) -> None:
    recorded = build_front_client(served_front)

    recorded.client.send_scenario(served_scenario(oversized_body))

    assert recorded.failures() == []
    assert served_front.horde.submit_route.call_count == 0
