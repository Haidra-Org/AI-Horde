# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fixtures running the chat front in process against a mocked Horde and an in-process fake Redis.

[`FakeHorde`][tests.unit.oai.conftest.FakeHorde] answers the Horde routes the front calls through ``respx`` and lets a
test change the status a request reports, publish its completion signal and append to its delta stream. The front is driven through
``httpx.ASGITransport``, which runs the app to completion before returning, so a streamed response arrives whole.
[`call_raw`][tests.unit.oai.conftest.call_raw] drives the app over raw ASGI where a test controls the body's arrival or
the client's disconnect. Each front records its metrics on its own meter provider with an in-memory reader, read
through [`metric_points`][tests.unit.oai.conftest.metric_points].
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, MutableMapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import fakeredis
import httpx
import pytest_asyncio
import respx
from fastapi import FastAPI
from opentelemetry.metrics import NoOpMeter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge, Histogram, InMemoryMetricReader, Sum

from horde.oai.app import create_app
from horde.oai.metrics import METER_NAME, FrontMetrics, histogram_views
from horde.oai.settings import FrontSettings
from horde.oai.waiter import CHAT_SIGNAL_CHANNEL_PREFIX, DELTA_STREAM_KEY_PREFIX

HORDE_URL: str = "http://horde.test/api"
"""The Horde API root the mocked routes live under."""

FRONT_URL: str = "http://front.test"
"""The base URL the test client addresses the front with."""

REQUEST_ID: str = "00000000-0000-4000-8000-000000000001"
"""The Horde request id every mocked submit returns."""

MODEL_NAME: str = "koboldcpp/LLaMA2-13B-Tiefighter"
"""The model every test request asks for."""

GENERATED_TEXT: str = "Hello from the horde."
"""The text the mocked worker generated."""

CHARGE_BASIS: str = "output_estimate"
"""The charge basis the mocked Horde reports; the front passes it through unchanged."""

SUBSCRIBE_TIMEOUT_SECONDS: float = 2.0
"""How long a test waits for the front's signal listener to subscribe before failing."""

CALL_WAIT_SECONDS: float = 2.0
"""How long a test waits for an expected Horde call or application exit."""

RAW_CLIENT_ADDRESS: tuple[str, int] = ("203.0.113.9", 50000)
"""The client address raw ASGI calls come from."""

FAST_TEST_SETTINGS: FrontSettings = FrontSettings(
    horde_urls=(HORDE_URL,),
    max_inflight=50,
    max_inflight_per_source=50,
    nonstream_timeout_seconds=5.0,
    stream_timeout_seconds=5.0,
    final_status_timeout_seconds=0.5,
    cancel_timeout_seconds=1.0,
    shutdown_cancel_seconds=1.0,
    heartbeat_seconds=0.05,
    fallback_poll_seconds=10.0,
    signal_recheck_seconds=0.1,
    fail_fast_grace_seconds=5.0,
    delta_pacing_max_window_seconds=0,
)
"""Settings with a fallback poll long enough that a test that finishes quickly proves another wake-up path. Delta
pacing is off, so a relayed stream sends one content chunk per delta. The pacing tests turn it on."""

UNRECORDED_METRICS: FrontMetrics = FrontMetrics(NoOpMeter(METER_NAME))
"""Instruments that record nothing, for tests building a client or watcher without a front."""


class MetricPoint:
    """Represents one data point read from the in-memory reader: its attributes and its value or count and sum."""

    def __init__(self, *, attributes: dict[str, Any], value: float, histogram_sum: float | None = None) -> None:
        """Create the point; ``value`` is a counter's sum, a gauge's value or a histogram's count."""
        self.attributes: dict[str, Any] = attributes
        """The point's attributes."""
        self.value: float = value
        """The counter sum, gauge value or histogram count."""
        self.histogram_sum: float | None = histogram_sum
        """The histogram's sum of recorded values; None for counters and gauges."""


def metric_points(metric_reader: InMemoryMetricReader, metric_name: str) -> list[MetricPoint]:
    """Return the current data points of ``metric_name``, collected from ``metric_reader``."""
    metrics_data = metric_reader.get_metrics_data()
    if metrics_data is None:
        return []
    points: list[MetricPoint] = []
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name != metric_name:
                    continue
                if isinstance(metric.data, Histogram):
                    points.extend(
                        MetricPoint(attributes=dict(point.attributes or {}), value=point.count, histogram_sum=point.sum)
                        for point in metric.data.data_points
                    )
                elif isinstance(metric.data, Sum | Gauge):
                    points.extend(
                        MetricPoint(attributes=dict(point.attributes or {}), value=point.value) for point in metric.data.data_points
                    )
    return points


def metric_total(metric_reader: InMemoryMetricReader, metric_name: str, **attributes: Any) -> float:
    """Return the summed value (or histogram count) of the points of ``metric_name`` whose attributes include ``attributes``.

    Attribute keys are given with ``horde_`` for the ``horde.`` prefix, such as ``horde_call_type="status"``.
    """
    wanted = {attribute_key.replace("horde_", "horde.", 1): attribute_value for attribute_key, attribute_value in attributes.items()}
    return sum(
        point.value
        for point in metric_points(metric_reader, metric_name)
        if all(point.attributes.get(attribute_key) == attribute_value for attribute_key, attribute_value in wanted.items())
    )


def build_chat_body(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid chat request body with ``overrides`` applied."""
    chat_body: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Say hello."},
        ],
        "max_tokens": 64,
    }
    chat_body.update(overrides)
    return chat_body


def waiting_status(
    *,
    queue_position: int = 3,
    wait_time: int = 20,
    is_possible: bool = True,
    processing: int = 0,
    eligible_worker_threads: int | None = None,
) -> dict[str, Any]:
    """Return the native status of a request still waiting for a worker.

    The eligibility fields follow ``is_possible`` (one worker with one thread, or none) unless
    ``eligible_worker_threads`` is given; the native status reports them on every read.
    """
    if eligible_worker_threads is None:
        eligible_worker_threads = 1 if is_possible else 0
    return {
        "done": False,
        "faulted": False,
        "finished": 0,
        "processing": processing,
        "waiting": 1 - processing,
        "restarted": 0,
        "queue_position": queue_position,
        "wait_time": wait_time,
        "kudos": 0,
        "is_possible": is_possible,
        "eligible_workers": min(eligible_worker_threads, 1),
        "eligible_worker_threads": eligible_worker_threads,
        "might_stall": False,
        "generations": [],
    }


def legacy_waiting_status(**waiting_overrides: Any) -> dict[str, Any]:
    """Return the native status of a waiting request from a Horde that does not report the eligibility fields."""
    status = waiting_status(**waiting_overrides)
    for eligibility_key in ("eligible_workers", "eligible_worker_threads", "might_stall"):
        del status[eligibility_key]
    return status


def done_generation(
    *,
    content: str = GENERATED_TEXT,
    finish_reason: str | None = "length",
    native_finish_reason: str | None = "length",
    prompt_tokens: int | None = 21,
    completion_tokens: int | None = 6,
    worker_id: str = "worker-1",
    model: str = MODEL_NAME,
    reasoning_content: str | None = None,
    reasoning_tokens: int | None = None,
) -> dict[str, Any]:
    """Return one finished generation in the native chat status shape; the message carries ``reasoning_content``
    only when it is set, as the native status does."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return {
        "text": content,
        "message": message,
        "model": model,
        "worker_id": worker_id,
        "worker_name": "Test Worker",
        "state": "ok",
        "seed": 7,
        "finish_reason": finish_reason,
        "native_finish_reason": native_finish_reason,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "charge_basis": CHARGE_BASIS,
        "queue_wait_seconds": 1.5,
    }


def done_status(**generation_overrides: Any) -> dict[str, Any]:
    """Return the native status of a request whose one generation is finished."""
    return {
        "done": True,
        "faulted": False,
        "finished": 1,
        "processing": 0,
        "waiting": 0,
        "restarted": 0,
        "queue_position": 0,
        "wait_time": 0,
        "kudos": 12.5,
        "is_possible": True,
        "generations": [done_generation(**generation_overrides)],
    }


class FakeHorde:
    """A mocked Horde whose status for the one request a test changes as it goes."""

    def __init__(
        self,
        router: respx.MockRouter,
        redis_client: fakeredis.FakeAsyncRedis,
        stream_redis_client: fakeredis.FakeAsyncRedis | None = None,
    ) -> None:
        """Mock the Horde routes on ``router``; signals are published on ``redis_client`` and deltas appended on
        ``stream_redis_client``."""
        self.stream_redis_client: fakeredis.FakeAsyncRedis | None = stream_redis_client
        """The database the request's delta stream is appended to, or None when the front does not read deltas."""
        self.status_payload: dict[str, Any] = waiting_status()
        """The body the status route returns."""
        self.next_status_payloads: list[dict[str, Any]] = []
        """Bodies the next status reads return in order; each becomes ``status_payload`` when read."""
        self.status_code: int = 200
        """The status code the status route returns."""
        self._redis_client = redis_client
        status_path = f"{HORDE_URL}/v2/generate/chat/status/{REQUEST_ID}"
        self.submit_route: respx.Route = router.post(f"{HORDE_URL}/v2/generate/chat/async").mock(
            return_value=httpx.Response(202, json={"id": REQUEST_ID, "kudos": 12.5}),
        )
        """The submit route."""
        self.status_route: respx.Route = router.get(status_path).mock(side_effect=self._answer_status)
        """The status route."""
        self.cancel_gate: asyncio.Event | None = None
        """When set, the cancel route answers only once this event is set."""
        self.cancels_received: int = 0
        """How many cancels reached the cancel route, answered or not."""
        self.on_cancel: Callable[[], None] | None = None
        """Called when a cancel arrives, before it is answered."""
        self.cancel_route: respx.Route = router.delete(status_path).mock(side_effect=self._answer_cancel)
        """The cancel route."""
        self.models_route: respx.Route = router.get(f"{HORDE_URL}/v2/status/models").mock(
            return_value=httpx.Response(200, json=[]),
        )
        """The model list route."""

    def _answer_status(self, _request: httpx.Request) -> httpx.Response:
        """Return the current status response, after taking the next of ``next_status_payloads`` when one is left."""
        if self.next_status_payloads:
            self.status_payload = self.next_status_payloads.pop(0)
        return httpx.Response(self.status_code, json=self.status_payload)

    async def _answer_cancel(self, request: httpx.Request) -> httpx.Response:
        """Return the current status response, after ``cancel_gate`` opens when one is set."""
        self.cancels_received += 1
        if self.on_cancel is not None:
            self.on_cancel()
        if self.cancel_gate is not None:
            await self.cancel_gate.wait()
        return self._answer_status(request)

    def submitted_body(self) -> dict[str, Any]:
        """Return the JSON body of the last submit."""
        submitted: dict[str, Any] = json.loads(self.submit_route.calls.last.request.content)
        return submitted

    async def publish(self, event: str, **fault_details: str) -> None:
        """Publish a completion signal for the request, in the native body shape, with any ``fault_reason`` and
        ``fault_rc`` a faulted event carries."""
        await self._redis_client.publish(
            f"{CHAT_SIGNAL_CHANNEL_PREFIX}{REQUEST_ID}",
            json.dumps({"id": REQUEST_ID, "event": event, "generations": 1, **fault_details}),
        )

    async def append_delta(
        self,
        generation_id: str,
        seq: int,
        content: str,
        *,
        worker_id: str | None = None,
        model: str | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        """Append a content delta of ``generation_id`` to the request's delta stream, as the progress route does.

        ``worker_id`` and ``model`` are the lease fields the append copies into the entry; None leaves the field out, as
        an entry from a Horde that does not write it. ``reasoning_content`` is written only when set, as the append
        script writes it.
        """
        fields = {"gen": generation_id, "seq": str(seq), "content": content}
        if reasoning_content is not None:
            fields["reasoning_content"] = reasoning_content
        if worker_id is not None:
            fields["worker"] = worker_id
        if model is not None:
            fields["model"] = model
        await self._append_stream_entry(fields)

    async def end_generation(self, generation_id: str) -> None:
        """Append the marker that ``generation_id`` faulted, as the Horde does when it restarts the request."""
        await self._append_stream_entry({"gen": generation_id, "ended": "1"})

    async def delete_stream(self) -> None:
        """Delete the request's delta stream, as its expiry or a Redis failover that lost the key does."""
        assert self.stream_redis_client is not None, "The front was started without a delta stream database."
        await self.stream_redis_client.delete(f"{DELTA_STREAM_KEY_PREFIX}{REQUEST_ID}")

    async def expire_stream(self, milliseconds: int) -> None:
        """Set the request's delta stream to expire after ``milliseconds``."""
        assert self.stream_redis_client is not None, "The front was started without a delta stream database."
        await self.stream_redis_client.pexpire(f"{DELTA_STREAM_KEY_PREFIX}{REQUEST_ID}", milliseconds)

    async def append_entry_with_id(self, entry_id: str, fields: dict[str, str]) -> None:
        """Append ``fields`` to the request's delta stream under the explicit ``entry_id``, as a stream recreated after
        a failover may hold ids below those already read."""
        assert self.stream_redis_client is not None, "The front was started without a delta stream database."
        await self.stream_redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}{REQUEST_ID}", fields, id=entry_id)

    async def _append_stream_entry(self, fields: dict[str, str]) -> None:
        """Append ``fields`` as one entry of the request's delta stream."""
        assert self.stream_redis_client is not None, "The front was started without a delta stream database."
        await self.stream_redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}{REQUEST_ID}", fields)

    async def finish_later(self, delay_seconds: float, *, signal: bool = True, **generation_overrides: Any) -> None:
        """Mark the request done after ``delay_seconds`` and publish ``done`` when ``signal`` is set."""
        await asyncio.sleep(delay_seconds)
        self.status_payload = done_status(**generation_overrides)
        if signal:
            await self.publish("done")


class RunningFront:
    """A started front with its test client and mocked Horde."""

    def __init__(self, *, app: FastAPI, client: httpx.AsyncClient, horde: FakeHorde, metric_reader: InMemoryMetricReader) -> None:
        """Bundle the running pieces."""
        self.app: FastAPI = app
        """The front application."""
        self.client: httpx.AsyncClient = client
        """A client addressing the front through ASGI."""
        self.horde: FakeHorde = horde
        """The mocked Horde."""
        self.metric_reader: InMemoryMetricReader = metric_reader
        """The reader of the front's metrics."""

    def metric_total(self, metric_name: str, **attributes: Any) -> float:
        """Return the summed value of ``metric_name``'s points matching ``attributes``; see the module function."""
        return metric_total(self.metric_reader, metric_name, **attributes)

    @property
    def inflight(self) -> int:
        """Return the front's open request count."""
        inflight: int = self.app.state.front.inflight_limiter.inflight
        return inflight


StartFront = Callable[..., AbstractAsyncContextManager[RunningFront]]
"""Start a front with optional settings, Redis and delta stream overrides; see the ``start_front`` fixture."""


@pytest_asyncio.fixture
async def start_front() -> AsyncIterator[StartFront]:
    """Return a context manager factory that runs a front against a mocked Horde for one test."""

    @asynccontextmanager
    async def run_front(
        settings: FrontSettings = FAST_TEST_SETTINGS,
        *,
        with_redis: bool = True,
        with_deltas: bool = False,
    ) -> AsyncIterator[RunningFront]:
        redis_client = fakeredis.FakeAsyncRedis()
        stream_redis_client = fakeredis.FakeAsyncRedis() if with_deltas else None
        metric_reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[metric_reader], views=histogram_views())
        with respx.mock(assert_all_called=False) as router:
            horde = FakeHorde(router, redis_client, stream_redis_client)
            async with httpx.AsyncClient() as horde_http_client:
                app = create_app(
                    settings,
                    http_client=horde_http_client,
                    cancel_http_client=horde_http_client,
                    redis_client=redis_client if with_redis else None,
                    stream_redis_client=stream_redis_client,
                    meter=meter_provider.get_meter(METER_NAME),
                )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url=FRONT_URL) as front_client:
                    async with app.router.lifespan_context(app):
                        if with_redis:
                            assert await app.state.front.listener.wait_until_subscribed(SUBSCRIBE_TIMEOUT_SECONDS)
                        yield RunningFront(app=app, client=front_client, horde=horde, metric_reader=metric_reader)
        await redis_client.close()
        if stream_redis_client is not None:
            await stream_redis_client.close()

    yield run_front


async def wait_for_call(call_count: Callable[[], int], *, minimum: int = 1) -> None:
    """Return once ``call_count()`` reaches ``minimum``, failing after ``CALL_WAIT_SECONDS``."""
    deadline = time.monotonic() + CALL_WAIT_SECONDS
    while call_count() < minimum:
        assert time.monotonic() < deadline, "The expected Horde call did not happen."
        await asyncio.sleep(0.01)


async def call_raw(
    app: FastAPI,
    *,
    body_chunks: Sequence[bytes],
    chunk_delay_seconds: float = 0,
    disconnect_after_seconds: float | None = None,
    response_start_delay_seconds: float = 0,
    headers: Sequence[tuple[bytes, bytes]] = (),
    wait_for_app: bool = True,
) -> tuple[asyncio.Task[None], list[dict[str, Any]]]:
    """Send ``POST /v1/chat/completions`` over raw ASGI.

    Args:
        app: The front application.
        body_chunks: The body, sent as these chunks.
        chunk_delay_seconds: The pause before each chunk.
        disconnect_after_seconds: When set, the client disconnects this long after the call starts.
        response_start_delay_seconds: How long sending the response start takes, which holds a streamed response
            before its event generator first runs.
        headers: Extra request headers.
        wait_for_app: Whether to wait for the application to finish handling the request before returning.

    Returns:
        The task running the application and the ASGI messages it sent.
    """
    pending_chunks = list(body_chunks)
    disconnected = asyncio.Event()
    sent_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if pending_chunks:
            await asyncio.sleep(chunk_delay_seconds)
            body_chunk = pending_chunks.pop(0)
            return {"type": "http.request", "body": body_chunk, "more_body": bool(pending_chunks)}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent_messages.append(dict(message))
        if message["type"] == "http.response.start" and response_start_delay_seconds > 0:
            await asyncio.sleep(response_start_delay_seconds)

    scope = raw_chat_scope(headers=headers)
    app_task = asyncio.create_task(app(scope, receive, send))
    if disconnect_after_seconds is not None:
        await asyncio.sleep(disconnect_after_seconds)
        disconnected.set()
    if wait_for_app:
        await asyncio.wait_for(app_task, timeout=CALL_WAIT_SECONDS)
    return app_task, sent_messages


def raw_chat_scope(*, headers: Sequence[tuple[bytes, bytes]] = ()) -> dict[str, Any]:
    """Return the raw ASGI scope of ``POST /v1/chat/completions`` from ``RAW_CLIENT_ADDRESS`` with extra ``headers``."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"host", b"front.test"), *headers],
        "client": RAW_CLIENT_ADDRESS,
        "server": ("front.test", 80),
        "state": {},
    }


def response_status(sent_messages: Sequence[dict[str, Any]]) -> int | None:
    """Return the status of a raw ASGI response, or None when no response started."""
    for message in sent_messages:
        if message["type"] == "http.response.start":
            status: int = message["status"]
            return status
    return None


def response_body(sent_messages: Sequence[dict[str, Any]]) -> bytes:
    """Return the body of a raw ASGI response."""
    return b"".join(message.get("body", b"") for message in sent_messages if message["type"] == "http.response.body")
