# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's metrics and its finished-request log line."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import TYPE_CHECKING

import httpx
import pytest
from loguru import logger

from horde.oai.metrics import (
    ADMITTED_NAME,
    CANCELS_NAME,
    COMPLETIONS_NAME,
    EMPTY_RETURNS_NAME,
    ERRORS_NAME,
    FIRST_CONTENT_NAME,
    INFLIGHT_NAME,
    INFLIGHT_SOURCE_MAX_NAME,
    INFLIGHT_SOURCES_NAME,
    NATIVE_CALL_DURATION_NAME,
    QUEUE_WAIT_NAME,
    REFUSED_NAME,
    REQUEST_DURATION_NAME,
    STREAM_FAILURES_NAME,
    SUBMIT_OUTCOME_UNKNOWN_NAME,
    WAKES_NAME,
)
from tests.unit.oai.conftest import (
    CALL_WAIT_SECONDS,
    FAST_TEST_SETTINGS,
    MODEL_NAME,
    REQUEST_ID,
    StartFront,
    build_chat_body,
    call_raw,
    done_status,
    metric_points,
    wait_for_call,
)

if TYPE_CHECKING:
    from loguru import Record

pytestmark = pytest.mark.asyncio

API_KEY: str = "observability-secret-key"
"""The API key a logged request is sent with; it must not reach the log."""

CLIENT_TITLE: str = "Observability Test Client"
"""The client title a logged request is sent with; it must not reach the log."""

PROMPT_TEXT: str = "Say hello."
"""The user message of every test request; it must not reach the log."""

FINISHED_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "streamed",
        "submitted_at",
        "woke_at",
        "woke_by",
        "waited_seconds",
        "status_code",
        "code",
        "wake_count",
        "estimated_usage",
    },
)
"""The structured fields of the finished-request line."""


class CapturedLogs:
    """Hold the Loguru records written while a capture is open."""

    def __init__(self) -> None:
        """Create an empty capture."""
        self.records: list[Record] = []
        """The captured records."""

    def finished_records(self) -> list[Record]:
        """Return the finished-request records."""
        return [record for record in self.records if " finished: " in record["message"]]


@pytest.fixture
def captured_logs() -> Iterator[CapturedLogs]:
    """Return the Loguru records written during the test."""
    capture = CapturedLogs()
    sink_id = logger.add(lambda message: capture.records.append(message.record), level="DEBUG")
    try:
        yield capture
    finally:
        logger.remove(sink_id)


async def test_completed_request_records_inflight_native_latency_wake_and_completion_time(start_front: StartFront) -> None:
    async with start_front() as front:
        pending_request = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        inflight_while_waiting = front.metric_total(INFLIGHT_NAME)
        sources_while_waiting = front.metric_total(INFLIGHT_SOURCES_NAME)
        source_max_while_waiting = front.metric_total(INFLIGHT_SOURCE_MAX_NAME)
        await front.horde.finish_later(0)
        response = await pending_request
        inflight_after = front.metric_total(INFLIGHT_NAME)

    assert response.status_code == 200
    assert (inflight_while_waiting, sources_while_waiting, source_max_while_waiting) == (1, 1, 1)
    assert inflight_after == 0
    assert front.metric_total(ADMITTED_NAME, horde_streamed=False) == 1
    assert front.metric_total(NATIVE_CALL_DURATION_NAME, horde_call_type="submit", horde_outcome="ok") == 1
    assert front.metric_total(NATIVE_CALL_DURATION_NAME, horde_call_type="status", horde_outcome="ok") == 2
    assert front.metric_total(WAKES_NAME, horde_wake_source="immediate", horde_found_finished=False) == 1
    assert front.metric_total(WAKES_NAME, horde_wake_source="signal", horde_found_finished=True) == 1
    assert front.metric_total(REQUEST_DURATION_NAME, horde_streamed=False, horde_status_code=200, horde_code="ok") == 1
    assert front.metric_total(FIRST_CONTENT_NAME, horde_streamed=False) == 1
    assert front.metric_total(COMPLETIONS_NAME, horde_model=MODEL_NAME, horde_estimated_usage=False) == 1
    assert front.metric_total(EMPTY_RETURNS_NAME) == 0
    (queue_wait_point,) = metric_points(front.metric_reader, QUEUE_WAIT_NAME)
    assert queue_wait_point.attributes == {"horde.model": MODEL_NAME}
    assert queue_wait_point.histogram_sum == pytest.approx(1.5)


async def test_completion_without_reported_counts_records_estimated_usage_and_empty_return(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status(content=" ", prompt_tokens=None, completion_tokens=None)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert front.metric_total(COMPLETIONS_NAME, horde_model=MODEL_NAME, horde_estimated_usage=True) == 1
    assert front.metric_total(EMPTY_RETURNS_NAME, horde_model=MODEL_NAME) == 1


async def test_front_at_capacity_records_the_refusal(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_inflight": 1})) as front:
        first_request = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        refused = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await front.horde.finish_later(0)
        await first_request

    assert refused.status_code == 503
    assert front.metric_total(REFUSED_NAME, horde_status_code=503, horde_code="FrontAtCapacity") == 1
    assert front.metric_total(ADMITTED_NAME) == 1
    assert front.metric_total(ERRORS_NAME) == 0


async def test_lost_submit_records_outcome_unknown_and_unreachable_recoveries(start_front: StartFront) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(update={"submit_recovery_attempts": 2})
    async with start_front(settings) as front:
        front.horde.submit_route.mock(side_effect=httpx.ReadTimeout("slow"))
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    assert front.metric_total(SUBMIT_OUTCOME_UNKNOWN_NAME) == 1
    assert front.metric_total(NATIVE_CALL_DURATION_NAME, horde_call_type="submit", horde_outcome="unreachable") == 1
    assert front.metric_total(NATIVE_CALL_DURATION_NAME, horde_call_type="recovery", horde_outcome="unreachable") == 2
    assert front.metric_total(ERRORS_NAME, horde_status_code=502, horde_code="HordeUnavailable", horde_streamed=False) == 1


async def test_deadline_records_a_408_by_code_and_the_acknowledged_cancel(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"nonstream_timeout_seconds": 0.3})) as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 408
    assert front.metric_total(ERRORS_NAME, horde_status_code=408, horde_code="ChatRequestTimeout") == 1
    assert front.metric_total(WAKES_NAME, horde_wake_source="deadline", horde_found_finished=False) == 1
    assert front.metric_total(CANCELS_NAME, horde_outcome="acknowledged") == 1


async def test_streamed_fault_after_headers_is_a_stream_failure_not_an_error_status(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = {**done_status(), "done": False, "faulted": True, "generations": []}
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=True))

    assert response.status_code == 200
    assert front.metric_total(STREAM_FAILURES_NAME, horde_code="ChatRequestFaulted") == 1
    assert front.metric_total(ERRORS_NAME) == 0
    assert front.metric_total(REQUEST_DURATION_NAME, horde_streamed=True, horde_status_code=200, horde_code="ChatRequestFaulted") == 1


async def test_streamed_completion_records_first_content(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=True))

    assert response.status_code == 200
    assert front.metric_total(FIRST_CONTENT_NAME, horde_streamed=True) == 1
    assert front.metric_total(REQUEST_DURATION_NAME, horde_streamed=True, horde_code="ok") == 1


async def test_disconnect_during_the_wait_records_a_disconnect_wake(start_front: StartFront) -> None:
    async with start_front() as front:
        app_task, _sent_messages = await call_raw(
            front.app,
            body_chunks=[json.dumps(build_chat_body()).encode()],
            disconnect_after_seconds=0.2,
            wait_for_app=False,
        )
        await asyncio.wait_for(app_task, timeout=CALL_WAIT_SECONDS)

    assert front.metric_total(WAKES_NAME, horde_wake_source="disconnect") == 1
    assert front.metric_total(REQUEST_DURATION_NAME, horde_status_code=499, horde_code="client_closed") == 1
    assert front.metric_total(CANCELS_NAME, horde_outcome="acknowledged") == 1


async def test_finished_line_carries_the_wait_fields_and_no_key_prompt_or_title(
    start_front: StartFront,
    captured_logs: CapturedLogs,
) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(),
            headers={"Authorization": f"Bearer {API_KEY}", "X-Title": CLIENT_TITLE},
        )

    assert response.status_code == 200
    (finished_record,) = captured_logs.finished_records()
    finished_fields = finished_record["extra"]
    assert FINISHED_FIELDS <= finished_fields.keys()
    assert finished_fields["request_id"] == REQUEST_ID
    assert finished_fields["woke_by"] == "immediate"
    assert finished_fields["status_code"] == 200
    assert finished_fields["code"] == "ok"
    assert finished_fields["wake_count"] == 1
    assert finished_fields["estimated_usage"] is False
    assert finished_fields["woke_at"] >= finished_fields["submitted_at"]
    assert finished_fields["waited_seconds"] >= 0
    logged_text = finished_record["message"] + repr(finished_fields)
    for private_text in (API_KEY, CLIENT_TITLE, PROMPT_TEXT):
        assert private_text not in logged_text


async def test_unsubmitted_request_logs_a_finished_line_without_wake_fields(
    start_front: StartFront,
    captured_logs: CapturedLogs,
) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(503, json={"message": "Chat is switched off.", "rc": "ChatAdmissionDisabled"})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 503
    (finished_record,) = captured_logs.finished_records()
    assert finished_record["extra"]["request_id"] is None
    assert finished_record["extra"]["code"] == "ChatAdmissionDisabled"
    assert finished_record["extra"]["wake_count"] == 0
