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
    waiting_status,
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
        "done_without_generation_reads",
        "requested_model",
        "served_model",
        "retry_after",
        "first_read_wait_time",
        "first_read_eligible_worker_threads",
    },
)
"""The structured fields of the finished-request line."""

FIRST_READ_WAIT_TIME: int = 2
"""The wait estimate of the first status an admitted request reads; under the 5 s test budget, so ``fail_fast``
admits."""

FIRST_READ_ELIGIBLE_WORKER_THREADS: int = 3
"""The eligible worker threads of the first status an admitted request reads."""

FINISH_DELAY_SECONDS: float = 0.2
"""How long after the request starts a test marks it done and signals it, so the first read sees it waiting."""

QUEUE_REFUSAL_FIELDS: frozenset[str] = frozenset({"queue_reason", "wait_time", "eligible_worker_threads"})
"""The structured fields a ``queue_unavailable`` refusal adds to the finished-request line."""


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


async def test_an_unsupported_parameter_refusal_records_a_bounded_parameter_label(start_front: StartFront) -> None:
    """A parameter the request schema, the default no-op table or the undefined OpenAI keys list is recorded by its
    key, any other key as ``other``, and other refusal codes record an empty label."""
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_inflight": 1})) as front:
        known_refused = await front.client.post("/v1/chat/completions", json=build_chat_body(store=True))
        undefined_refused = await front.client.post("/v1/chat/completions", json=build_chat_body(logit_bias={"50256": -100}))
        unknown_refused = await front.client.post("/v1/chat/completions", json=build_chat_body(client_chosen_key_1234=1))
        first_request = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        capacity_refused = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await front.horde.finish_later(0)
        await first_request

    assert known_refused.status_code == 400
    assert undefined_refused.status_code == 400
    assert unknown_refused.status_code == 400
    assert capacity_refused.status_code == 503
    assert front.metric_total(REFUSED_NAME, horde_code="UnsupportedChatParameter", horde_parameter="store") == 1
    assert front.metric_total(REFUSED_NAME, horde_code="UnsupportedChatParameter", horde_parameter="logit_bias") == 1
    assert front.metric_total(REFUSED_NAME, horde_code="UnsupportedChatParameter", horde_parameter="other") == 1
    assert front.metric_total(REFUSED_NAME, horde_code="FrontAtCapacity", horde_parameter="") == 1
    refused_parameter_labels = {point.attributes["horde.parameter"] for point in metric_points(front.metric_reader, REFUSED_NAME)}
    assert refused_parameter_labels == {"store", "logit_bias", "other", ""}


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
    assert finished_fields["requested_model"] == MODEL_NAME
    assert finished_fields["served_model"] == MODEL_NAME
    assert finished_fields["retry_after"] is None
    assert not QUEUE_REFUSAL_FIELDS & finished_fields.keys()
    assert finished_record["message"].endswith(
        f" requested_model={MODEL_NAME} served_model={MODEL_NAME} retry_after=none "
        "first_read_wait_time=none first_read_eligible_worker_threads=none",
    )
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


@pytest.mark.parametrize(
    ("status_payload", "reason", "retry_after", "wait_time", "eligible_worker_threads"),
    [
        (waiting_status(is_possible=False, wait_time=2), "no_eligible_threads", 30, 2, 0),
        (waiting_status(wait_time=600, eligible_worker_threads=4), "wait_exceeds_budget", 600, 600, 4),
    ],
    ids=["no_eligible_threads", "wait_exceeds_budget"],
)
async def test_queue_unavailable_records_the_reason_and_logs_the_estimate_and_retry_after(
    start_front: StartFront,
    captured_logs: CapturedLogs,
    status_payload: dict[str, object],
    reason: str,
    retry_after: int,
    wait_time: int,
    eligible_worker_threads: int,
) -> None:
    async with start_front() as front:
        front.horde.status_payload = status_payload
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"availability_mode": "fail_fast"}))

    assert response.status_code == 503
    assert front.metric_total(ERRORS_NAME, horde_status_code=503, horde_code="queue_unavailable", horde_reason=reason) == 1
    (finished_record,) = captured_logs.finished_records()
    finished_fields = finished_record["extra"]
    assert finished_fields["code"] == "queue_unavailable"
    assert finished_fields["requested_model"] == MODEL_NAME
    assert finished_fields["served_model"] is None
    assert finished_fields["retry_after"] == retry_after
    assert finished_fields["queue_reason"] == reason
    assert finished_fields["wait_time"] == wait_time
    assert finished_fields["eligible_worker_threads"] == eligible_worker_threads
    assert finished_fields["first_read_wait_time"] == wait_time
    assert finished_fields["first_read_eligible_worker_threads"] == eligible_worker_threads
    assert finished_record["message"].endswith(
        f" requested_model={MODEL_NAME} served_model=none retry_after={retry_after} queue_reason={reason} "
        f"wait_time={wait_time} eligible_worker_threads={eligible_worker_threads} "
        f"first_read_wait_time={wait_time} first_read_eligible_worker_threads={eligible_worker_threads}",
    )


@pytest.mark.parametrize(
    ("availability_mode", "logged_wait_time", "logged_eligible_worker_threads"),
    [
        ("fail_fast", FIRST_READ_WAIT_TIME, FIRST_READ_ELIGIBLE_WORKER_THREADS),
        ("wait", None, None),
    ],
    ids=["fail_fast", "wait"],
)
async def test_admitted_request_logs_the_first_reads_estimate_only_in_the_fail_fast_mode(
    start_front: StartFront,
    captured_logs: CapturedLogs,
    availability_mode: str,
    logged_wait_time: int | None,
    logged_eligible_worker_threads: int | None,
) -> None:
    """A later read with another estimate does not change the logged first-read facts."""
    async with start_front() as front:
        front.horde.status_payload = waiting_status(
            wait_time=FIRST_READ_WAIT_TIME,
            eligible_worker_threads=FIRST_READ_ELIGIBLE_WORKER_THREADS,
        )
        finisher = asyncio.create_task(front.horde.finish_later(FINISH_DELAY_SECONDS))
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(horde={"availability_mode": availability_mode}),
        )
        await finisher

    assert response.status_code == 200
    (finished_record,) = captured_logs.finished_records()
    finished_fields = finished_record["extra"]
    assert finished_fields["code"] == "ok"
    assert finished_fields["first_read_wait_time"] == logged_wait_time
    assert finished_fields["first_read_eligible_worker_threads"] == logged_eligible_worker_threads
    assert finished_record["message"].endswith(
        f" first_read_wait_time={logged_wait_time if logged_wait_time is not None else 'none'} "
        f"first_read_eligible_worker_threads={logged_eligible_worker_threads if logged_eligible_worker_threads is not None else 'none'}",
    )


async def test_an_error_other_than_queue_unavailable_records_an_empty_reason(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"nonstream_timeout_seconds": 0.3})) as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 408
    assert front.metric_total(ERRORS_NAME, horde_code="ChatRequestTimeout", horde_reason="") == 1


async def test_horde_rate_limit_without_an_rc_records_rate_limit_exceeded(
    start_front: StartFront,
    captured_logs: CapturedLogs,
) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(429, json={"message": "90 per 1 minute"}, headers={"Retry-After": "7"})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 429
    assert front.metric_total(ERRORS_NAME, horde_status_code=429, horde_code="rate_limit_exceeded") == 1
    (finished_record,) = captured_logs.finished_records()
    assert finished_record["extra"]["code"] == "rate_limit_exceeded"
    assert finished_record["extra"]["retry_after"] == 7
