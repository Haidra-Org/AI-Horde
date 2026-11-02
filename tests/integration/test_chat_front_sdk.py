# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The official OpenAI Python SDK against the chat front, the native Horde and a fake chat worker.

The module serves the native Flask app over HTTP with ``werkzeug.serving.make_server`` and the front with uvicorn,
each in a daemon thread on an ephemeral loopback port, so every SDK call crosses both real listeners. The Horde
publishes chat signals to the test Redis (database 7) that the front subscribes to. A
[`FakeChatWorker`][tests.integration._fake_chat_worker.FakeChatWorker] on its own thread pops through a Flask test
client and submits [`FIXED_REPLY`][tests.integration._front_stack.FIXED_REPLY] for a plain chat job, a tool
call for a job offering tools, the answer to a tool result, and a JSON document for a structured output job (see
[`build_reply_message`][tests.integration._front_stack.build_reply_message]). It declares ``vision`` with the
``url`` image transport, so an image request reaches it with a signed URL; the image case needs the test object store
as well. It also declares ``tools`` and ``json_schema``, so tool and structured output requests are eligible for it.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from io import BytesIO
from typing import TYPE_CHECKING, Any

import openai
import pytest
from openai.types.shared_params import ResponseFormatJSONSchema
from PIL import Image

from tests.integration._fake_chat_worker import CHAT_MODEL
from tests.integration._front_stack import (
    FIXED_REPLY,
    FRONT_CHAT_MESSAGES,
    SDK_TIMEOUT_SECONDS,
    STRUCTURED_REPLY,
    TOOL_ANSWER_REPLY,
    TOOL_MESSAGES,
    WEATHER_ARGUMENTS,
    WEATHER_CALL_ID,
    WEATHER_TOOL,
    WORKER_COMPLETION_TOKENS,
    WORKER_NAME,
    WORKER_PROMPT_TOKENS,
    FixedReplyWorker,
)

if TYPE_CHECKING:
    pass

pytestmark = pytest.mark.integration

WEATHER_TOOL_RESULT: str = "18C and clear"
"""The tool result the client sends back."""

COLOUR_RESPONSE_FORMAT: ResponseFormatJSONSchema = {
    "type": "json_schema",
    "json_schema": {
        "name": "colour",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"colour": {"type": "string"}},
            "required": ["colour"],
            "additionalProperties": False,
        },
    },
}
"""The structured output format of the structured output round."""

SIGNED_URL_PREFIXES: tuple[str, ...] = ("http://", "https://")
"""The schemes a signed image URL in a pop payload carries."""

FAIL_FAST_ANSWER_LIMIT_SECONDS: float = 15.0
"""The longest the fail-fast request may take: the grace, the status read it schedules and scheduling slack."""

SERVED_ANSWER_LIMIT_SECONDS: float = 10.0
"""The longest a served completion may take; under the fallback poll interval, so a lost signal fails the test."""


@pytest.fixture(scope="module", autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address submit limit; every request in this module comes from loopback."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(scope="module")
def unserved_model() -> str:
    """Return a model the text reference lists and no worker serves, so the Horde accepts it with no eligible worker.

    A model outside the reference with no online chat worker is refused at submit, which is a different path.
    """
    from horde.model_reference import model_reference

    return next(
        model_name for model_name in sorted(model_reference.get_text_model_names()) if model_name != CHAT_MODEL and ":" not in model_name
    )


@pytest.fixture(autouse=True)
def _worker_kept_up(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test during which the fake worker saw an unexpected answer or stopped."""
    yield
    if "fixed_reply_worker" in request.fixturenames:
        worker: FixedReplyWorker = request.getfixturevalue("fixed_reply_worker")
        assert worker.failures == []


@pytest.fixture
def sdk_client(front_base_url: str, owner_api_key: str) -> Iterator[openai.OpenAI]:
    """Return the official SDK client addressed at the front, with the owner's key as its bearer token."""
    client = openai.OpenAI(base_url=front_base_url, api_key=owner_api_key, timeout=SDK_TIMEOUT_SECONDS)
    try:
        yield client
    finally:
        client.close()


def test_the_model_list_includes_the_fake_workers_model(sdk_client: openai.OpenAI, unserved_model: str) -> None:
    model_ids = [model.id for model in sdk_client.models.list()]

    assert CHAT_MODEL in model_ids
    assert unserved_model not in model_ids


def test_a_completion_returns_the_workers_reply_with_its_usage(sdk_client: openai.OpenAI) -> None:
    started_at = time.monotonic()
    completion = sdk_client.chat.completions.create(model=CHAT_MODEL, messages=FRONT_CHAT_MESSAGES)
    elapsed_seconds = time.monotonic() - started_at

    [choice] = completion.choices
    assert choice.message.role == "assistant"
    assert choice.message.content == FIXED_REPLY
    assert choice.finish_reason == "stop"
    assert completion.model == CHAT_MODEL
    assert completion.usage is not None
    assert completion.usage.prompt_tokens == WORKER_PROMPT_TOKENS
    assert completion.usage.completion_tokens == WORKER_COMPLETION_TOKENS
    assert completion.usage.total_tokens == WORKER_PROMPT_TOKENS + WORKER_COMPLETION_TOKENS
    horde_details: dict[str, Any] = (completion.model_extra or {})["x_horde"]
    assert horde_details["request_id"]
    assert horde_details["worker_name"] == WORKER_NAME
    assert horde_details["estimated_usage"] is False
    assert elapsed_seconds < SERVED_ANSWER_LIMIT_SECONDS


def test_a_streamed_completion_yields_role_content_finish_and_usage_chunks(sdk_client: openai.OpenAI) -> None:
    started_at = time.monotonic()
    stream = sdk_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=FRONT_CHAT_MESSAGES,
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = list(stream)
    elapsed_seconds = time.monotonic() - started_at

    role_chunk, content_chunk, finish_chunk, usage_chunk = chunks
    assert len({chunk.id for chunk in chunks}) == 1
    assert role_chunk.choices[0].delta.role == "assistant"
    assert content_chunk.choices[0].delta.content == FIXED_REPLY
    assert finish_chunk.choices[0].finish_reason == "stop"
    assert (finish_chunk.model_extra or {})["x_horde"]["request_id"]
    assert usage_chunk.choices == []
    assert usage_chunk.usage is not None
    assert usage_chunk.usage.prompt_tokens == WORKER_PROMPT_TOKENS
    assert usage_chunk.usage.completion_tokens == WORKER_COMPLETION_TOKENS
    assert all(chunk.usage is None for chunk in (role_chunk, content_chunk, finish_chunk))
    assert elapsed_seconds < SERVED_ANSWER_LIMIT_SECONDS


def test_logit_bias_is_refused_with_its_param(sdk_client: openai.OpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as raised:
        sdk_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=FRONT_CHAT_MESSAGES,
            logit_bias={"50256": -100},
        )

    assert raised.value.status_code == 400
    assert raised.value.param == "logit_bias"
    assert isinstance(raised.value.body, dict)
    assert raised.value.body["param"] == "logit_bias"


def test_a_model_without_an_online_worker_fails_fast_without_an_sdk_retry(sdk_client: openai.OpenAI, unserved_model: str) -> None:
    started_at = time.monotonic()
    with pytest.raises(openai.InternalServerError) as raised:
        sdk_client.chat.completions.create(model=unserved_model, messages=FRONT_CHAT_MESSAGES)
    elapsed_seconds = time.monotonic() - started_at

    assert raised.value.status_code == 503
    assert raised.value.code == "no_eligible_worker"
    assert raised.value.response.headers["x-should-retry"] == "false"
    assert elapsed_seconds < FAIL_FAST_ANSWER_LIMIT_SECONDS


def _png_data_url() -> str:
    """Return a small PNG as a base64 data URL."""
    image_buffer = BytesIO()
    Image.new("RGB", (16, 12), (30, 120, 200)).save(image_buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(image_buffer.getvalue()).decode('ascii')}"


@pytest.mark.object_storage
@pytest.mark.usefixtures("object_store_ready")
def test_an_image_reaches_a_url_worker_as_a_signed_url(sdk_client: openai.OpenAI, fixed_reply_worker: FixedReplyWorker) -> None:
    data_url = _png_data_url()
    [served_model] = [model for model in sdk_client.models.list() if model.id == CHAT_MODEL]

    completion = sdk_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {"role": "system", "content": "Describe images briefly."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What colour is this?"},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                ],
            },
        ],
    )

    assert "image" in (served_model.model_extra or {})["architecture"]["input_modalities"]
    assert completion.choices[0].message.content == FIXED_REPLY
    image_payloads = [payload for payload in fixed_reply_worker.served_payloads if not isinstance(payload["messages"][-1]["content"], str)]
    [image_part] = [part for part in image_payloads[-1]["messages"][-1]["content"] if part["type"] == "image_url"]
    assert image_part["image_url"]["url"].startswith(SIGNED_URL_PREFIXES)
    assert image_part["image_url"]["url"] != data_url
    assert image_part["image_url"]["detail"] == "low"


def test_a_tool_call_round_returns_the_call_and_then_answers_the_result(
    sdk_client: openai.OpenAI,
    fixed_reply_worker: FixedReplyWorker,
) -> None:
    messages = list(TOOL_MESSAGES)

    calling = sdk_client.chat.completions.create(model=CHAT_MODEL, messages=messages, tools=[WEATHER_TOOL])

    [calling_choice] = calling.choices
    assert calling_choice.finish_reason == "tool_calls"
    assert calling_choice.message.content is None
    assert calling_choice.message.tool_calls is not None
    [tool_call] = calling_choice.message.tool_calls
    assert tool_call.type == "function"
    assert tool_call.id == WEATHER_CALL_ID
    assert tool_call.function.name == "get_weather"
    assert tool_call.function.arguments == WEATHER_ARGUMENTS
    assert fixed_reply_worker.served_payloads[-1]["tools"] == [WEATHER_TOOL]

    # SDK users continue a tool round by appending the returned message object; the SDK sends only its set fields.
    messages.append(calling_choice.message)  # type: ignore[arg-type]
    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": WEATHER_TOOL_RESULT})
    answering = sdk_client.chat.completions.create(model=CHAT_MODEL, messages=messages, tools=[WEATHER_TOOL])

    [answering_choice] = answering.choices
    assert answering_choice.finish_reason == "stop"
    assert answering_choice.message.content == TOOL_ANSWER_REPLY
    assert answering_choice.message.tool_calls is None
    served_messages = fixed_reply_worker.served_payloads[-1]["messages"]
    assert served_messages[-2]["tool_calls"][0]["id"] == WEATHER_CALL_ID
    assert served_messages[-1] == {"role": "tool", "tool_call_id": WEATHER_CALL_ID, "content": WEATHER_TOOL_RESULT}


def test_a_streamed_tool_call_round_accumulates_in_the_sdk_and_then_answers_the_result(
    sdk_client: openai.OpenAI,
    fixed_reply_worker: FixedReplyWorker,
) -> None:
    messages = list(TOOL_MESSAGES)
    with sdk_client.chat.completions.stream(model=CHAT_MODEL, messages=messages, tools=[WEATHER_TOOL]) as stream:
        final_completion = stream.get_final_completion()

    [choice] = final_completion.choices
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    [tool_call] = choice.message.tool_calls
    assert tool_call.id == WEATHER_CALL_ID
    assert tool_call.function.name == "get_weather"
    assert tool_call.function.arguments == WEATHER_ARGUMENTS

    # The accumulated message carries SDK fields (refusal, parsed, tool_calls[].index, parsed_arguments, ...) that the
    # SDK replays as history; the front drops them, as OpenAI ignores unknown message keys.
    messages.append(choice.message)  # type: ignore[arg-type]
    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": WEATHER_TOOL_RESULT})
    answering = sdk_client.chat.completions.create(model=CHAT_MODEL, messages=messages, tools=[WEATHER_TOOL])

    [answering_choice] = answering.choices
    assert answering_choice.finish_reason == "stop"
    assert answering_choice.message.content == TOOL_ANSWER_REPLY
    served_messages = fixed_reply_worker.served_payloads[-1]["messages"]
    assert served_messages[-2]["tool_calls"] == [
        {"id": WEATHER_CALL_ID, "type": "function", "function": {"name": "get_weather", "arguments": WEATHER_ARGUMENTS}},
    ]
    assert served_messages[-1] == {"role": "tool", "tool_call_id": WEATHER_CALL_ID, "content": WEATHER_TOOL_RESULT}


def test_a_json_schema_response_format_reaches_the_worker_and_returns_its_document(
    sdk_client: openai.OpenAI,
    fixed_reply_worker: FixedReplyWorker,
) -> None:
    completion = sdk_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=FRONT_CHAT_MESSAGES,
        response_format=COLOUR_RESPONSE_FORMAT,
    )

    content = completion.choices[0].message.content
    assert content is not None
    assert json.loads(content) == STRUCTURED_REPLY
    assert fixed_reply_worker.served_payloads[-1]["response_format"] == COLOUR_RESPONSE_FORMAT
