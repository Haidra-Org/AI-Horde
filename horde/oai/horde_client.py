# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Call the Horde's native chat and model routes over HTTP.

[`HordeClient`][horde.oai.horde_client.HordeClient] wraps shared ``httpx.AsyncClient`` instances, normally made by
[`create_http_client`][horde.oai.horde_client.create_http_client]. Calls rotate across the configured Horde API roots.
A root that refuses the connection is skipped for that call, which is safe because the request never reached it, and
any root that refuses or drops a connection is left out of the rotation for
[`UNHEALTHY_URL_BACKOFF_SECONDS`][horde.oai.horde_client.UNHEALTHY_URL_BACKOFF_SECONDS]. A Horde error response raises
[`HordeAPIError`][horde.oai.horde_client.HordeAPIError] carrying the status, the Horde ``rc`` and the rate-limit
headers; no root answering raises [`HordeUnavailableError`][horde.oai.horde_client.HordeUnavailableError], whose
``request_sent`` says whether the call may have reached a Horde. A submit sent again to recover a lost answer may fall
back to roots left out of the rotation, since waiting out their backoff would spend the request's budget.

Submits, status reads and model lists share one pool whose size the caller sets (``HORDE_OAI_NATIVE_MAX_CONNECTIONS``).
Submits wait on a semaphore of their own ([`SUBMIT_MAX_CONCURRENT_CALLS`][horde.oai.horde_client.SUBMIT_MAX_CONCURRENT_CALLS])
and status reads and model lists on another, each in arrival order, so a queue of slow status reads cannot hold back a
new request's submit. Cancels use a second, smaller pool and semaphore
([`cancel_max_connections`][horde.oai.horde_client.cancel_max_connections]), so a burst of slow cancels cannot starve
submits and a queue of status reads cannot delay a cancel.

The Horde response models here are parsed leniently (unknown fields ignored), so a Horde that adds status fields does
not break the front.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidatorFunctionWrapHandler, field_validator

from horde.chat_contract import ChatImpossibleReason, ChatToolCall
from horde.oai.metrics import FrontMetrics, NativeCallOutcome, NativeCallType

__all__ = [
    "CANCEL_MIN_CONNECTIONS",
    "HTTP_MAX_CONNECTIONS",
    "INFLIGHT_REQUESTS_PER_CANCEL_CONNECTION",
    "SUBMIT_MAX_CONCURRENT_CALLS",
    "CancelSlotUnavailableError",
    "HordeAPIError",
    "HordeActiveModel",
    "HordeAsyncSubmission",
    "HordeChatGeneration",
    "HordeChatMessage",
    "HordeChatModelOffer",
    "HordeChatStatus",
    "HordeClient",
    "HordeCredentials",
    "HordeUnavailableError",
    "cancel_max_connections",
    "create_http_client",
]

CHAT_SUBMIT_PATH: str = "/v2/generate/chat/async"
"""The Horde route that queues a chat request."""

CHAT_STATUS_PATH_TEMPLATE: str = "/v2/generate/chat/status/{request_id}"
"""The Horde route that reports (GET) or cancels (DELETE) a chat request."""

MODELS_PATH: str = "/v2/status/models"
"""The Horde route that lists active models."""

CHAT_MODELS_QUERY: Mapping[str, str] = {
    "type": "text",
    "chat": "true",
}
"""The model list query that returns only models with online chat workers, each with its ``chat`` object."""

APIKEY_HEADER: str = "apikey"
"""The header the Horde reads the API key from."""

CLIENT_AGENT_HEADER: str = "Client-Agent"
"""The header the Horde reads the client's name and version from."""

PROXY_AUTHORIZATION_HEADER: str = "Proxy-Authorization"
"""The header carrying a proxy passkey; the Horde trusts ``Proxied-For`` only when it holds a known passkey."""

PROXIED_FOR_HEADER: str = "Proxied-For"
"""The header carrying the end client's address for a trusted proxy."""

FORWARDED_RESPONSE_HEADERS: tuple[str, ...] = ("retry-after",)
"""Horde response headers an error passes back to the client, besides every ``x-ratelimit-*`` header."""

RATE_LIMIT_HEADER_PREFIX: str = "x-ratelimit-"
"""The prefix of the rate-limit headers an error passes back to the client."""

MAX_ERROR_BODY_CHARACTERS: int = 500
"""The most characters of a non-JSON Horde error body kept in the message, such as an HTML gateway page."""

UNEXPECTED_RESPONSE_CODE: str = "HordeUnexpectedResponse"
"""The error code for a successful Horde response whose body could not be parsed."""

HTTP_MAX_CONNECTIONS: int = 200
"""The default for the most simultaneous calls to the Horde, and the size of the connection pool.

[`HordeClient`][horde.oai.horde_client.HordeClient] admits this many calls at once through its submit semaphore and its
status read semaphore together, so the pool never queues a request. httpcore rescans every in-flight request against every pooled connection
on each pool event, probing each idle connection's socket, so the cost of one event grows with the square of this
number. The number also bounds how many slow calls can be in flight at once: with too few slots, submits queue behind
slow status reads until clients give up.
"""

SUBMIT_MAX_CONCURRENT_CALLS: int = 32
"""The most simultaneous submits, carved out of the main pool so status reads cannot take every connection.

Every waiting request reads its status once per poll interval, so status reads far outnumber submits; when the Horde
slows they fill every slot and a new request's submit would wait behind them until its client gives up. A share of the
main pool reserved for submits keeps admission moving without enlarging the pool, whose per-event cost grows with the
square of its size. A submit runs moderation and kudos checks and usually answers within a second, so 32 at once is
several times the submit rate of a busy front.
"""

CANCEL_MIN_CONNECTIONS: int = 16
"""The fewest simultaneous cancels, and the smallest size of their separate connection pool.

Cancels are rare next to status reads, but each can take its whole budget when the Horde is slow; a pool of their own
keeps them from holding the slots submits need, and keeps them moving when status reads fill the main pool.
"""

INFLIGHT_REQUESTS_PER_CANCEL_CONNECTION: int = 20
"""How many open requests share one cancel slot once the in-flight cap is large enough to raise the cancel pool."""

HTTP_KEEPALIVE_EXPIRY_SECONDS: float = 20.0
"""How long an idle pooled connection is kept before the front closes it.

Well under waitress's 120 second ``channel_timeout``, so the front closes idle connections itself rather than having
the server drop them under a request about to be sent.
"""

HTTP_CONNECT_TIMEOUT_SECONDS: float = 5.0
"""How long a Horde call waits for a connection before the root counts as unreachable."""

HTTP_READ_TIMEOUT_SECONDS: float = 30.0
"""How long a Horde call waits for its response; a submit runs moderation and kudos checks, so it is not instant."""

UNHEALTHY_URL_BACKOFF_SECONDS: float = 3.0
"""How long a Horde root that refused or dropped a connection is left out of the rotation.

Without it, every call that rotates onto a down or overloaded root opens a fresh connection to it at once, and the
connect-and-drop churn exhausts the server's descriptors faster than it can recover.
"""


def cancel_max_connections(max_inflight: int) -> int:
    """Return the most simultaneous cancels, and the cancel pool size, for an in-flight cap of ``max_inflight``.

    Requests admitted together while the Horde stalls reach their deadline together and are cancelled in one wave, so
    the cancel pool grows with the cap: one slot per
    [`INFLIGHT_REQUESTS_PER_CANCEL_CONNECTION`][horde.oai.horde_client.INFLIGHT_REQUESTS_PER_CANCEL_CONNECTION] open
    requests, and never fewer than [`CANCEL_MIN_CONNECTIONS`][horde.oai.horde_client.CANCEL_MIN_CONNECTIONS]. A pool sized
    for every open request would cost the quadratic per-event scan the main pool avoids; a wave larger than the pool
    waits its turn, and a cancel that does not start in time is retried later.

    Args:
        max_inflight: The front's in-flight cap, ``HORDE_OAI_MAX_INFLIGHT``.
    """
    return max(CANCEL_MIN_CONNECTIONS, max_inflight // INFLIGHT_REQUESTS_PER_CANCEL_CONNECTION)


def create_http_client(max_connections: int = HTTP_MAX_CONNECTIONS) -> httpx.AsyncClient:
    """Create an HTTP client for Horde calls.

    Args:
        max_connections: The pool size; keep it equal to the semaphores the calls through this client wait on, together.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_READ_TIMEOUT_SECONDS, connect=HTTP_CONNECT_TIMEOUT_SECONDS),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=HTTP_KEEPALIVE_EXPIRY_SECONDS,
        ),
    )


class HordeCredentials(BaseModel):
    """Represents the identity a Horde call is made with on behalf of one client request."""

    model_config = ConfigDict(frozen=True)

    apikey: str = Field(repr=False)
    """The client's Horde API key, or the anonymous key; left out of the representation so it is never logged."""

    client_agent: str
    """The ``Client-Agent`` value, from [`build_client_agent`][horde.oai.attribution.build_client_agent]."""

    client_address: str | None = None
    """The client's address, sent as ``Proxied-For`` when the front has a proxy passkey."""


class HordeAPIError(Exception):
    """The Horde answered a call with an error status."""

    def __init__(
        self,
        *,
        status_code: int,
        return_code: str | None,
        message: str,
        forwarded_headers: Mapping[str, str],
        field_errors: Mapping[str, str] | None = None,
    ) -> None:
        """Create the error.

        Args:
            status_code: The Horde's HTTP status.
            return_code: The Horde ``rc``, or None when the body had none.
            message: The Horde's message, or the start of the body when it was not JSON.
            forwarded_headers: The ``Retry-After`` and rate-limit headers to pass back to the client.
            field_errors: The native payload validation failures by dotted field path, such as
                ``{"temperature": "5.5 is greater than the maximum of 5.0"}``; empty when the body listed none.
        """
        super().__init__(f"{status_code} {return_code}: {message}")
        self.status_code: int = status_code
        """The Horde's HTTP status."""
        self.return_code: str | None = return_code
        """The Horde ``rc``."""
        self.message: str = message
        """The Horde's message."""
        self.forwarded_headers: dict[str, str] = dict(forwarded_headers)
        """The headers to pass back to the client."""
        self.field_errors: dict[str, str] = dict(field_errors) if field_errors is not None else {}
        """The native payload validation failures by dotted field path, in the Horde's order."""


class HordeUnavailableError(Exception):
    """No configured Horde API root answered the call."""

    def __init__(self, message: str, *, request_sent: bool) -> None:
        """Create the error.

        Args:
            message: What failed.
            request_sent: Whether the call may have reached a Horde: true after a transport failure once a connection
                was made, false when no root accepted a connection.
        """
        super().__init__(message)
        self.request_sent: bool = request_sent
        """Whether the call may have reached a Horde, which leaves a submit's outcome unknown."""


class CancelSlotUnavailableError(Exception):
    """No cancel slot became free within the wait allowed, so the cancel was not sent."""


class HordeAsyncSubmission(BaseModel):
    """Represents the Horde's answer to a queued chat request."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    request_id: str = Field(validation_alias="id")
    """The Horde request id."""

    kudos: float | None = None
    """The kudos the Horde expects the request to cost."""

    message: str | None = None
    """An informational message, if any."""

    existing_claim: bool = False
    """True when the Horde answered 200 with the request the operation key already claimed; set from the status, since
    a new request is answered 202 with the same body shape."""


class HordeChatMessage(BaseModel):
    """Represents the assistant message a chat generation carries."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    role: str | None = None
    """The role of the message, ``assistant`` for a reply."""

    content: str | None = None
    """The reply text; an empty string is a completed, empty reply, and null comes only with ``tool_calls``."""

    tool_calls: list[ChatToolCall] | None = None
    """The tool calls the worker returned, in the shape the native route validated them in."""


class HordeChatGeneration(BaseModel):
    """Represents one generation in a Horde chat status."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    text: str | None = None
    """The generated text as a plain string; the fallback when ``message`` is absent."""

    message: HordeChatMessage | None = None
    """The assistant message the worker submitted."""

    model: str | None = None
    """The model that generated it."""

    worker_id: str | None = None
    """The worker that generated it."""

    worker_name: str | None = None
    """The name of that worker."""

    state: str | None = None
    """The generation state, such as ``ok`` or ``faulted``."""

    seed: int | str | None = None
    """The seed the worker used."""

    finish_reason: str | None = None
    """Why the generation stopped, normalised by the worker."""

    native_finish_reason: str | None = None
    """The backend's own reason, when the worker reported it."""

    prompt_tokens: int | None = None
    """The prompt token count, when the worker reported it."""

    completion_tokens: int | None = None
    """The generated token count, when the worker reported it."""

    charge_basis: str | None = None
    """Which count the generation was charged and rewarded on."""

    queue_wait_seconds: float | None = None
    """Seconds from the request's creation to this generation's assignment, when the Horde has an assignment time."""


class HordeChatStatus(BaseModel):
    """Represents the Horde's status of a chat request."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    done: bool = False
    """True when every generation of the request is finished."""

    faulted: bool = False
    """True when the request failed and will not finish."""

    finished: int = 0
    """How many generations are finished."""

    processing: int = 0
    """How many generations a worker is working on."""

    waiting: int = 0
    """How many generations wait for a worker."""

    queue_position: int | None = None
    """The request's position in the Horde-wide queue."""

    wait_time: int | None = None
    """The Horde-wide estimate of seconds until the request finishes."""

    kudos: float = 0
    """The kudos the request consumed so far."""

    is_possible: bool = True
    """False when no current worker can serve the request."""

    generations: list[HordeChatGeneration] = Field(default_factory=list)
    """The finished generations, in the Horde's order."""

    context_adjustments: list[object] | None = None
    """The context changes the Horde applied to fit the messages, when it reports any."""

    impossible_reason: ChatImpossibleReason | None = None
    """Why no worker can take the request when waiting does not help, such as a declaration gap; None otherwise."""

    fault_reason: str | None = None
    """For a faulted request, the message the Horde refused its last refused reply with, or its message for the reason
    a worker gave for faulting a job; None otherwise."""

    fault_rc: str | None = None
    """The return code of that reason, such as ``ChatSubmitNonconformant`` or ``ChatWorkerFault``; None when
    ``fault_reason`` is."""

    @field_validator("impossible_reason", mode="wrap")
    @classmethod
    def _ignore_unknown_impossible_reason(cls, value: object, handler: ValidatorFunctionWrapHandler) -> ChatImpossibleReason | None:
        """Read a reason of a kind this front does not know as none, so a newer Horde leaves the front waiting."""
        try:
            impossible_reason: ChatImpossibleReason | None = handler(value)
        except ValidationError:
            return None
        return impossible_reason


class HordeChatModelOffer(BaseModel):
    """Represents the ``chat`` object of a chat model list entry: what the model's online chat workers declare."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    workers: int = 0
    """How many online workers serve chat jobs with the model."""

    max_context_length: int | None = None
    """The largest chat context any of those workers declares."""

    max_length: int | None = None
    """The largest chat output any of those workers declares."""

    features: list[str] = Field(default_factory=list)
    """The sorted union of the optional chat features those workers declare."""

    created: int | None = None
    """Unix seconds at which the longest-registered of those workers was created; None when unknown."""


class HordeActiveModel(BaseModel):
    """Represents one entry of the Horde's active model list."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    """The model name."""

    count: int = 0
    """How many worker threads serve it."""

    queued: float | None = None
    """The queued work for the model, which chat requests share with text requests."""

    chat: HordeChatModelOffer | None = None
    """What the model's online chat workers declare; present when the list was asked for with ``chat=true``."""


class HordeClient:
    """Call the Horde's chat and model routes, rotating across the configured API roots.

    Concurrency:
        Safe to share between concurrent requests on one event loop; the rotation counter and the unhealthy marks are
        updated without awaiting. At most ``max_concurrent_calls`` status reads and model lists, and separately
        ``max_concurrent_submits`` submits and ``max_concurrent_cancels`` cancels, are in flight; the rest wait in
        arrival order.
    """

    def __init__(
        self,
        *,
        horde_urls: Sequence[str],
        http_client: httpx.AsyncClient,
        proxy_passkey: str | None,
        front_metrics: FrontMetrics,
        cancel_http_client: httpx.AsyncClient | None = None,
        max_concurrent_calls: int = HTTP_MAX_CONNECTIONS,
        max_concurrent_submits: int = SUBMIT_MAX_CONCURRENT_CALLS,
        max_concurrent_cancels: int = CANCEL_MIN_CONNECTIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the client.

        Args:
            horde_urls: The Horde API roots, each ending in ``/api``.
            http_client: The HTTP client for submits, status reads and model lists; the caller owns and closes it.
            proxy_passkey: The front operator's proxy passkey, or None to send no ``Proxied-For``.
            front_metrics: Where each call's latency and outcome is recorded.
            cancel_http_client: The HTTP client for cancels; None sends them through ``http_client`` under their own
                semaphore. The caller owns and closes it.
            max_concurrent_calls: The most status reads and model lists in flight at once.
            max_concurrent_submits: The most submits in flight at once; keep it plus ``max_concurrent_calls`` within
                the main pool's ``max_connections`` so the pool never queues.
            max_concurrent_cancels: The most cancels in flight at once; keep it within the cancel pool's size.
            clock: The monotonic clock the unhealthy marks are measured on.
        """
        if not horde_urls:
            raise ValueError("At least one Horde URL is required.")
        self._horde_urls: tuple[str, ...] = tuple(horde_urls)
        self._http_client = http_client
        self._cancel_http_client = cancel_http_client if cancel_http_client is not None else http_client
        self._proxy_passkey = proxy_passkey
        self._front_metrics = front_metrics
        self._rotation = itertools.count()
        self._call_slots = asyncio.Semaphore(max_concurrent_calls)
        self._submit_slots = asyncio.Semaphore(max_concurrent_submits)
        self._cancel_slots = asyncio.Semaphore(max_concurrent_cancels)
        self._clock = clock
        self._unhealthy_until: dict[str, float] = {}

    async def submit_chat(
        self,
        *,
        request_body: Mapping[str, object],
        credentials: HordeCredentials,
        fall_back_to_unhealthy_roots: bool = False,
    ) -> HordeAsyncSubmission:
        """Queue a chat request and return the Horde's acknowledgement.

        Args:
            request_body: The native chat request body, as JSON-ready values.
            credentials: Who the call is made for.
            fall_back_to_unhealthy_roots: Whether to try the roots left out of the rotation when none is healthy; a
                recovery sets it, since the dropped connection it recovers from marks a single root unhealthy. The
                call is recorded as a recovery when it is set.

        Raises:
            HordeAPIError: The Horde rejected the request.
            HordeUnavailableError: No Horde root accepted the connection, or the connection failed after the call was
                sent (``request_sent`` true), so a request may exist.
        """
        response = await self._send(
            "POST",
            CHAT_SUBMIT_PATH,
            call_type=NativeCallType.RECOVERY if fall_back_to_unhealthy_roots else NativeCallType.SUBMIT,
            credentials=credentials,
            json_body=request_body,
            call_slots=self._submit_slots,
            fall_back_to_unhealthy_roots=fall_back_to_unhealthy_roots,
        )
        submission = _parse_response(response, HordeAsyncSubmission)
        return submission.model_copy(update={"existing_claim": response.status_code == HTTPStatus.OK})

    async def get_chat_status(self, *, request_id: str, credentials: HordeCredentials) -> HordeChatStatus:
        """Return the Horde's status of a chat request.

        Args:
            request_id: The Horde request id.
            credentials: Who the call is made for.

        Raises:
            HordeAPIError: The Horde answered with an error, such as 404 once the request expired.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        status_path = CHAT_STATUS_PATH_TEMPLATE.format(request_id=request_id)
        response = await self._send("GET", status_path, call_type=NativeCallType.STATUS, credentials=credentials)
        return _parse_response(response, HordeChatStatus)

    async def cancel_chat(
        self,
        *,
        request_id: str,
        credentials: HordeCredentials,
        slot_wait_seconds: float,
        call_timeout_seconds: float,
    ) -> HordeChatStatus:
        """Cancel a chat request through the cancel pool and return its last status.

        The wait for a cancel slot and the HTTP call have separate bounds, so a cancel queued behind a wave of others
        is told apart from one the Horde was slow to answer.

        Args:
            request_id: The Horde request id.
            credentials: Who the call is made for.
            slot_wait_seconds: How long to wait for a free cancel slot before giving up without sending anything.
            call_timeout_seconds: How long the HTTP call may take once a slot is held.

        Raises:
            CancelSlotUnavailableError: No cancel slot became free within ``slot_wait_seconds``;
                the cancel was not sent.
            TimeoutError: The call did not complete within ``call_timeout_seconds``.
            HordeAPIError: The Horde answered with an error.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        status_path = CHAT_STATUS_PATH_TEMPLATE.format(request_id=request_id)
        try:
            async with asyncio.timeout(slot_wait_seconds):
                await self._cancel_slots.acquire()
        except TimeoutError as error:
            raise CancelSlotUnavailableError(f"No cancel slot became free within {slot_wait_seconds:g}s for {request_id}") from error
        try:
            async with asyncio.timeout(call_timeout_seconds):
                response = await self._send_holding_slot(
                    "DELETE",
                    status_path,
                    call_type=NativeCallType.CANCEL,
                    credentials=credentials,
                    sending_client=self._cancel_http_client,
                )
        finally:
            self._cancel_slots.release()
        return _parse_response(response, HordeChatStatus)

    async def list_chat_models(self, *, credentials: HordeCredentials) -> list[HordeActiveModel]:
        """Return the Horde's text models that an online chat worker serves, each with its ``chat`` object.

        Args:
            credentials: Who the call is made for.

        Raises:
            HordeAPIError: The Horde answered with an error.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        response = await self._send(
            "GET",
            MODELS_PATH,
            call_type=NativeCallType.MODELS,
            credentials=credentials,
            query=CHAT_MODELS_QUERY,
        )
        try:
            return [HordeActiveModel.model_validate(model_entry) for model_entry in response.json()]
        except (ValueError, TypeError, ValidationError) as error:
            raise _unexpected_body_error(response) from error

    def _build_headers(self, credentials: HordeCredentials) -> dict[str, str]:
        """Return the headers a Horde call carries for ``credentials``."""
        headers = {
            APIKEY_HEADER: credentials.apikey,
            CLIENT_AGENT_HEADER: credentials.client_agent,
        }
        if self._proxy_passkey and credentials.client_address:
            headers[PROXY_AUTHORIZATION_HEADER] = self._proxy_passkey
            headers[PROXIED_FOR_HEADER] = credentials.client_address
        return headers

    async def _send(
        self,
        method: str,
        path: str,
        *,
        call_type: NativeCallType,
        credentials: HordeCredentials,
        json_body: Mapping[str, object] | None = None,
        query: Mapping[str, str] | None = None,
        call_slots: asyncio.Semaphore | None = None,
        fall_back_to_unhealthy_roots: bool = False,
    ) -> httpx.Response:
        """Send one call through the main client, trying each healthy root in rotation until one accepts the connection.

        Args:
            method: The HTTP method.
            path: The route below the API root.
            call_type: The kind of call, recorded with its latency and outcome.
            credentials: Who the call is made for.
            json_body: The JSON body, if any.
            query: The query parameters, if any.
            call_slots: The semaphore bounding the call; None selects the status read and model list one.
            fall_back_to_unhealthy_roots: Whether to try every root in rotation order when none is healthy.

        Returns:
            The Horde's successful response.

        Raises:
            HordeAPIError: The Horde answered with a status of 400 or above.
            HordeUnavailableError: No healthy root accepted the connection, or the connection failed after the call
                was sent.
        """
        async with call_slots if call_slots is not None else self._call_slots:
            return await self._send_holding_slot(
                method,
                path,
                call_type=call_type,
                credentials=credentials,
                sending_client=self._http_client,
                json_body=json_body,
                query=query,
                fall_back_to_unhealthy_roots=fall_back_to_unhealthy_roots,
            )

    async def _send_holding_slot(
        self,
        method: str,
        path: str,
        *,
        call_type: NativeCallType,
        credentials: HordeCredentials,
        sending_client: httpx.AsyncClient,
        json_body: Mapping[str, object] | None = None,
        query: Mapping[str, str] | None = None,
        fall_back_to_unhealthy_roots: bool = False,
    ) -> httpx.Response:
        """Send one call and record its latency and outcome; the caller holds the slot that bounds it.

        Raises:
            HordeAPIError: The Horde answered with a status of 400 or above.
            HordeUnavailableError: No healthy root accepted the connection, or the connection failed after the call
                was sent.
        """
        # Timed after the caller took a call slot, so time queued on the semaphore is not part of the latency.
        started_at = time.perf_counter()
        outcome = NativeCallOutcome.ABANDONED
        try:
            response = await self._send_in_rotation(
                method,
                path,
                sending_client=sending_client,
                headers=self._build_headers(credentials),
                json_body=json_body,
                query=query,
                fall_back_to_unhealthy_roots=fall_back_to_unhealthy_roots,
            )
            outcome = NativeCallOutcome.OK
        except HordeAPIError:
            outcome = NativeCallOutcome.REFUSED
            raise
        except HordeUnavailableError:
            outcome = NativeCallOutcome.UNREACHABLE
            raise
        finally:
            self._front_metrics.record_native_call(
                call_type=call_type,
                outcome=outcome,
                duration_seconds=time.perf_counter() - started_at,
            )
        return response

    async def _send_in_rotation(
        self,
        method: str,
        path: str,
        *,
        sending_client: httpx.AsyncClient,
        headers: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        query: Mapping[str, str] | None,
        fall_back_to_unhealthy_roots: bool,
    ) -> httpx.Response:
        """Return the first answer of the healthy roots in rotation; the caller holds a call slot.

        Raises:
            HordeAPIError: The Horde answered with a status of 400 or above.
            HordeUnavailableError: No healthy root accepted the connection, or the connection failed after the call
                was sent.
        """
        for horde_url in self._healthy_urls_in_rotation(fall_back_to_unhealthy_roots=fall_back_to_unhealthy_roots):
            try:
                response = await sending_client.request(
                    method,
                    f"{horde_url}{path}",
                    headers=headers,
                    json=json_body,
                    params=query,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                # A refused or timed-out connection can be retried elsewhere: the call never reached that Horde, so a submit
                # cannot be queued twice.
                self._mark_unhealthy(horde_url, error)
                continue
            except httpx.TransportError as error:
                # The call may have reached the Horde, so it is not retried elsewhere. A timeout says the Horde
                # is slow rather than gone, so it does not take the root out of the rotation.
                if not isinstance(error, httpx.TimeoutException):
                    self._mark_unhealthy(horde_url, error)
                raise HordeUnavailableError(
                    f"The Horde at {horde_url} failed {method} {path}: {error!r}",
                    request_sent=not isinstance(error, httpx.ConnectTimeout | httpx.PoolTimeout),
                ) from error
            if response.status_code >= HTTPStatus.BAD_REQUEST:
                raise _api_error_from_response(response)
            return response
        raise HordeUnavailableError(f"No healthy Horde accepted the connection for {method} {path}.", request_sent=False)

    def _healthy_urls_in_rotation(self, *, fall_back_to_unhealthy_roots: bool = False) -> list[str]:
        """Return the roots not marked unhealthy, starting from the next one in the rotation.

        With ``fall_back_to_unhealthy_roots``, every root is returned in rotation order when none is healthy.
        """
        start_index = next(self._rotation) % len(self._horde_urls)
        now = self._clock()
        rotated_urls = self._horde_urls[start_index:] + self._horde_urls[:start_index]
        healthy_urls = [horde_url for horde_url in rotated_urls if self._unhealthy_until.get(horde_url, 0.0) <= now]
        if not healthy_urls and fall_back_to_unhealthy_roots:
            return list(rotated_urls)
        return healthy_urls

    def _mark_unhealthy(self, horde_url: str, error: httpx.TransportError) -> None:
        """Leave ``horde_url`` out of the rotation for the backoff, logging only when it was healthy until now."""
        now = self._clock()
        if self._unhealthy_until.get(horde_url, 0.0) <= now:
            logger.warning(
                f"Horde at {horde_url} failed a connection ({type(error).__name__}: {error}); "
                f"leaving it out of the rotation for {UNHEALTHY_URL_BACKOFF_SECONDS:g}s",
            )
        self._unhealthy_until[horde_url] = now + UNHEALTHY_URL_BACKOFF_SECONDS


def _parse_response[ModelType: BaseModel](response: httpx.Response, model_class: type[ModelType]) -> ModelType:
    """Return ``response``'s JSON body validated as ``model_class``.

    Raises:
        HordeAPIError: The body is not the expected JSON object.
    """
    try:
        return model_class.model_validate(response.json())
    except (ValueError, ValidationError) as error:
        raise _unexpected_body_error(response) from error


def _unexpected_body_error(response: httpx.Response) -> HordeAPIError:
    """Return the error for a successful Horde response whose body could not be parsed."""
    return HordeAPIError(
        status_code=HTTPStatus.BAD_GATEWAY,
        return_code=UNEXPECTED_RESPONSE_CODE,
        message=f"The Horde returned an unexpected response body: {response.text[:MAX_ERROR_BODY_CHARACTERS]}",
        forwarded_headers={},
    )


def _api_error_from_response(response: httpx.Response) -> HordeAPIError:
    """Return the error for a Horde response with a status of 400 or above."""
    forwarded_headers = {
        header_name: header_value
        for header_name, header_value in response.headers.items()
        if header_name.lower() in FORWARDED_RESPONSE_HEADERS or header_name.lower().startswith(RATE_LIMIT_HEADER_PREFIX)
    }
    return_code: str | None = None
    message = response.text[:MAX_ERROR_BODY_CHARACTERS]
    field_errors: dict[str, str] = {}
    try:
        error_body = response.json()
    except ValueError:
        error_body = None
    if isinstance(error_body, dict):
        body_return_code = error_body.get("rc")
        body_message = error_body.get("message")
        return_code = body_return_code if isinstance(body_return_code, str) else None
        if isinstance(body_message, str):
            message = body_message
        # A native payload validation failure lists the failing fields under "errors" and carries no rc.
        body_field_errors = error_body.get("errors")
        if isinstance(body_field_errors, dict):
            field_errors = {str(field_path): str(field_message) for field_path, field_message in body_field_errors.items()}
    return HordeAPIError(
        status_code=response.status_code,
        return_code=return_code,
        message=message,
        forwarded_headers=forwarded_headers,
        field_errors=field_errors,
    )
