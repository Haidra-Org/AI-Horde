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
``request_sent`` says whether the call may have reached a Horde.

Submits, status reads and model lists share one pool whose size the caller sets (``HORDE_OAI_NATIVE_MAX_CONNECTIONS``);
the rest wait on a semaphore in arrival order before they reach the pool. Cancels use a second, smaller pool and
semaphore ([`CANCEL_MAX_CONNECTIONS`][horde.oai.horde_client.CANCEL_MAX_CONNECTIONS]), so a burst of slow cancels cannot
starve submits and a queue of status reads cannot delay a cancel.

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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "CANCEL_MAX_CONNECTIONS",
    "HTTP_MAX_CONNECTIONS",
    "HordeAPIError",
    "HordeActiveModel",
    "HordeAsyncSubmission",
    "HordeChatGeneration",
    "HordeChatMessage",
    "HordeChatStatus",
    "HordeClient",
    "HordeCredentials",
    "HordeUnavailableError",
    "create_http_client",
]

CHAT_SUBMIT_PATH: str = "/v2/generate/chat/async"
"""The Horde route that queues a chat request."""

CHAT_STATUS_PATH_TEMPLATE: str = "/v2/generate/chat/status/{request_id}"
"""The Horde route that reports (GET) or cancels (DELETE) a chat request."""

MODELS_PATH: str = "/v2/status/models"
"""The Horde route that lists active models."""

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

[`HordeClient`][horde.oai.horde_client.HordeClient] admits this many calls at once through a semaphore of the same
size, so the pool never queues a request. httpcore rescans every in-flight request against every pooled connection
on each pool event, probing each idle connection's socket, so the cost of one event grows with the square of this
number. The number also bounds how many slow calls can be in flight at once: with too few slots, submits queue behind
slow status reads until clients give up.
"""

CANCEL_MAX_CONNECTIONS: int = 16
"""The most simultaneous cancels, and the size of their separate connection pool.

Cancels are rare next to status reads, but each can take its whole budget when the Horde is slow; a pool of their own
keeps them from holding the slots submits need, and keeps them moving when status reads fill the main pool.
"""

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


def create_http_client(max_connections: int = HTTP_MAX_CONNECTIONS) -> httpx.AsyncClient:
    """Create an HTTP client for Horde calls.

    Args:
        max_connections: The pool size; keep it equal to the semaphore the calls through this client wait on.
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
    """The ``Client-Agent`` value: the front's name and version plus the client's user agent."""

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
    ) -> None:
        """Create the error.

        Args:
            status_code: The Horde's HTTP status.
            return_code: The Horde ``rc``, or None when the body had none.
            message: The Horde's message, or the start of the body when it was not JSON.
            forwarded_headers: The ``Retry-After`` and rate-limit headers to pass back to the client.
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


class HordeAsyncSubmission(BaseModel):
    """Represents the Horde's answer to a queued chat request."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    request_id: str = Field(validation_alias="id")
    """The Horde request id."""

    kudos: float | None = None
    """The kudos the Horde expects the request to cost."""

    message: str | None = None
    """An informational message, if any."""


class HordeChatMessage(BaseModel):
    """Represents the assistant message a chat generation carries."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    role: str | None = None
    """The role of the message, ``assistant`` for a reply."""

    content: str | None = None
    """The reply text; an empty string is a completed, empty reply."""


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


class HordeActiveModel(BaseModel):
    """Represents one entry of the Horde's active model list."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    """The model name."""

    count: int = 0
    """How many worker threads serve it."""

    queued: float | None = None
    """The queued work for the model."""


class HordeClient:
    """Call the Horde's chat and model routes, rotating across the configured API roots.

    Concurrency:
        Safe to share between concurrent requests on one event loop; the rotation counter and the unhealthy marks are
        updated without awaiting. At most ``max_concurrent_calls`` calls and, separately, ``max_concurrent_cancels``
        cancels are in flight; the rest wait in arrival order.
    """

    def __init__(
        self,
        *,
        horde_urls: Sequence[str],
        http_client: httpx.AsyncClient,
        proxy_passkey: str | None,
        cancel_http_client: httpx.AsyncClient | None = None,
        max_concurrent_calls: int = HTTP_MAX_CONNECTIONS,
        max_concurrent_cancels: int = CANCEL_MAX_CONNECTIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the client.

        Args:
            horde_urls: The Horde API roots, each ending in ``/api``.
            http_client: The HTTP client for submits, status reads and model lists; the caller owns and closes it.
            proxy_passkey: The front operator's proxy passkey, or None to send no ``Proxied-For``.
            cancel_http_client: The HTTP client for cancels; None sends them through ``http_client`` under their own
                semaphore. The caller owns and closes it.
            max_concurrent_calls: The most non-cancel calls in flight at once; keep it equal to the main pool's
                ``max_connections`` so the pool never queues.
            max_concurrent_cancels: The most cancels in flight at once.
            clock: The monotonic clock the unhealthy marks are measured on.
        """
        if not horde_urls:
            raise ValueError("At least one Horde URL is required.")
        self._horde_urls: tuple[str, ...] = tuple(horde_urls)
        self._http_client = http_client
        self._cancel_http_client = cancel_http_client if cancel_http_client is not None else http_client
        self._proxy_passkey = proxy_passkey
        self._rotation = itertools.count()
        self._call_slots = asyncio.Semaphore(max_concurrent_calls)
        self._cancel_slots = asyncio.Semaphore(max_concurrent_cancels)
        self._clock = clock
        self._unhealthy_until: dict[str, float] = {}

    async def submit_chat(self, *, request_body: Mapping[str, object], credentials: HordeCredentials) -> HordeAsyncSubmission:
        """Queue a chat request and return the Horde's acknowledgement.

        Args:
            request_body: The native chat request body, as JSON-ready values.
            credentials: Who the call is made for.

        Raises:
            HordeAPIError: The Horde rejected the request.
            HordeUnavailableError: No Horde root accepted the connection, or the connection failed after the call was
                sent (``request_sent`` true), so a request may exist.
        """
        response = await self._send("POST", CHAT_SUBMIT_PATH, credentials=credentials, json_body=request_body)
        return _parse_response(response, HordeAsyncSubmission)

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
        response = await self._send("GET", status_path, credentials=credentials)
        return _parse_response(response, HordeChatStatus)

    async def cancel_chat(self, *, request_id: str, credentials: HordeCredentials) -> HordeChatStatus:
        """Cancel a chat request through the cancel pool and return its last status.

        Args:
            request_id: The Horde request id.
            credentials: Who the call is made for.

        Raises:
            HordeAPIError: The Horde answered with an error.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        status_path = CHAT_STATUS_PATH_TEMPLATE.format(request_id=request_id)
        response = await self._send(
            "DELETE",
            status_path,
            credentials=credentials,
            http_client=self._cancel_http_client,
            call_slots=self._cancel_slots,
        )
        return _parse_response(response, HordeChatStatus)

    async def list_text_models(self, *, credentials: HordeCredentials) -> list[HordeActiveModel]:
        """Return the Horde's active text models.

        Args:
            credentials: Who the call is made for.

        Raises:
            HordeAPIError: The Horde answered with an error.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        response = await self._send("GET", MODELS_PATH, credentials=credentials, query={"type": "text"})
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
        credentials: HordeCredentials,
        json_body: Mapping[str, object] | None = None,
        query: Mapping[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        call_slots: asyncio.Semaphore | None = None,
    ) -> httpx.Response:
        """Send one call, trying each healthy root in rotation until one accepts the connection.

        Args:
            method: The HTTP method.
            path: The route below the API root.
            credentials: Who the call is made for.
            json_body: The JSON body, if any.
            query: The query parameters, if any.
            http_client: The client to send through; None selects the main client.
            call_slots: The semaphore bounding calls through that client; None selects the main one.

        Returns:
            The Horde's successful response.

        Raises:
            HordeAPIError: The Horde answered with a status of 400 or above.
            HordeUnavailableError: No healthy root accepted the connection, or the connection failed after the call
                was sent.
        """
        headers = self._build_headers(credentials)
        sending_client = http_client if http_client is not None else self._http_client
        async with call_slots if call_slots is not None else self._call_slots:
            for horde_url in self._healthy_urls_in_rotation():
                try:
                    response = await sending_client.request(
                        method,
                        f"{horde_url}{path}",
                        headers=headers,
                        json=json_body,
                        params=query,
                    )
                except httpx.ConnectError as error:
                    # Only a refused connection is retried elsewhere: the call never reached that Horde, so a submit
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

    def _healthy_urls_in_rotation(self) -> list[str]:
        """Return the roots not marked unhealthy, starting from the next one in the rotation."""
        start_index = next(self._rotation) % len(self._horde_urls)
        now = self._clock()
        rotated_urls = self._horde_urls[start_index:] + self._horde_urls[:start_index]
        return [horde_url for horde_url in rotated_urls if self._unhealthy_until.get(horde_url, 0.0) <= now]

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
    return HordeAPIError(
        status_code=response.status_code,
        return_code=return_code,
        message=message,
        forwarded_headers=forwarded_headers,
    )
