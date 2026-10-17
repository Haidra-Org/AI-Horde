# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The official OpenAI Python SDK against the chat front, the native Horde and a fake chat worker.

The module serves the native Flask app over HTTP with ``werkzeug.serving.make_server`` and the front with uvicorn,
each in a daemon thread on an ephemeral loopback port, so every SDK call crosses both real listeners. The Horde
publishes chat signals to the test Redis (database 7) that the front subscribes to. A
[`FakeChatWorker`][tests.integration._fake_chat_worker.FakeChatWorker] on its own thread pops through a Flask test
client and submits [`FIXED_REPLY`][tests.integration.test_chat_front_sdk.FIXED_REPLY] for every chat job.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import openai
import pytest
import redis
import uvicorn
from openai.types.chat import ChatCompletionMessageParam

from horde.oai.app import create_app as create_front_app
from horde.oai.settings import FrontSettings
from tests.dependency_runtime import REQUIRE_TEST_DEPS_ENV, redis_reachable
from tests.integration._fake_chat_worker import FakeChatWorker, build_chat_submit_body

if TYPE_CHECKING:
    from flask import Flask

pytestmark = pytest.mark.integration

LOOPBACK_HOST: str = "127.0.0.1"
"""The address both servers bind; an ephemeral port on loopback keeps them unreachable from elsewhere."""

CHAT_MODEL: str = "elinas/chronos-70b-v2"
"""The model the fake worker serves."""

WORKER_NAME: str = "CICD SDK Conformance Scribe"
"""The fake worker's name."""

FIXED_REPLY: str = "Blue."
"""The reply the fake worker submits for every chat job."""

WORKER_PROMPT_TOKENS: int = 21
"""The prompt token count the fake worker reports."""

WORKER_COMPLETION_TOKENS: int = 3
"""The completion token count the fake worker reports."""

CHAT_MESSAGES: list[ChatCompletionMessageParam] = [
    {"role": "system", "content": "You are a terse assistant."},
    {"role": "user", "content": "Name one colour."},
]
"""The messages of every completion request."""

CHAT_SIGNAL_REDIS_DB: int = 7
"""The Redis database the Horde publishes chat signals on, as ``horde.redis_ctrl.chat_signal_db``."""

POP_INTERVAL_SECONDS: float = 0.2
"""How long the fake worker waits after a pop that found no job."""

SERVER_START_TIMEOUT_SECONDS: float = 20.0
"""How long a fixture waits for a server or the worker's first pop before failing."""

SERVER_STOP_TIMEOUT_SECONDS: float = 30.0
"""How long teardown waits for a server thread to finish."""

FAIL_FAST_GRACE_SECONDS: float = 1.0
"""The front's no-eligible-worker grace, short so the fail-fast case finishes quickly."""

FAIL_FAST_ANSWER_LIMIT_SECONDS: float = 15.0
"""The longest the fail-fast request may take: the grace, the status read it schedules and scheduling slack."""

FALLBACK_POLL_SECONDS: float = 30.0
"""The front's fallback poll interval, longer than a served completion may take, so no answer waits on a fallback poll.

The fail-fast read is scheduled from the grace, not the poll interval, so the long interval does not slow that case.
"""

SERVED_ANSWER_LIMIT_SECONDS: float = 10.0
"""The longest a served completion may take; under the fallback poll interval, so a lost signal fails the test."""

REQUEST_BUDGET_SECONDS: float = 60.0
"""The front's request budgets; well above what a served request takes, so a stall fails rather than hangs."""

SDK_TIMEOUT_SECONDS: float = 90.0
"""The SDK's request timeout, above the front's budget so the front's own answer arrives first."""


@pytest.fixture(scope="module", autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address submit limit; every request in this module comes from loopback."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(scope="module")
def signal_redis_url() -> Iterator[str]:
    """Route the Horde's chat signals to the test Redis and return the URL the front subscribes with."""
    from horde import chat_signal

    redis_host = os.environ.get("REDIS_IP", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    if not redis_reachable(redis_host, redis_port):
        message = f"The SDK conformance run needs the test Redis at {redis_host}:{redis_port} for chat signals."
        if os.environ.get(REQUIRE_TEST_DEPS_ENV) == "1":
            pytest.fail(message)
        pytest.skip(message)
    publisher = redis.Redis(host=redis_host, port=redis_port, db=CHAT_SIGNAL_REDIS_DB, decode_responses=True)
    previous_publisher = chat_signal._publisher
    chat_signal._publisher = publisher
    try:
        yield f"redis://{redis_host}:{redis_port}/{CHAT_SIGNAL_REDIS_DB}"
    finally:
        chat_signal._publisher = previous_publisher
        publisher.close()


@pytest.fixture(scope="module")
def unserved_model() -> str:
    """Return a model the text reference lists and no worker serves, so the Horde accepts it with no eligible worker.

    A model outside the reference with no online chat worker is refused at submit, which is a different path.
    """
    from horde.model_reference import model_reference

    return next(
        model_name for model_name in sorted(model_reference.get_text_model_names()) if model_name != CHAT_MODEL and ":" not in model_name
    )


@pytest.fixture(scope="module")
def owner_api_key(app: Flask) -> str:
    """Create the trusted user that owns the fake worker and sends the SDK requests, and return its API key."""
    from horde.classes.base.user import User
    from horde.utils import generate_api_key, hash_api_key

    suffix = uuid.uuid4().hex[:8]
    raw_api_key: str = generate_api_key()  # type: ignore[no-untyped-call]
    with app.app_context():
        user = User(username=f"sdk_owner_{suffix}", oauth_id=f"sdk_owner_{suffix}", api_key=hash_api_key(raw_api_key))  # type: ignore[no-untyped-call]
        user.create()  # type: ignore[no-untyped-call]
        user.set_moderator(True)  # type: ignore[no-untyped-call]
        user.modify_kudos(10000, "admin")
        user.refresh_cache()  # type: ignore[no-untyped-call]
    return raw_api_key


@pytest.fixture(scope="module")
def horde_api_root(app: Flask) -> Iterator[str]:
    """Serve the native Flask app over HTTP on an ephemeral port and return its API root."""
    from werkzeug.serving import make_server

    native_server = make_server(LOOPBACK_HOST, 0, app, threaded=True)
    server_thread = threading.Thread(target=native_server.serve_forever, name="sdk-conformance-horde", daemon=True)
    server_thread.start()
    try:
        yield f"http://{LOOPBACK_HOST}:{native_server.server_port}/api"
    finally:
        native_server.shutdown()
        server_thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        native_server.server_close()


class FixedReplyWorker:
    """Pop chat jobs on a thread and submit [`FIXED_REPLY`][tests.integration.test_chat_front_sdk.FIXED_REPLY] for each.

    Thread Safety:
        The thread builds its own Flask test client; each of its requests pushes and pops its own app context, so it
        shares no ORM session with the test thread.
    """

    def __init__(self, *, app: Flask, headers: dict[str, str]) -> None:
        """Create the worker without starting it.

        Args:
            app: The native Flask app.
            headers: The owner's ``apikey`` and ``Client-Agent`` headers.
        """
        self._app = app
        self._headers = headers
        self._stop_event = threading.Event()
        self._first_pop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sdk-conformance-worker", daemon=True)
        self.failures: list[str] = []
        """Each unexpected pop or submit answer, or an exception that ended the thread."""

    def start(self) -> None:
        """Start popping and wait until the first pop registered the worker with the Horde."""
        self._thread.start()
        assert self._first_pop_event.wait(SERVER_START_TIMEOUT_SECONDS), self.failures

    def stop(self) -> None:
        """Stop popping and wait for the thread to finish."""
        self._stop_event.set()
        self._thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        worker = FakeChatWorker(client=self._app.test_client(), headers=self._headers, name=WORKER_NAME, models=[CHAT_MODEL])
        try:
            while not self._stop_event.is_set():
                if not self._serve_one(worker):
                    self._stop_event.wait(POP_INTERVAL_SECONDS)
        except Exception as error:
            self.failures.append(f"The worker thread stopped: {error!r}")
            raise

    def _serve_one(self, worker: FakeChatWorker) -> bool:
        """Pop once and submit the fixed reply for a job; return whether a job was served."""
        pop_response = worker.pop_response()
        self._first_pop_event.set()
        if pop_response.status_code != 200:
            self.failures.append(f"pop {pop_response.status_code}: {pop_response.get_data(as_text=True)}")
            return False
        generation_id = pop_response.get_json()["id"]
        if generation_id is None:
            return False
        submit_response = worker.submit(
            build_chat_submit_body(
                generation_id=generation_id,
                content=FIXED_REPLY,
                model_id=CHAT_MODEL,
                prompt_tokens=WORKER_PROMPT_TOKENS,
                completion_tokens=WORKER_COMPLETION_TOKENS,
            ),
        )
        if submit_response.status_code != 200:
            self.failures.append(f"submit {submit_response.status_code}: {submit_response.get_data(as_text=True)}")
        return True


@pytest.fixture(scope="module")
def fixed_reply_worker(app: Flask, owner_api_key: str, CIVERSION: str) -> Iterator[FixedReplyWorker]:
    """Run the fake worker for the module, registered with the Horde and its model list before the front starts.

    The native model list reads a cache that a maintenance thread refreshes in production; the fixture refreshes it
    once after the first pop, since the test app runs no maintenance threads.
    """
    from horde.database.threads import store_available_models

    worker = FixedReplyWorker(
        app=app,
        headers={"apikey": owner_api_key, "Client-Agent": f"aihorde_ci_sdk_worker:{CIVERSION}:(discord)db0#1625"},
    )
    worker.start()
    store_available_models()  # type: ignore[no-untyped-call]
    try:
        yield worker
    finally:
        worker.stop()


@pytest.fixture(autouse=True)
def _worker_kept_up(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test during which the fake worker saw an unexpected answer or stopped."""
    yield
    if "fixed_reply_worker" in request.fixturenames:
        worker: FixedReplyWorker = request.getfixturevalue("fixed_reply_worker")
        assert worker.failures == []


@pytest.fixture(scope="module")
def front_base_url(horde_api_root: str, signal_redis_url: str, fixed_reply_worker: FixedReplyWorker) -> Iterator[str]:
    """Serve the chat front with uvicorn on an ephemeral port and return its OpenAI base URL."""
    settings = FrontSettings(
        horde_urls=(horde_api_root,),
        listen=LOOPBACK_HOST,
        redis_url=signal_redis_url,
        nonstream_timeout_seconds=REQUEST_BUDGET_SECONDS,
        stream_timeout_seconds=REQUEST_BUDGET_SECONDS,
        fallback_poll_seconds=FALLBACK_POLL_SECONDS,
        fail_fast_grace_seconds=FAIL_FAST_GRACE_SECONDS,
        shutdown_drain_seconds=5.0,
    )
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.bind((LOOPBACK_HOST, 0))
    front_port = listening_socket.getsockname()[1]
    front_server = uvicorn.Server(
        uvicorn.Config(
            create_front_app(settings),
            lifespan="on",
            log_level="warning",
            timeout_graceful_shutdown=5,
        ),
    )
    server_thread = threading.Thread(
        target=front_server.run,
        kwargs={"sockets": [listening_socket]},
        name="sdk-conformance-front",
        daemon=True,
    )
    server_thread.start()
    started_by = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not front_server.started:
        assert server_thread.is_alive(), "The front's uvicorn thread exited before it started serving."
        assert time.monotonic() < started_by, "The front did not start serving in time."
        time.sleep(0.05)
    try:
        yield f"http://{LOOPBACK_HOST}:{front_port}/v1"
    finally:
        front_server.should_exit = True
        server_thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        listening_socket.close()


@pytest.fixture
def sdk_client(front_base_url: str, owner_api_key: str) -> Iterator[openai.OpenAI]:
    """Return the official SDK client addressed at the front, with the owner's key as its bearer token."""
    client = openai.OpenAI(base_url=front_base_url, api_key=owner_api_key, timeout=SDK_TIMEOUT_SECONDS)
    try:
        yield client
    finally:
        client.close()


def test_the_model_list_includes_the_fake_workers_model(sdk_client: openai.OpenAI, unserved_model: str) -> None:
    model_ids = [model.id for model in sdk_client.models.list()]

    assert CHAT_MODEL in model_ids
    assert unserved_model not in model_ids


def test_a_completion_returns_the_workers_reply_with_its_usage(sdk_client: openai.OpenAI) -> None:
    started_at = time.monotonic()
    completion = sdk_client.chat.completions.create(model=CHAT_MODEL, messages=CHAT_MESSAGES)
    elapsed_seconds = time.monotonic() - started_at

    [choice] = completion.choices
    assert choice.message.role == "assistant"
    assert choice.message.content == FIXED_REPLY
    assert choice.finish_reason == "stop"
    assert completion.model == CHAT_MODEL
    assert completion.usage is not None
    assert completion.usage.prompt_tokens == WORKER_PROMPT_TOKENS
    assert completion.usage.completion_tokens == WORKER_COMPLETION_TOKENS
    assert completion.usage.total_tokens == WORKER_PROMPT_TOKENS + WORKER_COMPLETION_TOKENS
    horde_details: dict[str, Any] = (completion.model_extra or {})["x_horde"]
    assert horde_details["request_id"]
    assert horde_details["worker_name"] == WORKER_NAME
    assert horde_details["estimated_usage"] is False
    assert elapsed_seconds < SERVED_ANSWER_LIMIT_SECONDS


def test_a_streamed_completion_yields_role_content_finish_and_usage_chunks(sdk_client: openai.OpenAI) -> None:
    started_at = time.monotonic()
    stream = sdk_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=CHAT_MESSAGES,
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = list(stream)
    elapsed_seconds = time.monotonic() - started_at

    role_chunk, content_chunk, finish_chunk, usage_chunk = chunks
    assert len({chunk.id for chunk in chunks}) == 1
    assert role_chunk.choices[0].delta.role == "assistant"
    assert content_chunk.choices[0].delta.content == FIXED_REPLY
    assert finish_chunk.choices[0].finish_reason == "stop"
    assert (finish_chunk.model_extra or {})["x_horde"]["request_id"]
    assert usage_chunk.choices == []
    assert usage_chunk.usage is not None
    assert usage_chunk.usage.prompt_tokens == WORKER_PROMPT_TOKENS
    assert usage_chunk.usage.completion_tokens == WORKER_COMPLETION_TOKENS
    assert all(chunk.usage is None for chunk in (role_chunk, content_chunk, finish_chunk))
    assert elapsed_seconds < SERVED_ANSWER_LIMIT_SECONDS


def test_logit_bias_is_refused_with_its_param(sdk_client: openai.OpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as raised:
        sdk_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=CHAT_MESSAGES,
            logit_bias={"50256": -100},
        )

    assert raised.value.status_code == 400
    assert raised.value.param == "logit_bias"
    assert isinstance(raised.value.body, dict)
    assert raised.value.body["param"] == "logit_bias"


def test_a_model_without_an_online_worker_fails_fast_without_an_sdk_retry(sdk_client: openai.OpenAI, unserved_model: str) -> None:
    started_at = time.monotonic()
    with pytest.raises(openai.InternalServerError) as raised:
        sdk_client.chat.completions.create(model=unserved_model, messages=CHAT_MESSAGES)
    elapsed_seconds = time.monotonic() - started_at

    assert raised.value.status_code == 503
    assert raised.value.code == "no_eligible_worker"
    assert raised.value.response.headers["x-should-retry"] == "false"
    assert elapsed_seconds < FAIL_FAST_ANSWER_LIMIT_SECONDS
