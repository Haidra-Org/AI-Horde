# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The FastAPI application serving the OpenAI-compatible chat completions front.

[`create_app`][horde.oai.app.create_app] builds the app with these routes:

- ``POST /v1/chat/completions``: takes an inflight slot (per-source cap first, then the global cap), reads the body
  under a byte ceiling and a read timeout, screens and validates it, submits to the Horde's
  ``/v2/generate/chat/async``, waits through [`ChatRequestWatcher`][horde.oai.waiter.ChatRequestWatcher] and answers
  with a ``chat.completion`` body, or with server-sent events when ``stream`` is true.
- ``GET /v1/models`` and ``GET /v1/models/{id}``: the text models an online chat worker serves, in the OpenAI list
  shape with OpenRouter's limit and parameter fields; an id may contain slashes.
- ``GET /healthz``: liveness, admission readiness and whether the completion signal subscription is up.

Every error before headers is an OpenAI error envelope with a ``metadata`` object; after stream headers it is one SSE
error event. Each client request gets a fresh operation key, sent as ``horde.operation_key``; a submit whose answer was
lost is sent again with the same key and body, which the Horde answers with the request the key already claimed. Once
the Horde holds a request, or may hold one, every error carries ``x-should-retry: false`` so a client SDK does not
create a second charged job; a successful stream carries it too.
Cancels on disconnect, deadline, fail-fast, a status read the wait cannot fix and shutdown are bounded, shielded from
the client's disconnect and use the client's separate cancel pool. A request's handling sends at most one cancel; the
shutdown drain sends a failed one once more, never alongside one in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from enum import StrEnum
from http import HTTPStatus
from types import MappingProxyType

import anyio
import httpx
import redis.asyncio
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from horde_sdk.generation_parameters.text.chat_policy import ChatGuarantee
from loguru import logger
from opentelemetry.metrics import Meter
from pydantic import ValidationError
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from horde.chat_contract import (
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    MAX_CHAT_IMAGE_BYTES,
    MAX_CHAT_IMAGE_COUNT,
    ChatImpossibleReason,
    ChatRequestRefusal,
    NonJsonConstantError,
    chat_tool_call_bytes,
    check_json_word,
    check_strict_response_schema,
    check_strict_tool_schemas,
    check_tool_fields_need_tools,
    check_tool_history,
    refuse_json_constant,
)
from horde.consts import ANONYMOUS_API_KEY, HORDE_VERSION
from horde.oai.attribution import build_client_agent
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
from horde.oai.mapping import (
    CompletionWithoutGenerationError,
    build_completion,
    build_error_envelope,
    build_model_card,
    build_native_chat_body,
)
from horde.oai.metrics import CancelOutcome, FrontMetrics, get_front_meter
from horde.oai.schema import (
    BAD_REQUEST_CODE,
    CHAT_REQUEST_CONFLICT_CODE,
    PARAMETER_CONTEXT_KEY,
    UNSUPPORTED_PARAMETER_CODE,
    UNSUPPORTED_PARAMETER_ERROR_TYPE,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ErrorMetadata,
    ImageContentPart,
    ModelCard,
    ModelList,
    OpenAIErrorEnvelope,
    SubmitOutcome,
    TextContentPart,
    drop_default_no_ops,
    drop_unknown_nested_keys,
    find_refused_parameter,
)
from horde.oai.settings import FrontSettings
from horde.oai.sse import build_completion_events, build_error_events, build_keepalive_event
from horde.oai.waiter import (
    TRANSIENT_STATUS_CODES,
    ChatDoneWithoutGenerationError,
    ChatRequestWatcher,
    ChatWaitTimeoutError,
    CompletionSignalListener,
    CompletionSubscription,
    DeclarationGapError,
    NoEligibleWorkerError,
    WaitBudget,
    WakeSource,
)

__all__ = [
    "CancelState",
    "ChatFrontError",
    "FrontState",
    "InflightLimiter",
    "InflightSlot",
    "OpenRequest",
    "RequestTrace",
    "create_app",
]

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

CORS_PREFLIGHT_MAX_AGE_SECONDS: int = 600
"""How long a browser may reuse a preflight answer; the allowed set never changes per request, so it is the
longest value browsers honour."""

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

CLIENT_CLOSED_CODE: str = "client_closed"
"""The code recorded for a request whose client disconnected before delivery; no client receives it."""

COMPLETED_CODE: str = "ok"
"""The code recorded for a request whose completion was delivered."""

TIMEOUT_CODE: str = "ChatRequestTimeout"
"""The error code of a request still waiting after its deadline and the final status read."""

FAULTED_CODE: str = "ChatRequestFaulted"
"""The error code of a request the Horde reports as faulted."""

NO_GENERATION_CODE: str = "ChatRequestNoGeneration"
"""The error code of a request the Horde settled done, not faulted and without a finished generation, such as one
cancelled before any generation, which leaves no reply to deliver."""

WORKER_FAULT_RC: str = "ChatWorkerFault"
"""The return code the Horde stores with a faulted request's reason when a worker reported why it faulted a job,
rather than the Horde refusing its reply."""

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

NO_WORKER_SUPPORTS_FEATURES_CODE: str = "no_worker_supports_features"
"""The error code of a request the online workers serving its model cannot take because of what they declare: a
capability or guarantee none declares, or a declared limit the request exceeds. OpenRouter answers the same case,
no endpoint supporting the requested features, with a 404."""

GUARANTEE_NAMES: frozenset[str] = frozenset(guarantee.value for guarantee in ChatGuarantee)
"""The declaration names that are guarantees, which ``horde.conformance: best_effort`` stops requiring."""

OPERATION_KEY_CONFLICT_CODE: str = "OperationKeyConflict"
"""The Horde rc for an operation key already claimed with a different request."""

IMAGE_SIZE_EXCEEDED_CODE: str = "SourceImageSizeExceeded"
"""The error code of an image data URL decoding to more than the native per-image cap; the native rc for the same."""

TOO_MANY_IMAGES_CODE: str = "TooManyExtraSourceImages"
"""The error code of a request with more image parts than the native cap; the native rc for the same."""

DATA_URL_PREFIX: str = "data:"
"""The scheme prefix of an inline image URL."""

DATA_URL_PAYLOAD_SEPARATOR: str = ","
"""The character that ends a data URL's media type and starts its payload."""

NATIVE_REQUEST_PATH_PATTERN: re.Pattern[str] = re.compile(r"'(?P<request_path>messages\[\d+\][^']*)'")
"""A quoted message path in a native error message, such as ``'messages[0].content[1].type=image_url'``; the
native route writes the offending path this way when it refuses a part."""

NATIVE_FIELD_PARAMETERS: Mapping[str, str] = MappingProxyType(
    {
        "max_length": "max_tokens",
        "max_context_length": "horde.max_context_length",
        "stop_sequence": "stop",
    },
)
"""The request key of each native text param whose name differs from it. The native route checks a chat request's
sampling values against the text params model, so a failure there names the text param."""

MAX_FAILED_CANCELS: int = 10_000
"""The most finished requests with a failed cancel held for the shutdown drain; the oldest is dropped past it.

Cancels fail together during a long Horde outage, and each held record keeps a request id and the client's
credentials in memory until the process stops.
"""

CLIENT_RETRY_COUNT_HEADER: str = "x-stainless-retry-count"
"""The request header OpenAI SDKs send with how many times they retried the call; a hint, never an identity."""


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

    @property
    def source_count(self) -> int:
        """Return how many distinct sources hold at least one request open."""
        return len(self._inflight_by_source)

    @property
    def largest_source_inflight(self) -> int:
        """Return the most requests one source holds open.

        Concurrency:
            The metric export thread reads it while the event loop updates the counts; ``tuple`` copies the values in
            one call under the GIL, so the iteration does not see the dictionary change size.
        """
        return max(tuple(self._inflight_by_source.values()), default=0)

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


class CancelState(StrEnum):
    """How far the cancel of one submitted Horde request got."""

    NOT_ATTEMPTED = "not_attempted"
    """No cancel was sent."""
    IN_PROGRESS = "in_progress"
    """A cancel is being sent; no second one starts until it ends."""
    ACKNOWLEDGED = "acknowledged"
    """The Horde answered the cancel with success."""
    FAILED = "failed"
    """The cancel was refused, unreachable or not answered within its budget; the shutdown drain sends it once more."""


class OpenRequest:
    """A submitted Horde request the process may still have to cancel.

    The request's own handling sends at most one cancel; the shutdown drain sends one more only when that one failed.
    """

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
        self.cancel_state: CancelState = CancelState.NOT_ATTEMPTED
        """How far the request's cancel got."""


class ModelListCache:
    """Hold the Horde's chat model list for [`MODEL_LIST_CACHE_SECONDS`][horde.oai.app.MODEL_LIST_CACHE_SECONDS]."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._models: list[HordeActiveModel] | None = None
        self._fetched_at: float = -math.inf
        self._refresh_lock = asyncio.Lock()

    async def get(self, horde_client: HordeClient, credentials: HordeCredentials) -> list[HordeActiveModel]:
        """Return the cached model list, fetching it when it is older than the cache period.

        Raises:
            HordeAPIError: The Horde answered with an error.
            HordeUnavailableError: No Horde root accepted the connection.
        """
        async with self._refresh_lock:
            if self._models is None or time.monotonic() - self._fetched_at >= MODEL_LIST_CACHE_SECONDS:
                self._models = await horde_client.list_chat_models(credentials=credentials)
                self._fetched_at = time.monotonic()
            return self._models


class FrontState:
    """Hold the process-wide objects the routes share."""

    def __init__(
        self,
        *,
        settings: FrontSettings,
        horde_client: HordeClient,
        listener: CompletionSignalListener,
        front_metrics: FrontMetrics,
    ) -> None:
        """Create the state and register the in-flight gauges on ``front_metrics``.

        Args:
            settings: The front's settings.
            horde_client: The Horde client.
            listener: The completion signal listener.
            front_metrics: The front's instruments.
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
        self.failed_cancels: dict[str, OpenRequest] = {}
        """The finished requests whose cancel failed, oldest first, by Horde request id; a clean stop sends each cancel
        once more. At most [`MAX_FAILED_CANCELS`][horde.oai.app.MAX_FAILED_CANCELS] are held."""
        self.model_list_cache: ModelListCache = ModelListCache()
        """The cached Horde chat model list."""
        self.front_metrics: FrontMetrics = front_metrics
        """The front's instruments."""
        front_metrics.observe_inflight(self.inflight_limiter)
        self.accepting: bool = True
        """False once the process is stopping; ``/healthz`` then reports it not ready."""

    def open_request(self, *, request_id: str, credentials: HordeCredentials) -> OpenRequest:
        """Create and register the record of a submitted request."""
        open_request = OpenRequest(request_id=request_id, credentials=credentials)
        self.open_requests[request_id] = open_request
        return open_request

    def close_request(self, open_request: OpenRequest) -> None:
        """Mutate the registry by removing a finished request; removing it twice is harmless.

        A request whose cancel failed moves to ``failed_cancels``, so the shutdown drain can send its cancel once more.
        """
        if self.open_requests.get(open_request.request_id) is not open_request:
            return
        del self.open_requests[open_request.request_id]
        if open_request.cancel_state != CancelState.FAILED:
            return
        while len(self.failed_cancels) >= MAX_FAILED_CANCELS:
            evicted_request_id = next(iter(self.failed_cancels))
            del self.failed_cancels[evicted_request_id]
            logger.warning(
                f"Dropped the failed cancel of chat request {evicted_request_id}: {MAX_FAILED_CANCELS} failed cancels are "
                "already held, so the shutdown drain does not retry it",
            )
        self.failed_cancels[open_request.request_id] = open_request


def create_app(
    settings: FrontSettings,
    *,
    http_client: httpx.AsyncClient | None = None,
    cancel_http_client: httpx.AsyncClient | None = None,
    redis_client: redis.asyncio.Redis | None = None,
    meter: Meter | None = None,
) -> FastAPI:
    """Create the chat front application.

    Args:
        settings: The front's settings.
        http_client: The HTTP client for submits, status reads and model lists; None creates one sized by
            ``settings.native_max_connections`` that the app closes on shutdown.
        cancel_http_client: The HTTP client for cancels; None creates a separate small pool the app closes on shutdown.
        redis_client: The client for the Horde's chat signal database; None creates one from ``settings.redis_url``
            when it is set, and otherwise requests wait on the fallback poll alone.
        meter: The meter the front's instruments are created on; None takes it from the global meter provider, which
            ``logfire.configure`` installs.

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
    front_metrics = FrontMetrics(meter if meter is not None else get_front_meter())
    front_state = FrontState(
        settings=settings,
        horde_client=HordeClient(
            horde_urls=settings.horde_urls,
            http_client=http_client,
            cancel_http_client=cancel_http_client,
            proxy_passkey=settings.proxy_passkey,
            front_metrics=front_metrics,
            max_concurrent_calls=settings.native_max_connections,
        ),
        listener=CompletionSignalListener(redis_client, liveness_seconds=settings.redis_liveness_seconds),
        front_metrics=front_metrics,
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
    # Browser clients such as KoboldAI Lite call the front directly with a bearer key, so every origin is allowed, as
    # the Horde API itself is served; a bearer key is not a cookie, so no credentials flag is needed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[RETRY_AFTER_HEADER, SHOULD_RETRY_HEADER],
        max_age=CORS_PREFLIGHT_MAX_AGE_SECONDS,
    )
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
    """Create the front error for a Horde error, keeping its status, ``rc`` and forwarded headers.

    A native payload validation failure, which has no ``rc``, is reported as ``BadRequest`` with its first failing
    field as ``param``.
    """
    retry_after_header = next(
        (
            header_value
            for header_name, header_value in error.forwarded_headers.items()
            if header_name.lower() == RETRY_AFTER_HEADER.lower()
        ),
        None,
    )
    request_path_match = NATIVE_REQUEST_PATH_PATTERN.search(error.message)
    message = error.message
    code = error.return_code
    param = request_path_match.group("request_path") if request_path_match is not None else None
    if param is None and error.field_errors:
        # A native payload validation failure carries only a generic message; the first failing field and its
        # reason tell the client which parameter to change.
        native_path, field_message = next(iter(error.field_errors.items()))
        param = _request_path_of_native_field(native_path) or None
        message = f"{param}: {field_message}" if param is not None else field_message
        code = code if code is not None else BAD_REQUEST_CODE
    return ChatFrontError(
        status_code=error.status_code,
        message=message,
        code=code,
        param=param,
        metadata=ErrorMetadata(
            request_id=request_id,
            rc=error.return_code,
            retry_after=int(retry_after_header) if retry_after_header is not None and retry_after_header.isdigit() else None,
        ),
        headers=error.forwarded_headers,
        job_may_exist=job_may_exist,
    )


def _request_path_of_native_field(native_path: str) -> str:
    """Return the request path of a dotted native field path, such as ``messages[0].content`` for
    ``messages.0.content`` or ``max_tokens`` for ``max_length``."""
    request_path = ""
    for path_part in native_path.split("."):
        if path_part.isdigit():
            request_path += f"[{path_part}]"
        elif request_path:
            request_path += f".{path_part}"
        else:
            request_path = NATIVE_FIELD_PARAMETERS.get(path_part, path_part)
    return request_path


def _build_credentials(request: Request) -> HordeCredentials:
    """Return the Horde identity for ``request``: its bearer API key (or the anonymous key), its attributed
    ``Client-Agent`` and its address.

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
    return HordeCredentials(
        apikey=apikey,
        client_agent=build_client_agent(
            user_agent=request.headers.get("user-agent"),
            referer=request.headers.get("http-referer"),
            title=request.headers.get("x-title"),
            openrouter_title=request.headers.get("x-openrouter-title"),
            standard_referer=request.headers.get("referer"),
        ),
        client_address=request.client.host if request.client is not None else None,
    )


def _carries_client_retry_hint(request: Request) -> bool:
    """Return whether ``request`` carries ``x-stainless-retry-count`` above zero; a malformed value counts as none."""
    retry_count = request.headers.get(CLIENT_RETRY_COUNT_HEADER, "").strip()
    return retry_count.isdigit() and int(retry_count) > 0


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

    The message is prefixed with the request path unless it already names the parameter in OpenAI's wording.

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
    names_parameter = PARAMETER_CONTEXT_KEY in first_error.get("ctx", {})
    return ChatFrontError(
        status_code=HTTPStatus.BAD_REQUEST,
        message=f"{error_location}: {first_error['msg']}" if error_location and not names_parameter else first_error["msg"],
        code=code,
        param=error_location or None,
    )


def _message_texts(chat_request: ChatCompletionRequest) -> Iterator[str]:
    """Yield every string content and text part of the messages of ``chat_request``, in order."""
    for message in chat_request.messages:
        if isinstance(message.content, str):
            yield message.content
        elif message.content is not None:
            yield from (content_part.text for content_part in message.content if isinstance(content_part, TextContentPart))


def _message_text_bytes(chat_request: ChatCompletionRequest) -> int:
    """Return the UTF-8 size of every message text of ``chat_request`` and of the function names and arguments of
    its history tool calls, as the native route counts them."""
    text_bytes = sum(len(message_text.encode()) for message_text in _message_texts(chat_request))
    return text_bytes + sum(chat_tool_call_bytes(message.tool_calls) for message in chat_request.messages)


def _find_contract_refusal(chat_request: ChatCompletionRequest) -> ChatFrontError | None:
    """Return the 400 for a request one of the request rules shared with the native route refuses, or None.

    Refusing here gives the client OpenAI's message and ``param``, where the native route's refusal would arrive
    without a ``param`` for keys outside the messages.
    """
    try:
        check_tool_fields_need_tools(
            offers_tools=bool(chat_request.tools),
            tool_choice_set=chat_request.tool_choice is not None,
            parallel_tool_calls_set=chat_request.parallel_tool_calls is not None,
        )
        check_tool_history(chat_request.messages)
        check_strict_tool_schemas(chat_request.tools)
        check_strict_response_schema(chat_request.response_format)
        openai_guards = chat_request.horde is not None and chat_request.horde.openai_guards
        check_json_word(_message_texts(chat_request), chat_request.response_format, openai_guards=openai_guards)
    except ChatRequestRefusal as refusal:
        return ChatFrontError(status_code=HTTPStatus.BAD_REQUEST, message=refusal.message, code=refusal.rc, param=refusal.param)
    return None


def _find_image_limit_error(chat_request: ChatCompletionRequest) -> ChatFrontError | None:
    """Return the 400 for more image parts or a larger image data URL than the native route accepts, or None.

    Refusing here keeps a body the native route would refuse from being forwarded. A data URL's decoded size is
    estimated from its payload length, as the native route does; any other malformation is left to the native route.
    """
    image_count = 0
    for message_index, message in enumerate(chat_request.messages):
        if message.content is None or isinstance(message.content, str):
            continue
        for part_index, content_part in enumerate(message.content):
            if not isinstance(content_part, ImageContentPart):
                continue
            image_count += 1
            image_url = content_part.image_url.url
            if not image_url.startswith(DATA_URL_PREFIX):
                continue
            _media_type, _separator, payload = image_url.partition(DATA_URL_PAYLOAD_SEPARATOR)
            # Four base64 characters encode three bytes, so the estimate is at most two bytes over the decoded size.
            if len(payload) * 3 // 4 > MAX_CHAT_IMAGE_BYTES:
                return ChatFrontError(
                    status_code=HTTPStatus.BAD_REQUEST,
                    message=f"An image cannot be larger than {MAX_CHAT_IMAGE_BYTES} bytes.",
                    code=IMAGE_SIZE_EXCEEDED_CODE,
                    param=f"messages[{message_index}].content[{part_index}].image_url.url",
                )
    if image_count > MAX_CHAT_IMAGE_COUNT:
        return ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST,
            message=f"A chat request can carry at most {MAX_CHAT_IMAGE_COUNT} images.",
            code=TOO_MANY_IMAGES_CODE,
            param="messages",
        )
    return None


async def _read_chat_request(request: Request, front_state: FrontState) -> ChatCompletionRequest:
    """Return the validated chat request of ``request`` and record the size of its body.

    Raises:
        ChatFrontError: 413 for an oversized body, 408 for a slow body, 400 for invalid JSON, a refused part, a value outside the
            service contract, a size limit or an image limit, and 404 for a model id with a variant.
    """
    settings = front_state.settings
    request_body = await _read_body(request, settings)
    front_state.front_metrics.record_body_bytes(len(request_body))
    try:
        decoded_body = json.loads(request_body, parse_constant=refuse_json_constant)
    except NonJsonConstantError as error:
        raise ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST,
            message=f"The request body is not valid JSON: {error.constant} is not a JSON value.",
            code=BAD_REQUEST_CODE,
        ) from error
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
    validated_body = drop_unknown_nested_keys(drop_default_no_ops(decoded_body))
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
            message=(
                "The message text of a chat request, tool call names and arguments included, may be at most "
                f"{settings.max_text_bytes} bytes."
            ),
            code=UNSUPPORTED_PARAMETER_CODE,
            param="messages",
        )
    contract_refusal = _find_contract_refusal(chat_request)
    if contract_refusal is not None:
        raise contract_refusal
    image_limit_error = _find_image_limit_error(chat_request)
    if image_limit_error is not None:
        raise image_limit_error
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
    operation_key: uuid.UUID,
    credentials: HordeCredentials,
    deadline: float,
) -> HordeAsyncSubmission:
    """Submit a chat request within the time left in its budget and return the Horde's acknowledgement.

    A refusal of the first submit, or no root accepting its connection, means the request was not queued. A submit
    that was sent without an answer leaves the outcome unknown and is recovered by ``_recover_submit``.

    Args:
        front_state: The process state.
        native_body: The native body, carrying ``operation_key`` as ``horde.operation_key``.
        operation_key: The key of this client request.
        credentials: Who the call is made for.
        deadline: The monotonic time the request's total budget ends.

    Raises:
        ChatFrontError: The Horde refused the request, no root accepted the connection, or the outcome stayed unknown.
    """
    try:
        return await _send_submit(front_state, native_body=native_body, credentials=credentials, deadline=deadline, recovering=False)
    except HordeAPIError as error:
        # These native refusals happen before activation. Other server errors, including a gateway's failure
        # or an unreadable success body, cannot establish whether the operation key was committed.
        native_refusal = error.status_code == HTTPStatus.SERVICE_UNAVAILABLE and error.return_code in {
            "ChatAdmissionDisabled",
            "ChatImageStorageUnavailable",
        }
        if error.status_code < HTTPStatus.INTERNAL_SERVER_ERROR or native_refusal:
            front_error = _horde_error(error, request_id=None, job_may_exist=False)
            if error.status_code == HTTPStatus.SERVICE_UNAVAILABLE and front_error.metadata.retry_after is None:
                front_error.headers[RETRY_AFTER_HEADER] = str(HORDE_UNAVAILABLE_RETRY_AFTER_SECONDS)
                front_error.metadata = front_error.metadata.model_copy(update={"retry_after": HORDE_UNAVAILABLE_RETRY_AFTER_SECONDS})
            raise front_error from error
        lost_detail = f"The submit was answered {error.status_code}: {error.message}"
    except HordeUnavailableError as error:
        if not error.request_sent:
            raise ChatFrontError(status_code=HTTPStatus.BAD_GATEWAY, message=str(error), code=HORDE_UNAVAILABLE_CODE) from error
        lost_detail = str(error)
    except TimeoutError:
        lost_detail = "The submit did not finish within the request's budget."
    return await _recover_submit(
        front_state,
        native_body=native_body,
        operation_key=operation_key,
        credentials=credentials,
        deadline=deadline,
        lost_detail=lost_detail,
    )


async def _send_submit(
    front_state: FrontState,
    *,
    native_body: Mapping[str, object],
    credentials: HordeCredentials,
    deadline: float,
    recovering: bool,
) -> HordeAsyncSubmission:
    """Send one submit, cut off at the end of the request's budget.

    Raises:
        HordeAPIError: The Horde answered with an error.
        HordeUnavailableError: No root accepted the connection, or the connection failed after the call was sent.
        TimeoutError: The budget ended before the Horde answered.
    """
    async with asyncio.timeout(deadline - time.monotonic()):
        return await front_state.horde_client.submit_chat(
            request_body=native_body,
            credentials=credentials,
            fall_back_to_unhealthy_roots=recovering,
        )


async def _recover_submit(
    front_state: FrontState,
    *,
    native_body: Mapping[str, object],
    operation_key: uuid.UUID,
    credentials: HordeCredentials,
    deadline: float,
    lost_detail: str,
) -> HordeAsyncSubmission:
    """Send a submit whose answer was lost again, with the same body and key, and return the Horde's acknowledgement.

    The native route answers a repeated key with the request it already claimed (200), or queues the request when the
    lost submit never created it (202), so a re-send cannot queue a second job. Re-sends stop at the first answer, at
    ``submit_recovery_attempts`` or at the end of the budget. A failed connection or a transient Horde error moves on
    to the next attempt; any other refusal ends recovery, since the lost submit may still have queued the request.

    Args:
        front_state: The process state.
        native_body: The body of the lost submit.
        operation_key: The key that body carries.
        credentials: Who the call is made for.
        deadline: The monotonic time the request's total budget ends.
        lost_detail: Why the lost submit got no answer, reported when recovery fails too.

    Raises:
        ChatFrontError: 502 when the Horde reports the key claimed with a different request, the Horde's refusal for
            any other definitive refusal, and 502 with the outcome unknown when no attempt was answered; each with
            ``x-should-retry: false`` and the operation key in ``metadata``.
    """
    for attempt_number in range(1, front_state.settings.submit_recovery_attempts + 1):
        if time.monotonic() >= deadline:
            break
        try:
            submission = await _send_submit(
                front_state,
                native_body=native_body,
                credentials=credentials,
                deadline=deadline,
                recovering=True,
            )
        except HordeAPIError as error:
            if error.return_code == OPERATION_KEY_CONFLICT_CODE:
                raise _operation_key_conflict(error, operation_key=operation_key) from error
            if error.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR or error.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                lost_detail = f"Recovery attempt {attempt_number} was answered {error.status_code}: {error.message}"
                continue
            front_error = _horde_error(error, request_id=None, job_may_exist=True)
            front_error.metadata = front_error.metadata.model_copy(
                update={"submit_outcome": SubmitOutcome.UNKNOWN, "operation_key": str(operation_key)},
            )
            raise front_error from error
        except HordeUnavailableError as error:
            lost_detail = str(error)
            continue
        except TimeoutError:
            lost_detail = "The submit was not answered within the request's budget."
            break
        front_state.front_metrics.record_submit_recovery(existing_claim=submission.existing_claim)
        logger.info(
            f"Recovered the lost submit of operation {operation_key} on attempt {attempt_number}: request "
            f"{submission.request_id}, existing_claim={submission.existing_claim}",
        )
        return submission
    raise _submit_outcome_unknown(lost_detail, operation_key=operation_key)


def _operation_key_conflict(error: HordeAPIError, *, operation_key: uuid.UUID) -> ChatFrontError:
    """Create the 502 for a re-sent submit the Horde refused because its key holds a different request.

    The front re-sends the identical body, so the conflict is a front defect rather than a client error.
    """
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=(
            "The AI Horde refused the front's recovery of a lost submit because its operation key holds a different "
            f"request; the request may have been queued and charged. {error.message}"
        ),
        code=error.return_code,
        metadata=ErrorMetadata(rc=error.return_code, submit_outcome=SubmitOutcome.UNKNOWN, operation_key=str(operation_key)),
        job_may_exist=True,
    )


def _submit_outcome_unknown(detail: str, *, operation_key: uuid.UUID) -> ChatFrontError:
    """Create the 502 for a submit that was sent and never answered, recovery included, so a Horde request may exist."""
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=f"The AI Horde did not answer the submit, so the request may have been queued and charged. {detail}",
        code=HORDE_UNAVAILABLE_CODE,
        metadata=ErrorMetadata(submit_outcome=SubmitOutcome.UNKNOWN, operation_key=str(operation_key)),
        job_may_exist=True,
    )


async def _cancel_within_budget(front_state: FrontState, open_request: OpenRequest, *, retry_failed: bool = False) -> None:
    """Cancel a Horde chat request within the cancel budget, logging rather than raising when it fails.

    Every cancel logs exactly one outcome: cancelled, failed, or not completed within the budget. A request whose
    cancel is in progress or acknowledged is left alone, so two cancels never overlap; a failed one is sent again only
    when ``retry_failed`` is set.

    Args:
        front_state: The process state.
        open_request: The request to cancel.
        retry_failed: Whether a request whose earlier cancel failed is cancelled again; the shutdown drain sets it.
    """
    retry_allowed = retry_failed and open_request.cancel_state == CancelState.FAILED
    if open_request.cancel_state != CancelState.NOT_ATTEMPTED and not retry_allowed:
        return
    open_request.cancel_state = CancelState.IN_PROGRESS
    acknowledged = False
    cancel_timeout_seconds = front_state.settings.cancel_timeout_seconds
    try:
        with anyio.move_on_after(cancel_timeout_seconds) as cancel_deadline:
            try:
                await front_state.horde_client.cancel_chat(request_id=open_request.request_id, credentials=open_request.credentials)
            except (HordeAPIError, HordeUnavailableError, httpx.HTTPError) as error:
                front_state.front_metrics.record_cancel(CancelOutcome.FAILED)
                logger.warning(f"Cancelling chat request {open_request.request_id} failed: {error}")
                return
            acknowledged = True
            front_state.front_metrics.record_cancel(CancelOutcome.ACKNOWLEDGED)
            logger.info(f"Cancelled chat request {open_request.request_id}")
        if cancel_deadline.cancelled_caught:
            front_state.front_metrics.record_cancel(CancelOutcome.TIMED_OUT)
            logger.warning(f"Cancelling chat request {open_request.request_id} did not complete within {cancel_timeout_seconds:g}s")
    finally:
        # Also reached when an enclosing scope, such as the shutdown bound, cuts the cancel off.
        open_request.cancel_state = CancelState.ACKNOWLEDGED if acknowledged else CancelState.FAILED


async def _cancel_shielded(front_state: FrontState, open_request: OpenRequest) -> None:
    """Cancel a Horde chat request once, shielded from the caller's cancellation.

    The cancel is usually issued because the caller is being torn down after its client disconnected; the shield lets
    it finish within its own budget.
    """
    with anyio.CancelScope(shield=True):
        await _cancel_within_budget(front_state, open_request)


async def _cancel_open_requests(front_state: FrontState) -> None:
    """Cancel every open request within the process-wide shutdown bound, sending a failed cancel once more.

    A request whose cancel is in progress is left to that cancel, so two cancels never overlap.
    """
    pending_requests = [
        open_request
        for open_request in (*front_state.open_requests.values(), *front_state.failed_cancels.values())
        if open_request.cancel_state in {CancelState.NOT_ATTEMPTED, CancelState.FAILED}
    ]
    if not pending_requests:
        return
    shutdown_cancel_seconds = front_state.settings.shutdown_cancel_seconds
    logger.info(f"Cancelling {len(pending_requests)} open chat requests before stopping")
    with anyio.move_on_after(shutdown_cancel_seconds) as shutdown_deadline:
        await asyncio.gather(
            *(_cancel_within_budget(front_state, open_request, retry_failed=True) for open_request in pending_requests),
        )
    if shutdown_deadline.cancelled_caught:
        logger.warning(f"Cancelling open chat requests did not complete within {shutdown_cancel_seconds:g}s")


class RequestTrace:
    """Time one admitted chat request and record how it ended, once.

    The line logged at the end carries the request id, the submit and wake times, what woke the request, the wait,
    the status and code, the wake count, whether usage was estimated and how many reads showed the request done
    without a generation. It does not carry the API key, the messages
    or the client's attribution.
    """

    def __init__(self, *, front_metrics: FrontMetrics, streamed: bool, received_at: float) -> None:
        """Create the trace of an admitted request.

        Args:
            front_metrics: The front's instruments.
            streamed: Whether the request is streamed.
            received_at: The monotonic time the request arrived.
        """
        self.streamed: bool = streamed
        """Whether the request is streamed."""
        self.request_id: str | None = None
        """The Horde request id, once the submit is acknowledged."""
        self.submitted_at: float | None = None
        """The wall-clock time the submit was acknowledged, in Unix seconds."""
        self._front_metrics = front_metrics
        self._received_at = received_at
        self._submitted_monotonic: float | None = None
        self._first_content_recorded = False
        self._finished = False

    def mark_submitted(self, request_id: str) -> float:
        """Mutate the trace with the Horde's acknowledgement of the submit and return its wall-clock time."""
        submitted_at = time.time()
        self.request_id = request_id
        self.submitted_at = submitted_at
        self._submitted_monotonic = time.monotonic()
        return submitted_at

    def record_first_content(self) -> None:
        """Record the time from the submit acknowledgement, when a stream's headers go out, to the first content."""
        if self._first_content_recorded or self._submitted_monotonic is None:
            return
        self._first_content_recorded = True
        self._front_metrics.record_first_content(
            duration_seconds=time.monotonic() - self._submitted_monotonic,
            streamed=self.streamed,
        )

    def record_completion(self, completion: ChatCompletionResponse, status: HordeChatStatus) -> None:
        """Record the content, usage, empty-return and queue-wait facts of a completion about to be sent."""
        self.record_first_content()
        self._front_metrics.record_completion(
            model=completion.model,
            estimated_usage=completion.x_horde.estimated_usage,
            has_empty_return=completion.x_horde.notice is not None,
        )
        for generation in status.generations:
            if generation.queue_wait_seconds is not None:
                self._front_metrics.record_queue_wait(
                    queue_wait_seconds=generation.queue_wait_seconds,
                    model=generation.model or completion.model,
                )

    def error_response(self, error: ChatFrontError, *, watcher: ChatRequestWatcher | None = None) -> JSONResponse:
        """Return the error response sent before headers, recording the error and the request's end."""
        self._front_metrics.record_error(status_code=int(error.status_code), code=error.code, streamed=self.streamed)
        if error.metadata.submit_outcome == SubmitOutcome.UNKNOWN:
            self._front_metrics.record_submit_outcome_unknown()
        self.finish(status_code=int(error.status_code), code=error.code or "", watcher=watcher)
        return error.to_response()

    def error_events(self, error: ChatFrontError, *, watcher: ChatRequestWatcher | None) -> list[ServerSentEvent]:
        """Return the error events sent after a stream's 200 headers, recording the failure and the request's end."""
        self._front_metrics.record_stream_failure(code=error.code)
        self.finish(status_code=HTTPStatus.OK, code=error.code or "", watcher=watcher)
        return error.to_events()

    def finish_disconnected(self, *, status_code: int, watcher: ChatRequestWatcher | None) -> None:
        """Record the end of a request whose client disconnected before delivery, as a disconnect wake.

        Does nothing once the request's end is recorded, so a stream's cleanup may call it after any outcome.
        """
        if self._finished:
            return
        self._front_metrics.record_wake(wake_source=WakeSource.DISCONNECT, found_finished=False, found_done_without_generation=False)
        self.finish(status_code=status_code, code=CLIENT_CLOSED_CODE, watcher=watcher, woke_by=WakeSource.DISCONNECT)

    def finish(
        self,
        *,
        status_code: int,
        code: str,
        watcher: ChatRequestWatcher | None,
        estimated_usage: bool | None = None,
        woke_by: WakeSource | None = None,
    ) -> None:
        """Record the request's duration and log its finished line; only the first call counts.

        Args:
            status_code: The HTTP status sent, 200 for a stream whose headers went out.
            code: The error code, [`COMPLETED_CODE`][horde.oai.app.COMPLETED_CODE] or
                [`CLIENT_CLOSED_CODE`][horde.oai.app.CLIENT_CLOSED_CODE].
            watcher: The request's watcher, when the wait started.
            estimated_usage: Whether the delivered completion's usage was estimated; None when none was delivered.
            woke_by: What ended the wait when the watcher did not find the request finished.
        """
        if self._finished:
            return
        self._finished = True
        self._front_metrics.record_request_duration(
            duration_seconds=time.monotonic() - self._received_at,
            streamed=self.streamed,
            status_code=int(status_code),
            code=code,
        )
        woke_at = watcher.woke_at if watcher is not None else None
        if woke_by is None and watcher is not None:
            woke_by = watcher.woke_by
        waited_seconds = None
        if self.submitted_at is not None:
            waited_seconds = (woke_at if woke_at is not None else time.time()) - self.submitted_at
        finished_fields: dict[str, object] = {
            "request_id": self.request_id,
            "streamed": self.streamed,
            "submitted_at": self.submitted_at,
            "woke_at": woke_at,
            "woke_by": str(woke_by) if woke_by is not None else None,
            "waited_seconds": waited_seconds,
            "status_code": int(status_code),
            "code": code,
            "wake_count": watcher.wake_count if watcher is not None else 0,
            "estimated_usage": estimated_usage,
            "done_without_generation_reads": watcher.done_without_generation_reads if watcher is not None else 0,
        }
        request_label = f"Chat request {self.request_id}" if self.request_id is not None else "Unsubmitted chat request"
        # tests/stress/chat_scale_demo.py parses the leading fields in this order and format.
        logger.bind(**finished_fields).info(
            f"{request_label} finished: streamed={self.streamed} submitted_at={_format_seconds(self.submitted_at)} "
            f"woke_at={_format_seconds(woke_at)} woke_by={finished_fields['woke_by']} "
            f"waited={_format_seconds(waited_seconds)}s status={int(status_code)} code={code} "
            f"wakes={finished_fields['wake_count']} estimated_usage={estimated_usage} "
            f"done_without_generation_reads={finished_fields['done_without_generation_reads']}",
        )


def _format_seconds(seconds: float | None) -> str:
    """Return ``seconds`` with millisecond precision, or ``none``."""
    return f"{seconds:.3f}" if seconds is not None else "none"


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
        front_metrics=front_state.front_metrics,
    )


def _wait_error(
    error: ChatWaitTimeoutError | ChatDoneWithoutGenerationError | NoEligibleWorkerError | HordeAPIError,
    *,
    request_id: str,
) -> ChatFrontError:
    """Create the front error for a wait that ended without a terminal outcome."""
    if isinstance(error, ChatDoneWithoutGenerationError):
        return _no_generation_error(request_id)
    if isinstance(error, ChatWaitTimeoutError):
        return ChatFrontError(
            status_code=HTTPStatus.REQUEST_TIMEOUT,
            message="The request was still waiting for a worker at its deadline and was cancelled.",
            code=TIMEOUT_CODE,
            metadata=ErrorMetadata(request_id=request_id),
            job_may_exist=True,
        )
    if isinstance(error, DeclarationGapError):
        return _declaration_gap_error(error.impossible_reason, request_id=request_id)
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


def _declaration_gap_error(impossible_reason: ChatImpossibleReason, *, request_id: str) -> ChatFrontError:
    """Create the 404 for a request the online workers serving its model cannot take because of what they declare.

    Retrying the same request does not help until a worker declaring the missing names comes online, so the error
    carries no ``Retry-After``.
    """
    causes: list[str] = []
    if impossible_reason.missing_declarations:
        causes.append(
            "no online worker serving the model declares " + ", ".join(impossible_reason.missing_declarations),
        )
    if impossible_reason.exceeded_limits:
        causes.append(
            f"the request exceeds the {', '.join(impossible_reason.exceeded_limits)} limit of every online worker serving the model",
        )
    message = f"No worker can serve this request: {'; '.join(causes) or 'the workers lack a declaration it needs'}."
    if GUARANTEE_NAMES.intersection(impossible_reason.missing_declarations):
        message += " Guarantees are not required under horde.conformance 'best_effort'."
    message += " The request was cancelled."
    return ChatFrontError(
        status_code=HTTPStatus.NOT_FOUND,
        message=message,
        code=NO_WORKER_SUPPORTS_FEATURES_CODE,
        metadata=ErrorMetadata(request_id=request_id),
        job_may_exist=True,
    )


def _status_error_needs_cancel(error: HordeAPIError) -> bool:
    """Return whether a status read that ended the wait leaves a request to cancel.

    A 404 means the Horde no longer holds the request. Any other status the wait cannot fix, such as a revoked key,
    leaves the request queued and charged on the Horde.
    """
    return error.status_code != HTTPStatus.NOT_FOUND and error.status_code not in TRANSIENT_STATUS_CODES


def _no_generation_error(request_id: str) -> ChatFrontError:
    """Create the 502 for a request that ended with no generation to deliver and no fault reported."""
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=f"The AI Horde reports request {request_id} as done without a generation; there is no reply to deliver.",
        code=NO_GENERATION_CODE,
        metadata=ErrorMetadata(request_id=request_id),
        job_may_exist=True,
    )


def _faulted_error(request_id: str, status: HordeChatStatus) -> ChatFrontError:
    """Create the front error for a request the Horde reports as faulted.

    When the Horde refused a reply to the request, or a worker reported why it faulted a job, the message carries
    that reason and ``metadata.rc`` its return code, so a client can tell a model that broke the request's contract
    from a worker that failed.
    """
    message = f"The AI Horde reports request {request_id} as faulted: the worker failed to generate it."
    if status.fault_reason is not None and status.fault_rc == WORKER_FAULT_RC:
        message = (
            f"The AI Horde reports request {request_id} as faulted after its worker reported a fault ({WORKER_FAULT_RC}): "
            f"{status.fault_reason}"
        )
    elif status.fault_reason is not None:
        refusal_code = f" ({status.fault_rc})" if status.fault_rc is not None else ""
        message = (
            f"The AI Horde reports request {request_id} as faulted after refusing a worker's reply{refusal_code}: {status.fault_reason}"
        )
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=message,
        code=FAULTED_CODE,
        metadata=ErrorMetadata(request_id=request_id, rc=status.fault_rc),
        job_may_exist=True,
    )


async def _chat_completions(request: Request) -> Response:
    """Serve ``POST /v1/chat/completions``."""
    received_at = time.monotonic()
    front_state = _front_state(request)
    front_metrics = front_state.front_metrics
    settings = front_state.settings
    if _carries_client_retry_hint(request):
        front_metrics.record_client_retry_hint()
    # The slot is taken before the body is read: a body may be tens of MiB, so a client past its cap is refused before
    # the front buffers any of it.
    inflight_slot = front_state.inflight_limiter.try_acquire(_request_source(request))
    if inflight_slot is None:
        front_metrics.record_refusal(status_code=HTTPStatus.SERVICE_UNAVAILABLE, code=FRONT_FULL_CODE)
        return ChatFrontError(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            message="This front holds its maximum of open requests for this client or in total; retry shortly.",
            code=FRONT_FULL_CODE,
            metadata=ErrorMetadata(retry_after=INFLIGHT_RETRY_AFTER_SECONDS),
            headers={RETRY_AFTER_HEADER: str(INFLIGHT_RETRY_AFTER_SECONDS)},
        ).to_response()
    try:
        credentials = _build_credentials(request)
        chat_request = await _read_chat_request(request, front_state)
    except ClientDisconnect:
        inflight_slot.release()
        front_metrics.record_refusal(status_code=CLIENT_CLOSED_REQUEST_STATUS, code=CLIENT_CLOSED_CODE)
        return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
    except ChatFrontError as error:
        inflight_slot.release()
        front_metrics.record_refusal(status_code=error.status_code, code=error.code)
        return error.to_response()
    except BaseException:
        inflight_slot.release()
        raise
    streamed = bool(chat_request.stream)
    front_metrics.record_admitted(streamed=streamed)
    request_trace = RequestTrace(front_metrics=front_metrics, streamed=streamed, received_at=received_at)
    budget_seconds = settings.stream_timeout_seconds if chat_request.stream else settings.nonstream_timeout_seconds
    deadline = time.monotonic() + budget_seconds
    # A fresh key per client request: a client's own retry is a new request and is not deduplicated (ADR 0026).
    operation_key = uuid.uuid4()
    try:
        submission = await _submit(
            front_state,
            native_body=build_native_chat_body(chat_request, operation_key=operation_key),
            operation_key=operation_key,
            credentials=credentials,
            deadline=deadline,
        )
    except ChatFrontError as error:
        inflight_slot.release()
        return request_trace.error_response(error)
    except BaseException:
        inflight_slot.release()
        raise
    submitted_at = request_trace.mark_submitted(submission.request_id)
    open_request = front_state.open_request(request_id=submission.request_id, credentials=credentials)
    if chat_request.stream:
        try:
            return _stream_completion(
                request,
                chat_request=chat_request,
                open_request=open_request,
                request_trace=request_trace,
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
            request_trace=request_trace,
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
    request_trace: RequestTrace,
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
            request_trace.finish_disconnected(status_code=CLIENT_CLOSED_REQUEST_STATUS, watcher=watcher)
            return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
        try:
            status = wait_task.result()
        except (ChatWaitTimeoutError, ChatDoneWithoutGenerationError, NoEligibleWorkerError) as error:
            inflight_slot.release()
            await _cancel_shielded(front_state, open_request)
            return request_trace.error_response(_wait_error(error, request_id=request_id), watcher=watcher)
        except HordeAPIError as error:
            if _status_error_needs_cancel(error):
                inflight_slot.release()
                await _cancel_shielded(front_state, open_request)
            return request_trace.error_response(_wait_error(error, request_id=request_id), watcher=watcher)
    if status.faulted:
        return request_trace.error_response(_faulted_error(request_id, status), watcher=watcher)
    try:
        completion = build_completion(
            request_id=request_id,
            created=int(submitted_at),
            chat_request=chat_request,
            status=status,
        )
    except CompletionWithoutGenerationError:
        # A guard: the watcher returns no status without a generation unless it is faulted. Should one get through,
        # it ends as the settled state does, cancelled, rather than as an empty completion.
        inflight_slot.release()
        await _cancel_shielded(front_state, open_request)
        return request_trace.error_response(_no_generation_error(request_id), watcher=watcher)
    request_trace.record_completion(completion, status)
    request_trace.finish(
        status_code=HTTPStatus.OK,
        code=COMPLETED_CODE,
        watcher=watcher,
        estimated_usage=completion.x_horde.estimated_usage,
    )
    return JSONResponse(content=completion.model_dump(mode="json"))


def _stream_completion(
    request: Request,
    *,
    chat_request: ChatCompletionRequest,
    open_request: OpenRequest,
    request_trace: RequestTrace,
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
                except (ChatWaitTimeoutError, ChatDoneWithoutGenerationError, NoEligibleWorkerError) as error:
                    inflight_slot.release()
                    await _cancel_shielded(front_state, open_request)
                    for error_event in request_trace.error_events(_wait_error(error, request_id=request_id), watcher=latest_watcher):
                        yield error_event
                    return
                except HordeAPIError as error:
                    if _status_error_needs_cancel(error):
                        inflight_slot.release()
                        await _cancel_shielded(front_state, open_request)
                    outcome_settled = True
                    for error_event in request_trace.error_events(_wait_error(error, request_id=request_id), watcher=latest_watcher):
                        yield error_event
                    return
            outcome_settled = True
            if status.faulted:
                for error_event in request_trace.error_events(_faulted_error(request_id, status), watcher=latest_watcher):
                    yield error_event
                return
            try:
                completion = build_completion(
                    request_id=request_id,
                    created=int(submitted_at),
                    chat_request=chat_request,
                    status=status,
                )
            except CompletionWithoutGenerationError:
                # A guard: the watcher returns no status without a generation unless it is faulted. Should one get
                # through, it ends as the settled state does, cancelled, rather than as an empty completion.
                inflight_slot.release()
                await _cancel_shielded(front_state, open_request)
                for error_event in request_trace.error_events(_no_generation_error(request_id), watcher=latest_watcher):
                    yield error_event
                return
            request_trace.record_completion(completion, status)
            include_usage = bool(chat_request.stream_options and chat_request.stream_options.include_usage)
            for completion_event in build_completion_events(completion, include_usage=include_usage):
                yield completion_event
            request_trace.finish(
                status_code=HTTPStatus.OK,
                code=COMPLETED_CODE,
                watcher=latest_watcher,
                estimated_usage=completion.x_horde.estimated_usage,
            )
        finally:
            inflight_slot.release()
            if not outcome_settled:
                if open_request.cancel_state == CancelState.NOT_ATTEMPTED:
                    logger.info(f"Client of streamed chat request {request_id} disconnected while waiting")
                await _cancel_shielded(front_state, open_request)
            request_trace.finish_disconnected(status_code=HTTPStatus.OK, watcher=latest_watcher)
            front_state.close_request(open_request)

    async def finish_unstarted_stream() -> None:
        inflight_slot.release()
        if not stream_started:
            logger.info(f"Client of streamed chat request {request_id} disconnected before the stream started")
            await _cancel_shielded(front_state, open_request)
            request_trace.finish_disconnected(status_code=HTTPStatus.OK, watcher=None)
        front_state.close_request(open_request)

    return EventSourceResponse(
        stream_events(),
        headers=STREAM_HEADERS,
        ping=settings.heartbeat_seconds,
        ping_message_factory=build_heartbeat,
        background=BackgroundTask(finish_unstarted_stream),
    )


async def _fetch_model_cards(request: Request) -> list[ModelCard]:
    """Return the model list entries of the models an online chat worker serves, read through the process cache.

    Raises:
        ChatFrontError: The Authorization header is malformed, or the Horde could not be asked.
    """
    front_state = _front_state(request)
    credentials = _build_credentials(request)
    try:
        active_models = await front_state.model_list_cache.get(front_state.horde_client, credentials)
    except HordeAPIError as error:
        raise _horde_error(error, request_id=None, job_may_exist=False) from error
    except HordeUnavailableError as error:
        raise ChatFrontError(status_code=HTTPStatus.BAD_GATEWAY, message=str(error), code=HORDE_UNAVAILABLE_CODE) from error
    model_cards = (build_model_card(active_model) for active_model in active_models)
    return [model_card for model_card in model_cards if model_card is not None]


async def _list_models(request: Request) -> Response:
    """Serve ``GET /v1/models``."""
    try:
        model_cards = await _fetch_model_cards(request)
    except ChatFrontError as error:
        return error.to_response()
    return JSONResponse(content=ModelList(data=model_cards).model_dump(mode="json"))


async def _get_model(request: Request, model_id: str) -> Response:
    """Serve ``GET /v1/models/{model_id}``; the id may contain slashes."""
    try:
        model_cards = await _fetch_model_cards(request)
    except ChatFrontError as error:
        return error.to_response()
    for model_card in model_cards:
        if model_card.model_id == model_id:
            return JSONResponse(content=model_card.model_dump(mode="json"))
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
