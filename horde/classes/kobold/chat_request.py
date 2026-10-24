# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validation and mapping for an OpenAI chat-completions request submitted to the horde.

A chat request is a text request whose worker input is a ``messages`` array instead of a prompt.
[`parse_chat_request`][horde.classes.kobold.chat_request.parse_chat_request] accepts the supported
subset of the OpenAI body and rejects everything else, with the offending key in the message.
[`build_text_params`][horde.classes.kobold.chat_request.build_text_params],
[`build_model_names`][horde.classes.kobold.chat_request.build_model_names] and
[`build_worker_targeting`][horde.classes.kobold.chat_request.build_worker_targeting] map it onto the text
params, model list and worker targeting, so the text request path sizes, prices and moderates it unchanged.

[`flatten_messages`][horde.classes.kobold.chat_request.flatten_messages] joins the messages into one
string. That string is stored as the request's ``prompt`` and is an estimate only: it feeds the
character-based token estimate, kudos and moderation, and is never sent to a worker. Every rewrite
validation makes to it has to be mirrored onto the messages, which
[`rewrite_message_texts`][horde.classes.kobold.chat_request.rewrite_message_texts] does.

A worker declares on pop which optional request features it implements and which parts of the OpenAI contract it
follows exactly, as the SDK's ``ChatFeature`` and ``ChatGuarantee`` values (ADR 0036).
[`required_chat_features`][horde.classes.kobold.chat_request.required_chat_features] lists the ones a request needs
under its conformance mode. [`build_chat_job_payload`][horde.classes.kobold.chat_request.build_chat_job_payload] fills
the sampling defaults a chat job's payload always carries.

A request may offer the model tools and ask for a response format; the values the request applies are stored apart
from the messages ([`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras]) and narrowed for the
worker at dispatch ([`build_worker_chat_extras`][horde.classes.kobold.chat_request.build_worker_chat_extras]). The
history may carry the model's earlier tool calls and ``tool`` messages answering them. On submit the reply is checked
against the stored values under the request's conformance mode
([`verify_chat_reply`][horde.classes.kobold.chat_request.verify_chat_reply]), so a worker that declares a guarantee
it does not keep has its submit refused rather than the client receiving a reply the request ruled out.

A user message may carry image parts, as a data URL or an http(s) URL. The native route stores each image in object
storage and keeps only a reference to it in the stored messages
([`replace_image_urls`][horde.classes.kobold.chat_request.replace_image_urls]); at pop the reference becomes a signed
URL or a data URL, by the transport the worker declared
([`select_chat_transport`][horde.classes.kobold.chat_request.select_chat_transport]). A large request's whole
``messages`` array, images inlined, may also be stored as one object at submit
([`build_chat_messages_offload`][horde.classes.kobold.chat_request.build_chat_messages_offload]); a worker that
declared the ``messages_url`` transport then receives a signed URL to it in place of the messages.
"""

from __future__ import annotations

import base64
import enum
import gzip
import hashlib
import itertools
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Annotated, Any, Literal, NamedTuple, NoReturn

from horde_sdk.generation_parameters.text.chat_limits import (
    CHAT_SAMPLING_DEFAULTS,
    DEFAULT_PARALLEL_TOOL_CALLS,
    MAX_CHAT_TEXT_BYTES,
    MAX_CHAT_TOOL_COUNT,
    MAX_CHAT_TOOL_SPEC_BYTES,
    OPENAI_SAMPLING_RANGES,
    SUPPORTED_CHAT_CHOICE_COUNT,
)
from horde_sdk.generation_parameters.text.chat_policy import (
    ChatConformance,
    ChatFeature,
    ChatGuarantee,
    ChatReasoningEffort,
    ChatRequestTraits,
    required_chat_guarantees,
)
from horde_sdk.generation_parameters.text.chat_policy import ChatResponseFormatType as PolicyResponseFormatType
from horde_sdk.generation_parameters.text.chat_policy import ChatToolChoiceMode as PolicyToolChoiceMode
from horde_sdk.worker.chat_declaration import ChatFaultReason, ChatFinishReason, ChatImageTransport
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from referencing.exceptions import Unresolvable

from horde import exceptions as e
from horde.chat_contract import (
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    CHAT_REQUEST_CONFLICT_RC,
    MAX_CHAT_IMAGE_BYTES,
    MAX_CHAT_IMAGE_COUNT,
    MAX_CHAT_MESSAGE_COUNT,
    MAX_CHAT_MODEL_COUNT,
    MAX_CHAT_TOOL_CALL_COUNT,
    MAX_CHAT_TOOL_CALL_ID_LENGTH,
    UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE,
    UNSUPPORTED_CHAT_PARAMETER_RC,
    ChatHordeOptions,
    ChatNamedToolChoice,
    ChatRequestRefusal,
    ChatResponseFormat,
    ChatResponseFormatType,
    ChatTool,
    ChatToolCall,
    ChatToolChoice,
    ChatToolChoiceMode,
    chat_tool_call_bytes,
    check_json_word,
    check_model_choice,
    check_strict_response_schema,
    check_strict_tool_schemas,
    check_tool_fields_need_tools,
    check_tool_history,
    refuse_json_constant,
)
from horde.classes.kobold.request_fit import (
    MAX_CONTEXT_LENGTH_PARAMETER,
    MAX_LENGTH_PARAMETER,
    estimate_prompt_tokens,
    prompt_fits_context,
)
from horde.image import parse_image_data_url
from horde.r2 import CHAT_MESSAGES_KEY_SUFFIX

__all__ = [
    "CHAT_IMAGE_DATA_URL_PREFIX",
    "CHAT_IMAGE_PLACEHOLDER",
    "CHAT_IMAGE_REFERENCE_PREFIX",
    "CHAT_REQUEST_CONFLICT_RC",
    "CHAT_STREAM_PROGRESS_KEY",
    "CHAT_SUBMIT_INVALID_RC",
    "CHAT_SUBMIT_NONCONFORMANT_RC",
    "CHAT_WORKER_FAULT_MESSAGES",
    "CHAT_WORKER_FAULT_RC",
    "DEFAULT_CHAT_MAX_TOKENS",
    "MAX_CHAT_FAULT_REASON_LENGTH",
    "MAX_CHAT_REFUSAL_RC_LENGTH",
    "MAX_NATIVE_FINISH_REASON_LENGTH",
    "MAX_REPORTED_TOKEN_COUNT",
    "UNSUPPORTED_CHAT_PARAMETER_RC",
    "ChargeBasis",
    "ChatCompletionRequest",
    "ChatImageDetail",
    "ChatMessage",
    "ChatHistoryTruncation",
    "ChatMessagesOffload",
    "ChatRole",
    "ChatSubmittedMessage",
    "build_chat_extras",
    "build_chat_image_key",
    "build_chat_job_payload",
    "build_chat_messages_key",
    "build_chat_messages_offload",
    "build_chat_request_traits",
    "build_model_names",
    "build_operation_fingerprint",
    "build_stored_chat_extras",
    "build_stored_reply",
    "build_text_params",
    "build_worker_chat_extras",
    "build_worker_targeting",
    "chat_reply_characters",
    "chat_reply_finish_reason",
    "chat_tools_temperature",
    "chat_worker_fault_message",
    "flatten_messages",
    "has_image_parts",
    "inline_chat_images",
    "list_chat_image_keys",
    "list_image_urls",
    "list_stored_chat_object_keys",
    "message_text",
    "parse_chat_request",
    "replace_image_urls",
    "required_chat_features",
    "resolve_image_references",
    "rewrite_message_texts",
    "select_chat_transport",
    "serialize_messages",
    "streams_progress",
    "truncate_oldest_messages",
    "verify_chat_reply",
]

CHAT_SUBMIT_INVALID_RC = "ChatSubmitInvalid"
"""Return code for a chat submit no client could use under any conformance mode: a malformed message, a call to a
function the request did not offer, or two calls sharing an id."""

CHAT_SUBMIT_NONCONFORMANT_RC = "ChatSubmitNonconformant"
"""Return code for a well-formed reply a ``strict`` request's ``tool_choice`` or ``response_format`` rules out, so a
worker can tell a reply its backend broke from one that contradicts the request contract."""

CHAT_WORKER_FAULT_RC = "ChatWorkerFault"
"""Return code stored on a chat request whose worker reported why it faulted a generation, so a client can tell a
worker-side failure from a reply the Horde refused."""

MAX_CHAT_REFUSAL_RC_LENGTH = 32
"""The width of the columns holding the return code of a refused chat submit. The longest code a chat submit is
refused with, ``ChatSubmitNonconformant``, has 23 characters."""

MAX_CHAT_FAULT_REASON_LENGTH = 1000
"""The width of the column holding the message of the last refused reply to a chat request. A refusal message is one
sentence, and a schema mismatch adds only the schema name, a keyword and a JSON path, so only a deeply nested path
reaches this width; a longer message is stored cut to it."""

DEFAULT_CHAT_MAX_TOKENS = 512
"""``max_length`` for a chat request that sets neither token limit. OpenAI clients usually omit it, and the text
default of 80 tokens is too short for a chat reply."""

CHAT_STREAM_PROGRESS_KEY = "stream_progress"
"""The stored extras key, and the job payload key, of the stream hint: ``true`` when the client streams the reply,
so the worker posts deltas to the progress route while it generates. Absent otherwise."""

SUPPORTED_REQUEST_KEYS = frozenset(
    {
        "model",
        "models",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "n",
        "seed",
        "reasoning_effort",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "stream",
        "stream_options",
        "user",
        "metadata",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "horde",
    },
)
"""Top-level keys a chat request may carry. Any other key, ``logprobs`` and ``audio`` among them, is refused with
the key in the message rather than dropped, so a client never receives a reply that silently ignored part of its request."""

SUPPORTED_MESSAGE_KEYS = frozenset(
    {
        "role",
        "content",
        "name",
    },
)
"""Keys a system, developer or user message may carry. Audio lives in other keys, which are refused."""

ROLE_MESSAGE_KEYS: Mapping[str, frozenset[str]] = {
    "assistant": SUPPORTED_MESSAGE_KEYS | {"tool_calls"},
    "tool": frozenset(
        {
            "role",
            "content",
            "tool_call_id",
        },
    ),
}
"""Keys a message of these roles may carry in place of
[`SUPPORTED_MESSAGE_KEYS`][horde.classes.kobold.chat_request.SUPPORTED_MESSAGE_KEYS]. Only an assistant message
records tool calls, and only a ``tool`` message answers one; OpenAI defines no participant name on a ``tool``
message, so one is refused like any other key outside the schema."""

MESSAGE_KEY_NAMES = frozenset().union(SUPPORTED_MESSAGE_KEYS, *ROLE_MESSAGE_KEYS.values())
"""Every message key some role may carry, which a message whose role is not a string is checked against."""

SUPPORTED_HORDE_OPTION_KEYS = frozenset(ChatHordeOptions.model_fields)
"""Keys of the ``horde`` extension object, read from the model the front shares. Other text request options are not
offered on chat requests."""

TEXT_CONTENT_PART_TYPE = "text"
"""The content part type carrying text, which any message may use."""

IMAGE_URL_CONTENT_PART_TYPE = "image_url"
"""The content part type carrying an image, which only a user message may use."""

COMPACT_JSON_SEPARATORS = (",", ":")
"""The separators of compact JSON, used for the tool spec bound and the flattened prompt alike."""

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

CHAT_MESSAGES_GZIP_LEVEL = 6
"""The gzip level an offloaded ``messages`` body is compressed at, zlib's default. Most of a large body is base64
JPEG, which compresses by about a quarter at any level, so a higher level adds compression time at submit for little
saving."""

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
        "availability_mode",
        "dry_run",
        "include_reasoning",
        "max_kudos",
        "operation_key",
    },
)
"""The ``horde`` keys left out of the operation fingerprint: the key is the claim itself, a dry run never
claims one, and ``include_reasoning`` changes what the front delivers and leaves the work unchanged."""

MAX_NATIVE_FINISH_REASON_LENGTH = 32
"""The longest backend finish reason a worker may report, which is the width of its column."""

CHAT_WORKER_FAULT_MESSAGES: Mapping[str, str] = {
    ChatFaultReason.PROMPT_OVERFLOW: "The worker could not fit the prompt into its context.",
    ChatFaultReason.UNBUILDABLE_GRAMMAR: "The worker could not build the grammar the request's tool choice or response format needs.",
    ChatFaultReason.TOOL_VIOLATION: "The model's reply broke the request's tool controls.",
    ChatFaultReason.REPLY_REFUSED: "The AI Horde refused the worker's reply.",
    ChatFaultReason.GENERATION_ERROR: "The worker's backend failed while generating.",
}
"""The message the Horde stores for each fault reason a worker reports, one per the SDK's ``ChatFaultReason`` values.
The Horde writes its own sentence rather than any worker text, so a client reads a fixed vocabulary."""


def chat_worker_fault_message(fault_reason: object) -> str | None:
    """Return the stored message for a fault reason a worker sent, or None when the value is not a known reason.

    An unknown value is ignored rather than refused, so a worker sending a reason this Horde does not know still has
    its fault accepted.

    Args:
        fault_reason: The ``fault_reason`` of a faulted chat submit, as parsed.

    Returns:
        The message for the reason, or None.
    """
    if not isinstance(fault_reason, str):
        return None
    return CHAT_WORKER_FAULT_MESSAGES.get(fault_reason)


class ChargeBasis(enum.StrEnum):
    """What a generation's charge and reward are computed from."""

    OUTPUT_ESTIMATE = "output_estimate"
    """The text formula's estimate of the output: the characters returned divided by 4, rounded up, at least 20
    and at most the requested ``max_length``. Identical to a text generation's; reported token counts do not
    enter it."""


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
    """An earlier model reply, which may carry the tool calls the model made."""

    TOOL = "tool"
    """The result of a tool call, answering one of an earlier assistant message's ``tool_calls`` by id."""


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

    content: StrictStr | list[Annotated[ChatTextPart | ChatImagePart, Field(discriminator="type")]] | None = None
    """The message text as one string, or an array of parts. Only a user message may hold image parts. Null only on
    an assistant message that carries ``tool_calls``."""

    name: StrictStr | None = None
    """An optional participant name, passed to the worker unchanged."""

    tool_calls: list[ChatToolCall] | None = Field(default=None, min_length=1, max_length=MAX_CHAT_TOOL_CALL_COUNT)
    """The tool calls an assistant message made, passed to the worker unchanged."""

    tool_call_id: StrictStr | None = Field(default=None, min_length=1, max_length=MAX_CHAT_TOOL_CALL_ID_LENGTH)
    """The id of the tool call a ``tool`` message answers; required on a ``tool`` message."""

    @model_validator(mode="after")
    def validate_content_and_tool_fields(self) -> ChatMessage:
        """Validate that only a tool-calling message omits its content and that a ``tool`` message references its call.

        Which roles may carry ``tool_calls`` and ``tool_call_id`` at all is checked by key before validation.

        Raises:
            ValueError: The content is null without tool calls, or a ``tool`` message has no ``tool_call_id``.
        """
        if self.content is None and not self.tool_calls:
            raise ValueError("'content' may be null only on an assistant message that carries 'tool_calls'")
        if self.role == ChatRole.TOOL and self.tool_call_id is None:
            raise ValueError("a 'tool' message needs 'tool_call_id'")
        return self


class ChatCompletionRequest(BaseModel):
    """Represents the supported subset of an OpenAI chat-completions request."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    model: StrictStr | None = Field(default=None, min_length=1)
    """The horde model name to generate with. A request sets exactly one of ``model`` and ``models``."""

    models: list[Annotated[StrictStr, Field(min_length=1)]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CHAT_MODEL_COUNT,
    )
    """The horde model names the request accepts, at most
    [`MAX_CHAT_MODEL_COUNT`][horde.chat_contract.MAX_CHAT_MODEL_COUNT], each listed once. Whichever of them an online
    worker runs serves the request, and the generation records the model that served it."""

    messages: list[ChatMessage] = Field(min_length=1)
    """The conversation, oldest message first."""

    max_tokens: StrictInt | None = None
    """How many tokens to generate. Must equal ``max_completion_tokens`` when both are set."""

    max_completion_tokens: StrictInt | None = None
    """How many tokens to generate; the newer OpenAI key for ``max_tokens``."""

    temperature: StrictFloat | StrictInt | None = None
    """Sampling temperature."""

    top_p: StrictFloat | StrictInt | None = None
    """Nucleus sampling value."""

    n: StrictInt | None = None
    """How many choices to return. Only 1 is served."""

    seed: StrictInt | None = None
    """A sampling seed, passed to the worker. Only workers that declare ``seed`` serve a request that sets it."""

    reasoning_effort: ChatReasoningEffort | None = None
    """How much the model reasons before answering, one of OpenAI's values, passed to the worker unchanged and without
    a default. Only workers that declare ``reasoning_effort`` serve a request that sets it, ``none`` included."""

    stop: StrictStr | list[StrictStr] | None = None
    """One stop sequence or a list of them."""

    frequency_penalty: Annotated[
        StrictFloat | StrictInt | None,
        Field(ge=OPENAI_SAMPLING_RANGES["frequency_penalty"][0], le=OPENAI_SAMPLING_RANGES["frequency_penalty"][1]),
    ] = None
    """OpenAI's frequency penalty. A non-zero value needs a worker that declares ``frequency_penalty``."""

    presence_penalty: Annotated[
        StrictFloat | StrictInt | None,
        Field(ge=OPENAI_SAMPLING_RANGES["presence_penalty"][0], le=OPENAI_SAMPLING_RANGES["presence_penalty"][1]),
    ] = None
    """OpenAI's presence penalty. A non-zero value needs a worker that declares ``presence_penalty``."""

    stream: StrictBool | None = None
    """Whether a client streams the reply. ``true`` is stored as the stream hint, which asks the worker to post
    deltas while it generates; the reply and its status are the same either way."""

    stream_options: dict[str, Any] | None = None
    """Accepted and not applied here; streaming is served by the OpenAI-compatible front."""

    user: StrictStr | None = None
    """Accepted for client compatibility and not applied."""

    metadata: dict[str, Any] | None = None
    """Accepted for client compatibility and not applied."""

    tools: list[ChatTool] | None = None
    """The functions the model may call, at most
    the SDK's ``MAX_CHAT_TOOL_COUNT``. An empty list is the same as none."""

    tool_choice: ChatToolChoice | None = None
    """Whether and which tool the model has to call. Only allowed with tools; ``none`` forwards no tool definitions."""

    parallel_tool_calls: StrictBool | None = None
    """Whether the model may call several tools in one reply. Only allowed with tools."""

    response_format: ChatResponseFormat | None = None
    """The shape the reply has to take. ``text`` is the same as none."""

    horde: ChatHordeOptions = Field(default_factory=ChatHordeOptions)
    """Request options OpenAI has no key for, validated by the model the front shares."""

    @field_validator("tools", mode="before")
    @classmethod
    def validate_tool_count(cls, tools: object) -> object:
        """Validate that at most the SDK's ``MAX_CHAT_TOOL_COUNT`` tools are sent.

        Checked before the tools are validated, so an oversized list costs no per-tool validation, and reported as a
        value outside the service contract rather than as a malformed one.

        Raises:
            PydanticCustomError: More tools are sent, with the type
                [`UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE`][horde.chat_contract.UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE].
        """
        if isinstance(tools, list) and len(tools) > MAX_CHAT_TOOL_COUNT:
            raise PydanticCustomError(
                UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE,
                "A chat request may define at most {max_tools} tools.",
                {"max_tools": MAX_CHAT_TOOL_COUNT},
            )
        return tools


class ChatSubmittedMessage(BaseModel):
    """Represents the assistant message a worker submits for a chat job."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["assistant"]
    """Always ``assistant``."""

    content: StrictStr | None = None
    """The reply text. An empty string is a completed reply with no visible text; null only with ``tool_calls``."""

    tool_calls: list[ChatToolCall] | None = Field(default=None, min_length=1, max_length=MAX_CHAT_TOOL_CALL_COUNT)
    """The tool calls the model made, as the backend minted them."""

    reasoning_content: StrictStr | None = None
    """The model's hidden reasoning, such as a ``<think>`` block the backend split off, never part of ``content``. It
    is stored and shown beside the reply and is not charged or verified as one."""

    @model_validator(mode="after")
    def validate_content_or_tool_calls(self) -> ChatSubmittedMessage:
        """Validate that the content is null only when the message carries tool calls.

        Raises:
            ValueError: The content is null and there are no tool calls.
        """
        if self.content is None and not self.tool_calls:
            raise ValueError("'content' may be null only when 'tool_calls' is set")
        return self


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
        message_index: Its position, used to report the offending key's path.

    Raises:
        horde.exceptions.BadRequest: If the message uses something unsupported.
    """
    if not isinstance(message, Mapping):
        return

    role = message.get("role")
    allowed_keys = ROLE_MESSAGE_KEYS.get(role, SUPPORTED_MESSAGE_KEYS) if isinstance(role, str) else MESSAGE_KEY_NAMES
    for message_key in message:
        if message_key not in allowed_keys:
            _raise_unsupported(f"messages[{message_index}].{message_key}")

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
    """Refuse every key, role and content part outside the supported subset, reporting the first one found.

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
        horde.exceptions.BadRequest: With ``UnsupportedChatParameter`` for an ``n`` other than 1, messages or a
            tool spec over the shared size limits, a tool history breaking OpenAI's rule, or a ``strict`` schema
            breaking OpenAI's structural rules; ``ChatRequestConflict`` for both ``model`` and ``models``, differing
            token limits, a ``tool_choice`` or ``parallel_tool_calls`` the tools do not allow, or, under
            ``horde.openai_guards``, a ``json_object`` request whose messages never mention JSON; the plain
            ``BadRequest`` code for neither ``model`` nor ``models`` or a model listed twice; or an image validation
            failure for too many, malformed or oversized image URLs.
    """
    try:
        check_model_choice(model=chat_request.model, models=chat_request.models)
    except ChatRequestRefusal as refusal:
        raise _refusal_error(refusal) from None

    if chat_request.n is not None and chat_request.n != SUPPORTED_CHAT_CHOICE_COUNT:
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
            f"The message text of a chat request, tool call names and arguments included, may be at most {MAX_CHAT_TEXT_BYTES} bytes.",
            rc=UNSUPPORTED_CHAT_PARAMETER_RC,
        )
    _validate_image_urls(_submitted_image_urls(chat_request))
    _reject_conflicting_tool_values(chat_request)
    try:
        check_strict_tool_schemas(chat_request.tools)
        check_strict_response_schema(chat_request.response_format)
        check_json_word(_message_texts(chat_request), chat_request.response_format, openai_guards=chat_request.horde.openai_guards)
    except ChatRequestRefusal as refusal:
        raise _refusal_error(refusal) from None


def _refusal_error(refusal: ChatRequestRefusal) -> e.BadRequest:
    """Return the 400 for a shared request rule's refusal, with the refusal's request path in the message."""
    return e.BadRequest(f"'{refusal.param}': {refusal.message}", rc=refusal.rc)


def _tool_spec_bytes(chat_request: ChatCompletionRequest) -> int:
    """Return the UTF-8 size of the compact JSON of ``tools``, ``tool_choice`` and ``response_format`` together.

    Args:
        chat_request: The validated request.

    Returns:
        The bytes of the three values as the client sent them, nulls left out.
    """
    tool_spec = {
        "tools": [tool.model_dump(mode="json", exclude_none=True) for tool in chat_request.tools or ()],
        "tool_choice": _dump_tool_choice(chat_request.tool_choice),
        "response_format": _dump_response_format(chat_request.response_format),
    }
    return len(_compact_json(tool_spec).encode())


def _reject_conflicting_tool_values(chat_request: ChatCompletionRequest) -> None:
    """Refuse a tool spec over its size bound, tool keys the tools do not allow, and a tool history OpenAI refuses.

    Args:
        chat_request: The validated request.

    Raises:
        horde.exceptions.BadRequest: With ``UnsupportedChatParameter`` for a tool spec over
            the SDK's ``MAX_CHAT_TOOL_SPEC_BYTES`` or a history
            [`check_tool_history`][horde.chat_contract.check_tool_history] refuses; ``ChatRequestConflict`` for a
            ``tool_choice`` (``none`` included) or ``parallel_tool_calls`` without tools, or a ``tool_choice``
            forcing a function the tools do not define.
    """
    if _tool_spec_bytes(chat_request) > MAX_CHAT_TOOL_SPEC_BYTES:
        raise e.BadRequest(
            f"'tools', 'tool_choice' and 'response_format' may take at most {MAX_CHAT_TOOL_SPEC_BYTES} bytes together.",
            rc=UNSUPPORTED_CHAT_PARAMETER_RC,
        )

    tool_choice = chat_request.tool_choice
    tool_names = {tool.function.name for tool in chat_request.tools or ()}
    try:
        check_tool_fields_need_tools(
            offers_tools=bool(tool_names),
            tool_choice_set=tool_choice is not None,
            parallel_tool_calls_set=chat_request.parallel_tool_calls is not None,
        )
        check_tool_history(chat_request.messages)
    except ChatRequestRefusal as refusal:
        raise _refusal_error(refusal) from None
    if isinstance(tool_choice, ChatNamedToolChoice) and tool_choice.function.name not in tool_names:
        raise e.BadRequest(
            f"'tool_choice' forces the function '{tool_choice.function.name}', which 'tools' does not define.",
            rc=CHAT_REQUEST_CONFLICT_RC,
        )


def _submitted_image_urls(chat_request: ChatCompletionRequest) -> list[str]:
    """Return the URL of every image part the client sent, in message order.

    Args:
        chat_request: The validated request.

    Returns:
        Each image part's ``url``.
    """
    image_urls: list[str] = []
    for message in chat_request.messages:
        if not isinstance(message.content, list):
            continue
        image_urls.extend(content_part.image_url.url for content_part in message.content if isinstance(content_part, ChatImagePart))
    return image_urls


def _message_texts(chat_request: ChatCompletionRequest) -> Iterator[str]:
    """Yield every string content and text part of the request's messages, in order."""
    for message in chat_request.messages:
        if isinstance(message.content, str):
            yield message.content
        elif message.content is not None:
            yield from (content_part.text for content_part in message.content if isinstance(content_part, ChatTextPart))


def _message_text_bytes(chat_request: ChatCompletionRequest) -> int:
    """Return the UTF-8 size of every message text and history tool call, measured as the front measures it.

    Args:
        chat_request: The validated request.

    Returns:
        The bytes of every string content and text part, and of every tool call's function name and arguments,
        together.
    """
    text_bytes = sum(len(message_text.encode()) for message_text in _message_texts(chat_request))
    return text_bytes + sum(chat_tool_call_bytes(message.tool_calls) for message in chat_request.messages)


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
        # A value outside the service contract, such as too many tools or a schema nested too deeply, keeps its code.
        if any(detail["type"] == UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE for detail in validation_error.errors()):
            raise e.BadRequest(
                f"The chat request is not valid. {_validation_reasons(validation_error)}.",
                rc=UNSUPPORTED_CHAT_PARAMETER_RC,
            ) from None
        raise e.BadRequest(f"The chat request is not valid. {_validation_reasons(validation_error)}.") from None

    _reject_conflicting_values(chat_request)
    _reject_stored_nuls(chat_request)
    return chat_request


def _reject_stored_nuls(chat_request: ChatCompletionRequest) -> None:
    """Refuse NUL in persisted request strings before PostgreSQL text or JSONB can reject the write.

    Metadata and user are deliberately ignored by the native route and are not persisted.
    """
    pending: list[Any] = [
        serialize_messages(chat_request),
        build_chat_extras(chat_request),
        chat_request.stop,
        chat_request.model,
        chat_request.models,
        chat_request.horde.model_dump(),
    ]
    while pending:
        value = pending.pop()
        if isinstance(value, str) and "\x00" in value:
            raise e.BadRequest("Request payload may not contain NUL (0x00) bytes.", rc="NulByteInPayload")
        if isinstance(value, Mapping):
            # Tool parameters and response schemas carry client keys into persisted JSONB, which refuses NUL in a
            # key as well as in a value.
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def serialize_messages(chat_request: ChatCompletionRequest) -> list[dict[str, Any]]:
    """Return the request's messages as the JSON array stored on the request and sent to the worker.

    A ``developer`` message is stored as ``system``, so a worker only meets the roles every chat template knows.
    ``tool`` messages and an assistant message's ``tool_calls`` are stored unchanged.

    Args:
        chat_request: The validated request.

    Returns:
        One dict per message, in the shape the client sent, without keys the client left unset. ``content`` is always
        present, null on an assistant message that only calls tools, as OpenAI sends it.
    """
    serialized_messages: list[dict[str, Any]] = []
    for message in chat_request.messages:
        serialized_message = {"content": None, **message.model_dump(mode="json", exclude_none=True)}
        if message.role == ChatRole.DEVELOPER:
            serialized_message["role"] = ChatRole.SYSTEM.value
        serialized_messages.append(serialized_message)

    return serialized_messages


def _compact_json(value: object) -> str:
    """Return a value as compact JSON, non-ASCII text kept as it is."""
    return json.dumps(value, separators=COMPACT_JSON_SEPARATORS, ensure_ascii=False)


def _dump_tool_choice(tool_choice: ChatToolChoice | None) -> str | dict[str, Any] | None:
    """Return a ``tool_choice`` as the JSON value the client sent."""
    if isinstance(tool_choice, ChatNamedToolChoice):
        return tool_choice.model_dump(mode="json")
    return None if tool_choice is None else tool_choice.value


def _dump_response_format(response_format: ChatResponseFormat | None) -> dict[str, Any] | None:
    """Return a ``response_format`` as the JSON value the client sent, with the schema under its ``schema`` key."""
    if response_format is None:
        return None
    return response_format.model_dump(mode="json", by_alias=True, exclude_none=True)


def build_chat_extras(chat_request: ChatCompletionRequest) -> dict[str, Any] | None:
    """Return the tool and response format keys the request applies, as stored.

    A value that means the default is left out: an empty ``tools``, a ``text`` response format, and a
    ``tool_choice`` or ``parallel_tool_calls`` without tools. ``tool_choice`` ``none`` beside tools is kept, since
    the request's guarantees, its dispatch narrowing
    ([`build_worker_chat_extras`][horde.classes.kobold.chat_request.build_worker_chat_extras]) and the operation
    fingerprint depend on it.

    Args:
        chat_request: The validated request.

    Returns:
        A dict holding only those of ``tools``, ``tool_choice``, ``parallel_tool_calls`` and ``response_format`` the
        request applies, each in the shape the client sent; None when it applies none.
    """
    chat_extras: dict[str, Any] = {}
    if chat_request.tools:
        chat_extras["tools"] = [tool.model_dump(mode="json", exclude_none=True) for tool in chat_request.tools]
        if chat_request.tool_choice is not None:
            chat_extras["tool_choice"] = _dump_tool_choice(chat_request.tool_choice)
        if chat_request.parallel_tool_calls is not None:
            chat_extras["parallel_tool_calls"] = chat_request.parallel_tool_calls
    response_format = chat_request.response_format
    if response_format is not None and response_format.type != ChatResponseFormatType.TEXT.value:
        chat_extras["response_format"] = _dump_response_format(response_format)
    return chat_extras or None


def build_stored_chat_extras(chat_request: ChatCompletionRequest) -> dict[str, Any] | None:
    """Return the extras a chat request is stored with: the applied keys and the stream hint.

    The stream hint, [`CHAT_STREAM_PROGRESS_KEY`][horde.classes.kobold.chat_request.CHAT_STREAM_PROGRESS_KEY], is
    stored only when the client sent ``stream: true``. It reaches the job payload through
    [`build_worker_chat_extras`][horde.classes.kobold.chat_request.build_worker_chat_extras] and decides at pop whether
    the worker receives a stream lease. It does not change the reply, so the operation fingerprint, which hashes
    [`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras], leaves it out.

    Args:
        chat_request: The validated request.

    Returns:
        The keys of [`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras] and the stream hint
        when set; None when there are none.
    """
    chat_extras = build_chat_extras(chat_request) or {}
    if chat_request.stream:
        chat_extras[CHAT_STREAM_PROGRESS_KEY] = True
    return chat_extras or None


def streams_progress(chat_extras: Mapping[str, Any] | None) -> bool:
    """Return whether a chat request's stored extras carry the stream hint.

    Args:
        chat_extras: The request's stored extras, from
            [`build_stored_chat_extras`][horde.classes.kobold.chat_request.build_stored_chat_extras].

    Returns:
        True when the client asked for a stream.
    """
    return bool((chat_extras or {}).get(CHAT_STREAM_PROGRESS_KEY))


def _is_image_part(content_part: Mapping[str, Any]) -> bool:
    """Return whether a content part is an image part."""
    return bool(content_part["type"] == IMAGE_URL_CONTENT_PART_TYPE)


def message_text(message: Mapping[str, Any]) -> str:
    """Return the text of one stored message, joining its parts when the content is an array.

    Each image part is rendered as [`CHAT_IMAGE_PLACEHOLDER`][horde.classes.kobold.chat_request.CHAT_IMAGE_PLACEHOLDER].

    Args:
        message: One entry of the stored ``messages`` array.

    Returns:
        The message's text; empty for an assistant message whose content is null.
    """
    content = message.get("content")
    if content is None:
        return ""
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
    return any(
        isinstance(content := message.get("content"), list) and any(_is_image_part(part) for part in content) for message in messages
    )


def list_image_urls(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the URL of every image part, in message order.

    An image's position in this list is its ordinal, which is part of the key of the object stored for it.

    Args:
        messages: A ``messages`` array.

    Returns:
        Each image part's ``url``.
    """
    image_urls: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
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
        content = message.get("content")
        if isinstance(content, list):
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


def select_chat_transport(
    declared_transports: Sequence[str] | None,
    *,
    carries_images: bool,
    messages_offloaded: bool,
) -> ChatImageTransport | None:
    """Return the transport a chat job's messages and images are sent to a worker with.

    ``messages_url`` is chosen whenever the request's messages were offloaded and the worker declared it. Otherwise a
    request with images uses ``url`` whenever the worker declared it, and ``base64`` only when that is the only image
    transport declared.

    Args:
        declared_transports: The ``image_transports`` the worker declared on its last pop.
        carries_images: Whether the request's messages hold an image part.
        messages_offloaded: Whether the request's messages were stored as one object at submit.

    Returns:
        The transport, or None when the messages are sent inline and do not carry an image, or the worker did
        not declare a transport for them.
    """
    declared = set(declared_transports or ())
    if messages_offloaded and ChatImageTransport.MESSAGES_URL.value in declared:
        return ChatImageTransport.MESSAGES_URL
    if not carries_images:
        return None
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


def build_chat_messages_key(request_id: str) -> str:
    """Return the object key a chat request's offloaded ``messages`` array is stored under.

    The key is the same whether the body is stored gzipped or plain; the object's ``Content-Encoding`` tells them
    apart, so deleting and signing the object needs only the request id. The key ends with
    [`CHAT_MESSAGES_KEY_SUFFIX`][horde.r2.CHAT_MESSAGES_KEY_SUFFIX], which
    [`is_chat_messages_key`][horde.r2.is_chat_messages_key] reads to sign and delete it in the messages bucket.

    Args:
        request_id: The chat request's id.

    Returns:
        The key in the ``R2_CHAT_MESSAGES_BUCKET`` bucket, which defaults to the source image bucket beside the
        request's images.
    """
    return f"{request_id}{CHAT_MESSAGES_KEY_SUFFIX}"


def list_stored_chat_object_keys(
    request_id: str,
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    messages_offloaded: bool,
) -> list[str]:
    """Return the key of every object a chat request stored at submit.

    Args:
        request_id: The chat request's id.
        messages: The request's stored messages, whose image parts hold references.
        messages_offloaded: Whether the request's messages were also stored as one object.

    Returns:
        The keys of the images the messages reference, in message order, then the offloaded messages key when there
        is one.
    """
    object_keys = list_chat_image_keys(messages or [])
    if messages_offloaded:
        object_keys.append(build_chat_messages_key(request_id))
    return object_keys


def inline_chat_images(messages: Sequence[Mapping[str, Any]], jpeg_images: Sequence[bytes]) -> list[dict[str, Any]]:
    """Return a copy of the messages with each image part's URL replaced by its stored JPEG as a data URL.

    Args:
        messages: A ``messages`` array whose image parts hold the submitted URLs.
        jpeg_images: The stored JPEG bytes of each image part, in message order.

    Returns:
        The messages as a ``base64`` worker receives them.

    Raises:
        ValueError: If the number of images differs from the number of image parts.
    """
    if len(jpeg_images) != len(list_image_urls(messages)):
        raise ValueError("Every image part needs exactly one stored image.")
    stored_images = iter(jpeg_images)
    return _replace_image_urls_in_order(
        messages,
        lambda _submitted_url: CHAT_IMAGE_DATA_URL_PREFIX + base64.b64encode(next(stored_images)).decode("ascii"),
    )


class ChatMessagesOffload(NamedTuple):
    """Represents the object a large chat request's ``messages`` array is stored as."""

    body: bytes
    """The UTF-8 JSON of the messages array, gzipped when ``gzipped`` is set."""

    gzipped: bool
    """Whether ``body`` is gzip compressed, which the object's ``Content-Encoding: gzip`` records."""


def build_chat_messages_offload(
    messages: Sequence[Mapping[str, Any]],
    *,
    threshold_bytes: int,
    compress: bool,
) -> ChatMessagesOffload | None:
    """Return the object a ``messages`` array is offloaded as, or None when it is small enough to send inline.

    The body is the plain JSON array. Compression applies to the whole body as a transport encoding, and the fields
    inside it stay plain. It is used only when ``compress`` is set and gzip makes the body smaller.

    Args:
        messages: The messages as a worker receives them, images inlined as data URLs.
        threshold_bytes: The largest compact JSON size, in bytes, that stays inline.
        compress: Whether to try gzip on the body.

    Returns:
        The body and whether it is gzipped, or None when the compact JSON is at most ``threshold_bytes``.
    """
    plain_body = _compact_json(list(messages)).encode("utf-8")
    if len(plain_body) <= threshold_bytes:
        return None
    if not compress:
        return ChatMessagesOffload(body=plain_body, gzipped=False)
    gzipped_body = gzip.compress(plain_body, compresslevel=CHAT_MESSAGES_GZIP_LEVEL, mtime=0)
    if len(gzipped_body) < len(plain_body):
        return ChatMessagesOffload(body=gzipped_body, gzipped=True)
    return ChatMessagesOffload(body=plain_body, gzipped=False)


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


def _flattened_message(message: Mapping[str, Any]) -> str:
    """Return one stored message as it appears in the flattened prompt: ``role: text``, then any tool calls as JSON."""
    flattened = f"{message['role']}: {message_text(message)}"
    tool_calls = message.get("tool_calls")
    if tool_calls:
        flattened = f"{flattened}{FLATTENED_MESSAGE_SEPARATOR}{_compact_json(tool_calls)}"
    return flattened


def flatten_messages(messages: Sequence[Mapping[str, Any]], chat_extras: Mapping[str, Any] | None = None) -> str:
    """Return the messages joined into the single string stored as the request's prompt.

    The string is an estimate only: the token estimate, kudos and moderation read it, and no worker
    ever receives it. A worker renders the tool definitions into its prompt too, so they follow the messages as
    compact JSON, and an assistant message's tool calls follow its text. A response format schema is left out: the
    reference worker compiles it into a grammar and never renders it into the prompt, so counting it would reserve
    context the worker does not use.

    Args:
        messages: The stored ``messages`` array.
        chat_extras: The request's stored extras, from
            [`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras].

    Returns:
        Each message as ``role: text``, a ``tool`` message as ``tool: <text>``, joined with newlines, followed by
        the ``tools`` JSON when the request carries tools.
    """
    flattened_parts = [_flattened_message(message) for message in messages]
    chat_extras = chat_extras or {}
    if "tools" in chat_extras:
        flattened_parts.append(_compact_json(chat_extras["tools"]))
    return FLATTENED_MESSAGE_SEPARATOR.join(flattened_parts)


TRUNCATION_KEPT_ROLES: frozenset[str] = frozenset(
    {
        ChatRole.SYSTEM.value,
        ChatRole.DEVELOPER.value,
    },
)
"""The roles ``truncate_oldest`` never drops, wherever their messages sit: they hold the instructions the rest of the
conversation is read under."""


class ChatHistoryTruncation(NamedTuple):
    """Represents the outcome of dropping a chat history's oldest turns to fit a context."""

    messages: list[dict[str, Any]]
    """The messages that are kept, in their original order."""

    prompt: str
    """The kept messages flattened by [`flatten_messages`][horde.classes.kobold.chat_request.flatten_messages]."""

    dropped_message_count: int
    """How many messages were dropped."""

    estimated_prompt_tokens_before: int
    """The token estimate of the full history."""

    estimated_prompt_tokens_after: int
    """The token estimate of the kept messages."""

    fits: bool
    """Whether the kept messages fit the context. False when every droppable turn was dropped and they still do
    not, in which case the request is refused."""


def _truncation_units(messages: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    """Return the droppable turns of a history, oldest first, each as the indexes of its messages.

    A turn is a ``user`` message with the ``assistant`` message right after it, or an ``assistant`` message carrying
    ``tool_calls`` with the ``tool`` messages that answer it; any other message is a turn of its own. A ``user``
    message whose reply carries ``tool_calls`` takes the answering ``tool`` messages too. Request validation
    (``check_tool_history``) already guarantees the answers follow the calling message at once, so taking the run of
    ``tool`` messages after it is the whole group, and a dropped turn never leaves a call or an answer without the
    other. Messages of the roles in ``TRUNCATION_KEPT_ROLES``, the latest ``user`` message and everything after it
    belong to no turn. A history without a ``user`` message keeps its last message, so a request keeps the message it
    ends with.

    Args:
        messages: The stored ``messages`` array, oldest first.

    Returns:
        The turns, each a list of message indexes in order.
    """
    user_indexes = [index for index, message in enumerate(messages) if message["role"] == ChatRole.USER.value]
    kept_tail_start = user_indexes[-1] if user_indexes else max(len(messages) - 1, 0)

    def takes_tool_answers(index: int) -> bool:
        return messages[index]["role"] == ChatRole.ASSISTANT.value and bool(messages[index].get("tool_calls"))

    units: list[list[int]] = []
    index = 0
    while index < kept_tail_start:
        role = messages[index]["role"]
        if role in TRUNCATION_KEPT_ROLES:
            index += 1
            continue
        unit = [index]
        index += 1
        if role == ChatRole.USER.value and index < kept_tail_start and messages[index]["role"] == ChatRole.ASSISTANT.value:
            unit.append(index)
            index += 1
        if takes_tool_answers(unit[-1]):
            while index < kept_tail_start and messages[index]["role"] == ChatRole.TOOL.value:
                unit.append(index)
                index += 1
        units.append(unit)
    return units


def truncate_oldest_messages(
    messages: Sequence[Mapping[str, Any]],
    chat_extras: Mapping[str, Any] | None,
    *,
    max_length: int,
    max_context_length: int,
) -> ChatHistoryTruncation:
    """Return the history with its oldest turns dropped, one turn at a time, until it fits the context.

    Each candidate is flattened as the request's prompt is, tools JSON included, and estimated again, so the fit is
    judged on the same estimate the request is sized and charged by. The turns are formed by ``_truncation_units``.

    Args:
        messages: The stored ``messages`` array, oldest first.
        chat_extras: The request's stored extras, whose ``tools`` count towards the estimate.
        max_length: How many tokens the request wants generated.
        max_context_length: The context the kept messages have to fit.

    Returns:
        The kept messages and the estimates before and after. ``fits`` is False when dropping every droppable turn
        does not make the history fit.
    """
    kept_messages = [dict(message) for message in messages]
    prompt = flatten_messages(kept_messages, chat_extras)
    estimated_prompt_tokens_before = estimate_prompt_tokens(prompt)
    estimated_prompt_tokens = estimated_prompt_tokens_before
    dropped_indexes: set[int] = set()
    units = iter(_truncation_units(messages))
    while not prompt_fits_context(
        estimated_prompt_tokens=estimated_prompt_tokens,
        max_length=max_length,
        max_context_length=max_context_length,
    ):
        unit = next(units, None)
        if unit is None:
            break
        dropped_indexes.update(unit)
        kept_messages = [dict(message) for index, message in enumerate(messages) if index not in dropped_indexes]
        prompt = flatten_messages(kept_messages, chat_extras)
        estimated_prompt_tokens = estimate_prompt_tokens(prompt)
    return ChatHistoryTruncation(
        messages=kept_messages,
        prompt=prompt,
        dropped_message_count=len(dropped_indexes),
        estimated_prompt_tokens_before=estimated_prompt_tokens_before,
        estimated_prompt_tokens_after=estimated_prompt_tokens,
        fits=prompt_fits_context(
            estimated_prompt_tokens=estimated_prompt_tokens,
            max_length=max_length,
            max_context_length=max_context_length,
        ),
    )


def _replace_nuls(value: Any) -> Any:
    """Return a JSON value with NUL replaced by U+FFFD in every string, the text submission's replacement policy."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {key: _replace_nuls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_nuls(item) for item in value]
    return value


def build_stored_reply(message: ChatSubmittedMessage) -> dict[str, Any]:
    """Return a worker's submitted message as stored on its generation and shown in the status.

    PostgreSQL text and JSONB both reject NUL, so every string has it replaced, as a text submission's generation
    does.

    Args:
        message: The validated submitted message.

    Returns:
        ``{role, content}``, with ``tool_calls`` only when the message carries them, ``content`` null only then, and
        ``reasoning_content`` only when the worker submitted it.
    """
    stored_reply = message.model_dump(mode="json")
    if message.tool_calls is None:
        del stored_reply["tool_calls"]
    if message.reasoning_content is None:
        del stored_reply["reasoning_content"]
    replaced_reply: dict[str, Any] = _replace_nuls(stored_reply)
    return replaced_reply


def chat_reply_characters(message: Mapping[str, Any]) -> int:
    """Return the characters a submitted chat reply is charged on.

    The text formula prices a reply by its characters. A tool call is output the model generated as well, so its
    function name and arguments count with the content.

    Args:
        message: The stored assistant message a worker submitted.

    Returns:
        The length of the content, plus the length of each tool call's function name and arguments.
    """
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or ()
    return len(content) + sum(len(tool_call["function"]["name"]) + len(tool_call["function"]["arguments"]) for tool_call in tool_calls)


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
        content = message.get("content")
        if isinstance(content, str):
            rewritten_message["content"] = rewrite_text(content) or ""
        elif isinstance(content, list):
            rewritten_message["content"] = [
                content_part if _is_image_part(content_part) else {**content_part, "text": rewrite_text(content_part["text"]) or ""}
                for content_part in content
            ]
        rewritten_messages.append(rewritten_message)

    return rewritten_messages


def build_model_names(chat_request: ChatCompletionRequest) -> list[str]:
    """Return the models the request accepts.

    Args:
        chat_request: The request, validated by
            [`parse_chat_request`][horde.classes.kobold.chat_request.parse_chat_request], which leaves exactly one of
            ``model`` and ``models`` set.

    Returns:
        ``models`` in the order the client listed them, or ``model`` alone.
    """
    if chat_request.models is not None:
        return list(chat_request.models)
    return [chat_request.model] if chat_request.model is not None else []


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
    if chat_request.reasoning_effort is not None:
        text_params["reasoning_effort"] = chat_request.reasoning_effort.value
    if chat_request.stop is not None:
        text_params["stop_sequence"] = [chat_request.stop] if isinstance(chat_request.stop, str) else list(chat_request.stop)

    return text_params


def build_operation_fingerprint(chat_request: ChatCompletionRequest) -> str:
    """Return the SHA-256 hex digest that tells a repeated submission of one operation key from a changed one.

    The hash covers the request as the horde applies it: the messages as stored (``developer`` rewritten to
    ``system``, unset keys left out), the text params the request maps onto, the model list sorted, and every ``horde``
    option with its default filled in, except ``operation_key``, ``dry_run`` and ``include_reasoning``, and under ``extras`` the tools and
    response format the request applies, only when it applies any. Two bodies that differ only in how
    they say the same thing, such as ``max_tokens`` against ``max_completion_tokens`` or an option sent at its
    default against one left out, share a fingerprint. Any listed model may serve the request, so ``models`` in another
    order is the same operation, and ``model: "a"`` hashes as ``models: ["a"]``. Keys that are accepted and not applied (``stream``,
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
        "models": sorted(build_model_names(chat_request)),
        "horde": chat_request.horde.model_dump(mode="json", exclude=set(OPERATION_FINGERPRINT_EXCLUDED_HORDE_KEYS)),
    }
    # Left out when empty, so the fingerprint of a request without tools or a response format covers the keys above only.
    chat_extras = build_chat_extras(chat_request)
    if chat_extras is not None:
        effective_request["extras"] = chat_extras
    canonical_json = json.dumps(effective_request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _tool_choice_mode(tool_choice: str | Mapping[str, Any] | None) -> PolicyToolChoiceMode:
    """Return the policy mode of a stored ``tool_choice``; an unset choice is OpenAI's ``auto``."""
    if tool_choice is None:
        return PolicyToolChoiceMode.AUTO
    if isinstance(tool_choice, Mapping):
        return PolicyToolChoiceMode.FUNCTION
    return PolicyToolChoiceMode(tool_choice)


def build_chat_request_traits(params: Mapping[str, Any], chat_extras: Mapping[str, Any] | None) -> ChatRequestTraits:
    """Return the traits that decide which guarantees and limits apply to a chat request.

    Args:
        params: The request's stored text params.
        chat_extras: The request's stored extras, from
            [`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras].

    Returns:
        The traits, with the temperature the job payload carries (the client's, or the one in
        the SDK's ``CHAT_SAMPLING_DEFAULTS``), the tool choice as
        stored (unset is ``auto``), ``parallel_tool_calls`` defaulting to true, and the response format's type and
        ``json_schema.strict`` flag.
    """
    chat_extras = chat_extras or {}
    response_format = chat_extras.get("response_format")
    response_format_type = None
    strict_schema = False
    if response_format is not None:
        response_format_type = PolicyResponseFormatType(response_format["type"])
        strict_schema = response_format_type is PolicyResponseFormatType.JSON_SCHEMA and bool(response_format["json_schema"].get("strict"))
    return ChatRequestTraits(
        offers_tools=bool(chat_extras.get("tools")),
        tool_choice_mode=_tool_choice_mode(chat_extras.get("tool_choice")),
        parallel_tool_calls=chat_extras.get("parallel_tool_calls", DEFAULT_PARALLEL_TOOL_CALLS),
        response_format_type=response_format_type,
        strict_schema=strict_schema,
        temperature=params.get("temperature", CHAT_SAMPLING_DEFAULTS["temperature"]),
    )


def chat_tools_temperature(traits: ChatRequestTraits) -> float | None:
    """Return the temperature a worker's ``tools_max_temperature`` limit is compared with.

    The SDK's ``violated_chat_limit`` defines the rule for workers and clients. The server stores this projection of
    the traits once per request and the pop query compares it with the same rule, so the queue scan never loads a
    candidate's extras.

    Args:
        traits: The request's traits, from
            [`build_chat_request_traits`][horde.classes.kobold.chat_request.build_chat_request_traits].

    Returns:
        The forwarded temperature when the request forwards tool definitions; None otherwise, when no tool
        temperature limit applies.
    """
    return traits.temperature if traits.forwards_tools else None


def required_chat_features(
    params: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    chat_extras: Mapping[str, Any] | None,
    conformance: ChatConformance,
) -> set[ChatFeature | ChatGuarantee]:
    """Return every capability and guarantee a worker must declare to serve a chat request.

    The only place a request's requirements are derived. The set is stored on the request when it is created, so
    eligibility compares a worker's declaration with it without loading the messages or extras of any candidate.
    Capabilities and guarantees share one namespace (ADR 0036), so both are stored in one list. A value at its
    documented default needs no capability, so a request that sends a zero penalty is served by any chat worker.

    Args:
        params: The request's stored text params.
        messages: The request's stored messages.
        chat_extras: The request's stored extras, from
            [`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras].
        conformance: The conformance mode the request selected.

    Returns:
        ``seed`` when a seed is set; ``reasoning_effort`` when a reasoning effort is set, ``none`` included, since a
        backend that ignores the key would reason anyway; each penalty whose value is not zero; ``vision`` when a
        message carries an image part; ``tools`` when the request offers tools under a ``tool_choice`` other than
        ``none``, or any message is a ``tool`` message or carries tool calls, since the worker's template has to render
        those turns;
        ``json_object`` or ``json_schema`` by the response format's type; ``tools_with_response_format`` when the
        request forwards tool definitions and sets a response format, since the backend then has to apply the format
        to the final answer and not to the tool calls; and the guarantees the SDK's
        ``required_chat_guarantees`` derives for the conformance mode, which under ``strict`` always include
        ``prompt_overflow_refused`` and under ``best_effort`` are none.
    """
    needed_features: set[ChatFeature | ChatGuarantee] = set()
    if params.get("seed") is not None:
        needed_features.add(ChatFeature.SEED)
    if params.get("reasoning_effort") is not None:
        needed_features.add(ChatFeature.REASONING_EFFORT)
    for penalty_feature in (ChatFeature.FREQUENCY_PENALTY, ChatFeature.PRESENCE_PENALTY):
        default_penalty = CHAT_SAMPLING_DEFAULTS[penalty_feature.value]
        if params.get(penalty_feature.value, default_penalty) != default_penalty:
            needed_features.add(penalty_feature)
    if has_image_parts(messages):
        needed_features.add(ChatFeature.VISION)
    traits = build_chat_request_traits(params, chat_extras)
    carries_tool_turns = any(message["role"] == ChatRole.TOOL or message.get("tool_calls") for message in messages)
    if traits.forwards_tools or carries_tool_turns:
        needed_features.add(ChatFeature.TOOLS)
    if traits.response_format_type is not None:
        needed_features.add(ChatFeature(traits.response_format_type.value))
        if traits.forwards_tools:
            needed_features.add(ChatFeature.TOOLS_WITH_RESPONSE_FORMAT)
    needed_features.update(required_chat_guarantees(traits, conformance))

    return needed_features


def build_worker_chat_extras(chat_extras: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the extras a chat job's payload carries, narrowed to what the tool choice lets the model call.

    The stored extras keep the client's values for the response and a replay; only the worker's copy is narrowed.
    Under ``tool_choice: none`` no tool key is forwarded, so a backend that ignores ``none`` has no definition to
    call. Under a ``tool_choice`` that specifies one function only that function is forwarded, with ``tool_choice: required``, so a
    backend that knows only the modes still has to call that function. ``auto`` and ``required`` are forwarded as
    stored, and so is the stream hint.

    Args:
        chat_extras: The request's stored extras, from
            [`build_stored_chat_extras`][horde.classes.kobold.chat_request.build_stored_chat_extras].

    Returns:
        The keys to add to the job payload; empty when the request applies none.
    """
    worker_extras = dict(chat_extras or {})
    tool_choice = worker_extras.get("tool_choice")
    if isinstance(tool_choice, Mapping):
        named_function = tool_choice["function"]["name"]
        worker_extras["tools"] = [tool for tool in worker_extras["tools"] if tool["function"]["name"] == named_function]
        worker_extras["tool_choice"] = ChatToolChoiceMode.REQUIRED.value
    elif _tool_choice_mode(tool_choice) is PolicyToolChoiceMode.NONE:
        for tool_key in ("tools", "tool_choice", "parallel_tool_calls"):
            worker_extras.pop(tool_key, None)
    return worker_extras


def _refuse_invalid_reply(message: str) -> NoReturn:
    """Refuse a chat submit no client could use.

    Raises:
        horde.exceptions.BadRequest: Always, with ``ChatSubmitInvalid``.
    """
    raise e.BadRequest(message, rc=CHAT_SUBMIT_INVALID_RC)


def _refuse_nonconformant_reply(message: str) -> NoReturn:
    """Refuse a chat submit a ``strict`` request's contract rules out.

    Raises:
        horde.exceptions.BadRequest: Always, with ``ChatSubmitNonconformant``.
    """
    raise e.BadRequest(message, rc=CHAT_SUBMIT_NONCONFORMANT_RC)


def _verify_tool_call_structure(tool_calls: Sequence[ChatToolCall], chat_extras: Mapping[str, Any]) -> None:
    """Refuse calls to functions the request did not offer, and calls sharing an id.

    A client dispatches a call by its function name and answers it by its id, so either fault leaves the reply
    unusable whatever the conformance mode. The client's stored tools are the reference, not the narrowed copy the
    worker received, so a ``best_effort`` reply calling another offered function passes.

    Raises:
        horde.exceptions.BadRequest: With ``ChatSubmitInvalid``.
    """
    offered_names = {tool["function"]["name"] for tool in chat_extras.get("tools") or ()}
    for tool_call in tool_calls:
        if not offered_names:
            _refuse_invalid_reply(f"'message.tool_calls' calls '{tool_call.function.name}', but the request offered no tools.")
        if tool_call.function.name not in offered_names:
            _refuse_invalid_reply(f"'message.tool_calls' calls '{tool_call.function.name}', which is not among the request's tools.")
    call_ids = [tool_call.id for tool_call in tool_calls]
    if len(set(call_ids)) != len(call_ids):
        _refuse_invalid_reply("'message.tool_calls' has two calls with the same 'id'.")


def _verify_tool_choice(tool_calls: Sequence[ChatToolCall], chat_extras: Mapping[str, Any]) -> None:
    """Refuse a reply whose calls the request's ``tool_choice`` rules out.

    Only the calls are checked. OpenAI finishes a forced call with ``stop``, so the finish reason proves nothing.

    Raises:
        horde.exceptions.BadRequest: With ``ChatSubmitNonconformant``.
    """
    tool_choice = chat_extras.get("tool_choice")
    tool_choice_mode = _tool_choice_mode(tool_choice)
    if tool_choice_mode is PolicyToolChoiceMode.NONE and tool_calls:
        _refuse_nonconformant_reply("The request's 'tool_choice' is 'none', so the reply may not carry 'tool_calls'.")
    if tool_choice_mode is PolicyToolChoiceMode.REQUIRED and not tool_calls:
        _refuse_nonconformant_reply("The request's 'tool_choice' is 'required', so the reply needs 'tool_calls'.")
    if isinstance(tool_choice, Mapping):
        named_function = tool_choice["function"]["name"]
        if not tool_calls or any(tool_call.function.name != named_function for tool_call in tool_calls):
            _refuse_nonconformant_reply(
                f"The request's 'tool_choice' forces '{named_function}', so the reply has to call it and no other function.",
            )


_NOT_JSON = object()
"""What [`_parse_reply_json`][horde.classes.kobold.chat_request._parse_reply_json] returns for content that is not
JSON, since None is the parsed ``null``."""


def _parse_reply_json(content: str) -> Any:
    """Return the reply content parsed as JSON, or [`_NOT_JSON`][horde.classes.kobold.chat_request._NOT_JSON].

    Nesting too deep for the parser counts as not JSON, so a reply cannot turn the check into a server error.
    """
    try:
        return json.loads(content, parse_constant=refuse_json_constant)
    except (ValueError, RecursionError):
        return _NOT_JSON


def _verify_strict_schema(reply_value: Any, json_schema: Mapping[str, Any]) -> None:
    """Refuse a parsed reply that does not match a ``strict`` ``json_schema`` response format.

    The validator is the one the schema's ``$schema`` declares, Draft 2020-12 otherwise. References resolve only
    inside the schema; ``jsonschema`` does not fetch a remote one, so validation makes no network call.

    Raises:
        horde.exceptions.BadRequest: With ``ChatSubmitNonconformant``, when the reply does not match, or when the
            schema cannot be compiled or its references resolved, since the reply then cannot be shown to match.
    """
    schema = json_schema["schema"]
    schema_name = json_schema["name"]
    validator_class = validator_for(schema, default=Draft202012Validator)
    try:
        validator_class.check_schema(schema)
        mismatch = best_match(validator_class(schema).iter_errors(reply_value))
    except (SchemaError, Unresolvable):
        _refuse_nonconformant_reply(
            f"The request's JSON Schema '{schema_name}' cannot be compiled, so the reply cannot be verified against it."
        )
    except RecursionError:
        _refuse_nonconformant_reply(f"The reply content is nested too deeply to verify against the request's JSON Schema '{schema_name}'.")
    if mismatch is not None:
        # The path and keyword only: the error's own message quotes the instance, which may be the whole reply.
        _refuse_nonconformant_reply(
            f"The reply content does not match the request's JSON Schema '{schema_name}': "
            f"'{mismatch.validator}' fails at {mismatch.json_path}.",
        )


def _verify_response_format(message: ChatSubmittedMessage, chat_extras: Mapping[str, Any]) -> None:
    """Refuse reply content the request's ``response_format`` rules out.

    A ``strict`` ``json_schema`` reply has to match the schema. ``json_object``, and ``json_schema`` without
    ``strict``, need only a JSON object, because OpenAI documents a non-strict schema as best effort. A reply with
    tool calls and no content carries no output to check.

    Raises:
        horde.exceptions.BadRequest: With ``ChatSubmitNonconformant``.
    """
    response_format = chat_extras.get("response_format")
    if response_format is None or (message.tool_calls and not message.content):
        return
    format_type = response_format["type"]
    reply_value = _parse_reply_json(message.content or "")
    if reply_value is _NOT_JSON:
        _refuse_nonconformant_reply(f"The request's 'response_format' is '{format_type}', so the reply content has to be JSON.")
    json_schema = response_format.get("json_schema")
    if json_schema is not None and json_schema.get("strict"):
        _verify_strict_schema(reply_value, json_schema)
    elif not isinstance(reply_value, dict):
        _refuse_nonconformant_reply(f"The request's 'response_format' is '{format_type}', so the reply content has to be a JSON object.")


def chat_reply_finish_reason(finish_reason: str, message: ChatSubmittedMessage, chat_extras: Mapping[str, Any] | None) -> str:
    """Return the finish reason the client receives for a verified reply.

    OpenAI finishes a reply to a ``tool_choice`` that specifies a function with ``stop`` and the call in ``tool_calls``, and finishes
    ``auto`` and ``required`` replies with ``tool_calls``. A worker receives such a choice as ``required`` (see
    [`build_worker_chat_extras`][horde.classes.kobold.chat_request.build_worker_chat_extras]), so its backend reports
    ``tool_calls``. The reason is mapped back to OpenAI's under every conformance mode, since it is the shape of the
    reply rather than how faithfully the backend followed the request.

    Args:
        finish_reason: The finish reason the worker submitted.
        message: The reply, as [`verify_chat_reply`][horde.classes.kobold.chat_request.verify_chat_reply] returned it.
        chat_extras: The request's stored extras.

    Returns:
        ``stop`` for a ``tool_calls`` finish of a reply with calls to a request whose ``tool_choice`` specifies a function; the
        submitted reason otherwise, so ``length`` and ``content_filter`` are kept.
    """
    if finish_reason == ChatFinishReason.TOOL_CALLS and message.tool_calls and isinstance((chat_extras or {}).get("tool_choice"), Mapping):
        return str(ChatFinishReason.STOP)
    return finish_reason


def verify_chat_reply(
    message: ChatSubmittedMessage,
    chat_extras: Mapping[str, Any] | None,
    conformance: ChatConformance,
) -> ChatSubmittedMessage:
    """Return a worker's reply as the client receives it, after verifying what the payload can prove (ADR 0036).

    Routing sends a request only to workers declaring what it needs, and dispatch narrows the tools a worker sees;
    this check trusts neither. A refused submit costs the worker a faulted job and the request one of its faulted
    generations, so only replies OpenAI would never return are refused.

    Under both modes a call to a function the request did not offer, or two calls sharing an id, is refused. Under
    ``strict`` the reply also has to honour the request's ``tool_choice`` and ``response_format``, and more than one
    call under ``parallel_tool_calls: false`` is reduced to the first, since an OpenAI client would only ever see one.
    The reduction comes first, so a call it drops is not checked. Under ``best_effort`` the reply otherwise passes
    through as submitted.

    ``reasoning_content`` is not the reply, so no conformance rule reads it. It is bounded by the SDK's
    ``MAX_CHAT_TEXT_BYTES``, the most message text a request may carry: the charge is computed on the reply alone, so
    no kudos cost bounds what a worker stores as reasoning, and a client sending it back as history is held to that
    cap anyway.

    Args:
        message: The validated submitted message.
        chat_extras: The request's stored extras, from
            [`build_chat_extras`][horde.classes.kobold.chat_request.build_chat_extras]; the client's values, not the
            narrowed copy the worker received.
        conformance: The conformance mode the request selected.

    Returns:
        The message, holding only its first tool call when the reduction applied, and unchanged otherwise.

    Raises:
        horde.exceptions.BadRequest: With ``ChatSubmitInvalid`` for a call to a function the request did not offer, a
            repeated call id or reasoning over the text cap; with ``ChatSubmitNonconformant`` for a reply a ``strict``
            request's ``tool_choice`` or ``response_format`` rules out.
    """
    if message.reasoning_content is not None and len(message.reasoning_content.encode("utf-8")) > MAX_CHAT_TEXT_BYTES:
        _refuse_invalid_reply(f"'message.reasoning_content' may be at most {MAX_CHAT_TEXT_BYTES} bytes.")
    chat_extras = chat_extras or {}
    is_strict = conformance is ChatConformance.STRICT
    parallel_tool_calls = chat_extras.get("parallel_tool_calls", DEFAULT_PARALLEL_TOOL_CALLS)
    if is_strict and not parallel_tool_calls and message.tool_calls and len(message.tool_calls) > 1:
        message = message.model_copy(update={"tool_calls": message.tool_calls[:1]})
    tool_calls = message.tool_calls or []
    _verify_tool_call_structure(tool_calls, chat_extras)
    if is_strict:
        _verify_tool_choice(tool_calls, chat_extras)
        _verify_response_format(message, chat_extras)
    return message


def build_chat_job_payload(params: Mapping[str, Any], *, max_length: int, max_context_length: int) -> dict[str, Any]:
    """Return the payload a chat job is sent with, apart from its messages.

    Every sampling field in
    the SDK's ``CHAT_SAMPLING_DEFAULTS`` is present, with its
    default where the client left it unset; ``stop_sequence``, ``seed`` and ``reasoning_effort`` only when the client
    sent them. There is
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
    for optional_field in ("stop_sequence", "seed", "reasoning_effort"):
        if params.get(optional_field) is not None:
            chat_payload[optional_field] = params[optional_field]
    chat_payload[MAX_LENGTH_PARAMETER] = max_length
    chat_payload[MAX_CONTEXT_LENGTH_PARAMETER] = max_context_length
    chat_payload["n"] = SUPPORTED_CHAT_CHOICE_COUNT
    return chat_payload
