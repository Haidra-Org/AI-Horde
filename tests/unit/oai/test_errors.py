# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's error scenarios: status, headers, ``code``, ``param`` and ``metadata`` per scenario."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx

from horde.chat_contract import KudosCapExcess
from horde.oai.app import create_app
from horde.oai.horde_client import HordeAPIError, HordeClient, HordeCredentials
from horde.oai.metrics import REFUSED_NAME
from horde.oai.settings import FrontSettings
from horde.oai.waiter import ChatRequestWatcher, CompletionEvent, CompletionEventKind, CompletionSubscription, WaitBudget
from tests.unit.oai.conftest import (
    FAST_TEST_SETTINGS,
    FRONT_URL,
    HORDE_URL,
    MODEL_NAME,
    REQUEST_ID,
    UNRECORDED_METRICS,
    StartFront,
    build_chat_body,
    call_raw,
    done_status,
    response_body,
    response_status,
    wait_for_call,
    waiting_status,
)
from tests.unit.oai.test_chat_front import MODEL_LIST, VISION_MODEL_NAME, data_payloads, parse_sse_events

pytestmark = pytest.mark.asyncio

SHOULD_RETRY: str = "x-should-retry"
"""The retry-suppression header."""


def chunked_body(chat_body: dict[str, object], *, chunk_bytes: int = 64) -> list[bytes]:
    """Return ``chat_body`` encoded and split into chunks with no declared length."""
    encoded_body = json.dumps(chat_body).encode()
    return [encoded_body[offset : offset + chunk_bytes] for offset in range(0, len(encoded_body), chunk_bytes)]


async def test_body_over_the_declared_limit_is_413_before_parsing(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_body_bytes": 100})) as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "RequestTooLarge"
    assert SHOULD_RETRY not in response.headers
    assert front.horde.submit_route.call_count == 0


async def test_chunked_body_over_the_limit_is_413_while_received(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_body_bytes": 100})) as front:
        _app_task, sent_messages = await call_raw(front.app, body_chunks=chunked_body(build_chat_body()))

    assert response_status(sent_messages) == 413
    assert json.loads(response_body(sent_messages))["error"]["code"] == "RequestTooLarge"
    assert front.horde.submit_route.call_count == 0


async def test_slow_body_is_408_after_the_read_timeout(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"body_read_timeout_seconds": 0.1})) as front:
        _app_task, sent_messages = await call_raw(
            front.app,
            body_chunks=chunked_body(build_chat_body()),
            chunk_delay_seconds=0.05,
        )

    assert response_status(sent_messages) == 408
    error = json.loads(response_body(sent_messages))["error"]
    assert error["code"] == "RequestBodyTimeout"
    assert error["type"] == "timeout_error"
    assert all(header_name != SHOULD_RETRY.encode() for header_name, _ in sent_messages[0]["headers"])
    assert front.horde.submit_route.call_count == 0
    assert front.inflight == 0
    assert front.metric_total(REFUSED_NAME, horde_status_code=408, horde_code="RequestBodyTimeout") == 1
    assert front.metric_total(REFUSED_NAME, horde_code="RequestTooLarge") == 0


@pytest.mark.parametrize("raw_body", [b"{not json", b"[1, 2]"])
async def test_invalid_json_or_a_non_object_is_a_bad_request(start_front: StartFront, raw_body: bytes) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", content=raw_body, headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": response.json()["error"]["message"],
            "type": "invalid_request_error",
            "param": None,
            "code": "BadRequest",
            "metadata": {},
        },
    }


@pytest.mark.parametrize(
    ("non_finite", "constant"),
    [
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
    ],
)
@pytest.mark.parametrize("placement", ["temperature", "tool_schema"])
async def test_a_non_finite_json_constant_is_a_bad_request_before_any_horde_call(
    start_front: StartFront,
    non_finite: float,
    constant: str,
    placement: str,
) -> None:
    if placement == "temperature":
        body = build_chat_body(temperature=non_finite)
    else:
        tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "number", "maximum": non_finite}}}
        body = build_chat_body(tools=[tool])
    # json.dumps writes the Python spellings NaN, Infinity and -Infinity, which are not JSON.
    raw_body = json.dumps(body).encode()
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", content=raw_body, headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == f"The request body is not valid JSON: {constant} is not a JSON value."
    assert (error["type"], error["param"], error["code"]) == ("invalid_request_error", None, "BadRequest")
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Bearer two words"])
async def test_malformed_authorization_is_401_not_anonymous(start_front: StartFront, authorization: str) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(), headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert response.json()["error"]["code"] == "InvalidAuthorizationHeader"
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize(
    ("horde_status", "horde_rc", "expected_type", "expected_code"),
    [
        (401, "InvalidAPIKey", "authentication_error", "InvalidAPIKey"),
        (402, "KudosUpfront", "api_error", "KudosUpfront"),
        (403, "CorruptPrompt", "permission_error", "CorruptPrompt"),
        (404, "RequestNotFound", "not_found_error", "RequestNotFound"),
    ],
)
async def test_horde_refusal_at_submit_keeps_its_status_and_rc(
    start_front: StartFront,
    horde_status: int,
    horde_rc: str,
    expected_type: str,
    expected_code: str,
) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(horde_status, json={"message": "Refused.", "rc": horde_rc})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == horde_status
    error = response.json()["error"]
    assert error["type"] == expected_type
    assert error["code"] == expected_code
    assert error["metadata"] == {"rc": horde_rc}
    assert SHOULD_RETRY not in response.headers
    assert front.inflight == 0


async def test_a_refusal_over_the_kudos_cap_is_400_on_max_kudos_with_both_values(start_front: StartFront) -> None:
    """The Horde refuses the request before creating it, so the front answers 400 without a request id."""
    horde_message = KudosCapExcess(kudos_estimate=57, max_kudos=40).to_message()
    async with start_front() as front:
        front.horde.submit_route.respond(400, json={"message": horde_message, "rc": "ChatKudosCapExceeded"})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"max_kudos": 40}))

    assert front.horde.submitted_body()["horde"]["max_kudos"] == 40
    assert response.status_code == 400
    assert SHOULD_RETRY not in response.headers
    assert response.json() == {
        "error": {
            "message": horde_message,
            "type": "invalid_request_error",
            "param": "horde.max_kudos",
            "code": "max_kudos_exceeded",
            "metadata": {"rc": "ChatKudosCapExceeded", "kudos_estimate": 57, "max_kudos": 40},
        },
    }
    assert front.inflight == 0


async def test_horde_rate_limit_passes_through_with_its_headers(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(
            429,
            json={"message": "Too many prompts.", "rc": "TooManyPrompts"},
            headers={"Retry-After": "7", "X-RateLimit-Limit": "2", "X-RateLimit-Remaining": "0"},
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.headers["x-ratelimit-limit"] == "2"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert SHOULD_RETRY not in response.headers
    assert response.json() == {
        "error": {
            "message": "Too many prompts.",
            "type": "rate_limit_error",
            "param": None,
            "code": "TooManyPrompts",
            "metadata": {"rc": "TooManyPrompts", "retry_after": 7},
        },
    }


async def test_horde_rate_limit_without_an_rc_is_rate_limit_exceeded_with_its_retry_after(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(
            429,
            json={"message": "90 per 1 minute"},
            headers={"Retry-After": "7", "X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "0"},
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.headers["x-ratelimit-limit"] == "90"
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert SHOULD_RETRY not in response.headers
    assert response.json() == {
        "error": {
            "message": "90 per 1 minute",
            "type": "rate_limit_error",
            "param": None,
            "code": "rate_limit_exceeded",
            "metadata": {"retry_after": 7},
        },
    }


async def test_chat_admission_disabled_is_503_with_retry_after(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(503, json={"message": "Chat is switched off.", "rc": "ChatAdmissionDisabled"})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["code"] == "ChatAdmissionDisabled"
    assert SHOULD_RETRY not in response.headers


async def test_horde_unreachable_before_send_is_a_retriable_502() -> None:
    unreachable_settings = FrontSettings(horde_urls=(HORDE_URL, "http://horde-second.test/api"))
    with respx.mock(assert_all_called=False) as router:
        first_route = router.post(f"{HORDE_URL}/v2/generate/chat/async").mock(side_effect=httpx.ConnectError("refused"))
        second_route = router.post("http://horde-second.test/api/v2/generate/chat/async").mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as horde_http_client:
            app = create_app(unreachable_settings, http_client=horde_http_client, cancel_http_client=horde_http_client)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url=FRONT_URL) as front_client:
                    response = await front_client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "HordeUnavailable"
    assert response.json()["error"]["metadata"] == {}
    assert SHOULD_RETRY not in response.headers
    assert first_route.call_count == 1
    assert second_route.call_count == 1


@pytest.mark.parametrize("transport_error", [httpx.ReadTimeout("slow"), httpx.ReadError("reset")])
async def test_submit_lost_after_send_and_recovery_is_502_without_retry(
    start_front: StartFront,
    transport_error: httpx.TransportError,
) -> None:
    async with start_front() as front:
        front.horde.submit_route.mock(side_effect=transport_error)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    assert response.headers[SHOULD_RETRY] == "false"
    error = response.json()["error"]
    assert error["code"] == "HordeUnavailable"
    operation_key = front.horde.submitted_body()["horde"]["operation_key"]
    assert error["metadata"] == {"submit_outcome": "unknown", "operation_key": operation_key}
    assert front.horde.submit_route.call_count == 1 + FAST_TEST_SETTINGS.submit_recovery_attempts
    assert front.inflight == 0


async def test_front_at_capacity_is_503_with_retry_after_and_no_submit(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_inflight": 1})) as front:
        first_request = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        refused = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await front.horde.finish_later(0)
        first_response = await first_request

    assert refused.status_code == 503
    assert refused.headers["retry-after"] == "5"
    assert SHOULD_RETRY not in refused.headers
    assert refused.json()["error"]["code"] == "FrontAtCapacity"
    assert refused.json()["error"]["metadata"] == {"retry_after": 5}
    assert first_response.status_code == 200
    assert front.horde.submit_route.call_count == 1


async def test_deadline_with_the_request_still_waiting_is_408_without_retry_and_cancels(start_front: StartFront) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"nonstream_timeout_seconds": 0.3})) as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 408
    assert response.headers[SHOULD_RETRY] == "false"
    error = response.json()["error"]
    assert error["code"] == "ChatRequestTimeout"
    assert error["type"] == "timeout_error"
    assert error["metadata"] == {"request_id": REQUEST_ID, "retry_after": 30}
    assert response.headers["retry-after"] == "30"
    assert front.horde.cancel_route.call_count == 1


async def test_faulted_request_is_502_without_retry(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = {**waiting_status(), "faulted": True}
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    assert response.headers[SHOULD_RETRY] == "false"
    assert response.json()["error"]["code"] == "ChatRequestFaulted"
    assert response.json()["error"]["metadata"] == {"request_id": REQUEST_ID}
    assert front.horde.cancel_route.call_count == 0


async def test_a_request_faulted_after_a_refused_reply_names_the_refusal_in_the_502(start_front: StartFront) -> None:
    refusal = "The request's 'response_format' is 'json_object', so the reply content has to be JSON."
    async with start_front() as front:
        front.horde.status_payload = {
            **waiting_status(),
            "faulted": True,
            "fault_reason": refusal,
            "fault_rc": "ChatSubmitNonconformant",
        }
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "ChatRequestFaulted"
    assert error["message"] == (
        f"The AI Horde reports request {REQUEST_ID} as faulted after refusing a worker's reply (ChatSubmitNonconformant): {refusal}"
    )
    assert error["metadata"] == {"request_id": REQUEST_ID, "rc": "ChatSubmitNonconformant"}


async def test_a_request_faulted_with_a_worker_fault_reason_names_it_in_the_502(start_front: StartFront) -> None:
    reason = "The worker could not fit the prompt into its context."
    async with start_front() as front:
        front.horde.status_payload = {
            **waiting_status(),
            "faulted": True,
            "fault_reason": reason,
            "fault_rc": "ChatWorkerFault",
        }
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "ChatRequestFaulted"
    assert error["message"] == (
        f"The AI Horde reports request {REQUEST_ID} as faulted after its worker reported a fault (ChatWorkerFault): {reason}"
    )
    assert error["metadata"] == {"request_id": REQUEST_ID, "rc": "ChatWorkerFault"}


async def test_a_faulted_signal_carries_the_refusal_to_a_request_whose_row_is_gone(start_front: StartFront) -> None:
    refusal = "'message.tool_calls' has two calls with the same 'id'."

    async def expire() -> None:
        await asyncio.sleep(0.1)
        front.horde.status_code = 404
        front.horde.status_payload = {"message": "Request not found.", "rc": "RequestNotFound"}
        await front.horde.publish("faulted", fault_reason=refusal, fault_rc="ChatSubmitInvalid")

    async with start_front() as front:
        expirer = asyncio.create_task(expire())
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await expirer

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["message"].endswith(f"(ChatSubmitInvalid): {refusal}")
    assert error["metadata"]["rc"] == "ChatSubmitInvalid"


async def test_faulted_signal_then_missing_status_is_an_expired_faulted_request(start_front: StartFront) -> None:
    async def expire() -> None:
        await asyncio.sleep(0.1)
        front.horde.status_code = 404
        front.horde.status_payload = {"message": "Request not found.", "rc": "RequestNotFound"}
        await front.horde.publish("faulted")

    async with start_front() as front:
        expirer = asyncio.create_task(expire())
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await expirer

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ChatRequestFaulted"


async def test_status_404_without_a_faulted_signal_passes_through_without_retry(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_code = 404
        front.horde.status_payload = {"message": "Request not found.", "rc": "RequestNotFound"}
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 404
    assert response.headers[SHOULD_RETRY] == "false"
    assert response.json()["error"]["metadata"] == {"request_id": REQUEST_ID, "rc": "RequestNotFound"}


@pytest.mark.parametrize("stream", [False, True])
async def test_no_eligible_worker_past_the_grace_is_503_and_cancels(start_front: StartFront, stream: bool) -> None:
    fail_fast_settings = FAST_TEST_SETTINGS.model_copy(update={"fail_fast_grace_seconds": 0.2})
    async with start_front(fail_fast_settings) as front:
        front.horde.status_payload = waiting_status(is_possible=False)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))

    assert front.horde.cancel_route.call_count == 1
    assert response.headers[SHOULD_RETRY] == "false"
    if stream:
        assert response.status_code == 200
        payloads = data_payloads(parse_sse_events(response.text))
        assert json.loads(payloads[0])["error"]["code"] == "no_eligible_worker"
        assert payloads[-1] == "[DONE]"
    else:
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        assert response.json()["error"]["code"] == "no_eligible_worker"
        assert response.json()["error"]["metadata"] == {"request_id": REQUEST_ID, "retry_after": 30}


def gap_status(*, missing_declarations: list[str], exceeded_limits: list[str], kind: str = "declaration_gap") -> dict[str, object]:
    """Return the native status of a request the online workers cannot take because of what they declare."""
    return {
        **waiting_status(is_possible=False),
        "impossible_reason": {"kind": kind, "missing_declarations": missing_declarations, "exceeded_limits": exceeded_limits},
    }


@pytest.mark.parametrize("stream", [False, True])
async def test_a_declaration_gap_is_404_on_the_first_status_without_the_grace_and_cancels(start_front: StartFront, stream: bool) -> None:
    async with start_front() as front:
        front.horde.status_payload = gap_status(missing_declarations=["tool_choice_enforced"], exceeded_limits=[])
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))
        elapsed_seconds = time.monotonic() - started_at

    assert elapsed_seconds < FAST_TEST_SETTINGS.fail_fast_grace_seconds / 2
    assert front.horde.cancel_route.call_count == 1
    assert response.headers[SHOULD_RETRY] == "false"
    assert "retry-after" not in response.headers
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    error = response.json()["error"]
    assert error["code"] == "no_worker_supports_features"
    assert error["type"] == "not_found_error"
    assert "declares tool_choice_enforced" in error["message"]
    assert "best_effort" in error["message"]
    assert error["metadata"] == {"request_id": REQUEST_ID}


async def test_an_exceeded_limit_is_404_naming_the_limit(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = gap_status(missing_declarations=[], exceeded_limits=["tools_max_temperature"])
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "no_worker_supports_features"
    assert "exceeds the tools_max_temperature limit" in error["message"]
    assert "best_effort" not in error["message"]


@pytest.mark.parametrize("stream", [False, True])
async def test_an_exceeded_completion_limit_is_400_on_max_tokens_stating_the_largest_declared(
    start_front: StartFront,
    stream: bool,
) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        front.horde.status_payload = gap_status(missing_declarations=[], exceeded_limits=["max_length"])
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))
        elapsed_seconds = time.monotonic() - started_at

    assert elapsed_seconds < FAST_TEST_SETTINGS.fail_fast_grace_seconds / 2
    assert front.horde.cancel_route.call_count == 1
    assert response.headers[SHOULD_RETRY] == "false"
    assert "retry-after" not in response.headers
    assert response.status_code == 400
    error = response.json()["error"]
    assert error == {
        "message": (
            "No worker eligible for this request currently declares a completion limit that high; the largest any online "
            "worker for the model declares now is 1024 tokens. The request was cancelled."
        ),
        "type": "invalid_request_error",
        "param": "max_tokens",
        "code": "no_worker_supports_limit",
        "metadata": {"request_id": REQUEST_ID},
    }


async def test_an_exceeded_context_limit_alone_is_400_on_the_context_length(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        front.horde.status_payload = gap_status(missing_declarations=[], exceeded_limits=["max_context_length"])
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "horde.max_context_length"
    assert error["message"].startswith(
        "No worker eligible for this request currently declares a context length that high; "
        "the largest any online worker for the model declares now is 8192 tokens.",
    )


async def test_both_exceeded_size_limits_are_400_on_max_tokens_stating_each(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        front.horde.status_payload = gap_status(missing_declarations=[], exceeded_limits=["max_context_length", "max_length"])
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == "max_tokens"
    assert "completion limit that high; the largest any online worker for the model declares now is 1024 tokens." in error["message"]
    assert "context length that high; the largest any online worker for the model declares now is 8192 tokens." in error["message"]


async def test_an_exceeded_size_limit_of_listed_models_states_the_largest_declared_across_them(start_front: StartFront) -> None:
    """Any listed model may serve the request, so the 400 states the largest limit any of them declares."""
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        front.horde.status_payload = gap_status(missing_declarations=[], exceeded_limits=["max_context_length", "max_length"])
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(model=None, models=[VISION_MODEL_NAME, MODEL_NAME]),
        )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "completion limit that high; the largest any online worker for the listed models declares now is 1024 tokens." in message
    assert "context length that high; the largest any online worker for the listed models declares now is 8192 tokens." in message


async def test_an_exceeded_size_limit_of_a_model_missing_from_the_list_states_the_limit_name(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = gap_status(missing_declarations=[], exceeded_limits=["max_length"])
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "No worker eligible for this request currently declares a completion limit (max_length) that high. The request was cancelled."
    )


async def test_an_exceeded_size_limit_beside_another_gap_keeps_the_404(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        front.horde.status_payload = gap_status(missing_declarations=["tools"], exceeded_limits=["max_length"])
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "no_worker_supports_features"
    assert "declares tools" in error["message"]
    assert "exceeds the max_length limit" in error["message"]


async def test_an_impossible_reason_of_an_unknown_kind_waits_out_the_grace(start_front: StartFront) -> None:
    fail_fast_settings = FAST_TEST_SETTINGS.model_copy(update={"fail_fast_grace_seconds": 0.2})
    async with start_front(fail_fast_settings) as front:
        front.horde.status_payload = gap_status(missing_declarations=["x"], exceeded_limits=[], kind="a_later_kind")
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_eligible_worker"


def no_eligible_worker_refusal(
    *,
    missing_declarations: list[str] | None = None,
    exceeded_limits: list[str] | None = None,
) -> dict[str, object]:
    """Return the native body of a submit refused ``NoEligibleWorker``, with ``impossible_reason`` when a gap is given."""
    if missing_declarations is None and exceeded_limits is None:
        return {
            "message": "No online worker can serve this request.",
            "rc": "NoEligibleWorker",
        }
    return {
        "message": "No online worker can serve this request: a declaration gap.",
        "rc": "NoEligibleWorker",
        "impossible_reason": {
            "kind": "declaration_gap",
            "missing_declarations": missing_declarations or [],
            "exceeded_limits": exceeded_limits or [],
        },
    }


@pytest.mark.parametrize("stream", [False, True])
async def test_a_submit_refused_for_a_declaration_gap_is_404_without_a_request(start_front: StartFront, stream: bool) -> None:
    """The Horde refuses the submit and creates no request, so the 404 states the gap, carries no request id and
    leaves retrying to the client; a streaming request gets the same JSON error before any stream header."""
    async with start_front() as front:
        front.horde.submit_route.respond(403, json=no_eligible_worker_refusal(missing_declarations=["vision"]))
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))

    assert front.horde.status_route.call_count == 0
    assert front.horde.cancel_route.call_count == 0
    assert SHOULD_RETRY not in response.headers
    assert "retry-after" not in response.headers
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "error": {
            "message": "No worker can serve this request: no online worker serving the model declares vision.",
            "type": "not_found_error",
            "param": None,
            "code": "no_worker_supports_features",
            "metadata": {},
        },
    }
    assert front.inflight == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_a_submit_refused_for_a_size_limit_gap_is_400_stating_the_largest_declared(
    start_front: StartFront,
    stream: bool,
) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        front.horde.submit_route.respond(403, json=no_eligible_worker_refusal(exceeded_limits=["max_length"]))
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))

    assert SHOULD_RETRY not in response.headers
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": (
                "No worker eligible for this request currently declares a completion limit that high; the largest any online "
                "worker for the model declares now is 1024 tokens."
            ),
            "type": "invalid_request_error",
            "param": "max_tokens",
            "code": "no_worker_supports_limit",
            "metadata": {},
        },
    }


@pytest.mark.parametrize("stream", [False, True])
async def test_a_submit_refused_without_a_gap_is_503_no_eligible_worker_without_a_request(start_front: StartFront, stream: bool) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(403, json=no_eligible_worker_refusal())
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))

    assert front.horde.cancel_route.call_count == 0
    assert SHOULD_RETRY not in response.headers
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json() == {
        "error": {
            "message": "No online worker can serve this request at the moment. Retry later.",
            "type": "api_error",
            "param": None,
            "code": "no_eligible_worker",
            "metadata": {"retry_after": 30},
        },
    }


async def test_no_eligible_worker_does_not_fail_while_a_worker_is_processing(start_front: StartFront) -> None:
    fail_fast_settings = FAST_TEST_SETTINGS.model_copy(update={"fail_fast_grace_seconds": 0.1, "fallback_poll_seconds": 0.05})
    async with start_front(fail_fast_settings, with_redis=False) as front:
        front.horde.status_payload = waiting_status(is_possible=False, processing=1)
        finisher = asyncio.create_task(front.horde.finish_later(0.4, signal=False))
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await finisher

    assert response.status_code == 200
    assert front.horde.cancel_route.call_count == 0


async def test_stream_error_event_writes_explicit_nulls_and_no_choice(start_front: StartFront) -> None:
    """A status read after the first one fails with 404, so the error arrives after the stream's headers."""
    async with start_front() as front:
        request_task = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body(stream=True)))
        await wait_for_call(lambda: front.horde.status_route.call_count)
        front.horde.status_code = 404
        front.horde.status_payload = {"detail": "gone"}
        await front.horde.publish("generation")
        response = await request_task

    assert response.status_code == 200
    payloads = data_payloads(parse_sse_events(response.text))
    assert payloads[-1] == "[DONE]"
    assert len(payloads) == 2
    assert '"param":null' in payloads[0]
    assert '"code":null' in payloads[0]
    error_event = json.loads(payloads[0])
    assert error_event == {
        "error": {
            "message": error_event["error"]["message"],
            "type": "not_found_error",
            "param": None,
            "code": None,
            "metadata": {"request_id": REQUEST_ID},
        },
    }
    assert "choices" not in error_event


@pytest.mark.parametrize("failure", ["faulted", "timeout"])
async def test_stream_failure_after_headers_is_an_error_event_then_done(start_front: StartFront, failure: str) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"stream_timeout_seconds": 0.3})) as front:
        if failure == "faulted":
            front.horde.status_payload = {**waiting_status(), "faulted": True}
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=True))

    assert response.status_code == 200
    assert response.headers[SHOULD_RETRY] == "false"
    payloads = data_payloads(parse_sse_events(response.text))
    expected_code = "ChatRequestFaulted" if failure == "faulted" else "ChatRequestTimeout"
    assert json.loads(payloads[0])["error"]["code"] == expected_code
    assert payloads[-1] == "[DONE]"
    assert front.horde.cancel_route.call_count == (1 if failure == "timeout" else 0)


async def test_completed_non_streamed_response_carries_no_retry_header(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    assert SHOULD_RETRY not in response.headers


@pytest.mark.parametrize(
    ("horde_status", "horde_message", "horde_rc", "expected_param"),
    [
        (
            400,
            "'messages[1].content[1].type=image_url' is not supported on chat requests.",
            "UnsupportedChatParameter",
            "messages[1].content[1].type=image_url",
        ),
        (400, "The image URL resolves to a private address.", "SourceImageUrlRefused", None),
        (
            400,
            "Image validation failed. The images of a chat request reach 6291468 bytes, over the 5242880 bytes allowed in total.",
            "SourceImageSizeExceeded",
            None,
        ),
        (503, "The image store refused the image.", "ChatImageStorageUnavailable", None),
    ],
)
async def test_horde_image_refusal_passes_through_with_its_rc_and_path(
    start_front: StartFront,
    horde_status: int,
    horde_message: str,
    horde_rc: str,
    expected_param: str | None,
) -> None:
    async with start_front() as front:
        front.horde.submit_route.respond(horde_status, json={"message": horde_message, "rc": horde_rc})
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == horde_status
    error = response.json()["error"]
    assert error["code"] == horde_rc
    assert error["param"] == expected_param
    assert error["message"] == horde_message
    assert front.inflight == 0


async def test_an_unknown_listed_model_is_404_on_its_entry(start_front: StartFront) -> None:
    """The native refusal of an unknown entry of ``models`` quotes the entry's path, which becomes ``param``."""
    horde_message = (
        "'models[1]': The model 'koboldcpp/Absent' is not known to this horde: the text model reference does not list "
        "it and no online worker serves chat jobs with it."
    )
    async with start_front() as front:
        front.horde.submit_route.respond(404, json={"message": horde_message, "rc": "UnknownChatModel"})
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(model=None, models=[MODEL_NAME, "koboldcpp/Absent"]),
        )

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "not_found_error"
    assert error["code"] == "UnknownChatModel"
    assert error["param"] == "models[1]"
    assert error["message"] == horde_message
    assert front.inflight == 0


@pytest.mark.parametrize(
    ("native_field", "expected_param"),
    [
        ("temperature", "temperature"),
        ("max_length", "max_tokens"),
        ("stop_sequence.0", "stop[0]"),
        ("max_context_length", "horde.max_context_length"),
        ("messages.0.content", "messages[0].content"),
    ],
)
async def test_native_payload_validation_failure_is_a_bad_request_with_the_failing_param(
    start_front: StartFront,
    native_field: str,
    expected_param: str,
) -> None:
    """A native validation failure, which carries no rc and a generic message, is a 400 ``BadRequest`` whose
    ``param`` and message give the failing request key."""
    async with start_front() as front:
        front.horde.submit_route.respond(
            400,
            json={"errors": {native_field: "5.5 is greater than the maximum of 5.0"}, "message": "Input payload validation failed"},
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == expected_param
    assert error["code"] == "BadRequest"
    assert error["message"] == f"{expected_param}: 5.5 is greater than the maximum of 5.0"
    assert front.inflight == 0


@pytest.mark.parametrize("failure", [httpx.Response(503, json={"message": "Unavailable"}), httpx.ReadTimeout("slow")])
async def test_failed_fail_fast_recheck_does_not_cancel_from_stale_status(start_front: StartFront, failure: object) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(update={"fail_fast_grace_seconds": 0.02, "signal_recheck_seconds": 0.02})
    async with start_front(settings, with_redis=False) as front:
        front.horde.status_route.mock(
            side_effect=[
                httpx.Response(200, json=waiting_status(is_possible=False)),
                failure,
                httpx.Response(200, json=done_status()),
            ]
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
    assert response.status_code == 200
    assert front.horde.status_route.call_count == 3
    assert front.horde.cancel_route.call_count == 0


@pytest.mark.parametrize("token", [b"caf\xe9", b"key\x00", b"key\x7f", b"key\tpart"])
async def test_non_ascii_or_control_bearer_token_is_401(start_front: StartFront, token: bytes) -> None:
    async with start_front() as front:
        _, messages = await call_raw(
            front.app, body_chunks=[json.dumps(build_chat_body()).encode()], headers=[(b"authorization", b"Bearer " + token)]
        )
    assert response_status(messages) == 401
    assert json.loads(response_body(messages))["error"]["code"] == "InvalidAuthorizationHeader"
    assert front.horde.submit_route.call_count == 0
    assert front.inflight == 0


@pytest.mark.parametrize("field", ["temperature", "top_p", "frequency_penalty", "presence_penalty"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_sampling_value_is_refused_before_forwarding(start_front: StartFront, field: str, value: float) -> None:
    async with start_front() as front:
        body = {**build_chat_body(), field: value}
        response = await front.client.post("/v1/chat/completions", content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert front.horde.submit_route.call_count == 0
    assert front.inflight == 0


async def test_invalid_authorization_is_refused_without_receiving_a_body(start_front: StartFront) -> None:
    from tests.unit.oai.conftest import raw_chat_scope

    async def receive() -> dict[str, object]:
        raise AssertionError("An invalid bearer token must be refused before receiving body bytes")

    messages = []

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async with start_front() as front:
        await front.app(raw_chat_scope(headers=[(b"authorization", b"Basic bad")]), receive, send)
    assert response_status(messages) == 401
    assert front.inflight == 0
    assert front.horde.submit_route.call_count == 0


async def test_mid_body_disconnect_is_quiet_and_releases_admission(start_front: StartFront) -> None:
    from tests.unit.oai.conftest import raw_chat_scope

    events = iter(
        [
            {"type": "http.request", "body": b'{"model":', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    messages = []

    async def receive() -> dict[str, object]:
        return next(events)

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    async with start_front() as front:
        await front.app(raw_chat_scope(), receive, send)
    assert response_status(messages) == 499
    assert front.inflight == 0
    assert front.horde.submit_route.call_count == 0
    assert front.metric_total(REFUSED_NAME, horde_code="client_closed") == 1


@pytest.mark.parametrize("publish_fault", [False, True])
async def test_missing_previously_visible_status_reconciles_expiry_within_a_bound(start_front: StartFront, publish_fault: bool) -> None:
    settings = FAST_TEST_SETTINGS.model_copy(update={"fallback_poll_seconds": 0.02, "signal_recheck_seconds": 0.1})
    missing_read = asyncio.Event()
    reads = 0

    async def answer(_request: httpx.Request) -> httpx.Response:
        nonlocal reads
        reads += 1
        if reads == 1:
            return httpx.Response(200, json=waiting_status(wait_time=0))
        missing_read.set()
        return httpx.Response(404, json={"message": "Gone", "rc": "RequestNotFound"})

    async with start_front(settings) as front:
        front.horde.status_route.mock(side_effect=answer)
        request = asyncio.create_task(front.client.post("/v1/chat/completions", json=build_chat_body()))
        await asyncio.wait_for(missing_read.wait(), timeout=1)
        if publish_fault:
            await asyncio.sleep(0.02)
            await front.horde.publish("faulted")
        response = await asyncio.wait_for(request, timeout=0.5)
    assert response.status_code == (502 if publish_fault else 404)
    assert response.json()["error"]["code"] == ("ChatRequestFaulted" if publish_fault else "RequestNotFound")
    assert response.headers[SHOULD_RETRY] == "false"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("horde_status", "expected_cancels"), [(400, 1), (401, 1), (403, 1), (422, 1), (404, 0)])
async def test_status_read_the_wait_cannot_fix_cancels_unless_the_request_is_gone(
    start_front: StartFront,
    stream: bool,
    horde_status: int,
    expected_cancels: int,
) -> None:
    async with start_front() as front:
        front.horde.status_code = horde_status
        front.horde.status_payload = {"message": "Refused.", "rc": "StatusRefused"}
        front.horde.cancel_route.mock(return_value=httpx.Response(200, json=waiting_status()))
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))

    assert front.horde.cancel_route.call_count == expected_cancels
    assert response.headers[SHOULD_RETRY] == "false"
    assert response.status_code == horde_status
    assert response.json()["error"]["metadata"] == {"request_id": REQUEST_ID, "rc": "StatusRefused"}
    assert front.inflight == 0
    assert not front.app.state.front.open_requests


async def test_events_other_than_faulted_during_reconciliation_stay_queued_for_the_watcher() -> None:
    subscription = CompletionSubscription(REQUEST_ID)
    done_event = CompletionEvent.model_validate({"id": REQUEST_ID, "event": CompletionEventKind.DONE})
    reads = 0

    async def answer(_request: httpx.Request) -> httpx.Response:
        nonlocal reads
        reads += 1
        if reads == 1:
            return httpx.Response(200, json=waiting_status(wait_time=0))
        # Queued while the row is missing, so the reconciliation's wait takes it from the subscription.
        subscription.deliver(done_event)
        return httpx.Response(404, json={"message": "Gone", "rc": "RequestNotFound"})

    with respx.mock() as router:
        router.get(f"{HORDE_URL}/v2/generate/chat/status/{REQUEST_ID}").mock(side_effect=answer)
        async with httpx.AsyncClient() as http_client:
            watcher = ChatRequestWatcher(
                horde_client=HordeClient(
                    horde_urls=(HORDE_URL,),
                    http_client=http_client,
                    proxy_passkey=None,
                    front_metrics=UNRECORDED_METRICS,
                ),
                credentials=HordeCredentials(apikey="test-key", client_agent="test:1:test"),
                subscription=subscription,
                front_metrics=UNRECORDED_METRICS,
                budget=WaitBudget(
                    deadline=time.monotonic() + 5,
                    final_status_timeout_seconds=0.5,
                    fallback_poll_seconds=0.02,
                    signal_recheck_seconds=0.05,
                    fail_fast_grace_seconds=10,
                ),
            )
            with pytest.raises(HordeAPIError) as raised:
                await asyncio.wait_for(watcher.wait_until_finished(), timeout=2)

    assert raised.value.status_code == 404
    assert await subscription.wait(0) == done_event
    assert await subscription.wait(0) is None
