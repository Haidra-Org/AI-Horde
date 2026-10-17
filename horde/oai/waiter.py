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

[`ChatRequestWatcher`][horde.oai.waiter.ChatRequestWatcher] is the one wait implementation both response modes use.
After the submit it reads the status once (the request may have finished before the registration), then sleeps until
a signal, the jittered fallback poll, the fail-fast check or the deadline, and reads the status once on each wake.
A terminal signal whose outcome the next read does not yet show earns one short recheck. The poll covers a lost
message and a Redis outage; its interval stretches with the request's reported wait (see
[`compute_fallback_poll_seconds`][horde.oai.waiter.compute_fallback_poll_seconds]).

The deadline bounds the wait, not the outcome: a status read that returns a terminal outcome is returned even when it
completes after the deadline. At the deadline one final read runs under its own allowance; only a request still
waiting after it raises [`ChatWaitTimeoutError`][horde.oai.waiter.ChatWaitTimeoutError].
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
import redis.asyncio
import redis.exceptions
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from horde.oai.horde_client import HordeAPIError, HordeChatStatus, HordeClient, HordeCredentials, HordeUnavailableError

__all__ = [
    "CHAT_SIGNAL_CHANNEL_PREFIX",
    "MAX_STRETCHED_POLL_SECONDS",
    "ChatRequestWatcher",
    "ChatWaitTimeoutError",
    "CompletionEvent",
    "CompletionEventKind",
    "CompletionSignalListener",
    "CompletionSubscription",
    "NoEligibleWorkerError",
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
    """What woke a waiting request when it found the request finished."""

    IMMEDIATE = "immediate"
    SIGNAL = "signal"
    RECHECK = "recheck"
    POLL = "poll"
    DEADLINE = "deadline"


class CompletionEvent(BaseModel):
    """Represents one completion signal message."""

    model_config = ConfigDict(frozen=True)

    request_id: str = Field(validation_alias="id")
    """The Horde request id."""

    event: CompletionEventKind
    """What happened to the request."""


class ChatWaitTimeoutError(Exception):
    """The request was still waiting after its deadline and the final status read."""


class NoEligibleWorkerError(Exception):
    """The Horde reported no eligible worker, with nothing processing, for longer than the fail-fast grace."""

    def __init__(self, message: str, *, status: HordeChatStatus | None) -> None:
        """Create the error.

        Args:
            message: What happened.
            status: The last status read, whose ``wait_time`` hints when to retry.
        """
        super().__init__(message)
        self.status: HordeChatStatus | None = status
        """The last status read."""


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
        clock: Callable[[], float] = time.monotonic,
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        """Create the watcher.

        Args:
            horde_client: The Horde client.
            credentials: Who the status calls are made for.
            subscription: The request's registration with the completion listener.
            budget: The deadline and the intervals of the wait.
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
        self._horde_client = horde_client
        self._credentials = credentials
        self._subscription = subscription
        self._budget = budget
        self._impossible_since: float | None = None
        self._faulted_signal_seen = False
        self._clock = clock
        self._random_fraction = random_fraction
        self._next_poll_at: float = clock()
        self._recheck_at: float | None = None
        self._recheck_earned = False
        self._last_read_at: float | None = None
        self._fail_fast_retry_at: float = -math.inf

    async def wait_until_finished(self) -> HordeChatStatus:
        """Return the request's terminal status once it is done or faulted.

        A status observed terminal at or after the deadline is returned, never raised. A request whose ``faulted`` signal
        is followed by a 404 status read expired on the Horde and is returned as faulted.

        Raises:
            ChatWaitTimeoutError: The request was still waiting after the deadline and the final status read.
            NoEligibleWorkerError: No eligible worker, with nothing processing, for longer than the grace.
            HordeAPIError: A status read before the deadline failed in a way waiting cannot fix, such as 404.
        """
        status = await self._read_before_deadline(WakeSource.IMMEDIATE)
        while True:
            if status is not None and _is_finished(status):
                return status
            if status is not None:
                self._check_fail_fast(status)
            now = self._clock()
            if now >= self._budget.deadline:
                break
            event = await self._subscription.wait(self._next_wake_at() - now)
            if event is not None:
                if event.event in FINISHING_EVENT_KINDS:
                    self._recheck_earned = True
                if event.event == CompletionEventKind.FAULTED:
                    self._faulted_signal_seen = True
                status = await self._read_before_deadline(WakeSource.SIGNAL)
                continue
            now = self._clock()
            if now >= self._budget.deadline:
                break
            if self._recheck_at is not None and now >= self._recheck_at:
                self._recheck_at = None
                status = await self._read_before_deadline(WakeSource.RECHECK)
            elif now >= self._next_poll_at or now >= self._fail_fast_read_due_at():
                status = await self._read_before_deadline(WakeSource.POLL)
        return await self._read_final_status()

    def _next_wake_at(self) -> float:
        """Return the ``clock`` time of the earliest scheduled wake."""
        return min(
            self._next_poll_at,
            self._budget.deadline,
            self._recheck_at if self._recheck_at is not None else math.inf,
            self._fail_fast_read_due_at(),
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

        Raises:
            NoEligibleWorkerError: No eligible worker and nothing processing for at least the grace.
        """
        if status.is_possible or status.processing > 0:
            self._impossible_since = None
            return
        now = self._clock()
        if self._impossible_since is None:
            self._impossible_since = now
        if now >= self._fail_fast_due_at():
            raise NoEligibleWorkerError(
                f"No worker is eligible for chat request {self.request_id} and none is processing it.",
                status=status,
            )

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
        raise ChatWaitTimeoutError(f"Chat request {self.request_id} was still waiting at its deadline.")

    async def _refresh(self, wake_source: WakeSource) -> HordeChatStatus | None:
        """Return a freshly read status, or None when the read failed in a way the next poll may fix.

        Raises:
            HordeAPIError: The Horde answered with a status waiting cannot fix.
        """
        read_started_at = self._clock()
        self._next_poll_at = read_started_at + jitter_poll_seconds(self._budget.fallback_poll_seconds, self._random_fraction)
        try:
            status = await self._horde_client.get_chat_status(request_id=self.request_id, credentials=self._credentials)
        except HordeAPIError as error:
            if error.status_code == HTTPStatus.NOT_FOUND and self.latest_status is not None and not self._faulted_signal_seen:
                await self._reconcile_missing_status()
            if error.status_code == HTTPStatus.NOT_FOUND and self._faulted_signal_seen:
                # The cleanup thread deletes an expired request and then publishes faulted, so the read finds no row.
                logger.info(f"Chat request {self.request_id} expired: faulted signal and no status row")
                status = HordeChatStatus(faulted=True)
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
        stretched_poll_seconds = compute_fallback_poll_seconds(base_seconds=self._budget.fallback_poll_seconds, wait_time=status.wait_time)
        self._next_poll_at = read_started_at + jitter_poll_seconds(stretched_poll_seconds, self._random_fraction)
        if _is_finished(status):
            self.woke_by = wake_source
            self.woke_at = time.time()
        elif self._recheck_earned:
            self._recheck_earned = False
            self._recheck_at = self._clock() + self._budget.signal_recheck_seconds
        return status

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
            while not self._faulted_signal_seen:
                remaining = reconcile_until - self._clock()
                if remaining <= 0:
                    return
                event = await self._subscription.wait(remaining)
                if event is None:
                    return
                if event.event == CompletionEventKind.FAULTED:
                    self._faulted_signal_seen = True
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


def _is_finished(status: HordeChatStatus) -> bool:
    """Return whether ``status`` is terminal."""
    return status.done or status.faulted
