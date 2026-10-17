# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Map a chat request to the native body, a finished Horde chat status to the OpenAI completion, a chat model list
entry to the OpenAI model entry, and an error to the OpenAI error envelope.

Usage counts come from the worker's report when there is one. A worker that does not report counts gets an estimate
of one token per [`CHARACTERS_PER_ESTIMATED_TOKEN`][horde.oai.mapping.CHARACTERS_PER_ESTIMATED_TOKEN] characters, and
``x_horde.estimated_usage`` says so. A generation whose reply holds no visible text gets
[`EMPTY_RETURN_NOTICE`][horde.oai.mapping.EMPTY_RETURN_NOTICE] in ``x_horde.notice``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from http import HTTPStatus

from horde.oai.horde_client import HordeActiveModel, HordeChatGeneration, HordeChatStatus
from horde.oai.schema import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseMessage,
    ChatCompletionUsage,
    ChatMessage,
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
    "build_completion",
    "build_completion_id",
    "build_error",
    "build_error_envelope",
    "build_model_card",
    "build_native_chat_body",
    "estimate_tokens",
    "flatten_messages",
    "generation_content",
]

CHARACTERS_PER_ESTIMATED_TOKEN: int = 3
"""Characters counted as one token when a worker does not report counts.

A rough ratio for English text; the estimate only fills the usage object, which ``x_horde.estimated_usage`` marks.
"""

COMPLETION_ID_PREFIX: str = "chatcmpl-"
"""The prefix OpenAI clients expect on a completion id; the Horde request id follows it."""

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
"""Request keys the front applies or drops itself and never forwards: streaming is the front's, annotations are unused."""

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
"""The OpenAI request fields a model lists only when an online chat worker declares the chat feature of that name."""

VISION_FEATURE: str = "vision"
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


def build_native_chat_body(chat_request: ChatCompletionRequest, *, operation_key: uuid.UUID) -> dict[str, object]:
    """Create the native ``POST /v2/generate/chat/async`` body for a validated request.

    Omitted and null keys stay omitted, so the native route fills its defaults; ``developer`` messages and message
    names are forwarded unchanged. The public request model refuses ``horde.operation_key``, so the key set here is
    always the front's.

    Args:
        chat_request: The validated request, after the default no-op keys were dropped.
        operation_key: The key claiming this submission for its user, sent as ``horde.operation_key``; a re-sent
            submit carries the same key and body so the native route returns the request it already created.
    """
    native_body = chat_request.model_dump(mode="json", exclude_unset=True, exclude_none=True, exclude=set(NATIVE_BODY_EXCLUDED_KEYS))
    horde_options = native_body.get(HORDE_OPTIONS_KEY)
    native_body[HORDE_OPTIONS_KEY] = {
        **(horde_options if isinstance(horde_options, dict) else {}),
        OPERATION_KEY_OPTION: str(operation_key),
    }
    return native_body


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


def flatten_messages(messages: Sequence[ChatMessage]) -> str:
    """Return the messages' roles and texts joined by newlines, the text the prompt token estimate counts."""
    flattened_lines: list[str] = []
    for message in messages:
        flattened_lines.append(message.role.value)
        if isinstance(message.content, str):
            flattened_lines.append(message.content)
        else:
            flattened_lines.extend(
                content_part.text if isinstance(content_part, TextContentPart) else IMAGE_PLACEHOLDER for content_part in message.content
            )
    return "\n".join(flattened_lines)


def estimate_tokens(text: str) -> int:
    """Return the estimated token count of ``text``."""
    return math.ceil(len(text) / CHARACTERS_PER_ESTIMATED_TOKEN)


def generation_content(generation: HordeChatGeneration) -> str:
    """Return a generation's reply text: the message content when present, else the plain ``text`` field."""
    if generation.message is not None and generation.message.content is not None:
        return generation.message.content
    return generation.text or ""


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
        chat_request: The client's request, used for the model fallback and the prompt estimate.
        status: The Horde's terminal status of the request.

    Returns:
        One choice per generation, in the Horde's order; ``model`` and the ``x_horde`` worker details are the first
        generation's.
    """
    choices: list[ChatCompletionChoice] = []
    completion_tokens = 0
    estimated_usage = False
    prompt_tokens: int | None = None
    has_empty_return = False
    for generation_index, generation in enumerate(status.generations):
        generated_text = generation_content(generation)
        is_filtered = generation.state in FILTERED_GENERATION_STATES
        if not is_filtered and not generated_text.strip():
            has_empty_return = True
        choices.append(
            ChatCompletionChoice(
                index=generation_index,
                message=ChatCompletionResponseMessage(content=generated_text),
                finish_reason=FinishReason.CONTENT_FILTER if is_filtered else _finish_reason(generation.finish_reason),
                native_finish_reason=generation.native_finish_reason,
            ),
        )
        if generation.completion_tokens is None:
            completion_tokens += estimate_tokens(generated_text)
            estimated_usage = True
        else:
            completion_tokens += generation.completion_tokens
        if prompt_tokens is None and generation.prompt_tokens is not None:
            prompt_tokens = generation.prompt_tokens
    if prompt_tokens is None:
        prompt_tokens = estimate_tokens(flatten_messages(chat_request.messages))
        estimated_usage = True
    first_generation = status.generations[0] if status.generations else None
    return ChatCompletionResponse(
        completion_id=build_completion_id(request_id),
        created=created,
        model=first_generation.model if first_generation is not None and first_generation.model else chat_request.model,
        choices=choices,
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
        x_horde=HordeResponseDetails(
            request_id=request_id,
            worker_id=first_generation.worker_id if first_generation is not None else None,
            worker_name=first_generation.worker_name if first_generation is not None else None,
            kudos=status.kudos,
            charge_basis=first_generation.charge_basis if first_generation is not None else None,
            estimated_usage=estimated_usage,
            queue_wait_seconds=first_generation.queue_wait_seconds if first_generation is not None else None,
            context_adjustments=status.context_adjustments,
            notice=EMPTY_RETURN_NOTICE.format(request_id=request_id) if has_empty_return else None,
        ),
    )


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
