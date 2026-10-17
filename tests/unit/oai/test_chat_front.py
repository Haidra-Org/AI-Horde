# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's successful paths: completions, streams, models, health and the Horde call headers."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from horde.consts import ANONYMOUS_API_KEY
from tests.unit.oai.conftest import (
    CHARGE_BASIS,
    FAST_TEST_SETTINGS,
    GENERATED_TEXT,
    HORDE_URL,
    MODEL_NAME,
    REQUEST_ID,
    StartFront,
    build_chat_body,
    done_status,
    waiting_status,
)

pytestmark = pytest.mark.asyncio

COMPLETION_ID: str = f"chatcmpl-{REQUEST_ID}"
"""The completion id the front derives from the mocked request id."""

QUICK_FINISH_SECONDS: float = 2.0
"""An upper bound well below ``FAST_TEST_SETTINGS.fallback_poll_seconds``: finishing within it proves the request did
not wait for the fallback poll."""


def parse_sse_events(body: str) -> list[str]:
    """Return the raw events of a server-sent event body, comments included, whichever line separator it uses."""
    normalized_body = body.replace("\r\n", "\n")
    return [raw_event for raw_event in normalized_body.split("\n\n") if raw_event.strip()]


def data_payloads(events: list[str]) -> list[str]:
    """Return the ``data:`` payloads of ``events``, in order."""
    return [raw_event.removeprefix("data: ") for raw_event in events if raw_event.startswith("data: ")]


async def test_non_streamed_request_returns_the_completion_when_the_signal_arrives(start_front: StartFront) -> None:
    async with start_front() as front:
        finisher = asyncio.create_task(front.horde.finish_later(0.1))
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        elapsed = time.monotonic() - started_at
        await finisher

    assert response.status_code == 200
    assert elapsed < QUICK_FINISH_SECONDS
    assert "x-should-retry" not in response.headers
    assert response.json() == {
        "id": COMPLETION_ID,
        "object": "chat.completion",
        "created": response.json()["created"],
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": GENERATED_TEXT},
                "finish_reason": "length",
                "native_finish_reason": "length",
                "logprobs": None,
            },
        ],
        "usage": {"prompt_tokens": 21, "completion_tokens": 6, "total_tokens": 27},
        "x_horde": {
            "request_id": REQUEST_ID,
            "worker_id": "worker-1",
            "worker_name": "Test Worker",
            "kudos": 12.5,
            "charge_basis": CHARGE_BASIS,
            "estimated_usage": False,
            "queue_wait_seconds": 1.5,
        },
    }


async def test_native_body_carries_the_request_without_front_only_keys(start_front: StartFront) -> None:
    chat_body = build_chat_body(
        messages=[
            {"role": "developer", "content": "Answer in French."},
            {"role": "user", "content": [{"type": "text", "text": "Say hello."}], "name": "alice"},
        ],
        temperature=None,
        user="end-user-7",
        metadata={"trace": "abc"},
        horde={"only": ["worker-1"], "trusted_workers": True},
    )
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=chat_body)

    assert response.status_code == 200
    assert front.horde.submitted_body() == {
        "model": MODEL_NAME,
        "messages": [
            {"role": "developer", "content": "Answer in French."},
            {"role": "user", "content": [{"type": "text", "text": "Say hello."}], "name": "alice"},
        ],
        "max_tokens": 64,
        "horde": {"only": ["worker-1"], "trusted_workers": True},
    }


async def test_non_streamed_request_returns_at_once_when_the_status_is_already_done(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        elapsed = time.monotonic() - started_at

    assert response.status_code == 200
    assert elapsed < QUICK_FINISH_SECONDS
    assert front.horde.status_route.call_count == 1


async def test_signal_before_the_outcome_is_visible_earns_one_short_recheck(start_front: StartFront) -> None:
    async def signal_then_settle() -> None:
        await asyncio.sleep(0.1)
        await front.horde.publish("done")
        await asyncio.sleep(0.05)
        front.horde.status_payload = done_status()

    async with start_front() as front:
        settler = asyncio.create_task(signal_then_settle())
        started_at = time.monotonic()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        elapsed = time.monotonic() - started_at
        await settler

    assert response.status_code == 200
    assert elapsed < QUICK_FINISH_SECONDS
    assert front.horde.status_route.call_count == 3


async def test_polling_without_reported_counts_estimates_usage(start_front: StartFront) -> None:
    polling_settings = FAST_TEST_SETTINGS.model_copy(update={"fallback_poll_seconds": 0.05})
    async with start_front(polling_settings, with_redis=False) as front:
        front.horde.status_payload = waiting_status(wait_time=0)
        finisher = asyncio.create_task(
            front.horde.finish_later(
                0.2,
                signal=False,
                finish_reason=None,
                native_finish_reason=None,
                prompt_tokens=None,
                completion_tokens=None,
            ),
        )
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())
        await finisher

    assert response.status_code == 200
    completion = response.json()
    assert completion["choices"][0]["finish_reason"] == "stop"
    assert completion["choices"][0]["native_finish_reason"] is None
    assert completion["x_horde"]["estimated_usage"] is True
    assert completion["usage"]["completion_tokens"] == 7
    assert completion["usage"]["total_tokens"] == completion["usage"]["prompt_tokens"] + 7
    assert front.horde.status_route.call_count >= 2


async def test_message_content_is_preferred_over_the_plain_text_field(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.status_payload["generations"][0]["text"] = "flattened"
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.json()["choices"][0]["message"]["content"] == GENERATED_TEXT


async def test_plain_text_field_is_used_when_the_generation_has_no_message(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        del front.horde.status_payload["generations"][0]["message"]
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.json()["choices"][0]["message"]["content"] == GENERATED_TEXT


@pytest.mark.parametrize("stream", [False, True])
async def test_empty_return_carries_the_notice_with_the_request_id(start_front: StartFront, stream: bool) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status(content="")
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))

    assert response.status_code == 200
    if stream:
        chunks = [json.loads(payload) for payload in data_payloads(parse_sse_events(response.text))[:-1]]
        x_horde = chunks[-1]["x_horde"]
    else:
        x_horde = response.json()["x_horde"]
    assert REQUEST_ID in x_horde["notice"]
    assert "no visible text" in x_horde["notice"]


async def test_non_empty_return_carries_no_notice(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert "notice" not in response.json()["x_horde"]


@pytest.mark.parametrize("include_usage", [True, False])
async def test_streamed_request_sends_keepalives_then_the_completion_chunks(start_front: StartFront, include_usage: bool) -> None:
    chat_body = build_chat_body(stream=True, stream_options={"include_usage": include_usage})
    async with start_front() as front:
        finisher = asyncio.create_task(front.horde.finish_later(0.3))
        response = await front.client.post("/v1/chat/completions", json=chat_body)
        await finisher

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-should-retry"] == "false"
    events = parse_sse_events(response.text)
    assert ": horde queue_position=3 wait_time=20" in events
    payloads = data_payloads(events)
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(payload) for payload in payloads[:-1]]
    assert all(chunk["id"] == COMPLETION_ID and chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"] == [
        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None, "native_finish_reason": None, "logprobs": None},
    ]
    assert chunks[1]["choices"] == [
        {"index": 0, "delta": {"content": GENERATED_TEXT}, "finish_reason": None, "native_finish_reason": None, "logprobs": None},
    ]
    assert chunks[2]["choices"] == [
        {"index": 0, "delta": {}, "finish_reason": "length", "native_finish_reason": "length", "logprobs": None},
    ]
    assert chunks[2]["x_horde"]["request_id"] == REQUEST_ID
    assert chunks[2]["x_horde"]["charge_basis"] == CHARGE_BASIS
    if include_usage:
        assert all(chunk["usage"] is None for chunk in chunks[:3])
        assert len(chunks) == 4
        assert chunks[3]["choices"] == []
        assert chunks[3]["usage"] == {"prompt_tokens": 21, "completion_tokens": 6, "total_tokens": 27}
    else:
        assert all("usage" not in chunk for chunk in chunks)
        assert len(chunks) == 3
    keepalive_positions = [index for index, raw_event in enumerate(events) if raw_event.startswith(":")]
    first_data_position = next(index for index, raw_event in enumerate(events) if raw_event.startswith("data: "))
    assert min(keepalive_positions) < first_data_position


@pytest.mark.parametrize("state", ["censored", "csam"])
@pytest.mark.parametrize("stream", [False, True])
async def test_filtered_generations_report_content_filter(start_front: StartFront, state: str, stream: bool) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status(finish_reason="length")
        front.horde.status_payload["generations"][0]["state"] = state
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(stream=stream))
    if stream:
        chunks = [json.loads(payload) for payload in data_payloads(parse_sse_events(response.text))[:-1]]
        assert chunks[-1]["choices"][0]["finish_reason"] == "content_filter"
    else:
        assert response.json()["choices"][0]["finish_reason"] == "content_filter"


@pytest.mark.parametrize(
    ("authorization", "expected_apikey"),
    [
        ("Bearer my-horde-key", "my-horde-key"),
        ("bearer my-horde-key", "my-horde-key"),
        (None, ANONYMOUS_API_KEY),
    ],
)
async def test_bearer_token_becomes_the_horde_apikey_and_a_missing_one_is_anonymous(
    start_front: StartFront,
    authorization: str | None,
    expected_apikey: str,
) -> None:
    headers = {"User-Agent": "test-client/1.0"}
    if authorization is not None:
        headers["Authorization"] = authorization
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(), headers=headers)

    assert response.status_code == 200
    horde_headers = front.horde.submit_route.calls.last.request.headers
    assert horde_headers["apikey"] == expected_apikey
    assert horde_headers["client-agent"].startswith("horde-oai-front:")
    assert horde_headers["client-agent"].endswith(":test-client/1.0")
    assert "proxy-authorization" not in horde_headers
    assert "proxied-for" not in horde_headers


async def test_proxy_passkey_forwards_the_client_address(start_front: StartFront) -> None:
    passkey_settings = FAST_TEST_SETTINGS.model_copy(update={"proxy_passkey": "operator-passkey"})
    async with start_front(passkey_settings) as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    for horde_call in (front.horde.submit_route.calls.last, front.horde.status_route.calls.last):
        assert horde_call.request.headers["proxy-authorization"] == "operator-passkey"
        assert horde_call.request.headers["proxied-for"] == "127.0.0.1"


MODEL_LIST: list[dict[str, object]] = [
    {"name": MODEL_NAME, "count": 3, "performance": 10.0, "queued": 7, "jobs": 0, "eta": 0, "type": "text"},
    {"name": "aphrodite/other-model", "count": 1, "type": "text"},
]
"""The Horde's active text model list the model route tests serve."""


async def test_models_lists_the_horde_text_models_in_the_openai_shape(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        response = await front.client.get("/v1/models")

    assert response.status_code == 200
    text_architecture = {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]}
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "aihorde",
                "architecture": text_architecture,
                "x_horde": {"workers": 3, "queued": 7},
            },
            {
                "id": "aphrodite/other-model",
                "object": "model",
                "created": 0,
                "owned_by": "aihorde",
                "architecture": text_architecture,
                "x_horde": {"workers": 1},
            },
        ],
    }
    assert front.horde.models_route.calls.last.request.url == f"{HORDE_URL}/v2/status/models?type=text"


async def test_model_route_resolves_an_id_containing_slashes(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        found = await front.client.get(f"/v1/models/{MODEL_NAME}")
        missing = await front.client.get("/v1/models/koboldcpp/Absent")

    assert found.status_code == 200
    assert found.json()["id"] == MODEL_NAME
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"
    assert front.horde.models_route.call_count == 1


async def test_health_reports_liveness_readiness_and_subscription_separately(start_front: StartFront) -> None:
    async with start_front() as front:
        subscribed = await front.client.get("/healthz")
    async with start_front(with_redis=False) as front:
        poll_only = await front.client.get("/healthz")

    assert subscribed.json() == {"live": True, "ready": True, "signal_subscribed": True, "inflight": 0}
    assert poll_only.json() == {"live": True, "ready": True, "signal_subscribed": False, "inflight": 0}
