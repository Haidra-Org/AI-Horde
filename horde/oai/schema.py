# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OpenAI chat completions request, response and error models the front accepts and returns.

The request side is screened in three steps before validation.
[`find_refused_parameter`][horde.oai.schema.find_refused_parameter] returns the first top-level key, role, content
part type or ``horde`` option the service does not serve, so the client gets a 400 that reports it rather than a silently
degraded answer. [`drop_default_no_ops`][horde.oai.schema.drop_default_no_ops] then removes the keys listed in
[`DEFAULT_NO_OP_PARAMETERS`][horde.oai.schema.DEFAULT_NO_OP_PARAMETERS] when they hold their documented default,
because stock clients send them unprompted. The table is a closed list; any other top-level key outside
[`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest] is refused.
[`drop_unknown_nested_keys`][horde.oai.schema.drop_unknown_nested_keys] removes the keys a message and its nested
objects, a tool definition or the ``tool_choice`` object do not serve, because OpenAI accepts and ignores unknown keys
inside those objects and the OpenAI SDK replays its accumulated reply with keys of its own. Every other object refuses
unknown keys, as OpenAI does.

[`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest] then validates what remains with ``extra="forbid"``.
Values outside the service contract (``n`` other than 1, a token limit outside 16 to 4096, more than four stop
sequences, a sampling parameter outside OpenAI's range) raise the error type
[`UNSUPPORTED_PARAMETER_ERROR_TYPE`][horde.oai.schema.UNSUPPORTED_PARAMETER_ERROR_TYPE] so the front reports them as
``UnsupportedChatParameter``; contradictory values raise
[`CHAT_REQUEST_CONFLICT_ERROR_TYPE`][horde.chat_contract.CHAT_REQUEST_CONFLICT_ERROR_TYPE]. A value OpenAI refuses as
well is refused with OpenAI's message, which holds the parameter itself. A request that sets no
token limit is forwarded without one and the native route applies its default of 512.

The response side follows the OpenAI ``chat.completion``, ``chat.completion.chunk``, error and ``model`` shapes;
``x_horde`` carries the Horde-specific details. Error envelopes always carry ``message``, ``type``, ``param`` and
``code``, with null written explicitly, plus a ``metadata`` object holding only the details that are known.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from horde_sdk.generation_parameters.text.chat_limits import (
    MAX_CHAT_MAX_TOKENS,
    MAX_CHAT_STOP_SEQUENCES,
    MIN_CHAT_MAX_TOKENS,
    OPENAI_MIN_MAX_TOKENS,
    OPENAI_SAMPLING_RANGES,
    SUPPORTED_CHAT_CHOICE_COUNT,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_serializer,
)
from pydantic_core import PydanticCustomError

from horde.chat_contract import (
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    CHAT_REQUEST_CONFLICT_RC,
    INTERNAL_HORDE_OPTION_KEYS,
    MAX_CHAT_TOOL_CALL_ID_LENGTH,
    UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE,
    UNSUPPORTED_CHAT_PARAMETER_RC,
    ChatFunctionCall,
    ChatFunctionDefinition,
    ChatHordeOptions,
    ChatNamedFunction,
    ChatNamedToolChoice,
    ChatResponseFormat,
    ChatTool,
    ChatToolCall,
    ChatToolChoice,
)

__all__ = [
    "BAD_REQUEST_CODE",
    "CHAT_REQUEST_CONFLICT_CODE",
    "DEFAULT_NO_OP_PARAMETERS",
    "PARAMETER_CONTEXT_KEY",
    "SUPPORTED_MESSAGE_KEYS",
    "TOOL_CALLS_MESSAGE_KEY",
    "UNSUPPORTED_PARAMETER_CODE",
    "UNSUPPORTED_PARAMETER_ERROR_TYPE",
    "ChatCompletionChoice",
    "ChatCompletionChunk",
    "ChatCompletionChunkChoice",
    "ChatCompletionChunkDelta",
    "ChatCompletionChunkToolCall",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatCompletionResponseMessage",
    "ChatCompletionUsage",
    "ChatMessage",
    "ChatRole",
    "ErrorMetadata",
    "FinishReason",
    "HordeModelDetails",
    "HordeResponseDetails",
    "ImageContentPart",
    "ImageDetail",
    "ImageUrl",
    "ModelArchitecture",
    "ModelCard",
    "ModelList",
    "ModelTopProvider",
    "OpenAIError",
    "OpenAIErrorEnvelope",
    "OpenAIErrorType",
    "RefusedParameter",
    "StreamOptions",
    "SubmitOutcome",
    "TextContentPart",
    "drop_default_no_ops",
    "drop_unknown_nested_keys",
    "find_refused_parameter",
]

UNSUPPORTED_PARAMETER_CODE: str = UNSUPPORTED_CHAT_PARAMETER_RC
"""The error code for a key, role, part or value the service does not serve; the native route returns the same rc."""

BAD_REQUEST_CODE: str = "BadRequest"
"""The error code for a body that is not valid JSON or fails validation; the Horde's generic rc for a 400."""

CHAT_REQUEST_CONFLICT_CODE: str = CHAT_REQUEST_CONFLICT_RC
"""The error code for two keys whose values contradict each other; the native route returns the same rc."""

UNSUPPORTED_PARAMETER_ERROR_TYPE: str = UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE
"""The pydantic error type for a value outside the service contract, reported as ``UnsupportedChatParameter``; the
shared tool and schema models raise the same type."""

EMPTY_ARRAY_ERROR_TYPE: str = "empty_array"
"""The pydantic error type for an empty ``messages``, OpenAI's code for it; reported as ``BadRequest``, as any
malformed body is."""

PARAMETER_CONTEXT_KEY: str = "parameter"
"""The validation error context key holding the parameter a message already holds in OpenAI's ``Invalid '<key>'``
form; the front reports such a message without prefixing the request path."""

MAX_USER_CHARACTERS: int = 256
"""The longest ``user`` annotation accepted; it is unused and only bounded so it cannot carry bulk data."""

MAX_METADATA_PAIRS: int = 16
"""The most ``metadata`` pairs accepted, as OpenAI documents; unused and not persisted."""

MAX_METADATA_KEY_CHARACTERS: int = 64
"""The longest ``metadata`` key accepted, as OpenAI documents."""

MAX_METADATA_VALUE_CHARACTERS: int = 512
"""The longest ``metadata`` value accepted, as OpenAI documents."""

TEXT_CONTENT_PART_TYPE: str = "text"
"""The content part type carrying text, which any message may use."""

IMAGE_URL_CONTENT_PART_TYPE: str = "image_url"
"""The content part type carrying an image, which only a user message may use, as on the native route."""

IMAGE_PART_ROLES: frozenset[str] = frozenset(
    {
        "user",
    },
)
"""The roles whose messages may carry image parts; OpenAI accepts images only from the user."""


class ChatRole(StrEnum):
    """The message roles a chat accepts."""

    SYSTEM = "system"
    """Instructions for the model."""

    DEVELOPER = "developer"
    """Instructions for the model from the application; the native side stores it as ``system``."""

    USER = "user"
    """The end user's turn."""

    ASSISTANT = "assistant"
    """A previous reply of the model."""

    TOOL = "tool"
    """The result of a tool call, answering the call whose id it carries."""


class ImageDetail(StrEnum):
    """The OpenAI ``detail`` hint of an image part; forwarded to the worker unchanged."""

    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class FinishReason(StrEnum):
    """The OpenAI reasons a generation stopped."""

    STOP = "stop"
    """The model ended the reply or hit a stop sequence."""

    LENGTH = "length"
    """The generation reached its token limit."""

    CONTENT_FILTER = "content_filter"
    """The output was withheld by a filter."""

    TOOL_CALLS = "tool_calls"
    """The model called one or more tools and waits for their results."""


class OpenAIErrorType(StrEnum):
    """The OpenAI error ``type`` values the front returns, chosen by HTTP status."""

    INVALID_REQUEST_ERROR = "invalid_request_error"
    AUTHENTICATION_ERROR = "authentication_error"
    PERMISSION_ERROR = "permission_error"
    NOT_FOUND_ERROR = "not_found_error"
    TIMEOUT_ERROR = "timeout_error"
    RATE_LIMIT_ERROR = "rate_limit_error"
    API_ERROR = "api_error"


class SubmitOutcome(StrEnum):
    """What the front knows about whether a submit created a Horde request."""

    UNKNOWN = "unknown"
    """The submit was sent and no answer arrived, so a request may exist."""


class RefusedParameter(BaseModel):
    """Represents the first part of a raw chat request the service does not serve."""

    model_config = ConfigDict(frozen=True)

    parameter: str
    """The request path of the part, such as ``tools`` or ``messages[1].content[0].type=input_audio``; a refused content
    part's path holds its type the way the native route does."""

    message: str
    """The explanation returned to the client."""


NoOpPredicate = Callable[[object, Mapping[str, object]], bool]
"""Return whether a key's value, given the whole request body, is the default that may be dropped."""


def _is_empty_or_null(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is null or an empty list."""
    return value is None or (isinstance(value, list) and not value)


def _is_text_response_format(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is exactly ``{"type": "text"}``."""
    return value == {"type": "text"}


def _is_false_or_null(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is false or null."""
    return value is None or value is False


def _is_zero_or_null(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is the integer 0 or null; ``false`` is not 0 here."""
    return value is None or (type(value) is int and value == 0)


def _is_false(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is false."""
    return value is False


def _is_default_service_tier(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is ``auto`` or ``default``."""
    return value == "auto" or value == "default"


DEFAULT_NO_OP_PARAMETERS: Mapping[str, NoOpPredicate] = MappingProxyType(
    {
        "tools": _is_empty_or_null,
        "response_format": _is_text_response_format,
        "logprobs": _is_false_or_null,
        "top_logprobs": _is_zero_or_null,
        "store": _is_false,
        "service_tier": _is_default_service_tier,
    },
)
"""Keys accepted and dropped when they hold their documented default, each with the test for that default.

Stock clients send these unprompted. A key here that is also a field of
[`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest] (``tools``, ``response_format``) is validated and
forwarded when it holds any other value. Any other value of the remaining keys is refused with its key in the message, as is any key that
is neither here nor a field of the request model. ``tool_choice`` and ``parallel_tool_calls`` are not here: OpenAI
refuses either on a request without tools, whatever the value, and so does the front
([`check_tool_fields_need_tools`][horde.chat_contract.check_tool_fields_need_tools]).
"""

TOOL_CALLS_MESSAGE_KEY: str = "tool_calls"
"""The message key holding an assistant's tool calls; refused on any other role."""

COMMON_MESSAGE_KEYS: frozenset[str] = frozenset(
    {
        "role",
        "content",
        "name",
    },
)
"""The message keys of a ``system``, ``developer`` or ``user`` message, and of a message whose role is not served."""

SUPPORTED_MESSAGE_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        ChatRole.SYSTEM.value: COMMON_MESSAGE_KEYS,
        ChatRole.DEVELOPER.value: COMMON_MESSAGE_KEYS,
        ChatRole.USER.value: COMMON_MESSAGE_KEYS,
        ChatRole.ASSISTANT.value: COMMON_MESSAGE_KEYS | {TOOL_CALLS_MESSAGE_KEY},
        ChatRole.TOOL.value: frozenset(
            {
                "role",
                "content",
                "tool_call_id",
            },
        ),
    },
)
"""The message keys each role may carry; any other key (``audio``, ``refusal``, ``tool_call_id`` outside a tool
message, ...) is dropped before validation and never forwarded, except ``tool_calls`` outside an assistant message,
which [`find_refused_parameter`][horde.oai.schema.find_refused_parameter] refuses as OpenAI does. OpenAI defines no
``name`` on a tool message, so it is dropped there."""

ACCEPTED_ROLES: frozenset[str] = frozenset(chat_role.value for chat_role in ChatRole)
"""The role strings a message may carry; ``function`` belongs to the legacy function calling, which is not served."""


def find_refused_parameter(request_body: Mapping[str, object]) -> RefusedParameter | None:
    """Return the first top-level key, role, content part type or ``horde`` option of a raw request that is not served.

    Malformed values of served keys are left to [`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest]
    validation; this only finds what the service does not serve at all. Unknown keys inside a message, a tool
    definition or the ``tool_choice`` object are not refused;
    [`drop_unknown_nested_keys`][horde.oai.schema.drop_unknown_nested_keys] removes them.

    Args:
        request_body: The decoded JSON request body.

    Returns:
        The refused part and an explanation, or None when every part is served.
    """
    for request_key, value in request_body.items():
        if request_key in ChatCompletionRequest.model_fields:
            continue
        no_op_predicate = DEFAULT_NO_OP_PARAMETERS.get(request_key)
        if no_op_predicate is None:
            return RefusedParameter(
                parameter=request_key,
                message=f"The parameter '{request_key}' is not supported by the AI Horde chat completions endpoint.",
            )
        if not no_op_predicate(value, request_body):
            return RefusedParameter(
                parameter=request_key,
                message=f"The parameter '{request_key}' is accepted only at its default value; other values are not served.",
            )
    messages = request_body.get("messages")
    if isinstance(messages, list):
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            refused_in_message = _find_refused_in_message(message, message_index=message_index)
            if refused_in_message is not None:
                return refused_in_message
    horde_options = request_body.get("horde")
    if isinstance(horde_options, dict):
        return _find_refused_horde_option(horde_options)
    return None


def _find_refused_in_message(message: Mapping[str, object], *, message_index: int) -> RefusedParameter | None:
    """Return the refused ``tool_calls``, role or first refused content part type of one raw message, or None."""
    role = message.get("role")
    # OpenAI refuses tool calls on any role but assistant while it ignores other keys a role does not define, which
    # drop_unknown_nested_keys drops.
    if TOOL_CALLS_MESSAGE_KEY in message and role != ChatRole.ASSISTANT.value:
        return RefusedParameter(
            parameter=f"messages[{message_index}].{TOOL_CALLS_MESSAGE_KEY}",
            message=f"The message key '{TOOL_CALLS_MESSAGE_KEY}' is not supported on a '{role}' message.",
        )
    if isinstance(role, str) and role not in ACCEPTED_ROLES:
        return RefusedParameter(
            parameter=f"messages[{message_index}].role",
            message=f"The role '{role}' is not supported; use one of {', '.join(chat_role.value for chat_role in ChatRole)}.",
        )
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for part_index, content_part in enumerate(content):
        if not isinstance(content_part, dict):
            continue
        part_type = content_part.get("type")
        if part_type == TEXT_CONTENT_PART_TYPE:
            continue
        if part_type == IMAGE_URL_CONTENT_PART_TYPE and role in IMAGE_PART_ROLES:
            continue
        return RefusedParameter(
            parameter=f"messages[{message_index}].content[{part_index}].type={part_type}",
            message=f"The content part type '{part_type}' is not supported here; 'text' parts are served on every message "
            "and 'image_url' parts on user messages.",
        )
    return None


def _find_refused_horde_option(horde_options: Mapping[str, object]) -> RefusedParameter | None:
    """Return the first ``horde`` key a public request may not send, or None."""
    for option_key in horde_options:
        if option_key in INTERNAL_HORDE_OPTION_KEYS:
            return RefusedParameter(
                parameter=f"horde.{option_key}",
                message=f"The horde option '{option_key}' is set by the front and cannot be sent.",
            )
        if option_key not in ChatHordeOptions.model_fields:
            return RefusedParameter(
                parameter=f"horde.{option_key}",
                message=f"The horde option '{option_key}' is not supported.",
            )
    return None


def drop_default_no_ops(request_body: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of ``request_body`` without the keys of the default no-op table.

    Only a key holding its default is dropped, so a request model field of the table holding another value stays for
    validation. Call it only after [`find_refused_parameter`][horde.oai.schema.find_refused_parameter] found nothing,
    which guarantees every other key of the table holds its default.

    Args:
        request_body: The decoded JSON request body.
    """
    return {request_key: value for request_key, value in request_body.items() if not _holds_no_op_default(request_key, value, request_body)}


def _holds_no_op_default(request_key: str, value: object, request_body: Mapping[str, object]) -> bool:
    """Return whether ``request_key`` is in the default no-op table and ``value`` is its default."""
    no_op_predicate = DEFAULT_NO_OP_PARAMETERS.get(request_key)
    return no_op_predicate is not None and no_op_predicate(value, request_body)


def drop_unknown_nested_keys(request_body: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of ``request_body`` whose messages, tool definitions and tool choice carry only served keys.

    OpenAI accepts a message carrying a key it does not define (a ``name`` on a tool message, an arbitrary key), and an
    unknown key inside a content part, a history tool call, a ``tools`` entry or its ``function``, or the
    ``tool_choice`` object or its ``function``, and ignores the key, while it refuses an unknown top-level parameter
    and an unknown key inside ``response_format`` or ``stream_options``; the front matches all of them. The OpenAI
    SDK's accumulated streamed reply carries ``refusal``, ``audio``, ``annotations``, ``function_call`` and ``parsed``
    on the message, ``index`` on each tool call and ``parsed_arguments`` on each function, and the SDK replays that
    reply as history. ``tool_calls`` on a message other than an assistant message is refused by
    [`find_refused_parameter`][horde.oai.schema.find_refused_parameter], as OpenAI refuses it. Other keys outside
    the role's set in [`SUPPORTED_MESSAGE_KEYS`][horde.oai.schema.SUPPORTED_MESSAGE_KEYS] are dropped, as are keys
    outside the fields of [`ChatToolCall`][horde.chat_contract.ChatToolCall],
    [`ChatFunctionCall`][horde.chat_contract.ChatFunctionCall], [`TextContentPart`][horde.oai.schema.TextContentPart],
    [`ImageContentPart`][horde.oai.schema.ImageContentPart], [`ImageUrl`][horde.oai.schema.ImageUrl],
    [`ChatTool`][horde.chat_contract.ChatTool], [`ChatFunctionDefinition`][horde.chat_contract.ChatFunctionDefinition],
    [`ChatNamedToolChoice`][horde.chat_contract.ChatNamedToolChoice] and
    [`ChatNamedFunction`][horde.chat_contract.ChatNamedFunction].
    A function's ``parameters`` schema is client data and is forwarded unchanged. Served keys keep their validation,
    and values that are not objects are left for validation to report.

    Args:
        request_body: The decoded JSON request body.
    """
    served_body = dict(request_body)
    messages = served_body.get("messages")
    if isinstance(messages, list):
        served_body["messages"] = [_served_message(message) if isinstance(message, dict) else message for message in messages]
    tools = served_body.get("tools")
    if isinstance(tools, list):
        served_body["tools"] = [_served_tool(tool) if isinstance(tool, dict) else tool for tool in tools]
    tool_choice = served_body.get("tool_choice")
    if isinstance(tool_choice, dict):
        served_body["tool_choice"] = _served_tool_choice(tool_choice)
    return served_body


def _served_tool_choice(tool_choice: Mapping[str, object]) -> dict[str, object]:
    """Return a ``tool_choice`` object with only the served choice fields, and its function with only its name."""
    served_tool_choice = _served_keys(tool_choice, ChatNamedToolChoice.model_fields)
    function = served_tool_choice.get("function")
    if isinstance(function, dict):
        served_tool_choice["function"] = _served_keys(function, ChatNamedFunction.model_fields)
    return served_tool_choice


def _served_tool(tool: Mapping[str, object]) -> dict[str, object]:
    """Return a ``tools`` entry with only the tool fields, and its function with only the function definition fields."""
    served_tool = _served_keys(tool, ChatTool.model_fields)
    function = served_tool.get("function")
    if isinstance(function, dict):
        served_tool["function"] = _served_keys(function, ChatFunctionDefinition.model_fields)
    return served_tool


def _served_message(message: Mapping[str, object]) -> dict[str, object]:
    """Return ``message`` with only the keys of its role, its tool calls and content parts reduced the same way."""
    role = message.get("role")
    role_keys = SUPPORTED_MESSAGE_KEYS.get(role, COMMON_MESSAGE_KEYS) if isinstance(role, str) else COMMON_MESSAGE_KEYS
    served_message = _served_keys(message, role_keys)
    tool_calls = served_message.get("tool_calls")
    if isinstance(tool_calls, list):
        served_message["tool_calls"] = [
            _served_tool_call(tool_call) if isinstance(tool_call, dict) else tool_call for tool_call in tool_calls
        ]
    content = served_message.get("content")
    if isinstance(content, list):
        served_message["content"] = [
            _served_content_part(content_part) if isinstance(content_part, dict) else content_part for content_part in content
        ]
    return served_message


def _served_tool_call(tool_call: Mapping[str, object]) -> dict[str, object]:
    """Return ``tool_call`` with only the tool call fields, and its function with only the function call fields."""
    served_tool_call = _served_keys(tool_call, ChatToolCall.model_fields)
    function = served_tool_call.get("function")
    if isinstance(function, dict):
        served_tool_call["function"] = _served_keys(function, ChatFunctionCall.model_fields)
    return served_tool_call


def _served_content_part(content_part: Mapping[str, object]) -> dict[str, object]:
    """Return ``content_part`` with only the fields of its part type; a part of another type is copied unchanged."""
    part_type = content_part.get("type")
    if part_type == TEXT_CONTENT_PART_TYPE:
        return _served_keys(content_part, TextContentPart.model_fields)
    if part_type != IMAGE_URL_CONTENT_PART_TYPE:
        return dict(content_part)
    served_part = _served_keys(content_part, ImageContentPart.model_fields)
    image_url = served_part.get("image_url")
    if isinstance(image_url, dict):
        served_part["image_url"] = _served_keys(image_url, ImageUrl.model_fields)
    return served_part


def _served_keys(raw_object: Mapping[str, object], served_keys: Collection[str]) -> dict[str, object]:
    """Return the entries of ``raw_object`` whose key is in ``served_keys``."""
    return {object_key: value for object_key, value in raw_object.items() if object_key in served_keys}


class TextContentPart(BaseModel):
    """Represents one text part of a message's content array."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"]
    """Always ``text``."""

    text: StrictStr
    """The text of this part."""


class ImageUrl(BaseModel):
    """Represents the ``image_url`` object of an image part."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: StrictStr
    """A ``data:image/...;base64,`` URL or an ``http(s)`` URL; the native route validates, fetches and stores it."""

    detail: ImageDetail | None = None
    """The client's detail hint."""


class ImageContentPart(BaseModel):
    """Represents one image part of a user message's content array."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["image_url"]
    """Always ``image_url``."""

    image_url: ImageUrl
    """Where the image is and how the client wants it read."""


ContentPart = Annotated[TextContentPart | ImageContentPart, Field(discriminator="type")]
"""One part of a message's content array."""


class ChatMessage(BaseModel):
    """Represents one message of a chat request.

    Which keys each role may carry is enforced by
    [`drop_unknown_nested_keys`][horde.oai.schema.drop_unknown_nested_keys] before validation (see
    [`SUPPORTED_MESSAGE_KEYS`][horde.oai.schema.SUPPORTED_MESSAGE_KEYS]). Counts, sizes and whether the tool turns
    follow OpenAI's rule ([`check_tool_history`][horde.chat_contract.check_tool_history]) are checked after
    validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ChatRole
    """Who wrote the message."""

    name: StrictStr | None = None
    """An optional participant name, passed through to the worker."""

    tool_call_id: Annotated[StrictStr, Field(min_length=1, max_length=MAX_CHAT_TOOL_CALL_ID_LENGTH)] | None = Field(
        default=None,
        validate_default=True,
    )
    """The id of the call a ``tool`` message answers; required on a tool message."""

    tool_calls: Annotated[list[ChatToolCall], Field(min_length=1)] | None = None
    """The tool calls of an earlier assistant reply, forwarded unchanged."""

    # Declared after ``tool_calls`` so its validator can read them and report at this path; the default is validated
    # so an omitted content is checked too.
    content: StrictStr | Annotated[list[ContentPart], Field(min_length=1)] | None = Field(default=None, validate_default=True)
    """The message text as one string, or an array of parts; only a user message may hold image parts, which
    [`find_refused_parameter`][horde.oai.schema.find_refused_parameter] enforces before validation. Null or omitted
    only on an assistant message with ``tool_calls``."""

    @field_validator("content", mode="after")
    @classmethod
    def validate_content_present(
        cls,
        content: str | list[TextContentPart | ImageContentPart] | None,
        info: ValidationInfo,
    ) -> str | list[TextContentPart | ImageContentPart] | None:
        """Validate that only an assistant message with tool calls lacks content.

        Raises:
            ValueError: The content is null or omitted on any other message.
        """
        if content is None and not (info.data.get("role") is ChatRole.ASSISTANT and info.data.get("tool_calls")):
            raise ValueError("'content' may be null only on an assistant message with 'tool_calls'.")
        return content

    @field_validator("tool_call_id", mode="after")
    @classmethod
    def validate_tool_message_answers_a_call(cls, tool_call_id: str | None, info: ValidationInfo) -> str | None:
        """Validate that a tool message carries the id of the call it answers.

        Raises:
            ValueError: A tool message has no ``tool_call_id``.
        """
        if tool_call_id is None and info.data.get("role") is ChatRole.TOOL:
            raise ValueError("A tool message needs the 'tool_call_id' of the call it answers.")
        return tool_call_id


class StreamOptions(BaseModel):
    """Represents the ``stream_options`` object of a chat request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    include_usage: StrictBool | None = None
    """Send a usage chunk with ``choices: []`` before ``[DONE]``."""


MetadataKey = Annotated[str, StringConstraints(strict=True, max_length=MAX_METADATA_KEY_CHARACTERS)]
"""A ``metadata`` key within OpenAI's documented bound."""

MetadataValue = Annotated[str, StringConstraints(strict=True, max_length=MAX_METADATA_VALUE_CHARACTERS)]
"""A ``metadata`` value within OpenAI's documented bound."""


def _unsupported_value_error(message: str) -> PydanticCustomError:
    """Create the validation error for a value outside the service contract."""
    return PydanticCustomError(UNSUPPORTED_PARAMETER_ERROR_TYPE, message)


def _openai_value_error(
    parameter: str,
    problem_template: str,
    *,
    error_type: str = UNSUPPORTED_PARAMETER_ERROR_TYPE,
    **template_values: object,
) -> PydanticCustomError:
    """Create the validation error for a value OpenAI refuses, worded as OpenAI words it.

    Args:
        parameter: The request key at fault, written into the message and the context under
            [`PARAMETER_CONTEXT_KEY`][horde.oai.schema.PARAMETER_CONTEXT_KEY].
        problem_template: The message after ``Invalid '<parameter>': ``, with ``{name}`` placeholders.
        error_type: The pydantic error type, which selects the front's error code.
        template_values: The values of the placeholders.
    """
    return PydanticCustomError(
        error_type,
        "Invalid '{" + PARAMETER_CONTEXT_KEY + "}': " + problem_template,
        {
            PARAMETER_CONTEXT_KEY: parameter,
            **template_values,
        },
    )


class ChatCompletionRequest(BaseModel):
    """Represents an OpenAI chat completions request in the subset the service serves.

    A null value means the key was omitted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    model: StrictStr = Field(min_length=1)
    """The Horde text model name."""

    messages: list[ChatMessage]
    """The conversation so far, in order; at least one message."""

    stream: StrictBool | None = None
    """Answer as server-sent events; the whole text arrives in one content chunk."""

    stream_options: StreamOptions | None = None
    """Options for a streamed answer; only allowed with ``stream: true``."""

    max_tokens: StrictInt | None = None
    """The most tokens to generate, 16 to 4096; OpenAI accepts any value from 1."""

    max_completion_tokens: StrictInt | None = None
    """The newer key for ``max_tokens``; both must agree when both are set."""

    temperature: StrictFloat | StrictInt | None = None
    """The sampling temperature, 0 to 2."""

    top_p: StrictFloat | StrictInt | None = None
    """The nucleus sampling probability, 0 to 1."""

    frequency_penalty: StrictFloat | StrictInt | None = None
    """The frequency penalty, -2 to 2; a non-default value needs a worker that declares it."""

    presence_penalty: StrictFloat | StrictInt | None = None
    """The presence penalty, -2 to 2; a non-default value needs a worker that declares it."""

    stop: StrictStr | list[StrictStr] | None = None
    """One stop sequence or up to four."""

    seed: StrictInt | None = None
    """The sampling seed."""

    n: StrictInt | None = None
    """How many choices to generate; only 1 is served."""

    user: Annotated[StrictStr, Field(max_length=MAX_USER_CHARACTERS)] | None = None
    """An end-user identifier; accepted, unused and not persisted."""

    metadata: Annotated[dict[MetadataKey, MetadataValue], Field(max_length=MAX_METADATA_PAIRS)] | None = None
    """Free-form annotations; accepted, unused and not persisted."""

    tools: list[ChatTool] | None = None
    """The functions the model may call, forwarded verbatim; an empty or null list is dropped before validation."""

    tool_choice: ChatToolChoice | None = None
    """Whether and which tool the model calls, forwarded verbatim; only allowed with tools, as under OpenAI."""

    parallel_tool_calls: StrictBool | None = None
    """Whether the model may call several tools in one reply, forwarded verbatim; only allowed with tools."""

    response_format: ChatResponseFormat | None = None
    """The output format, forwarded verbatim; ``{"type": "text"}`` is dropped before validation."""

    horde: ChatHordeOptions | None = None
    """The Horde extension object."""

    @field_validator("messages")
    @classmethod
    def validate_messages_not_empty(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Validate that at least one message is sent.

        Raises:
            PydanticCustomError: ``messages`` is empty; reported as ``BadRequest``.
        """
        if not messages:
            raise _openai_value_error(
                "messages",
                "empty array. Expected an array with minimum length 1, but got an empty array instead.",
                error_type=EMPTY_ARRAY_ERROR_TYPE,
            )
        return messages

    @field_validator(*OPENAI_SAMPLING_RANGES)
    @classmethod
    def validate_sampling_range(cls, sampling_value: float | None, info: ValidationInfo) -> float | None:
        """Validate that a sampling parameter is within the range OpenAI accepts; it is never clamped.

        Raises:
            PydanticCustomError: The value is outside its range in
                the SDK's ``OPENAI_SAMPLING_RANGES``.
        """
        if sampling_value is None or info.field_name is None:
            return sampling_value
        minimum, maximum = OPENAI_SAMPLING_RANGES[info.field_name]
        if sampling_value < minimum:
            raise _openai_value_error(
                info.field_name,
                "decimal below minimum value. Expected a value >= {limit}, but got {value} instead.",
                limit=minimum,
                value=sampling_value,
            )
        if sampling_value > maximum:
            raise _openai_value_error(
                info.field_name,
                "decimal above maximum value. Expected a value <= {limit}, but got {value} instead.",
                limit=maximum,
                value=sampling_value,
            )
        return sampling_value

    @field_validator("stream_options")
    @classmethod
    def validate_stream_options_need_stream(cls, stream_options: StreamOptions | None, info: ValidationInfo) -> StreamOptions | None:
        """Validate that ``stream_options`` is only set on a streamed request.

        Raises:
            ValueError: ``stream_options`` is set and ``stream`` is not true.
        """
        if stream_options is not None and info.data.get("stream") is not True:
            raise ValueError("'stream_options' is only allowed when 'stream' is true.")
        return stream_options

    @field_validator("max_tokens", "max_completion_tokens")
    @classmethod
    def validate_token_limit_range(cls, token_limit: int | None, info: ValidationInfo) -> int | None:
        """Validate that a token limit is within the served range; it is never clamped.

        A limit below OpenAI's minimum of 1 is refused in OpenAI's wording; one from 1 to 15 is refused with the
        Horde floor, which OpenAI does not have.

        Raises:
            PydanticCustomError: The limit is outside 16 to 4096.
        """
        if token_limit is None or info.field_name is None:
            return token_limit
        if token_limit < OPENAI_MIN_MAX_TOKENS:
            raise _openai_value_error(
                info.field_name,
                "integer below minimum value. Expected a value >= {limit}, but got {value} instead.",
                limit=OPENAI_MIN_MAX_TOKENS,
                value=token_limit,
            )
        if token_limit < MIN_CHAT_MAX_TOKENS:
            raise _openai_value_error(
                info.field_name,
                "the AI Horde refuses a token limit below {limit}, the minimum generation length of its native text "
                "API, rather than raising it, but got {value} instead.",
                limit=MIN_CHAT_MAX_TOKENS,
                value=token_limit,
            )
        if token_limit > MAX_CHAT_MAX_TOKENS:
            raise _unsupported_value_error(f"The token limit must be between {MIN_CHAT_MAX_TOKENS} and {MAX_CHAT_MAX_TOKENS}.")
        return token_limit

    @field_validator("max_completion_tokens")
    @classmethod
    def validate_token_limits_agree(cls, max_completion_tokens: int | None, info: ValidationInfo) -> int | None:
        """Validate that ``max_completion_tokens`` equals ``max_tokens`` when both are set.

        Raises:
            PydanticCustomError: The two limits differ.
        """
        max_tokens = info.data.get("max_tokens")
        if max_completion_tokens is not None and max_tokens is not None and max_completion_tokens != max_tokens:
            raise PydanticCustomError(
                CHAT_REQUEST_CONFLICT_ERROR_TYPE,
                "'max_tokens' and 'max_completion_tokens' must be equal when both are set.",
            )
        return max_completion_tokens

    @field_validator("stop")
    @classmethod
    def validate_stop_count(cls, stop: str | list[str] | None) -> str | list[str] | None:
        """Validate that at most four stop sequences are sent.

        Raises:
            PydanticCustomError: More than four sequences are sent.
        """
        if isinstance(stop, list) and len(stop) > MAX_CHAT_STOP_SEQUENCES:
            raise _openai_value_error(
                "stop",
                "array too long. Expected an array with maximum length {limit}, but got an array with length {length} instead.",
                limit=MAX_CHAT_STOP_SEQUENCES,
                length=len(stop),
            )
        return stop

    @field_validator("n")
    @classmethod
    def validate_single_choice(cls, choice_count: int | None) -> int | None:
        """Validate that one choice is asked for.

        Raises:
            PydanticCustomError: ``n`` is set to anything but 1; below 1 in OpenAI's wording.
        """
        if choice_count is not None and choice_count < SUPPORTED_CHAT_CHOICE_COUNT:
            raise _openai_value_error(
                "n",
                "integer below minimum value. Expected a value >= {limit}, but got {value} instead.",
                limit=SUPPORTED_CHAT_CHOICE_COUNT,
                value=choice_count,
            )
        if choice_count is not None and choice_count != SUPPORTED_CHAT_CHOICE_COUNT:
            raise _unsupported_value_error(f"Only {SUPPORTED_CHAT_CHOICE_COUNT} choice is served; 'n' must be 1 or omitted.")
        return choice_count


def _drop_absent_fields(model: BaseModel, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
    """Return the serialised ``model`` without its null fields."""
    serialised: dict[str, object] = handler(model)
    return {field_name: value for field_name, value in serialised.items() if value is not None}


class ChatCompletionResponseMessage(BaseModel):
    """Represents the assistant message of one completion choice.

    ``content`` is always written, null included; ``tool_calls`` only when the worker returned calls.
    """

    model_config = ConfigDict(frozen=True)

    role: Literal["assistant"] = "assistant"
    """Always ``assistant``."""

    content: str | None
    """The generated text; null only when the worker answered with tool calls alone."""

    tool_calls: list[ChatToolCall] | None = None
    """The tool calls the worker returned, unchanged."""

    @model_serializer(mode="wrap")
    def serialize_without_absent_tool_calls(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Convert the message to a dict, leaving ``tool_calls`` out when the worker returned none."""
        serialised: dict[str, object] = handler(self)
        if self.tool_calls is None:
            serialised.pop("tool_calls", None)
        return serialised


class ChatCompletionChoice(BaseModel):
    """Represents one completion choice, built from one Horde generation."""

    model_config = ConfigDict(frozen=True)

    index: int
    """The position of the choice, in the order the Horde lists its generations."""

    message: ChatCompletionResponseMessage
    """The generated message."""

    finish_reason: FinishReason
    """Why the generation stopped; ``stop`` when the worker did not say."""

    native_finish_reason: str | None = None
    """The backend's own reason, as the worker reported it; null when it did not."""

    logprobs: None = None
    """Always null; log probabilities are not served."""


class ChatCompletionUsage(BaseModel):
    """Represents the token counts of a completion."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int
    """Tokens in the prompt, as the worker reported or as estimated."""

    completion_tokens: int
    """Tokens generated, as the worker reported or as estimated."""

    total_tokens: int
    """The sum of the prompt and completion tokens."""


class HordeResponseDetails(BaseModel):
    """Represents the ``x_horde`` details of a completion; fields the Horde did not supply are left out."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    """The Horde request id; also the completion id without its prefix."""

    worker_id: str | None = None
    """The worker that generated the choice."""

    worker_name: str | None = None
    """The name of that worker."""

    kudos: float
    """The kudos the request consumed."""

    charge_basis: str | None = None
    """Which count the kudos were computed on, as the Horde reports it."""

    estimated_usage: bool
    """True when any usage count is an estimate from text length because the worker did not report it."""

    queue_wait_seconds: float | None = None
    """Seconds from submit to the first assignment to a worker, when the Horde supplies an assignment time."""

    context_adjustments: list[object] | None = None
    """The context changes the Horde applied to fit the messages, when it reports any."""

    notice: str | None = None
    """Present on an empty return: the worker ran the job and returned no visible text."""

    @model_serializer(mode="wrap")
    def serialize_without_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Convert the details to a dict without the fields the Horde did not supply."""
        return _drop_absent_fields(self, handler)


class ChatCompletionResponse(BaseModel):
    """Represents an OpenAI ``chat.completion`` response."""

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    completion_id: str = Field(serialization_alias="id")
    """``chatcmpl-`` followed by the Horde request id."""

    object_type: Literal["chat.completion"] = Field(default="chat.completion", serialization_alias="object")
    """Always ``chat.completion``."""

    created: int
    """When the request was submitted, in Unix seconds."""

    model: str
    """The model that generated the first choice."""

    choices: list[ChatCompletionChoice]
    """One choice per Horde generation."""

    usage: ChatCompletionUsage
    """The token counts."""

    system_fingerprint: str | None = None
    """The serving worker and model the first choice came from, as a short hash; null when no worker is known."""

    x_horde: HordeResponseDetails
    """The Horde-specific details."""


class ErrorMetadata(BaseModel):
    """Represents the ``metadata`` object of an error; only the details that are known are written."""

    model_config = ConfigDict(frozen=True)

    request_id: str | None = None
    """The Horde request id, once one exists."""

    rc: str | None = None
    """The Horde return code, when the Horde produced the error."""

    retry_after: int | None = None
    """Seconds to wait before retrying, when waiting helps."""

    submit_outcome: SubmitOutcome | None = None
    """Set when the front cannot tell whether the submit created a request."""

    operation_key: str | None = None
    """The operation key the submit carried, when its outcome is unknown or the Horde refused the key; an operator
    finds the request it claimed by it."""

    @model_serializer(mode="wrap")
    def serialize_without_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Convert the metadata to a dict without the details that are not known."""
        return _drop_absent_fields(self, handler)


class OpenAIError(BaseModel):
    """Represents the body of an OpenAI error envelope; the four OpenAI fields are always written, null included."""

    model_config = ConfigDict(frozen=True)

    message: str
    """The explanation for the client."""

    type: OpenAIErrorType
    """The error class, chosen by HTTP status."""

    param: str | None = None
    """The request path at fault, when one is."""

    code: str | None
    """The Horde return code when the Horde produced the error, or the front's own code."""

    metadata: ErrorMetadata = Field(default_factory=ErrorMetadata)
    """The request id, Horde return code and retry details that are known."""


class OpenAIErrorEnvelope(BaseModel):
    """Represents an OpenAI error response, ``{"error": {...}}``, before or after stream headers."""

    model_config = ConfigDict(frozen=True)

    error: OpenAIError
    """The error details."""


class ModelArchitecture(BaseModel):
    """Represents the modalities a model list entry serves."""

    model_config = ConfigDict(frozen=True)

    modality: str = "text->text"
    """The input and output modalities in OpenRouter's notation."""

    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    """The content the model accepts; ``image`` is added when an online chat worker declares ``vision``."""

    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    """The content the model produces."""


class ModelTopProvider(BaseModel):
    """Represents the ``top_provider`` limits of a model list entry: the largest an online chat worker declares."""

    model_config = ConfigDict(frozen=True)

    context_length: int | None
    """The largest context, in tokens."""

    max_completion_tokens: int | None
    """The largest output, in tokens."""


class HordeModelDetails(BaseModel):
    """Represents the ``x_horde`` details of a model list entry."""

    model_config = ConfigDict(frozen=True)

    workers: int
    """How many online workers serve chat jobs with the model."""

    queued: float | None = None
    """The queued work for the model, as the Horde's model list reports it."""

    @model_serializer(mode="wrap")
    def serialize_without_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Convert the details to a dict without the values the Horde did not report."""
        return _drop_absent_fields(self, handler)


class ModelCard(BaseModel):
    """Represents one entry of the OpenAI model list, with OpenRouter's limit and parameter fields.

    The limits and parameters are availability hints over the online chat workers; a request can still find no
    eligible worker a moment later.
    """

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    model_id: str = Field(serialization_alias="id")
    """The Horde text model name."""

    object_type: Literal["model"] = Field(default="model", serialization_alias="object")
    """Always ``model``."""

    created: int = 0
    """Unix seconds the Horde supplies for the model; 0 when it records none."""

    owned_by: str = "aihorde"
    """Always ``aihorde``."""

    context_length: int | None
    """The largest context an online chat worker declares, in tokens."""

    architecture: ModelArchitecture = Field(default_factory=ModelArchitecture)
    """The modalities served."""

    top_provider: ModelTopProvider
    """The largest context and output an online chat worker declares."""

    supported_parameters: list[str]
    """The OpenAI request fields some online chat worker honours."""

    x_horde: HordeModelDetails
    """The Horde-specific details."""


class ModelList(BaseModel):
    """Represents the OpenAI model list response."""

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    object_type: Literal["list"] = Field(default="list", serialization_alias="object")
    """Always ``list``."""

    data: list[ModelCard]
    """The text models with at least one online chat worker serving them."""


class ChatCompletionChunkToolCall(BaseModel):
    """Represents one whole tool call in a streamed ``delta``, with the position a client accumulates it by."""

    model_config = ConfigDict(frozen=True)

    index: int
    """The position of the call in the reply's ``tool_calls``, from 0."""

    id: str
    """The id the worker returned for the call."""

    type: Literal["function"] = "function"
    """Always ``function``."""

    function: ChatFunctionCall
    """The function called and its arguments."""


class ChatCompletionChunkDelta(BaseModel):
    """Represents the ``delta`` of one streamed choice; unset fields are left out of the chunk."""

    model_config = ConfigDict(frozen=True)

    role: Literal["assistant"] | None = None
    """``assistant`` in the first chunk of a choice, otherwise absent."""

    content: str | None = None
    """The generated text in the content chunk, otherwise absent."""

    tool_calls: list[ChatCompletionChunkToolCall] | None = None
    """The tool calls in the content chunk when the worker returned any, otherwise absent."""


class ChatCompletionChunkChoice(BaseModel):
    """Represents one choice of a streamed chunk."""

    model_config = ConfigDict(frozen=True)

    index: int
    """The position of the choice, in the order the Horde lists its generations."""

    delta: ChatCompletionChunkDelta
    """What this chunk adds to the choice."""

    finish_reason: FinishReason | None = None
    """Why the generation stopped, in the finish chunk; null before it."""

    native_finish_reason: str | None = None
    """The backend's own reason, in the finish chunk when the worker reported it; null otherwise."""

    logprobs: None = None
    """Always null; log probabilities are not served."""


class ChatCompletionChunk(BaseModel):
    """Represents an OpenAI ``chat.completion.chunk`` server-sent event payload."""

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    completion_id: str = Field(serialization_alias="id")
    """``chatcmpl-`` followed by the Horde request id; the same in every chunk of a stream."""

    object_type: Literal["chat.completion.chunk"] = Field(
        default="chat.completion.chunk",
        serialization_alias="object",
    )
    """Always ``chat.completion.chunk``."""

    created: int
    """When the request was submitted, in Unix seconds."""

    model: str
    """The model that generated the first choice."""

    choices: list[ChatCompletionChunkChoice]
    """The choices this chunk adds to; empty in the usage chunk."""

    usage: ChatCompletionUsage | None = None
    """The token counts, in the usage chunk only."""

    system_fingerprint: str | None = None
    """The completion's ``system_fingerprint``; the same in every chunk of a stream."""

    x_horde: HordeResponseDetails | None = None
    """The Horde-specific details, in the finish chunk only."""
