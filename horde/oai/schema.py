# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OpenAI chat completions request, response and error models the front accepts and returns.

The request side is screened in two steps before validation.
[`find_refused_parameter`][horde.oai.schema.find_refused_parameter] returns the first key, role, content part or
``horde`` option the service does not serve, so the client gets a 400 naming it rather than a silently degraded
answer. [`drop_default_no_ops`][horde.oai.schema.drop_default_no_ops] then removes the keys listed in
[`DEFAULT_NO_OP_PARAMETERS`][horde.oai.schema.DEFAULT_NO_OP_PARAMETERS] when they hold their documented default,
because stock clients send them unprompted. The table is a closed list; any other key outside
[`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest] is refused.

[`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest] then validates what remains with ``extra="forbid"``.
Values outside the service contract (``n`` other than 1, a token limit outside 16 to 4096, more than four stop
sequences) raise the error type [`UNSUPPORTED_PARAMETER_ERROR_TYPE`][horde.oai.schema.UNSUPPORTED_PARAMETER_ERROR_TYPE]
so the front reports them as ``UnsupportedChatParameter``; contradictory values raise
[`CHAT_REQUEST_CONFLICT_ERROR_TYPE`][horde.chat_contract.CHAT_REQUEST_CONFLICT_ERROR_TYPE]. A request that sets no
token limit is forwarded without one and the native route applies its default of 512.

The response side follows the OpenAI ``chat.completion``, ``chat.completion.chunk``, error and ``model`` shapes;
``x_horde`` carries the Horde-specific details. Error envelopes always carry ``message``, ``type``, ``param`` and
``code``, with null written explicitly, plus a ``metadata`` object holding only the details that are known.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

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

from horde.chat_contract import CHAT_REQUEST_CONFLICT_ERROR_TYPE, INTERNAL_HORDE_OPTION_KEYS, ChatHordeOptions

__all__ = [
    "BAD_REQUEST_CODE",
    "CHAT_REQUEST_CONFLICT_CODE",
    "DEFAULT_NO_OP_PARAMETERS",
    "UNSUPPORTED_PARAMETER_CODE",
    "UNSUPPORTED_PARAMETER_ERROR_TYPE",
    "ChatCompletionChoice",
    "ChatCompletionChunk",
    "ChatCompletionChunkChoice",
    "ChatCompletionChunkDelta",
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
    "find_refused_parameter",
]

UNSUPPORTED_PARAMETER_CODE: str = "UnsupportedChatParameter"
"""The error code for a key, role, part or value the service does not serve; the native route returns the same rc."""

BAD_REQUEST_CODE: str = "BadRequest"
"""The error code for a body that is not valid JSON or fails validation; the Horde's generic rc for a 400."""

CHAT_REQUEST_CONFLICT_CODE: str = "ChatRequestConflict"
"""The error code for two keys whose values contradict each other; the native route returns the same rc."""

UNSUPPORTED_PARAMETER_ERROR_TYPE: str = "unsupported_chat_parameter"
"""The pydantic error type for a value outside the service contract, reported as ``UnsupportedChatParameter``."""

SUPPORTED_CHOICE_COUNT: int = 1
"""The only ``n`` served: each further choice would need its own pinned model and stored reply."""

MIN_MAX_TOKENS: int = 16
"""The smallest token limit served; the native minimum for ``max_length``."""

MAX_MAX_TOKENS: int = 4096
"""The largest token limit served; the native cap for ``max_length``."""

MAX_STOP_SEQUENCES: int = 4
"""The most stop sequences a request may carry, as OpenAI documents."""

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
    part's path names its type the way the native route does."""

    message: str
    """The explanation returned to the client."""


NoOpPredicate = Callable[[object, Mapping[str, object]], bool]
"""Return whether a key's value, given the whole request body, is the default that may be dropped."""


def _is_empty_or_null(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is null or an empty list."""
    return value is None or (isinstance(value, list) and not value)


def _is_null_or_none_choice(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is null or the string ``none``."""
    return value is None or value == "none"


def _is_text_response_format(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is exactly ``{"type": "text"}``."""
    return value == {"type": "text"}


def _is_false_or_null(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is false or null."""
    return value is None or value is False


def _is_zero_or_null(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is the integer 0 or null; ``false`` is not 0 here."""
    return value is None or (type(value) is int and value == 0)


def _is_parallel_tool_calls_without_tools(value: object, request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is a boolean or null and the request carries no tools."""
    is_boolean_or_null = value is None or isinstance(value, bool)
    return is_boolean_or_null and _is_empty_or_null(request_body.get("tools"), request_body)


def _is_false(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is false."""
    return value is False


def _is_default_service_tier(value: object, _request_body: Mapping[str, object]) -> bool:
    """Return whether ``value`` is ``auto`` or ``default``."""
    return value == "auto" or value == "default"


DEFAULT_NO_OP_PARAMETERS: Mapping[str, NoOpPredicate] = MappingProxyType(
    {
        "tools": _is_empty_or_null,
        "tool_choice": _is_null_or_none_choice,
        "response_format": _is_text_response_format,
        "logprobs": _is_false_or_null,
        "top_logprobs": _is_zero_or_null,
        "parallel_tool_calls": _is_parallel_tool_calls_without_tools,
        "store": _is_false,
        "service_tier": _is_default_service_tier,
    },
)
"""Keys accepted and dropped when they hold their documented default, each with the test for that default.

Stock clients send these unprompted. Any other value of one of these keys is refused by name, as is any key that is
neither here nor a field of [`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest].
"""

ACCEPTED_MESSAGE_KEYS: frozenset[str] = frozenset(
    {
        "role",
        "content",
        "name",
    },
)
"""The message keys a chat message carries; others (``tool_calls``, ``audio``, ...) belong to features not served."""

ACCEPTED_ROLES: frozenset[str] = frozenset(chat_role.value for chat_role in ChatRole)
"""The role strings a message may carry; ``tool`` and ``function`` belong to features not served."""


def find_refused_parameter(request_body: Mapping[str, object]) -> RefusedParameter | None:
    """Return the first key, role, content part or ``horde`` option of a raw chat request that is not served, or None.

    Malformed values of served keys are left to [`ChatCompletionRequest`][horde.oai.schema.ChatCompletionRequest]
    validation; this only finds what the service does not serve at all.

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
    """Return the first refused key, role or content part of one raw message, or None."""
    for message_key in message:
        if message_key not in ACCEPTED_MESSAGE_KEYS:
            return RefusedParameter(
                parameter=f"messages[{message_index}].{message_key}",
                message=f"The message key '{message_key}' is not supported; only text messages are served.",
            )
    role = message.get("role")
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

    Call it only after [`find_refused_parameter`][horde.oai.schema.find_refused_parameter] found nothing, which
    guarantees every such key holds its default.

    Args:
        request_body: The decoded JSON request body.
    """
    return {request_key: value for request_key, value in request_body.items() if request_key not in DEFAULT_NO_OP_PARAMETERS}


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
    """Represents one message of a chat request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ChatRole
    """Who wrote the message."""

    content: StrictStr | Annotated[list[ContentPart], Field(min_length=1)]
    """The message text as one string, or an array of parts; only a user message may hold image parts, which
    [`find_refused_parameter`][horde.oai.schema.find_refused_parameter] enforces before validation."""

    name: StrictStr | None = None
    """An optional participant name, passed through to the worker."""


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


class ChatCompletionRequest(BaseModel):
    """Represents an OpenAI chat completions request in the subset the service serves.

    A null value means the key was omitted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    model: StrictStr = Field(min_length=1)
    """The Horde text model name."""

    messages: list[ChatMessage] = Field(min_length=1)
    """The conversation so far, in order."""

    stream: StrictBool | None = None
    """Answer as server-sent events; the whole text arrives in one content chunk."""

    stream_options: StreamOptions | None = None
    """Options for a streamed answer; only allowed with ``stream: true``."""

    max_tokens: StrictInt | None = None
    """The most tokens to generate, 16 to 4096."""

    max_completion_tokens: StrictInt | None = None
    """The newer name for ``max_tokens``; both must agree when both are set."""

    temperature: StrictFloat | StrictInt | None = None
    """The sampling temperature."""

    top_p: StrictFloat | StrictInt | None = None
    """The nucleus sampling probability."""

    frequency_penalty: StrictFloat | StrictInt | None = None
    """The frequency penalty; a non-default value needs a worker that declares it."""

    presence_penalty: StrictFloat | StrictInt | None = None
    """The presence penalty; a non-default value needs a worker that declares it."""

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

    horde: ChatHordeOptions | None = None
    """The Horde extension object."""

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
    def validate_token_limit_range(cls, token_limit: int | None) -> int | None:
        """Validate that a token limit is within the served range; it is never clamped.

        Raises:
            PydanticCustomError: The limit is outside 16 to 4096.
        """
        if token_limit is not None and not MIN_MAX_TOKENS <= token_limit <= MAX_MAX_TOKENS:
            raise _unsupported_value_error(f"The token limit must be between {MIN_MAX_TOKENS} and {MAX_MAX_TOKENS}.")
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
        if isinstance(stop, list) and len(stop) > MAX_STOP_SEQUENCES:
            raise _unsupported_value_error(f"At most {MAX_STOP_SEQUENCES} stop sequences are served.")
        return stop

    @field_validator("n")
    @classmethod
    def validate_single_choice(cls, choice_count: int | None) -> int | None:
        """Validate that one choice is asked for.

        Raises:
            PydanticCustomError: ``n`` is set to anything but 1.
        """
        if choice_count is not None and choice_count != SUPPORTED_CHOICE_COUNT:
            raise _unsupported_value_error(f"Only {SUPPORTED_CHOICE_COUNT} choice is served; 'n' must be 1 or omitted.")
        return choice_count


def _drop_absent_fields(model: BaseModel, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
    """Return the serialised ``model`` without its null fields."""
    serialised: dict[str, object] = handler(model)
    return {field_name: value for field_name, value in serialised.items() if value is not None}


class ChatCompletionResponseMessage(BaseModel):
    """Represents the assistant message of one completion choice."""

    model_config = ConfigDict(frozen=True)

    role: Literal["assistant"] = "assistant"
    """Always ``assistant``."""

    content: str
    """The generated text."""


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


class ChatCompletionChunkDelta(BaseModel):
    """Represents the ``delta`` of one streamed choice; unset fields are left out of the chunk."""

    model_config = ConfigDict(frozen=True)

    role: Literal["assistant"] | None = None
    """``assistant`` in the first chunk of a choice, otherwise absent."""

    content: str | None = None
    """The generated text in the content chunk, otherwise absent."""


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

    x_horde: HordeResponseDetails | None = None
    """The Horde-specific details, in the finish chunk only."""
