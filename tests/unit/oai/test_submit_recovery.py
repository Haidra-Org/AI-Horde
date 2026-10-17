# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of submit recovery by operation key and of the recovery and client retry metrics."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
import pytest

from horde.oai.metrics import CLIENT_RETRY_HINTS_NAME, SUBMIT_RECOVERIES_NAME
from tests.unit.oai.conftest import (
    FAST_TEST_SETTINGS,
    REQUEST_ID,
    FakeHorde,
    RunningFront,
    StartFront,
    build_chat_body,
    done_status,
    waiting_status,
)

pytestmark = pytest.mark.asyncio

SHOULD_RETRY: str = "x-should-retry"
"""The retry-suppression header."""

QUICK_FINISH_SECONDS: float = 2.0
"""An upper bound well below ``FAST_TEST_SETTINGS.fallback_poll_seconds``: finishing within it proves the request woke
on the signal rather than the fallback poll."""

CLAIMED_SUBMISSION: dict[str, Any] = {"id": REQUEST_ID, "kudos": 12.5}
"""The submit body the Horde answers, 200 for a repeated key and 202 for a new request."""


def submitted_bodies(front_horde: FakeHorde) -> list[dict[str, Any]]:
    """Return the JSON body of every submit the mocked Horde received, in order."""
    return [json.loads(submit_call.request.content) for submit_call in front_horde.submit_route.calls]


def recovered_claim_matches(front: RunningFront) -> float:
    """Return how many recovered submits the front recorded as matching an existing claim."""
    return front.metric_total(SUBMIT_RECOVERIES_NAME, horde_existing_claim=True)


async def test_lost_submit_is_recovered_by_resending_the_same_body_and_key(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.submit_route.mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=CLAIMED_SUBMISSION)],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert response.json()["x_horde"]["request_id"] == REQUEST_ID
    first_body, recovery_body = submitted_bodies(front.horde)
    assert recovery_body == first_body
    assert uuid.UUID(first_body["horde"]["operation_key"]).version == 4
    assert recovered_claim_matches(front) == 1
    assert front.metric_total(CLIENT_RETRY_HINTS_NAME) == 0


async def test_recovery_that_queues_the_request_is_not_counted_as_a_claim_match(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.submit_route.mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(202, json=CLAIMED_SUBMISSION)],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert recovered_claim_matches(front) == 0
    assert front.metric_total(SUBMIT_RECOVERIES_NAME, horde_existing_claim=False) == 1


async def test_first_try_submit_is_not_counted_as_a_claim_match(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.submit_route.respond(200, json=CLAIMED_SUBMISSION)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert recovered_claim_matches(front) == 0


async def test_recovery_after_a_dropped_connection_retries_the_only_root(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.submit_route.mock(
            side_effect=[httpx.ReadError("reset"), httpx.Response(200, json=CLAIMED_SUBMISSION)],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert front.horde.submit_route.call_count == 2


async def test_recovered_request_is_registered_for_its_signal_before_the_first_status_read(start_front: StartFront) -> None:
    async with start_front() as front:
        status_reads = 0

        async def answer_status(_request: httpx.Request) -> httpx.Response:
            # The done signal goes out while the first read is in flight and that read still reports waiting, so only
            # a subscription made before the read wakes the request before the fallback poll.
            nonlocal status_reads
            status_reads += 1
            if status_reads == 1:
                front.horde.status_payload = done_status()
                await front.horde.publish("done")
                return httpx.Response(200, json=waiting_status())
            return httpx.Response(200, json=front.horde.status_payload)

        front.horde.status_route.mock(side_effect=answer_status)
        front.horde.submit_route.mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json=CLAIMED_SUBMISSION)],
        )
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    assert status_reads == 2
    assert elapsed < QUICK_FINISH_SECONDS


@pytest.mark.parametrize("recovery_attempts", [0, 2, 3])
async def test_recovery_stops_at_the_attempt_bound(start_front: StartFront, recovery_attempts: int) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(update={"submit_recovery_attempts": recovery_attempts})
    lost_answers: list[httpx.Response | Exception] = [
        httpx.ReadTimeout("slow"),
        httpx.Response(503, json={"message": "Overloaded.", "rc": "ServiceUnavailable"}),
        httpx.ReadError("reset"),
        httpx.ReadTimeout("slow"),
    ]
    async with start_front(settings) as front:
        front.horde.submit_route.mock(side_effect=lost_answers)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    assert response.headers[SHOULD_RETRY] == "false"
    error = response.json()["error"]
    assert error["code"] == "HordeUnavailable"
    bodies = submitted_bodies(front.horde)
    assert len(bodies) == 1 + recovery_attempts
    assert all(body == bodies[0] for body in bodies)
    assert error["metadata"] == {"submit_outcome": "unknown", "operation_key": bodies[0]["horde"]["operation_key"]}
    assert front.horde.status_route.call_count == 0
    assert front.inflight == 0


async def test_recovery_stops_at_the_end_of_the_budget(start_front: StartFront) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(update={"submit_recovery_attempts": 5, "nonstream_timeout_seconds": 0.5})
    submits_started = 0

    async def answer_slowly(_request: httpx.Request) -> httpx.Response:
        nonlocal submits_started
        submits_started += 1
        await asyncio.sleep(0.3)
        raise httpx.ReadTimeout("slow")

    async with start_front(settings) as front:
        front.horde.submit_route.mock(side_effect=answer_slowly)
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        elapsed = time.monotonic() - started_at

    assert response.status_code == 502
    assert response.headers[SHOULD_RETRY] == "false"
    assert response.json()["error"]["metadata"]["submit_outcome"] == "unknown"
    assert submits_started == 2
    assert elapsed < QUICK_FINISH_SECONDS
    assert front.inflight == 0


async def test_operation_key_conflict_during_recovery_is_502_with_the_rc(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.submit_route.mock(
            side_effect=[
                httpx.ReadTimeout("slow"),
                httpx.Response(400, json={"message": "Key used for a different request.", "rc": "OperationKeyConflict"}),
            ],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    assert response.headers[SHOULD_RETRY] == "false"
    error = response.json()["error"]
    assert error["code"] == "OperationKeyConflict"
    assert error["metadata"] == {
        "rc": "OperationKeyConflict",
        "submit_outcome": "unknown",
        "operation_key": submitted_bodies(front.horde)[0]["horde"]["operation_key"],
    }
    assert front.horde.submit_route.call_count == 2
    assert front.horde.status_route.call_count == 0


async def test_refusal_during_recovery_passes_through_without_retry(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.submit_route.mock(
            side_effect=[
                httpx.ReadTimeout("slow"),
                httpx.Response(403, json={"message": "Refused.", "rc": "KudosUpfront"}),
            ],
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 403
    assert response.headers[SHOULD_RETRY] == "false"
    error = response.json()["error"]
    assert error["code"] == "KudosUpfront"
    assert error["metadata"]["submit_outcome"] == "unknown"
    assert front.horde.submit_route.call_count == 2


async def test_each_client_request_gets_a_fresh_operation_key(start_front: StartFront) -> None:
    """Two client requests with identical bodies, such as an SDK retry after a lost response, are two submissions:
    the front does not deduplicate client requests."""
    async with start_front() as front:
        front.horde.status_payload = done_status()
        first_response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        second_response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(),
            headers={"x-stainless-retry-count": "1"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_body, second_body = submitted_bodies(front.horde)
    assert first_body["horde"]["operation_key"] != second_body["horde"]["operation_key"]


@pytest.mark.parametrize(
    ("retry_count", "counted"),
    [
        ("1", 1),
        ("2", 1),
        ("0", 0),
        ("not-a-number", 0),
        (None, 0),
    ],
)
async def test_client_retry_hint_is_counted_when_above_zero(start_front: StartFront, retry_count: str | None, counted: int) -> None:
    headers = {} if retry_count is None else {"x-stainless-retry-count": retry_count}
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(), headers=headers)

    assert response.status_code == 200
    assert recovered_claim_matches(front) == 0
    assert front.metric_total(CLIENT_RETRY_HINTS_NAME) == counted


@pytest.mark.parametrize(
    "first_response",
    [
        httpx.Response(500, json={"message": "Post-activation failure"}),
        httpx.Response(504, text="Gateway timeout"),
        httpx.Response(202, json={"unexpected": "body"}),
    ],
)
async def test_ambiguous_server_answer_recovers_the_identical_operation(start_front: StartFront, first_response: httpx.Response) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.submit_route.mock(side_effect=[first_response, httpx.Response(200, json=CLAIMED_SUBMISSION)])
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
    assert response.status_code == 200
    bodies = submitted_bodies(front.horde)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert recovered_claim_matches(front) == 1


@pytest.mark.parametrize("rc", ["ChatAdmissionDisabled"])
async def test_known_native_pre_activation_refusal_does_not_recover(start_front: StartFront, rc: str) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(503, json={"message": "Refused before activation", "rc": rc})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
    assert response.status_code == 503
    assert front.horde.submit_route.call_count == 1
    assert "submit_outcome" not in response.json()["error"]["metadata"]


async def test_model_list_cache_shares_cold_and_expired_refreshes(start_front: StartFront) -> None:
    async def answer(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=[])

    async with start_front() as front:
        front.horde.models_route.mock(side_effect=answer)
        for expected_calls in (1, 2):
            front.app.state.front.model_list_cache._fetched_at = -float("inf")
            responses = await asyncio.gather(*(front.client.get("/v1/models") for _ in range(12)))
            assert all(response.status_code == 200 for response in responses)
            assert front.horde.models_route.call_count == expected_calls
