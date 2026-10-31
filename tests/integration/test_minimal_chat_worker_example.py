# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The minimal chat worker example under ``tools/chat_worker_example`` served against the in-process horde."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import requests
from horde_sdk.generation_parameters.text.chat_policy import ChatFeature

from tests.integration._fake_chat_worker import (
    LOOKUP_TOOL,
    build_tool_call,
    cancel_request,
    chat_status,
    submit_chat_request,
    take_worker_offline,
)
from tests.unit.test_minimal_chat_worker_example import (
    HORDE_URL,
    PROBE_PROMPT_TOKENS,
    BackendSession,
    BackendStub,
    build_completion,
    build_config,
    fixed_backend,
    is_probe,
    worker_example,
)

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

pytestmark = pytest.mark.integration

REQUESTED_MAX_TOKENS = 64
"""The ``max_tokens`` each chat request asks for."""

SUBMIT_PATH = "/api/v2/generate/text/submit"
"""The horde route the worker submits on."""

POP_PATH = "/api/v2/generate/text/pop"
"""The horde route the worker pops on."""

QueueChatRequest = Callable[..., str]
"""Queues a chat request with one user message and returns its id."""


class RoutingSession(BackendSession):
    """Sends horde URLs to the Flask test client and backend URLs to a stub, recording every exchange."""

    def __init__(self, client: Any, backend_factory: Callable[[BackendSession], BackendStub]) -> None:
        """Build the session.

        Args:
            client: The Flask test client that serves horde URLs.
            backend_factory: Returns the stub backend for the session.
        """
        self.client = client
        super().__init__(backend_factory)

    def post_elsewhere(self, url: str, body: Any, headers: dict[str, str] | None) -> tuple[int, Any]:
        """Return the Flask test client's status and JSON body for a horde post."""
        flask_response = self.client.post(url.removeprefix(HORDE_URL), json=body, headers=headers)
        return flask_response.status_code, flask_response.get_json()

    def submits(self) -> list[Any]:
        """Return the submit exchanges in order."""
        return [exchange for exchange in self.exchanges if exchange.url.endswith(SUBMIT_PATH)]

    def popped_job(self) -> dict[str, Any]:
        """Return the response body of the last pop."""
        [*_, pop] = [exchange for exchange in self.exchanges if exchange.url.endswith(POP_PATH)]
        popped: dict[str, Any] = pop.response_body
        return popped

    def popped_payload(self) -> dict[str, Any]:
        """Return the payload of the last pop."""
        payload: dict[str, Any] = self.popped_job()["payload"]
        return payload


def _replying(completion: dict[str, Any]) -> Callable[[BackendSession], BackendStub]:
    """Return a backend factory whose backend responds to every path with ``completion``."""
    return fixed_backend(lambda path, body: (200, completion))


@pytest.fixture
def worker_config(app: Flask, request_headers: dict[str, str]) -> Iterator[Any]:
    """A configuration under a fresh worker name with the test user's key, taken offline afterwards."""
    config = build_config(**{"--api-key": request_headers["apikey"]})
    yield config
    take_worker_offline(app, config.worker_name)


@pytest.fixture
def queue_chat_request(client: FlaskClient, request_headers: dict[str, str]) -> Iterator[QueueChatRequest]:
    """A function that queues a chat request, each one cancelled afterwards, so a failed test does not leave it queued."""
    request_ids: list[str] = []

    def queue(content: str, **overrides: Any) -> str:
        messages = [{"role": "user", "content": content}]
        request_id = submit_chat_request(client, request_headers, messages=messages, max_tokens=REQUESTED_MAX_TOKENS, **overrides)
        request_ids.append(request_id)
        return request_id

    yield queue
    for request_id in request_ids:
        cancel_request(client, request_headers, "chat", request_id)


def _check_in(session: RoutingSession, worker_config: Any) -> Callable[[], None]:
    """Start the worker and pop once on an empty queue, then return a function that pops and serves one job.

    The horde refuses a chat request for a model without an online chat worker, so the worker checks in before the
    test queues its request.
    """
    template_overhead = worker_example.check_fit_counting(session, worker_config)
    declaration = worker_example.build_declaration(worker_config, template_overhead)

    def serve_one_pop() -> bool:
        served: bool = worker_example.serve_one_pop(
            session,
            config=worker_config,
            declaration=declaration,
            template_overhead=template_overhead or 0,
        )
        return served

    assert serve_one_pop() is False

    def serve_one_job() -> None:
        assert serve_one_pop() is True

    return serve_one_job


def _with_llamacpp_counter(config: Any) -> Any:
    return replace(config, backend=replace(config.backend, token_counter=worker_example.TokenCounter.LLAMACPP))


def test_a_chat_request_completes_with_the_backend_reply(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """The worker declares chat only with the overflow guarantee, and submits the backend's reply and counts."""
    serve_one_job = _check_in(RoutingSession(client, _replying(build_completion("Blue."))), worker_config)
    request_id = queue_chat_request("Name one colour.")

    serve_one_job()

    [generation] = chat_status(client, request_headers, request_id)["generations"]
    assert generation["message"] == {"role": "assistant", "content": "Blue."}
    assert generation["finish_reason"] == "stop"
    assert generation["prompt_tokens"] == PROBE_PROMPT_TOKENS
    assert generation["completion_tokens"] == 3
    details = client.get(f"/api/v2/workers/name/{worker_config.worker_name}", headers=request_headers).get_json()
    assert details["accepts_chat_jobs"] is True
    assert details["accepts_prompt_jobs"] is False
    assert details["chat"]["features"] == ["prompt_overflow_refused"]


def test_the_backend_reasoning_token_count_is_submitted_with_the_reply(
    client: FlaskClient,
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """The backend's ``usage.completion_tokens_details.reasoning_tokens`` reaches the submit as ``reasoning_tokens``."""
    completion = build_completion("Blue.", reasoning_content="The sky scatters blue light.")
    completion["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2}
    session = RoutingSession(client, _replying(completion))
    serve_one_job = _check_in(session, worker_config)
    queue_chat_request("Name one colour.")

    serve_one_job()

    [reply] = session.submits()
    assert reply.status == 200, reply.response_body
    assert reply.body["reasoning_tokens"] == 2


def test_the_backend_generates_with_the_requested_length_temperature_and_stop(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """The generation body has the request's ``max_tokens``, ``temperature`` and ``stop`` as the job holds them."""
    session = RoutingSession(client, _replying(build_completion("Blue.")))
    serve_one_job = _check_in(session, worker_config)
    request_id = queue_chat_request("Name one colour.", temperature=0.3, stop=["\nuser:", "###"])

    serve_one_job()

    [generation_body] = session.generation_bodies()
    assert generation_body["max_tokens"] == REQUESTED_MAX_TOKENS
    assert generation_body["temperature"] == 0.3
    assert generation_body["stop"] == ["\nuser:", "###"]
    assert chat_status(client, request_headers, request_id)["done"] is True


def test_a_conversation_over_the_byte_bound_faults_with_prompt_overflow(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """A conversation whose UTF-8 bytes exceed the job's context is faulted without a generation request."""
    session = RoutingSession(client, _replying(build_completion("unused")))
    serve_one_job = _check_in(session, worker_config)
    request_id = queue_chat_request("word " * 1000)

    serve_one_job()

    [fault] = session.submits()
    assert fault.body["state"] == "faulted"
    assert fault.body["fault_reason"] == "prompt_overflow"
    assert session.generation_bodies() == [], "only the template overhead probe reaches the backend"
    assert chat_status(client, request_headers, request_id)["restarted"] == 1


def test_a_backend_failure_faults_with_generation_error(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """A backend error on the generation is submitted as a fault with ``generation_error``."""

    def backend(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return (200, build_completion("")) if is_probe(body) else (500, {"error": "model crashed"})

    session = RoutingSession(client, fixed_backend(backend))
    serve_one_job = _check_in(session, worker_config)
    request_id = queue_chat_request("Name one colour.")

    serve_one_job()

    [fault] = session.submits()
    assert fault.body["state"] == "faulted"
    assert fault.body["fault_reason"] == "generation_error"
    assert chat_status(client, request_headers, request_id)["restarted"] == 1


def test_a_reply_the_horde_refuses_is_faulted_with_reply_refused(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """A reply refused with ``400`` is followed by a fault with ``reply_refused``, so the job returns to the queue.

    The example's tools run on the template strategy, which cannot back ``tool_choice_enforced``, so the worker never
    pops a strict ``tool_choice: required`` request. A strict ``json_object`` request with a prose reply is refused
    the same way.
    """
    config = replace(worker_config, features=frozenset({ChatFeature.JSON_OBJECT}))
    session = RoutingSession(client, _replying(build_completion("Blue.")))
    serve_one_job = _check_in(session, config)
    request_id = queue_chat_request("Name one colour as JSON.", response_format={"type": "json_object"})

    serve_one_job()

    refused, fault = session.submits()
    assert refused.status == 400
    assert refused.response_body["rc"] == "ChatSubmitNonconformant"
    assert fault.status == 200
    assert fault.body["fault_reason"] == "reply_refused"
    assert chat_status(client, request_headers, request_id)["restarted"] == 1


def _abort_popped_generation(app: Flask, generation_id: str) -> None:
    """Age a popped generation past its lease and fault it as the stale-generation path does."""
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(generation_id))
        assert generation is not None
        generation.start_time = datetime.utcnow() - timedelta(seconds=generation.job_ttl + 1)
        db.session.commit()
        generation.fault_and_restart(stale=True)


def test_a_reply_past_its_lease_is_not_faulted_with_reply_refused(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """A reply refused with ``AbortedGen``, a ``400`` for a job past its lease, raises without a ``reply_refused`` fault."""

    def aborting_backend(session: BackendSession) -> BackendStub:
        assert isinstance(session, RoutingSession)

        def backend(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if not is_probe(body):
                _abort_popped_generation(app, session.popped_job()["id"])
            return 200, build_completion("Blue.")

        return backend

    session = RoutingSession(client, aborting_backend)
    serve_one_job = _check_in(session, worker_config)
    request_id = queue_chat_request("Name one colour.")

    with pytest.raises(requests.HTTPError):
        serve_one_job()

    [aborted] = session.submits()
    assert aborted.status == 400
    assert aborted.response_body["rc"] == "AbortedGen"
    assert chat_status(client, request_headers, request_id)["restarted"] == 1


def test_a_backend_tool_call_reaches_the_requester_with_its_finish_reason(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """A backend reply that calls a tool is submitted with its ``tool_calls`` and ``finish_reason`` ``tool_calls``."""
    config = replace(
        worker_config,
        features=frozenset({ChatFeature.TOOLS}),
        backend=replace(worker_config.backend, template_overhead=PROBE_PROMPT_TOKENS),
    )
    tool_reply = build_completion(None, finish_reason="tool_calls", tool_calls=[build_tool_call()])
    serve_one_job = _check_in(RoutingSession(client, _replying(tool_reply)), config)
    request_id = queue_chat_request("Weather in Bergen?", tools=[LOOKUP_TOOL])

    serve_one_job()

    [generation] = chat_status(client, request_headers, request_id)["generations"]
    assert generation["message"] == {"role": "assistant", "content": None, "tool_calls": [build_tool_call()]}
    assert generation["finish_reason"] == "tool_calls"


def _llamacpp_backend(job_count: Callable[[dict[str, Any]], int]) -> Callable[[BackendSession], BackendStub]:
    """Return a llama.cpp backend factory whose probe counts as generation does and whose job counts ``job_count(payload)``.

    The job's count is computed from the payload of the session's last pop, so a test can place it against the job's
    own context and length.
    """

    def factory(session: BackendSession) -> BackendStub:
        assert isinstance(session, RoutingSession)

        def backend(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if path == "/v1/chat/completions":
                return 200, build_completion("unused")
            if path == "/apply-template":
                return 200, {"prompt": "<probe>" if body["messages"][-1]["content"] == "" else "<job>"}
            if body["content"] == "<probe>":
                return 200, {"tokens": list(range(PROBE_PROMPT_TOKENS))}
            return 200, {"tokens": list(range(job_count(session.popped_payload())))}

        return backend

    return factory


def _past_the_context_with_the_reply(payload: dict[str, Any]) -> int:
    """Return the smallest prompt count that overflows with the job's ``max_length`` added and fits its context alone."""
    return int(payload["max_context_length"] - payload["max_length"] + 1)


def test_an_exact_counter_faults_a_conversation_over_the_job_context(
    client: FlaskClient,
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """A conversation that the exact counter counts past the job's context is faulted without a generation request."""
    session = RoutingSession(client, _llamacpp_backend(lambda payload: 100_000))
    serve_one_job = _check_in(session, _with_llamacpp_counter(worker_config))
    queue_chat_request("Name one colour.")

    serve_one_job()

    [fault] = session.submits()
    assert fault.body["fault_reason"] == "prompt_overflow"
    assert session.generation_bodies() == []


def test_a_prompt_that_fits_the_context_but_not_with_the_reply_length_faults_with_prompt_overflow(
    client: FlaskClient,
    worker_config: Any,
    queue_chat_request: QueueChatRequest,
) -> None:
    """The fit check adds ``max_length`` to the prompt count, so a prompt within the context can still overflow."""
    session = RoutingSession(client, _llamacpp_backend(_past_the_context_with_the_reply))
    serve_one_job = _check_in(session, _with_llamacpp_counter(worker_config))
    queue_chat_request("Name one colour.")

    serve_one_job()

    payload = session.popped_payload()
    prompt_count = _past_the_context_with_the_reply(payload)
    assert prompt_count <= payload["max_context_length"] < prompt_count + payload["max_length"]
    [fault] = session.submits()
    assert fault.body["fault_reason"] == "prompt_overflow"
    assert session.generation_bodies() == []
