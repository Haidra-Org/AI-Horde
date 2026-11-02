# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OpenAI-compatible front's wire examples in ``docs/reference/chat_wire_examples.md`` match the front.

The examples whose id starts with ``FRONT_EXAMPLE_PREFIX`` are checked here against the front served by the harness
of ``conftest.py`` and ``_front_stack.py``: uvicorn in front of the in-process Horde, with their fake worker serving
the chat model. Each response example is compared with the live body as
[`structure_differences`][tests.integration._wire_examples.structure_differences] describes; the bodies are read as
raw HTTP, so each is the JSON the front writes and not an SDK object. The request example is posted and has to get the
status of the response example that answers it; the front refuses unknown keys, so a misspelt key fails the post.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from tests.integration._fake_chat_worker import CHAT_MODEL, take_worker_offline
from tests.integration._front_stack import (
    FRONT_CHAT_MESSAGES,
    SDK_TIMEOUT_SECONDS,
    TOOL_MESSAGES,
    WEATHER_TOOL,
    WORKER_NAME,
    FixedReplyWorker,
)
from tests.integration._wire_examples import (
    DOCUMENTED,
    FRONT_EXAMPLE_PREFIX,
    MODEL_NAME_PLACEHOLDER,
    filled,
    structure_differences,
)

if TYPE_CHECKING:
    from flask import Flask

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address submit limit, which this module's request volume would otherwise trip."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(autouse=True)
def _worker_kept_up(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test during which the fake worker saw an unexpected answer or stopped."""
    yield
    if "fixed_reply_worker" in request.fixturenames:
        worker: FixedReplyWorker = request.getfixturevalue("fixed_reply_worker")
        assert worker.failures == []


COMPLETIONS_PATH = "chat/completions"
"""The chat completions route, relative to the front's ``/v1`` base URL."""

SSE_DATA_PREFIX = "data: "
"""The prefix of each server-sent event line that carries a chunk."""

SSE_DONE = "[DONE]"
"""The data of the event that ends a stream."""

FRONT_CHAT_BODY: dict[str, Any] = {
    "model": CHAT_MODEL,
    "messages": FRONT_CHAT_MESSAGES,
    "max_tokens": 64,
    "temperature": 0.5,
}
"""The body the response and stream examples send, which the documented front request holds as well."""

FEATURE_GAP_REASONING_EFFORT = "low"
"""A ``reasoning_effort``, which needs a feature the fake worker does not declare, so the request has a declaration
gap."""

KUDOS_CAP = 1
"""A ``horde.max_kudos`` below the quote of any chat request, so the native route refuses the request."""


@dataclass(frozen=True)
class FrontCapture:
    """One body the front sent, with the HTTP status of the response that carried it."""

    status: int
    """The HTTP status; for a streamed chunk, the status of the stream."""

    body: Any
    """The parsed JSON body or chunk."""


@dataclass(frozen=True)
class FrontRun:
    """Holds one test's HTTP client for the front."""

    http: httpx.Client
    """A client addressed at the front's ``/v1`` base URL, carrying the owner's key as its bearer token."""

    def complete(self, chat_body: dict[str, Any]) -> FrontCapture:
        """Send a non-streamed completion request and return its answer.

        Args:
            chat_body: The request body.

        Returns:
            The response's status and JSON body.
        """
        response = self.http.post(COMPLETIONS_PATH, json=chat_body)
        return FrontCapture(status=response.status_code, body=response.json())

    def stream_chunks(self) -> tuple[int, list[dict[str, Any]]]:
        """Send the documented request streamed with usage and return the stream's status and its chunks in order.

        Returns:
            The HTTP status and every ``data`` event before ``[DONE]``, parsed.
        """
        streamed_body = {**FRONT_CHAT_BODY, "stream": True, "stream_options": {"include_usage": True}}
        with self.http.stream("POST", COMPLETIONS_PATH, json=streamed_body) as response:
            event_data = [line.removeprefix(SSE_DATA_PREFIX) for line in response.iter_lines() if line.startswith(SSE_DATA_PREFIX)]
        assert event_data[-1] == SSE_DONE, event_data
        return response.status_code, [json.loads(data) for data in event_data[:-1]]


@pytest.fixture(scope="module")
def _front_worker_offline(app: Flask, request: pytest.FixtureRequest) -> Iterator[None]:
    """Start the fake worker, then stop it and take it offline when the module ends.

    The harness leaves the worker online, which would make later tests' requests for its model possible. The thread
    stops first, since its next pop would bring the worker back online.
    """
    worker: FixedReplyWorker = request.getfixturevalue("fixed_reply_worker")
    yield
    worker.stop()
    take_worker_offline(app, WORKER_NAME)


@pytest.fixture
def front(request: pytest.FixtureRequest, _front_worker_offline: None) -> Iterator[FrontRun]:
    """A scenario context with an HTTP client for the front.

    The harness fixtures are read by name, since parameters named after the imported fixtures would shadow them.
    """
    with httpx.Client(
        base_url=request.getfixturevalue("front_base_url"),
        headers={"Authorization": f"Bearer {request.getfixturevalue('owner_api_key')}"},
        timeout=SDK_TIMEOUT_SECONDS,
    ) as http:
        yield FrontRun(http=http)


def _post_front_request(front: FrontRun, documented: dict[str, Any]) -> httpx.Response:
    return front.http.post(COMPLETIONS_PATH, json=filled(documented, {MODEL_NAME_PLACEHOLDER: CHAT_MODEL}))


def _live_front_response(front: FrontRun) -> FrontCapture:
    return front.complete(FRONT_CHAT_BODY)


def _live_stream_chunk(front: FrontRun, select: Callable[[dict[str, Any]], bool]) -> FrontCapture:
    """Return the one chunk of the documented stream that ``select`` picks."""
    status, chunks = front.stream_chunks()
    [chunk] = [chunk for chunk in chunks if select(chunk)]
    return FrontCapture(status=status, body=chunk)


def _is_content_chunk(chunk: dict[str, Any]) -> bool:
    return bool(chunk["choices"]) and "content" in chunk["choices"][0]["delta"]


def _is_finish_chunk(chunk: dict[str, Any]) -> bool:
    return bool(chunk["choices"]) and chunk["choices"][0]["finish_reason"] is not None


def _is_usage_chunk(chunk: dict[str, Any]) -> bool:
    return not chunk["choices"]


def _live_front_tool_call_response(front: FrontRun) -> FrontCapture:
    return front.complete({"model": CHAT_MODEL, "messages": TOOL_MESSAGES, "tools": [WEATHER_TOOL]})


def _live_front_no_worker_supports_features(front: FrontRun) -> FrontCapture:
    return front.complete({**FRONT_CHAT_BODY, "reasoning_effort": FEATURE_GAP_REASONING_EFFORT})


def _live_front_kudos_cap(front: FrontRun) -> FrontCapture:
    return front.complete({**FRONT_CHAT_BODY, "horde": {"max_kudos": KUDOS_CAP}})


def _live_front_model_list(front: FrontRun) -> FrontCapture:
    response = front.http.get("models")
    return FrontCapture(status=response.status_code, body=response.json())


@dataclass(frozen=True)
class FrontScenario:
    """Produces one front response example's live body, or posts the front request example."""

    live: Callable[[FrontRun], FrontCapture] | None = None
    """Returns the live ``FrontCapture``; None for a request example."""

    status: int | None = None
    """The HTTP status of a response example, which its prose states; None for a request example."""

    post: Callable[[FrontRun, dict[str, Any]], httpx.Response] | None = None
    """Posts the documented request body with its placeholders filled in; None for a response example."""

    answered_by: str | None = None
    """The response example whose status the posted documented body gets."""


FRONT_SCENARIOS: dict[str, FrontScenario] = {
    "front-request": FrontScenario(
        post=_post_front_request,
        answered_by="front-response",
    ),
    "front-response": FrontScenario(
        live=_live_front_response,
        status=200,
    ),
    "front-stream-content-chunk": FrontScenario(
        live=lambda front: _live_stream_chunk(front, _is_content_chunk),
        status=200,
    ),
    "front-stream-finish-chunk": FrontScenario(
        live=lambda front: _live_stream_chunk(front, _is_finish_chunk),
        status=200,
    ),
    "front-stream-usage-chunk": FrontScenario(
        live=lambda front: _live_stream_chunk(front, _is_usage_chunk),
        status=200,
    ),
    "front-tool-call-response": FrontScenario(
        live=_live_front_tool_call_response,
        status=200,
    ),
    "front-no-worker-supports-features": FrontScenario(
        live=_live_front_no_worker_supports_features,
        status=404,
    ),
    "front-kudos-cap": FrontScenario(
        live=_live_front_kudos_cap,
        status=400,
    ),
    "front-model-list": FrontScenario(
        live=_live_front_model_list,
        status=200,
    ),
}
"""Every front example on the page by its marker id."""


def test_every_front_marker_has_a_scenario_and_every_scenario_a_marker() -> None:
    """The other examples are matched to their scenarios in ``test_chat_wire_examples.py``."""
    assert sorted(example_id for example_id in DOCUMENTED if example_id.startswith(FRONT_EXAMPLE_PREFIX)) == sorted(FRONT_SCENARIOS)


@pytest.mark.parametrize("example_id", sorted(example_id for example_id, scenario in FRONT_SCENARIOS.items() if scenario.live is not None))
def test_each_front_response_example_matches_the_live_response(front: FrontRun, example_id: str) -> None:
    """A front response example has the live body's keys, contract values and value types."""
    scenario = FRONT_SCENARIOS[example_id]
    assert scenario.live is not None

    live = scenario.live(front)

    assert live.status == scenario.status, json.dumps(live.body, indent=2)
    assert structure_differences(DOCUMENTED[example_id].body, live.body) == [], json.dumps(live.body, indent=2)


@pytest.mark.parametrize(
    "example_id",
    sorted(example_id for example_id, scenario in FRONT_SCENARIOS.items() if scenario.post is not None),
)
def test_each_documented_front_request_body_gets_the_documented_status(front: FrontRun, example_id: str) -> None:
    """The documented front request gets the status of the response example that answers it."""
    scenario = FRONT_SCENARIOS[example_id]
    assert scenario.post is not None
    assert scenario.answered_by is not None

    response = scenario.post(front, DOCUMENTED[example_id].body)

    assert response.status_code == FRONT_SCENARIOS[scenario.answered_by].status, response.text


@pytest.mark.parametrize(
    "example_id",
    sorted(example_id for example_id, scenario in FRONT_SCENARIOS.items() if scenario.status is not None),
)
def test_each_front_response_example_states_its_status(example_id: str) -> None:
    """The prose before a front response example states the status the live response has."""
    assert f"`{FRONT_SCENARIOS[example_id].status}`" in DOCUMENTED[example_id].prose
