# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's request lifecycle: the deadline rule, admission slots, cancels and shutdown."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import anyio
import httpx
import pytest
import redis.asyncio
from loguru import logger

from horde.oai import waiter
from horde.oai.app import CancelState, _cancel_open_requests, create_app
from horde.oai.horde_client import HordeCredentials
from horde.oai.waiter import CompletionSignalListener
from tests.unit.oai.conftest import (
    CALL_WAIT_SECONDS,
    FAST_TEST_SETTINGS,
    REQUEST_ID,
    RunningFront,
    StartFront,
    build_chat_body,
    call_raw,
    done_status,
    response_status,
    wait_for_call,
    waiting_status,
)

pytestmark = pytest.mark.asyncio

DEADLINE_SETTINGS = FAST_TEST_SETTINGS.model_copy(
    update={"nonstream_timeout_seconds": 0.3, "fallback_poll_seconds": 0.05, "final_status_timeout_seconds": 0.2},
)
"""A short budget with frequent polls, so the deadline arrives within a test."""


def encoded_chat_body(**overrides: Any) -> list[bytes]:
    """Return a chat body as one raw ASGI chunk."""
    return [json.dumps(build_chat_body(**overrides)).encode()]


def assert_slot_released(front: RunningFront) -> None:
    """Assert the front holds no slot and no open request."""
    assert front.app.state.front.inflight_limiter.inflight == 0
    assert front.app.state.front.inflight_limiter.inflight_for("127.0.0.1") == 0
    assert front.app.state.front.inflight_limiter.inflight_for("203.0.113.9") == 0
    assert not front.app.state.front.open_requests


async def test_status_terminal_at_the_deadline_is_returned_not_408(start_front: StartFront) -> None:
    async with start_front(DEADLINE_SETTINGS, with_redis=False) as front:
        budget_ends_at = time.monotonic() + DEADLINE_SETTINGS.nonstream_timeout_seconds

        def answer_status(_request: httpx.Request) -> httpx.Response:
            if time.monotonic() < budget_ends_at:
                return httpx.Response(200, json=waiting_status(wait_time=0))
            return httpx.Response(200, json=done_status())

        front.horde.status_route.mock(side_effect=answer_status)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert front.horde.cancel_route.call_count == 0


async def test_read_finishing_after_the_deadline_with_a_terminal_status_is_returned(start_front: StartFront) -> None:
    async def answer_status(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=done_status())

    settings = FAST_TEST_SETTINGS.model_copy(update={"nonstream_timeout_seconds": 0.05, "final_status_timeout_seconds": 0.5})
    async with start_front(settings, with_redis=False) as front:
        front.horde.status_route.mock(side_effect=answer_status)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert front.horde.cancel_route.call_count == 0


@pytest.mark.parametrize("final_read", ["error", "slow", "unanswered"])
async def test_failed_final_read_still_cancels_and_answers_408(start_front: StartFront, final_read: str) -> None:
    async with start_front(DEADLINE_SETTINGS, with_redis=False) as front:
        budget_ends_at = time.monotonic() + DEADLINE_SETTINGS.nonstream_timeout_seconds

        async def answer_status(_request: httpx.Request) -> httpx.Response:
            if time.monotonic() < budget_ends_at:
                return httpx.Response(200, json=waiting_status(wait_time=0))
            if final_read == "slow":
                await asyncio.sleep(1)
                return httpx.Response(200, json=done_status())
            if final_read == "unanswered":
                raise httpx.ReadTimeout("no answer")
            return httpx.Response(404, json={"message": "Request not found.", "rc": "RequestNotFound"})

        front.horde.status_route.mock(side_effect=answer_status)
        started_at = time.monotonic()
        response = await asyncio.wait_for(front.client.post("/v1/chat/completions", json=build_chat_body()), timeout=CALL_WAIT_SECONDS)
        elapsed = time.monotonic() - started_at

    assert response.status_code == 408
    assert response.headers["x-should-retry"] == "false"
    assert front.horde.cancel_route.call_count == 1
    assert elapsed < DEADLINE_SETTINGS.nonstream_timeout_seconds + DEADLINE_SETTINGS.final_status_timeout_seconds + 0.5
    assert_slot_released(front)


@pytest.mark.parametrize("slow_read", [1, 2])
async def test_slow_status_read_is_bounded_by_the_deadline(start_front: StartFront, slow_read: int) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(
        update={"nonstream_timeout_seconds": 0.1, "fallback_poll_seconds": 0.01, "final_status_timeout_seconds": 0.1},
    )
    read_cancelled = asyncio.Event()
    calls = 0

    async def answer_status(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == slow_read:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                read_cancelled.set()
                raise
            return httpx.Response(200, json=done_status())
        return httpx.Response(200, json=waiting_status(wait_time=0))

    async with start_front(settings, with_redis=False) as front:
        front.horde.status_route.mock(side_effect=answer_status)
        response = await asyncio.wait_for(front.client.post("/v1/chat/completions", json=build_chat_body()), timeout=2)

    assert response.status_code == 408
    assert read_cancelled.is_set()
    assert front.horde.cancel_route.call_count == 1
    assert_slot_released(front)


async def test_streamed_request_has_its_own_finite_budget(start_front: StartFront) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(update={"nonstream_timeout_seconds": 5.0, "stream_timeout_seconds": 0.2})
    async with start_front(settings) as front:
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=True))
        elapsed = time.monotonic() - started_at

    assert '"ChatRequestTimeout"' in response.text
    assert elapsed < 2
    assert front.horde.cancel_route.call_count == 1


async def test_per_source_cap_refuses_a_second_request_from_one_address(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_inflight_per_source": 1})) as front:
        first_request = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        refused = await front.client.post("/v1/chat/completions", json=build_chat_body())
        _app_task, other_source_messages = await call_raw(
            front.app,
            body_chunks=encoded_chat_body(),
            disconnect_after_seconds=None,
            wait_for_app=False,
        )
        await wait_for_call(lambda: front.horde.submit_route.call_count, minimum=2)
        await front.horde.finish_later(0)
        first_response = await first_request
        await asyncio.wait_for(_app_task, timeout=CALL_WAIT_SECONDS)

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "FrontAtCapacity"
    assert first_response.status_code == 200
    assert response_status(other_source_messages) == 200
    assert_slot_released(front)


@pytest.mark.parametrize(
    "exit_path",
    ["completed", "refused", "unreachable", "lost_submit", "timeout", "faulted", "fail_fast", "status_error"],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_slot_is_released_once_on_every_exit_path(start_front: StartFront, exit_path: str, stream: bool) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(
        update={"nonstream_timeout_seconds": 0.2, "stream_timeout_seconds": 0.2, "fail_fast_grace_seconds": 0.05},
    )
    async with start_front(settings) as front:
        if exit_path == "completed":
            front.horde.status_payload = done_status()
        elif exit_path == "refused":
            front.horde.submit_route.respond(403, json={"message": "No.", "rc": "Forbidden"})
        elif exit_path == "unreachable":
            front.horde.submit_route.mock(side_effect=httpx.ConnectError("refused"))
        elif exit_path == "lost_submit":
            front.horde.submit_route.mock(side_effect=httpx.ReadError("reset"))
        elif exit_path == "faulted":
            front.horde.status_payload = {**waiting_status(), "faulted": True}
        elif exit_path == "fail_fast":
            front.horde.status_payload = waiting_status(is_possible=False)
        elif exit_path == "status_error":
            front.horde.status_code = 410
            front.horde.status_payload = {"message": "Gone.", "rc": "Gone"}
        await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))
        await asyncio.sleep(0.05)
        assert_slot_released(front)


async def test_disconnect_before_the_id_is_known_releases_the_slot_and_cancels_once(start_front: StartFront) -> None:
    async def answer_submit_slowly(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(202, json={"id": REQUEST_ID})

    async with start_front() as front:
        front.horde.submit_route.mock(side_effect=answer_submit_slowly)
        await call_raw(front.app, body_chunks=encoded_chat_body(), disconnect_after_seconds=0.05)
        await wait_for_call(lambda: front.horde.cancel_route.call_count)
        await asyncio.sleep(0.05)

    assert front.horde.cancel_route.call_count == 1
    assert_slot_released(front)


@pytest.mark.parametrize("stream", [False, True])
async def test_disconnect_during_the_wait_releases_the_slot_before_the_cancel_answers(start_front: StartFront, stream: bool) -> None:
    async with start_front() as front:
        front.horde.cancel_gate = asyncio.Event()
        app_task, _sent_messages = await call_raw(
            front.app,
            body_chunks=encoded_chat_body(stream=stream),
            disconnect_after_seconds=0.2,
            wait_for_app=False,
        )
        await wait_for_call(lambda: front.horde.cancels_received)
        inflight_while_cancel_pending = front.inflight
        front.horde.cancel_gate.set()
        await asyncio.wait_for(app_task, timeout=CALL_WAIT_SECONDS)

    assert inflight_while_cancel_pending == 0
    assert front.horde.cancel_route.call_count == 1
    assert_slot_released(front)


async def test_streamed_client_gone_before_the_stream_starts_is_cancelled_once_and_freed(start_front: StartFront) -> None:
    async with start_front() as front:
        await call_raw(
            front.app,
            body_chunks=encoded_chat_body(stream=True),
            disconnect_after_seconds=0,
            response_start_delay_seconds=0.3,
        )
        await wait_for_call(lambda: front.horde.cancel_route.call_count)
        await asyncio.sleep(0.05)

    assert front.horde.status_route.call_count == 0
    assert front.horde.cancel_route.call_count == 1
    assert_slot_released(front)


async def test_cancel_that_does_not_complete_in_time_is_logged(start_front: StartFront) -> None:
    warnings: list[str] = []
    sink_id = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        async with start_front(FAST_TEST_SETTINGS.model_copy(update={"cancel_timeout_seconds": 0.1})) as front:
            front.horde.cancel_gate = asyncio.Event()
            await call_raw(front.app, body_chunks=encoded_chat_body(), disconnect_after_seconds=0.2)
            front.horde.cancel_gate.set()
    finally:
        logger.remove(sink_id)

    assert f"Cancelling chat request {REQUEST_ID} did not complete within 0.1s" in warnings


@pytest.mark.parametrize("cancel_mode", ["asyncio", "repeated", "anyio"])
async def test_cancelled_nonstream_handler_cleans_up_the_job(start_front: StartFront, cancel_mode: str) -> None:
    scope = anyio.CancelScope()

    async with start_front() as front:
        front.horde.cancel_gate = asyncio.Event()

        async def request_completion() -> None:
            with scope:
                await front.client.post("/v1/chat/completions", json=build_chat_body())

        request_task = asyncio.create_task(request_completion())
        await wait_for_call(lambda: front.horde.status_route.call_count)
        if cancel_mode == "anyio":
            scope.cancel()
        else:
            request_task.cancel()
        await wait_for_call(lambda: front.horde.cancels_received)
        assert front.inflight == 0
        if cancel_mode == "repeated":
            request_task.cancel()
            await asyncio.sleep(0)
        assert not request_task.done()
        front.horde.cancel_gate.set()
        if cancel_mode == "anyio":
            await asyncio.wait_for(request_task, timeout=2)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request_task, timeout=2)
        assert front.horde.cancel_route.call_count == 1
        assert not front.app.state.front.listener._subscriptions


async def test_task_cancellation_cleanup_is_bounded(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"cancel_timeout_seconds": 0.05})) as front:
        front.horde.cancel_gate = asyncio.Event()
        request_task = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request_task, timeout=2)
        assert front.horde.cancels_received == 1
        assert front.inflight == 0


async def test_shutdown_cancels_open_requests_before_the_subscription_closes_within_the_bound(start_front: StartFront) -> None:
    subscribed_at_cancel: list[bool] = []
    shutdown_settings = FAST_TEST_SETTINGS.model_copy(update={"shutdown_cancel_seconds": 0.2, "cancel_timeout_seconds": 5.0})
    async with start_front(shutdown_settings) as front:
        front.horde.cancel_gate = asyncio.Event()
        front.horde.on_cancel = lambda: subscribed_at_cancel.append(front.app.state.front.listener.subscribed)
        request_task = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        shutdown_started_at = time.monotonic()
    shutdown_seconds = time.monotonic() - shutdown_started_at
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert subscribed_at_cancel == [True]
    assert front.horde.cancels_received == 1
    assert shutdown_seconds < shutdown_settings.shutdown_cancel_seconds + 1.0
    assert not front.app.state.front.listener.subscribed


async def test_shutdown_drain_retries_a_cancel_that_failed_after_the_budget_ended_once(start_front: StartFront) -> None:
    async with start_front(DEADLINE_SETTINGS, with_redis=False) as front:
        front.horde.cancel_route.mock(
            side_effect=[
                httpx.Response(503, json={"message": "Unavailable"}),
                httpx.Response(200, json=waiting_status()),
            ],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        assert_slot_released(front)
        open_request = front.app.state.front.failed_cancels[REQUEST_ID]
        assert open_request.cancel_state == CancelState.FAILED
        assert front.horde.cancel_route.call_count == 1

    assert response.status_code == 408
    assert front.horde.cancel_route.call_count == 2
    assert open_request.cancel_state == CancelState.ACKNOWLEDGED


async def test_shutdown_drain_does_not_resend_an_acknowledged_cancel(start_front: StartFront) -> None:
    async with start_front(DEADLINE_SETTINGS, with_redis=False) as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        assert front.horde.cancel_route.call_count == 1

    assert response.status_code == 408
    assert front.horde.cancel_route.call_count == 1
    assert_slot_released(front)
    assert not front.app.state.front.failed_cancels


async def test_shutdown_drain_leaves_a_cancel_in_progress_to_finish_alone(start_front: StartFront) -> None:
    settings = DEADLINE_SETTINGS.model_copy(update={"cancel_timeout_seconds": 5.0, "shutdown_cancel_seconds": 1.0})
    async with start_front(settings, with_redis=False) as front:
        front.horde.cancel_gate = asyncio.Event()
        request_task = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.cancels_received)
        open_request = front.app.state.front.open_requests[REQUEST_ID]
        assert open_request.cancel_state == CancelState.IN_PROGRESS

        await _cancel_open_requests(front.app.state.front)
        assert front.horde.cancels_received == 1

        front.horde.cancel_gate.set()
        response = await asyncio.wait_for(request_task, timeout=CALL_WAIT_SECONDS)

    assert response.status_code == 408
    assert front.horde.cancels_received == 1
    assert open_request.cancel_state == CancelState.ACKNOWLEDGED


async def test_owned_redis_client_enables_keepalive_without_the_client_health_check(monkeypatch: pytest.MonkeyPatch) -> None:
    created_clients: list[redis.asyncio.Redis] = []
    create_from_url = redis.asyncio.Redis.from_url

    def record_from_url(url: str, **kwargs: Any) -> redis.asyncio.Redis:
        redis_client = create_from_url(url, **kwargs)
        created_clients.append(redis_client)
        return redis_client

    monkeypatch.setattr(redis.asyncio.Redis, "from_url", record_from_url)
    settings = FAST_TEST_SETTINGS.model_copy(update={"redis_url": "redis://redis.test:6379/7", "redis_liveness_seconds": 12.5})
    async with httpx.AsyncClient() as horde_http_client:
        create_app(settings, http_client=horde_http_client, cancel_http_client=horde_http_client)

    assert len(created_clients) == 1
    connection_kwargs = created_clients[0].connection_pool.connection_kwargs
    await created_clients[0].close()
    assert connection_kwargs["socket_keepalive"] is True
    assert connection_kwargs.get("health_check_interval", 0) == 0
    assert created_clients[0].connection_pool.get_encoder().decode_responses is False


class FakePubSub:
    """A pattern subscription that delivers no signal and answers each PING only when ``answers_pings`` is set."""

    def __init__(self, *, answers_pings: bool) -> None:
        """Create the subscription."""
        self.answers_pings: bool = answers_pings
        """Whether a PING is answered with its PONG."""
        self.pings_sent: int = 0
        """How many PINGs the listener sent."""
        self.closed: bool = False
        """Whether the listener closed the subscription."""
        self._replies: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @property
    def subscribed(self) -> bool:
        """Return whether the subscription is open."""
        return not self.closed

    async def psubscribe(self, _pattern: str) -> None:
        """Accept the pattern subscription."""

    async def ping(self, token: str) -> None:
        """Record a PING and queue its PONG when the subscription answers."""
        self.pings_sent += 1
        if self.answers_pings:
            self._replies.put_nowait({"type": "pong", "pattern": None, "channel": None, "data": token.encode()})

    async def get_message(self, *, ignore_subscribe_messages: bool, timeout: float) -> dict[str, Any] | None:
        """Return the next queued reply, or None after ``timeout`` seconds."""
        try:
            return await asyncio.wait_for(self._replies.get(), timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        """Close the subscription."""
        self.closed = True


class FakeSignalRedis:
    """A Redis client whose every subscription is a new [`FakePubSub`][tests.unit.oai.test_lifecycle.FakePubSub]."""

    def __init__(self, *, answers_pings: bool) -> None:
        """Create the client."""
        self._answers_pings = answers_pings
        self.pubsubs: list[FakePubSub] = []
        """Every subscription the listener made, oldest first."""

    def pubsub(self) -> FakePubSub:
        """Create and record a subscription."""
        fake_pubsub = FakePubSub(answers_pings=self._answers_pings)
        self.pubsubs.append(fake_pubsub)
        return fake_pubsub


LIVENESS_SECONDS: float = 0.1
"""A liveness interval short enough that several pass within a test."""


async def test_unanswered_liveness_ping_drops_the_subscription_and_subscribes_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waiter, "LISTENER_RECONNECT_DELAY_SECONDS", 0.3)
    fake_redis = FakeSignalRedis(answers_pings=False)
    listener = CompletionSignalListener(fake_redis, liveness_seconds=LIVENESS_SECONDS)
    await listener.start()
    try:
        assert await listener.wait_until_subscribed(CALL_WAIT_SECONDS)
        await wait_for_call(lambda: fake_redis.pubsubs[0].pings_sent)
        await wait_for_call(lambda: int(not listener.subscribed))
        assert fake_redis.pubsubs[0].pings_sent == 1
        assert len(fake_redis.pubsubs) == 1
        await wait_for_call(lambda: len(fake_redis.pubsubs), minimum=2)
        assert await listener.wait_until_subscribed(CALL_WAIT_SECONDS)
        assert fake_redis.pubsubs[0].closed
    finally:
        await listener.stop()


async def test_answered_liveness_pings_keep_the_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waiter, "LISTENER_RECONNECT_DELAY_SECONDS", 0.05)
    fake_redis = FakeSignalRedis(answers_pings=True)
    listener = CompletionSignalListener(fake_redis, liveness_seconds=LIVENESS_SECONDS)
    await listener.start()
    try:
        assert await listener.wait_until_subscribed(CALL_WAIT_SECONDS)
        await wait_for_call(lambda: fake_redis.pubsubs[0].pings_sent, minimum=3)
        assert listener.subscribed
        assert len(fake_redis.pubsubs) == 1
    finally:
        await listener.stop()


async def test_failed_cancels_past_the_cap_drop_the_oldest_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("horde.oai.app.MAX_FAILED_CANCELS", 2)
    async with httpx.AsyncClient() as horde_http_client:
        front_state = create_app(FAST_TEST_SETTINGS, http_client=horde_http_client, cancel_http_client=horde_http_client).state.front
    credentials = HordeCredentials(apikey="test-key", client_agent="test:1:test")
    warnings: list[str] = []
    sink_id = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        for request_number in range(3):
            open_request = front_state.open_request(request_id=f"request-{request_number}", credentials=credentials)
            open_request.cancel_state = CancelState.FAILED
            front_state.close_request(open_request)
    finally:
        logger.remove(sink_id)

    assert list(front_state.failed_cancels) == ["request-1", "request-2"]
    assert len(warnings) == 1
    assert "request-0" in warnings[0]
