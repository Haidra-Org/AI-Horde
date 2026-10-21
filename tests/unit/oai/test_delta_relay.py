# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of relaying a worker's content deltas to a streamed request: which requests relay, how the delta stream
is read, the chunk order, the reconciliation with the final reply and the errors after a delta was sent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import fakeredis
import pytest
import redis.exceptions

from horde.oai import waiter
from horde.oai.app import STREAM_DIVERGED_CODE, _create_stream_redis_client
from horde.oai.mapping import build_system_fingerprint, relays_deltas
from horde.oai.metrics import FIRST_CONTENT_NAME, RELAY_OUTCOMES_NAME, RELAYED_NAME
from horde.oai.schema import ChatCompletionRequest
from horde.oai.waiter import DELTA_STREAM_KEY_PREFIX, DeltaEntry, DeltaStreamReader
from tests.unit.oai.conftest import (
    GENERATED_TEXT,
    MODEL_NAME,
    RunningFront,
    StartFront,
    build_chat_body,
    done_status,
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
    redis_client = fakeredis.FakeAsyncRedis()
    reader = DeltaStreamReader(redis_client)
    try:
        with reader.register("request-a") as first, reader.register("request-b") as second:
            assert first is not None
            assert second is not None
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"gen": "g1", "seq": "1", "content": "Hi"})
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"seq": "2", "content": "no generation"})
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-a", {"gen": "g1", "ended": "1"})
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-b", {"gen": "g2", "seq": "1", "content": "Yo"})
            await redis_client.xadd(f"{DELTA_STREAM_KEY_PREFIX}request-c", {"gen": "g3", "seq": "1", "content": "No"})

            await reader.read_once(redis_client)
            first_entries = first.take_queued()
            second_entries = second.take_queued()
            await reader.read_once(redis_client)
            entries_after_second_read = first.take_queued() + second.take_queued()
    finally:
        await redis_client.close()

    assert first_entries == [
        DeltaEntry.model_validate({"gen": "g1", "seq": 1, "content": "Hi"}),
        DeltaEntry.model_validate({"gen": "g1", "ended": True}),
    ]
    assert second_entries == [DeltaEntry.model_validate({"gen": "g2", "seq": 1, "content": "Yo"})]
    assert entries_after_second_read == []


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
