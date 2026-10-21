# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's successful paths: completions, streams, models, health and the Horde call headers."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from horde.consts import ANONYMOUS_API_KEY
from horde.oai.mapping import build_system_fingerprint
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
        "system_fingerprint": build_system_fingerprint("worker-1", MODEL_NAME),
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
    submitted_body = front.horde.submitted_body()
    operation_key = submitted_body["horde"].pop("operation_key")
    assert uuid.UUID(operation_key).version == 4
    assert submitted_body == {
        "model": MODEL_NAME,
        "messages": [
            {"role": "developer", "content": "Answer in French."},
            {"role": "user", "content": [{"type": "text", "text": "Say hello."}], "name": "alice"},
        ],
        "max_tokens": 64,
        "horde": {"only": ["worker-1"], "trusted_workers": True},
    }


async def test_the_conformance_mode_is_forwarded_as_sent(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"conformance": "best_effort"}))

    assert response.status_code == 200
    assert front.horde.submitted_body()["horde"]["conformance"] == "best_effort"


async def test_truncate_oldest_is_forwarded_and_its_context_adjustments_reach_x_horde(start_front: StartFront) -> None:
    context_adjustments = [
        {"from": 2048, "to": 8192, "reason": "grow"},
        {"from": 21000, "to": 7900, "reason": "truncate_oldest", "dropped_messages": 10},
    ]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        front.horde.status_payload["context_adjustments"] = context_adjustments
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"context_fit": "truncate_oldest"}))

    assert response.status_code == 200
    assert front.horde.submitted_body()["horde"]["context_fit"] == "truncate_oldest"
    assert response.json()["x_horde"]["context_adjustments"] == context_adjustments


async def test_an_unknown_conformance_mode_is_refused_before_submitting(start_front: StartFront) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"conformance": "lenient"}))

    assert response.status_code == 400
    assert front.horde.submit_route.call_count == 0


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


@pytest.mark.parametrize("stream", [False, True])
async def test_system_fingerprint_follows_the_serving_worker_and_model(start_front: StartFront, stream: bool) -> None:
    """Every response and chunk carries one ``fp_`` fingerprint, stable for a worker and model and different for
    another worker."""
    fingerprints: list[set[str]] = []
    for worker_id in ("worker-1", "worker-1", "worker-2"):
        async with start_front() as front:
            front.horde.status_payload = done_status()
            front.horde.status_payload["generations"][0]["worker_id"] = worker_id
            response = await front.client.post(
                "/v1/chat/completions",
                json=build_chat_body(stream=stream, **({"stream_options": {"include_usage": True}} if stream else {})),
            )
        assert response.status_code == 200
        if stream:
            chunks = [json.loads(payload) for payload in data_payloads(parse_sse_events(response.text))[:-1]]
            fingerprints.append({chunk["system_fingerprint"] for chunk in chunks})
        else:
            fingerprints.append({response.json()["system_fingerprint"]})

    assert all(len(response_fingerprints) == 1 for response_fingerprints in fingerprints)
    first, repeated, other_worker = (response_fingerprints.pop() for response_fingerprints in fingerprints)
    assert first.startswith("fp_")
    assert first == repeated
    assert first != other_worker


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
    assert horde_headers["client-agent"].endswith(":test-client/1.0 via horde-oai-front")
    assert "proxy-authorization" not in horde_headers
    assert "proxied-for" not in horde_headers


async def test_the_client_agent_attributes_the_request_to_the_titled_application(start_front: StartFront) -> None:
    headers = {
        "User-Agent": "test-client/1.0",
        "HTTP-Referer": "https://chat.example.org/",
        "X-Title": "Example Chat",
    }
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(), headers=headers)

    assert response.status_code == 200
    assert front.horde.submit_route.calls.last.request.headers["client-agent"] == "Example Chat:0:test-client/1.0 via horde-oai-front"


async def test_proxy_passkey_forwards_the_client_address(start_front: StartFront) -> None:
    passkey_settings = FAST_TEST_SETTINGS.model_copy(update={"proxy_passkey": "operator-passkey"})
    async with start_front(passkey_settings) as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    for horde_call in (front.horde.submit_route.calls.last, front.horde.status_route.calls.last):
        assert horde_call.request.headers["proxy-authorization"] == "operator-passkey"
        assert horde_call.request.headers["proxied-for"] == "127.0.0.1"


VISION_MODEL_NAME: str = "aphrodite/vision-model"
"""A model whose online chat worker declares ``vision``."""

MODEL_LIST: list[dict[str, object]] = [
    {
        "name": MODEL_NAME,
        "count": 4,
        "performance": 10.0,
        "queued": 7,
        "jobs": 0,
        "eta": 0,
        "type": "text",
        "chat": {
            "workers": 3,
            "max_context_length": 8192,
            "max_length": 1024,
            "features": ["frequency_penalty", "seed"],
            "created": 1700000000,
        },
    },
    {
        "name": VISION_MODEL_NAME,
        "count": 1,
        "type": "text",
        "chat": {"workers": 1, "max_context_length": 4096, "max_length": 512, "features": ["vision"], "created": None},
    },
    {"name": "aphrodite/text-only-model", "count": 2, "queued": 1, "type": "text"},
    {
        "name": "aphrodite/no-chat-workers",
        "count": 1,
        "type": "text",
        "chat": {"workers": 0, "max_context_length": None, "max_length": None, "features": [], "created": None},
    },
]
"""The Horde's chat model list the model route tests serve, with two entries no online chat worker serves."""


async def test_models_lists_the_chat_models_in_the_openai_shape(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        response = await front.client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": 1700000000,
                "owned_by": "aihorde",
                "context_length": 8192,
                "architecture": {"modality": "text->text", "input_modalities": ["text"], "output_modalities": ["text"]},
                "top_provider": {"context_length": 8192, "max_completion_tokens": 1024},
                "supported_parameters": ["max_tokens", "temperature", "top_p", "stop", "seed", "frequency_penalty"],
                "x_horde": {"workers": 3, "queued": 7},
            },
            {
                "id": VISION_MODEL_NAME,
                "object": "model",
                "created": 0,
                "owned_by": "aihorde",
                "context_length": 4096,
                "architecture": {
                    "modality": "text+image->text",
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
                "top_provider": {"context_length": 4096, "max_completion_tokens": 512},
                "supported_parameters": ["max_tokens", "temperature", "top_p", "stop"],
                "x_horde": {"workers": 1},
            },
        ],
    }
    assert front.horde.models_route.calls.last.request.url == f"{HORDE_URL}/v2/status/models?type=text&chat=true"


async def test_model_route_does_not_find_a_model_without_online_chat_workers(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.models_route.respond(200, json=MODEL_LIST)
        text_only = await front.client.get("/v1/models/aphrodite/text-only-model")
        no_chat_workers = await front.client.get("/v1/models/aphrodite/no-chat-workers")

    assert text_only.status_code == 404
    assert no_chat_workers.status_code == 404


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


TOOL_CALLS: list[dict[str, object]] = [
    {"id": "call_a", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}},
    {"id": "call_b", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Oslo"}'}},
]
"""Two tool calls a worker returns in one reply."""


def tool_call_status(*, content: str | None = None) -> dict[str, object]:
    """Return a finished status whose one generation answers with ``TOOL_CALLS`` and ``finish_reason: tool_calls``."""
    status = done_status(finish_reason="tool_calls", native_finish_reason="tool_calls")
    status["generations"][0]["text"] = ""
    status["generations"][0]["message"] = {"role": "assistant", "content": content, "tool_calls": TOOL_CALLS}
    return status


async def test_a_tool_call_reply_returns_the_calls_with_null_content(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = tool_call_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 200
    [choice] = response.json()["choices"]
    assert choice["message"] == {"role": "assistant", "content": None, "tool_calls": TOOL_CALLS}
    assert choice["finish_reason"] == "tool_calls"
    assert "notice" not in response.json()["x_horde"]


async def test_a_reply_with_text_and_tool_calls_returns_both(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = tool_call_status(content="Checking both cities.")
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.json()["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Checking both cities.",
        "tool_calls": TOOL_CALLS,
    }


async def test_a_reply_without_tool_calls_has_no_tool_calls_key(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert "tool_calls" not in response.json()["choices"][0]["message"]


async def test_a_browser_preflight_is_answered_for_any_origin(start_front: StartFront) -> None:
    """A browser client sends a preflight before posting with a bearer key; the front answers it for every origin."""
    async with start_front() as front:
        preflight = await front.client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "https://lite.koboldai.net",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        response = await front.client.get("/v1/models", headers={"Origin": "https://lite.koboldai.net"})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
    assert "POST" in preflight.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-origin"] == "*"
    assert "Retry-After" in response.headers["access-control-expose-headers"]


@pytest.mark.parametrize("content", [None, "Checking both cities."])
async def test_a_streamed_tool_call_reply_sends_indexed_calls_in_their_own_chunk(start_front: StartFront, content: str | None) -> None:
    """The tool-call chunk carries no content: clients that read tool calls only from content-free chunks must see them."""
    chat_body = build_chat_body(stream=True, stream_options={"include_usage": True})
    async with start_front() as front:
        front.horde.status_payload = tool_call_status(content=content)
        response = await front.client.post("/v1/chat/completions", json=chat_body)

    payloads = data_payloads(parse_sse_events(response.text))
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(payload) for payload in payloads[:-1]]
    role_chunk, *body_chunks, finish_chunk, usage_chunk = chunks
    assert role_chunk["choices"][0]["delta"] == {"role": "assistant"}
    expected_body_deltas: list[dict[str, object]] = []
    if content is not None:
        expected_body_deltas.append({"content": content})
    expected_body_deltas.append(
        {"tool_calls": [{"index": call_index, **tool_call} for call_index, tool_call in enumerate(TOOL_CALLS)]},
    )
    assert [body_chunk["choices"][0]["delta"] for body_chunk in body_chunks] == expected_body_deltas
    assert finish_chunk["choices"][0]["delta"] == {}
    assert finish_chunk["choices"][0]["finish_reason"] == "tool_calls"
    assert "notice" not in finish_chunk["x_horde"]
    assert usage_chunk["choices"] == []


async def test_models_list_tool_and_response_format_parameters_from_declared_features(start_front: StartFront) -> None:
    model_list = [
        {"name": "tools-model", "chat": {"workers": 1, "features": ["tools"]}},
        {"name": "json-object-model", "chat": {"workers": 1, "features": ["json_object"]}},
        {
            "name": "json-schema-model",
            "chat": {
                "workers": 1,
                "features": ["json_schema", "seed", "single_tool_call_enforced", "tool_choice_enforced", "tools"],
            },
        },
    ]
    async with start_front() as front:
        front.horde.models_route.respond(200, json=model_list)
        response = await front.client.get("/v1/models")

    supported_by_model = {model["id"]: model["supported_parameters"] for model in response.json()["data"]}
    assert supported_by_model == {
        "tools-model": ["max_tokens", "temperature", "top_p", "stop", "tools"],
        "json-object-model": ["max_tokens", "temperature", "top_p", "stop", "response_format"],
        "json-schema-model": [
            "max_tokens",
            "temperature",
            "top_p",
            "stop",
            "seed",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
        ],
    }


@pytest.mark.parametrize(
    ("declared_features", "expected_tool_parameters"),
    [
        (["tools"], ["tools"]),
        (["tools", "tool_choice_enforced"], ["tools", "tool_choice"]),
        (["tools", "single_tool_call_enforced"], ["tools", "parallel_tool_calls"]),
        (["tool_choice_enforced", "single_tool_call_enforced"], []),
    ],
    ids=["capability-only", "tool-choice-guarantee", "single-call-guarantee", "guarantees-without-tools"],
)
async def test_models_list_a_tool_constraint_only_with_its_declared_guarantee(
    start_front: StartFront,
    declared_features: list[str],
    expected_tool_parameters: list[str],
) -> None:
    model_list = [{"name": "tools-model", "chat": {"workers": 1, "features": declared_features}}]
    async with start_front() as front:
        front.horde.models_route.respond(200, json=model_list)
        response = await front.client.get("/v1/models")

    (model_card,) = response.json()["data"]
    assert model_card["supported_parameters"] == ["max_tokens", "temperature", "top_p", "stop", *expected_tool_parameters]


REASONING_TEXT: str = "The user wants a greeting."
"""The reasoning a worker submitted beside the reply."""


@pytest.mark.parametrize("include_reasoning", [True, False])
async def test_reasoning_is_returned_on_the_message_only_when_the_client_asks_for_it(
    start_front: StartFront,
    include_reasoning: bool,
) -> None:
    """OpenAI's chat API does not return reasoning, so the front hides it unless ``horde.include_reasoning`` is set."""
    async with start_front() as front:
        front.horde.status_payload = done_status(reasoning_content=REASONING_TEXT)
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(horde={"include_reasoning": include_reasoning}),
        )

    assert response.status_code == 200
    expected_message: dict[str, object] = {"role": "assistant", "content": GENERATED_TEXT}
    if include_reasoning:
        expected_message["reasoning_content"] = REASONING_TEXT
    assert response.json()["choices"][0]["message"] == expected_message


async def test_models_is_forwarded_unchanged_and_the_response_reports_the_model_that_served(start_front: StartFront) -> None:
    """A request listing acceptable models is forwarded with the list as sent, and the response ``model`` and
    fingerprint are those of the listed model a worker served it with."""
    served_model = "koboldcpp/Other-Model"
    async with start_front() as front:
        front.horde.status_payload = done_status(model=served_model, worker_id="worker-1")
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(model=None, models=[MODEL_NAME, served_model]),
        )

    assert response.status_code == 200
    submitted_body = front.horde.submitted_body()
    assert submitted_body["models"] == [MODEL_NAME, served_model]
    assert "model" not in submitted_body
    assert response.json()["model"] == served_model
    assert response.json()["system_fingerprint"] == build_system_fingerprint("worker-1", served_model)


async def test_include_reasoning_is_forwarded_to_the_native_route(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"include_reasoning": True}))

    assert response.status_code == 200
    assert front.horde.submitted_body()["horde"]["include_reasoning"] is True


@pytest.mark.parametrize("include_reasoning", [True, False])
async def test_reported_reasoning_tokens_appear_in_the_usage_details_whether_or_not_reasoning_is_returned(
    start_front: StartFront,
    include_reasoning: bool,
) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status(reasoning_content=REASONING_TEXT, completion_tokens=40, reasoning_tokens=34)
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(horde={"include_reasoning": include_reasoning}),
        )

    assert response.json()["usage"] == {
        "prompt_tokens": 21,
        "completion_tokens": 40,
        "total_tokens": 61,
        "completion_tokens_details": {"reasoning_tokens": 34},
    }


async def test_usage_omits_completion_details_when_the_worker_did_not_report_reasoning_tokens(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status(reasoning_content=REASONING_TEXT)
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(horde={"include_reasoning": True}))

    assert "completion_tokens_details" not in response.json()["usage"]


@pytest.mark.parametrize("include_reasoning", [True, False])
async def test_a_streamed_reply_sends_the_reasoning_chunk_before_the_content_only_when_asked_for(
    start_front: StartFront,
    include_reasoning: bool,
) -> None:
    chat_body = build_chat_body(stream=True, stream_options={"include_usage": True}, horde={"include_reasoning": include_reasoning})
    async with start_front() as front:
        front.horde.status_payload = done_status(reasoning_content=REASONING_TEXT, reasoning_tokens=3)
        response = await front.client.post("/v1/chat/completions", json=chat_body)

    payloads = data_payloads(parse_sse_events(response.text))
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(payload) for payload in payloads[:-1]]
    expected_deltas: list[dict[str, object]] = [{"role": "assistant"}]
    if include_reasoning:
        expected_deltas.append({"reasoning_content": REASONING_TEXT})
    expected_deltas.extend([{"content": GENERATED_TEXT}, {}])
    assert [chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"]] == expected_deltas
    assert chunks[-1]["usage"]["completion_tokens_details"] == {"reasoning_tokens": 3}
