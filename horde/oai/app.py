# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The FastAPI application serving the OpenAI-compatible chat completions front.

[`create_app`][horde.oai.app.create_app] builds the app with these routes:

- ``POST /v1/chat/completions``: reads the body under a byte ceiling and a read timeout, screens and validates it,
  takes an inflight slot (per-source cap first, then the global cap), submits to the Horde's
  ``/v2/generate/chat/async``, waits through [`ChatRequestWatcher`][horde.oai.waiter.ChatRequestWatcher] and answers
  with a ``chat.completion`` body, or with server-sent events when ``stream`` is true.
- ``GET /v1/models`` and ``GET /v1/models/{id}``: the Horde's active text models in the OpenAI list shape; an id may
  contain slashes.
- ``GET /healthz``: liveness, admission readiness and whether the completion signal subscription is up.

Every error before headers is an OpenAI error envelope with a ``metadata`` object; after stream headers it is one SSE
error event. Once the Horde holds a request, or may hold one after a submit whose answer was lost, every error carries
``x-should-retry: false`` so a client SDK does not create a second charged job; a successful stream carries it too.
Cancels on disconnect, deadline, fail-fast and shutdown are bounded, shielded from the client's disconnect, sent at
most once per request and use the client's separate cancel pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections.abc import AsyncIterator, Mapping
from http import HTTPStatus

import anyio
import httpx
import redis.asyncio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import ValidationError
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from horde.chat_contract import CHAT_REQUEST_CONFLICT_ERROR_TYPE
from horde.consts import ANONYMOUS_API_KEY, HORDE_VERSION
from horde.oai.horde_client import (
    HordeActiveModel,
    HordeAPIError,
    HordeAsyncSubmission,
    HordeChatStatus,
    HordeClient,
    HordeCredentials,
    HordeUnavailableError,
    create_http_client,
)
from horde.oai.mapping import build_completion, build_error_envelope, build_native_chat_body
from horde.oai.schema import (
    BAD_REQUEST_CODE,
    CHAT_REQUEST_CONFLICT_CODE,
    UNSUPPORTED_PARAMETER_CODE,
    UNSUPPORTED_PARAMETER_ERROR_TYPE,
    ChatCompletionRequest,
    ErrorMetadata,
    HordeModelDetails,
    ModelCard,
    ModelList,
    OpenAIErrorEnvelope,
    SubmitOutcome,
    drop_default_no_ops,
    find_refused_parameter,
)
from horde.oai.settings import FrontSettings
from horde.oai.sse import build_completion_events, build_error_events, build_keepalive_event
from horde.oai.waiter import (
    ChatRequestWatcher,
    ChatWaitTimeoutError,
    CompletionSignalListener,
    CompletionSubscription,
    NoEligibleWorkerError,
    WaitBudget,
)

__all__ = [
    "ChatFrontError",
    "FrontState",
    "InflightLimiter",
    "InflightSlot",
    "OpenRequest",
    "create_app",
]

CLIENT_AGENT_NAME: str = "horde-oai-front"
"""The name the front gives itself in the ``Client-Agent`` it sends the Horde."""

MAX_FORWARDED_USER_AGENT_CHARACTERS: int = 200
"""The most characters of the client's ``User-Agent`` appended to the ``Client-Agent``; it is stored per request."""

BEARER_SCHEME: str = "bearer"
"""The lower-cased ``Authorization`` scheme carrying an API key."""

UNKNOWN_SOURCE: str = "unknown"
"""The source key of a request whose ASGI client address is not known; such requests share one per-source cap."""

MODEL_VARIANT_SEPARATOR: str = ":"
"""The character reserved for model id variants; no variant is served, so an id containing it is unknown."""

SHOULD_RETRY_HEADER: str = "x-should-retry"
"""The response header OpenAI SDKs honour to skip their automatic retries."""

SHOULD_NOT_RETRY: str = "false"
"""The ``x-should-retry`` value sent once a retry could create a second charged job."""

RETRY_AFTER_HEADER: str = "Retry-After"
"""The response header telling a client how many seconds to wait before retrying."""

STREAM_HEADERS: Mapping[str, str] = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    SHOULD_RETRY_HEADER: SHOULD_NOT_RETRY,
}
"""Headers of every streamed response: no caching or proxy buffering, and no automatic retry once a job exists."""

INFLIGHT_RETRY_AFTER_SECONDS: int = 5
"""The ``Retry-After`` of a 503 for a full front; waiting requests usually finish within seconds of each other."""

NO_ELIGIBLE_WORKER_RETRY_AFTER_SECONDS: int = 30
"""The ``Retry-After`` of a 503 for no eligible worker; long enough for a worker restart or model load to show."""

HORDE_UNAVAILABLE_RETRY_AFTER_SECONDS: int = 30
"""The ``Retry-After`` added to a Horde 503 that carries none, such as chat admission switched off for maintenance."""

MODEL_LIST_CACHE_SECONDS: float = 5.0
"""How long one process reuses the Horde's model list, so clients listing models often do not each cost a native call."""

CLIENT_CLOSED_REQUEST_STATUS: int = 499
"""The status recorded for a non-streamed request whose client disconnected; no client receives it."""

TIMEOUT_CODE: str = "ChatRequestTimeout"
"""The error code of a request still waiting after its deadline and the final status read."""

FAULTED_CODE: str = "ChatRequestFaulted"
"""The error code of a request the Horde reports as faulted."""

FRONT_FULL_CODE: str = "FrontAtCapacity"
"""The error code of a request refused because the process or the request's source holds its maximum of requests."""

HORDE_UNAVAILABLE_CODE: str = "HordeUnavailable"
"""The error code of a request no Horde root answered."""

INVALID_AUTHORIZATION_CODE: str = "InvalidAuthorizationHeader"
"""The error code of a request whose ``Authorization`` header is not ``Bearer <key>``."""

REQUEST_TOO_LARGE_CODE: str = "RequestTooLarge"
"""The error code of a body over the byte ceiling."""

REQUEST_BODY_TIMEOUT_CODE: str = "RequestBodyTimeout"
"""The error code of a body not received within the body-read timeout; a slow sender is not an oversized body."""

MODEL_NOT_FOUND_CODE: str = "model_not_found"
"""The OpenAI error code of an unknown model id."""

NO_ELIGIBLE_WORKER_CODE: str = "no_eligible_worker"
"""The error code of a request no online worker could serve within the fail-fast grace."""


class ChatFrontError(Exception):
    """An error the front answers with an OpenAI error envelope, or an SSE error event after headers."""

    def __init__(
        self,
        *,
        status_code: int,
        message: str,
        code: str | None,
        param: str | None = None,
        metadata: ErrorMetadata | None = None,
        headers: Mapping[str, str] | None = None,
        job_may_exist: bool = False,
    ) -> None:
        """Create the error.

        Args:
            status_code: The HTTP status.
            message: The explanation for the client.
            code: The Horde return code, or the front's own code.
            param: The request path at fault, when one is.
            metadata: The request id, return code and retry details that are known.
            headers: Extra response headers, such as ``Retry-After``.
            job_may_exist: Whether the Horde holds, or may hold, a request for this call; adds ``x-should-retry: false``.
        """
        super().__init__(message)
        self.status_code: int = status_code
        """The HTTP status."""
        self.message: str = message
        """The explanation for the client."""
        self.code: str | None = code
        """The Horde return code, or the front's own code."""
        self.param: str | None = param
        """The request path at fault."""
        self.metadata: ErrorMetadata = metadata if metadata is not None else ErrorMetadata()
        """The request id, return code and retry details that are known."""
        self.headers: dict[str, str] = dict(headers or {})
        """Extra response headers."""
        self.job_may_exist: bool = job_may_exist
        """Whether a retry could create a second charged job."""

    def to_response(self) -> JSONResponse:
        """Create the JSON error response, with ``x-should-retry: false`` when a job may exist."""
        headers = dict(self.headers)
        if self.job_may_exist:
            headers[SHOULD_RETRY_HEADER] = SHOULD_NOT_RETRY
        return JSONResponse(status_code=self.status_code, content=self._envelope_payload(), headers=headers)

    def to_events(self) -> list[ServerSentEvent]:
        """Create the SSE error event and ``[DONE]`` for an error after stream headers."""
        return build_error_events(self._envelope())

    def _envelope(self) -> OpenAIErrorEnvelope:
        """Create the OpenAI error envelope."""
        return build_error_envelope(
            status_code=self.status_code,
            message=self.message,
            code=self.code,
            param=self.param,
            metadata=self.metadata,
        )

    def _envelope_payload(self) -> dict[str, object]:
        """Return the envelope as JSON-ready values, null fields included."""
        payload: dict[str, object] = self._envelope().model_dump(mode="json")
        return payload


class InflightLimiter:
    """Count the chat requests the process holds open, globally and per source, and refuse new ones past the caps.

    Concurrency:
        Acquire and release run on the event loop without awaiting, so the counts need no lock.
    """

    def __init__(self, *, max_inflight: int, max_inflight_per_source: int) -> None:
        """Create the limiter.

        Args:
            max_inflight: The most requests held open at once.
            max_inflight_per_source: The most requests held open at once for one source address.
        """
        self._max_inflight = max_inflight
        self._max_inflight_per_source = max_inflight_per_source
        self._inflight = 0
        self._inflight_by_source: dict[str, int] = {}

    @property
    def inflight(self) -> int:
        """Return how many requests are held open."""
        return self._inflight

    @property
    def has_capacity(self) -> bool:
        """Return whether the global cap admits one more request."""
        return self._inflight < self._max_inflight

    def inflight_for(self, source: str) -> int:
        """Return how many requests are held open for ``source``."""
        return self._inflight_by_source.get(source, 0)

    def try_acquire(self, source: str) -> InflightSlot | None:
        """Return a slot for one more request from ``source``, or None when the source's cap or the global cap is reached.

        The source's cap is checked first, so one client filling its share is told so even when the process is full.
        """
        if self.inflight_for(source) >= self._max_inflight_per_source:
            return None
        if not self.has_capacity:
            return None
        self._inflight += 1
        self._inflight_by_source[source] = self.inflight_for(source) + 1
        return InflightSlot(self, source)

    def _release(self, source: str) -> None:
        """Mutate the counts by returning one slot of ``source``."""
        self._inflight -= 1
        remaining_for_source = self.inflight_for(source) - 1
        if remaining_for_source > 0:
            self._inflight_by_source[source] = remaining_for_source
        else:
            self._inflight_by_source.pop(source, None)


class InflightSlot:
    """One request's place in the [`InflightLimiter`][horde.oai.app.InflightLimiter]; only the first release counts."""

    def __init__(self, limiter: InflightLimiter, source: str) -> None:
        """Create the slot.

        Args:
            limiter: The limiter the slot belongs to.
            source: The source address the slot counts against.
        """
        self._limiter = limiter
        self._source = source
        self._released = False

    def release(self) -> None:
        """Return the slot to the limiter, once."""
        if self._released:
            return
        self._released = True
        self._limiter._release(self._source)


class OpenRequest:
    """A submitted Horde request the process may still have to cancel; it is cancelled at most once."""

    def __init__(self, *, request_id: str, credentials: HordeCredentials) -> None:
        """Create the record.

        Args:
            request_id: The Horde request id.
            credentials: Who the cancel is made for.
        """
        self.request_id: str = request_id
        """The Horde request id."""
        self.credentials: HordeCredentials = credentials
        """Who the cancel is made for."""
        self.cancel_started: bool = False
        """Whether a cancel was sent or is being sent."""


class ModelListCache:
    """Hold the Horde's model list for [`MODEL_LIST_CACHE_SECONDS`][horde.oai.app.MODEL_LIST_CACHE_SECONDS]."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._models: list[HordeActiveModel] | None = None
        self._fetched_at: float = -math.inf

    async def get(self, horde_client: HordeClient, credentials: HordeCredentials) -> list[HordeActiveModel]:
        """Return the cached model list, fetching it when it is older than the cache period.

        Raises:
            HordeAPIError: The Horde answered with an error.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        now = time.monotonic()
        if self._models is None or now - self._fetched_at >= MODEL_LIST_CACHE_SECONDS:
            self._models = await horde_client.list_text_models(credentials=credentials)
            self._fetched_at = now
        return self._models


class FrontState:
    """Hold the process-wide objects the routes share."""

    def __init__(
        self,
        *,
        settings: FrontSettings,
        horde_client: HordeClient,
        listener: CompletionSignalListener,
    ) -> None:
        """Create the state.

        Args:
            settings: The front's settings.
            horde_client: The Horde client.
            listener: The completion signal listener.
        """
        self.settings: FrontSettings = settings
        """The front's settings."""
        self.horde_client: HordeClient = horde_client
        """The Horde client."""
        self.listener: CompletionSignalListener = listener
        """The completion signal listener."""
        self.inflight_limiter: InflightLimiter = InflightLimiter(
            max_inflight=settings.max_inflight,
            max_inflight_per_source=settings.max_inflight_per_source,
        )
        """The open request counts."""
        self.open_requests: dict[str, OpenRequest] = {}
        """The submitted requests not yet finished, by Horde request id; a clean stop cancels them."""
        self.model_list_cache: ModelListCache = ModelListCache()
        """The cached Horde model list."""
        self.accepting: bool = True
        """False once the process is stopping; ``/healthz`` then reports it not ready."""

    def open_request(self, *, request_id: str, credentials: HordeCredentials) -> OpenRequest:
        """Create and register the record of a submitted request."""
        open_request = OpenRequest(request_id=request_id, credentials=credentials)
        self.open_requests[request_id] = open_request
        return open_request

    def close_request(self, open_request: OpenRequest) -> None:
        """Mutate the registry by removing a finished request; removing it twice is harmless."""
        if self.open_requests.get(open_request.request_id) is open_request:
            del self.open_requests[open_request.request_id]


def create_app(
    settings: FrontSettings,
    *,
    http_client: httpx.AsyncClient | None = None,
    cancel_http_client: httpx.AsyncClient | None = None,
    redis_client: redis.asyncio.Redis | None = None,
) -> FastAPI:
    """Create the chat front application.

    Args:
        settings: The front's settings.
        http_client: The HTTP client for submits, status reads and model lists; None creates one sized by
            ``settings.native_max_connections`` that the app closes on shutdown.
        cancel_http_client: The HTTP client for cancels; None creates a separate small pool the app closes on shutdown.
        redis_client: The client for the Horde's chat signal database; None creates one from ``settings.redis_url``
            when it is set, and otherwise requests wait on the fallback poll alone.

    Returns:
        The application; its lifespan starts the signal listener and, on stop, cancels every open request within
        ``settings.shutdown_cancel_seconds`` before closing the subscription.
    """
    owned_http_clients: list[httpx.AsyncClient] = []
    if http_client is None:
        http_client = create_http_client(settings.native_max_connections)
        owned_http_clients.append(http_client)
    if cancel_http_client is None:
        cancel_http_client = create_http_client()
        owned_http_clients.append(cancel_http_client)
    owns_redis_client = redis_client is None and settings.redis_url is not None
    if redis_client is None and settings.redis_url is not None:
        # The listener's own liveness ping detects a black-holed subscription. redis-py's health_check_interval is left
        # off: its PINGs would interleave with the listener's and it drops the PONGs the listener waits for.
        redis_client = redis.asyncio.Redis.from_url(settings.redis_url, socket_keepalive=True)
    if redis_client is None:
        logger.warning("HORDE_OAI_REDIS_URL is not set; chat requests wait on the fallback poll alone.")
    front_state = FrontState(
        settings=settings,
        horde_client=HordeClient(
            horde_urls=settings.horde_urls,
            http_client=http_client,
            cancel_http_client=cancel_http_client,
            proxy_passkey=settings.proxy_passkey,
            max_concurrent_calls=settings.native_max_connections,
        ),
        listener=CompletionSignalListener(redis_client, liveness_seconds=settings.redis_liveness_seconds),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await front_state.listener.start()
        try:
            yield
        finally:
            front_state.accepting = False
            await _cancel_open_requests(front_state)
            await front_state.listener.stop()
            if owns_redis_client and redis_client is not None:
                await redis_client.close()
            for owned_http_client in owned_http_clients:
                await owned_http_client.aclose()

    app = FastAPI(title="AI Horde OpenAI-compatible front", version=HORDE_VERSION, lifespan=lifespan)
    app.state.front = front_state
    app.add_api_route("/v1/chat/completions", _chat_completions, methods=["POST"], response_model=None)
    app.add_api_route("/v1/models", _list_models, methods=["GET"], response_model=None)
    app.add_api_route("/v1/models/{model_id:path}", _get_model, methods=["GET"], response_model=None)
    app.add_api_route("/healthz", _health, methods=["GET"], response_model=None)
    return app


def _front_state(request: Request) -> FrontState:
    """Return the shared state of the app serving ``request``."""
    front_state: FrontState = request.app.state.front
    return front_state


def _request_source(request: Request) -> str:
    """Return the end-user address of ``request``: the ASGI client address after uvicorn's forwarded handling."""
    return request.client.host if request.client is not None else UNKNOWN_SOURCE


def _horde_error(error: HordeAPIError, *, request_id: str | None, job_may_exist: bool) -> ChatFrontError:
    """Create the front error for a Horde error, keeping its status, ``rc`` and forwarded headers."""
    retry_after_header = next(
        (
            header_value
            for header_name, header_value in error.forwarded_headers.items()
            if header_name.lower() == RETRY_AFTER_HEADER.lower()
        ),
        None,
    )
    return ChatFrontError(
        status_code=error.status_code,
        message=error.message,
        code=error.return_code,
        metadata=ErrorMetadata(
            request_id=request_id,
            rc=error.return_code,
            retry_after=int(retry_after_header) if retry_after_header is not None and retry_after_header.isdigit() else None,
        ),
        headers=error.forwarded_headers,
        job_may_exist=job_may_exist,
    )


def _build_credentials(request: Request) -> HordeCredentials:
    """Return the Horde identity for ``request``: its bearer API key (or the anonymous key) and its address.

    Raises:
        ChatFrontError: 401 when an ``Authorization`` header is present but is not ``Bearer <key>``.
    """
    authorization = request.headers.get("authorization")
    apikey = ANONYMOUS_API_KEY
    if authorization is not None:
        scheme, _, token = authorization.strip().partition(" ")
        token = token.strip()
        if scheme.lower() != BEARER_SCHEME or not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise ChatFrontError(
                status_code=HTTPStatus.UNAUTHORIZED,
                message="The Authorization header must be 'Bearer <AI Horde API key>'; omit it to use the anonymous key.",
                code=INVALID_AUTHORIZATION_CODE,
            )
        apikey = token
    user_agent = request.headers.get("user-agent", "unknown")[:MAX_FORWARDED_USER_AGENT_CHARACTERS]
    return HordeCredentials(
        apikey=apikey,
        client_agent=f"{CLIENT_AGENT_NAME}:{HORDE_VERSION}:{user_agent}",
        client_address=request.client.host if request.client is not None else None,
    )


def _request_too_large(message: str) -> ChatFrontError:
    """Create the 413 for a body over the ceiling."""
    return ChatFrontError(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, message=message, code=REQUEST_TOO_LARGE_CODE)


async def _read_body(request: Request, settings: FrontSettings) -> bytes:
    """Return the request body, counted against the byte ceiling while it is received.

    Raises:
        ChatFrontError: 413 when the declared or received size passes the ceiling, and 408 when the body is not
            received within the body-read timeout.
    """
    too_large_message = f"The request body is larger than {settings.max_body_bytes} bytes."
    declared_length = request.headers.get("content-length")
    if declared_length is not None and declared_length.isdigit() and int(declared_length) > settings.max_body_bytes:
        raise _request_too_large(too_large_message)
    body_chunks: list[bytes] = []
    received_bytes = 0
    try:
        async with asyncio.timeout(settings.body_read_timeout_seconds):
            async for body_chunk in request.stream():
                received_bytes += len(body_chunk)
                if received_bytes > settings.max_body_bytes:
                    raise _request_too_large(too_large_message)
                body_chunks.append(body_chunk)
    except TimeoutError as error:
        raise ChatFrontError(
            status_code=HTTPStatus.REQUEST_TIMEOUT,
            message=f"The request body was not received within {settings.body_read_timeout_seconds:g} seconds.",
            code=REQUEST_BODY_TIMEOUT_CODE,
        ) from error
    return b"".join(body_chunks)


def _format_location(location: tuple[int | str, ...], request_body: object) -> str:
    """Return a pydantic error location as a request path, such as ``messages[1].content``.

    A location also carries the union member or discriminator tag pydantic tried, such as ``str-enum[...]`` under
    ``tool_choice`` or ``json_schema`` under ``response_format``. Those are not keys the client sent, so a part is
    kept only where the body has it: an index of the array at that point, or a key of the object there. The last part
    is kept under any object, because a required key the body left out is reported by that key.

    Args:
        location: The pydantic error location.
        request_body: The decoded body the model was validated against.
    """
    request_path = ""
    body_at_path: object = request_body
    last_index = len(location) - 1
    for index, location_part in enumerate(location):
        if isinstance(location_part, int):
            if not isinstance(body_at_path, list) or not 0 <= location_part < len(body_at_path):
                continue
            request_path += f"[{location_part}]"
            body_at_path = body_at_path[location_part]
        else:
            if not isinstance(body_at_path, dict) or (location_part not in body_at_path and index != last_index):
                continue
            request_path += f".{location_part}" if request_path else location_part
            body_at_path = body_at_path.get(location_part)
    return request_path


def _validation_error(error: ValidationError, request_body: object) -> ChatFrontError:
    """Create the 400 for the first validation failure of a request body.

    Args:
        error: The pydantic failure.
        request_body: The decoded body the model was validated against, which anchors the error path.
    """
    first_error = error.errors()[0]
    error_location = _format_location(tuple(first_error["loc"]), request_body)
    code = BAD_REQUEST_CODE
    if first_error["type"] == UNSUPPORTED_PARAMETER_ERROR_TYPE:
        code = UNSUPPORTED_PARAMETER_CODE
    elif first_error["type"] == CHAT_REQUEST_CONFLICT_ERROR_TYPE:
        code = CHAT_REQUEST_CONFLICT_CODE
    return ChatFrontError(
        status_code=HTTPStatus.BAD_REQUEST,
        message=f"{error_location}: {first_error['msg']}" if error_location else first_error["msg"],
        code=code,
        param=error_location or None,
    )


def _message_text_bytes(chat_request: ChatCompletionRequest) -> int:
    """Return the UTF-8 size of every message text of ``chat_request``."""
    text_bytes = 0
    for message in chat_request.messages:
        if isinstance(message.content, str):
            text_bytes += len(message.content.encode())
        else:
            text_bytes += sum(len(content_part.text.encode()) for content_part in message.content)
    return text_bytes


async def _read_chat_request(request: Request, settings: FrontSettings) -> ChatCompletionRequest:
    """Return the validated chat request of ``request``.

    Raises:
        ChatFrontError: 413 for an oversized body, 408 for a slow body, 400 for invalid JSON, a refused part, a value outside the
            service contract or a size limit, and 404 for a model id with a variant.
    """
    request_body = await _read_body(request, settings)
    try:
        decoded_body = json.loads(request_body)
    except ValueError as error:
        raise ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST, message="The request body is not valid JSON.", code=BAD_REQUEST_CODE
        ) from error
    if not isinstance(decoded_body, dict):
        raise ChatFrontError(status_code=HTTPStatus.BAD_REQUEST, message="The request body must be a JSON object.", code=BAD_REQUEST_CODE)
    refused_parameter = find_refused_parameter(decoded_body)
    if refused_parameter is not None:
        raise ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST,
            message=refused_parameter.message,
            code=UNSUPPORTED_PARAMETER_CODE,
            param=refused_parameter.parameter,
        )
    validated_body = drop_default_no_ops(decoded_body)
    try:
        chat_request = ChatCompletionRequest.model_validate(validated_body)
    except ValidationError as error:
        raise _validation_error(error, validated_body) from error
    if len(chat_request.messages) > settings.max_message_count:
        raise ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST,
            message=f"A chat request may carry at most {settings.max_message_count} messages.",
            code=UNSUPPORTED_PARAMETER_CODE,
            param="messages",
        )
    if _message_text_bytes(chat_request) > settings.max_text_bytes:
        raise ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST,
            message=f"The message text of a chat request may be at most {settings.max_text_bytes} bytes.",
            code=UNSUPPORTED_PARAMETER_CODE,
            param="messages",
        )
    if MODEL_VARIANT_SEPARATOR in chat_request.model:
        raise _model_not_found(chat_request.model)
    return chat_request


def _model_not_found(model_id: str) -> ChatFrontError:
    """Create the 404 for an unknown model id."""
    return ChatFrontError(
        status_code=HTTPStatus.NOT_FOUND,
        message=f"The model '{model_id}' does not exist on the AI Horde.",
        code=MODEL_NOT_FOUND_CODE,
        param="model",
    )


async def _submit(
    front_state: FrontState,
    *,
    native_body: Mapping[str, object],
    credentials: HordeCredentials,
    deadline: float,
) -> HordeAsyncSubmission:
    """Submit a chat request within the time left in its budget and return the Horde's acknowledgement.

    Raises:
        ChatFrontError: The Horde refused the request, no root accepted the connection, or the outcome is unknown.
    """
    try:
        async with asyncio.timeout(deadline - time.monotonic()):
            return await front_state.horde_client.submit_chat(request_body=native_body, credentials=credentials)
    except HordeAPIError as error:
        front_error = _horde_error(error, request_id=None, job_may_exist=False)
        if error.status_code == HTTPStatus.SERVICE_UNAVAILABLE and front_error.metadata.retry_after is None:
            front_error.headers[RETRY_AFTER_HEADER] = str(HORDE_UNAVAILABLE_RETRY_AFTER_SECONDS)
            front_error.metadata = front_error.metadata.model_copy(update={"retry_after": HORDE_UNAVAILABLE_RETRY_AFTER_SECONDS})
        raise front_error from error
    except HordeUnavailableError as error:
        if error.request_sent:
            raise _submit_outcome_unknown(str(error)) from error
        raise ChatFrontError(status_code=HTTPStatus.BAD_GATEWAY, message=str(error), code=HORDE_UNAVAILABLE_CODE) from error
    except TimeoutError as error:
        raise _submit_outcome_unknown("The submit did not finish within the request's budget.") from error


def _submit_outcome_unknown(detail: str) -> ChatFrontError:
    """Create the 502 for a submit that was sent and never answered, so a Horde request may exist.

    Recovery by operation key belongs here: re-sending the identical body with the same key returns the existing
    request id, after which the wait continues as for any submit. Without it, the client is told not to retry.
    """
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=f"The AI Horde did not answer the submit, so the request may have been queued and charged. {detail}",
        code=HORDE_UNAVAILABLE_CODE,
        metadata=ErrorMetadata(submit_outcome=SubmitOutcome.UNKNOWN),
        job_may_exist=True,
    )


async def _cancel_within_budget(front_state: FrontState, open_request: OpenRequest) -> None:
    """Cancel a Horde chat request once, within the cancel budget, logging rather than raising when it fails.

    Every cancel logs exactly one outcome: cancelled, failed, or not completed within the budget. A request already
    cancelled or being cancelled is left alone, so each request receives at most one cancel.
    """
    if open_request.cancel_started:
        return
    open_request.cancel_started = True
    cancel_timeout_seconds = front_state.settings.cancel_timeout_seconds
    with anyio.move_on_after(cancel_timeout_seconds) as cancel_deadline:
        try:
            await front_state.horde_client.cancel_chat(request_id=open_request.request_id, credentials=open_request.credentials)
        except (HordeAPIError, HordeUnavailableError, httpx.HTTPError) as error:
            logger.warning(f"Cancelling chat request {open_request.request_id} failed: {error}")
            return
        logger.info(f"Cancelled chat request {open_request.request_id}")
    if cancel_deadline.cancelled_caught:
        logger.warning(f"Cancelling chat request {open_request.request_id} did not complete within {cancel_timeout_seconds:g}s")


async def _cancel_shielded(front_state: FrontState, open_request: OpenRequest) -> None:
    """Cancel a Horde chat request once, shielded from the caller's cancellation.

    The cancel is usually issued because the caller is being torn down after its client disconnected; the shield lets
    it finish within its own budget.
    """
    with anyio.CancelScope(shield=True):
        await _cancel_within_budget(front_state, open_request)


async def _cancel_open_requests(front_state: FrontState) -> None:
    """Cancel every open request within the process-wide shutdown bound."""
    pending_requests = [open_request for open_request in front_state.open_requests.values() if not open_request.cancel_started]
    if not pending_requests:
        return
    shutdown_cancel_seconds = front_state.settings.shutdown_cancel_seconds
    logger.info(f"Cancelling {len(pending_requests)} open chat requests before stopping")
    with anyio.move_on_after(shutdown_cancel_seconds) as shutdown_deadline:
        await asyncio.gather(*(_cancel_within_budget(front_state, open_request) for open_request in pending_requests))
    if shutdown_deadline.cancelled_caught:
        logger.warning(f"Cancelling open chat requests did not complete within {shutdown_cancel_seconds:g}s")


def _log_finished(watcher: ChatRequestWatcher, *, submitted_at: float, streamed: bool) -> None:
    """Log one line per finished request with its submit and wake times, for latency analysis."""
    woke_at = watcher.woke_at if watcher.woke_at is not None else time.time()
    logger.info(
        f"Chat request {watcher.request_id} finished: streamed={streamed} submitted_at={submitted_at:.3f} "
        f"woke_at={woke_at:.3f} woke_by={watcher.woke_by} waited={woke_at - submitted_at:.3f}s",
    )


async def _wait_for_disconnect(request: Request) -> None:
    """Return once the client of ``request`` disconnects; the request body must already be read."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _create_watcher(
    front_state: FrontState,
    *,
    open_request: OpenRequest,
    subscription: CompletionSubscription,
    deadline: float,
) -> ChatRequestWatcher:
    """Create the watcher of a submitted request."""
    settings = front_state.settings
    return ChatRequestWatcher(
        horde_client=front_state.horde_client,
        credentials=open_request.credentials,
        subscription=subscription,
        budget=WaitBudget(
            deadline=deadline,
            final_status_timeout_seconds=settings.final_status_timeout_seconds,
            fallback_poll_seconds=settings.fallback_poll_seconds,
            signal_recheck_seconds=settings.signal_recheck_seconds,
            fail_fast_grace_seconds=settings.fail_fast_grace_seconds,
        ),
    )


def _wait_error(error: ChatWaitTimeoutError | NoEligibleWorkerError | HordeAPIError, *, request_id: str) -> ChatFrontError:
    """Create the front error for a wait that ended without a terminal outcome."""
    if isinstance(error, ChatWaitTimeoutError):
        return ChatFrontError(
            status_code=HTTPStatus.REQUEST_TIMEOUT,
            message="The request was still waiting for a worker at its deadline and was cancelled.",
            code=TIMEOUT_CODE,
            metadata=ErrorMetadata(request_id=request_id),
            job_may_exist=True,
        )
    if isinstance(error, NoEligibleWorkerError):
        return ChatFrontError(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            message="No online worker can serve this request at the moment; it was cancelled. Retry later.",
            code=NO_ELIGIBLE_WORKER_CODE,
            metadata=ErrorMetadata(request_id=request_id, retry_after=NO_ELIGIBLE_WORKER_RETRY_AFTER_SECONDS),
            headers={RETRY_AFTER_HEADER: str(NO_ELIGIBLE_WORKER_RETRY_AFTER_SECONDS)},
            job_may_exist=True,
        )
    return _horde_error(error, request_id=request_id, job_may_exist=True)


def _faulted_error(request_id: str) -> ChatFrontError:
    """Create the front error for a request the Horde reports as faulted."""
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=f"The AI Horde reports request {request_id} as faulted: the worker failed to generate it.",
        code=FAULTED_CODE,
        metadata=ErrorMetadata(request_id=request_id),
        job_may_exist=True,
    )


async def _chat_completions(request: Request) -> Response:
    """Serve ``POST /v1/chat/completions``."""
    front_state = _front_state(request)
    settings = front_state.settings
    try:
        chat_request = await _read_chat_request(request, settings)
        credentials = _build_credentials(request)
    except ChatFrontError as error:
        return error.to_response()
    inflight_slot = front_state.inflight_limiter.try_acquire(_request_source(request))
    if inflight_slot is None:
        return ChatFrontError(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            message="This front holds its maximum of open requests for this client or in total; retry shortly.",
            code=FRONT_FULL_CODE,
            metadata=ErrorMetadata(retry_after=INFLIGHT_RETRY_AFTER_SECONDS),
            headers={RETRY_AFTER_HEADER: str(INFLIGHT_RETRY_AFTER_SECONDS)},
        ).to_response()
    budget_seconds = settings.stream_timeout_seconds if chat_request.stream else settings.nonstream_timeout_seconds
    deadline = time.monotonic() + budget_seconds
    try:
        submission = await _submit(
            front_state, native_body=build_native_chat_body(chat_request), credentials=credentials, deadline=deadline
        )
    except ChatFrontError as error:
        inflight_slot.release()
        return error.to_response()
    except BaseException:
        inflight_slot.release()
        raise
    submitted_at = time.time()
    open_request = front_state.open_request(request_id=submission.request_id, credentials=credentials)
    if chat_request.stream:
        try:
            return _stream_completion(
                request,
                chat_request=chat_request,
                open_request=open_request,
                submitted_at=submitted_at,
                deadline=deadline,
                inflight_slot=inflight_slot,
            )
        except BaseException:
            inflight_slot.release()
            await _cancel_shielded(front_state, open_request)
            front_state.close_request(open_request)
            raise
    try:
        return await _wait_for_completion(
            request,
            chat_request=chat_request,
            open_request=open_request,
            submitted_at=submitted_at,
            deadline=deadline,
            inflight_slot=inflight_slot,
        )
    except asyncio.CancelledError:
        inflight_slot.release()
        # A separate task survives repeated Task.cancel() calls; the AnyIO shield also protects against cancellation
        # scopes during ASGI teardown. The cancel has its own bounded budget and finishes before cleanup returns.
        cancel_task = asyncio.create_task(_cancel_within_budget(front_state, open_request))
        with anyio.CancelScope(shield=True):
            while not cancel_task.done():
                try:
                    await asyncio.shield(cancel_task)
                except asyncio.CancelledError:
                    continue
        cancel_task.result()
        raise
    finally:
        inflight_slot.release()
        front_state.close_request(open_request)


async def _wait_for_completion(
    request: Request,
    *,
    chat_request: ChatCompletionRequest,
    open_request: OpenRequest,
    submitted_at: float,
    deadline: float,
    inflight_slot: InflightSlot,
) -> Response:
    """Wait for a submitted request and return its completion, or the error that ended the wait.

    The in-flight slot is released before an abandoned request's cancel, so a slow cancel does not count against new
    requests; the cancel's own duration is bounded by the cancel budget.
    """
    front_state = _front_state(request)
    request_id = open_request.request_id
    with front_state.listener.register(request_id) as subscription:
        watcher = _create_watcher(
            front_state,
            open_request=open_request,
            subscription=subscription,
            deadline=deadline,
        )
        wait_task = asyncio.create_task(watcher.wait_until_finished())
        disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
        try:
            await asyncio.wait({wait_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            disconnect_task.cancel()
            if not wait_task.done():
                wait_task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(wait_task, disconnect_task, return_exceptions=True)
        if wait_task.cancelled():
            logger.info(f"Client of chat request {request_id} disconnected while waiting")
            inflight_slot.release()
            await _cancel_shielded(front_state, open_request)
            return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
        try:
            status = wait_task.result()
        except (ChatWaitTimeoutError, NoEligibleWorkerError) as error:
            inflight_slot.release()
            await _cancel_shielded(front_state, open_request)
            return _wait_error(error, request_id=request_id).to_response()
        except HordeAPIError as error:
            return _wait_error(error, request_id=request_id).to_response()
    _log_finished(watcher, submitted_at=submitted_at, streamed=False)
    if status.faulted:
        return _faulted_error(request_id).to_response()
    completion = build_completion(
        request_id=request_id,
        created=int(submitted_at),
        chat_request=chat_request,
        status=status,
    )
    return JSONResponse(content=completion.model_dump(mode="json"))


def _stream_completion(
    request: Request,
    *,
    chat_request: ChatCompletionRequest,
    open_request: OpenRequest,
    submitted_at: float,
    deadline: float,
    inflight_slot: InflightSlot,
) -> EventSourceResponse:
    """Return the server-sent event response that waits for a submitted request and then delivers it.

    The in-flight slot is released before an abandoned request's cancel. When the client disconnects before the event
    generator first runs, sse-starlette never starts it, so the response's background task releases the slot and
    cancels the request instead.
    """
    front_state = _front_state(request)
    settings = front_state.settings
    request_id = open_request.request_id
    latest_watcher: ChatRequestWatcher | None = None
    stream_started = False

    def build_heartbeat() -> ServerSentEvent:
        latest_status: HordeChatStatus | None = latest_watcher.latest_status if latest_watcher is not None else None
        return build_keepalive_event(latest_status)

    async def stream_events() -> AsyncIterator[ServerSentEvent]:
        nonlocal latest_watcher, stream_started
        stream_started = True
        outcome_settled = False
        try:
            with front_state.listener.register(request_id) as subscription:
                latest_watcher = _create_watcher(
                    front_state,
                    open_request=open_request,
                    subscription=subscription,
                    deadline=deadline,
                )
                try:
                    status = await latest_watcher.wait_until_finished()
                except (ChatWaitTimeoutError, NoEligibleWorkerError) as error:
                    inflight_slot.release()
                    await _cancel_shielded(front_state, open_request)
                    for error_event in _wait_error(error, request_id=request_id).to_events():
                        yield error_event
                    return
                except HordeAPIError as error:
                    outcome_settled = True
                    for error_event in _wait_error(error, request_id=request_id).to_events():
                        yield error_event
                    return
            outcome_settled = True
            _log_finished(latest_watcher, submitted_at=submitted_at, streamed=True)
            if status.faulted:
                for error_event in _faulted_error(request_id).to_events():
                    yield error_event
                return
            completion = build_completion(
                request_id=request_id,
                created=int(submitted_at),
                chat_request=chat_request,
                status=status,
            )
            include_usage = bool(chat_request.stream_options and chat_request.stream_options.include_usage)
            for completion_event in build_completion_events(completion, include_usage=include_usage):
                yield completion_event
        finally:
            inflight_slot.release()
            if not outcome_settled:
                if not open_request.cancel_started:
                    logger.info(f"Client of streamed chat request {request_id} disconnected while waiting")
                await _cancel_shielded(front_state, open_request)
            front_state.close_request(open_request)

    async def finish_unstarted_stream() -> None:
        inflight_slot.release()
        if not stream_started:
            logger.info(f"Client of streamed chat request {request_id} disconnected before the stream started")
            await _cancel_shielded(front_state, open_request)
        front_state.close_request(open_request)

    return EventSourceResponse(
        stream_events(),
        headers=STREAM_HEADERS,
        ping=settings.heartbeat_seconds,
        ping_message_factory=build_heartbeat,
        background=BackgroundTask(finish_unstarted_stream),
    )


def _build_model_card(active_model: HordeActiveModel) -> ModelCard:
    """Create the OpenAI model list entry for one of the Horde's active text models."""
    return ModelCard(
        model_id=active_model.name,
        x_horde=HordeModelDetails(workers=active_model.count, queued=active_model.queued),
    )


async def _fetch_models(request: Request) -> list[HordeActiveModel]:
    """Return the Horde's active text models through the process cache.

    Raises:
        ChatFrontError: The Authorization header is malformed, or the Horde could not be asked.
    """
    front_state = _front_state(request)
    credentials = _build_credentials(request)
    try:
        return await front_state.model_list_cache.get(front_state.horde_client, credentials)
    except HordeAPIError as error:
        raise _horde_error(error, request_id=None, job_may_exist=False) from error
    except HordeUnavailableError as error:
        raise ChatFrontError(status_code=HTTPStatus.BAD_GATEWAY, message=str(error), code=HORDE_UNAVAILABLE_CODE) from error


async def _list_models(request: Request) -> Response:
    """Serve ``GET /v1/models``."""
    try:
        active_models = await _fetch_models(request)
    except ChatFrontError as error:
        return error.to_response()
    model_list = ModelList(data=[_build_model_card(active_model) for active_model in active_models])
    return JSONResponse(content=model_list.model_dump(mode="json"))


async def _get_model(request: Request, model_id: str) -> Response:
    """Serve ``GET /v1/models/{model_id}``; the id may contain slashes."""
    try:
        active_models = await _fetch_models(request)
    except ChatFrontError as error:
        return error.to_response()
    for active_model in active_models:
        if active_model.name == model_id:
            return JSONResponse(content=_build_model_card(active_model).model_dump(mode="json"))
    return _model_not_found(model_id).to_response()


async def _health(request: Request) -> Response:
    """Serve ``GET /healthz``: liveness, admission readiness and the signal subscription, as separate fields."""
    front_state = _front_state(request)
    return JSONResponse(
        content={
            "live": True,
            "ready": front_state.accepting and front_state.inflight_limiter.has_capacity,
            "signal_subscribed": front_state.listener.subscribed,
            "inflight": front_state.inflight_limiter.inflight,
        },
    )
