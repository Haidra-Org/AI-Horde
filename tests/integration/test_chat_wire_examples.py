# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The worker and native requester chat wire examples in ``docs/reference/chat_wire_examples.md`` match the code.

Each response example has a scenario here that produces the live body through the in-process app, compared as
[`structure_differences`][tests.integration._wire_examples.structure_differences] describes. Each request example is
posted with its placeholders filled in and has to get the status of the response example that answers it. The worker
routes ignore keys they do not read, so each worker request example's keys are also checked against the route's
parser and models. The examples whose id starts with ``FRONT_EXAMPLE_PREFIX`` are checked against the
OpenAI-compatible front in ``test_chat_front_wire_examples.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from flask_restx import fields as restx_fields

from tests.integration._fake_chat_worker import (
    ANONYMOUS_HEADERS,
    CHAT_MESSAGES,
    CHAT_MODEL,
    LONG_HISTORY,
    LOOKUP_TOOL,
    MESSAGES_URL_TRANSPORTS,
    TOOL_HISTORY,
    TOOLS_FEATURES,
    TOOLS_REQUEST_EXTRAS,
    VISION_FEATURES,
    FakeChatWorker,
    build_chat_submit_body,
    build_faulted_chat_submit_body,
    cancel_request,
    chat_status,
    image_body,
    png_data_url,
    post_progress,
    serve_reply,
    submit_chat_request,
    submit_image_chat_request,
    take_worker_offline,
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
    from flask.testing import FlaskClient
    from flask_restx import Model
    from flask_restx.reqparse import RequestParser
    from werkzeug.test import TestResponse

pytestmark = pytest.mark.integration

CHAT_ASYNC_ROUTE = "/api/v2/generate/chat/async"
"""The native route a requester queues a chat request on."""

NO_ELIGIBLE_WORKER_MAX_TOKENS = 1024
"""A completion length above the free output floor of 512, which the anonymous key cannot hold kudos for, and within
the scribe's chat limit, so the only gap the refusal states is the missing ``vision`` declaration."""


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address submit limit, which this module's request volume would otherwise trip."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


JOB_ID_PLACEHOLDER = "<job-id>"
"""The placeholder the page writes for a generation id."""

WORKER_NAME_PLACEHOLDER = "<worker-name>"
"""The placeholder the page writes for the worker name."""

BRIDGE_AGENT_PLACEHOLDER = "<bridge-agent>"
"""The placeholder the page writes for the worker's bridge agent string."""

IMAGE_TRANSPORTS = ["url", "base64"]
"""The image transports the documented worker details declare."""

SCRIBE_NAME = "CICD Wire Example Scribe"
"""The worker most scenarios pop with; the user's worker count is capped, so scenarios share one."""

DECLARED_FEATURES = [
    "seed",
    "presence_penalty",
    "vision",
    "tool_choice_enforced",
    "prompt_overflow_refused",
]
"""The features and guarantees the documented worker details declare."""


@dataclass
class WireRun:
    """Holds one test's client and the workers and requests it creates, released when the test ends."""

    app: Flask
    """The in-process app."""

    client: FlaskClient
    """The Flask test client."""

    headers: dict[str, str]
    """The requesting user's headers, who also owns the workers."""

    request: pytest.FixtureRequest
    """The test's fixture request, for fixtures only some scenarios need."""

    worker_names: list[str] = field(default_factory=list)
    """The workers to take offline when the test ends."""

    open_requests: list[str] = field(default_factory=list)
    """The chat requests to cancel when the test ends."""

    def worker(self, *, fresh_name: bool = False, **pop_options: Any) -> FakeChatWorker:
        """Return a worker and bring it online with one pop, which finds no job.

        Each pop replaces the stored declaration, so scenarios share ``SCRIBE_NAME`` with their own pop options.

        Args:
            fresh_name: Whether to pop under a new name, for a scenario that reads worker details: single worker
                details are cached for 30 seconds per worker.
            **pop_options: Pop body options the worker sends on every pop.

        Returns:
            The online worker.
        """
        worker = FakeChatWorker(
            client=self.client,
            headers=self.headers,
            name=self.worker_name(fresh_name=fresh_name),
            models=[CHAT_MODEL],
            pop_options=pop_options,
        )
        assert worker.pop()["id"] is None
        return worker

    def worker_name(self, *, fresh_name: bool = False) -> str:
        """Return the name a scenario's worker pops under, tracked to be taken offline when the test ends.

        Args:
            fresh_name: Whether to return a new name in place of ``SCRIBE_NAME``.

        Returns:
            The worker name.
        """
        worker_name = f"{SCRIBE_NAME} {uuid.uuid4().hex[:8]}" if fresh_name else SCRIBE_NAME
        if worker_name not in self.worker_names:
            self.worker_names.append(worker_name)
        return worker_name

    def queue(self, **overrides: Any) -> str:
        """Queue a chat request through the native route and track it for cancellation.

        Args:
            **overrides: Request body overrides.

        Returns:
            The request id.
        """
        return self.track(submit_chat_request(self.client, self.headers, **overrides))

    def track(self, request_id: str) -> str:
        """Track a request queued elsewhere for cancellation.

        Args:
            request_id: The request id.

        Returns:
            The same request id.
        """
        self.open_requests.append(request_id)
        return request_id

    def cancel(self, request_id: str) -> None:
        """Cancel a tracked request now, while the fixtures it depends on are still in place.

        Args:
            request_id: The request id.
        """
        cancel_request(self.client, self.headers, "chat", request_id)
        self.open_requests.remove(request_id)

    def popped_job(self, worker: FakeChatWorker) -> str:
        """Pop the queued job and return its generation id.

        Args:
            worker: The worker that pops.

        Returns:
            The generation id.
        """
        pop_result = worker.pop()
        generation_id: str | None = pop_result["id"]
        assert generation_id is not None, pop_result
        return generation_id


@pytest.fixture
def wire(app: Flask, client: FlaskClient, request_headers: dict[str, str], request: pytest.FixtureRequest) -> Iterator[WireRun]:
    """A scenario context that cancels its requests and takes its workers offline afterwards.

    A worker left online would make later tests' requests possible, which tests of an unserved model rely on not being.
    """
    run = WireRun(app=app, client=client, headers=request_headers, request=request)
    yield run
    for request_id in run.open_requests:
        client.delete(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)
    for worker_name in run.worker_names:
        take_worker_offline(app, worker_name)


@dataclass(frozen=True)
class RouteSchema:
    """Represents what a worker route reads from a request body: its parser's arguments and its body model."""

    parser: RequestParser
    """The parser the route parses its arguments with."""

    model: Model
    """The model the route validates the body against, whose nested models hold the keys of nested objects."""

    def json_arguments(self) -> set[str]:
        """Return the body keys the parser reads; empty for a parser that reads only headers."""
        return {argument.name for argument in self.parser.args if "json" in argument.location}


def _pop_schema() -> RouteSchema:
    from horde.apis.v2.kobold import models, parsers

    return RouteSchema(parser=parsers.job_pop_parser, model=models.input_model_job_pop)


def _chat_submit_schema() -> RouteSchema:
    from horde.apis.v2.kobold import models, parsers

    return RouteSchema(parser=parsers.chat_job_submit_parser, model=models.input_model_job_submit)


def _progress_schema() -> RouteSchema:
    from horde.apis.v2.kobold import models, parsers

    return RouteSchema(parser=parsers.chat_progress_parser, model=models.input_model_chat_progress)


def _unread_nested_keys(documented: Any, model_field: Any, path: str) -> list[str]:
    """Return the keys of a documented nested object that its model field does not declare."""
    if isinstance(model_field, restx_fields.Nested) and isinstance(documented, dict):
        nested_model = model_field.nested
        unread = [f"{path}.{key}" for key in documented if key not in nested_model]
        for key, value in documented.items():
            unread += _unread_nested_keys(value, nested_model.get(key), f"{path}.{key}")
        return unread
    if isinstance(model_field, restx_fields.List) and isinstance(documented, list):
        return [
            unread_key
            for index, element in enumerate(documented)
            for unread_key in _unread_nested_keys(element, model_field.container, f"{path}[{index}]")
        ]
    return []


def _unread_keys(documented: dict[str, Any], schema: RouteSchema) -> list[str]:
    """Return the documented keys the route does not read.

    A top-level key has to be an argument of the route's parser, or a field of its model when the parser reads only
    headers, as the progress route's does. A nested object's keys have to be fields of the nested model the route's
    model declares for it.
    """
    body_model = schema.model.resolved
    top_level_keys = schema.json_arguments() or set(body_model)
    unread = [f"$.{key}" for key in documented if key not in top_level_keys]
    for key, value in documented.items():
        unread += _unread_nested_keys(value, body_model.get(key), f"$.{key}")
    return unread


def _post_pop_request(wire: WireRun, documented: dict[str, Any]) -> TestResponse:
    # A fresh name keeps the details read after this pop clear of the 30-second worker details cache.
    placeholders = {
        WORKER_NAME_PLACEHOLDER: wire.worker_name(fresh_name=True),
        MODEL_NAME_PLACEHOLDER: CHAT_MODEL,
        BRIDGE_AGENT_PLACEHOLDER: wire.headers["Client-Agent"],
    }
    return wire.client.post("/api/v2/generate/text/pop", json=filled(documented, placeholders), headers=wire.headers)


def _live_pop_chat_job(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    wire.queue()
    return worker.pop_response()


def _live_pop_tools_job(wire: WireRun) -> TestResponse:
    worker = wire.worker(chat_features=TOOLS_FEATURES)
    wire.queue(messages=TOOL_HISTORY, **TOOLS_REQUEST_EXTRAS)
    return worker.pop_response()


def _live_pop_image_job(wire: WireRun) -> TestResponse:
    wire.request.getfixturevalue("object_store_ready")
    worker = wire.worker(chat_features=VISION_FEATURES, image_transports=["base64"])
    wire.track(submit_image_chat_request(wire.client, wire.headers, image_body(png_data_url())))
    return worker.pop_response()


def _live_pop_messages_url_job(wire: WireRun) -> TestResponse:
    wire.request.getfixturevalue("object_store_ready")
    wire.request.getfixturevalue("messages_offload")
    worker = wire.worker(chat_features=VISION_FEATURES, image_transports=MESSAGES_URL_TRANSPORTS)
    wire.track(submit_image_chat_request(wire.client, wire.headers, image_body(png_data_url())))
    return worker.pop_response()


def _live_pop_no_job(wire: WireRun) -> TestResponse:
    return wire.worker().pop_response()


def _post_submit(wire: WireRun, documented: dict[str, Any], *, finished: bool, **request_overrides: Any) -> TestResponse:
    """Pop a job for a request with ``request_overrides`` and post the documented submit for it."""
    worker = wire.worker(chat_features=TOOLS_FEATURES)
    request_id = wire.queue(**request_overrides)
    response = worker.submit(filled(documented, {JOB_ID_PLACEHOLDER: wire.popped_job(worker), MODEL_NAME_PLACEHOLDER: CHAT_MODEL}))
    if finished and response.status_code == 200:
        wire.open_requests.remove(request_id)
    return response


def _live_submit_reply_response(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    wire.queue()
    return worker.submit(build_chat_submit_body(generation_id=wire.popped_job(worker), content="Blue.", model_id=CHAT_MODEL))


def _live_submit_fault_response(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    wire.queue()
    return worker.submit(
        build_faulted_chat_submit_body(generation_id=wire.popped_job(worker), model_id=CHAT_MODEL, fault_reason="prompt_overflow"),
    )


def _streamed_job(wire: WireRun) -> tuple[str, str]:
    """Pop a streamed chat job with its lease on an in-memory Redis, and return the request and generation ids."""
    wire.request.getfixturevalue("lua_redis")
    worker = wire.worker()
    request_id = wire.queue(stream=True)
    return request_id, wire.popped_job(worker)


def _post_progress_post(wire: WireRun, documented: dict[str, Any]) -> TestResponse:
    request_id, generation_id = _streamed_job(wire)
    response = wire.client.post(
        "/api/v2/generate/text/progress",
        json=filled(documented, {JOB_ID_PLACEHOLDER: generation_id}),
        headers={"apikey": wire.headers["apikey"]},
    )
    wire.cancel(request_id)
    return response


def _live_progress_accepted(wire: WireRun) -> TestResponse:
    request_id, generation_id = _streamed_job(wire)
    response = post_progress(wire.client, wire.headers["apikey"], generation_id, 1, "Blue")
    wire.cancel(request_id)
    return response


def _live_progress_cancelled(wire: WireRun) -> TestResponse:
    request_id, generation_id = _streamed_job(wire)
    post_progress(wire.client, wire.headers["apikey"], generation_id, 1, "Bl")
    wire.cancel(request_id)
    return post_progress(wire.client, wire.headers["apikey"], generation_id, 2, "ue")


def _live_progress_out_of_sequence(wire: WireRun) -> TestResponse:
    request_id, generation_id = _streamed_job(wire)
    post_progress(wire.client, wire.headers["apikey"], generation_id, 1, "Bl")
    response = post_progress(wire.client, wire.headers["apikey"], generation_id, 3, "ue")
    wire.cancel(request_id)
    return response


def _live_chat_declaration_invalid(wire: WireRun) -> TestResponse:
    return wire.worker().pop_response(model_id="koboldcpp/model_Q4")


def _live_chat_submit_invalid(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    wire.queue()
    submit_body = build_chat_submit_body(generation_id=wire.popped_job(worker), content="Blue.", model_id=CHAT_MODEL)
    del submit_body["finish_reason"]
    return worker.submit(submit_body)


def _live_chat_submit_nonconformant(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    request_id, submit_response = serve_reply(
        wire.client,
        wire.headers,
        worker,
        {"tools": [LOOKUP_TOOL], "tool_choice": "required"},
        content="Sunny.",
    )
    wire.track(request_id)
    refusal: TestResponse = submit_response
    return refusal


def _live_chat_model_mismatch(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    wire.queue()
    return worker.submit(build_chat_submit_body(generation_id=wire.popped_job(worker), content="Blue.", model_id="another/model"))


def _live_worker_details(wire: WireRun) -> TestResponse:
    worker = wire.worker(
        fresh_name=True,
        chat_features=DECLARED_FEATURES,
        chat_guarantees=[],
        tools_max_temperature=0.7,
        image_transports=IMAGE_TRANSPORTS,
    )
    return wire.client.get(f"/api/v2/workers/name/{worker.name}", headers={"Client-Agent": wire.headers["Client-Agent"]})


def _post_chat_request(wire: WireRun, documented: dict[str, Any]) -> TestResponse:
    wire.worker()
    response = wire.client.post(CHAT_ASYNC_ROUTE, json=filled(documented, {MODEL_NAME_PLACEHOLDER: CHAT_MODEL}), headers=wire.headers)
    if response.status_code == 202:
        wire.track(response.get_json()["id"])
    return response


def _live_chat_request_accepted(wire: WireRun) -> TestResponse:
    wire.worker()
    response = wire.client.post(CHAT_ASYNC_ROUTE, json={"model": CHAT_MODEL, "messages": CHAT_MESSAGES}, headers=wire.headers)
    if response.status_code == 202:
        wire.track(response.get_json()["id"])
    return response


def _live_chat_dry_run(wire: WireRun) -> TestResponse:
    wire.worker()
    return wire.client.post(
        CHAT_ASYNC_ROUTE,
        json={"model": CHAT_MODEL, "messages": CHAT_MESSAGES, "max_tokens": 64, "horde": {"dry_run": True}},
        headers=wire.headers,
    )


def _status_response(wire: WireRun, request_id: str) -> TestResponse:
    return wire.client.get(f"/api/v2/generate/chat/status/{request_id}", headers=wire.headers)


def _live_chat_status_waiting(wire: WireRun) -> TestResponse:
    wire.worker()
    return _status_response(wire, wire.queue(max_tokens=64))


def _live_chat_status_done(wire: WireRun) -> TestResponse:
    worker = wire.worker()
    request_id = wire.queue(max_tokens=64)
    submit_response = worker.submit(
        build_chat_submit_body(
            generation_id=wire.popped_job(worker),
            content="Blue.",
            model_id=CHAT_MODEL,
            prompt_tokens=21,
            completion_tokens=3,
            native_finish_reason="eos_token",
        ),
    )
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    wire.open_requests.remove(request_id)
    assert chat_status(wire.client, wire.headers, request_id)["done"] is True
    return _status_response(wire, request_id)


def _live_chat_status_context_adjustments(wire: WireRun) -> TestResponse:
    wire.worker()
    return _status_response(wire, wire.queue(messages=LONG_HISTORY, max_tokens=64, horde={"max_context_length": 2048}))


def _live_no_eligible_worker(wire: WireRun) -> TestResponse:
    wire.request.getfixturevalue("object_store_ready")
    wire.worker()
    return wire.client.post(
        CHAT_ASYNC_ROUTE,
        json=image_body(png_data_url(), max_tokens=NO_ELIGIBLE_WORKER_MAX_TOKENS),
        headers=ANONYMOUS_HEADERS,
    )


def _live_unsupported_chat_parameter(wire: WireRun) -> TestResponse:
    return wire.client.post(CHAT_ASYNC_ROUTE, json={"model": CHAT_MODEL, "messages": CHAT_MESSAGES, "logprobs": True}, headers=wire.headers)


def _live_chat_model_list(wire: WireRun) -> TestResponse:
    from horde.database.threads import store_available_models

    wire.worker()
    # The test app runs no maintenance threads, so the model list cache is refreshed here, as the production thread would.
    store_available_models()  # type: ignore[no-untyped-call]
    return wire.client.get("/api/v2/status/models?type=text&chat=true", headers={"Client-Agent": wire.headers["Client-Agent"]})


@dataclass(frozen=True)
class Scenario:
    """Produces one response example's live body, or posts one request example."""

    live: Callable[[WireRun], TestResponse] | None = None
    """Returns the live response; None for a request example."""

    status: int | None = None
    """The HTTP status of a response example, which its prose states; None for a request example."""

    post: Callable[[WireRun, dict[str, Any]], TestResponse] | None = None
    """Posts the documented request body with its placeholders filled in; None for a response example."""

    answered_by: str | None = None
    """The response example whose status the posted documented body gets."""

    schema: Callable[[], RouteSchema] | None = None
    """Returns what the route reads from the body, for a worker request example; the route ignores other keys."""

    object_storage: bool = False
    """Whether the scenario stores images, which needs the object store."""


SCENARIOS: dict[str, Scenario] = {
    "pop-request": Scenario(
        post=_post_pop_request,
        answered_by="pop-no-job",
        schema=_pop_schema,
    ),
    "pop-chat-job": Scenario(
        live=_live_pop_chat_job,
        status=200,
    ),
    "pop-chat-job-tools": Scenario(
        live=_live_pop_tools_job,
        status=200,
    ),
    "pop-chat-job-image": Scenario(
        live=_live_pop_image_job,
        status=200,
        object_storage=True,
    ),
    "pop-chat-job-messages-url": Scenario(
        live=_live_pop_messages_url_job,
        status=200,
        object_storage=True,
    ),
    "pop-no-job": Scenario(
        live=_live_pop_no_job,
        status=200,
    ),
    "submit-reply": Scenario(
        post=lambda wire, documented: _post_submit(wire, documented, finished=True),
        answered_by="submit-reply-response",
        schema=_chat_submit_schema,
    ),
    "submit-tool-calls": Scenario(
        post=lambda wire, documented: _post_submit(
            wire,
            documented,
            finished=True,
            messages=TOOL_HISTORY,
            **TOOLS_REQUEST_EXTRAS,
        ),
        answered_by="submit-reply-response",
        schema=_chat_submit_schema,
    ),
    "submit-reasoning": Scenario(
        post=lambda wire, documented: _post_submit(wire, documented, finished=True),
        answered_by="submit-reply-response",
        schema=_chat_submit_schema,
    ),
    "submit-fault": Scenario(
        post=lambda wire, documented: _post_submit(wire, documented, finished=False),
        answered_by="submit-fault-response",
        schema=_chat_submit_schema,
    ),
    "submit-reply-response": Scenario(
        live=_live_submit_reply_response,
        status=200,
    ),
    "submit-fault-response": Scenario(
        live=_live_submit_fault_response,
        status=200,
    ),
    "progress-post": Scenario(
        post=_post_progress_post,
        answered_by="progress-accepted",
        schema=_progress_schema,
    ),
    "progress-accepted": Scenario(
        live=_live_progress_accepted,
        status=200,
    ),
    "progress-cancelled": Scenario(
        live=_live_progress_cancelled,
        status=200,
    ),
    "progress-out-of-sequence": Scenario(
        live=_live_progress_out_of_sequence,
        status=409,
    ),
    "refusal-chat-declaration-invalid": Scenario(
        live=_live_chat_declaration_invalid,
        status=400,
    ),
    "refusal-chat-submit-invalid": Scenario(
        live=_live_chat_submit_invalid,
        status=400,
    ),
    "refusal-chat-submit-nonconformant": Scenario(
        live=_live_chat_submit_nonconformant,
        status=400,
    ),
    "refusal-chat-model-mismatch": Scenario(
        live=_live_chat_model_mismatch,
        status=400,
    ),
    "worker-details": Scenario(
        live=_live_worker_details,
        status=200,
    ),
    "chat-request": Scenario(
        post=_post_chat_request,
        answered_by="chat-request-accepted",
    ),
    "chat-request-accepted": Scenario(
        live=_live_chat_request_accepted,
        status=202,
    ),
    "chat-dry-run": Scenario(
        live=_live_chat_dry_run,
        status=200,
    ),
    "chat-status-waiting": Scenario(
        live=_live_chat_status_waiting,
        status=200,
    ),
    "chat-status-done": Scenario(
        live=_live_chat_status_done,
        status=200,
    ),
    "chat-status-context-adjustments": Scenario(
        live=_live_chat_status_context_adjustments,
        status=200,
    ),
    "refusal-no-eligible-worker": Scenario(
        live=_live_no_eligible_worker,
        status=403,
        object_storage=True,
    ),
    "refusal-unsupported-chat-parameter": Scenario(
        live=_live_unsupported_chat_parameter,
        status=400,
    ),
    "chat-model-list": Scenario(
        live=_live_chat_model_list,
        status=200,
    ),
}
"""Every worker and native requester example on the page by its marker id."""


def _scenario_params(example_ids: list[str]) -> list[Any]:
    return [
        pytest.param(example_id, id=example_id, marks=[pytest.mark.object_storage] if SCENARIOS[example_id].object_storage else [])
        for example_id in example_ids
    ]


def test_every_marker_has_a_scenario_and_every_scenario_a_marker() -> None:
    """The front's examples are matched to their scenarios in ``test_chat_front_wire_examples.py``."""
    assert sorted(example_id for example_id in DOCUMENTED if not example_id.startswith(FRONT_EXAMPLE_PREFIX)) == sorted(SCENARIOS)


@pytest.mark.parametrize(
    "example_id",
    _scenario_params(sorted(example_id for example_id, scenario in SCENARIOS.items() if scenario.live is not None)),
)
def test_each_response_example_matches_the_live_response(wire: WireRun, example_id: str) -> None:
    """A response example has the live response's keys, contract values and value types."""
    scenario = SCENARIOS[example_id]
    assert scenario.live is not None

    live = scenario.live(wire)

    assert live.status_code == scenario.status, live.get_data(as_text=True)
    assert structure_differences(DOCUMENTED[example_id].body, live.get_json()) == [], json.dumps(live.get_json(), indent=2)


@pytest.mark.parametrize("example_id", sorted(example_id for example_id, scenario in SCENARIOS.items() if scenario.schema is not None))
def test_each_worker_request_example_holds_only_keys_the_route_reads(example_id: str) -> None:
    """A misspelt key in a worker request example would be ignored by the route, so the post alone does not catch it."""
    scenario = SCENARIOS[example_id]
    assert scenario.schema is not None

    assert _unread_keys(DOCUMENTED[example_id].body, scenario.schema()) == []


@pytest.mark.parametrize(
    "example_id",
    _scenario_params(sorted(example_id for example_id, scenario in SCENARIOS.items() if scenario.post is not None)),
)
def test_each_documented_request_body_gets_the_documented_status(wire: WireRun, example_id: str) -> None:
    """The documented request, posted with live ids, gets the status of the response example that answers it."""
    scenario = SCENARIOS[example_id]
    assert scenario.post is not None
    assert scenario.answered_by is not None

    response = scenario.post(wire, DOCUMENTED[example_id].body)

    assert response.status_code == SCENARIOS[scenario.answered_by].status, response.get_data(as_text=True)


def test_the_documented_pop_request_reaches_the_worker_details(wire: WireRun) -> None:
    """The worker details read back the chat declaration the documented pop request sends."""
    documented = DOCUMENTED["pop-request"].body
    response = _post_pop_request(wire, documented)
    assert response.status_code == 200, response.get_data(as_text=True)

    [worker_name] = wire.worker_names
    details = wire.client.get(f"/api/v2/workers/name/{worker_name}", headers=wire.headers).get_json()

    assert details["accepts_chat_jobs"] is documented["chat_jobs"]
    assert details["chat"] == {
        "model_id": CHAT_MODEL,
        "features": sorted(documented["chat_features"]),
        "limits": {**documented["chat_limits"], "tools_max_temperature": None},
        "image_transports": sorted(documented["image_transports"]),
    }


@pytest.mark.parametrize("example_id", sorted(example_id for example_id, scenario in SCENARIOS.items() if scenario.status is not None))
def test_each_response_example_states_its_status(example_id: str) -> None:
    """The prose before a response example states the status the live response has."""
    assert f"`{SCENARIOS[example_id].status}`" in DOCUMENTED[example_id].prose
