# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of relaying a worker's content deltas to a streamed request: which requests relay, how the delta stream
is read, the chunk order, the pacing of each post's text, the reconciliation with the final reply and the errors after
a delta was sent."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from types import SimpleNamespace
from typing import Any, cast

import fakeredis
import httpx
import pytest
import redis.exceptions
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sse_starlette import ServerSentEvent

from horde.oai import waiter
from horde.oai.app import (
    STREAM_DIVERGED_CODE,
    AcceptedDelta,
    DeltaPacer,
    DeltaRelay,
    DeltaText,
    PacedPostTracker,
    RequestTrace,
    _create_stream_redis_client,
    _is_visible_character_boundary,
    _relay_deltas,
    _split_evenly,
)
from horde.oai.horde_client import HordeChatStatus
from horde.oai.mapping import build_system_fingerprint, relays_deltas
from horde.oai.metrics import (
    FIRST_CONTENT_NAME,
    METER_NAME,
    PACING_FLUSHES_NAME,
    PACING_LAG_NAME,
    PIECES_PER_POST_NAME,
    POST_GAP_NAME,
    RELAY_OUTCOMES_NAME,
    RELAYED_NAME,
    FrontMetrics,
    histogram_views,
)
from horde.oai.schema import ChatCompletionRequest
from horde.oai.waiter import DELTA_STREAM_KEY_PREFIX, DeltaEntry, DeltaStreamReader, DeltaSubscription
from tests.unit.oai.conftest import (
    FAST_TEST_SETTINGS,
    GENERATED_TEXT,
    MODEL_NAME,
    REQUEST_ID,
    UNRECORDED_METRICS,
    RunningFront,
    StartFront,
    build_chat_body,
    done_status,
    metric_points,
    metric_total,
    waiting_status,
)
from tests.unit.oai.test_chat_front import COMPLETION_ID, data_payloads, parse_sse_events

pytestmark = pytest.mark.asyncio

GENERATION_ID: str = "gen-1"
"""The generation whose deltas a test relays."""

REPLACEMENT_GENERATION_ID: str = "gen-2"
"""The generation of a restarted attempt."""

RELAY_SETTLE_SECONDS: float = 0.5
"""How long a test waits after appending deltas for the reader to read them and the stream to send them; twice the
reader's block, so a read in progress when they were appended has returned."""

FIRST_DELTA_DELAY_SECONDS: float = 0.1
"""How long after the request starts a test appends its first delta, so the stream has registered."""

TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}
"""A tool definition, whose presence keeps a streamed request from relaying deltas."""


def stream_chunks(response_text: str) -> tuple[list[dict[str, Any]], str]:
    """Return the JSON payloads of a stream's data events and its last data payload."""
    payloads = data_payloads(parse_sse_events(response_text))
    return [json.loads(payload) for payload in payloads[:-1]], payloads[-1]


def chunk_deltas(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the first choice's delta of each chunk that has a choice."""
    return [chunk["choices"][0]["delta"] for chunk in chunks if chunk.get("choices")]


async def stream_while(front: RunningFront, generate: Callable[[], Coroutine[Any, Any, None]], **body_overrides: Any) -> str:
    """Return the body of a streamed request sent while ``generate`` appends deltas and settles the request."""
    generator = asyncio.create_task(generate())
    response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=True, **body_overrides))
    await generator
    assert response.status_code == 200
    return response.text


async def finish(front: RunningFront, **generation_overrides: Any) -> None:
    """Mark the request done with one generation and publish ``done``."""
    front.horde.status_payload = done_status(**generation_overrides)
    await front.horde.publish("done")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"stream": True}, True),
        ({"stream": True, "response_format": {"type": "text"}}, True),
        ({"stream": False}, False),
        ({}, False),
        ({"stream": True, "tools": [TOOL]}, False),
        ({"stream": True, "response_format": {"type": "json_object"}}, False),
        (
            {
                "stream": True,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "reply",
                        "schema": {"type": "object"},
                    },
                },
            },
            False,
        ),
    ],
)
async def test_only_plain_text_streamed_requests_relay_deltas(overrides: dict[str, Any], expected: bool) -> None:
    """Tool calls and structured output are verified only once the reply is complete, so those requests stay whole."""
    chat_request = ChatCompletionRequest.model_validate(build_chat_body(**overrides))

    assert relays_deltas(chat_request) is expected


@pytest.mark.parametrize(
    ("with_deltas", "overrides", "forwards_stream"),
    [
        (True, {"stream": True, "stream_options": {"include_usage": True}}, True),
        (True, {"stream": True, "tools": [TOOL]}, False),
        (True, {"stream": True, "response_format": {"type": "json_object"}}, False),
        (True, {}, False),
        (False, {"stream": True}, False),
    ],
)
async def test_stream_is_forwarded_only_for_a_request_whose_deltas_are_relayed(
    start_front: StartFront,
    with_deltas: bool,
    overrides: dict[str, Any],
    forwards_stream: bool,
) -> None:
    async with start_front(with_deltas=with_deltas) as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**overrides))

    assert response.status_code == 200
    submitted = front.horde.submitted_body()
    assert submitted.get("stream") is (True if forwards_stream else None)
    assert "stream_options" not in submitted


async def test_reader_queues_each_registered_requests_entries_in_stream_order() -> None:
    """Each queued entry has the append time held in its stream id."""
    redis_client = fakeredis.FakeAsyncRedis()
    reader = DeltaStreamReader(redis_client)
    try:
        with reader.register("request-a") as first, reader.register("request-b") as second:
            assert first is not None
            assert second is not None
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"gen": "g1", "seq": "1", "content": "Hi"}, id="1000001000-0")
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"seq": "2", "content": "no generation"}, id="1000001500-0")
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"gen": "g1", "ended": "1"}, id="1000002250-0")
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-b", {"gen": "g2", "seq": "1", "content": "Yo"}, id="1000003000-0")
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-c", {"gen": "g3", "seq": "1", "content": "No"})

            await reader.read_once(redis_client)
            first_entries = first.take_queued()
            second_entries = second.take_queued()
            await reader.read_once(redis_client)
            entries_after_second_read = first.take_queued() + second.take_queued()
    finally:
        await redis_client.close()

    assert first_entries == [
        DeltaEntry.model_validate({"gen": "g1", "seq": 1, "content": "Hi", "appended_at": 1_000_001.0}),
        DeltaEntry.model_validate({"gen": "g1", "ended": True, "appended_at": 1_000_002.25}),
    ]
    assert second_entries == [DeltaEntry.model_validate({"gen": "g2", "seq": 1, "content": "Yo", "appended_at": 1_000_003.0})]
    assert entries_after_second_read == []


class RecordedSpan:
    """A stand-in for a Logfire span that keeps its name and the attributes set on it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}

    def __enter__(self) -> RecordedSpan:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


async def test_reader_records_one_span_per_read_that_returns_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The span counts every returned entry and its bytes, and the lag runs from the oldest entry's stream id."""
    spans: list[RecordedSpan] = []
    monkeypatch.setattr(waiter.logfire, "span", lambda name, **_attributes: spans.append(RecordedSpan(name)) or spans[-1])
    monkeypatch.setattr(waiter, "time", SimpleNamespace(time=lambda: 1_000_002.5))
    redis_client = fakeredis.FakeAsyncRedis()
    reader = DeltaStreamReader(redis_client)
    try:
        with reader.register("request-a"), reader.register("request-b"):
            await reader.read_once(redis_client)
            spans_after_empty_read = len(spans)
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"gen": "g1", "seq": "1", "content": "Hi"}, id="1000001000-0")
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-b", {"gen": "g2", "ended": "1"}, id="1000000000-0")
            await reader.read_once(redis_client)
    finally:
        await redis_client.close()

    assert spans_after_empty_read == 0
    (span,) = spans
    assert span.name == waiter.DELTA_READ_SPAN_NAME
    assert span.attributes == {
        waiter.DELTA_READ_ENTRY_COUNT_ATTRIBUTE: 2,
        waiter.DELTA_READ_BYTE_COUNT_ATTRIBUTE: sum(
            len(text) for text in ("gen", "g1", "seq", "1", "content", "Hi", "gen", "g2", "ended", "1")
        ),
        waiter.DELTA_READ_FIRST_ENTRY_LAG_ATTRIBUTE: pytest.approx(2.5),
    }


@pytest.mark.parametrize(
    ("fields", "worker_id", "model"),
    [
        ({"gen": "g1", "seq": "1", "content": "Hi", "worker": "worker-1", "model": MODEL_NAME}, "worker-1", MODEL_NAME),
        ({"gen": "g1", "seq": "1", "content": "Hi"}, None, None),
    ],
)
async def test_delta_entry_reads_the_worker_and_model_when_the_stream_carries_them(
    fields: dict[str, str],
    worker_id: str | None,
    model: str | None,
) -> None:
    """An entry from a Horde that does not copy the lease's worker and model reads with neither."""
    entry = DeltaEntry.model_validate(fields)

    assert (entry.worker_id, entry.model) == (worker_id, model)


async def test_reader_without_redis_registers_nothing() -> None:
    reader = DeltaStreamReader(None)
    await reader.start()

    with reader.register("request-a") as subscription:
        assert subscription is None
    assert reader.enabled is False


@pytest.mark.parametrize("include_usage", [False, True])
async def test_relayed_deltas_arrive_as_content_chunks_before_the_finish(start_front: StartFront, include_usage: bool) -> None:
    """A final reply equal to the relayed text does not add a content chunk; the finish and usage chunks close the stream."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await front.horde.append_delta(GENERATION_ID, 2, "from the horde.")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate, stream_options={"include_usage": include_usage})
        relayed = front.metric_total(RELAYED_NAME)
        equal_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="equal")
        relayed_first_content = front.metric_total(FIRST_CONTENT_NAME, horde_relayed=True)

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert all(chunk["id"] == COMPLETION_ID for chunk in chunks)
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]
    finish_chunk = next(chunk for chunk in chunks if chunk.get("x_horde"))
    assert finish_chunk["choices"][0]["finish_reason"] == "length"
    if include_usage:
        assert chunks[-1]["choices"] == []
        assert chunks[-1]["usage"]["total_tokens"] == 27
        assert all(chunk["usage"] is None for chunk in chunks[:-1])
    else:
        assert all("usage" not in chunk for chunk in chunks)
    assert relayed == 1
    assert equal_outcomes == 1
    assert relayed_first_content == 1


async def test_a_delta_appended_before_the_stream_opens_is_relayed_first(start_front: StartFront) -> None:
    """The first status read precedes the response; a ``fail_fast`` request without a queue position reads again
    before it is admitted, and the delta the worker appended during those reads is the stream's first content."""
    async with start_front(with_deltas=True) as front:
        first_delta_appended = False

        async def answer_with_a_delta_first(_request: httpx.Request) -> httpx.Response:
            nonlocal first_delta_appended
            if not first_delta_appended:
                first_delta_appended = True
                await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
                return httpx.Response(200, json=waiting_status(queue_position=0, wait_time=0))
            return httpx.Response(200, json=front.horde.status_payload)

        front.horde.status_route.mock(side_effect=answer_with_a_delta_first)
        front.horde.status_payload = waiting_status(processing=1)

        async def generate() -> None:
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 2, "from the horde.")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate, horde={"availability_mode": "fail_fast"})

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]


@pytest.mark.parametrize(
    ("final_worker_id", "final_model"),
    [
        ("worker-1", MODEL_NAME),
        ("worker-2", "koboldcpp/Other-Model"),
    ],
)
async def test_every_chunk_of_a_relayed_stream_carries_the_relayed_generations_fingerprint(
    start_front: StartFront,
    final_worker_id: str,
    final_model: str,
) -> None:
    """The fingerprint comes from the worker and model the first delta carries, and the final chunks keep it even when
    the final reply carries another, since the client holds content from the relayed generation."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ", worker_id="worker-1", model=MODEL_NAME)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front, worker_id=final_worker_id, model=final_model)

        body = await stream_while(front, generate, stream_options={"include_usage": True})

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks)[:2] == [{"role": "assistant"}, {"content": "Hello "}]
    finish_chunk = next(chunk for chunk in chunks if chunk.get("x_horde"))
    expected = build_system_fingerprint("worker-1", MODEL_NAME)
    assert expected is not None
    assert finish_chunk["system_fingerprint"] == expected
    assert {chunk["system_fingerprint"] for chunk in chunks} == {expected}


async def test_relayed_chunks_of_a_request_listing_models_carry_the_model_its_first_delta_reports(start_front: StartFront) -> None:
    """Any listed model may serve the request, so the relayed chunks carry the model of the pinned generation rather
    than the first one listed."""
    served_model = "koboldcpp/Other-Model"
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ", worker_id="worker-1", model=served_model)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front, worker_id="worker-1", model=served_model)

        body = await stream_while(front, generate, model=None, models=[MODEL_NAME, served_model])

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks)[:2] == [{"role": "assistant"}, {"content": "Hello "}]
    assert {chunk["model"] for chunk in chunks} == {served_model}


async def test_relayed_chunks_without_a_worker_carry_no_fingerprint(start_front: StartFront) -> None:
    """Entries from a Horde that does not write the worker leave the relayed chunks without a fingerprint."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)

    chunks, _ = stream_chunks(body)
    relayed_chunks = chunks[:2]
    assert chunk_deltas(relayed_chunks) == [{"role": "assistant"}, {"content": "Hello "}]
    assert [chunk["system_fingerprint"] for chunk in relayed_chunks] == [None, None]


async def test_final_reply_extending_the_relayed_text_sends_the_rest_as_one_chunk(start_front: StartFront) -> None:
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        prefix_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="prefix")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]
    assert "".join(delta.get("content", "") for delta in chunk_deltas(chunks)) == GENERATED_TEXT
    assert prefix_outcomes == 1


async def test_final_reply_not_continuing_the_relayed_text_ends_with_an_error_event(start_front: StartFront) -> None:
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Goodbye")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        mismatch_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="mismatch")
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks[:-1]) == [{"role": "assistant"}, {"content": "Goodbye"}]
    assert chunks[-1]["error"]["code"] == STREAM_DIVERGED_CODE
    assert chunks[-1]["error"]["metadata"]["request_id"] is not None
    assert mismatch_outcomes == 1
    assert cancels == 0


@pytest.mark.parametrize(
    ("outcome", "break_relay"),
    [
        (
            "replaced_attempt",
            lambda front: front.horde.append_delta(REPLACEMENT_GENERATION_ID, 1, "Another start"),
        ),
        (
            "ended",
            lambda front: front.horde.end_generation(GENERATION_ID),
        ),
        (
            "gap",
            lambda front: front.horde.append_delta(GENERATION_ID, 3, "skipped ahead"),
        ),
    ],
)
async def test_attempt_lost_after_a_delta_was_sent_ends_with_an_error_event_and_a_cancel(
    start_front: StartFront,
    outcome: str,
    break_relay: Callable[[RunningFront], Awaitable[None]],
) -> None:
    """A restarted attempt is never spliced into a choice the client already holds part of."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await break_relay(front)

        body = await stream_while(front, generate)
        outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome=outcome)
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks[:-1]) == [{"role": "assistant"}, {"content": "Hello "}]
    assert chunks[-1]["error"]["code"] == STREAM_DIVERGED_CODE
    assert outcomes == 1
    assert cancels == 1


async def test_fault_after_a_delta_was_sent_ends_with_the_fault_error_event(start_front: StartFront) -> None:
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            front.horde.status_payload = {**waiting_status(), "faulted": True}
            await front.horde.publish("faulted")

        body = await stream_while(front, generate)

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks[:-1]) == [{"role": "assistant"}, {"content": "Hello "}]
    assert chunks[-1]["error"]["code"] == "ChatRequestFaulted"


async def test_attempt_that_ended_before_any_delta_was_sent_leaves_the_buffered_stream(start_front: StartFront) -> None:
    """A reader that reads a delta together with its generation's ended marker sends neither."""
    async with start_front(with_deltas=True) as front:
        await front.horde.append_delta(GENERATION_ID, 1, "Stale start")
        await front.horde.end_generation(GENERATION_ID)

        async def generate() -> None:
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        relayed = front.metric_total(RELAYED_NAME)
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [{"role": "assistant"}, {"content": GENERATED_TEXT}, {}]
    assert relayed == 0
    assert cancels == 0


async def test_streamed_request_without_redis_is_delivered_whole(start_front: StartFront) -> None:
    async with start_front(with_redis=False) as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=True))
        relayed = front.metric_total(RELAYED_NAME)

    chunks, last_payload = stream_chunks(response.text)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [{"role": "assistant"}, {"content": GENERATED_TEXT}, {}]
    assert relayed == 0


async def test_stream_client_selects_the_stream_database_whatever_the_url_selects() -> None:
    """Pub/sub ignores the database, so ``redis_url`` may select any; the delta streams live in one."""
    stream_client = _create_stream_redis_client("redis://:secret@redis.test:6380/0", db=7)
    connection_kwargs = dict(stream_client.connection_pool.connection_kwargs)
    await stream_client.close(close_connection_pool=True)

    assert connection_kwargs["db"] == 7
    assert connection_kwargs["host"] == "redis.test"
    assert connection_kwargs["port"] == 6380
    assert connection_kwargs["password"] == "secret"


REASONING_TEXT: str = "The user wants a greeting."
"""The reasoning the worker posts before the reply's text and submits with it."""


async def test_reasoning_deltas_are_relayed_before_the_text_when_the_client_asks_for_them(start_front: StartFront) -> None:
    """The reasoning is not part of the reconciliation: the final reply equals the relayed text."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "", reasoning_content="The user wants ")
            await front.horde.append_delta(GENERATION_ID, 2, "", reasoning_content="a greeting.")
            await front.horde.append_delta(GENERATION_ID, 3, GENERATED_TEXT)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front, reasoning_content=REASONING_TEXT)

        body = await stream_while(front, generate, horde={"include_reasoning": True})
        equal_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="equal")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"reasoning_content": "The user wants "},
        {"reasoning_content": "a greeting."},
        {"content": GENERATED_TEXT},
        {},
    ]
    assert equal_outcomes == 1


async def test_reasoning_deltas_are_dropped_and_the_text_still_relayed_when_the_client_did_not_ask(start_front: StartFront) -> None:
    """A delta carrying only reasoning advances the sequence without sending anything, so the text that follows it is
    relayed as the stream's first delta."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "", reasoning_content=REASONING_TEXT)
            await front.horde.append_delta(GENERATION_ID, 2, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front, reasoning_content=REASONING_TEXT)

        body = await stream_while(front, generate)
        relayed = front.metric_total(RELAYED_NAME)
        prefix_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="prefix")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]
    assert relayed == 1
    assert prefix_outcomes == 1


async def test_the_final_reply_sends_the_reasoning_the_deltas_did_not_cover(start_front: StartFront) -> None:
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "", reasoning_content="The user wants ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front, reasoning_content=REASONING_TEXT)

        body = await stream_while(front, generate, horde={"include_reasoning": True})

    chunks, _ = stream_chunks(body)
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"reasoning_content": "The user wants "},
        {"reasoning_content": "a greeting."},
        {"content": GENERATED_TEXT},
        {},
    ]


async def test_a_pinned_attempt_that_ended_before_sending_anything_leaves_the_next_attempt_relayable(start_front: StartFront) -> None:
    """An attempt whose dropped reasoning was all it posted before it faulted does not hold the relay."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "", reasoning_content=REASONING_TEXT)
            await front.horde.end_generation(GENERATION_ID)
            await front.horde.append_delta(REPLACEMENT_GENERATION_ID, 1, GENERATED_TEXT)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        relayed = front.metric_total(RELAYED_NAME)
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [{"role": "assistant"}, {"content": GENERATED_TEXT}, {}]
    assert relayed == 1
    assert cancels == 0


LOWER_ENTRY_ID: str = "1-1"
"""An entry id below every id the stream assigned earlier, as a stream recreated after a failover that lost the key
may assign."""

READER_RECONNECT_SECONDS: float = 0.3
"""How long a test's reader waits after a failed read before reading again."""


@pytest.mark.parametrize("lose_stream", ["delete", "expire"])
async def test_stream_lost_before_any_delta_was_sent_delivers_the_request_whole(start_front: StartFront, lose_stream: str) -> None:
    """A stream key that is deleted or expires produces no entries and no error, so the request waits for its status
    and is delivered as a request that relayed nothing, without a relay outcome."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "", reasoning_content=REASONING_TEXT)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            if lose_stream == "delete":
                await front.horde.delete_stream()
            else:
                await front.horde.expire_stream(1)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        relayed = front.metric_total(RELAYED_NAME)
        relay_outcomes = front.metric_total(RELAY_OUTCOMES_NAME)
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [{"role": "assistant"}, {"content": GENERATED_TEXT}, {}]
    assert relayed == 0
    assert relay_outcomes == 0
    assert cancels == 0


async def test_stream_deleted_after_a_delta_was_sent_finishes_with_the_rest_of_a_continuing_reply(start_front: StartFront) -> None:
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.delete_stream()
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        prefix_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="prefix")
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]
    assert prefix_outcomes == 1
    assert cancels == 0


async def test_stream_deleted_after_a_delta_was_sent_ends_with_an_error_event_when_the_reply_does_not_continue_it(
    start_front: StartFront,
) -> None:
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Goodbye")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.delete_stream()
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        mismatch_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="mismatch")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks[:-1]) == [{"role": "assistant"}, {"content": "Goodbye"}]
    assert chunks[-1]["error"]["code"] == STREAM_DIVERGED_CODE
    assert mismatch_outcomes == 1


class FailingStreamRead:
    """Wrap a stream client's ``xread`` so the read after ``fail_next`` is set raises a connection error once."""

    def __init__(self, stream_redis_client: fakeredis.FakeAsyncRedis) -> None:
        """Wrap the ``xread`` of ``stream_redis_client``."""
        self.fail_next: asyncio.Event = asyncio.Event()
        """Set to make the next read fail."""
        self.failed: asyncio.Event = asyncio.Event()
        """Set once a read failed."""
        self._xread = stream_redis_client.xread

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Return the wrapped read's reply, or raise when ``fail_next`` is set."""
        if self.fail_next.is_set():
            self.fail_next.clear()
            self.failed.set()
            raise redis.exceptions.ConnectionError("The connection to the stream database was lost.")
        return await self._xread(*args, **kwargs)


async def test_relay_resumes_after_a_failed_read_with_the_entries_appended_meanwhile(
    start_front: StartFront,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream retains its entries, so the read after the reconnect starts after the last entry read and the
    entries appended while the reader waited arrive in order."""
    monkeypatch.setattr(waiter, "LISTENER_RECONNECT_DELAY_SECONDS", READER_RECONNECT_SECONDS)
    async with start_front(with_deltas=True) as front:
        assert front.horde.stream_redis_client is not None
        failing_read = FailingStreamRead(front.horde.stream_redis_client)
        monkeypatch.setattr(front.horde.stream_redis_client, "xread", failing_read)

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            failing_read.fail_next.set()
            await asyncio.wait_for(failing_read.failed.wait(), RELAY_SETTLE_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 2, "from ")
            await asyncio.sleep(READER_RECONNECT_SECONDS + RELAY_SETTLE_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 3, "the horde.")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        equal_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="equal")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from "},
        {"content": "the horde."},
        {},
    ]
    assert equal_outcomes == 1


async def test_request_finishing_while_the_reader_reconnects_reconciles_the_unread_entries(
    start_front: StartFront,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entries appended after the failed read are sent by the final reply's reconciliation."""
    monkeypatch.setattr(waiter, "LISTENER_RECONNECT_DELAY_SECONDS", READER_RECONNECT_SECONDS * 10)
    async with start_front(with_deltas=True) as front:
        assert front.horde.stream_redis_client is not None
        failing_read = FailingStreamRead(front.horde.stream_redis_client)
        monkeypatch.setattr(front.horde.stream_redis_client, "xread", failing_read)

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            failing_read.fail_next.set()
            await asyncio.wait_for(failing_read.failed.wait(), RELAY_SETTLE_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 2, "from the horde.")
            await finish(front)

        body = await stream_while(front, generate)
        prefix_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="prefix")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]
    assert prefix_outcomes == 1


async def test_stream_replaced_with_lower_ids_after_a_delta_was_sent_finishes_through_the_reconciliation(
    start_front: StartFront,
) -> None:
    """A read after the last entry id does not return a recreated stream's entries whose ids are below it, so the
    relay sends nothing more and the final reply sends the rest of the text as one chunk, without an error event or a
    cancel."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "Hello ")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.delete_stream()
            await front.horde.append_entry_with_id(LOWER_ENTRY_ID, {"gen": GENERATION_ID, "seq": "2", "content": "from the horde."})
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        prefix_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="prefix")
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        {"content": "from the horde."},
        {},
    ]
    assert all("error" not in chunk for chunk in chunks)
    assert prefix_outcomes == 1
    assert cancels == 0


async def test_stream_replaced_with_lower_ids_before_any_delta_was_sent_delivers_the_request_whole(
    start_front: StartFront,
) -> None:
    """The recreated stream's entry below the last id read is not read, and the later entry continues a generation
    whose first delta was not read, so it does not pin a generation and the request is delivered whole."""
    async with start_front(with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            await front.horde.append_delta(GENERATION_ID, 1, "", reasoning_content=REASONING_TEXT)
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.delete_stream()
            await front.horde.append_entry_with_id(
                LOWER_ENTRY_ID, {"gen": REPLACEMENT_GENERATION_ID, "seq": "1", "content": "Another start"}
            )
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.append_delta(REPLACEMENT_GENERATION_ID, 2, " continued")
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        relayed = front.metric_total(RELAYED_NAME)
        relay_outcomes = front.metric_total(RELAY_OUTCOMES_NAME)
        cancels = front.horde.cancels_received

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [{"role": "assistant"}, {"content": GENERATED_TEXT}, {}]
    assert relayed == 0
    assert relay_outcomes == 0
    assert cancels == 0


PACED_TEXT: str = "0123456789abcdefghijklmnopqrstuvwxyzABCD"
"""A post's text long enough that pacing splits it into many pieces."""

PACING_TEST_SETTINGS = FAST_TEST_SETTINGS.model_copy(
    update={
        "delta_pacing_max_window_seconds": 2.0,
        "delta_pacing_max_chunks_per_second": 20,
    },
)
"""The fast test settings with the default pacing settings."""

PACED_POST_GAP_MILLISECONDS: int = 1500
"""The gap in append time between the two posts written by a front-level test, so the second post is paced over 1.5 s."""


def accepted(content: str, appended_at: float | None, reasoning_content: str = "") -> AcceptedDelta:
    """Create an accepted delta of ``content`` appended at ``appended_at``."""
    return AcceptedDelta(content=content, reasoning_content=reasoning_content, appended_at=appended_at)


def drain_schedule(pacer: DeltaPacer) -> list[tuple[float, DeltaText]]:
    """Return each pending piece with its due time, taking each piece at exactly that time."""
    schedule: list[tuple[float, DeltaText]] = []
    while (due_at := pacer.next_due_at) is not None:
        schedule.extend((due_at, piece) for piece in pacer.take_due(due_at))
    return schedule


def piece_contents(pieces: Sequence[DeltaText]) -> list[str]:
    """Return the content of each piece."""
    return [piece.content for piece in pieces]


async def test_the_first_post_is_due_whole_when_it_arrives() -> None:
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20)

    pacer.add([accepted("Hello ", 100.0)], now=5.0)

    assert pacer.interval_seconds is None
    assert piece_contents(pacer.take_due(5.0)) == ["Hello "]
    assert pacer.next_due_at is None


async def test_a_later_post_is_spread_evenly_over_the_interval_since_the_previous_post() -> None:
    """A 1 s gap at 20 chunks a second splits a 40 character post into 20 pieces of 2, 50 ms apart."""
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20)
    pacer.add([accepted("Hello ", 100.0)], now=0.0)
    pacer.take_due(0.0)

    pacer.add([accepted(PACED_TEXT, 101.0)], now=10.0)
    schedule = drain_schedule(pacer)

    assert pacer.interval_seconds == pytest.approx(1.0)
    assert [due_at for due_at, _piece in schedule] == pytest.approx([10.0 + index * 0.05 for index in range(20)])
    assert [len(piece.content) for _due_at, piece in schedule] == [2] * 20
    assert "".join(piece.content for _due_at, piece in schedule) == PACED_TEXT


@pytest.mark.parametrize(
    ("max_chunks_per_second", "content", "expected_piece_lengths"),
    [
        (4, PACED_TEXT, [10, 10, 10, 10]),
        (20, "abc", [1, 1, 1]),
    ],
)
async def test_the_piece_count_is_capped_by_the_chunk_rate_and_the_text_length(
    max_chunks_per_second: int,
    content: str,
    expected_piece_lengths: list[int],
) -> None:
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=max_chunks_per_second)
    pacer.add([accepted("Hello ", 100.0), accepted(content, 101.0)], now=0.0)

    schedule = drain_schedule(pacer)

    assert [len(piece.content) for _due_at, piece in schedule] == [6, *expected_piece_lengths]
    assert schedule[-1][0] < 1.0


async def test_a_post_arriving_mid_drain_merges_with_the_pending_text_and_spreads_over_the_next_window() -> None:
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=10)
    pacer.add([accepted("Hi", 100.0), accepted("0123456789", 101.0)], now=0.0)
    sent_before_arrival = piece_contents(pacer.take_due(0.45))

    pacer.add([accepted("abcdefghij", 102.0)], now=0.5)
    schedule = drain_schedule(pacer)

    assert sent_before_arrival == ["Hi", "0", "1", "2", "3", "4"]
    assert schedule[0][0] == pytest.approx(0.5)
    assert schedule[-1][0] < 0.5 + 1.0
    assert len(schedule) == 10
    assert "".join(piece.content for _due_at, piece in schedule) == "56789abcdefghij"


async def test_the_interval_estimate_follows_the_cadence_within_its_bounds() -> None:
    """A slower post widens the window halfway. A stalled worker's gap is capped at the maximum window. A gap below the
    floor, such as one from a stream recreated with lower ids, is raised to the floor."""
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20)
    pacer.add([accepted("a", 100.0), accepted("b", 101.0)], now=0.0)
    interval_after_one_gap = pacer.interval_seconds
    pacer.add([accepted("c", 102.5)], now=0.0)
    interval_after_slower_post = pacer.interval_seconds

    stalled_pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20)
    stalled_pacer.add([accepted("a", 100.0), accepted("b", 160.0)], now=0.0)
    recreated_pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20)
    recreated_pacer.add([accepted("a", 100.0), accepted("b", 0.001)], now=0.0)

    assert interval_after_one_gap == pytest.approx(1.0)
    assert interval_after_slower_post == pytest.approx(1.25)
    assert stalled_pacer.interval_seconds == pytest.approx(2.0)
    assert recreated_pacer.interval_seconds == pytest.approx(0.05)


async def test_a_batch_without_new_text_leaves_the_pending_pieces_due_as_scheduled() -> None:
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=10)
    pacer.add([accepted("Hi", 100.0), accepted("0123456789", 101.0)], now=0.0)
    pacer.take_due(0.0)

    pacer.add([], now=0.5)
    schedule = drain_schedule(pacer)

    assert [due_at for due_at, _piece in schedule] == pytest.approx([index * 0.1 for index in range(1, 10)])
    assert "".join(piece.content for _due_at, piece in schedule) == "123456789"


async def test_posts_without_an_append_time_are_kept_whole_in_order_behind_pending_pieces() -> None:
    """Without two append times there is no interval estimate, so each post is due whole. Posts not yet taken stay
    scheduled in stream order."""
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20)

    pacer.add([accepted("Hello ", None)], now=0.0)
    pacer.add([accepted("from ", None)], now=1.0)
    pacer.add([accepted("the horde.", 101.0)], now=2.0)
    schedule = drain_schedule(pacer)

    assert [(due_at, piece.content) for due_at, piece in schedule] == [(0.0, "Hello "), (1.0, "from "), (2.0, "the horde.")]


async def test_the_post_tracker_counts_the_pieces_of_each_post_and_finishes_it_with_its_last_text() -> None:
    post_tracker = PacedPostTracker()
    post_tracker.add([accepted("abc", 100.0), accepted("de", 101.0, reasoning_content="r")])

    first_sent = post_tracker.record_sent(DeltaText(content="ab", reasoning_content="r"))
    second_sent = post_tracker.record_sent(DeltaText(content="cd"))
    pending_after_second = post_tracker.pending_post_count
    third_sent = post_tracker.record_sent(DeltaText(content="e"))

    assert first_sent == []
    assert [(post.appended_at, post.piece_count) for post in second_sent] == [(100.0, 2)]
    assert pending_after_second == 1
    assert [(post.appended_at, post.piece_count) for post in third_sent] == [(101.0, 3)]
    assert post_tracker.pending_post_count == 0


async def test_a_maximum_window_of_zero_sends_each_post_whole_when_it_arrives() -> None:
    pacer = DeltaPacer(max_window_seconds=0, max_chunks_per_second=20)

    pacer.add([accepted("Hello ", 100.0), accepted(PACED_TEXT, 101.0)], now=3.0)
    pacer.add([accepted("more", 102.0)], now=4.0)
    schedule = drain_schedule(pacer)

    assert pacer.interval_seconds is None
    assert [(due_at, piece.content) for due_at, piece in schedule] == [(3.0, "Hello "), (3.0, PACED_TEXT), (4.0, "more")]


async def test_reasoning_is_paced_with_the_text() -> None:
    pacer = DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=3)
    pacer.add([accepted("Hello ", 100.0), accepted("abcdef", 101.0, reasoning_content="thinking")], now=0.0)

    schedule = drain_schedule(pacer)
    paced_pieces = [piece for _due_at, piece in schedule][1:]

    assert piece_contents(paced_pieces) == ["ab", "cd", "ef"]
    assert [piece.reasoning_content for piece in paced_pieces] == ["th", "ink", "ing"]


@pytest.mark.parametrize(
    ("text", "allowed_cuts"),
    [
        pytest.param("abc", [1, 2], id="plain ASCII"),
        pytest.param("e\u0301te\u0301", [2, 3], id="accent"),
        pytest.param("\U0001f468\u200d\U0001f469\u200d\U0001f467!", [5], id="family ZWJ emoji"),
        pytest.param("\U0001f1fa\U0001f1f8\U0001f1ec\U0001f1e7", [2], id="flag pair"),
        pytest.param("\U0001f44d\U0001f3fdx", [2], id="skin-tone emoji"),
        pytest.param("1\ufe0f\u20e3x", [3], id="keycap"),
        pytest.param("\u0915\u093f\u0915", [2], id="Devanagari vowel sign"),
        pytest.param("\u2764\ufe0fx", [2], id="variation selector"),
    ],
)
async def test_a_cut_is_allowed_only_between_visible_characters(text: str, allowed_cuts: list[int]) -> None:
    cuts = [index for index in range(1, len(text)) if _is_visible_character_boundary(text, index)]

    assert cuts == allowed_cuts


async def test_pieces_are_cut_at_nearby_whitespace() -> None:
    assert _split_evenly("alpha beta gamma delta", 4) == ["alpha ", "beta ", "gamma ", "delta"]


async def test_pieces_without_whitespace_do_not_cut_inside_a_visible_character() -> None:
    accented_text = "e\u0301" * 6

    pieces = _split_evenly(accented_text, 4)

    assert "".join(pieces) == accented_text
    assert [len(piece) for piece in pieces] == [4, 2, 4, 2]


def build_relay() -> DeltaRelay:
    """Create a relay of a stream that has not sent anything."""
    return DeltaRelay(completion_id=COMPLETION_ID, created=0, model=MODEL_NAME, include_usage=False)


def delta_entry(seq: int, content: str, appended_at: float) -> DeltaEntry:
    """Create a delta of the test generation."""
    return DeltaEntry.model_validate({"gen": GENERATION_ID, "seq": seq, "content": content, "appended_at": appended_at})


def event_contents(relay_events: Sequence[ServerSentEvent]) -> list[str]:
    """Return the content of each content chunk among ``relay_events``."""
    deltas = [json.loads(str(relay_event.data))["choices"][0]["delta"] for relay_event in relay_events]
    return [delta["content"] for delta in deltas if "content" in delta]


RELAY_WALL_CLOCK_TIME: float = 102.5
"""The Unix time read by the relay generator under test when it sends a piece. It is 2.5 s after the first post's append
time."""


def recording_metrics(metric_reader: InMemoryMetricReader) -> FrontMetrics:
    """Create front instruments whose points ``metric_reader`` collects."""
    meter_provider = MeterProvider(metric_readers=[metric_reader], views=histogram_views())
    return FrontMetrics(meter_provider.get_meter(METER_NAME))


def histogram_counts_and_sums(metric_reader: InMemoryMetricReader, metric_name: str) -> tuple[float, float]:
    """Return the summed count and the summed value of the points of the histogram ``metric_name``."""
    points = metric_points(metric_reader, metric_name)
    return sum(point.value for point in points), sum(point.histogram_sum or 0 for point in points)


def start_relaying(
    relay: DeltaRelay,
    finished: asyncio.Event,
    front_metrics: FrontMetrics = UNRECORDED_METRICS,
) -> tuple[AsyncGenerator[ServerSentEvent, None], asyncio.Task[HordeChatStatus]]:
    """Return a relay generator and its wait task.

    The generator's subscription holds a first post and a 40 character post appended 1 s later. The wait ends when
    ``finished`` is set. The generator's Unix clock reads ``RELAY_WALL_CLOCK_TIME``.
    """
    subscription = DeltaSubscription(REQUEST_ID)
    subscription.deliver(delta_entry(1, "Hello ", 100.0))
    subscription.deliver(delta_entry(2, PACED_TEXT, 101.0))

    async def wait_until_finished() -> HordeChatStatus:
        await finished.wait()
        return cast(HordeChatStatus, None)

    wait_task = asyncio.create_task(wait_until_finished())
    request_trace = cast(RequestTrace, SimpleNamespace(record_first_content=lambda *, relayed: None))
    relay_events = _relay_deltas(
        relay,
        subscription,
        wait_task,
        request_trace,
        front_metrics,
        DeltaPacer(max_window_seconds=2.0, max_chunks_per_second=20),
        wall_clock=lambda: RELAY_WALL_CLOCK_TIME,
    )
    return cast(AsyncGenerator[ServerSentEvent, None], relay_events), wait_task


async def test_pending_pieces_are_sent_at_once_when_the_wait_ends() -> None:
    """The first post arrives whole and at once. After the wait ends, the rest of the paced text follows without
    waiting for its window."""
    relay = build_relay()
    finished = asyncio.Event()
    metric_reader = InMemoryMetricReader()
    relay_events, wait_task = start_relaying(relay, finished, recording_metrics(metric_reader))
    yielded_events: list[ServerSentEvent] = []
    started_at = time.monotonic()
    finished_at: float | None = None
    async for relay_event in relay_events:
        yielded_events.append(relay_event)
        if finished_at is None and len(event_contents(yielded_events)) == 3:
            finished.set()
            finished_at = time.monotonic()
    ended_at = time.monotonic()
    await wait_task

    contents = event_contents(yielded_events)
    assert json.loads(str(yielded_events[0].data))["choices"][0]["delta"] == {"role": "assistant"}
    assert contents[0] == "Hello "
    assert len(contents) == 21
    assert "".join(contents) == relay.relayed_content == f"Hello {PACED_TEXT}"
    assert finished_at is not None
    assert finished_at - started_at < 0.5
    assert ended_at - finished_at < 0.5
    assert histogram_counts_and_sums(metric_reader, POST_GAP_NAME) == pytest.approx((1, 1.0))
    assert histogram_counts_and_sums(metric_reader, PIECES_PER_POST_NAME) == (2, 21)
    assert histogram_counts_and_sums(metric_reader, PACING_LAG_NAME) == pytest.approx((2, 2.5 + 1.5))
    assert metric_total(metric_reader, PACING_FLUSHES_NAME, horde_reason="finished") == 1
    assert metric_total(metric_reader, PACING_FLUSHES_NAME) == 1


async def test_relayed_content_is_the_text_yielded_when_the_client_stops_mid_drain() -> None:
    relay = build_relay()
    finished = asyncio.Event()
    metric_reader = InMemoryMetricReader()
    relay_events, wait_task = start_relaying(relay, finished, recording_metrics(metric_reader))
    yielded_events: list[ServerSentEvent] = []
    async for relay_event in relay_events:
        yielded_events.append(relay_event)
        if len(event_contents(yielded_events)) == 4:
            break
    await relay_events.aclose()
    wait_task.cancel()

    assert "".join(event_contents(yielded_events)) == relay.relayed_content == f"Hello {PACED_TEXT[:6]}"
    assert histogram_counts_and_sums(metric_reader, PIECES_PER_POST_NAME) == (1, 1)
    assert metric_total(metric_reader, PACING_FLUSHES_NAME, horde_reason="disconnected") == 1
    assert metric_total(metric_reader, PACING_FLUSHES_NAME) == 1


async def test_a_paced_stream_sends_the_rest_at_the_finish_and_reconciles_equal(start_front: StartFront) -> None:
    """The second post, 1.5 s after the first by its stream id, is paced one character per piece. The finish arrives
    mid-window. The pending pieces precede the finish chunk, so the final reply does not add a content chunk."""
    async with start_front(PACING_TEST_SETTINGS, with_deltas=True) as front:

        async def generate() -> None:
            await asyncio.sleep(FIRST_DELTA_DELAY_SECONDS)
            first_appended_milliseconds = int(time.time() * 1000)
            await front.horde.append_entry_with_id(
                f"{first_appended_milliseconds}-0", {"gen": GENERATION_ID, "seq": "1", "content": "Hello "}
            )
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await front.horde.append_entry_with_id(
                f"{first_appended_milliseconds + PACED_POST_GAP_MILLISECONDS}-0",
                {"gen": GENERATION_ID, "seq": "2", "content": "from the horde."},
            )
            await asyncio.sleep(RELAY_SETTLE_SECONDS)
            await finish(front)

        body = await stream_while(front, generate)
        equal_outcomes = front.metric_total(RELAY_OUTCOMES_NAME, horde_outcome="equal")

    chunks, last_payload = stream_chunks(body)
    assert last_payload == "[DONE]"
    assert chunk_deltas(chunks) == [
        {"role": "assistant"},
        {"content": "Hello "},
        *({"content": character} for character in "from the horde."),
        {},
    ]
    assert equal_outcomes == 1
