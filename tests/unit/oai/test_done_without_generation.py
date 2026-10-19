# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A native status that is done, not faulted and without a finished generation is never delivered as a completion.

The Horde could report this state for a moment while a worker's fault and the restart of its job landed, and reports it
for good for a request cancelled before any generation finished. The wait reads such a status again after the recheck
delay, whether a poll or a signal led to it; a request still in that state then, after a cancelled signal, or at the
deadline ends in a 502, streamed or not.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from horde.oai.metrics import WAKES_NAME
from horde.oai.settings import FrontSettings
from tests.unit.oai.conftest import (
    FAST_TEST_SETTINGS,
    REQUEST_ID,
    RunningFront,
    StartFront,
    build_chat_body,
    done_status,
    wait_for_call,
    waiting_status,
)
from tests.unit.oai.test_chat_front import data_payloads, parse_sse_events
from tests.unit.oai.test_observability import CapturedLogs, captured_logs  # noqa: F401  # the fixture is used by name

pytestmark = pytest.mark.asyncio

POLL_SETTINGS: FrontSettings = FAST_TEST_SETTINGS.model_copy(update={"fallback_poll_seconds": 0.3})
"""Settings whose fallback poll wakes the wait well before the deadline, for a front without a signal listener; the poll
is longer than the recheck delay, so the read after a poll is the recheck."""

DEADLINE_SETTINGS: FrontSettings = POLL_SETTINGS.model_copy(
    update={"nonstream_timeout_seconds": 0.3, "stream_timeout_seconds": 0.3, "signal_recheck_seconds": 1.0},
)
"""Poll settings whose deadline comes before the recheck, so the deadline's final read is the one that decides."""

SETTLE_SECONDS: float = 1.5
"""How soon a request settled by a recheck or a cancelled signal answers; far below the five-second test deadline."""

NO_GENERATION_CODE: str = "ChatRequestNoGeneration"
"""The error code of a request settled done without a generation and not faulted."""


def done_without_generation_status() -> dict[str, Any]:
    """Return the native status of a request whose every job faulted, before the restart or the request's fault shows."""
    return {**done_status(), "finished": 0, "restarted": 1, "generations": []}


def faulted_status() -> dict[str, Any]:
    """Return the native status of a request the Horde faulted after refusing its worker's replies."""
    return {
        **done_without_generation_status(),
        "faulted": True,
        "restarted": 3,
        "fault_reason": "The request's 'response_format' is 'json_object', so the reply content has to be JSON.",
        "fault_rc": "ChatSubmitNonconformant",
    }


def answer_in_order(front: RunningFront, payloads: Sequence[dict[str, Any]]) -> None:
    """Make the status route return ``payloads`` one per read, repeating the last one."""
    remaining_payloads = list(payloads)

    def answer(_request: httpx.Request) -> httpx.Response:
        payload = remaining_payloads.pop(0) if len(remaining_payloads) > 1 else remaining_payloads[0]
        return httpx.Response(200, json=payload)

    front.horde.status_route.mock(side_effect=answer)


async def publish_after_first_read(front: RunningFront, event: str) -> None:
    """Publish ``event`` once the read after the submit is answered, so the next read is the signal's."""
    await wait_for_call(lambda: front.horde.status_route.call_count)
    await front.horde.publish(event)


@pytest.mark.parametrize("wake_path", ["poll", "signal"])
async def test_a_done_status_without_a_generation_then_faulted_is_a_faulted_502(start_front: StartFront, wake_path: str) -> None:
    settings = POLL_SETTINGS if wake_path == "poll" else FAST_TEST_SETTINGS
    async with start_front(settings, with_redis=wake_path == "signal") as front:
        answer_in_order(front, [waiting_status(wait_time=0), done_without_generation_status(), faulted_status()])
        publisher = asyncio.create_task(publish_after_first_read(front, "faulted")) if wake_path == "signal" else None
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        if publisher is not None:
            await publisher

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "ChatRequestFaulted"
    assert error["metadata"] == {"request_id": REQUEST_ID, "rc": "ChatSubmitNonconformant"}
    assert front.horde.status_route.call_count == 3
    assert front.metric_total(WAKES_NAME, horde_wake_source=wake_path, horde_found_done_without_generation=True) == 1
    assert front.metric_total(WAKES_NAME, horde_wake_source="recheck", horde_found_finished=True) == 1


@pytest.mark.parametrize("wake_path", ["poll", "signal"])
async def test_a_done_status_without_a_generation_then_a_generation_is_a_completion(
    start_front: StartFront,
    captured_logs: CapturedLogs,  # noqa: F811  # the imported fixture
    wake_path: str,
) -> None:
    settings = POLL_SETTINGS if wake_path == "poll" else FAST_TEST_SETTINGS
    async with start_front(settings, with_redis=wake_path == "signal") as front:
        answer_in_order(front, [waiting_status(wait_time=0), done_without_generation_status(), done_status()])
        publisher = asyncio.create_task(publish_after_first_read(front, "done")) if wake_path == "signal" else None
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        if publisher is not None:
            await publisher

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == done_status()["generations"][0]["message"]["content"]
    assert front.horde.status_route.call_count == 3
    (finished_record,) = captured_logs.finished_records()
    assert finished_record["extra"]["woke_by"] == "recheck"
    assert finished_record["extra"]["done_without_generation_reads"] == 1
    assert "done_without_generation_reads=1" in finished_record["message"]


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_request_still_done_without_a_generation_on_the_recheck_is_a_502_and_cancelled(
    start_front: StartFront,
    captured_logs: CapturedLogs,  # noqa: F811  # the imported fixture
    streamed: bool,
) -> None:
    async with start_front(POLL_SETTINGS, with_redis=False) as front:
        front.horde.status_payload = done_without_generation_status()
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=streamed))
        elapsed = time.monotonic() - started_at

    assert elapsed < SETTLE_SECONDS
    assert front.horde.status_route.call_count == 2
    assert front.horde.cancel_route.call_count == 1
    assert front.metric_total(WAKES_NAME, horde_wake_source="recheck", horde_found_done_without_generation=True) == 1
    if streamed:
        assert response.status_code == 200
        payloads = data_payloads(parse_sse_events(response.text))
        assert payloads[-1] == "[DONE]"
        assert len(payloads) == 2
        error_event = json.loads(payloads[0])
        assert error_event["error"]["code"] == NO_GENERATION_CODE
        assert "choices" not in error_event
    else:
        assert response.status_code == 502
        assert response.headers["x-should-retry"] == "false"
        assert response.json()["error"]["code"] == NO_GENERATION_CODE
        assert response.json()["error"]["metadata"] == {"request_id": REQUEST_ID}
        assert "choices" not in response.json()
    (finished_record,) = captured_logs.finished_records()
    assert finished_record["extra"]["code"] == NO_GENERATION_CODE
    assert finished_record["extra"]["woke_by"] == "recheck"
    assert finished_record["extra"]["done_without_generation_reads"] == 2


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_request_done_without_a_generation_at_the_deadline_is_a_502_never_an_empty_completion(
    start_front: StartFront,
    streamed: bool,
) -> None:
    async with start_front(DEADLINE_SETTINGS, with_redis=False) as front:
        front.horde.status_payload = done_without_generation_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=streamed))

    assert front.metric_total(WAKES_NAME, horde_wake_source="deadline", horde_found_done_without_generation=True) == 1
    assert front.horde.cancel_route.call_count == 1
    if streamed:
        assert response.status_code == 200
        payloads = data_payloads(parse_sse_events(response.text))
        assert json.loads(payloads[0])["error"]["code"] == NO_GENERATION_CODE
        assert payloads[-1] == "[DONE]"
    else:
        assert response.status_code == 502
        assert response.json()["error"]["code"] == NO_GENERATION_CODE


@pytest.mark.parametrize("wake_path", ["poll", "signal"])
async def test_a_request_cancelled_with_no_generation_is_a_502_without_waiting_for_the_deadline(
    start_front: StartFront,
    wake_path: str,
) -> None:
    """Maintenance or the cancel route leaves a request with no generation done without one for good.

    After a ``cancelled`` signal the first read showing it settles the wait; without the signal, the recheck does.
    """
    settings = POLL_SETTINGS if wake_path == "poll" else FAST_TEST_SETTINGS
    async with start_front(settings, with_redis=wake_path == "signal") as front:
        answer_in_order(front, [waiting_status(wait_time=0), done_without_generation_status()])
        publisher = asyncio.create_task(publish_after_first_read(front, "cancelled")) if wake_path == "signal" else None
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        elapsed = time.monotonic() - started_at
        if publisher is not None:
            await publisher

    assert response.status_code == 502
    assert response.json()["error"]["code"] == NO_GENERATION_CODE
    assert elapsed < SETTLE_SECONDS
    assert front.horde.status_route.call_count == (3 if wake_path == "poll" else 2)
    assert front.horde.cancel_route.call_count == 1


def done_with_a_job_processing_status() -> dict[str, Any]:
    """Return a status done without a generation that still reports a job processing, as a Horde counting faulted
    generations as finished reported during a request's third attempt."""
    return {**done_without_generation_status(), "processing": 1, "restarted": 2}


async def test_a_done_status_with_a_job_processing_is_waited_on_by_the_poll_until_the_generation(start_front: StartFront) -> None:
    async with start_front(POLL_SETTINGS, with_redis=False) as front:
        answer_in_order(
            front,
            [waiting_status(wait_time=0), done_with_a_job_processing_status(), done_with_a_job_processing_status(), done_status()],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert front.horde.status_route.call_count == 4
    assert front.metric_total(WAKES_NAME, horde_wake_source="recheck") == 0
    assert front.horde.cancel_route.call_count == 0


async def test_a_done_status_with_a_job_processing_is_waited_on_past_the_signal_recheck(start_front: StartFront) -> None:
    async def signal_twice() -> None:
        await publish_after_first_read(front, "done")
        await wait_for_call(lambda: front.horde.status_route.call_count, minimum=3)
        await asyncio.sleep(0.2)
        await front.horde.publish("done")

    async with start_front() as front:
        answer_in_order(
            front,
            [waiting_status(wait_time=0), done_with_a_job_processing_status(), done_with_a_job_processing_status(), done_status()],
        )
        publisher = asyncio.create_task(signal_twice())
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await publisher

    assert response.status_code == 200
    assert front.horde.status_route.call_count == 4
    assert front.metric_total(WAKES_NAME, horde_wake_source="recheck", horde_found_done_without_generation=True) == 1
    assert front.horde.cancel_route.call_count == 0


async def test_a_done_status_with_a_job_processing_at_the_deadline_is_a_timeout_not_a_502(start_front: StartFront) -> None:
    async with start_front(DEADLINE_SETTINGS, with_redis=False) as front:
        front.horde.status_payload = done_with_a_job_processing_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 408
    assert response.json()["error"]["code"] == "ChatRequestTimeout"
