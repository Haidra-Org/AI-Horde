# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wait for Horde chat requests to finish, woken by the Horde's Redis completion signal or a fallback poll.

The Horde publishes ``{"id": <request id>, "event": ...}`` on channel ``horde:chat:<request id>`` when a chat
generation lands and when the request finishes, faults or is cancelled.
[`CompletionSignalListener`][horde.oai.waiter.CompletionSignalListener] holds one pattern subscription to every such
channel for the whole process and hands each message to the request registered for it, so thousands of waiting
requests share one Redis connection; messages for requests this process is not waiting on are dropped. A
subscription that receives nothing is pinged, and one whose PONG does not arrive is dropped and made again.

[`DeltaStreamReader`][horde.oai.waiter.DeltaStreamReader] reads the content deltas a worker posts while it generates.
The Horde appends them to the stream ``horde:chat:stream:<request id>``; one task per process reads the streams of
every registered request with a single blocking ``XREAD`` and queues each entry on that request's
[`DeltaSubscription`][horde.oai.waiter.DeltaSubscription]. The block is short, so a request registered meanwhile
joins the next read and one connection serves every request.

[`ChatRequestWatcher`][horde.oai.waiter.ChatRequestWatcher] is the one wait implementation both response modes use.
After the submit it reads the status once (the request may have finished before the registration), then sleeps until
a signal, the jittered fallback poll, the fail-fast check or the deadline, and reads the status once on each wake.
A terminal signal whose outcome the next read does not yet show earns one short recheck. A status that is done, not
faulted and without a finished generation is not terminal at first: a Horde in the middle of a fault and its restart
can show it. When it still reports a job waiting or processing it is waited on as any unfinished request; otherwise
it earns one short recheck too, and ends the wait with
[`ChatDoneWithoutGenerationError`][horde.oai.waiter.ChatDoneWithoutGenerationError] once that recheck still shows it,
or at once after a ``cancelled`` signal, since a request cancelled with no generation stays in that state. The poll
covers a lost message and a Redis outage; its interval stretches with the request's reported wait (see
[`compute_fallback_poll_seconds`][horde.oai.waiter.compute_fallback_poll_seconds]).

The deadline bounds the wait, not the outcome: a status read that returns a terminal outcome is returned even when it
completes after the deadline. At the deadline one final read runs under its own allowance; only a request still
waiting after it raises [`ChatWaitTimeoutError`][horde.oai.waiter.ChatWaitTimeoutError], or
[`ChatDoneWithoutGenerationError`][horde.oai.waiter.ChatDoneWithoutGenerationError] when that read shows it done
without a generation.

A request no worker is eligible for, with nothing processing, ends the wait after the fail-fast grace, which covers a
worker restart or model load. A status that reports the cause as a declaration gap ends it at once with
[`DeclarationGapError`][horde.oai.waiter.DeclarationGapError]: a worker coming back does not supply a declaration no
online worker serving the model makes.

A request in the ``fail_fast`` availability mode is also decided at its first successful status read: with nothing
processing, it ends with [`QueueUnavailableError`][horde.oai.waiter.QueueUnavailableError] when the pool has zero
eligible worker threads or when the Horde's wait estimate exceeds the time left in the budget. A request just
submitted is absent from the Horde's cached queue positions, so a first status without queue information is read
again until one carries it or [`POSITIONED_READ_WAIT_SECONDS`][horde.oai.waiter.POSITIONED_READ_WAIT_SECONDS] passes,
and the latest reading then decides. Later reads follow the grace as in the ``wait`` mode.
[`wait_for_admission`][horde.oai.waiter.ChatRequestWatcher.wait_for_admission] runs the wait up to that decision, so
a streamed request can answer a refusal before its response opens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from enum import StrEnum
from http import HTTPStatus

import httpx
import logfire
import redis.asyncio
import redis.exceptions
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from horde.chat_contract import ChatAvailabilityMode, ChatImpossibleReason
from horde.oai.horde_client import HordeAPIError, HordeChatStatus, HordeClient, HordeCredentials, HordeUnavailableError
from horde.oai.metrics import FrontMetrics

__all__ = [
    "CHAT_SIGNAL_CHANNEL_PREFIX",
    "MAX_STRETCHED_POLL_SECONDS",
    "ChatDoneWithoutGenerationError",
    "ChatRequestWatcher",
    "ChatWaitTimeoutError",
    "CompletionEvent",
    "CompletionEventKind",
    "CompletionSignalListener",
    "CompletionSubscription",
    "DeclarationGapError",
    "DeltaEntry",
    "DeltaStreamReader",
    "DeltaSubscription",
    "NoEligibleWorkerError",
    "QueueUnavailableError",
    "QueueUnavailableReason",
    "WaitBudget",
    "WakeSource",
    "compute_fallback_poll_seconds",
    "jitter_poll_seconds",
]

CHAT_SIGNAL_CHANNEL_PREFIX: str = "horde:chat:"
"""The prefix of the channel the Horde publishes a chat request's events on; the request id follows it."""

CHAT_SIGNAL_CHANNEL_PATTERN: str = f"{CHAT_SIGNAL_CHANNEL_PREFIX}*"
"""The pattern the listener subscribes to, covering every chat request's channel."""

LISTENER_READ_TIMEOUT_SECONDS: float = 1.0
"""How long one read of the subscription waits for a message, so a stop request is noticed within this time."""

PONG_MESSAGE_TYPE: str = "pong"
"""The pub/sub message type redis-py gives the answer to a PING sent on a subscribed connection."""

LISTENER_RECONNECT_DELAY_SECONDS: float = 2.0
"""How long the listener waits before subscribing again after a Redis error, so an outage does not spin the loop."""

DELTA_STREAM_KEY_PREFIX: str = "horde:chat:stream:"
"""The prefix of the stream the Horde appends a chat request's content deltas to; the request id follows it."""

DELTA_STREAM_START_ID: str = "0-0"
"""The stream id a registered request's first read starts after, so it receives every entry the stream holds."""

DELTA_READ_BLOCK_MILLISECONDS: int = 250
"""How long one ``XREAD`` blocks waiting for an entry.

The reader reads the registered key set again after each block, so a newly registered request waits at most this long
to join, and a delta reaches its client within it.
"""

DELTA_READ_COUNT: int = 100
"""The most entries one ``XREAD`` returns per stream; a reader that fell behind catches up over several reads."""

DELTA_READ_SPAN_NAME: str = "horde.oai.delta_read"
"""The span recorded for each delta stream read that returns entries.

An empty read does not get a span: with any streamed request registered the reader reads every block, so empty
reads would add several spans a second per process without a relayed chunk.
"""

DELTA_READ_ENTRY_COUNT_ATTRIBUTE: str = "horde.entry_count"
"""The read span attribute holding how many stream entries the read returned, across every stream."""

DELTA_READ_BYTE_COUNT_ATTRIBUTE: str = "horde.byte_count"
"""The read span attribute holding the UTF-8 size of the returned entries' field names and values."""

DELTA_READ_FIRST_ENTRY_LAG_ATTRIBUTE: str = "horde.first_entry_lag_seconds"
"""The read span attribute holding the seconds from the oldest returned entry's append to the read's return.

The append time is the millisecond part of the entry's auto-generated stream id, taken from the Redis server's
clock, so a skew between that clock and the front's shifts the value by the same amount.
"""

STREAM_ID_MILLISECONDS_PER_SECOND: int = 1000
"""The unit of the time part of a Redis stream entry id, which counts milliseconds."""

DELTA_READ_TIMEOUT_SECONDS: float = 5.0
"""How long one ``XREAD`` may take before the connection is treated as dead and the read is made again.

A blocking read answers within its block, so a read this far past it is on a connection a network fault
black-holed; no PING is needed.
"""

MAX_STRETCHED_POLL_SECONDS: float = 60.0
"""The longest a stretched fallback poll interval grows, so a lost signal costs at most about a minute of extra wait."""

WAIT_TIME_POLL_DIVISOR: float = 2.0
"""The fraction of the Horde's ``wait_time`` estimate a poll interval stretches to: half, so a request finishing on
schedule is still polled about twice before its estimate runs out."""

POLL_JITTER_FRACTION: float = 0.2
"""The most a poll interval is shortened at random, so requests submitted together do not poll the Horde in step.

Jitter only shortens the interval, so a lost signal still costs at most the stretched maximum.
"""

TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
"""Horde statuses that a status read survives: the request keeps waiting and tries again at the next poll."""

POSITIONED_READ_WAIT_SECONDS: float = 2.0
"""How long a ``fail_fast`` request's first-read decision waits for a status that carries queue information.

A request just submitted is absent from the Horde's cached queue positions until the cache next refreshes: the Horde
stores the positions every second and each process reloads them at most once a second, so a fresh request can read a
position and wait of zero for about 2 s whatever is queued ahead of it.
"""


class CompletionEventKind(StrEnum):
    """The events the Horde publishes for a chat request."""

    GENERATION = "generation"
    DONE = "done"
    FAULTED = "faulted"
    CANCELLED = "cancelled"


FINISHING_EVENT_KINDS: frozenset[CompletionEventKind] = frozenset(
    {
        CompletionEventKind.DONE,
        CompletionEventKind.FAULTED,
        CompletionEventKind.CANCELLED,
    },
)
"""The events after which the request's status should show it finished."""


class WakeSource(StrEnum):
    """What woke a waiting request."""

    IMMEDIATE = "immediate"
    """The status read right after the submit."""
    SIGNAL = "signal"
    """A completion signal for the request."""
    RECHECK = "recheck"
    """The short recheck a terminal signal earns when the read after it does not show the outcome yet."""
    POLL = "poll"
    """The fallback poll or the fail-fast check."""
    DEADLINE = "deadline"
    """The final status read at the deadline."""
    DISCONNECT = "disconnect"
    """The client disconnected; the application ends the wait without a status read."""


class CompletionEvent(BaseModel):
    """Represents one completion signal message."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(validation_alias="id")
    """The Horde request id."""

    event: CompletionEventKind
    """What happened to the request."""

    fault_reason: str | None = None
    """For a ``faulted`` event, the message the Horde refused the request's last refused reply with; None otherwise."""

    fault_rc: str | None = None
    """The return code of that refusal; None when ``fault_reason`` is."""


class ChatWaitTimeoutError(Exception):
    """The request was still waiting after its deadline and the final status read."""


class ChatDoneWithoutGenerationError(Exception):
    """The request settled done, not faulted and without a finished generation, so no completion can be built.

    Raised when a recheck still shows that state, after a ``cancelled`` signal, or at the deadline's final read.
    """


class NoEligibleWorkerError(Exception):
    """The Horde reported no eligible worker, with nothing processing, for longer than the fail-fast grace, or with a
    cause the grace does not change."""

    def __init__(self, message: str, *, status: HordeChatStatus | None) -> None:
        """Create the error.

        Args:
            message: What happened.
            status: The last status read, whose ``wait_time`` hints when to retry.
        """
        super().__init__(message)
        self.status: HordeChatStatus | None = status
        """The last status read."""


class DeclarationGapError(NoEligibleWorkerError):
    """The Horde reported that the online chat workers serving the model lack a declaration the request needs or apply
    a limit it exceeds; raised on the first such status, without the grace."""

    def __init__(self, message: str, *, status: HordeChatStatus, impossible_reason: ChatImpossibleReason) -> None:
        """Create the error.

        Args:
            message: What happened.
            status: The status that reported the gap.
            impossible_reason: The missing declarations and exceeded limits it reported.
        """
        super().__init__(message, status=status)
        self.impossible_reason: ChatImpossibleReason = impossible_reason
        """The missing declarations and exceeded limits."""


class QueueUnavailableReason(StrEnum):
    """Why a ``fail_fast`` request was refused at its first status read."""

    NO_ELIGIBLE_THREADS = "no_eligible_threads"
    """The pool had zero eligible worker threads and nothing processed the request."""
    WAIT_EXCEEDS_BUDGET = "wait_exceeds_budget"
    """The Horde's wait estimate exceeded the time left in the request's budget."""


class QueueUnavailableError(Exception):
    """The first status read of a ``fail_fast`` request showed the online pool unable to start it within its budget."""

    def __init__(
        self,
        message: str,
        *,
        status: HordeChatStatus,
        reason: QueueUnavailableReason,
        remaining_seconds: float,
    ) -> None:
        """Create the error.

        Args:
            message: What happened.
            status: The status read that decided the refusal, with the pool's thread count and wait estimate.
            reason: Which rule refused the request.
            remaining_seconds: The time left in the request's budget at that read.
        """
        super().__init__(message)
        self.status: HordeChatStatus = status
        """The status read that decided the refusal."""
        self.reason: QueueUnavailableReason = reason
        """Which rule refused the request."""
        self.remaining_seconds: float = remaining_seconds
        """The time left in the request's budget at that read."""


class WaitBudget(BaseModel):
    """Represents the time limits of one request's wait."""

    model_config = ConfigDict(frozen=True)

    deadline: float
    """The ``clock`` time the wait ends at; it bounds the wait, not the outcome."""

    final_status_timeout_seconds: float
    """The allowance for the one status read made at the deadline."""

    fallback_poll_seconds: float
    """The base poll interval when no signal arrives."""

    signal_recheck_seconds: float
    """How soon to read once more after a terminal signal the first read did not yet show."""

    fail_fast_grace_seconds: float
    """How long no eligible worker, with nothing processing, is tolerated."""


class CompletionSubscription:
    """Receive the completion events of one request from the listener."""

    def __init__(self, request_id: str) -> None:
        """Create the subscription.

        Args:
            request_id: The Horde request id.
        """
        self.request_id: str = request_id
        """The Horde request id."""
        self._events: asyncio.Queue[CompletionEvent] = asyncio.Queue()

    def deliver(self, event: CompletionEvent) -> None:
        """Mutate the subscription by queueing ``event`` for the waiting request."""
        self._events.put_nowait(event)

    def requeue(self, events: Sequence[CompletionEvent]) -> None:
        """Mutate the subscription by putting ``events``, taken from it in this order, back ahead of any queued since.

        Args:
            events: Events the waiting request took from the subscription without acting on them, oldest first.
        """
        if not events:
            return
        queued_since: list[CompletionEvent] = []
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                queued_since.append(self._events.get_nowait())
        for event in (*events, *queued_since):
            self._events.put_nowait(event)

    async def wait(self, timeout: float) -> CompletionEvent | None:
        """Return the next event, waiting up to ``timeout`` seconds, or None when none arrives in time.

        Args:
            timeout: The most seconds to wait; 0 or less only takes an event already queued.
        """
        if timeout <= 0:
            with contextlib.suppress(asyncio.QueueEmpty):
                return self._events.get_nowait()
            return None
        try:
            return await asyncio.wait_for(self._events.get(), timeout)
        except TimeoutError:
            return None


class CompletionSignalListener:
    """Hold the process's Redis subscription to every chat channel and wake the registered requests.

    With no Redis client the listener never delivers anything and requests wait on their fallback poll alone.

    Concurrency:
        Registration and delivery run on the event loop, so the registry needs no lock.
    """

    def __init__(self, redis_client: redis.asyncio.Redis | None, *, liveness_seconds: float) -> None:
        """Create the listener.

        Args:
            redis_client: The client for the Redis database the Horde publishes chat signals to, or None.
            liveness_seconds: How long the subscription may receive nothing before a PING is sent, and how long its
                PONG may take before the subscription is dropped and made again.
        """
        self._redis_client = redis_client
        self._liveness_seconds = liveness_seconds
        self._subscriptions: dict[str, set[CompletionSubscription]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._subscribed = asyncio.Event()

    @property
    def subscribed(self) -> bool:
        """Return whether the Redis subscription is currently established."""
        return self._subscribed.is_set()

    async def start(self) -> None:
        """Start reading the subscription in a background task; does nothing without a Redis client."""
        if self._redis_client is None or self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(self._read_forever(self._redis_client), name="horde-oai-signal-listener")

    async def wait_until_subscribed(self, timeout: float) -> bool:
        """Return whether the subscription is established, waiting up to ``timeout`` seconds for it."""
        try:
            await asyncio.wait_for(self._subscribed.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        """Stop the background reader and wait for it to close the subscription."""
        if self._reader_task is None:
            return
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task
        self._reader_task = None

    @contextlib.contextmanager
    def register(self, request_id: str) -> Iterator[CompletionSubscription]:
        """Context manager that routes ``request_id``'s events to a new subscription until it exits.

        Args:
            request_id: The Horde request id.
        """
        subscription = CompletionSubscription(request_id)
        self._subscriptions.setdefault(request_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            request_subscriptions = self._subscriptions.get(request_id)
            if request_subscriptions is not None:
                request_subscriptions.discard(subscription)
                if not request_subscriptions:
                    del self._subscriptions[request_id]

    def dispatch(self, channel: str, payload: bytes | str) -> None:
        """Hand one published message to the subscriptions registered for its request.

        Args:
            channel: The channel the message arrived on.
            payload: The message body.
        """
        try:
            event = CompletionEvent.model_validate(json.loads(payload))
        except (ValueError, ValidationError) as error:
            logger.warning(f"Ignoring a malformed chat signal on {channel}: {error}")
            return
        for subscription in tuple(self._subscriptions.get(event.request_id, ())):
            subscription.deliver(event)

    async def _read_forever(self, redis_client: redis.asyncio.Redis) -> None:
        """Read the pattern subscription until cancelled, subscribing again after any Redis error."""
        while True:
            pubsub = redis_client.pubsub()
            try:
                await pubsub.psubscribe(CHAT_SIGNAL_CHANNEL_PATTERN)
                self._subscribed.set()
                logger.info(f"Subscribed to chat signals on {CHAT_SIGNAL_CHANNEL_PATTERN}")
                await self._read_messages(pubsub)
            except (redis.exceptions.RedisError, OSError) as error:
                logger.warning(f"Chat signal subscription failed; requests fall back to polling until it returns: {error}")
            finally:
                self._subscribed.clear()
                with contextlib.suppress(redis.exceptions.RedisError, OSError):
                    await pubsub.close()
            await asyncio.sleep(LISTENER_RECONNECT_DELAY_SECONDS)

    async def _read_messages(self, pubsub: redis.asyncio.client.PubSub) -> None:
        """Dispatch messages from ``pubsub`` until it errors, stops answering or is no longer subscribed.

        A connection a network fault black-holes delivers nothing and raises nothing, the same as a quiet one. After
        ``liveness_seconds`` with no message a PING is sent; a PONG carrying its token that does not arrive within
        ``liveness_seconds`` more marks the listener unsubscribed and raises, so the caller subscribes again.

        Raises:
            redis.exceptions.ConnectionError: The PING went unanswered, or the subscription ended.
        """
        read_timeout_seconds = min(LISTENER_READ_TIMEOUT_SECONDS, self._liveness_seconds)
        last_message_at = time.monotonic()
        ping_token: str | None = None
        ping_sent_at = -math.inf
        while pubsub.subscribed:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=read_timeout_seconds)
            now = time.monotonic()
            if message is not None:
                last_message_at = now
            if message is not None and message.get("type") == PONG_MESSAGE_TYPE:
                if _decode_text(message.get("data")) == ping_token:
                    ping_token = None
                continue
            if ping_token is not None and now - ping_sent_at >= self._liveness_seconds:
                self._subscribed.clear()
                raise redis.exceptions.ConnectionError(
                    f"The chat signal subscription did not answer a PING within {self._liveness_seconds:g}s.",
                )
            if ping_token is None and now - last_message_at >= self._liveness_seconds:
                ping_token = uuid.uuid4().hex
                ping_sent_at = now
                await pubsub.ping(ping_token)
            if message is None:
                continue
            channel = message.get("channel")
            channel_name = channel.decode("utf-8", "replace") if isinstance(channel, bytes) else str(channel)
            self.dispatch(channel_name, message.get("data", b""))
        raise redis.exceptions.ConnectionError("The chat signal subscription ended.")


class DeltaEntry(BaseModel):
    """Represents one entry of a request's delta stream: a content delta, or the marker that a generation ended."""

    model_config = ConfigDict(frozen=True)

    generation_id: str = Field(validation_alias="gen")
    """The generation the entry belongs to: one attempt at the request."""

    seq: int | None = None
    """The delta's sequence number, from 1 per generation; None on an ended marker."""

    content: str | None = None
    """The text since the generation's previous delta; None on an ended marker, empty on a delta carrying only
    reasoning."""

    reasoning_content: str | None = None
    """The reasoning since the generation's previous delta, never part of ``content``; None when the delta carries
    none."""

    worker_id: str | None = Field(default=None, validation_alias="worker")
    """The worker generating the delta; None on an ended marker and on a delta from a Horde that does not write it."""

    model: str | None = None
    """The model generating the delta; None on an ended marker and on a delta from a Horde that does not write it."""

    ended: bool = False
    """Whether the entry marks that the generation faulted and does not post more deltas."""

    appended_at: float | None = None
    """The Unix time in seconds at which the entry was appended. The relay uses it to pace later deltas. The reader sets
    it from the stream id, or from the read time when the id does not hold a time. None on an entry built elsewhere."""


class DeltaSubscription:
    """Receive the delta stream entries of one request from the reader."""

    def __init__(self, request_id: str) -> None:
        """Create the subscription.

        Args:
            request_id: The Horde request id.
        """
        self.request_id: str = request_id
        """The Horde request id."""
        self.last_entry_id: str = DELTA_STREAM_START_ID
        """The id of the last stream entry read for the request; the next read starts after it."""
        self._entries: asyncio.Queue[DeltaEntry] = asyncio.Queue()

    def deliver(self, entry: DeltaEntry) -> None:
        """Mutate the subscription by queueing ``entry`` for the streaming request."""
        self._entries.put_nowait(entry)

    async def get(self) -> DeltaEntry:
        """Return the next entry, waiting until one arrives."""
        return await self._entries.get()

    def take_queued(self) -> list[DeltaEntry]:
        """Return every entry already queued, oldest first, without waiting."""
        entries: list[DeltaEntry] = []
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                entries.append(self._entries.get_nowait())
        return entries


class DeltaStreamReader:
    """Read the delta streams of every registered request over one Redis connection and queue their entries.

    With no Redis client the reader is disabled: registering yields None and the request is delivered whole.

    Concurrency:
        Registration and delivery run on the event loop, so the registry does not need a lock.
    """

    def __init__(self, redis_client: redis.asyncio.Redis | None) -> None:
        """Create the reader.

        Args:
            redis_client: The client for the Redis database the Horde appends delta streams to, or None.
        """
        self._redis_client = redis_client
        self._subscriptions: dict[str, DeltaSubscription] = {}
        self._registered = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """Return whether the reader has a Redis client to read from."""
        return self._redis_client is not None

    async def start(self) -> None:
        """Start reading in a background task; does nothing without a Redis client."""
        if self._redis_client is None or self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(self._read_forever(self._redis_client), name="horde-oai-delta-reader")

    async def stop(self) -> None:
        """Stop the background reader."""
        if self._reader_task is None:
            return
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task
        self._reader_task = None

    @contextlib.contextmanager
    def register(self, request_id: str) -> Iterator[DeltaSubscription | None]:
        """Context manager that routes ``request_id``'s stream entries to a new subscription until it exits.

        Yields None when the reader is disabled.

        Args:
            request_id: The Horde request id; one request has at most one registration.
        """
        if self._redis_client is None:
            yield None
            return
        subscription = DeltaSubscription(request_id)
        self._subscriptions[request_id] = subscription
        self._registered.set()
        try:
            yield subscription
        finally:
            if self._subscriptions.get(request_id) is subscription:
                del self._subscriptions[request_id]

    async def read_once(self, redis_client: redis.asyncio.Redis) -> None:
        """Read the registered streams once, blocking up to the read block, and queue each entry on its subscription.

        A malformed entry is skipped with a warning; the read position still moves past it.

        Raises:
            redis.exceptions.RedisError: The read failed.
            TimeoutError: The read did not answer within the read timeout.
        """
        stream_ids = {
            f"{DELTA_STREAM_KEY_PREFIX}{request_id}": subscription.last_entry_id for request_id, subscription in self._subscriptions.items()
        }
        if not stream_ids:
            return
        replies = await asyncio.wait_for(
            redis_client.xread(
                stream_ids,
                count=DELTA_READ_COUNT,
                block=DELTA_READ_BLOCK_MILLISECONDS,
            ),
            DELTA_READ_TIMEOUT_SECONDS,
        )
        if not replies:
            return
        read_at = time.time()
        with logfire.span(DELTA_READ_SPAN_NAME) as span:
            entry_count = 0
            byte_count = 0
            oldest_appended_at: float | None = None
            for stream_key, stream_entries in replies:
                request_id = _decode_text(stream_key).removeprefix(DELTA_STREAM_KEY_PREFIX)
                if stream_entries:
                    first_appended_at = _stream_entry_appended_at(_decode_text(stream_entries[0][0]))
                    if first_appended_at is not None and (oldest_appended_at is None or first_appended_at < oldest_appended_at):
                        oldest_appended_at = first_appended_at
                subscription = self._subscriptions.get(request_id)
                for entry_id, entry_fields in stream_entries:
                    fields = {_decode_text(field): _decode_text(value) for field, value in entry_fields.items()}
                    entry_count += 1
                    byte_count += sum(len(field.encode()) + len(value.encode()) for field, value in fields.items())
                    if subscription is None:
                        continue
                    subscription.last_entry_id = _decode_text(entry_id)
                    try:
                        entry = DeltaEntry.model_validate(fields)
                    except ValidationError as error:
                        logger.warning(f"Ignoring a malformed delta of chat request {request_id}: {error}")
                        continue
                    # The read time and Redis-generated ids use the same Unix clock. The gaps between the entries of
                    # one stream are therefore comparable, whichever source each time came from.
                    appended_at = _stream_entry_appended_at(subscription.last_entry_id)
                    subscription.deliver(entry.model_copy(update={"appended_at": read_at if appended_at is None else appended_at}))
            span.set_attribute(DELTA_READ_ENTRY_COUNT_ATTRIBUTE, entry_count)
            span.set_attribute(DELTA_READ_BYTE_COUNT_ATTRIBUTE, byte_count)
            if oldest_appended_at is not None:
                span.set_attribute(DELTA_READ_FIRST_ENTRY_LAG_ATTRIBUTE, max(0.0, read_at - oldest_appended_at))

    async def _read_forever(self, redis_client: redis.asyncio.Redis) -> None:
        """Read the registered streams until cancelled, waiting while none is registered and reading again after a
        Redis error."""
        while True:
            if not self._subscriptions:
                self._registered.clear()
                await self._registered.wait()
                continue
            try:
                await self.read_once(redis_client)
            except (redis.exceptions.RedisError, OSError, TimeoutError) as error:
                logger.warning(f"Reading chat delta streams failed; streamed requests are delivered whole until it returns: {error}")
                await asyncio.sleep(LISTENER_RECONNECT_DELAY_SECONDS)


class ChatRequestWatcher:
    """Follow one queued Horde chat request until it finishes, fails fast or outlives its budget.

    Both response modes wait through this class; the streamed mode's heartbeat reads
    [`latest_status`][horde.oai.waiter.ChatRequestWatcher.latest_status].
    """

    def __init__(
        self,
        *,
        horde_client: HordeClient,
        credentials: HordeCredentials,
        subscription: CompletionSubscription,
        budget: WaitBudget,
        front_metrics: FrontMetrics,
        availability_mode: ChatAvailabilityMode = ChatAvailabilityMode.WAIT,
        clock: Callable[[], float] = time.monotonic,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        """Create the watcher.

        Args:
            horde_client: The Horde client.
            credentials: Who the status calls are made for.
            subscription: The request's registration with the completion listener.
            budget: The deadline and the intervals of the wait.
            front_metrics: Where each wake is recorded.
            availability_mode: Whether the first successful status read may refuse the request for the online pool.
            clock: The monotonic clock the deadline is measured on.
            random_fraction: Return a number in ``[0, 1)`` for the poll jitter.
        """
        self.request_id: str = subscription.request_id
        """The Horde request id."""
        self.latest_status: HordeChatStatus | None = None
        """The last status read, or None before the first successful read."""
        self.woke_by: WakeSource | None = None
        """What woke the request when it was found finished."""
        self.woke_at: float | None = None
        """The wall-clock time the request was found finished, in Unix seconds."""
        self.wake_count: int = 0
        """How many status reads the wait made, the read that found the request finished included."""
        self.done_without_generation_reads: int = 0
        """How many status reads showed the request done, not faulted and without a finished generation."""
        self.first_read_status: HordeChatStatus | None = None
        """The status that took a ``fail_fast`` request's first-read decision, whether it admitted or refused the
        request; None in the ``wait`` mode and before the decision."""
        self._front_metrics = front_metrics
        self._horde_client = horde_client
        self._credentials = credentials
        self._subscription = subscription
        self._budget = budget
        self._availability_mode = availability_mode
        self._first_read_checked = False
        self._admission_started = False
        self._admission_status: HordeChatStatus | None = None
        self._positioned_read_due_at: float = math.inf
        self._positioned_reread_at: float = math.inf
        self._impossible_since: float | None = None
        self._fault_signal: CompletionEvent | None = None
        self._clock = clock
        self._random_fraction = random_fraction
        self._next_poll_at: float = clock()
        self._recheck_at: float | None = None
        self._recheck_earned = False
        self._recheck_for_done_without_generation = False
        self._cancel_signal_seen = False
        self._done_without_generation_settled = False
        self._last_read_at: float | None = None
        self._fail_fast_retry_at: float = -math.inf

    async def wait_for_admission(self) -> None:
        """Read the status right after the submit and wait until the first-read decision is taken.

        Returns once a status passed the first-read checks, or showed the request finished or done without a
        generation, or once a read failed or a completion signal arrived before a ``fail_fast`` request read a status
        with queue information; [`wait_until_finished`][horde.oai.waiter.ChatRequestWatcher.wait_until_finished]
        continues from there without reading again. A second call returns at once.

        Raises:
            NoEligibleWorkerError: As its subclass ``DeclarationGapError``, the status reports a declaration gap.
            QueueUnavailableError: In the ``fail_fast`` mode, the deciding status showed zero eligible worker threads
                or a wait estimate over the time left, with nothing processing.
            HordeAPIError: A status read failed in a way waiting cannot fix, such as 404.
        """
        if self._admission_started:
            return
        self._admission_started = True
        self._positioned_read_due_at = self._clock() + POSITIONED_READ_WAIT_SECONDS
        status = await self._read_before_deadline(WakeSource.IMMEDIATE)
        self._admission_status = status
        while status is not None and not _is_finished(status) and not self._done_without_generation_settled:
            self._check_fail_fast(status)
            self._check_first_read(status)
            if self._first_read_checked:
                return
            event = await self._subscription.wait(min(self._positioned_reread_at, self._budget.deadline) - self._clock())
            if event is not None:
                # The main loop acts on the signal and the read it earns; the decision then follows that read.
                self._subscription.requeue([event])
                return
            self._positioned_reread_at = math.inf
            status = await self._read_before_deadline(WakeSource.POLL)
            self._admission_status = status

    async def wait_until_finished(self) -> HordeChatStatus:
        """Return the request's terminal status once it is done or faulted.

        Runs [`wait_for_admission`][horde.oai.waiter.ChatRequestWatcher.wait_for_admission] first unless it already ran.
        A status observed terminal at or after the deadline is returned, never raised. A request whose ``faulted``
        signal is followed by a 404 status read expired on the Horde and is returned as faulted.

        Raises:
            ChatWaitTimeoutError: The request was still waiting after the deadline and the final status read.
            ChatDoneWithoutGenerationError: The request settled done, not faulted and without a finished generation.
            NoEligibleWorkerError: No eligible worker, with nothing processing, for longer than the grace, or at once
                as its subclass ``DeclarationGapError`` when the status reports a declaration gap.
            QueueUnavailableError: In the ``fail_fast`` mode, the deciding status read showed zero eligible worker
                threads or a wait estimate over the time left, with nothing processing.
            HordeAPIError: A status read before the deadline failed in a way waiting cannot fix, such as 404.
        """
        await self.wait_for_admission()
        status = self._admission_status
        while True:
            if status is not None and _is_finished(status):
                return status
            if self._done_without_generation_settled:
                raise ChatDoneWithoutGenerationError(
                    f"Chat request {self.request_id} is done without a generation and not faulted.",
                )
            if status is not None:
                self._check_fail_fast(status)
                self._check_first_read(status)
            now = self._clock()
            if now >= self._budget.deadline:
                break
            event = await self._subscription.wait(self._next_wake_at() - now)
            if event is not None:
                if event.event in FINISHING_EVENT_KINDS:
                    self._recheck_earned = True
                if event.event == CompletionEventKind.FAULTED:
                    self._fault_signal = event
                if event.event == CompletionEventKind.CANCELLED:
                    self._cancel_signal_seen = True
                status = await self._read_before_deadline(WakeSource.SIGNAL)
                continue
            now = self._clock()
            if now >= self._budget.deadline:
                break
            if self._recheck_at is not None and now >= self._recheck_at:
                self._recheck_at = None
                status = await self._read_before_deadline(WakeSource.RECHECK)
            elif now >= self._next_poll_at or now >= self._fail_fast_read_due_at() or now >= self._positioned_reread_at:
                # A failed read does not reschedule the positioned re-read, so a Horde that keeps failing cannot spin
                # the loop; the next successful read decides once the bound has passed.
                self._positioned_reread_at = math.inf
                status = await self._read_before_deadline(WakeSource.POLL)
        return await self._read_final_status()

    def _next_wake_at(self) -> float:
        """Return the ``clock`` time of the earliest scheduled wake."""
        return min(
            self._next_poll_at,
            self._budget.deadline,
            self._recheck_at if self._recheck_at is not None else math.inf,
            self._fail_fast_read_due_at(),
            self._positioned_reread_at,
        )

    def _fail_fast_read_due_at(self) -> float:
        """Return the ``clock`` time of the read that decides fail-fast, or infinity once a read after the grace ran.

        One read after the grace is enough: later reads are ordinary polls, so failing reads cannot spin the loop.
        """
        fail_fast_due_at = self._fail_fast_due_at()
        if self._last_read_at is not None and self._last_read_at >= fail_fast_due_at:
            return math.inf
        return max(fail_fast_due_at, self._fail_fast_retry_at)

    def _fail_fast_due_at(self) -> float:
        """Return the ``clock`` time the fail-fast grace ends, or infinity while a worker is eligible."""
        if self._impossible_since is None:
            return math.inf
        return self._impossible_since + self._budget.fail_fast_grace_seconds

    def _check_fail_fast(self, status: HordeChatStatus) -> None:
        """Mutate the fail-fast clock from ``status`` and raise once the grace has passed.

        A grace rescue is counted when a read shows an eligible worker or the request processing after an earlier
        read showed neither. Without the grace, the front would have refused that request. Compared with the
        ``no_eligible_worker`` errors at the ``grace`` stage, the count shows how many refusals the grace prevents.

        Raises:
            DeclarationGapError: No eligible worker, nothing processing, and the status reports a declaration gap.
            NoEligibleWorkerError: No eligible worker and nothing processing for at least the grace.
        """
        if status.is_possible or status.processing > 0:
            if self._impossible_since is not None:
                self._front_metrics.record_grace_rescue()
            self._impossible_since = None
            return
        if status.impossible_reason is not None:
            raise DeclarationGapError(
                f"No online worker declares what chat request {self.request_id} needs.",
                status=status,
                impossible_reason=status.impossible_reason,
            )
        now = self._clock()
        if self._impossible_since is None:
            self._impossible_since = now
        if now >= self._fail_fast_due_at():
            raise NoEligibleWorkerError(
                f"No worker is eligible for chat request {self.request_id} and none is processing it.",
                status=status,
            )

    def _check_first_read(self, status: HordeChatStatus) -> None:
        """Mutate the watcher to mark the first successful read seen and, in the ``fail_fast`` mode, raise when that
        read shows the pool unable to start the request within its budget.

        Only the first read decides, so a request admitted at submission is not refused later on an estimate that
        moved. A request with a generation processing or finished is not refused: a worker already holds it. A thread
        count of None, from a Horde that does not report it, does not refuse.

        In the ``fail_fast`` mode a status without queue information does not decide the wait rule until
        [`POSITIONED_READ_WAIT_SECONDS`][horde.oai.waiter.POSITIONED_READ_WAIT_SECONDS] after the first read: it
        schedules a re-read instead, and the first status that carries the information, or the latest once the bound
        has passed, decides. With zero eligible worker threads the request does not need a position and is refused on the first read.

        Raises:
            QueueUnavailableError: The pool has zero eligible worker threads, or the wait estimate exceeds the time
                left in the budget.
        """
        if self._first_read_checked:
            return
        is_fail_fast = self._availability_mode == ChatAvailabilityMode.FAIL_FAST
        if is_fail_fast and self._awaits_positioned_read(status):
            self._positioned_reread_at = min(self._clock() + self._budget.signal_recheck_seconds, self._positioned_read_due_at)
            return
        self._first_read_checked = True
        self._positioned_reread_at = math.inf
        if not is_fail_fast:
            return
        self.first_read_status = status
        if status.processing > 0 or status.finished > 0:
            return
        remaining_seconds = self._budget.deadline - self._clock()
        if _has_zero_eligible_threads(status):
            raise QueueUnavailableError(
                f"The pool has zero eligible worker threads for chat request {self.request_id} and nothing is processing it.",
                status=status,
                reason=QueueUnavailableReason.NO_ELIGIBLE_THREADS,
                remaining_seconds=remaining_seconds,
            )
        if status.wait_time is not None and status.wait_time > remaining_seconds:
            raise QueueUnavailableError(
                f"The estimated wait of chat request {self.request_id}, {status.wait_time} s, exceeds the "
                f"{remaining_seconds:.0f} s left in its budget.",
                status=status,
                reason=QueueUnavailableReason.WAIT_EXCEEDS_BUDGET,
                remaining_seconds=remaining_seconds,
            )

    def _awaits_positioned_read(self, status: HordeChatStatus) -> bool:
        """Return whether the ``fail_fast`` decision waits for a later read because ``status`` carries no queue
        information and the bound has not passed."""
        if _has_zero_eligible_threads(status) or _carries_queue_information(status):
            return False
        return self._clock() < self._positioned_read_due_at

    async def _read_before_deadline(self, wake_source: WakeSource) -> HordeChatStatus | None:
        """Return a status read bounded by the time left before the deadline, or None when the read did not finish.

        A read that finishes is returned whatever the clock shows by then.

        Raises:
            HordeAPIError: The Horde answered with a status waiting cannot fix.
        """
        remaining = self._budget.deadline - self._clock()
        if remaining <= 0:
            return None
        try:
            async with asyncio.timeout(remaining):
                status = await self._refresh(wake_source)
        except TimeoutError:
            status = None
        if status is None:
            # Retry the deciding read after a bounded pause; stale observations cannot cancel live work.
            self._fail_fast_retry_at = self._clock() + self._budget.signal_recheck_seconds
        return status

    async def _read_final_status(self) -> HordeChatStatus:
        """Return the terminal status from one read under the final allowance.

        Raises:
            ChatWaitTimeoutError: The read failed, did not finish in its allowance, or showed the request waiting.
        """
        status: HordeChatStatus | None = None
        try:
            async with asyncio.timeout(self._budget.final_status_timeout_seconds):
                status = await self._refresh(WakeSource.DEADLINE)
        except TimeoutError:
            logger.warning(f"The final status read of chat request {self.request_id} did not finish in its allowance")
        except HordeAPIError as error:
            logger.warning(f"The final status read of chat request {self.request_id} failed: {error}")
        if status is not None and _is_finished(status):
            return status
        if status is not None and _is_settled_without_generation(status):
            raise ChatDoneWithoutGenerationError(
                f"Chat request {self.request_id} was still reported done without a generation at its deadline.",
            )
        raise ChatWaitTimeoutError(f"Chat request {self.request_id} was still waiting at its deadline.")

    async def _refresh(self, wake_source: WakeSource) -> HordeChatStatus | None:
        """Return a freshly read status, or None when the read failed in a way the next poll may fix; record the wake.

        Raises:
            HordeAPIError: The Horde answered with a status waiting cannot fix.
        """
        self.wake_count += 1
        found_finished = False
        found_done_without_generation = False
        try:
            status = await self._read_status(wake_source)
            found_finished = status is not None and _is_finished(status)
            found_done_without_generation = status is not None and _is_done_without_generation(status)
            return status
        finally:
            self._front_metrics.record_wake(
                wake_source=wake_source,
                found_finished=found_finished,
                found_done_without_generation=found_done_without_generation,
            )

    async def _read_status(self, wake_source: WakeSource) -> HordeChatStatus | None:
        """Return a freshly read status, or None when the read failed in a way the next poll may fix.

        Raises:
            HordeAPIError: The Horde answered with a status waiting cannot fix.
        """
        read_started_at = self._clock()
        self._next_poll_at = read_started_at + jitter_poll_seconds(self._budget.fallback_poll_seconds, self._random_fraction)
        try:
            status = await self._horde_client.get_chat_status(request_id=self.request_id, credentials=self._credentials)
        except HordeAPIError as error:
            if error.status_code == HTTPStatus.NOT_FOUND and self.latest_status is not None and self._fault_signal is None:
                await self._reconcile_missing_status()
            if error.status_code == HTTPStatus.NOT_FOUND and self._fault_signal is not None:
                # The cleanup thread deletes an expired request and then publishes faulted, so the read finds no row.
                logger.info(f"Chat request {self.request_id} expired: faulted signal and no status row")
                status = HordeChatStatus(
                    faulted=True,
                    fault_reason=self._fault_signal.fault_reason,
                    fault_rc=self._fault_signal.fault_rc,
                )
                self.latest_status = status
                self.woke_by = wake_source
                self.woke_at = time.time()
                return status
            if error.status_code not in TRANSIENT_STATUS_CODES:
                raise
            logger.warning(f"Status of chat request {self.request_id} unavailable, retrying at the next poll: {error}")
            return None
        except (HordeUnavailableError, httpx.TransportError) as error:
            logger.warning(f"Status of chat request {self.request_id} unreachable, retrying at the next poll: {error}")
            return None
        self._last_read_at = read_started_at
        self.latest_status = status
        is_recheck_of_done_without_generation = wake_source == WakeSource.RECHECK and self._recheck_for_done_without_generation
        if wake_source == WakeSource.RECHECK:
            self._recheck_for_done_without_generation = False
        stretched_poll_seconds = compute_fallback_poll_seconds(base_seconds=self._budget.fallback_poll_seconds, wait_time=status.wait_time)
        self._next_poll_at = read_started_at + jitter_poll_seconds(stretched_poll_seconds, self._random_fraction)
        if _is_done_without_generation(status):
            self.done_without_generation_reads += 1
        if _is_finished(status):
            self.woke_by = wake_source
            self.woke_at = time.time()
        elif _is_settled_without_generation(status):
            self._note_done_without_generation(wake_source, is_recheck_of_this_state=is_recheck_of_done_without_generation)
        elif self._recheck_earned:
            self._recheck_earned = False
            self._recheck_at = self._clock() + self._budget.signal_recheck_seconds
        return status

    def _note_done_without_generation(self, wake_source: WakeSource, *, is_recheck_of_this_state: bool) -> None:
        """Mutate the wait for a read showing the request done without a generation, not faulted and with no job left.

        A Horde in the middle of a fault and its restart can show this for a moment, so the first sighting earns a
        short recheck. The state is settled when the recheck still shows it, or when a ``cancelled`` signal came: a
        request cancelled with no generation stays done without one.

        Args:
            wake_source: What caused the read.
            is_recheck_of_this_state: Whether the read is the recheck an earlier sighting of this state scheduled.
        """
        self._recheck_earned = False
        if self._cancel_signal_seen or is_recheck_of_this_state:
            self._done_without_generation_settled = True
            self.woke_by = wake_source
            self.woke_at = time.time()
            return
        self._recheck_for_done_without_generation = True
        self._recheck_at = self._clock() + self._budget.signal_recheck_seconds

    async def _reconcile_missing_status(self) -> None:
        """Wait once for expiry's post-commit signal after a previously visible row disappears.

        Storage cleanup happens between deletion and publication. An initial unknown id stays a 404, and a missing
        row without a fault signal stays a 404 after this bounded allowance. The enclosing read budget still applies.
        Other events taken from the subscription while waiting are queued again in their order, so the main loop
        still sees them.
        """
        reconcile_until = self._clock() + self._budget.signal_recheck_seconds
        other_events: list[CompletionEvent] = []
        try:
            while self._fault_signal is None:
                remaining = reconcile_until - self._clock()
                if remaining <= 0:
                    return
                event = await self._subscription.wait(remaining)
                if event is None:
                    return
                if event.event == CompletionEventKind.FAULTED:
                    self._fault_signal = event
                else:
                    other_events.append(event)
        finally:
            self._subscription.requeue(other_events)


def compute_fallback_poll_seconds(*, base_seconds: float, wait_time: int | None) -> float:
    """Return the fallback poll interval for a request whose latest status estimates ``wait_time`` seconds.

    The interval is ``max(base_seconds, min(MAX_STRETCHED_POLL_SECONDS, wait_time / WAIT_TIME_POLL_DIVISOR))``: never
    shorter than the configured poll, and stretched for a request the Horde expects to wait long.

    Args:
        base_seconds: The configured fallback poll interval.
        wait_time: The Horde's wait estimate in seconds, or None when it reported none.
    """
    if wait_time is None or wait_time <= 0:
        return base_seconds
    return max(base_seconds, min(MAX_STRETCHED_POLL_SECONDS, wait_time / WAIT_TIME_POLL_DIVISOR))


def jitter_poll_seconds(poll_seconds: float, random_fraction: Callable[[], float] = random.random) -> float:
    """Return ``poll_seconds`` shortened by up to [`POLL_JITTER_FRACTION`][horde.oai.waiter.POLL_JITTER_FRACTION].

    Args:
        poll_seconds: The interval before jitter.
        random_fraction: Return a number in ``[0, 1)``.
    """
    return poll_seconds * (1 - POLL_JITTER_FRACTION * random_fraction())


def _decode_text(text: object) -> str:
    """Return a pub/sub field as text, whether the client decodes responses or not."""
    return text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)


def _stream_entry_appended_at(entry_id: str) -> float | None:
    """Return the Unix time in seconds a stream entry was appended, from its ``<milliseconds>-<sequence>`` id.

    Returns None for an id Redis did not generate, which does not carry a time.
    """
    milliseconds, _, _ = entry_id.partition("-")
    try:
        return int(milliseconds) / STREAM_ID_MILLISECONDS_PER_SECOND
    except ValueError:
        return None


def _is_finished(status: HordeChatStatus) -> bool:
    """Return whether ``status`` is terminal: faulted, or done with a finished generation to deliver."""
    has_generation = len(status.generations) > 0
    return status.faulted or (status.done and has_generation)


def _is_done_without_generation(status: HordeChatStatus) -> bool:
    """Return whether ``status`` is done, not faulted and without a finished generation, which is not terminal.

    A request whose every job faulted has nothing to deliver until the Horde restarts the job or faults the request.
    """
    has_generation = len(status.generations) > 0
    return status.done and not status.faulted and not has_generation


def _is_settled_without_generation(status: HordeChatStatus) -> bool:
    """Return whether ``status`` is done without a generation, not faulted, and has no job waiting or processing.

    Only such a status can settle the wait. One that still reports a job waiting or processing contradicts its own
    ``done``, as a Horde counting faulted generations as finished reported during a third attempt, so the wait goes
    on as for any unfinished request.
    """
    has_outstanding_job = status.processing > 0 or status.waiting > 0
    return _is_done_without_generation(status) and not has_outstanding_job


def _has_zero_eligible_threads(status: HordeChatStatus) -> bool:
    """Return whether ``status`` reports zero eligible worker threads without a declaration gap as the cause."""
    return status.eligible_worker_threads == 0 and status.impossible_reason is None


def _carries_queue_information(status: HordeChatStatus) -> bool:
    """Return whether ``status`` reflects the request's place in the queue: a position or wait above zero, or a
    generation processing or finished.

    A request the Horde's cached queue positions do not yet hold reads a position and wait of zero.
    """
    is_positioned = status.queue_position is not None and status.queue_position > 0
    has_wait_estimate = status.wait_time is not None and status.wait_time > 0
    return is_positioned or has_wait_estimate or status.processing > 0 or status.finished > 0
