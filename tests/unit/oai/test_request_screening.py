# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's request screening: the default no-op table, refusals by name, dropped message keys, and
contract limits."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from horde.chat_contract import MAX_CHAT_IMAGE_BYTES, MAX_CHAT_IMAGE_COUNT, MAX_CHAT_SCHEMA_NESTING
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


WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Read the current weather.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
"""A function tool as the OpenAI SDK sends it."""

WEATHER_CALL: dict[str, Any] = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
}
"""An assistant tool call answering [`WEATHER_TOOL`][tests.unit.oai.test_request_screening.WEATHER_TOOL]."""

NO_OP_ROWS: list[tuple[str, object]] = [
    ("tools", []),
    ("tools", None),
    ("response_format", {"type": "text"}),
    ("logprobs", False),
    ("logprobs", None),
    ("top_logprobs", 0),
    ("top_logprobs", None),
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
        ({"logprobs": True}, "logprobs"),
        ({"top_logprobs": 2}, "top_logprobs"),
        ({"top_logprobs": False}, "top_logprobs"),
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
        ({"messages": [{"role": "user", "content": "x", "tool_calls": [WEATHER_CALL]}]}, "messages[0].tool_calls"),
        ({"messages": [{"role": "system", "content": "x", "tool_calls": [WEATHER_CALL]}]}, "messages[0].tool_calls"),
        (
            {"messages": [{"role": "tool", "content": "x", "tool_call_id": "call_1", "tool_calls": [WEATHER_CALL]}]},
            "messages[0].tool_calls",
        ),
        ({"messages": [{"role": "function", "content": "x", "name": "f"}]}, "messages[0].role"),
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
    ("body_overrides", "expected_param", "expected_code", "expected_message"),
    [
        (
            {"temperature": 2.5},
            "temperature",
            "UnsupportedChatParameter",
            "Invalid 'temperature': decimal above maximum value. Expected a value <= 2, but got 2.5 instead.",
        ),
        (
            {"temperature": -0.5},
            "temperature",
            "UnsupportedChatParameter",
            "Invalid 'temperature': decimal below minimum value. Expected a value >= 0, but got -0.5 instead.",
        ),
        (
            {"top_p": 1.5},
            "top_p",
            "UnsupportedChatParameter",
            "Invalid 'top_p': decimal above maximum value. Expected a value <= 1, but got 1.5 instead.",
        ),
        (
            {"top_p": -0.1},
            "top_p",
            "UnsupportedChatParameter",
            "Invalid 'top_p': decimal below minimum value. Expected a value >= 0, but got -0.1 instead.",
        ),
        (
            {"presence_penalty": 2.5},
            "presence_penalty",
            "UnsupportedChatParameter",
            "Invalid 'presence_penalty': decimal above maximum value. Expected a value <= 2, but got 2.5 instead.",
        ),
        (
            {"presence_penalty": -2.5},
            "presence_penalty",
            "UnsupportedChatParameter",
            "Invalid 'presence_penalty': decimal below minimum value. Expected a value >= -2, but got -2.5 instead.",
        ),
        (
            {"frequency_penalty": 2.5},
            "frequency_penalty",
            "UnsupportedChatParameter",
            "Invalid 'frequency_penalty': decimal above maximum value. Expected a value <= 2, but got 2.5 instead.",
        ),
        (
            {"frequency_penalty": -2.5},
            "frequency_penalty",
            "UnsupportedChatParameter",
            "Invalid 'frequency_penalty': decimal below minimum value. Expected a value >= -2, but got -2.5 instead.",
        ),
        (
            {"n": 0},
            "n",
            "UnsupportedChatParameter",
            "Invalid 'n': integer below minimum value. Expected a value >= 1, but got 0 instead.",
        ),
        (
            {"stop": ["a", "b", "c", "d", "e"]},
            "stop",
            "UnsupportedChatParameter",
            "Invalid 'stop': array too long. Expected an array with maximum length 4, but got an array with length 5 instead.",
        ),
        (
            {"max_tokens": 0},
            "max_tokens",
            "UnsupportedChatParameter",
            "Invalid 'max_tokens': integer below minimum value. Expected a value >= 1, but got 0 instead.",
        ),
        (
            {"messages": []},
            "messages",
            "BadRequest",
            "Invalid 'messages': empty array. Expected an array with minimum length 1, but got an empty array instead.",
        ),
    ],
)
async def test_value_outside_openai_range_is_refused_with_openai_message_before_submitting(
    start_front: StartFront,
    body_overrides: dict[str, Any],
    expected_param: str,
    expected_code: str,
    expected_message: str,
) -> None:
    """A value OpenAI refuses is refused with OpenAI's message and ``param``, without a path prefix, and is not
    forwarded."""
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == expected_message
    assert error["param"] == expected_param
    assert error["code"] == expected_code
    assert error["type"] == "invalid_request_error"
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize(
    "body_overrides",
    [
        {"temperature": 0},
        {"temperature": 2},
        {"top_p": 0},
        {"top_p": 1},
        {"presence_penalty": -2},
        {"presence_penalty": 2},
        {"frequency_penalty": -2},
        {"frequency_penalty": 2},
        {"n": 1},
        {"stop": ["a", "b", "c", "d"]},
        {"max_tokens": 16},
        {"messages": [{"role": "user", "content": "Say hello."}]},
    ],
)
async def test_value_at_the_edge_of_its_range_is_forwarded(start_front: StartFront, body_overrides: dict[str, Any]) -> None:
    """A value on the boundary of its accepted range is accepted and submitted."""
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 200
    assert front.horde.submit_route.call_count == 1


@pytest.mark.parametrize("token_field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("token_limit", [1, 15])
async def test_token_limit_openai_accepts_below_the_horde_floor_is_refused_with_the_floor(
    start_front: StartFront,
    token_field: str,
    token_limit: int,
) -> None:
    """A token limit from 1 to 15 is refused, not raised, with a message stating the floor of 16 and its reason."""
    body_overrides: dict[str, Any] = {"max_tokens": None, token_field: token_limit}
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["param"] == token_field
    assert error["code"] == "UnsupportedChatParameter"
    assert error["message"] == (
        f"Invalid '{token_field}': the AI Horde refuses a token limit below 16, the minimum generation length of its "
        f"native text API, rather than raising it, but got {token_limit} instead."
    )
    assert front.horde.submit_route.call_count == 0


@pytest.mark.parametrize(
    ("body_overrides", "expected_param"),
    [
        ({"messages": []}, "messages"),
        ({"stream_options": {"include_usage": True}}, "stream_options"),
        ({"stream": True, "stream_options": {"include_obfuscation": True}}, "stream_options.include_obfuscation"),
        ({"temperature": "hot"}, None),
        ({"user": "u" * 300}, "user"),
        ({"horde": {"context_fit": "truncate_oldest"}}, "horde.context_fit"),
        ({"messages": [{"role": "assistant", "content": None}]}, "messages[0].content"),
        ({"messages": [{"role": "assistant"}]}, "messages[0].content"),
        ({"messages": [{"role": "user", "content": None}]}, "messages[0].content"),
        ({"messages": [{"role": "assistant", "content": "", "tool_calls": []}]}, "messages[0].tool_calls"),
        ({"messages": [{"role": "tool", "content": "72F"}]}, "messages[0].tool_call_id"),
        ({"messages": [{"role": "tool", "content": "72F", "name": "get_weather"}]}, "messages[0].tool_call_id"),
        ({"messages": [{"role": "user", "content": 7, "x_client_note": "n"}]}, "messages[0].content"),
        (
            {
                "messages": [
                    {"role": "user", "content": "Weather in Paris?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{**WEATHER_CALL, "index": 0, "function": {"name": "get_weather", "arguments": {"city": "Paris"}}}],
                    },
                ],
            },
            "messages[1].tool_calls[0].function.arguments",
        ),
        ({"tools": [{"type": "function"}]}, "tools[0].function"),
        ({"tools": [{"type": "retrieval", "function": {"name": "f"}}]}, "tools[0].type"),
        ({"tool_choice": "sometimes"}, "tool_choice"),
        ({"response_format": {"type": "json_schema", "json_schema": {"name": "answer"}}}, "response_format.json_schema.schema"),
        ({"parallel_tool_calls": "yes", "tools": [WEATHER_TOOL]}, "parallel_tool_calls"),
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


STRUCTURED_OUTPUT_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "colour",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"colour": {"type": "string", "default": None}},
            "required": ["colour"],
            "additionalProperties": False,
        },
    },
}
"""A structured output format as the OpenAI SDK sends it, with a null inside the schema the worker has to receive."""


async def test_tools_tool_choice_and_parallel_tool_calls_are_forwarded_verbatim(start_front: StartFront) -> None:
    tool_choice = {"type": "function", "function": {"name": "get_weather"}}
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(tools=[WEATHER_TOOL], tool_choice=tool_choice, parallel_tool_calls=False),
        )

    assert response.status_code == 200
    submitted_body = front.horde.submitted_body()
    assert submitted_body["tools"] == [WEATHER_TOOL]
    assert submitted_body["tool_choice"] == tool_choice
    assert submitted_body["parallel_tool_calls"] is False


@pytest.mark.parametrize("tool_choice", ["none", "auto", "required"])
async def test_a_tool_choice_mode_is_forwarded_with_tools(start_front: StartFront, tool_choice: str) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[WEATHER_TOOL], tool_choice=tool_choice))

    assert response.status_code == 200
    assert front.horde.submitted_body()["tool_choice"] == tool_choice


@pytest.mark.parametrize(
    ("body_overrides", "refused_key"),
    [
        ({"tool_choice": "none"}, "tool_choice"),
        ({"tool_choice": "auto"}, "tool_choice"),
        ({"tools": [], "tool_choice": "none"}, "tool_choice"),
        ({"parallel_tool_calls": True}, "parallel_tool_calls"),
        ({"parallel_tool_calls": False}, "parallel_tool_calls"),
    ],
)
async def test_a_tool_key_without_tools_is_refused_as_openai_refuses_it(
    start_front: StartFront,
    body_overrides: dict[str, Any],
    refused_key: str,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    assert response.json()["error"] == {
        "message": f"Invalid value for '{refused_key}': '{refused_key}' is only allowed when 'tools' are specified.",
        "type": "invalid_request_error",
        "param": refused_key,
        "code": "ChatRequestConflict",
        "metadata": {},
    }
    assert front.horde.submit_route.call_count == 0


async def test_null_tool_keys_without_tools_are_accepted_and_not_forwarded(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tool_choice=None, parallel_tool_calls=None))

    assert response.status_code == 200
    assert not {"tool_choice", "parallel_tool_calls"} & set(front.horde.submitted_body())


def _weather_call(call_id: str) -> dict[str, Any]:
    return {**WEATHER_CALL, "id": call_id}


def _tool_result(call_id: str) -> dict[str, Any]:
    return {"role": "tool", "content": "18C", "tool_call_id": call_id}


QUESTION: dict[str, Any] = {"role": "user", "content": "Weather in Paris and Rome?"}
"""The user turn that opens each tool history below."""


@pytest.mark.parametrize(
    ("messages", "expected_param", "expected_message"),
    [
        (
            [QUESTION, {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]}, {"role": "user", "content": "Well?"}],
            "messages[2].role",
            "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. "
            "The following tool_call_ids did not have response messages: call_1",
        ),
        (
            [
                QUESTION,
                {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]},
                _tool_result("call_1"),
                _tool_result("call_1"),
            ],
            "messages[3].role",
            "Invalid parameter: messages with role 'tool' must be a response to a preceding message with 'tool_calls'.",
        ),
        (
            [QUESTION, {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]}, _tool_result("call_nope")],
            "messages[2].tool_call_id",
            "Invalid parameter: 'tool_call_id' of 'call_nope' not found in 'tool_calls' of previous message.",
        ),
    ],
    ids=["unanswered-call", "second-answer", "unknown-id"],
)
async def test_a_tool_history_openai_refuses_is_refused_with_its_message_and_param(
    start_front: StartFront,
    messages: list[dict[str, Any]],
    expected_param: str,
    expected_message: str,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["message"], error["param"], error["code"]) == (expected_message, expected_param, "UnsupportedChatParameter")
    assert front.horde.submit_route.call_count == 0


async def test_answers_to_every_call_in_any_order_are_forwarded(start_front: StartFront) -> None:
    messages = [
        QUESTION,
        {"role": "assistant", "content": None, "tool_calls": [_weather_call("call_1"), _weather_call("call_2")]},
        _tool_result("call_2"),
        _tool_result("call_1"),
    ]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 200
    assert len(front.horde.submitted_body()["messages"]) == 4


async def test_a_strict_schema_breaking_openais_structural_rules_is_refused(start_front: StartFront) -> None:
    open_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "n",
            "strict": True,
            "schema": {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]},
        },
    }
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(response_format=open_format))

    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "Invalid schema for response_format 'n': In context=(), 'additionalProperties' is required to be supplied and to be false."
    )
    assert response.json()["error"]["param"] == "response_format"
    assert front.horde.submit_route.call_count == 0


def _weather_tool(parameters: dict[str, Any], *, strict: bool | None = True) -> dict[str, Any]:
    function: dict[str, Any] = {"name": "get_weather", "parameters": parameters}
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


@pytest.mark.parametrize(
    ("parameters", "expected_message"),
    [
        (
            {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]},
            "Invalid schema for function 'get_weather': In context=(), 'additionalProperties' is required to be supplied and to be false.",
        ),
        (
            {
                "type": "object",
                "properties": {"location": {"type": "string"}, "unit": {"type": "string"}},
                "required": ["location"],
                "additionalProperties": False,
            },
            "Invalid schema for function 'get_weather': In context=(), 'required' is required to be supplied and to be an array "
            "including every key in properties. Missing 'unit'.",
        ),
    ],
    ids=["open-object", "optional-property"],
)
async def test_a_strict_tool_breaking_openais_structural_rules_is_refused_with_its_message_and_param(
    start_front: StartFront,
    parameters: dict[str, Any],
    expected_message: str,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[_weather_tool(parameters)]))

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["message"], error["param"], error["code"]) == (
        expected_message,
        "tools[0].function.parameters",
        "UnsupportedChatParameter",
    )
    assert front.horde.submit_route.call_count == 0


async def test_a_closed_strict_tool_is_forwarded_with_its_strict_flag(start_front: StartFront) -> None:
    parameters = {
        "type": "object",
        "properties": {"location": {"type": "string"}, "unit": {"type": "string"}},
        "required": ["location", "unit"],
        "additionalProperties": False,
    }
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[_weather_tool(parameters)]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["tools"][0]["function"]["strict"] is True


@pytest.mark.parametrize("strict", [False, None], ids=["strict-false", "strict-unset"])
async def test_a_non_strict_tool_with_an_optional_property_is_forwarded(start_front: StartFront, strict: bool | None) -> None:
    parameters = {"type": "object", "properties": {"location": {"type": "string"}, "unit": {"type": "string"}}}
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[_weather_tool(parameters, strict=strict)]))

    assert response.status_code == 200
    assert front.horde.submit_route.call_count == 1


def _nested_schema(levels: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(levels):
        schema = {"type": "object", "properties": {"x": schema}}
    return schema


@pytest.mark.parametrize(
    ("body_overrides", "expected_param"),
    [
        (
            {"tools": [{"type": "function", "function": {"name": "deep", "parameters": _nested_schema(300)}}]},
            "tools[0].function.parameters",
        ),
        (
            {"response_format": {"type": "json_schema", "json_schema": {"name": "deep", "schema": _nested_schema(300)}}},
            "response_format.json_schema.schema",
        ),
    ],
    ids=["tool-parameters", "response-schema"],
)
async def test_a_schema_nested_past_the_limit_is_a_400_naming_it(
    start_front: StartFront,
    body_overrides: dict[str, Any],
    expected_param: str,
) -> None:
    async with start_front() as front:
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(**body_overrides))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "UnsupportedChatParameter"
    assert error["param"] == expected_param
    assert f"at most {MAX_CHAT_SCHEMA_NESTING} levels" in error["message"]
    assert front.horde.submit_route.call_count == 0


async def test_openai_guards_refuse_a_json_object_request_without_the_word_json(start_front: StartFront) -> None:
    async with start_front() as front:
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(response_format={"type": "json_object"}, horde={"openai_guards": True}),
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == "'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'."
    assert (error["param"], error["code"]) == ("messages", "ChatRequestConflict")
    assert front.horde.submit_route.call_count == 0


async def test_openai_guards_are_forwarded_with_the_horde_options(start_front: StartFront) -> None:
    messages = [{"role": "user", "content": "Reply in JSON."}]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(messages=messages, response_format={"type": "json_object"}, horde={"openai_guards": True}),
        )

    assert response.status_code == 200
    assert front.horde.submitted_body()["horde"]["openai_guards"] is True


async def test_tool_call_arguments_count_toward_the_text_byte_limit(start_front: StartFront) -> None:
    messages = [QUESTION, {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]}, _tool_result("call_1")]
    text_bytes = len(QUESTION["content"].encode()) + len(b"18C")
    call_bytes = len(WEATHER_CALL["function"]["name"].encode()) + len(WEATHER_CALL["function"]["arguments"].encode())
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_text_bytes": text_bytes + call_bytes - 1})) as front:
        refused = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))
    async with start_front(FAST_TEST_SETTINGS.model_copy(update={"max_text_bytes": text_bytes + call_bytes})) as front:
        front.horde.status_payload = done_status()
        accepted = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "UnsupportedChatParameter"
    assert accepted.status_code == 200


@pytest.mark.parametrize("response_format", [{"type": "json_object"}, STRUCTURED_OUTPUT_FORMAT])
async def test_a_response_format_is_forwarded_under_the_clients_keys(start_front: StartFront, response_format: dict[str, Any]) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(response_format=response_format))

    assert response.status_code == 200
    assert front.horde.submitted_body()["response_format"] == response_format


@pytest.mark.parametrize(
    ("keeps_schema", "refused_param"),
    [
        (False, "response_format.json_schema.schema"),
        (True, "response_format.json_schema.schema_"),
    ],
    ids=["schema_-alone", "schema_-beside-schema"],
)
async def test_a_json_schema_under_the_python_field_name_is_refused(
    start_front: StartFront,
    keeps_schema: bool,
    refused_param: str,
) -> None:
    json_schema = STRUCTURED_OUTPUT_FORMAT["json_schema"]
    sent_json_schema = {key: value for key, value in json_schema.items() if keeps_schema or key != "schema"}
    sent_json_schema["schema_"] = json_schema["schema"]
    async with start_front() as front:
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(response_format={"type": "json_schema", "json_schema": sent_json_schema}),
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["param"], error["code"]) == (refused_param, "BadRequest")
    assert front.horde.submit_route.call_count == 0


async def test_a_tool_round_in_the_history_is_forwarded_verbatim(start_front: StartFront) -> None:
    messages = [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]},
        {"role": "tool", "content": "18C and clear", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "It is 18C.", "tool_calls": [WEATHER_CALL]},
        {"role": "tool", "content": [{"type": "text", "text": "18C"}], "tool_call_id": "call_1"},
    ]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"] == messages


async def test_an_omitted_assistant_content_beside_tool_calls_is_forwarded_as_null(start_front: StartFront) -> None:
    messages = [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "tool_calls": [WEATHER_CALL]},
        {"role": "tool", "content": "18C", "tool_call_id": "call_1"},
    ]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"][1] == {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]}


async def test_tools_count_toward_the_estimated_prompt_tokens_and_a_response_schema_does_not(start_front: StartFront) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status(prompt_tokens=None)
        plain = await front.client.post("/v1/chat/completions", json=build_chat_body())
        with_tools = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[WEATHER_TOOL]))
        with_schema = await front.client.post("/v1/chat/completions", json=build_chat_body(response_format=STRUCTURED_OUTPUT_FORMAT))

    plain_prompt_tokens = plain.json()["usage"]["prompt_tokens"]
    assert with_tools.json()["usage"]["prompt_tokens"] > plain_prompt_tokens
    assert with_schema.json()["usage"]["prompt_tokens"] == plain_prompt_tokens
    assert with_tools.json()["x_horde"]["estimated_usage"] is True


@pytest.mark.parametrize(
    ("sent_message", "forwarded_message"),
    [
        (
            {"role": "tool", "content": "18C", "tool_call_id": "call_1", "name": "get_weather"},
            {"role": "tool", "content": "18C", "tool_call_id": "call_1"},
        ),
        (
            {"role": "tool", "content": "18C", "tool_call_id": "call_1", "x_client_note": {"any": "value"}},
            {"role": "tool", "content": "18C", "tool_call_id": "call_1"},
        ),
        (
            {"role": "assistant", "content": "Checking.", "tool_call_id": "call_1", "audio": None, "refusal": None},
            {"role": "assistant", "content": "Checking."},
        ),
    ],
)
async def test_a_message_key_outside_its_role_is_dropped_and_not_forwarded(
    start_front: StartFront,
    sent_message: dict[str, Any],
    forwarded_message: dict[str, Any],
) -> None:
    messages = [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]},
    ]
    # An assistant message has to wait for the answer to the open call, as OpenAI requires.
    if sent_message["role"] != "tool":
        messages.append({"role": "tool", "content": "18C", "tool_call_id": "call_1"})
    messages.append(sent_message)
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"][-1] == forwarded_message


@pytest.mark.parametrize("role", ["system", "user"])
async def test_a_tool_call_id_on_another_role_is_dropped_and_not_forwarded(start_front: StartFront, role: str) -> None:
    messages = [{"role": role, "content": "x", "tool_call_id": "call_1"}]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"] == [{"role": role, "content": "x"}]


@pytest.mark.parametrize("role", ["system", "user", "assistant"])
async def test_a_participant_name_is_forwarded_on_the_roles_that_define_it(start_front: StartFront, role: str) -> None:
    messages = [{"role": "system", "content": "You are terse."}, {"role": role, "content": "Say hello.", "name": "alice"}]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"][1] == {"role": role, "content": "Say hello.", "name": "alice"}


async def test_unknown_keys_inside_tool_calls_and_content_parts_are_dropped_and_not_forwarded(start_front: StartFront) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?", "cache_control": {"type": "ephemeral"}},
                {"type": "image_url", "image_url": {"url": TINY_PNG_DATA_URL, "detail": "low", "x_hint": 1}, "x_part": True},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    **WEATHER_CALL,
                    "index": 0,
                    "function": {**WEATHER_CALL["function"], "parsed_arguments": {"city": "Paris"}},
                },
            ],
        },
        {"role": "tool", "content": [{"type": "text", "text": "18C", "annotations": []}], "tool_call_id": "call_1"},
    ]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": TINY_PNG_DATA_URL, "detail": "low"}},
            ],
        },
        {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]},
        {"role": "tool", "content": [{"type": "text", "text": "18C"}], "tool_call_id": "call_1"},
    ]


SDK_ACCUMULATED_TOOL_CALL_REPLY: dict[str, Any] = {
    "role": "assistant",
    "content": None,
    "refusal": None,
    "audio": None,
    "annotations": None,
    "function_call": None,
    "parsed": None,
    "tool_calls": [
        {
            "id": "call_1",
            "type": "function",
            "index": 0,
            "function": {"name": "get_weather", "arguments": '{"city":"Paris"}', "parsed_arguments": None},
        },
    ],
}
"""An assistant tool call reply as the OpenAI SDK accumulates it from a stream and replays it as history."""


async def test_the_sdk_accumulated_streamed_reply_is_accepted_as_history_without_its_extra_keys(start_front: StartFront) -> None:
    messages = [
        {"role": "user", "content": "Weather in Paris?"},
        SDK_ACCUMULATED_TOOL_CALL_REPLY,
        {"role": "tool", "content": "18C and clear", "tool_call_id": "call_1"},
    ]
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(messages=messages, tools=[WEATHER_TOOL]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["messages"] == [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]},
        {"role": "tool", "content": "18C and clear", "tool_call_id": "call_1"},
    ]


@pytest.mark.parametrize(
    "sent_tool",
    [
        {**WEATHER_TOOL, "x_tool_note": "n"},
        {**WEATHER_TOOL, "function": {**WEATHER_TOOL["function"], "x_function_note": {"any": "value"}}},
    ],
)
async def test_unknown_keys_on_a_tool_definition_are_dropped_and_the_definition_forwarded(
    start_front: StartFront,
    sent_tool: dict[str, Any],
) -> None:
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[sent_tool]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["tools"] == [WEATHER_TOOL]


async def test_unknown_keys_inside_a_tool_parameters_schema_are_forwarded_unchanged(start_front: StartFront) -> None:
    parameters = {"type": "object", "properties": {"city": {"type": "string", "x-unit": "name"}}, "x-extension": True}
    tool = {"type": "function", "function": {"name": "get_weather", "parameters": parameters}}
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post("/v1/chat/completions", json=build_chat_body(tools=[tool]))

    assert response.status_code == 200
    assert front.horde.submitted_body()["tools"] == [tool]


async def test_unknown_keys_on_a_tool_choice_object_are_dropped_and_the_choice_forwarded(start_front: StartFront) -> None:
    tool_choice = {"type": "function", "function": {"name": "get_weather", "x_function_note": 1}, "x_choice_note": "n"}
    async with start_front() as front:
        front.horde.status_payload = done_status()
        response = await front.client.post(
            "/v1/chat/completions",
            json=build_chat_body(tools=[WEATHER_TOOL], tool_choice=tool_choice),
        )

    assert response.status_code == 200
    assert front.horde.submitted_body()["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
