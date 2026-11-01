# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ``horde`` extension object of a chat request, shared by the native chat route and the OpenAI-compatible front.

Both the native ``POST /v2/generate/chat/async`` route and the ``horde.oai`` front validate a request's ``horde``
object against [`ChatHordeOptions`][horde.chat_contract.ChatHordeOptions], so the two cannot disagree on which keys
exist or what values they take. The module imports only pydantic, the standard library and the SDK's chat policy
vocabulary, so the front can import it without the Flask application, its database or its configuration. The
request limits a client can check before sending (the tool count, the tool spec and text sizes, the schema nesting)
are the SDK's ``chat_limits`` constants, so the server, workers and clients apply one set of values.

The module also holds the shapes both sides validate a request's tools, tool calls and response format against
([`ChatTool`][horde.chat_contract.ChatTool], [`ChatToolCall`][horde.chat_contract.ChatToolCall],
[`ChatResponseFormat`][horde.chat_contract.ChatResponseFormat] and their parts), so a worker receives exactly what a
client sent in a shape one definition bounds.

``dry_run`` and ``operation_key`` belong to native callers: the front refuses both in a public request body (see
[`INTERNAL_HORDE_OPTION_KEYS`][horde.chat_contract.INTERNAL_HORDE_OPTION_KEYS]) and supplies the operation key itself.

The request rules both sides apply after validation live here as well, so the native route and the front refuse the
same requests with the same wording: [`check_model_choice`][horde.chat_contract.check_model_choice],
[`check_tool_fields_need_tools`][horde.chat_contract.check_tool_fields_need_tools],
[`check_tool_history`][horde.chat_contract.check_tool_history],
[`check_strict_response_schema`][horde.chat_contract.check_strict_response_schema],
[`check_strict_tool_schemas`][horde.chat_contract.check_strict_tool_schemas] and
[`check_json_word`][horde.chat_contract.check_json_word]. Each raises
[`ChatRequestRefusal`][horde.chat_contract.ChatRequestRefusal] with OpenAI's message and ``param`` where OpenAI has one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, NoReturn, Protocol
from uuid import UUID

from horde_sdk.generation_parameters.text.chat_limits import (
    MAX_CHAT_SCHEMA_NESTING,
    chat_schema_exceeds_nesting,
)
from horde_sdk.generation_parameters.text.chat_policy import DEFAULT_CHAT_CONFORMANCE, ChatConformance, ChatServiceGapKind
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

__all__ = [
    "BAD_REQUEST_RC",
    "CHAT_IMAGE_PROMPT_TOKENS",
    "CHAT_KUDOS_CAP_EXCEEDED_RC",
    "CHAT_REQUEST_CONFLICT_ERROR_TYPE",
    "CHAT_REQUEST_CONFLICT_RC",
    "INTERNAL_HORDE_OPTION_KEYS",
    "MAX_CHAT_BODY_BYTES",
    "MAX_CHAT_IMAGE_BYTES",
    "MAX_CHAT_IMAGE_COUNT",
    "MAX_CHAT_IMAGE_TOTAL_BYTES",
    "MAX_CHAT_IMAGE_TOTAL_BYTES_UNTRUSTED",
    "MAX_CHAT_MESSAGE_COUNT",
    "MAX_CHAT_MODEL_COUNT",
    "MAX_CHAT_TOOL_CALL_COUNT",
    "MAX_CHAT_TOOL_CALL_ID_LENGTH",
    "MAX_CHAT_TOOL_NAME_LENGTH",
    "JSON_WORD",
    "KUDOS_CAP_EXCESS_PATTERN",
    "NO_ELIGIBLE_WORKER_RC",
    "OBJECT_SCHEMA_TYPE",
    "SCHEMA_KEYWORDS_HOLDING_A_SCHEMA",
    "SCHEMA_KEYWORDS_HOLDING_SCHEMA_ARRAYS",
    "SCHEMA_KEYWORDS_HOLDING_SCHEMA_MAPS",
    "TOOL_NAME_PATTERN",
    "TOOL_ROLE",
    "UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE",
    "UNSUPPORTED_CHAT_PARAMETER_RC",
    "ChatAvailabilityMode",
    "ChatContextAdjustmentReason",
    "ChatContextFit",
    "ChatFunctionCall",
    "ChatFunctionDefinition",
    "ChatHistoryMessage",
    "ChatHordeOptions",
    "ChatImpossibleReason",
    "ChatJsonObjectResponseFormat",
    "ChatJsonSchema",
    "ChatJsonSchemaResponseFormat",
    "ChatNamedFunction",
    "ChatNamedToolChoice",
    "ChatRequestRefusal",
    "ChatResponseFormat",
    "ChatResponseFormatType",
    "ChatTextResponseFormat",
    "ChatTool",
    "ChatToolCall",
    "ChatToolChoice",
    "ChatToolChoiceMode",
    "KudosCapExcess",
    "chat_tool_call_bytes",
    "check_json_word",
    "check_model_choice",
    "check_strict_response_schema",
    "check_strict_tool_schemas",
    "check_tool_fields_need_tools",
    "check_tool_history",
]

MAX_CHAT_MESSAGE_COUNT: int = 1024
"""The most messages one chat request may carry. A conversation longer than this outgrows any served context long
before it reaches the cap, so the cap only refuses bodies built to make validation and flattening expensive."""

MAX_CHAT_MODEL_COUNT: int = 5
"""The most entries ``models`` may list. The native text route sets no bound on its model list; this matches the
most models a text style may list. Each entry costs a model check before the request is queued, and a list this
long already covers every model a client would accept in place of another."""

MAX_CHAT_IMAGE_BYTES: int = 5 * 1024 * 1024
"""The largest decoded size of one image in a chat request. Matches the cap on a source image fetched from a URL."""

MAX_CHAT_IMAGE_COUNT: int = 20
"""The most image parts one chat request may carry, for every user. The total size caps bound the bytes a request's
images cost; this bounds the per-image work, a fetch or decode, a re-encode and a stored object each, and the
[`CHAT_IMAGE_PROMPT_TOKENS`][horde.chat_contract.CHAT_IMAGE_PROMPT_TOKENS] each image adds to the prompt estimate."""

MAX_CHAT_IMAGE_TOTAL_BYTES: int = 25 * 1024 * 1024
"""The most decoded or fetched image bytes one chat request may carry in all, for trusted users and patrons. Five
images at [`MAX_CHAT_IMAGE_BYTES`][horde.chat_contract.MAX_CHAT_IMAGE_BYTES], the extra source image cap on image
requests, which bounds the same storage and transfer cost. Every request is held to it, since a request is parsed
before its user is known.

Both totals hold the images as sent and again as the JPEGs they are stored as: storage, transfer and the worker's
download cost the stored bytes, and a compact PNG can re-encode to a far larger JPEG."""

MAX_CHAT_IMAGE_TOTAL_BYTES_UNTRUSTED: int = 5 * 1024 * 1024
"""The most decoded or fetched image bytes one chat request may carry in all, for a user who is neither trusted nor a
patron. One image at [`MAX_CHAT_IMAGE_BYTES`][horde.chat_contract.MAX_CHAT_IMAGE_BYTES], which keeps the storage and
transfer cost of the extra source image rule on image requests while letting the bytes be split over several
smaller images."""

CHAT_IMAGE_PROMPT_TOKENS: int = 512
"""The tokens each image part adds to a chat request's prompt estimate, on top of the placeholder it is flattened to.
koboldcpp's worker adds this many per image in its own context overflow estimate, so counting them keeps a request
with many images from being estimated to fit a context the worker then refuses. The context is fitted, and so the
request charged, on this estimate."""

MAX_CHAT_BODY_BYTES: int = 36 * 1024 * 1024
"""The most bytes a chat request body may have, and the bound on what is read before the body is parsed.

A request at every cap carries [`MAX_CHAT_IMAGE_TOTAL_BYTES`][horde.chat_contract.MAX_CHAT_IMAGE_TOTAL_BYTES] (25 MiB)
of images as data URLs, which base64 grows by 4/3 to about 33.3 MiB, plus the SDK's ``MAX_CHAT_TEXT_BYTES`` (1 MiB)
of text: about 34.3 MiB. 36 MiB leaves about 1.7 MiB for JSON structure, escaping and the data URL prefixes on top
of that."""

CHAT_REQUEST_CONFLICT_ERROR_TYPE: str = "chat_request_conflict"
"""The pydantic error type raised for two keys whose values contradict each other.

Callers map it to the ``ChatRequestConflict`` return code rather than to a generic validation failure, so a client can
tell a contradiction from a malformed value.
"""

UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE: str = "unsupported_chat_parameter"
"""The pydantic error type raised for a value outside the service contract, such as a schema nested deeper than
the SDK's ``MAX_CHAT_SCHEMA_NESTING``; callers map it to ``UnsupportedChatParameter``."""

UNSUPPORTED_CHAT_PARAMETER_RC: str = "UnsupportedChatParameter"
"""Return code for a key, value or content part this horde does not serve, so a client can branch on it."""

CHAT_REQUEST_CONFLICT_RC: str = "ChatRequestConflict"
"""Return code for two values of a request that contradict each other, such as differing token limits."""

BAD_REQUEST_RC: str = "BadRequest"
"""Return code for a request that is malformed, such as a body that is not JSON, a value of the wrong type or a
request without a model; the Horde's generic code for a 400, which the native route and the front both return."""

CHAT_KUDOS_CAP_EXCEEDED_RC: str = "ChatKudosCapExceeded"
"""Return code for a request whose kudos estimate exceeds the ``horde.max_kudos`` its client set."""

NO_ELIGIBLE_WORKER_RC: str = "NoEligibleWorker"
"""Return code for a request the kudos rules would refuse while no online worker can serve it; the error body holds
``impossible_reason`` when a declaration gap is the cause."""

KUDOS_CAP_EXCESS_PATTERN: re.Pattern[str] = re.compile(
    r"kudos estimate at current pricing is (?P<kudos_estimate>\d+), above its horde\.max_kudos of (?P<max_kudos>\d+)",
)
"""The wording of [`KudosCapExcess.to_message`][horde.chat_contract.KudosCapExcess.to_message] that carries both
values. The native error body holds only the message and the return code, so the OpenAI front reads the values back
from the message with this pattern."""

INTERNAL_HORDE_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "dry_run",
        "operation_key",
    },
)
"""The ``horde`` keys only native callers may send; the public front refuses each, with its key in the message."""


class ChatContextFit(StrEnum):
    """The ways a chat request's context length is fitted to its messages."""

    GROW = "grow"
    """Grow the context up to what an online chat worker serves."""

    REJECT = "reject"
    """Refuse a request whose messages do not fit the requested context."""

    TRUNCATE_OLDEST = "truncate_oldest"
    """Grow as ``grow`` does, then drop whole turns from the oldest forward until the messages fit. Every system and
    developer message is kept, as are the latest user message and everything after it; a request that still does not
    fit is refused as under ``reject``."""


class ChatContextAdjustmentReason(StrEnum):
    """The reason in a ``context_adjustments`` entry for a change the horde made to a chat request's size."""

    GROW = ChatContextFit.GROW.value
    """The context was grown to fit the messages. ``from`` and ``to`` are context lengths."""

    TRUNCATE_OLDEST = ChatContextFit.TRUNCATE_OLDEST.value
    """The oldest turns were dropped so the messages fit. ``from`` and ``to`` are the estimated token counts of the
    submitted and the kept messages."""

    WORKER_MAX_LENGTH = "worker_max_length"
    """The completion length was lowered to the largest ``chat_max_length`` declared by the online chat workers
    serving the requested models. ``from`` and ``to`` are completion lengths. ``max_tokens`` is an upper bound in the
    OpenAI API, so a request above the largest declared length is served with that length and is not refused."""


class ChatAvailabilityMode(StrEnum):
    """How a request is treated when the online pool cannot serve it within the client's budget."""

    WAIT = "wait"
    """Queue and wait for the budget; refuse only when nothing is eligible for the grace period."""

    FAIL_FAST = "fail_fast"
    """Refuse at the first status read when no eligible worker thread is online and nothing is processing, or when
    the queue's estimated wait exceeds the remaining budget, with a Retry-After."""


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

    context_fit: ChatContextFit = ChatContextFit.TRUNCATE_OLDEST
    """What to do when the messages do not fit the context. The default is ``truncate_oldest``: a client that keeps
    a long history is served with its oldest turns dropped rather than refused, and the request records the drop in
    its context adjustments. ``grow`` and ``reject`` are explicit choices."""

    conformance: ChatConformance = DEFAULT_CHAT_CONFORMANCE
    """How closely the serving worker has to follow the OpenAI chat contract (ADR 0036). ``best_effort`` drops the
    guarantee requirements and keeps capabilities and limits."""

    openai_guards: StrictBool = False
    """Turn on the request refusals OpenAI applies and the Horde relaxes on purpose. It covers one refusal: a
    ``json_object`` response format without the word "json" in any message (see
    [`check_json_word`][horde.chat_contract.check_json_word]). It acts when the request is admitted and is not stored."""

    availability_mode: ChatAvailabilityMode = ChatAvailabilityMode.WAIT
    """How a request is treated when the online pool cannot serve it within the client's budget. ``fail_fast``
    refuses at once with a Retry-After instead of waiting for workers to appear. It changes the wait, not the work,
    so it is not stored and the operation fingerprint leaves it out."""

    max_kudos: StrictInt | None = Field(default=None, ge=1)
    """The most kudos the request may cost. A request whose kudos estimate at creation exceeds it is refused before it
    is queued, with the estimate in the refusal. The estimate assumes the full ``max_length`` is generated while the
    charge is the output estimate, so the check is conservative. It acts only at creation, so it is not stored and the
    operation fingerprint leaves it out."""

    include_reasoning: StrictBool = False
    """Return the model's reasoning as ``reasoning_content`` beside the reply. OpenAI's chat API does not return
    reasoning text, so the OpenAI front hides it unless asked for. It changes what the front delivers and leaves the
    work unchanged, so it is not stored and the operation fingerprint leaves it out; the native status shows the
    reasoning a worker submitted regardless."""

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


class KudosCapExcess(BaseModel):
    """Represents a chat request refused because its kudos estimate exceeds the ``horde.max_kudos`` its client set.

    The estimate is the request's kudos quote at the pricing in force at creation, computed from ``max_length``, so it
    is a ceiling on what the request is charged.
    """

    model_config = ConfigDict(frozen=True)

    kudos_estimate: int
    """The request's kudos estimate, rounded as the native route returns it."""

    max_kudos: int
    """The cap the client set."""

    def to_message(self) -> str:
        """Return the refusal's message, which states both values in the wording
        [`KUDOS_CAP_EXCESS_PATTERN`][horde.chat_contract.KUDOS_CAP_EXCESS_PATTERN] reads."""
        return (
            f"The request's kudos estimate at current pricing is {self.kudos_estimate}, above its horde.max_kudos of "
            f"{self.max_kudos}. The estimate assumes the full max_tokens is generated."
        )

    @classmethod
    def read_from_message(cls, message: str) -> KudosCapExcess | None:
        """Return the values a refusal's message states, or None when the message does not state them.

        Args:
            message: The message of a ``ChatKudosCapExceeded`` refusal.
        """
        values_match = KUDOS_CAP_EXCESS_PATTERN.search(message)
        if values_match is None:
            return None
        return cls(
            kudos_estimate=int(values_match.group("kudos_estimate")),
            max_kudos=int(values_match.group("max_kudos")),
        )


MAX_CHAT_TOOL_CALL_COUNT: int = 128
"""The most tool calls one assistant message may carry, in the request history and in a worker's submission;
mirrors the SDK's ``MAX_CHAT_TOOL_COUNT``."""

MAX_CHAT_TOOL_NAME_LENGTH: int = 64
"""The longest function name, OpenAI's own bound."""

MAX_CHAT_TOOL_CALL_ID_LENGTH: int = 256
"""The longest tool call id. Clients echo the id a model minted back in ``tool`` messages, so the bound only has to
leave room for any backend's format."""

TOOL_NAME_PATTERN: str = r"^[A-Za-z0-9_-]+$"
"""The characters a function name may use, OpenAI's own rule, so a name is safe to render into any chat template."""


def _validate_schema_nesting(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate that a JSON Schema nests at most the SDK's ``MAX_CHAT_SCHEMA_NESTING`` levels below its root, by the
    SDK's ``chat_schema_exceeds_nesting``.

    Raises:
        PydanticCustomError: The schema nests deeper, with the type
            [`UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE`][horde.chat_contract.UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE].
    """
    if chat_schema_exceeds_nesting(schema):
        raise PydanticCustomError(
            UNSUPPORTED_CHAT_PARAMETER_ERROR_TYPE,
            "A JSON Schema may nest at most {max_nesting} levels below its root.",
            {"max_nesting": MAX_CHAT_SCHEMA_NESTING},
        )
    return schema


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

    @field_validator("parameters")
    @classmethod
    def validate_parameters_nesting(cls, parameters: dict[str, Any] | None) -> dict[str, Any] | None:
        """Validate that the schema nests within the SDK's ``MAX_CHAT_SCHEMA_NESTING``.

        Raises:
            PydanticCustomError: The schema nests deeper.
        """
        return _validate_schema_nesting(parameters)


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
"""A ``tool_choice`` value: a mode value or one forced function."""


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
    name; dump with ``by_alias=True`` to get the client's key back. Validation reads only the ``schema`` key, so
    ``schema_`` in a request is an unknown key and refused like any other.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StrictStr = Field(min_length=1, max_length=MAX_CHAT_TOOL_NAME_LENGTH, pattern=TOOL_NAME_PATTERN)
    """What the client calls the schema."""

    description: StrictStr | None = None
    """What the output is, for the model."""

    schema_: dict[str, Any] = Field(alias="schema")
    """The JSON Schema the output has to match; forwarded to the worker unchanged. Required: a worker compiles it
    into a grammar, and without one the format would mean nothing."""

    strict: StrictBool | None = None
    """Whether the output has to match the schema exactly; forwarded to the worker, which decides what it honours.
    ``true`` also subjects the schema to OpenAI's structural rules
    ([`check_strict_response_schema`][horde.chat_contract.check_strict_response_schema])."""

    @field_validator("schema_")
    @classmethod
    def validate_schema_nesting(cls, schema: dict[str, Any]) -> dict[str, Any]:
        """Validate that the schema nests within the SDK's ``MAX_CHAT_SCHEMA_NESTING``.

        Raises:
            PydanticCustomError: The schema nests deeper.
        """
        _validate_schema_nesting(schema)
        return schema


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


class ChatImpossibleReason(BaseModel):
    """Represents why no worker can take a chat request, when waiting does not help while the online workers stay
    the same.

    The native chat status reports it and the front refuses the request on it without waiting. The fields match the
    SDK's ``ChatServiceGap``, which a client computes locally; this model stays separate because the server
    validates it from cached and HTTP payloads.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: ChatServiceGapKind = ChatServiceGapKind.DECLARATION_GAP
    """The cause."""

    missing_declarations: tuple[str, ...] = ()
    """The capabilities and guarantees the request needs that a refused worker does not declare, with
    ``image_transports`` for a worker that declares ``vision`` without a way to receive images."""

    exceeded_limits: tuple[str, ...] = ()
    """The declared limits the request exceeds on a refused worker, such as ``tools_max_temperature``."""


class ChatRequestRefusal(Exception):
    """A request one of the shared request rules refuses, with what both routes report for it.

    The native route raises it as a 400 with ``rc``; the front returns it as an OpenAI error with ``param``.
    """

    def __init__(self, message: str, *, param: str, rc: str) -> None:
        """Initialise the refusal.

        Args:
            message: The explanation, in OpenAI's wording where OpenAI refuses the same request.
            param: The request path at fault, such as ``messages[2].role``.
            rc: The return code, [`UNSUPPORTED_CHAT_PARAMETER_RC`][horde.chat_contract.UNSUPPORTED_CHAT_PARAMETER_RC],
                [`CHAT_REQUEST_CONFLICT_RC`][horde.chat_contract.CHAT_REQUEST_CONFLICT_RC] or
                [`BAD_REQUEST_RC`][horde.chat_contract.BAD_REQUEST_RC].
        """
        super().__init__(message)
        self.message: str = message
        """The explanation returned to the client."""

        self.param: str = param
        """The request path at fault."""

        self.rc: str = rc
        """The return code."""


class NonJsonConstantError(ValueError):
    """``NaN``, ``Infinity`` or ``-Infinity`` in text parsed as JSON.

    Python's parser accepts the three constants although JSON does not define them, so without
    [`refuse_json_constant`][horde.chat_contract.refuse_json_constant] a non-finite number would reach tool and
    response schemas and numeric parameters.
    """

    def __init__(self, constant: str) -> None:
        """Initialise the error.

        Args:
            constant: The constant as the text spells it.
        """
        super().__init__(f"{constant} is not valid JSON")
        self.constant: str = constant
        """The constant as the text spells it."""


def refuse_json_constant(constant: str) -> NoReturn:
    """Refuse ``constant``; passed as ``parse_constant`` to ``json.loads``, it limits the parse to JSON.

    Raises:
        NonJsonConstantError: Always.
    """
    raise NonJsonConstantError(constant)


class ChatHistoryMessage(Protocol):
    """The parts of a request message the tool history rule reads; both routes' message models provide them."""

    @property
    def role(self) -> str:
        """Who wrote the message."""

    @property
    def tool_calls(self) -> Sequence[ChatToolCall] | None:
        """The tool calls of an assistant message."""

    @property
    def tool_call_id(self) -> str | None:
        """The id of the call a ``tool`` message answers."""


TOOL_ROLE: str = "tool"
"""The role of a message answering a tool call."""


def chat_tool_call_bytes(tool_calls: Sequence[ChatToolCall] | None) -> int:
    """Return the UTF-8 size of the function names and arguments of a message's tool calls.

    Counted into the SDK's ``MAX_CHAT_TEXT_BYTES`` with the message texts.
    """
    return sum(len(tool_call.function.name.encode()) + len(tool_call.function.arguments.encode()) for tool_call in tool_calls or ())


def check_tool_fields_need_tools(*, offers_tools: bool, tool_choice_set: bool, parallel_tool_calls_set: bool) -> None:
    """Refuse ``tool_choice`` or ``parallel_tool_calls`` on a request without tools, as OpenAI does for any value,
    ``none`` included.

    Args:
        offers_tools: Whether the request carries a non-empty ``tools``.
        tool_choice_set: Whether the request sets ``tool_choice`` to a value other than null.
        parallel_tool_calls_set: Whether the request sets ``parallel_tool_calls`` to a value other than null.

    Raises:
        ChatRequestRefusal: With ``ChatRequestConflict`` and the key as ``param``.
    """
    if offers_tools:
        return
    for request_key, is_set in (("tool_choice", tool_choice_set), ("parallel_tool_calls", parallel_tool_calls_set)):
        if is_set:
            raise ChatRequestRefusal(
                f"Invalid value for '{request_key}': '{request_key}' is only allowed when 'tools' are specified.",
                param=request_key,
                rc=CHAT_REQUEST_CONFLICT_RC,
            )


def check_model_choice(*, model: str | None, models: Sequence[str] | None) -> None:
    """Refuse a request that sets both or neither of ``model`` and ``models``, or lists a model twice.

    ``models`` lists the models a client accepts in place of one ``model``; the request is served by whichever of
    them an online worker runs. Listing a model twice would make two spellings of one list, so it is refused.

    Args:
        model: The request's ``model``, or None when it is not set.
        models: The request's ``models``, or None when it is not set.

    Raises:
        ChatRequestRefusal: With ``ChatRequestConflict`` and ``param`` ``models`` when both are set;
            ``BadRequest`` and ``param`` ``model`` when neither is; ``BadRequest`` and the repeated entry's path,
            such as ``models[2]``, when an entry repeats an earlier one.
    """
    if model is not None and models is not None:
        raise ChatRequestRefusal(
            "'model' and 'models' cannot be combined; send one of them.",
            param="models",
            rc=CHAT_REQUEST_CONFLICT_RC,
        )
    if model is None and models is None:
        raise ChatRequestRefusal(
            "A chat request needs 'model' or 'models'.",
            param="model",
            rc=BAD_REQUEST_RC,
        )
    seen_models: set[str] = set()
    for model_index, model_name in enumerate(models or ()):
        if model_name in seen_models:
            raise ChatRequestRefusal(
                f"'models' lists the model '{model_name}' more than once.",
                param=f"models[{model_index}]",
                rc=BAD_REQUEST_RC,
            )
        seen_models.add(model_name)


def _unanswered_calls_refusal(unanswered_ids: Sequence[str], param: str) -> ChatRequestRefusal:
    """Create the refusal for tool calls no ``tool`` message answers before the message at ``param`` or the end."""
    return ChatRequestRefusal(
        "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. "
        f"The following tool_call_ids did not have response messages: {', '.join(unanswered_ids)}",
        param=param,
        rc=UNSUPPORTED_CHAT_PARAMETER_RC,
    )


def check_tool_history(messages: Sequence[ChatHistoryMessage]) -> None:
    """Refuse a history whose tool turns break OpenAI's rule.

    An assistant message with ``tool_calls`` has to be followed at once by ``tool`` messages answering each of its
    call ids exactly once, in any order among themselves. A ``tool`` message answers a call of that assistant message
    only, and the ids within one assistant message are unique. An id may recur in a later round, since each round is
    answered on its own.

    Args:
        messages: The request's messages, oldest first.

    Raises:
        ChatRequestRefusal: With ``UnsupportedChatParameter``: at ``messages[n].role`` for a ``tool`` message that
            follows no open tool calls or answers an id a second time, and for a message arriving before every open
            call was answered; at ``messages[n].tool_call_id`` for an id the preceding assistant message did not
            issue; at ``messages[n].tool_calls`` for an id repeated within one assistant message; and at ``messages``
            when the history ends with calls left unanswered.
    """
    open_call_ids: list[str] = []
    answered_ids: set[str] = set()
    for message_index, message in enumerate(messages):
        if message.role == TOOL_ROLE:
            if not open_call_ids or message.tool_call_id in answered_ids:
                raise ChatRequestRefusal(
                    "Invalid parameter: messages with role 'tool' must be a response to a preceding message with 'tool_calls'.",
                    param=f"messages[{message_index}].role",
                    rc=UNSUPPORTED_CHAT_PARAMETER_RC,
                )
            if message.tool_call_id not in open_call_ids:
                raise ChatRequestRefusal(
                    f"Invalid parameter: 'tool_call_id' of '{message.tool_call_id}' not found in 'tool_calls' of previous message.",
                    param=f"messages[{message_index}].tool_call_id",
                    rc=UNSUPPORTED_CHAT_PARAMETER_RC,
                )
            answered_ids.add(message.tool_call_id)
            continue
        unanswered_ids = [call_id for call_id in open_call_ids if call_id not in answered_ids]
        if unanswered_ids:
            raise _unanswered_calls_refusal(unanswered_ids, f"messages[{message_index}].role")
        open_call_ids = [tool_call.id for tool_call in message.tool_calls or ()]
        answered_ids = set()
        if len(set(open_call_ids)) != len(open_call_ids):
            repeated_id = next(call_id for call_id in open_call_ids if open_call_ids.count(call_id) > 1)
            raise ChatRequestRefusal(
                f"Invalid parameter: the tool call id '{repeated_id}' appears more than once in 'tool_calls'.",
                param=f"messages[{message_index}].tool_calls",
                rc=UNSUPPORTED_CHAT_PARAMETER_RC,
            )
    unanswered_ids = [call_id for call_id in open_call_ids if call_id not in answered_ids]
    if unanswered_ids:
        raise _unanswered_calls_refusal(unanswered_ids, "messages")


SCHEMA_KEYWORDS_HOLDING_A_SCHEMA: frozenset[str] = frozenset(
    {
        "items",
        "additionalProperties",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
    },
)
"""JSON Schema keywords whose value is one subschema; ``items`` may also hold an array of them, as in draft 7."""

SCHEMA_KEYWORDS_HOLDING_SCHEMA_ARRAYS: frozenset[str] = frozenset(
    {
        "anyOf",
        "allOf",
        "oneOf",
        "prefixItems",
    },
)
"""JSON Schema keywords whose value is an array of subschemas."""

SCHEMA_KEYWORDS_HOLDING_SCHEMA_MAPS: frozenset[str] = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
    },
)
"""JSON Schema keywords whose value maps names to subschemas."""

OBJECT_SCHEMA_TYPE: str = "object"
"""The JSON Schema ``type`` of an object, the schemas OpenAI's strict structural rules apply to."""


def _is_object_schema(schema: dict[str, Any]) -> bool:
    """Return whether a schema describes an object: its ``type`` is or lists ``object``, or it has ``properties``."""
    schema_type = schema.get("type")
    lists_object = isinstance(schema_type, list) and OBJECT_SCHEMA_TYPE in schema_type
    return schema_type == OBJECT_SCHEMA_TYPE or lists_object or "properties" in schema


def _object_schema_violation(schema: dict[str, Any], context: tuple[str | int, ...]) -> str | None:
    """Return OpenAI's description of the strict rule an object schema breaks at ``context``, or None."""
    if schema.get("additionalProperties") is not False:
        return f"In context={context!r}, 'additionalProperties' is required to be supplied and to be false."
    properties = schema.get("properties")
    property_names = list(properties) if isinstance(properties, dict) else []
    required = schema.get("required")
    if not property_names:
        return None
    required_names = required if isinstance(required, list) else []
    missing_names = [property_name for property_name in property_names if property_name not in required_names]
    if isinstance(required, list) and not missing_names:
        return None
    # OpenAI lists the first missing property only.
    missing_suffix = f" Missing '{missing_names[0]}'." if missing_names else ""
    return (
        f"In context={context!r}, 'required' is required to be supplied and to be an array including every key in "
        f"properties.{missing_suffix}"
    )


def _subschemas(schema: dict[str, Any], context: tuple[str | int, ...]) -> list[tuple[object, tuple[str | int, ...]]]:
    """Return every direct subschema of ``schema`` with its context, the path of keys and indexes leading to it."""
    subschemas: list[tuple[object, tuple[str | int, ...]]] = []
    for keyword, value in schema.items():
        if keyword in SCHEMA_KEYWORDS_HOLDING_SCHEMA_MAPS and isinstance(value, dict):
            subschemas.extend((subschema, (*context, keyword, name)) for name, subschema in value.items())
        elif keyword in SCHEMA_KEYWORDS_HOLDING_SCHEMA_ARRAYS and isinstance(value, list):
            subschemas.extend((subschema, (*context, keyword, index)) for index, subschema in enumerate(value))
        elif keyword in SCHEMA_KEYWORDS_HOLDING_A_SCHEMA and isinstance(value, list):
            subschemas.extend((subschema, (*context, keyword, index)) for index, subschema in enumerate(value))
        elif keyword in SCHEMA_KEYWORDS_HOLDING_A_SCHEMA:
            subschemas.append((value, (*context, keyword)))
    return subschemas


def _strict_schema_violation(schema: object, context: tuple[str | int, ...]) -> str | None:
    """Return OpenAI's description of the first strict structural rule ``schema`` or a subschema breaks, or None.

    The context is written as OpenAI writes it, a Python tuple of the keys leading to the subschema.
    """
    if not isinstance(schema, dict):
        return None
    if _is_object_schema(schema):
        violation = _object_schema_violation(schema, context)
        if violation is not None:
            return violation
    for subschema, subschema_context in _subschemas(schema, context):
        violation = _strict_schema_violation(subschema, subschema_context)
        if violation is not None:
            return violation
    return None


def _check_strict_schema(schema: object, *, subject: str, param: str) -> None:
    """Refuse a ``strict`` schema that breaks OpenAI's structural rules.

    Under ``strict: true`` every object schema, at the root, under ``properties``, in array ``items``, under ``$defs``
    and in every other subschema, sets ``additionalProperties: false`` and lists each of its properties in
    ``required``. Other keywords are not checked here; OpenAI accepts and applies ``minimum`` and the like. The walk
    recurses once per nesting level, which validation already bounded by the SDK's ``MAX_CHAT_SCHEMA_NESTING``.

    Args:
        schema: The schema to check.
        subject: What OpenAI's message calls the schema's owner, such as ``response_format 'answer'``.
        param: The request path of the schema, reported as the refusal's ``param``.

    Raises:
        ChatRequestRefusal: With ``UnsupportedChatParameter``.
    """
    violation = _strict_schema_violation(schema, ())
    if violation is not None:
        raise ChatRequestRefusal(
            f"Invalid schema for {subject}: {violation}",
            param=param,
            rc=UNSUPPORTED_CHAT_PARAMETER_RC,
        )


def check_strict_response_schema(response_format: ChatResponseFormat | None) -> None:
    """Refuse a ``strict`` ``json_schema`` response format whose schema breaks OpenAI's structural rules.

    The rules are those [`check_strict_tool_schemas`][horde.chat_contract.check_strict_tool_schemas] applies to a
    ``strict`` tool definition. A schema without ``strict: true`` is not checked structurally.

    Args:
        response_format: The request's validated response format.

    Raises:
        ChatRequestRefusal: With ``UnsupportedChatParameter`` and ``param`` ``response_format``.
    """
    if not isinstance(response_format, ChatJsonSchemaResponseFormat) or response_format.json_schema.strict is not True:
        return
    _check_strict_schema(
        response_format.json_schema.schema_,
        subject=f"response_format '{response_format.json_schema.name}'",
        param="response_format",
    )


def check_strict_tool_schemas(tools: Sequence[ChatTool] | None) -> None:
    """Refuse a tool definition with ``strict: true`` whose ``parameters`` schema breaks OpenAI's structural rules.

    The rules are those [`check_strict_response_schema`][horde.chat_contract.check_strict_response_schema] applies
    to a ``strict`` response format. A definition without ``strict: true`` is not checked structurally, as OpenAI
    does not check one.

    Args:
        tools: The request's validated tools.

    Raises:
        ChatRequestRefusal: With ``UnsupportedChatParameter`` and ``param`` ``tools[i].function.parameters`` for the
            first offending tool.
    """
    for tool_index, tool in enumerate(tools or ()):
        if tool.function.strict is not True:
            continue
        _check_strict_schema(
            tool.function.parameters,
            subject=f"function '{tool.function.name}'",
            param=f"tools[{tool_index}].function.parameters",
        )


JSON_WORD: str = "json"
"""The word OpenAI requires in the messages of a ``json_object`` request, matched without regard to case."""


def check_json_word(message_texts: Iterable[str], response_format: ChatResponseFormat | None, *, openai_guards: bool) -> None:
    """Refuse a ``json_object`` request whose messages never mention JSON, when the request asks for OpenAI's guards.

    OpenAI refuses such a request because a model constrained to JSON without being asked for it in the prompt may
    generate whitespace until its token limit. The Horde serves it by default; ``horde.openai_guards`` restores
    OpenAI's refusal for a client that relies on it.

    Args:
        message_texts: Every message text and text part of the request.
        response_format: The request's validated response format.
        openai_guards: The request's ``horde.openai_guards``.

    Raises:
        ChatRequestRefusal: With ``ChatRequestConflict`` and ``param`` ``messages``.
    """
    if not openai_guards or not isinstance(response_format, ChatJsonObjectResponseFormat):
        return
    if any(JSON_WORD in message_text.lower() for message_text in message_texts):
        return
    raise ChatRequestRefusal(
        "'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'.",
        param="messages",
        rc=CHAT_REQUEST_CONFLICT_RC,
    )
