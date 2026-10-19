# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Redis pub/sub signals that tell a waiting client a chat request changed state.

A process holding a chat request open subscribes to ``horde:chat:<request id>`` instead of polling the
status route. [`publish_chat_event`][horde.chat_signal.publish_chat_event] publishes
``{"id": "<request id>", "event": "<event>", "generations": <stored generation count>}`` there, where the
event is one of [`ChatEvent`][horde.chat_signal.ChatEvent]. A ``faulted`` event adds ``"fault_reason"`` and
``"fault_rc"`` when a reply to the request was refused, so a client that finds the row gone still learns why. A
message never carries generated text.

Publishing is best effort. A subscriber that misses a message still reaches the result through the
status route, so a Redis failure is logged and never fails the submit, cancel or cleanup that published.
"""

from __future__ import annotations

import enum
import json
import threading

import redis

from horde.logger import logger
from horde.redis_ctrl import get_chat_signal_db

CHAT_SIGNAL_CHANNEL_PREFIX = "horde:chat:"
"""Prefix of the channel a chat request's events are published on; the request id follows it."""


class ChatEvent(enum.StrEnum):
    """What happened to a chat request."""

    GENERATION = "generation"
    """One generation landed. A request with ``n`` above 1 publishes one per generation."""

    DONE = "done"
    """Every generation the request asked for has finished."""

    FAULTED = "faulted"
    """The request faulted or expired before it finished."""

    CANCELLED = "cancelled"
    """The request was cancelled."""


_publisher_lock = threading.Lock()
_publisher: redis.Redis | None = None


def _get_publisher() -> redis.Redis:
    """Return the shared publishing client, creating it once; a redis client reconnects on its own."""
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            _publisher = get_chat_signal_db()
        return _publisher


def chat_signal_channel(request_id: str) -> str:
    """Return the channel a chat request's events are published on.

    Args:
        request_id: The chat request's id.

    Returns:
        ``horde:chat:<request id>``.
    """
    return f"{CHAT_SIGNAL_CHANNEL_PREFIX}{request_id}"


def publish_chat_event(
    request_id: str,
    event: ChatEvent,
    generation_count: int,
    *,
    fault_reason: str | None = None,
    fault_rc: str | None = None,
) -> None:
    """Publish one event for a chat request, logging rather than raising when Redis is unavailable.

    Args:
        request_id: The chat request's id.
        event: What happened to it.
        generation_count: How many of its generations hold a stored result when the event is published.
        fault_reason: The message of the last refused reply, for a ``faulted`` event; left out of the message when
            None.
        fault_rc: The return code of that refusal; left out of the message when None.

    Side Effects:
        Publishes one message on the request's channel.
    """
    payload: dict[str, object] = {"id": request_id, "event": event.value, "generations": generation_count}
    if fault_reason is not None:
        payload["fault_reason"] = fault_reason
    if fault_rc is not None:
        payload["fault_rc"] = fault_rc
    message = json.dumps(payload)
    try:
        _get_publisher().publish(chat_signal_channel(request_id), message)
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to publish chat event '{event.value}' for request {request_id}: {redis_error}")
