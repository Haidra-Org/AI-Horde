# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ``horde`` extension object of a chat request, shared by the native chat route and the OpenAI-compatible front.

Both the native ``POST /v2/generate/chat/async`` route and the ``horde.oai`` front validate a request's ``horde``
object against [`ChatHordeOptions`][horde.chat_contract.ChatHordeOptions], so the two cannot disagree on which keys
exist or what values they take. The module imports only pydantic, the standard library and the SDK's chat policy
vocabulary, so the front can import it without the Flask application, its database or its configuration.

The module also holds the shapes both sides validate a request's tools, tool calls and response format against
([`ChatTool`][horde.chat_contract.ChatTool], [`ChatToolCall`][horde.chat_contract.ChatToolCall],
[`ChatResponseFormat`][horde.chat_contract.ChatResponseFormat] and their parts), so a worker receives exactly what a
client sent in a shape one definition bounds.

``dry_run`` and ``operation_key`` belong to native callers: the front refuses both in a public request body (see
[`INTERNAL_HORDE_OPTION_KEYS`][horde.chat_contract.INTERNAL_HORDE_OPTION_KEYS]) and supplies the operation key itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from horde_sdk.generation_parameters.text.chat_policy import DEFAULT_CHAT_CONFORMANCE, ChatConformance
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

__all__ = [
    "CHAT_REQUEST_CONFLICT_ERROR_TYPE",
    "INTERNAL_HORDE_OPTION_KEYS",
    "MAX_CHAT_BODY_BYTES",
    "MAX_CHAT_IMAGE_BYTES",
    "MAX_CHAT_IMAGE_COUNT",
    "MAX_CHAT_IMAGE_COUNT_UNTRUSTED",
    "MAX_CHAT_MESSAGE_COUNT",
    "MAX_CHAT_TEXT_BYTES",
    "MAX_CHAT_TOOL_CALL_COUNT",
    "MAX_CHAT_TOOL_CALL_ID_LENGTH",
    "MAX_CHAT_TOOL_COUNT",
    "MAX_CHAT_TOOL_NAME_LENGTH",
    "MAX_CHAT_TOOL_SPEC_BYTES",
    "TOOL_NAME_PATTERN",
    "ChatContextFit",
    "ChatFunctionCall",
    "ChatFunctionDefinition",
    "ChatHordeOptions",
    "ChatImpossibleKind",
    "ChatImpossibleReason",
    "ChatJsonObjectResponseFormat",
    "ChatJsonSchema",
    "ChatJsonSchemaResponseFormat",
    "ChatNamedFunction",
    "ChatNamedToolChoice",
    "ChatResponseFormat",
    "ChatResponseFormatType",
    "ChatTextResponseFormat",
    "ChatTool",
    "ChatToolCall",
    "ChatToolChoice",
    "ChatToolChoiceMode",
]

MAX_CHAT_MESSAGE_COUNT: int = 1024
"""The most messages one chat request may carry. A conversation longer than this outgrows any served context long
before it reaches the cap, so the cap only refuses bodies built to make validation and flattening expensive."""

MAX_CHAT_TEXT_BYTES: int = 1024 * 1024
"""The most UTF-8 bytes the message texts of one chat request may hold together. 1 MiB is several times the text of
the largest context a worker serves, so no servable conversation reaches it, while the flattened estimate,
moderation and the stored messages stay bounded."""

MAX_CHAT_IMAGE_BYTES: int = 5 * 1024 * 1024
"""The largest decoded size of one image in a chat request. Matches the cap on a source image fetched from a URL."""

MAX_CHAT_IMAGE_COUNT: int = 5
"""The most image parts one chat request may carry. Matches the extra source image cap on image requests, which
bounds the same storage and transfer cost."""

MAX_CHAT_IMAGE_COUNT_UNTRUSTED: int = 1
"""The most image parts a user who is neither trusted nor a patron may send. Matches the extra source image rule on
image requests."""

MAX_CHAT_BODY_BYTES: int = 36 * 1024 * 1024
"""The most bytes a chat request body may have, and the bound on what is read before the body is parsed.

A request at every cap carries [`MAX_CHAT_IMAGE_COUNT`][horde.chat_contract.MAX_CHAT_IMAGE_COUNT] data URLs of
[`MAX_CHAT_IMAGE_BYTES`][horde.chat_contract.MAX_CHAT_IMAGE_BYTES] each, which base64 grows by 4/3 to about 33.3 MiB,
plus [`MAX_CHAT_TEXT_BYTES`][horde.chat_contract.MAX_CHAT_TEXT_BYTES] of text: about 34.3 MiB. 36 MiB leaves room
for JSON structure and escaping on top of that."""

CHAT_REQUEST_CONFLICT_ERROR_TYPE: str = "chat_request_conflict"
"""The pydantic error type raised for two keys whose values contradict each other.

Callers map it to the ``ChatRequestConflict`` return code rather than to a generic validation failure, so a client can
tell a contradiction from a malformed value.
"""

INTERNAL_HORDE_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "dry_run",
        "operation_key",
    },
)
"""The ``horde`` keys only native callers may send; the public front refuses them by name."""


class ChatContextFit(StrEnum):
    """The ways a chat request's context length is fitted to its messages."""

    GROW = "grow"
    """Grow the context up to what an online chat worker serves."""

    REJECT = "reject"
    """Refuse a request whose messages do not fit the requested context."""


class ChatHordeOptions(BaseModel):
    """Represents the ``horde`` extension object of a chat request: options OpenAI has no key for.

    Every field defaults to the behaviour of a request that omits it, so a caller forwarding only the keys a client set
    does not change the request's meaning.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    only: list[str] | None = Field(default=None, min_length=1)
    """Worker ids the request is limited to; cannot be combined with ``ignore``."""

    ignore: list[str] | None = Field(default=None, min_length=1)
    """Worker ids that never serve the request; cannot be combined with ``only``."""

    trusted_workers: StrictBool = False
    """Limit the request to trusted workers."""

    validated_backends: StrictBool = False
    """Limit the request to workers running a validated inference backend."""

    slow_workers: StrictBool = True
    """Allow workers below the speed threshold to serve the request."""

    max_context_length: StrictInt | None = Field(default=None, ge=1)
    """An explicit context length in tokens, in place of the one fitted to the messages."""

    context_fit: ChatContextFit = ChatContextFit.GROW
    """What to do when the messages do not fit the context."""

    conformance: ChatConformance = DEFAULT_CHAT_CONFORMANCE
    """How closely the serving worker has to follow the OpenAI chat contract (ADR 0036). ``best_effort`` drops the
    guarantee requirements and keeps capabilities and limits."""

    dry_run: StrictBool = False
    """Return the kudos cost without queueing; native callers only."""

    operation_key: UUID | None = None
    """Identifies one submission so a repeated submit returns the existing request; native callers only."""

    @field_validator("ignore")
    @classmethod
    def validate_ignore_excludes_only(cls, ignore: list[str] | None, info: ValidationInfo) -> list[str] | None:
        """Validate that ``ignore`` is not combined with ``only``.

        Raises:
            PydanticCustomError: Both lists are set, with the type
                [`CHAT_REQUEST_CONFLICT_ERROR_TYPE`][horde.chat_contract.CHAT_REQUEST_CONFLICT_ERROR_TYPE].
        """
        if ignore is not None and info.data.get("only") is not None:
            raise PydanticCustomError(
                CHAT_REQUEST_CONFLICT_ERROR_TYPE,
                "'only' and 'ignore' cannot both be set.",
            )
        return ignore


MAX_CHAT_TOOL_COUNT: int = 128
"""The most tools one request may define, OpenAI's own bound. Each definition is rendered into the worker's prompt,
so the count also bounds how much prompt a request can add outside its messages."""

MAX_CHAT_TOOL_CALL_COUNT: int = 128
"""The most tool calls one assistant message may carry, in the request history and in a worker's submission;
mirrors [`MAX_CHAT_TOOL_COUNT`][horde.chat_contract.MAX_CHAT_TOOL_COUNT]."""

MAX_CHAT_TOOL_NAME_LENGTH: int = 64
"""The longest function name, OpenAI's own bound."""

MAX_CHAT_TOOL_CALL_ID_LENGTH: int = 256
"""The longest tool call id. Clients echo the id a model minted back in ``tool`` messages, so the bound only has to
leave room for any backend's format."""

MAX_CHAT_TOOL_SPEC_BYTES: int = 256 * 1024
"""The most bytes the compact JSON of ``tools``, ``tool_choice`` and ``response_format`` together may take. The
worker renders these into the prompt or compiles them into a grammar, so an unbounded definition is an unbounded
amount of worker time outside the token budget."""

TOOL_NAME_PATTERN: str = r"^[A-Za-z0-9_-]+$"
"""The characters a function name may use, OpenAI's own rule, so a name is safe to render into any chat template."""


class ChatToolChoiceMode(StrEnum):
    """The ``tool_choice`` values that pick a mode rather than one function."""

    NONE = "none"
    """The model never calls a tool; the same as sending no tools."""

    AUTO = "auto"
    """The model decides whether to call a tool."""

    REQUIRED = "required"
    """The model has to call at least one tool."""


class ChatFunctionDefinition(BaseModel):
    """Represents one callable function a client offers the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StrictStr = Field(min_length=1, max_length=MAX_CHAT_TOOL_NAME_LENGTH, pattern=TOOL_NAME_PATTERN)
    """What the model calls the function by."""

    description: StrictStr | None = None
    """What the function does, for the model."""

    parameters: dict[str, Any] | None = None
    """The JSON Schema of the arguments; forwarded to the worker unchanged."""

    strict: StrictBool | None = None
    """Whether the arguments have to match the schema exactly; forwarded to the worker, which decides what it honours."""


class ChatTool(BaseModel):
    """Represents one entry of a request's ``tools``. Only function tools exist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function"]
    """Always ``function``."""

    function: ChatFunctionDefinition
    """The function offered."""


class ChatNamedFunction(BaseModel):
    """Represents the function a ``tool_choice`` object points at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StrictStr = Field(min_length=1, max_length=MAX_CHAT_TOOL_NAME_LENGTH, pattern=TOOL_NAME_PATTERN)
    """The name of one of the request's tools."""


class ChatNamedToolChoice(BaseModel):
    """Represents a ``tool_choice`` that forces one function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function"]
    """Always ``function``."""

    function: ChatNamedFunction
    """The function the model has to call."""


ChatToolChoice = ChatToolChoiceMode | ChatNamedToolChoice
"""A ``tool_choice`` value: a mode name or one forced function."""


class ChatResponseFormatType(StrEnum):
    """The ``response_format`` types."""

    TEXT = "text"
    """Free text; the same as sending no response format."""

    JSON_OBJECT = "json_object"
    """Any JSON object."""

    JSON_SCHEMA = "json_schema"
    """A JSON value matching a client schema."""


class ChatTextResponseFormat(BaseModel):
    """Represents ``{"type": "text"}``, the default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text"]
    """Always ``text``."""


class ChatJsonObjectResponseFormat(BaseModel):
    """Represents ``{"type": "json_object"}``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["json_object"]
    """Always ``json_object``."""


class ChatJsonSchema(BaseModel):
    """Represents the ``json_schema`` object of a structured output request.

    The schema itself is read from the ``schema`` key and held in ``schema_``, because ``schema`` is a pydantic method
    name; dump with ``by_alias=True`` to get the client's key back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: StrictStr = Field(min_length=1, max_length=MAX_CHAT_TOOL_NAME_LENGTH, pattern=TOOL_NAME_PATTERN)
    """What the client calls the schema."""

    description: StrictStr | None = None
    """What the output is, for the model."""

    schema_: dict[str, Any] = Field(alias="schema")
    """The JSON Schema the output has to match; forwarded to the worker unchanged. Required: a worker compiles it
    into a grammar, and without one the format would mean nothing."""

    strict: StrictBool | None = None
    """Whether the output has to match the schema exactly; forwarded to the worker, which decides what it honours."""


class ChatJsonSchemaResponseFormat(BaseModel):
    """Represents ``{"type": "json_schema", "json_schema": {...}}``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["json_schema"]
    """Always ``json_schema``."""

    json_schema: ChatJsonSchema
    """The schema the output has to match."""


ChatResponseFormat = Annotated[
    ChatTextResponseFormat | ChatJsonObjectResponseFormat | ChatJsonSchemaResponseFormat,
    Field(discriminator="type"),
]
"""A ``response_format`` value, told apart by its ``type``."""


class ChatFunctionCall(BaseModel):
    """Represents the function part of a tool call: which function, with which arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StrictStr = Field(min_length=1, max_length=MAX_CHAT_TOOL_NAME_LENGTH, pattern=TOOL_NAME_PATTERN)
    """The function called."""

    arguments: StrictStr
    """The arguments as the model wrote them, a JSON document in a string; never parsed or rewritten by the horde."""


class ChatToolCall(BaseModel):
    """Represents one tool call, in an assistant message of the history or in a worker's submission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: StrictStr = Field(min_length=1, max_length=MAX_CHAT_TOOL_CALL_ID_LENGTH)
    """The id a ``tool`` message answers with; minted by the backend and never rewritten by the horde."""

    type: Literal["function"]
    """Always ``function``."""

    function: ChatFunctionCall
    """The function called."""


class ChatImpossibleKind(StrEnum):
    """Identify why no worker can take a chat request, as the chat status's ``impossible_reason`` reports it."""

    DECLARATION_GAP = "declaration_gap"
    """Online chat workers serve the request's model, but each lacks a capability or guarantee the request needs, or
    applies a declared limit the request exceeds."""


class ChatImpossibleReason(BaseModel):
    """Represents why no worker can take a chat request, when waiting does not help while the online workers stay
    the same.

    The native chat status reports it and the front refuses the request on it without waiting.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: ChatImpossibleKind = ChatImpossibleKind.DECLARATION_GAP
    """The cause."""

    missing_declarations: tuple[str, ...] = ()
    """The capabilities and guarantees the request needs that a refused worker does not declare, with
    ``image_transports`` for a worker that declares ``vision`` without a way to receive images."""

    exceeded_limits: tuple[str, ...] = ()
    """The declared limits the request exceeds on a refused worker, such as ``tools_max_temperature``."""
