# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Render a streamed chat answer as server-sent events.

The Horde produces the whole text before it reports a generation, so a stream is buffered: keepalive comments while
the request waits (``: horde queue_position=7 wait_time=42``), then the finished completion as a role chunk, a
content chunk when the reply has text, a tool-call chunk when it has calls, a finish chunk carrying ``x_horde``, the
usage chunk when the client asked for it, and ``[DONE]``. The tool-call chunk carries every call whole with its
``index``, so a client that accumulates deltas rebuilds the calls. Nullable choice fields remain explicit; absent delta
members are omitted.

An error after the headers is one ``data:`` event carrying the same ``{"error": {...}}`` envelope as a response
before headers, with null fields written explicitly and no choice, followed by ``[DONE]``.
"""

from __future__ import annotations

import json

from sse_starlette import ServerSentEvent

from horde.chat_contract import ChatToolCall
from horde.oai.horde_client import HordeChatStatus
from horde.oai.schema import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionChunkToolCall,
    ChatCompletionResponse,
    OpenAIErrorEnvelope,
)

__all__ = [
    "DONE_MARKER",
    "KEEPALIVE_COMMENT_PREFIX",
    "build_completion_events",
    "build_error_events",
    "build_keepalive_event",
]

DONE_MARKER: str = "[DONE]"
"""The data of the last event of every stream, which OpenAI clients read as the end."""

KEEPALIVE_COMMENT_PREFIX: str = "horde"
"""The first word of a keepalive comment; the queue position and wait estimate follow it as ``key=value`` pairs."""


def build_keepalive_event(status: HordeChatStatus | None) -> ServerSentEvent:
    """Create the keepalive comment for the request's latest status.

    Args:
        status: The latest status read, or None before the first read succeeded.

    Returns:
        A comment event, ``: horde queue_position=7 wait_time=42``; a value the Horde did not report is left out.
    """
    comment_parts = [KEEPALIVE_COMMENT_PREFIX]
    if status is not None and status.queue_position is not None:
        comment_parts.append(f"queue_position={status.queue_position}")
    if status is not None and status.wait_time is not None:
        comment_parts.append(f"wait_time={status.wait_time}")
    return ServerSentEvent(comment=" ".join(comment_parts))


def build_completion_events(completion: ChatCompletionResponse, *, include_usage: bool) -> list[ServerSentEvent]:
    """Create the events that deliver a finished completion, ending with ``[DONE]``.

    Args:
        completion: The finished completion.
        include_usage: Whether to send the usage chunk the client asked for with ``stream_options.include_usage``.
    """
    role_chunk = _build_chunk(
        completion,
        choices=[
            ChatCompletionChunkChoice(index=choice.index, delta=ChatCompletionChunkDelta(role="assistant")) for choice in completion.choices
        ],
    )
    # Text and tool calls travel in separate chunks, as OpenAI streams them: clients such as KoboldAI Lite read
    # tool-call deltas only from chunks that carry no content, so a chunk holding both would lose the calls.
    body_chunks: list[ChatCompletionChunk] = []
    if any(choice.message.content is not None for choice in completion.choices):
        body_chunks.append(
            _build_chunk(
                completion,
                choices=[
                    ChatCompletionChunkChoice(index=choice.index, delta=ChatCompletionChunkDelta(content=choice.message.content))
                    for choice in completion.choices
                ],
            ),
        )
    if any(choice.message.tool_calls for choice in completion.choices):
        body_chunks.append(
            _build_chunk(
                completion,
                choices=[
                    ChatCompletionChunkChoice(
                        index=choice.index,
                        delta=ChatCompletionChunkDelta(tool_calls=_chunk_tool_calls(choice.message.tool_calls)),
                    )
                    for choice in completion.choices
                ],
            ),
        )
    finish_chunk = _build_chunk(
        completion,
        choices=[
            ChatCompletionChunkChoice(
                index=choice.index,
                delta=ChatCompletionChunkDelta(),
                finish_reason=choice.finish_reason,
                native_finish_reason=choice.native_finish_reason,
            )
            for choice in completion.choices
        ],
        x_horde=True,
    )
    events = [
        _chunk_event(role_chunk, include_usage=include_usage),
        *(_chunk_event(body_chunk, include_usage=include_usage) for body_chunk in body_chunks),
        _chunk_event(finish_chunk, include_usage=include_usage),
    ]
    if include_usage:
        usage_chunk = ChatCompletionChunk(
            completion_id=completion.completion_id,
            created=completion.created,
            model=completion.model,
            choices=[],
            usage=completion.usage,
            system_fingerprint=completion.system_fingerprint,
        )
        events.append(_chunk_event(usage_chunk, include_usage=True))
    events.append(ServerSentEvent(data=DONE_MARKER))
    return events


def build_error_events(envelope: OpenAIErrorEnvelope) -> list[ServerSentEvent]:
    """Create the events that report an error after the stream has started, ending with ``[DONE]``.

    The envelope is serialised with its null fields, so ``param`` and ``code`` are present even when null.

    Args:
        envelope: The error to report.
    """
    return [
        ServerSentEvent(data=envelope.model_dump_json()),
        ServerSentEvent(data=DONE_MARKER),
    ]


def _chunk_tool_calls(tool_calls: list[ChatToolCall] | None) -> list[ChatCompletionChunkToolCall] | None:
    """Return a reply's tool calls as delta entries, each with its position in the reply, or None when it has none."""
    if tool_calls is None:
        return None
    return [
        ChatCompletionChunkToolCall(index=call_index, id=tool_call.id, type=tool_call.type, function=tool_call.function)
        for call_index, tool_call in enumerate(tool_calls)
    ]


def _build_chunk(
    completion: ChatCompletionResponse,
    *,
    choices: list[ChatCompletionChunkChoice],
    x_horde: bool = False,
) -> ChatCompletionChunk:
    """Create a chunk of ``completion`` carrying ``choices``, and the Horde details when ``x_horde`` is set."""
    return ChatCompletionChunk(
        completion_id=completion.completion_id,
        created=completion.created,
        model=completion.model,
        choices=choices,
        system_fingerprint=completion.system_fingerprint,
        x_horde=completion.x_horde if x_horde else None,
    )


def _chunk_event(chunk: ChatCompletionChunk, *, include_usage: bool) -> ServerSentEvent:
    """Create the data event for one chunk."""
    payload = chunk.model_dump(mode="json")
    if chunk.x_horde is None:
        payload.pop("x_horde")
    if not include_usage:
        payload.pop("usage")
    for choice in payload["choices"]:
        choice["delta"] = {key: value for key, value in choice["delta"].items() if value is not None}
    return ServerSentEvent(data=json.dumps(payload))
