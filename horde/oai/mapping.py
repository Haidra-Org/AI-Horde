# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Map a chat request to the native body, a finished Horde chat status to the OpenAI completion, a chat model list
entry to the OpenAI model entry, and an error to the OpenAI error envelope.

Usage counts come from the worker's report when there is one. A worker that does not report counts gets an estimate
of one token per [`CHARACTERS_PER_ESTIMATED_TOKEN`][horde.oai.mapping.CHARACTERS_PER_ESTIMATED_TOKEN] characters, and
``x_horde.estimated_usage`` says so. A generation whose reply holds neither visible text nor tool calls gets
[`EMPTY_RETURN_NOTICE`][horde.oai.mapping.EMPTY_RETURN_NOTICE] in ``x_horde.notice``.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from http import HTTPStatus
from typing import cast

from horde_sdk.generation_parameters.text.chat_policy import ChatFeature, ChatGuarantee

from horde.chat_contract import ChatTextResponseFormat, ChatTool, ChatToolCall
from horde.oai.horde_client import HordeActiveModel, HordeChatGeneration, HordeChatStatus
from horde.oai.schema import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseMessage,
    ChatCompletionUsage,
    ChatMessage,
    CompletionTokensDetails,
    ErrorMetadata,
    FinishReason,
    HordeModelDetails,
    HordeResponseDetails,
    ModelArchitecture,
    ModelCard,
    ModelTopProvider,
    OpenAIError,
    OpenAIErrorEnvelope,
    OpenAIErrorType,
    TextContentPart,
)

__all__ = [
    "CHARACTERS_PER_ESTIMATED_TOKEN",
    "COMPLETION_ID_PREFIX",
    "EMPTY_RETURN_NOTICE",
    "CompletionWithoutGenerationError",
    "build_completion",
    "build_completion_id",
    "build_system_fingerprint",
    "build_error",
    "build_error_envelope",
    "build_model_card",
    "build_native_chat_body",
    "estimate_tokens",
    "flatten_messages",
    "generation_content",
    "generation_reasoning",
    "generation_tool_calls",
    "relays_deltas",
]

CHARACTERS_PER_ESTIMATED_TOKEN: int = 3
"""Characters counted as one token when a worker does not report counts.

A rough ratio for English text; the estimate only fills the usage object, which ``x_horde.estimated_usage`` marks.
"""

COMPLETION_ID_PREFIX: str = "chatcmpl-"
"""The prefix OpenAI clients expect on a completion id; the Horde request id follows it."""

SYSTEM_FINGERPRINT_PREFIX: str = "fp_"
"""The prefix of a ``system_fingerprint``, as OpenAI writes it."""

SYSTEM_FINGERPRINT_HEX_CHARACTERS: int = 10
"""The hex characters of the hash kept in a ``system_fingerprint``, as many as OpenAI's carry."""

FILTERED_GENERATION_STATES: frozenset[str] = frozenset(
    {
        "censored",
        "csam",
    },
)
"""Generation states whose output a filter withheld; their choice reports ``content_filter``."""

EMPTY_RETURN_NOTICE: str = (
    "The worker ran this request and returned no visible text. A sampling setting or the model choice is the likely "
    "cause. Quote request id {request_id} when reporting it."
)
"""The ``x_horde.notice`` of a completion whose reply holds no visible text; the request id is filled in."""

NATIVE_BODY_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {
        "stream",
        "stream_options",
        "user",
        "metadata",
    },
)
"""Request keys the front applies or drops itself and never forwards: streaming is the front's, annotations are unused.

``stream: true`` is written back for a request whose deltas the front relays; see
[`build_native_chat_body`][horde.oai.mapping.build_native_chat_body]."""

NATIVE_STREAM_KEY: str = "stream"
"""The native body key that stores the stream hint, which makes the Horde write the progress lease at pop and tell
the worker to post its deltas."""

HORDE_OPTIONS_KEY: str = "horde"
"""The native body key of the Horde extension object."""

OPERATION_KEY_OPTION: str = "operation_key"
"""The Horde option carrying the front's operation key."""

BASE_SUPPORTED_PARAMETERS: tuple[str, ...] = (
    "max_tokens",
    "temperature",
    "top_p",
    "stop",
)
"""The OpenAI request fields every chat worker honours, listed for every model."""

FEATURE_SUPPORTED_PARAMETERS: tuple[str, ...] = (
    "seed",
    "frequency_penalty",
    "presence_penalty",
)
"""The OpenAI request fields a model lists only when an online chat worker declares the chat feature with that value."""

TOOLS_FEATURE: str = ChatFeature.TOOLS.value
"""The chat feature of a worker that serves function calling."""

TOOL_SUPPORTED_PARAMETERS: tuple[str, ...] = ("tools",)
"""The OpenAI request fields a model lists when an online chat worker declares
[`TOOLS_FEATURE`][horde.oai.mapping.TOOLS_FEATURE]."""

GUARANTEED_TOOL_PARAMETERS: dict[str, str] = {
    "tool_choice": ChatGuarantee.TOOL_CHOICE_ENFORCED.value,
    "parallel_tool_calls": ChatGuarantee.SINGLE_TOOL_CALL_ENFORCED.value,
}
"""The OpenAI tool fields a model lists only when an online chat worker also declares the guarantee mapped to each.
A strict request that sets one of them to a constraining value needs that guarantee (ADR 0036), so a card without it
does not advertise the field."""

RESPONSE_FORMAT_FEATURES: frozenset[str] = frozenset(
    {
        ChatFeature.JSON_OBJECT.value,
        ChatFeature.JSON_SCHEMA.value,
    },
)
"""The chat features of which any one lists ``response_format`` for a model."""

RESPONSE_FORMAT_PARAMETER: str = "response_format"
"""The OpenAI request field a model lists when an online chat worker declares one of
[`RESPONSE_FORMAT_FEATURES`][horde.oai.mapping.RESPONSE_FORMAT_FEATURES]."""

COMPACT_JSON_SEPARATORS: tuple[str, str] = (
    ",",
    ":",
)
"""The separators of the compact JSON the prompt token estimate counts for tools and tool calls."""

VISION_FEATURE: str = ChatFeature.VISION.value
"""The chat feature that adds the image input modality to a model list entry."""

TEXT_MODALITY: str = "text"
"""The input modality every model accepts."""

IMAGE_MODALITY: str = "image"
"""The input modality of a model an online worker declares ``vision`` for."""

IMAGE_PLACEHOLDER: str = "[image]"
"""The text an image part counts as in the prompt token estimate; the native route renders an image the same way."""

ERROR_TYPE_BY_STATUS: dict[int, OpenAIErrorType] = {
    HTTPStatus.BAD_REQUEST: OpenAIErrorType.INVALID_REQUEST_ERROR,
    HTTPStatus.UNAUTHORIZED: OpenAIErrorType.AUTHENTICATION_ERROR,
    HTTPStatus.FORBIDDEN: OpenAIErrorType.PERMISSION_ERROR,
    HTTPStatus.NOT_FOUND: OpenAIErrorType.NOT_FOUND_ERROR,
    HTTPStatus.REQUEST_TIMEOUT: OpenAIErrorType.TIMEOUT_ERROR,
    HTTPStatus.TOO_MANY_REQUESTS: OpenAIErrorType.RATE_LIMIT_ERROR,
}
"""The OpenAI error type for each HTTP status; any status not listed is an ``api_error``."""


def build_completion_id(request_id: str) -> str:
    """Return the OpenAI completion id for a Horde request id."""
    return f"{COMPLETION_ID_PREFIX}{request_id}"


def build_system_fingerprint(worker_id: str | None, model: str) -> str | None:
    """Return the ``system_fingerprint`` of a reply from ``worker_id`` serving ``model``, or None without a worker.

    OpenAI's fingerprint changes with the backend configuration that served a request, so a client can tell which
    replies a changed backend produced. The worker and model pair is the closest such configuration the Horde
    reports.
    """
    if worker_id is None:
        return None
    serving_digest = hashlib.sha256(f"{worker_id}\n{model}".encode()).hexdigest()
    return f"{SYSTEM_FINGERPRINT_PREFIX}{serving_digest[:SYSTEM_FINGERPRINT_HEX_CHARACTERS]}"


def build_native_chat_body(
    chat_request: ChatCompletionRequest,
    *,
    operation_key: uuid.UUID,
    relay_deltas: bool = False,
) -> dict[str, object]:
    """Create the native ``POST /v2/generate/chat/async`` body for a validated request.

    Omitted and null keys stay omitted, so the native route fills its defaults; ``developer`` messages, message
    names, ``tool`` messages, tool calls, ``tools``, ``tool_choice``, ``parallel_tool_calls`` and ``response_format``
    are forwarded unchanged, under the client's keys (``json_schema.schema`` included). The one null written is
    the ``content`` of an assistant message that carries only tool calls, which OpenAI writes as null. The public
    request model refuses ``horde.operation_key``, so the key set here is always the front's.

    Args:
        chat_request: The validated request, after the default no-op keys were dropped.
        operation_key: The key claiming this submission for its user, sent as ``horde.operation_key``; a re-sent
            submit carries the same key and body so the native route returns the request it already created.
        relay_deltas: Whether the front relays the request's content deltas, which forwards ``stream: true`` so the
            Horde asks the worker to post them.
    """
    native_body = chat_request.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
        exclude_none=True,
        exclude=set(NATIVE_BODY_EXCLUDED_KEYS),
    )
    native_messages = cast("list[dict[str, object]]", native_body["messages"])
    for native_message, message in zip(native_messages, chat_request.messages, strict=True):
        if message.content is None:
            native_message["content"] = None
    if relay_deltas:
        native_body[NATIVE_STREAM_KEY] = True
    horde_options = native_body.get(HORDE_OPTIONS_KEY)
    native_body[HORDE_OPTIONS_KEY] = {
        **(horde_options if isinstance(horde_options, dict) else {}),
        OPERATION_KEY_OPTION: str(operation_key),
    }
    return native_body


def relays_deltas(chat_request: ChatCompletionRequest) -> bool:
    """Return whether the front relays the worker's content deltas for ``chat_request`` as they are generated.

    Only a streamed request without ``tools`` and with no ``response_format`` other than ``text`` is relayed. The
    Horde verifies a reply's tool calls and, under strict conformance, its structured output only once the reply is
    complete, so text relayed before that check could belong to a reply it then refuses; tool calls also do not
    stream on most chat templates. Every other streamed request is delivered whole when it finishes.

    Args:
        chat_request: The validated request, after the default no-op keys were dropped.
    """
    if chat_request.stream is not True or chat_request.tools:
        return False
    response_format = chat_request.response_format
    return response_format is None or isinstance(response_format, ChatTextResponseFormat)


def build_model_card(active_model: HordeActiveModel) -> ModelCard | None:
    """Create the OpenAI model list entry for a model from the Horde's chat model list.

    Args:
        active_model: One entry of ``GET /v2/status/models?type=text&chat=true``.

    Returns:
        The entry, or None when no online chat worker serves the model.
    """
    chat_offer = active_model.chat
    if chat_offer is None or chat_offer.workers <= 0:
        return None
    declared_features = set(chat_offer.features)
    input_modalities = [TEXT_MODALITY]
    if VISION_FEATURE in declared_features:
        input_modalities.append(IMAGE_MODALITY)
    supported_parameters = [
        *BASE_SUPPORTED_PARAMETERS,
        *(parameter for parameter in FEATURE_SUPPORTED_PARAMETERS if parameter in declared_features),
    ]
    if TOOLS_FEATURE in declared_features:
        supported_parameters.extend(TOOL_SUPPORTED_PARAMETERS)
        supported_parameters.extend(
            parameter for parameter, guarantee in GUARANTEED_TOOL_PARAMETERS.items() if guarantee in declared_features
        )
    if RESPONSE_FORMAT_FEATURES & declared_features:
        supported_parameters.append(RESPONSE_FORMAT_PARAMETER)
    return ModelCard(
        model_id=active_model.name,
        created=chat_offer.created or 0,
        context_length=chat_offer.max_context_length,
        architecture=ModelArchitecture(
            modality=f"{'+'.join(input_modalities)}->{TEXT_MODALITY}",
            input_modalities=input_modalities,
        ),
        top_provider=ModelTopProvider(
            context_length=chat_offer.max_context_length,
            max_completion_tokens=chat_offer.max_length,
        ),
        supported_parameters=supported_parameters,
        x_horde=HordeModelDetails(workers=chat_offer.workers, queued=active_model.queued),
    )


def flatten_messages(
    messages: Sequence[ChatMessage],
    *,
    tools: Sequence[ChatTool] | None = None,
) -> str:
    """Return the text the prompt token estimate counts, joined by newlines.

    The worker renders tool definitions into the prompt, so their compact JSON counts as well as each message's role,
    text and tool calls. A response format schema does not count: the reference worker compiles it into a grammar and
    never renders it into the prompt, as the native route's estimate assumes too.

    Args:
        messages: The request's messages.
        tools: The request's tools, when it carries any.
    """
    flattened_lines: list[str] = []
    if tools:
        flattened_lines.append(_compact_json([tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]))
    for message in messages:
        flattened_lines.append(message.role.value)
        if isinstance(message.content, str):
            flattened_lines.append(message.content)
        elif message.content is not None:
            flattened_lines.extend(
                content_part.text if isinstance(content_part, TextContentPart) else IMAGE_PLACEHOLDER for content_part in message.content
            )
        if message.tool_calls:
            flattened_lines.append(_compact_json([tool_call.model_dump(mode="json") for tool_call in message.tool_calls]))
    return "\n".join(flattened_lines)


def _compact_json(value: object) -> str:
    """Return ``value`` as compact JSON, the form the token estimate counts."""
    return json.dumps(value, separators=COMPACT_JSON_SEPARATORS, ensure_ascii=False)


def estimate_tokens(text: str) -> int:
    """Return the estimated token count of ``text``."""
    return math.ceil(len(text) / CHARACTERS_PER_ESTIMATED_TOKEN)


def generation_content(generation: HordeChatGeneration) -> str | None:
    """Return a generation's reply text: the message content when present, else the plain ``text`` field.

    A message carrying tool calls supplies the content alone, so the plain field never stands in for a null content.

    Returns:
        The text, or None when the message carries tool calls and no content.
    """
    if generation.message is not None and (generation.message.content is not None or generation.message.tool_calls):
        return generation.message.content
    return generation.text or ""


def generation_reasoning(generation: HordeChatGeneration) -> str | None:
    """Return the reasoning of a generation's message, or None when the worker submitted none."""
    if generation.message is None:
        return None
    return generation.message.reasoning_content


def generation_tool_calls(generation: HordeChatGeneration) -> list[ChatToolCall] | None:
    """Return the tool calls of a generation's message, or None when it carries none."""
    if generation.message is None or not generation.message.tool_calls:
        return None
    return generation.message.tool_calls


class CompletionWithoutGenerationError(Exception):
    """The status a completion was to be built from has no finished generation, so it would carry no reply."""


def build_completion(
    *,
    request_id: str,
    created: int,
    chat_request: ChatCompletionRequest,
    status: HordeChatStatus,
) -> ChatCompletionResponse:
    """Create the OpenAI completion for a finished Horde chat request.

    Args:
        request_id: The Horde request id.
        created: When the request was submitted, in Unix seconds.
        chat_request: The client's request, used for the model fallback, the prompt estimate and
            ``horde.include_reasoning``.
        status: The Horde's terminal status of the request.

    Returns:
        One choice per generation, in the Horde's order; ``model``, ``system_fingerprint`` and the ``x_horde`` worker
        details are the first generation's. A message carries ``reasoning_content`` only under
        ``horde.include_reasoning``. ``usage.completion_tokens_details.reasoning_tokens`` is the sum of the reasoning
        counts the workers reported, present when any reported one, whether or not the reasoning is returned.

    Raises:
        CompletionWithoutGenerationError: ``status`` has no finished generation. A completion built from it would be a
            reply with no choice and estimated usage, delivered as a success.
    """
    if not status.generations:
        raise CompletionWithoutGenerationError(f"Chat request {request_id} has no finished generation to build a completion from.")
    choices: list[ChatCompletionChoice] = []
    completion_tokens = 0
    estimated_usage = False
    prompt_tokens: int | None = None
    reasoning_tokens: int | None = None
    has_empty_return = False
    include_reasoning = chat_request.horde is not None and chat_request.horde.include_reasoning
    for generation_index, generation in enumerate(status.generations):
        generated_text = generation_content(generation)
        tool_calls = generation_tool_calls(generation)
        is_filtered = generation.state in FILTERED_GENERATION_STATES
        if not is_filtered and tool_calls is None and not (generated_text or "").strip():
            has_empty_return = True
        choices.append(
            ChatCompletionChoice(
                index=generation_index,
                message=ChatCompletionResponseMessage(
                    content=generated_text,
                    tool_calls=tool_calls,
                    reasoning_content=generation_reasoning(generation) if include_reasoning else None,
                ),
                finish_reason=FinishReason.CONTENT_FILTER if is_filtered else _finish_reason(generation.finish_reason),
                native_finish_reason=generation.native_finish_reason,
            ),
        )
        if generation.completion_tokens is None:
            completion_tokens += estimate_tokens(_generated_output_text(generated_text, tool_calls))
            estimated_usage = True
        else:
            completion_tokens += generation.completion_tokens
        if prompt_tokens is None and generation.prompt_tokens is not None:
            prompt_tokens = generation.prompt_tokens
        if generation.reasoning_tokens is not None:
            reasoning_tokens = (reasoning_tokens or 0) + generation.reasoning_tokens
    if prompt_tokens is None:
        prompt_tokens = estimate_tokens(
            flatten_messages(chat_request.messages, tools=chat_request.tools),
        )
        estimated_usage = True
    first_generation = status.generations[0]
    model = first_generation.model or chat_request.requested_models()[0]
    return ChatCompletionResponse(
        completion_id=build_completion_id(request_id),
        created=created,
        model=model,
        choices=choices,
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            completion_tokens_details=None if reasoning_tokens is None else CompletionTokensDetails(reasoning_tokens=reasoning_tokens),
        ),
        system_fingerprint=build_system_fingerprint(first_generation.worker_id, model),
        x_horde=HordeResponseDetails(
            request_id=request_id,
            worker_id=first_generation.worker_id,
            worker_name=first_generation.worker_name,
            kudos=status.kudos,
            charge_basis=first_generation.charge_basis,
            estimated_usage=estimated_usage,
            queue_wait_seconds=first_generation.queue_wait_seconds,
            context_adjustments=status.context_adjustments,
            notice=EMPTY_RETURN_NOTICE.format(request_id=request_id) if has_empty_return else None,
        ),
    )


def _generated_output_text(generated_text: str | None, tool_calls: Sequence[ChatToolCall] | None) -> str:
    """Return the text the completion token estimate counts: the reply text and the compact JSON of its tool calls."""
    output_parts = [generated_text or ""]
    if tool_calls:
        output_parts.append(_compact_json([tool_call.model_dump(mode="json") for tool_call in tool_calls]))
    return "\n".join(output_parts)


def _finish_reason(reported_finish_reason: str | None) -> FinishReason:
    """Return the finish reason a worker reported, or ``stop`` when it reported none or an unknown one."""
    if reported_finish_reason is None:
        return FinishReason.STOP
    try:
        return FinishReason(reported_finish_reason)
    except ValueError:
        return FinishReason.STOP


def build_error(
    *,
    status_code: int,
    message: str,
    code: str | None,
    param: str | None = None,
    metadata: ErrorMetadata | None = None,
) -> OpenAIError:
    """Create the OpenAI error body for an HTTP status.

    Args:
        status_code: The HTTP status the error is returned with, or would have been before stream headers.
        message: The explanation for the client.
        code: The Horde return code, or the front's own code.
        param: The request path at fault, when one is.
        metadata: The request id, return code and retry details that are known.
    """
    return OpenAIError(
        message=message,
        type=ERROR_TYPE_BY_STATUS.get(status_code, OpenAIErrorType.API_ERROR),
        param=param,
        code=code,
        metadata=metadata if metadata is not None else ErrorMetadata(),
    )


def build_error_envelope(
    *,
    status_code: int,
    message: str,
    code: str | None,
    param: str | None = None,
    metadata: ErrorMetadata | None = None,
) -> OpenAIErrorEnvelope:
    """Create the OpenAI error envelope for an HTTP status.

    Args:
        status_code: The HTTP status the error is returned with, or would have been before stream headers.
        message: The explanation for the client.
        code: The Horde return code, or the front's own code.
        param: The request path at fault, when one is.
        metadata: The request id, return code and retry details that are known.
    """
    return OpenAIErrorEnvelope(
        error=build_error(status_code=status_code, message=message, code=code, param=param, metadata=metadata),
    )
