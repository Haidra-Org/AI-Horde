# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validation and mapping for an OpenAI chat-completions request submitted to the horde.

A chat request is a text request whose worker input is a ``messages`` array instead of a prompt.
[`parse_chat_request`][horde.classes.kobold.chat_request.parse_chat_request] accepts the supported
subset of the OpenAI body and rejects everything else by name.
[`build_text_params`][horde.classes.kobold.chat_request.build_text_params],
[`build_model_names`][horde.classes.kobold.chat_request.build_model_names] and
[`build_worker_targeting`][horde.classes.kobold.chat_request.build_worker_targeting] map it onto the text
params, model list and worker targeting, so the text request path sizes, prices and moderates it unchanged.

[`flatten_messages`][horde.classes.kobold.chat_request.flatten_messages] joins the messages into one
string. That string is stored as the request's ``prompt`` and is an estimate only: it feeds the
character-based token estimate, kudos and moderation, and is never sent to a worker. Every rewrite
validation makes to it has to be mirrored onto the messages, which
[`rewrite_message_texts`][horde.classes.kobold.chat_request.rewrite_message_texts] does.

A worker declares which optional request features it implements as
[`ChatFeature`][horde.classes.kobold.chat_request.ChatFeature] names on pop;
[`required_chat_features`][horde.classes.kobold.chat_request.required_chat_features] lists the ones a
request needs. [`build_chat_job_payload`][horde.classes.kobold.chat_request.build_chat_job_payload] fills
the sampling defaults a chat job's payload always carries.

A user message may carry image parts, as a data URL or an http(s) URL. The native route stores each image in object
storage and keeps only a reference to it in the stored messages
([`replace_image_urls`][horde.classes.kobold.chat_request.replace_image_urls]); at pop the reference becomes a signed
URL or a data URL, by the transport the worker declared
([`select_image_transport`][horde.classes.kobold.chat_request.select_image_transport]).
"""

from __future__ import annotations

import enum
import hashlib
import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from horde import exceptions as e
from horde.chat_contract import (
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    MAX_CHAT_IMAGE_BYTES,
    MAX_CHAT_IMAGE_COUNT,
    MAX_CHAT_MESSAGE_COUNT,
    MAX_CHAT_TEXT_BYTES,
    ChatHordeOptions,
)
from horde.classes.kobold.request_fit import (
    MAX_CONTEXT_LENGTH_PARAMETER,
    MAX_LENGTH_PARAMETER,
)
from horde.image import parse_image_data_url

__all__ = [
    "CHAT_IMAGE_DATA_URL_PREFIX",
    "CHAT_IMAGE_PLACEHOLDER",
    "CHAT_IMAGE_REFERENCE_PREFIX",
    "CHAT_REQUEST_CONFLICT_RC",
    "CHAT_SAMPLING_DEFAULTS",
    "DEFAULT_CHAT_MAX_TOKENS",
    "MAX_NATIVE_FINISH_REASON_LENGTH",
    "MAX_REPORTED_TOKEN_COUNT",
    "UNSUPPORTED_CHAT_PARAMETER_RC",
    "ChargeBasis",
    "ChatCompletionRequest",
    "ChatFeature",
    "ChatImageDetail",
    "ChatImageTransport",
    "ChatMessage",
    "ChatRole",
    "ChatSubmittedMessage",
    "FinishReason",
    "build_chat_image_key",
    "build_chat_job_payload",
    "build_model_names",
    "build_operation_fingerprint",
    "build_text_params",
    "build_worker_targeting",
    "flatten_messages",
    "has_image_parts",
    "list_chat_image_keys",
    "list_image_urls",
    "message_text",
    "parse_chat_request",
    "replace_image_urls",
    "required_chat_features",
    "resolve_image_references",
    "rewrite_message_texts",
    "select_image_transport",
    "serialize_messages",
]

UNSUPPORTED_CHAT_PARAMETER_RC = "UnsupportedChatParameter"
"""Return code for a key, value or content part this horde does not serve, so a client can branch on it."""

CHAT_REQUEST_CONFLICT_RC = "ChatRequestConflict"
"""Return code for two keys whose values contradict each other, such as differing token limits."""

DEFAULT_CHAT_MAX_TOKENS = 512
"""``max_length`` for a chat request that sets neither token limit. OpenAI clients usually omit it, and the text
default of 80 tokens is too short for a chat reply."""

SUPPORTED_CHOICE_COUNT = 1
"""The only ``n`` a chat request may ask for. Each choice would need its own model pin and reply to be stored."""

MIN_SAMPLING_PENALTY = -2.0
"""The lowest ``frequency_penalty`` or ``presence_penalty`` OpenAI accepts."""

MAX_SAMPLING_PENALTY = 2.0
"""The highest ``frequency_penalty`` or ``presence_penalty`` OpenAI accepts."""

SUPPORTED_REQUEST_KEYS = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "n",
        "seed",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "stream",
        "stream_options",
        "user",
        "metadata",
        "horde",
    },
)
"""Top-level keys a chat request may carry. Any other key, ``tools`` and ``response_format`` among them, is
refused by name rather than dropped, so a client never receives a reply that silently ignored part of its
request."""

SUPPORTED_MESSAGE_KEYS = frozenset(
    {
        "role",
        "content",
        "name",
    },
)
"""Keys a message may carry. Tool calls and audio live in other keys, which are refused."""

SUPPORTED_HORDE_OPTION_KEYS = frozenset(ChatHordeOptions.model_fields)
"""Keys of the ``horde`` extension object, read from the model the front shares. Other text request options are not
offered on chat requests."""

TEXT_CONTENT_PART_TYPE = "text"
"""The content part type carrying text, which any message may use."""

IMAGE_URL_CONTENT_PART_TYPE = "image_url"
"""The content part type carrying an image, which only a user message may use."""

IMAGE_PART_ROLES = frozenset(
    {
        "user",
    },
)
"""The roles whose messages may carry image parts. OpenAI accepts images only from the user."""

HTTP_URL_PREFIXES = (
    "http://",
    "https://",
)
"""URL schemes the horde fetches an image from, as it does for a source image given by URL."""

CHAT_IMAGE_REFERENCE_PREFIX = "horde-r2://"
"""Prefix of the reference that replaces an image's URL in stored messages. It is followed by the object key in
the source image bucket, so the database never holds image bytes or a fetchable URL."""

CHAT_IMAGE_DATA_URL_PREFIX = "data:image/jpeg;base64,"
"""Prefix of a data URL holding a stored chat image, which is always JPEG."""

CHAT_IMAGE_PLACEHOLDER = "[image]"
"""The text an image part is rendered as in the flattened prompt, so the token estimate and moderation see that the
message carries an image."""

FLATTENED_MESSAGE_SEPARATOR = "\n"
"""Separator between messages, and between the text parts of one message, in the flattened estimate."""

MAX_REPORTED_TOKEN_COUNT = 10_000_000
"""The largest token count a worker may report on submit. A reported count is stored and shown, never charged,
so the cap only rejects values no generation could produce."""

OPERATION_FINGERPRINT_EXCLUDED_HORDE_KEYS = frozenset(
    {
        "dry_run",
        "operation_key",
    },
)
"""The ``horde`` keys left out of the operation fingerprint: the key is the claim itself, and a dry run never
claims one."""

MAX_NATIVE_FINISH_REASON_LENGTH = 32
"""The longest backend finish reason a worker may report, which is the width of its column."""

CHAT_SAMPLING_DEFAULTS: Mapping[str, float | int] = {
    "temperature": 1.0,
    "top_p": 1.0,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "rep_pen": 1.0,
    "top_k": 0,
    "min_p": 0.0,
}
"""The sampling values a chat job's payload carries when the client did not set them. They are OpenAI's defaults,
and neutral for the samplers OpenAI has no key for, so a worker's own defaults never apply to a chat job."""


class FinishReason(enum.StrEnum):
    """Why a worker stopped generating a chat reply, normalised by the worker on submit."""

    STOP = "stop"
    """The model ended the reply or hit a stop sequence."""

    LENGTH = "length"
    """Generation reached ``max_length``."""

    CONTENT_FILTER = "content_filter"
    """The backend withheld or cut the reply on a content rule."""


class ChargeBasis(enum.StrEnum):
    """What a generation's charge and reward are computed from."""

    OUTPUT_ESTIMATE = "output_estimate"
    """The text formula's estimate of the output: the characters returned divided by 4, rounded up, at least 20
    and at most the requested ``max_length``. Identical to a text generation's; reported token counts do not
    enter it."""


class ChatFeature(enum.StrEnum):
    """Optional request features a worker declares in ``chat_features`` on pop.

    A request that sets one of these to a non-default value is only given to workers that declared it. The
    vocabulary grows by adding names; a name never changes meaning.
    """

    SEED = "seed"
    """The backend honours a sampling seed."""

    PRESENCE_PENALTY = "presence_penalty"
    """The backend applies OpenAI's presence penalty."""

    FREQUENCY_PENALTY = "frequency_penalty"
    """The backend applies OpenAI's frequency penalty."""

    VISION = "vision"
    """The loaded model reads image parts. A worker also has to declare an image transport to receive them."""


class ChatImageTransport(enum.StrEnum):
    """How a worker accepts a chat job's images, declared in ``image_transports`` on pop."""

    URL = "url"
    """The worker fetches a signed object storage URL. Preferred whenever a worker declares it."""

    BASE64 = "base64"
    """The worker reads a ``data:image/jpeg;base64,`` URL inlined in the pop response."""


class ChatRole(enum.StrEnum):
    """The message roles a chat request may use."""

    SYSTEM = "system"
    """Instructions to the model."""

    DEVELOPER = "developer"
    """Instructions to the model from the application. Stored as ``system``, which loses its precedence over a
    system message."""

    USER = "user"
    """The person in the conversation."""

    ASSISTANT = "assistant"
    """An earlier model reply."""


class ChatImageDetail(enum.StrEnum):
    """The OpenAI ``detail`` hint of an image part."""

    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ChatTextPart(BaseModel):
    """Represents one text part of a message whose content is an array of parts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"]
    """Always ``text``."""

    text: StrictStr
    """The part's text."""


class ChatImageUrl(BaseModel):
    """Represents the ``image_url`` object of an image part."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: StrictStr
    """A ``data:image/...;base64,`` URL or an ``http(s)`` URL to fetch the image from."""

    detail: ChatImageDetail | None = None
    """The client's detail hint, passed to the worker unchanged."""


class ChatImagePart(BaseModel):
    """Represents one image part of a user message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["image_url"]
    """Always ``image_url``."""

    image_url: ChatImageUrl
    """Where the image is and how the client wants it read."""


class ChatMessage(BaseModel):
    """Represents one message of the conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: ChatRole
    """Who wrote the message."""

    content: StrictStr | list[Annotated[ChatTextPart | ChatImagePart, Field(discriminator="type")]]
    """The message text as one string, or an array of parts. Only a user message may hold image parts."""

    name: StrictStr | None = None
    """An optional participant name, passed to the worker unchanged."""


class ChatCompletionRequest(BaseModel):
    """Represents the supported subset of an OpenAI chat-completions request."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    model: StrictStr = Field(min_length=1)
    """The horde model name to generate with."""

    messages: list[ChatMessage] = Field(min_length=1)
    """The conversation, oldest message first."""

    max_tokens: StrictInt | None = None
    """How many tokens to generate. Must equal ``max_completion_tokens`` when both are set."""

    max_completion_tokens: StrictInt | None = None
    """How many tokens to generate; the newer OpenAI name for ``max_tokens``."""

    temperature: StrictFloat | StrictInt | None = None
    """Sampling temperature."""

    top_p: StrictFloat | StrictInt | None = None
    """Nucleus sampling value."""

    n: StrictInt | None = None
    """How many choices to return. Only 1 is served."""

    seed: StrictInt | None = None
    """A sampling seed, passed to the worker. Only workers that declare ``seed`` serve a request that sets it."""

    stop: StrictStr | list[StrictStr] | None = None
    """One stop sequence or a list of them."""

    frequency_penalty: Annotated[
        StrictFloat | StrictInt | None,
        Field(ge=MIN_SAMPLING_PENALTY, le=MAX_SAMPLING_PENALTY),
    ] = None
    """OpenAI's frequency penalty. A non-zero value needs a worker that declares ``frequency_penalty``."""

    presence_penalty: Annotated[
        StrictFloat | StrictInt | None,
        Field(ge=MIN_SAMPLING_PENALTY, le=MAX_SAMPLING_PENALTY),
    ] = None
    """OpenAI's presence penalty. A non-zero value needs a worker that declares ``presence_penalty``."""

    stream: StrictBool | None = None
    """Accepted and not applied here; streaming is served by the OpenAI-compatible front."""

    stream_options: dict[str, Any] | None = None
    """Accepted and not applied here; streaming is served by the OpenAI-compatible front."""

    user: StrictStr | None = None
    """Accepted for client compatibility and not applied."""

    metadata: dict[str, Any] | None = None
    """Accepted for client compatibility and not applied."""

    horde: ChatHordeOptions = Field(default_factory=ChatHordeOptions)
    """Request options OpenAI has no key for, validated by the model the front shares."""


class ChatSubmittedMessage(BaseModel):
    """Represents the assistant message a worker submits for a chat job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["assistant"]
    """Always ``assistant``."""

    content: StrictStr
    """The reply text. An empty string is a completed reply with no visible text."""


def _raise_unsupported(key_path: str) -> None:
    """Refuse a key, value or content part this horde does not serve.

    Args:
        key_path: Where the unsupported value sits in the request body, such as ``tools`` or
            ``messages[0].content[1].type=image_url``.

    Raises:
        horde.exceptions.BadRequest: Always, with the ``UnsupportedChatParameter`` return code.
    """
    raise e.BadRequest(
        f"'{key_path}' is not supported on chat requests.",
        rc=UNSUPPORTED_CHAT_PARAMETER_RC,
    )


def _reject_unsupported_message(message: object, message_index: int) -> None:
    """Refuse a message carrying a role, key or content part this horde does not serve.

    Values of the wrong type are left for the model validation, which reports them with the rest.

    Args:
        message: One entry of the request's ``messages``.
        message_index: Its position, used to name the offending key.

    Raises:
        horde.exceptions.BadRequest: If the message uses something unsupported.
    """
    if not isinstance(message, Mapping):
        return

    for message_key in message:
        if message_key not in SUPPORTED_MESSAGE_KEYS:
            _raise_unsupported(f"messages[{message_index}].{message_key}")

    role = message.get("role")
    if isinstance(role, str) and role not in {chat_role.value for chat_role in ChatRole}:
        _raise_unsupported(f"messages[{message_index}].role={role}")

    content = message.get("content")
    if not isinstance(content, list):
        return

    for part_index, content_part in enumerate(content):
        if not isinstance(content_part, Mapping):
            continue
        part_type = content_part.get("type")
        if part_type == TEXT_CONTENT_PART_TYPE:
            continue
        if part_type == IMAGE_URL_CONTENT_PART_TYPE and role in IMAGE_PART_ROLES:
            continue
        _raise_unsupported(f"messages[{message_index}].content[{part_index}].type={part_type}")


def _reject_unsupported_keys(request_body: Mapping[str, Any]) -> None:
    """Refuse every key, role and content part outside the supported subset, naming the first one found.

    Args:
        request_body: The decoded JSON body.

    Raises:
        horde.exceptions.BadRequest: If the body uses something unsupported.
    """
    for request_key in request_body:
        if request_key not in SUPPORTED_REQUEST_KEYS:
            _raise_unsupported(request_key)

    horde_options = request_body.get("horde")
    if isinstance(horde_options, Mapping):
        for option_key in horde_options:
            if option_key not in SUPPORTED_HORDE_OPTION_KEYS:
                _raise_unsupported(f"horde.{option_key}")

    messages = request_body.get("messages")
    if isinstance(messages, list):
        for message_index, message in enumerate(messages):
            _reject_unsupported_message(message, message_index)


def _reject_conflicting_values(chat_request: ChatCompletionRequest) -> None:
    """Refuse a request whose values the horde cannot serve or that contradict each other.

    Args:
        chat_request: The validated request.

    Raises:
        horde.exceptions.BadRequest: With ``UnsupportedChatParameter`` for an ``n`` other than 1 or messages over
            the shared size limits, ``ChatRequestConflict`` for differing token limits, or an image validation
            failure for too many, malformed or oversized image URLs.
    """
    if chat_request.n is not None and chat_request.n != SUPPORTED_CHOICE_COUNT:
        _raise_unsupported(f"n={chat_request.n}")

    both_limits_set = chat_request.max_tokens is not None and chat_request.max_completion_tokens is not None
    if both_limits_set and chat_request.max_tokens != chat_request.max_completion_tokens:
        raise e.BadRequest(
            "'max_tokens' and 'max_completion_tokens' must be equal when both are set.",
            rc=CHAT_REQUEST_CONFLICT_RC,
        )

    if len(chat_request.messages) > MAX_CHAT_MESSAGE_COUNT:
        raise e.BadRequest(
            f"A chat request may carry at most {MAX_CHAT_MESSAGE_COUNT} messages.",
            rc=UNSUPPORTED_CHAT_PARAMETER_RC,
        )
    if _message_text_bytes(chat_request) > MAX_CHAT_TEXT_BYTES:
        raise e.BadRequest(
            f"The message text of a chat request may be at most {MAX_CHAT_TEXT_BYTES} bytes.",
            rc=UNSUPPORTED_CHAT_PARAMETER_RC,
        )
    _validate_image_urls(_submitted_image_urls(chat_request))


def _submitted_image_urls(chat_request: ChatCompletionRequest) -> list[str]:
    """Return the URL of every image part the client sent, in message order.

    Args:
        chat_request: The validated request.

    Returns:
        Each image part's ``url``.
    """
    image_urls: list[str] = []
    for message in chat_request.messages:
        if isinstance(message.content, str):
            continue
        image_urls.extend(content_part.image_url.url for content_part in message.content if isinstance(content_part, ChatImagePart))
    return image_urls


def _message_text_bytes(chat_request: ChatCompletionRequest) -> int:
    """Return the UTF-8 size of every message text, measured as the front measures it.

    Args:
        chat_request: The validated request.

    Returns:
        The bytes of every string content and text part, together.
    """
    text_bytes = 0
    for message in chat_request.messages:
        if isinstance(message.content, str):
            text_bytes += len(message.content.encode())
        else:
            text_bytes += sum(len(content_part.text.encode()) for content_part in message.content if isinstance(content_part, ChatTextPart))
    return text_bytes


def _validation_reasons(error: ValidationError) -> str:
    """Return every reason a pydantic ValidationError gives, as one line for a client to read.

    Args:
        error: The error the model raised.

    Returns:
        The reasons, each prefixed with the location it applies to, separated by semicolons.
    """
    reasons: list[str] = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"])
        reasons.append(f"{location}: {detail['msg']}" if location else detail["msg"])

    return "; ".join(reasons)


def _validate_image_url(image_url: str) -> None:
    """Validate the form and size of one image URL without decoding or fetching it.

    Args:
        image_url: The ``url`` of an image part.

    Raises:
        horde.exceptions.ImageValidationFailed: With ``SourceImageUnreadable`` when a ``data:`` URL is not a base64
            image, ``SourceImageUrlInvalid`` when the URL is neither a data URL nor an ``http(s)`` URL, or
            ``SourceImageSizeExceeded`` when a data URL decodes to more than
            [`MAX_CHAT_IMAGE_BYTES`][horde.chat_contract.MAX_CHAT_IMAGE_BYTES].
    """
    base64_payload = parse_image_data_url(image_url)
    if base64_payload is None:
        if image_url.startswith("data:"):
            raise e.ImageValidationFailed("An image data URL must be a base64 encoded image.", rc="SourceImageUnreadable")
        if not image_url.startswith(HTTP_URL_PREFIXES):
            raise e.ImageValidationFailed("An image URL must be a base64 data URL or an http(s) URL.", rc="SourceImageUrlInvalid")
        return

    # Four base64 characters encode three bytes, so the estimate is at most two bytes over the decoded size.
    estimated_decoded_bytes = len(base64_payload) * 3 // 4
    if estimated_decoded_bytes > MAX_CHAT_IMAGE_BYTES:
        raise e.ImageValidationFailed("An image cannot be larger than 5 MiB.", rc="SourceImageSizeExceeded")


def _validate_image_urls(image_urls: Sequence[str]) -> None:
    """Validate the number of image parts and the form of each image URL.

    Args:
        image_urls: Every image part's ``url``, in message order.

    Raises:
        horde.exceptions.BadRequest: With ``TooManyExtraSourceImages`` when there are more than
            [`MAX_CHAT_IMAGE_COUNT`][horde.chat_contract.MAX_CHAT_IMAGE_COUNT] images, or an image validation
            failure for a malformed or oversized image URL.
    """
    if len(image_urls) > MAX_CHAT_IMAGE_COUNT:
        raise e.BadRequest(f"A chat request can carry at most {MAX_CHAT_IMAGE_COUNT} images.", rc="TooManyExtraSourceImages")

    for image_url in image_urls:
        _validate_image_url(image_url)


def parse_chat_request(request_body: object) -> ChatCompletionRequest:
    """Parse and validate a chat-completions request body.

    Args:
        request_body: The decoded JSON body, or None when the body was not JSON.

    Returns:
        The validated request.

    Raises:
        horde.exceptions.BadRequest: With ``UnsupportedChatParameter`` when the body uses a key, role, value or
            content part this horde does not serve, ``ChatRequestConflict`` when two values contradict each
            other, or the plain ``BadRequest`` code when a value has the wrong type or the body is not a JSON
            object.
    """
    if not isinstance(request_body, Mapping):
        raise e.BadRequest("A chat request body must be a JSON object.")

    _reject_unsupported_keys(request_body)
    try:
        chat_request = ChatCompletionRequest.model_validate(request_body)
    except ValidationError as validation_error:
        # The shared horde model reports contradicting keys under its own error type, which keeps its return code.
        if any(detail["type"] == CHAT_REQUEST_CONFLICT_ERROR_TYPE for detail in validation_error.errors()):
            raise e.BadRequest(
                "'horde.only' and 'horde.ignore' cannot be combined.",
                rc=CHAT_REQUEST_CONFLICT_RC,
            ) from None
        raise e.BadRequest(f"The chat request is not valid. {_validation_reasons(validation_error)}.") from None

    _reject_conflicting_values(chat_request)
    _reject_stored_nuls(chat_request)
    return chat_request


def _reject_stored_nuls(chat_request: ChatCompletionRequest) -> None:
    """Refuse NUL in persisted request strings before PostgreSQL text or JSONB can reject the write.

    Metadata and user are deliberately ignored by the native route and are not persisted.
    """
    pending: list[Any] = [serialize_messages(chat_request), chat_request.stop, chat_request.model, chat_request.horde.model_dump()]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and "\x00" in value:
            raise e.BadRequest("Request payload may not contain NUL (0x00) bytes.", rc="NulByteInPayload")
        if isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def serialize_messages(chat_request: ChatCompletionRequest) -> list[dict[str, Any]]:
    """Return the request's messages as the JSON array stored on the request and sent to the worker.

    A ``developer`` message is stored as ``system``, so a worker only meets the roles every chat template knows.

    Args:
        chat_request: The validated request.

    Returns:
        One dict per message, in the shape the client sent, without keys the client left unset.
    """
    serialized_messages: list[dict[str, Any]] = []
    for message in chat_request.messages:
        serialized_message = message.model_dump(mode="json", exclude_none=True)
        if message.role == ChatRole.DEVELOPER:
            serialized_message["role"] = ChatRole.SYSTEM.value
        serialized_messages.append(serialized_message)

    return serialized_messages


def _is_image_part(content_part: Mapping[str, Any]) -> bool:
    """Return whether a content part is an image part."""
    return bool(content_part["type"] == IMAGE_URL_CONTENT_PART_TYPE)


def message_text(message: Mapping[str, Any]) -> str:
    """Return the text of one stored message, joining its parts when the content is an array.

    Each image part is rendered as [`CHAT_IMAGE_PLACEHOLDER`][horde.classes.kobold.chat_request.CHAT_IMAGE_PLACEHOLDER].

    Args:
        message: One entry of the stored ``messages`` array.

    Returns:
        The message's text.
    """
    content = message["content"]
    if isinstance(content, str):
        return content

    return FLATTENED_MESSAGE_SEPARATOR.join(
        CHAT_IMAGE_PLACEHOLDER if _is_image_part(content_part) else content_part["text"] for content_part in content
    )


def has_image_parts(messages: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether any message carries an image part.

    Args:
        messages: A ``messages`` array.

    Returns:
        True when at least one image part is present.
    """
    return any(not isinstance(message["content"], str) and any(_is_image_part(part) for part in message["content"]) for message in messages)


def list_image_urls(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the URL of every image part, in message order.

    An image's position in this list is its ordinal, which names the object stored for it.

    Args:
        messages: A ``messages`` array.

    Returns:
        Each image part's ``url``.
    """
    image_urls: list[str] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            continue
        image_urls.extend(content_part["image_url"]["url"] for content_part in content if _is_image_part(content_part))

    return image_urls


def _replace_image_urls_in_order(
    messages: Sequence[Mapping[str, Any]],
    replace_url: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Return a copy of the messages with each image part's URL replaced, in message order.

    Args:
        messages: A ``messages`` array.
        replace_url: Called once per image part, in order, with its current URL; returns the new URL.

    Returns:
        The messages in the same shape. Messages and parts without an image are copied unchanged.
    """
    replaced_messages: list[dict[str, Any]] = []
    for message in messages:
        replaced_message = dict(message)
        content = message["content"]
        if not isinstance(content, str):
            replaced_message["content"] = [
                {**content_part, "image_url": {**content_part["image_url"], "url": replace_url(content_part["image_url"]["url"])}}
                if _is_image_part(content_part)
                else content_part
                for content_part in content
            ]
        replaced_messages.append(replaced_message)

    return replaced_messages


def list_chat_image_keys(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the object key of every stored image a stored ``messages`` array references.

    Args:
        messages: Stored messages, whose image parts hold references.

    Returns:
        The keys, in message order. An image URL that is not a stored reference contributes nothing.
    """
    return [
        image_url.removeprefix(CHAT_IMAGE_REFERENCE_PREFIX)
        for image_url in list_image_urls(messages)
        if image_url.startswith(CHAT_IMAGE_REFERENCE_PREFIX)
    ]


def select_image_transport(declared_transports: Sequence[str] | None) -> ChatImageTransport | None:
    """Return the transport a worker's images are sent with.

    ``url`` is chosen whenever the worker declared it; ``base64`` only when it is the only one declared.

    Args:
        declared_transports: The ``image_transports`` the worker declared on its last pop.

    Returns:
        The transport, or None when the worker declared none.
    """
    declared = set(declared_transports or ())
    for transport in (ChatImageTransport.URL, ChatImageTransport.BASE64):
        if transport.value in declared:
            return transport
    return None


def build_chat_image_key(request_id: str, ordinal: int) -> str:
    """Return the object key a chat request's image is stored under.

    Args:
        request_id: The chat request's id.
        ordinal: The image's position among the request's image parts, from 0.

    Returns:
        The key in the source image bucket.
    """
    return f"{request_id}_chat_{ordinal}.jpg"


def replace_image_urls(messages: Sequence[Mapping[str, Any]], request_id: str) -> list[dict[str, Any]]:
    """Return a copy of the messages with each image URL replaced by a reference to its stored object.

    Args:
        messages: A ``messages`` array carrying image parts.
        request_id: The chat request's id, which the object keys are built from.

    Returns:
        The messages to store. The ``url`` of the image with ordinal ``n`` is
        [`CHAT_IMAGE_REFERENCE_PREFIX`][horde.classes.kobold.chat_request.CHAT_IMAGE_REFERENCE_PREFIX] followed by
        [`build_chat_image_key`][horde.classes.kobold.chat_request.build_chat_image_key] of ``n``.
    """
    image_ordinals = itertools.count()
    return _replace_image_urls_in_order(
        messages,
        lambda _submitted_url: f"{CHAT_IMAGE_REFERENCE_PREFIX}{build_chat_image_key(request_id, next(image_ordinals))}",
    )


def resolve_image_references(
    messages: Sequence[Mapping[str, Any]],
    resolve_image_key: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Return a copy of stored messages with each image reference resolved into the URL a worker receives.

    Args:
        messages: Stored messages whose image parts hold references.
        resolve_image_key: Called with each image's object key; returns the URL to send in its place.

    Returns:
        The messages in the same shape, each image part's ``url`` replaced.

    Raises:
        ValueError: If an image part's ``url`` is not a stored reference.
    """

    def resolve_reference(image_reference: str) -> str:
        if not image_reference.startswith(CHAT_IMAGE_REFERENCE_PREFIX):
            raise ValueError("A stored chat image URL is not an object reference.")
        return resolve_image_key(image_reference.removeprefix(CHAT_IMAGE_REFERENCE_PREFIX))

    return _replace_image_urls_in_order(messages, resolve_reference)


def flatten_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """Return the messages joined into the single string stored as the request's prompt.

    The string is an estimate only: the token estimate, kudos and moderation read it, and no worker
    ever receives it.

    Args:
        messages: The stored ``messages`` array.

    Returns:
        Each message as ``role: text``, joined with newlines.
    """
    return FLATTENED_MESSAGE_SEPARATOR.join(f"{message['role']}: {message_text(message)}" for message in messages)


def rewrite_message_texts(
    messages: Sequence[Mapping[str, Any]],
    rewrite_text: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return a copy of the messages with every text rewritten.

    Validation rewrites the flattened prompt (moderation replacements, for instance). Applying the same
    rewrite to each text keeps what the worker receives in step with what was validated.

    Args:
        messages: The stored ``messages`` array.
        rewrite_text: The rewrite applied to each text. A None result, which means the rewrite removed
            everything, becomes an empty text.

    Returns:
        The rewritten messages, in the same shape.
    """
    rewritten_messages: list[dict[str, Any]] = []
    for message in messages:
        rewritten_message = dict(message)
        content = message["content"]
        if isinstance(content, str):
            rewritten_message["content"] = rewrite_text(content) or ""
        else:
            rewritten_message["content"] = [
                content_part if _is_image_part(content_part) else {**content_part, "text": rewrite_text(content_part["text"]) or ""}
                for content_part in content
            ]
        rewritten_messages.append(rewritten_message)

    return rewritten_messages


def build_model_names(chat_request: ChatCompletionRequest) -> list[str]:
    """Return the models the request accepts.

    Args:
        chat_request: The validated request.

    Returns:
        ``model`` alone; a chat request runs on exactly one model.
    """
    return [chat_request.model]


def build_worker_targeting(chat_request: ChatCompletionRequest) -> tuple[list[str], bool]:
    """Return the text request's worker list and whether that list excludes rather than limits.

    Args:
        chat_request: The validated request.

    Returns:
        ``horde.only`` with False, ``horde.ignore`` with True, or an empty list with False when neither is set.
    """
    if chat_request.horde.only is not None:
        return list(chat_request.horde.only), False
    if chat_request.horde.ignore is not None:
        return list(chat_request.horde.ignore), True
    return [], False


def build_text_params(chat_request: ChatCompletionRequest) -> dict[str, Any]:
    """Return the text params the request maps onto.

    ``max_tokens`` and ``max_completion_tokens`` set ``max_length``; with neither, it is
    [`DEFAULT_CHAT_MAX_TOKENS`][horde.classes.kobold.chat_request.DEFAULT_CHAT_MAX_TOKENS]. ``stop`` becomes
    ``stop_sequence``. Keys the client left unset are left out; the job payload fills their defaults.

    Args:
        chat_request: The request, validated by
            [`parse_chat_request`][horde.classes.kobold.chat_request.parse_chat_request].

    Returns:
        A params dict in the shape the text params model accepts.
    """
    max_length = chat_request.max_completion_tokens
    if max_length is None:
        max_length = chat_request.max_tokens
    if max_length is None:
        max_length = DEFAULT_CHAT_MAX_TOKENS

    text_params: dict[str, Any] = {MAX_LENGTH_PARAMETER: max_length}
    if chat_request.horde.max_context_length is not None:
        text_params[MAX_CONTEXT_LENGTH_PARAMETER] = chat_request.horde.max_context_length
    if chat_request.temperature is not None:
        text_params["temperature"] = chat_request.temperature
    if chat_request.top_p is not None:
        text_params["top_p"] = chat_request.top_p
    if chat_request.frequency_penalty is not None:
        text_params["frequency_penalty"] = chat_request.frequency_penalty
    if chat_request.presence_penalty is not None:
        text_params["presence_penalty"] = chat_request.presence_penalty
    if chat_request.seed is not None:
        text_params["seed"] = chat_request.seed
    if chat_request.stop is not None:
        text_params["stop_sequence"] = [chat_request.stop] if isinstance(chat_request.stop, str) else list(chat_request.stop)

    return text_params


def build_operation_fingerprint(chat_request: ChatCompletionRequest) -> str:
    """Return the SHA-256 hex digest that tells a repeated submission of one operation key from a changed one.

    The hash covers the request as the horde applies it: the messages as stored (``developer`` rewritten to
    ``system``, unset keys left out), the text params the request maps onto, the model list, and every ``horde``
    option with its default filled in, except ``operation_key`` and ``dry_run``. Two bodies that differ only in how
    they say the same thing, such as ``max_tokens`` against ``max_completion_tokens`` or an option sent at its
    default against one left out, share a fingerprint. Keys that are accepted and not applied (``stream``,
    ``stream_options``, ``user``, ``metadata``) are not in the hash.

    The serialisation is canonical JSON: keys sorted, no insignificant whitespace, non-ASCII text kept as UTF-8.

    Args:
        chat_request: The validated request.

    Returns:
        64 lowercase hexadecimal characters.
    """
    effective_request = {
        "messages": serialize_messages(chat_request),
        "params": build_text_params(chat_request),
        "models": build_model_names(chat_request),
        "horde": chat_request.horde.model_dump(mode="json", exclude=set(OPERATION_FINGERPRINT_EXCLUDED_HORDE_KEYS)),
    }
    canonical_json = json.dumps(effective_request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def required_chat_features(params: Mapping[str, Any]) -> set[ChatFeature]:
    """Return the worker features a chat request's params need.

    A value at its documented default needs nothing, so a request that sends a zero penalty is served by any chat
    worker.

    Args:
        params: The request's stored text params.

    Returns:
        ``seed`` when a seed is set, and each penalty whose value is not zero.
    """
    needed_features: set[ChatFeature] = set()
    if params.get("seed") is not None:
        needed_features.add(ChatFeature.SEED)
    for penalty_feature in (ChatFeature.FREQUENCY_PENALTY, ChatFeature.PRESENCE_PENALTY):
        default_penalty = CHAT_SAMPLING_DEFAULTS[penalty_feature.value]
        if params.get(penalty_feature.value, default_penalty) != default_penalty:
            needed_features.add(penalty_feature)

    return needed_features


def build_chat_job_payload(params: Mapping[str, Any], *, max_length: int, max_context_length: int) -> dict[str, Any]:
    """Return the payload a chat job is sent with, apart from its messages.

    Every sampling field in
    [`CHAT_SAMPLING_DEFAULTS`][horde.classes.kobold.chat_request.CHAT_SAMPLING_DEFAULTS] is present, with its
    default where the client left it unset; ``stop_sequence`` and ``seed`` only when the client sent them. There is
    no prompt.

    Args:
        params: The request's stored text params.
        max_length: The validated number of tokens to generate.
        max_context_length: The validated context length.

    Returns:
        The payload, with ``n`` of 1.
    """
    chat_payload: dict[str, Any] = {
        sampling_field: params.get(sampling_field, default_value) for sampling_field, default_value in CHAT_SAMPLING_DEFAULTS.items()
    }
    for optional_field in ("stop_sequence", "seed"):
        if params.get(optional_field) is not None:
            chat_payload[optional_field] = params[optional_field]
    chat_payload[MAX_LENGTH_PARAMETER] = max_length
    chat_payload[MAX_CONTEXT_LENGTH_PARAMETER] = max_context_length
    chat_payload["n"] = SUPPORTED_CHOICE_COUNT
    return chat_payload
