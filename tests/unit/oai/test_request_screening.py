# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's request screening: the default no-op table, refusals by name, and contract limits."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from horde.chat_contract import MAX_CHAT_IMAGE_BYTES, MAX_CHAT_IMAGE_COUNT
from tests.unit.oai.conftest import FAST_TEST_SETTINGS, StartFront, build_chat_body, done_status

pytestmark = pytest.mark.asyncio

TINY_PNG_DATA_URL: str = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
"""A one-pixel PNG as a data URL."""


def image_part(url: str, **image_url_extras: Any) -> dict[str, Any]:
    """Return an ``image_url`` content part for ``url``."""
    return {"type": "image_url", "image_url": {"url": url, **image_url_extras}}


def user_image_messages(*image_parts: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a system message and a user message holding a text part and ``image_parts``."""
    return [
        {"role": "system", "content": "Describe."},
        {"role": "user", "content": [{"type": "text", "text": "What is this?"}, *image_parts]},
    ]


NO_OP_ROWS: list[tuple[str, object]] = [
    ("tools", []),
    ("tools", None),
    ("tool_choice", None),
    ("tool_choice", "none"),
    ("response_format", {"type": "text"}),
    ("logprobs", False),
    ("logprobs", None),
    ("top_logprobs", 0),
    ("top_logprobs", None),
    ("parallel_tool_calls", True),
    ("parallel_tool_calls", False),
    ("store", False),
    ("service_tier", "auto"),
    ("service_tier", "default"),
]
"""Every accepted value of the default no-op table."""


@pytest.mark.parametrize(("parameter", "value"), NO_OP_ROWS)
async def test_default_no_op_is_accepted_and_not_forwarded(start_front: StartFront, parameter: str, value: object) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**{parameter: value}))

    assert response.status_code == 200
    assert parameter not in front.horde.submitted_body()


@pytest.mark.parametrize(
    ("body_overrides", "expected_param"),
    [
        ({"tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}, "tools"),
        ({"tool_choice": "auto"}, "tool_choice"),
        ({"response_format": {"type": "json_object"}}, "response_format"),
        ({"logprobs": True}, "logprobs"),
        ({"top_logprobs": 2}, "top_logprobs"),
        ({"top_logprobs": False}, "top_logprobs"),
        ({"parallel_tool_calls": True, "tools": [{"type": "function"}]}, "parallel_tool_calls"),
        ({"store": True}, "store"),
        ({"service_tier": "flex"}, "service_tier"),
        ({"logit_bias": {"50256": -100}}, "logit_bias"),
        ({"functions": []}, "functions"),
        ({"function_call": "none"}, "function_call"),
        ({"audio": {"voice": "alloy"}}, "audio"),
        ({"modalities": ["text"]}, "modalities"),
        ({"prediction": {"type": "content", "content": "x"}}, "prediction"),
        ({"web_search_options": {}}, "web_search_options"),
        ({"models": ["a", "b"]}, "models"),
        ({"prompt_cache_key": "k"}, "prompt_cache_key"),
        ({"session_id": "s"}, "session_id"),
        ({"some_future_key": 1}, "some_future_key"),
        ({"messages": [{"role": "tool", "content": "x", "tool_call_id": "1"}]}, "messages[0].tool_call_id"),
        ({"messages": [{"role": "tool", "content": "x"}]}, "messages[0].role"),
        ({"messages": [{"role": "assistant", "content": "", "tool_calls": []}]}, "messages[0].tool_calls"),
        (
            {
                "messages": [
                    {"role": "system", "content": "Describe."},
                    {"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]},
                ],
            },
            "messages[1].content[0].type=image_url",
        ),
        (
            {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}]}]},
            "messages[0].content[0].type=input_audio",
        ),
        (
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "file", "file": {"file_id": "f"}}]}]},
            "messages[0].content[1].type=file",
        ),
        ({"horde": {"dry_run": True}}, "horde.dry_run"),
        ({"horde": {"operation_key": "00000000-0000-4000-8000-000000000002"}}, "horde.operation_key"),
        ({"horde": {"workers": ["w"]}}, "horde.workers"),
        ({"n": 2}, "n"),
        ({"max_tokens": 8}, "max_tokens"),
        ({"max_tokens": 5000}, "max_tokens"),
        ({"max_completion_tokens": 5000, "max_tokens": None}, "max_completion_tokens"),
        ({"stop": ["a", "b", "c", "d", "e"]}, "stop"),
    ],
)
async def test_unsupported_parameter_is_refused_with_param_before_submitting(
    start_front: StartFront,
    body_overrides: dict[str, Any],
    expected_param: str,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "UnsupportedChatParameter"
    assert error["type"] == "invalid_request_error"
    assert error["param"] == expected_param
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize(
    ("body_overrides", "expected_param"),
    [
        ({"max_tokens": 64, "max_completion_tokens": 128}, "max_completion_tokens"),
        ({"horde": {"only": ["a"], "ignore": ["b"]}}, "horde.ignore"),
    ],
)
async def test_contradictory_values_are_refused_as_a_conflict(
    start_front: StartFront,
    body_overrides: dict[str, Any],
    expected_param: str,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ChatRequestConflict"
    assert response.json()["error"]["param"] == expected_param
    assert front.horde.submit_route.call_count == 0


async def test_agreeing_token_limits_and_n_of_one_are_accepted(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(max_tokens=128, max_completion_tokens=128, n=1, stop=["a", "b", "c", "d"]),
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("body_overrides", "expected_param"),
    [
        ({"messages": []}, "messages"),
        ({"stream_options": {"include_usage": True}}, "stream_options"),
        ({"stream": True, "stream_options": {"include_obfuscation": True}}, "stream_options.include_obfuscation"),
        ({"temperature": "hot"}, None),
        ({"user": "u" * 300}, "user"),
        ({"horde": {"context_fit": "truncate_oldest"}}, "horde.context_fit"),
    ],
)
async def test_invalid_values_are_bad_requests(
    start_front: StartFront,
    body_overrides: dict[str, Any],
    expected_param: str | None,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "BadRequest"
    if expected_param is not None:
        assert error["param"] == expected_param
    else:
        assert error["param"].startswith("temperature")
    assert front.horde.submit_route.call_count == 0


async def test_model_id_with_a_variant_suffix_is_not_found(start_front: StartFront) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(model="koboldcpp/Tiefighter:free"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize(
    "settings_update",
    [
        {"max_message_count": 1},
        {"max_text_bytes": 10},
    ],
)
async def test_message_count_and_text_size_limits_refuse_before_submitting(
    start_front: StartFront, settings_update: dict[str, int]
) -> None:
    async with start_front(FAST_TEST_SETTINGS.model_copy(update=settings_update)) as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UnsupportedChatParameter"
    assert response.json()["error"]["param"] == "messages"
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize("role", ["system", "developer", "assistant"])
async def test_an_image_part_outside_a_user_message_is_refused_with_its_path(start_front: StartFront, role: str) -> None:
    messages = [{"role": role, "content": [image_part(TINY_PNG_DATA_URL)]}, {"role": "user", "content": "Hi."}]
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UnsupportedChatParameter"
    assert response.json()["error"]["param"] == "messages[0].content[0].type=image_url"
    assert front.horde.submit_route.call_count == 0


async def test_user_image_parts_are_forwarded_verbatim(start_front: StartFront) -> None:
    messages = user_image_messages(
        image_part(TINY_PNG_DATA_URL, detail="low"),
        image_part("https://example.test/cat.png"),
    )
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"] == messages


async def test_image_data_urls_do_not_count_toward_the_text_size_limit(start_front: StartFront) -> None:
    messages = user_image_messages(image_part(TINY_PNG_DATA_URL))
    text_bytes = len(b"Describe.") + len(b"What is this?")
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_text_bytes": text_bytes})) as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 200


async def test_an_image_data_url_over_the_native_cap_is_refused_with_its_path(start_front: StartFront) -> None:
    oversized_payload = base64.b64encode(bytes(MAX_CHAT_IMAGE_BYTES + 3)).decode("ascii")
    messages = user_image_messages(image_part(TINY_PNG_DATA_URL), image_part(f"data:image/png;base64,{oversized_payload}"))
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "SourceImageSizeExceeded"
    assert error["param"] == "messages[1].content[2].image_url.url"
    assert front.horde.submit_route.call_count == 0


async def test_an_image_data_url_at_the_native_cap_is_forwarded(start_front: StartFront) -> None:
    # A length divisible by three encodes without padding, so the size estimate equals the decoded size.
    payload_at_cap = base64.b64encode(bytes(MAX_CHAT_IMAGE_BYTES - MAX_CHAT_IMAGE_BYTES % 3)).decode("ascii")
    messages = user_image_messages(image_part(f"data:image/png;base64,{payload_at_cap}"))
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 200


async def test_more_image_parts_than_the_native_cap_are_refused(start_front: StartFront) -> None:
    messages = user_image_messages(*(image_part(TINY_PNG_DATA_URL) for _ in range(MAX_CHAT_IMAGE_COUNT + 1)))
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "TooManyExtraSourceImages"
    assert error["param"] == "messages"
    assert front.horde.submit_route.call_count == 0


async def test_an_image_part_with_an_unknown_detail_is_a_bad_request(start_front: StartFront) -> None:
    messages = user_image_messages(image_part(TINY_PNG_DATA_URL, detail="ultra"))
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BadRequest"
    assert response.json()["error"]["param"].startswith("messages[1].content")
    assert front.horde.submit_route.call_count == 0
