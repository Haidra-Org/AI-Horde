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
the client's disconnect and use the client's separate cancel pool. A request's handling sends at most one cancel and
holds its in-flight slot until that cancel ends. A failed cancel is sent again every ``HORDE_OAI_CANCEL_RETRY_SECONDS``
until the Horde would have expired the request, and once more by the shutdown drain, never alongside one in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator, Mapping, Sequence
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
from pydantic import BaseModel, ConfigDict, ValidationError
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from horde.chat_contract import (
    BAD_REQUEST_RC,
    CHAT_KUDOS_CAP_EXCEEDED_RC,
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    MAX_CHAT_IMAGE_BYTES,
    MAX_CHAT_IMAGE_COUNT,
    ChatAvailabilityMode,
    ChatImpossibleReason,
    ChatRequestRefusal,
    KudosCapExcess,
    NonJsonConstantError,
    chat_tool_call_bytes,
    check_json_word,
    check_model_choice,
    check_strict_response_schema,
    check_strict_tool_schemas,
    check_tool_fields_need_tools,
    check_tool_history,
    refuse_json_constant,
)
from horde.consts import ANONYMOUS_API_KEY, HORDE_VERSION
from horde.oai.attribution import build_client_agent
from horde.oai.horde_client import (
    SUBMIT_MAX_CONCURRENT_CALLS,
    CancelSlotUnavailableError,
    HordeActiveModel,
    HordeAPIError,
    HordeAsyncSubmission,
    HordeChatModelOffer,
    HordeChatStatus,
    HordeClient,
    HordeCredentials,
    HordeUnavailableError,
    cancel_max_connections,
    create_http_client,
)
from horde.oai.mapping import (
    CompletionWithoutGenerationError,
    build_completion,
    build_completion_id,
    build_error_envelope,
    build_model_card,
    build_native_chat_body,
    build_system_fingerprint,
    relays_deltas,
)
from horde.oai.metrics import CancelOutcome, FrontMetrics, RelayOutcome, get_front_meter
from horde.oai.schema import (
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
from horde.oai.sse import build_completion_events, build_delta_events, build_error_events, build_keepalive_event
from horde.oai.waiter import (
    TRANSIENT_STATUS_CODES,
    ChatDoneWithoutGenerationError,
    ChatRequestWatcher,
    ChatWaitTimeoutError,
    CompletionSignalListener,
    CompletionSubscription,
    DeclarationGapError,
    DeltaEntry,
    DeltaStreamReader,
    DeltaSubscription,
    NoEligibleWorkerError,
    QueueUnavailableError,
    QueueUnavailableReason,
    WaitBudget,
    WakeSource,
)

__all__ = [
    "CancelState",
    "ChatFrontError",
    "DeltaRelay",
    "FrontState",
    "InflightLimiter",
    "InflightSlot",
    "OpenRequest",
    "QueueRefusalFacts",
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

TIMEOUT_RETRY_AFTER_SECONDS: int = NO_ELIGIBLE_WORKER_RETRY_AFTER_SECONDS

QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS: int = 600
"""The most ``Retry-After`` a ``fail_fast`` refusal over the wait estimate carries. The estimate is horde-wide and
the pool changes minute to minute, so a client is told to come back within the native ``NoValidWorkers`` interval
rather than to sleep through the whole estimate."""
"""The ``Retry-After`` of a 408 ``ChatRequestTimeout``.

A request that waited its whole budget usually met a stalled or saturated Horde; an immediate retry would join the same
queue, so clients are asked to wait as long as for a missing worker.
"""

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

STREAM_DIVERGED_CODE: str = "ChatStreamDiverged"
"""The error code of a relayed stream that cannot finish with the text it already sent: the generation it relayed
was replaced or ended, a delta of it arrived out of sequence, or the final reply does not start with the relayed
text."""

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

QUEUE_UNAVAILABLE_CODE: str = "queue_unavailable"
"""The error code of a ``fail_fast`` request refused at its first status read: the online pool had zero eligible
worker threads, or the Horde's wait estimate exceeded the time left in the budget."""

AVAILABILITY_MODE_PARAM: str = "horde.availability_mode"
"""The request path of the option a ``queue_unavailable`` refusal follows from."""

NO_WORKER_SUPPORTS_FEATURES_CODE: str = "no_worker_supports_features"
"""The error code of a request the online workers serving its model cannot take because of what they declare: a
capability or guarantee none declares, or a declared limit the request exceeds. OpenRouter answers the same case,
no endpoint supporting the requested features, with a 404."""

NO_WORKER_SUPPORTS_LIMIT_CODE: str = "no_worker_supports_limit"
"""The error code of a request whose ``max_tokens`` or context length exceeds the limit every online worker serving
its model declares. The front answers 400, as OpenAI does for a ``max_tokens`` over the model's limit; volunteer
workers come and go, so the refusal is about the online workers at the time, and retrying is the client's choice."""

MAX_KUDOS_EXCEEDED_CODE: str = "max_kudos_exceeded"
"""The error code of a request whose kudos estimate at current pricing exceeds its ``horde.max_kudos``; the Horde
refuses it with ``ChatKudosCapExceeded`` before creating it."""

RATE_LIMIT_EXCEEDED_CODE: str = "rate_limit_exceeded"
"""The error code of a Horde 429 that carries no ``rc``, such as a per-route request limit; OpenAI's code for a rate
limit refusal. A 429 with an ``rc``, such as ``TooManyPrompts``, keeps the ``rc``."""

MAX_KUDOS_PARAMETER: str = "horde.max_kudos"
"""The request path of the client's kudos cap."""

DECLARED_SIZE_LIMITS: Mapping[str, str] = MappingProxyType(
    {
        "max_length": "completion limit",
        "max_context_length": "context length",
    },
)
"""The declared chat limits a request exceeds by asking for more tokens, with the wording of each in the 400 message.
Each key is also the field of the model list's chat entry holding the largest value the online workers declare."""

GUARANTEE_NAMES: frozenset[str] = frozenset(guarantee.value for guarantee in ChatGuarantee)
"""The declared values that are guarantees, which ``horde.conformance: best_effort`` stops requiring."""

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

NATIVE_REQUEST_PATH_PATTERN: re.Pattern[str] = re.compile(r"'(?P<request_path>(?:messages|models)\[\d+\][^']*)'")
"""A quoted message or model list path in a native error message, such as
``'messages[0].content[1].type=image_url'`` or ``'models[1]'``; the native route writes the offending path this way
when it refuses a part or an unknown entry of ``models``."""

NATIVE_FIELD_PARAMETERS: Mapping[str, str] = MappingProxyType(
    {
        "max_length": "max_tokens",
        "max_context_length": "horde.max_context_length",
        "stop_sequence": "stop",
    },
)
"""The request key of each native text param whose key differs from it. The native route checks a chat request's
sampling values against the text params model, so a failure there reports the text param."""

MAX_FAILED_CANCELS: int = 10_000
"""The most finished requests with a failed cancel held for the shutdown drain; the oldest is dropped past it.

Cancels fail together during a long Horde outage, and each held record keeps a request id and the client's
credentials in memory until the process stops.
"""

HORDE_CHAT_REQUEST_EXPIRY_SECONDS: float = 20 * 60
"""How long after its submit the Horde keeps a chat request before its cleanup deletes it.

Matches the default of ``WaitingPrompt.expiry`` in ``horde/classes/base/waiting_prompt.py``, which is
``horde.utils.get_expiry_date``: the creation time plus 20 minutes. A failed cancel older than this is dropped instead
of retried, since the request it would cancel no longer exists.
"""

CLIENT_RETRY_COUNT_HEADER: str = "x-stainless-retry-count"
"""The request header OpenAI SDKs send with how many times they retried the call; a hint, never an identity."""


class QueueRefusalFacts(BaseModel):
    """Represents the pool a ``fail_fast`` request's first status read reported when it refused the request.

    The facts go to the error metric and the finished-request line; the client reads them in the error message only.
    """

    model_config = ConfigDict(frozen=True)

    reason: QueueUnavailableReason
    """Which rule refused the request."""
    wait_time: int | None
    """The Horde's wait estimate in seconds, when the status carried one."""
    eligible_worker_threads: int | None
    """The eligible worker threads online, when the status carried the count."""


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
        queue_refusal: QueueRefusalFacts | None = None,
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
            queue_refusal: The pool facts of a ``queue_unavailable`` refusal, for the metric and log; not sent.
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
        self.queue_refusal: QueueRefusalFacts | None = queue_refusal
        """The pool facts of a ``queue_unavailable`` refusal."""

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
    """The Horde answered the cancel with success, or with 404 because the request no longer exists; never retried."""
    FAILED = "failed"
    """The cancel was refused, unreachable, not started or not answered within its budget; it is sent again later."""


class OpenRequest:
    """A submitted Horde request the process may still have to cancel.

    The request's own handling sends at most one cancel; the retry sweep and the shutdown drain send more only when
    that one failed.
    """

    def __init__(self, *, request_id: str, credentials: HordeCredentials, opened_at: float) -> None:
        """Create the record.

        Args:
            request_id: The Horde request id.
            credentials: Who the cancel is made for.
            opened_at: The monotonic time the Horde acknowledged the submit.
        """
        self.request_id: str = request_id
        """The Horde request id."""
        self.credentials: HordeCredentials = credentials
        """Who the cancel is made for."""
        self.opened_at: float = opened_at
        """The monotonic time the Horde acknowledged the submit; the retry sweep measures the request's age from it."""
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
        delta_reader: DeltaStreamReader,
        front_metrics: FrontMetrics,
    ) -> None:
        """Create the state and register the in-flight gauges on ``front_metrics``.

        Args:
            settings: The front's settings.
            horde_client: The Horde client.
            listener: The completion signal listener.
            delta_reader: The reader of the content delta streams.
            front_metrics: The front's instruments.
        """
        self.settings: FrontSettings = settings
        """The front's settings."""
        self.horde_client: HordeClient = horde_client
        """The Horde client."""
        self.listener: CompletionSignalListener = listener
        """The completion signal listener."""
        self.delta_reader: DeltaStreamReader = delta_reader
        """The reader of the content delta streams."""
        self.inflight_limiter: InflightLimiter = InflightLimiter(
            max_inflight=settings.max_inflight,
            max_inflight_per_source=settings.max_inflight_per_source,
        )
        """The open request counts."""
        self.open_requests: dict[str, OpenRequest] = {}
        """The submitted requests not yet finished, by Horde request id; a clean stop cancels them."""
        self.failed_cancels: dict[str, OpenRequest] = {}
        """The finished requests whose cancel failed, oldest first, by Horde request id; the retry sweep and a clean
        stop send each cancel again. At most [`MAX_FAILED_CANCELS`][horde.oai.app.MAX_FAILED_CANCELS] are held."""
        self.model_list_cache: ModelListCache = ModelListCache()
        """The cached Horde chat model list."""
        self.front_metrics: FrontMetrics = front_metrics
        """The front's instruments."""
        front_metrics.observe_inflight(self.inflight_limiter)
        self.accepting: bool = True
        """False once the process is stopping; ``/healthz`` then reports it not ready."""

    def open_request(self, *, request_id: str, credentials: HordeCredentials) -> OpenRequest:
        """Create and register the record of a submitted request."""
        open_request = OpenRequest(request_id=request_id, credentials=credentials, opened_at=time.monotonic())
        self.open_requests[request_id] = open_request
        return open_request

    def close_request(self, open_request: OpenRequest) -> None:
        """Mutate the registry by removing a finished request; removing it twice is harmless.

        A request whose cancel failed moves to ``failed_cancels``, so the retry sweep and the shutdown drain can send
        its cancel again.
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
                "already held, so it is not retried",
            )
        self.failed_cancels[open_request.request_id] = open_request


def create_app(
    settings: FrontSettings,
    *,
    http_client: httpx.AsyncClient | None = None,
    cancel_http_client: httpx.AsyncClient | None = None,
    redis_client: redis.asyncio.Redis | None = None,
    stream_redis_client: redis.asyncio.Redis | None = None,
    meter: Meter | None = None,
) -> FastAPI:
    """Create the chat front application.

    Args:
        settings: The front's settings.
        http_client: The HTTP client for submits, status reads and model lists; None creates one sized by
            ``settings.native_max_connections`` that the app closes on shutdown.
        cancel_http_client: The HTTP client for cancels; None creates a separate pool sized by
            [`cancel_max_connections`][horde.oai.horde_client.cancel_max_connections] that the app closes on shutdown.
        redis_client: The client for the Horde's chat signal database; None creates one from ``settings.redis_url``
            when it is set, and otherwise requests wait on the fallback poll alone.
        stream_redis_client: The client for the Redis database the Horde appends content delta streams to; None
            creates one from ``settings.redis_url`` on ``settings.stream_redis_db`` when ``redis_url`` is set and no
            ``redis_client`` is given, and otherwise streamed requests are delivered whole.
        meter: The meter the front's instruments are created on; None takes it from the global meter provider, which
            ``logfire.configure`` installs.

    Returns:
        The application; its lifespan starts the signal listener, the delta reader and the failed cancel retry sweep
        and, on stop, cancels every open request within ``settings.shutdown_cancel_seconds`` before closing the
        subscription and the reader.
    """
    cancel_connections = cancel_max_connections(settings.max_inflight)
    owned_http_clients: list[httpx.AsyncClient] = []
    if http_client is None:
        http_client = create_http_client(settings.native_max_connections)
        owned_http_clients.append(http_client)
    if cancel_http_client is None:
        cancel_http_client = create_http_client(cancel_connections)
        owned_http_clients.append(cancel_http_client)
    owns_redis_client = redis_client is None and settings.redis_url is not None
    if redis_client is None and settings.redis_url is not None:
        # The listener's own liveness ping detects a black-holed subscription. redis-py's health_check_interval is left
        # off: its PINGs would interleave with the listener's and it drops the PONGs the listener waits for.
        redis_client = redis.asyncio.Redis.from_url(settings.redis_url, socket_keepalive=True)
    if redis_client is None:
        logger.warning("HORDE_OAI_REDIS_URL is not set; chat requests wait on the fallback poll alone.")
    if stream_redis_client is None and owns_redis_client and settings.redis_url is not None:
        stream_redis_client = _create_stream_redis_client(settings.redis_url, db=settings.stream_redis_db)
    owns_stream_redis_client = owns_redis_client and stream_redis_client is not None
    front_metrics = FrontMetrics(meter if meter is not None else get_front_meter())
    front_state = FrontState(
        settings=settings,
        horde_client=HordeClient(
            horde_urls=settings.horde_urls,
            http_client=http_client,
            cancel_http_client=cancel_http_client,
            proxy_passkey=settings.proxy_passkey,
            front_metrics=front_metrics,
            # Submits take a reserved share of the main pool, so the pool's size is still the total of native calls.
            max_concurrent_calls=settings.native_max_connections - SUBMIT_MAX_CONCURRENT_CALLS,
            max_concurrent_submits=SUBMIT_MAX_CONCURRENT_CALLS,
            max_concurrent_cancels=cancel_connections,
        ),
        listener=CompletionSignalListener(redis_client, liveness_seconds=settings.redis_liveness_seconds),
        delta_reader=DeltaStreamReader(stream_redis_client),
        front_metrics=front_metrics,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await front_state.listener.start()
        await front_state.delta_reader.start()
        cancel_retry_task = asyncio.create_task(_retry_failed_cancels_forever(front_state), name="horde-oai-cancel-retry")
        try:
            yield
        finally:
            front_state.accepting = False
            cancel_retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_retry_task
            await _cancel_open_requests(front_state)
            await front_state.listener.stop()
            await front_state.delta_reader.stop()
            if owns_redis_client and redis_client is not None:
                await redis_client.close()
            if owns_stream_redis_client and stream_redis_client is not None:
                await stream_redis_client.close(close_connection_pool=True)
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


def _create_stream_redis_client(redis_url: str, *, db: int) -> redis.asyncio.Redis:
    """Return a client for logical database ``db`` on the host of ``redis_url``, whatever database the URL selects.

    ``Redis.from_url`` lets a database in the URL override one passed beside it, so the pool's database is set after
    parsing. A client given a pool leaves it open on ``close()`` unless ``close_connection_pool`` is set.
    """
    connection_pool = redis.asyncio.ConnectionPool.from_url(redis_url, socket_keepalive=True)
    connection_pool.connection_kwargs["db"] = db
    return redis.asyncio.Redis(connection_pool=connection_pool)


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
    field as ``param``. A 429 without an ``rc`` is reported as ``rate_limit_exceeded``, keeping the Horde's message and
    ``Retry-After``. A refusal over the client's kudos cap is reported as ``max_kudos_exceeded``.
    """
    if error.return_code == CHAT_KUDOS_CAP_EXCEEDED_RC:
        return _kudos_cap_error(error, request_id=request_id, job_may_exist=job_may_exist)
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
        code = code if code is not None else BAD_REQUEST_RC
    if code is None and error.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        code = RATE_LIMIT_EXCEEDED_CODE
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


def _kudos_cap_error(error: HordeAPIError, *, request_id: str | None, job_may_exist: bool) -> ChatFrontError:
    """Create the 400 for a request the Horde refused because its kudos estimate exceeds ``horde.max_kudos``.

    The message is the Horde's, which states the estimate at current pricing and the cap; both are also in
    ``metadata`` when the message carries them. The Horde did not create the request.
    """
    kudos_cap_excess = KudosCapExcess.read_from_message(error.message)
    return ChatFrontError(
        status_code=HTTPStatus.BAD_REQUEST,
        message=error.message,
        code=MAX_KUDOS_EXCEEDED_CODE,
        param=MAX_KUDOS_PARAMETER,
        metadata=ErrorMetadata(
            request_id=request_id,
            rc=error.return_code,
            kudos_estimate=kudos_cap_excess.kudos_estimate if kudos_cap_excess is not None else None,
            max_kudos=kudos_cap_excess.max_kudos if kudos_cap_excess is not None else None,
        ),
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

    The message is prefixed with the request path unless it already holds the parameter in OpenAI's wording.

    Args:
        error: The pydantic failure.
        request_body: The decoded body the model was validated against, which anchors the error path.
    """
    first_error = error.errors()[0]
    error_location = _format_location(tuple(first_error["loc"]), request_body)
    code = BAD_REQUEST_RC
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
        check_model_choice(model=chat_request.model, models=chat_request.models)
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
            service contract, a size limit or an image limit, and 404 for a model id with a variant, in ``model`` or
            in any entry of ``models``.
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
            code=BAD_REQUEST_RC,
        ) from error
    except ValueError as error:
        raise ChatFrontError(
            status_code=HTTPStatus.BAD_REQUEST, message="The request body is not valid JSON.", code=BAD_REQUEST_RC
        ) from error
    if not isinstance(decoded_body, dict):
        raise ChatFrontError(status_code=HTTPStatus.BAD_REQUEST, message="The request body must be a JSON object.", code=BAD_REQUEST_RC)
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
    for model_index, model_id in enumerate(chat_request.requested_models()):
        if MODEL_VARIANT_SEPARATOR in model_id:
            raise _model_not_found(model_id, param="model" if chat_request.models is None else f"models[{model_index}]")
    return chat_request


def _model_not_found(model_id: str, *, param: str = "model") -> ChatFrontError:
    """Create the 404 for an unknown model id, with ``param`` the request path holding it."""
    return ChatFrontError(
        status_code=HTTPStatus.NOT_FOUND,
        message=f"The model '{model_id}' does not exist on the AI Horde.",
        code=MODEL_NOT_FOUND_CODE,
        param=param,
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

    Every cancel logs exactly one outcome: cancelled, already gone (404, so there is nothing left to cancel), failed,
    not started because no cancel slot became free, or not completed within the budget. The budget covers the HTTP
    call; the wait for a cancel slot has a bound of the same length of its own, so a wave of cancels queued behind each
    other is not reported as a slow Horde. A request whose cancel is in progress or acknowledged is left alone, so two
    cancels never overlap; a failed one is sent again only when ``retry_failed`` is set.

    Args:
        front_state: The process state.
        open_request: The request to cancel.
        retry_failed: Whether a request whose earlier cancel failed is cancelled again; the retry sweep and the shutdown
            drain set it.
    """
    retry_allowed = retry_failed and open_request.cancel_state == CancelState.FAILED
    if open_request.cancel_state != CancelState.NOT_ATTEMPTED and not retry_allowed:
        return
    open_request.cancel_state = CancelState.IN_PROGRESS
    acknowledged = False
    cancel_timeout_seconds = front_state.settings.cancel_timeout_seconds
    try:
        try:
            await front_state.horde_client.cancel_chat(
                request_id=open_request.request_id,
                credentials=open_request.credentials,
                slot_wait_seconds=cancel_timeout_seconds,
                call_timeout_seconds=cancel_timeout_seconds,
            )
        except CancelSlotUnavailableError:
            front_state.front_metrics.record_cancel(CancelOutcome.NOT_STARTED)
            logger.warning(
                f"Cancelling chat request {open_request.request_id} did not start: no cancel slot freed within {cancel_timeout_seconds:g}s",
            )
            return
        except TimeoutError:
            front_state.front_metrics.record_cancel(CancelOutcome.TIMED_OUT)
            logger.warning(f"Cancelling chat request {open_request.request_id} did not complete within {cancel_timeout_seconds:g}s")
            return
        except HordeAPIError as error:
            if error.status_code == HTTPStatus.NOT_FOUND:
                # The Horde deleted the request already, such as at its expiry; a retry would only meet the same 404.
                acknowledged = True
                front_state.front_metrics.record_cancel(CancelOutcome.ALREADY_GONE)
                logger.info(f"Chat request {open_request.request_id} was already gone when cancelled")
                return
            front_state.front_metrics.record_cancel(CancelOutcome.FAILED)
            logger.warning(f"Cancelling chat request {open_request.request_id} failed: {error}")
            return
        except (HordeUnavailableError, httpx.HTTPError) as error:
            front_state.front_metrics.record_cancel(CancelOutcome.FAILED)
            logger.warning(f"Cancelling chat request {open_request.request_id} failed: {error}")
            return
        acknowledged = True
        front_state.front_metrics.record_cancel(CancelOutcome.ACKNOWLEDGED)
        logger.info(f"Cancelled chat request {open_request.request_id}")
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


async def _retry_failed_cancels_forever(front_state: FrontState) -> None:
    """Run [`_retry_failed_cancels`][horde.oai.app._retry_failed_cancels] every ``settings.cancel_retry_seconds`` until
    the task is cancelled."""
    while True:
        await asyncio.sleep(front_state.settings.cancel_retry_seconds)
        try:
            await _retry_failed_cancels(front_state)
        except Exception:
            logger.exception("Retrying failed chat request cancels raised; the next sweep tries again")


async def _retry_failed_cancels(front_state: FrontState) -> None:
    """Send the failed cancels once more, dropping those whose request the Horde has expired.

    The retries run at most half the cancel slots at a time, so a long list of failures from an outage does not take
    the slots the cancels of requests ending now need. A retry that is acknowledged, or answered 404 because the
    request is gone, leaves ``failed_cancels``; one that fails again stays for the next sweep.
    """
    now = time.monotonic()
    expired_request_ids = [
        request_id
        for request_id, open_request in front_state.failed_cancels.items()
        if now - open_request.opened_at >= HORDE_CHAT_REQUEST_EXPIRY_SECONDS
    ]
    for expired_request_id in expired_request_ids:
        del front_state.failed_cancels[expired_request_id]
    if expired_request_ids:
        logger.info(
            f"Dropped {len(expired_request_ids)} failed chat request cancels older than the Horde's "
            f"{HORDE_CHAT_REQUEST_EXPIRY_SECONDS:g}s expiry",
        )
    retryable_requests = [
        open_request for open_request in front_state.failed_cancels.values() if open_request.cancel_state == CancelState.FAILED
    ]
    if not retryable_requests:
        return
    logger.info(f"Retrying {len(retryable_requests)} failed chat request cancels")
    retry_batch_size = max(1, cancel_max_connections(front_state.settings.max_inflight) // 2)
    for batch_start in range(0, len(retryable_requests), retry_batch_size):
        retry_batch = retryable_requests[batch_start : batch_start + retry_batch_size]
        await asyncio.gather(*(_cancel_within_budget(front_state, open_request, retry_failed=True) for open_request in retry_batch))
        for open_request in retry_batch:
            still_held = front_state.failed_cancels.get(open_request.request_id) is open_request
            if still_held and open_request.cancel_state == CancelState.ACKNOWLEDGED:
                del front_state.failed_cancels[open_request.request_id]


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
    the status and code, the wake count, whether usage was estimated, how many reads showed the request done without
    a generation, the requested and served models and the ``Retry-After`` sent, for a ``queue_unavailable``
    refusal the pool facts that decided it, and for a ``fail_fast`` request the wait estimate and eligible worker
    threads of the status that took its first-read decision. It does not carry the API key, the messages or the
    client's attribution.
    """

    def __init__(self, *, front_metrics: FrontMetrics, streamed: bool, received_at: float, requested_models: Sequence[str]) -> None:
        """Create the trace of an admitted request.

        Args:
            front_metrics: The front's instruments.
            streamed: Whether the request is streamed.
            received_at: The monotonic time the request arrived.
            requested_models: The models the request accepts.
        """
        self.streamed: bool = streamed
        """Whether the request is streamed."""
        self.requested_models: tuple[str, ...] = tuple(requested_models)
        """The models the request accepts."""
        self.served_model: str | None = None
        """The model of the delivered completion, once one is recorded."""
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

    def record_first_content(self, *, relayed: bool = False) -> None:
        """Record the time from the submit acknowledgement, when a stream's headers go out, to the first content.

        Args:
            relayed: Whether the first content is a delta relayed while the worker generates.
        """
        if self._first_content_recorded or self._submitted_monotonic is None:
            return
        self._first_content_recorded = True
        self._front_metrics.record_first_content(
            duration_seconds=time.monotonic() - self._submitted_monotonic,
            streamed=self.streamed,
            relayed=relayed,
        )

    def record_completion(self, completion: ChatCompletionResponse, status: HordeChatStatus) -> None:
        """Record the content, usage, empty-return and queue-wait facts of a completion about to be sent."""
        self.record_first_content()
        self.served_model = completion.model
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
        self._front_metrics.record_error(
            status_code=int(error.status_code),
            code=error.code,
            streamed=self.streamed,
            reason=error.queue_refusal.reason if error.queue_refusal is not None else None,
        )
        if error.metadata.submit_outcome == SubmitOutcome.UNKNOWN:
            self._front_metrics.record_submit_outcome_unknown()
        self.finish(status_code=int(error.status_code), code=error.code or "", watcher=watcher, front_error=error)
        return error.to_response()

    def error_events(self, error: ChatFrontError, *, watcher: ChatRequestWatcher | None) -> list[ServerSentEvent]:
        """Return the error events sent after a stream's 200 headers, recording the failure and the request's end."""
        self._front_metrics.record_stream_failure(code=error.code)
        self.finish(status_code=HTTPStatus.OK, code=error.code or "", watcher=watcher, front_error=error)
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
        front_error: ChatFrontError | None = None,
    ) -> None:
        """Record the request's duration and log its finished line; only the first call counts.

        Args:
            status_code: The HTTP status sent, 200 for a stream whose headers went out.
            code: The error code, [`COMPLETED_CODE`][horde.oai.app.COMPLETED_CODE] or
                [`CLIENT_CLOSED_CODE`][horde.oai.app.CLIENT_CLOSED_CODE].
            watcher: The request's watcher, when the wait started.
            estimated_usage: Whether the delivered completion's usage was estimated; None when none was delivered.
            woke_by: What ended the wait when the watcher did not find the request finished.
            front_error: The error the request was answered with, for its ``Retry-After`` and queue refusal facts.
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
            "requested_model": ",".join(self.requested_models),
            "served_model": self.served_model,
            "retry_after": front_error.metadata.retry_after if front_error is not None else None,
        }
        queue_refusal = front_error.queue_refusal if front_error is not None else None
        if queue_refusal is not None:
            finished_fields["queue_reason"] = str(queue_refusal.reason)
            finished_fields["wait_time"] = queue_refusal.wait_time
            finished_fields["eligible_worker_threads"] = queue_refusal.eligible_worker_threads
        first_read_status = watcher.first_read_status if watcher is not None else None
        finished_fields["first_read_wait_time"] = first_read_status.wait_time if first_read_status is not None else None
        finished_fields["first_read_eligible_worker_threads"] = (
            first_read_status.eligible_worker_threads if first_read_status is not None else None
        )
        request_label = f"Chat request {self.request_id}" if self.request_id is not None else "Unsubmitted chat request"
        # tests/stress/chat_scale_demo.py parses the leading fields in this order and format, and the horde-chat
        # dashboard's LogQL reads streamed, woke_by and code with greedy matches, so later fields go at the end and no
        # later key ends in "code", "woke_by" or "streamed".
        message = (
            f"{request_label} finished: streamed={self.streamed} submitted_at={_format_seconds(self.submitted_at)} "
            f"woke_at={_format_seconds(woke_at)} woke_by={finished_fields['woke_by']} "
            f"waited={_format_seconds(waited_seconds)}s status={int(status_code)} code={code} "
            f"wakes={finished_fields['wake_count']} estimated_usage={estimated_usage} "
            f"done_without_generation_reads={finished_fields['done_without_generation_reads']} "
            f"requested_model={finished_fields['requested_model']} served_model={_format_optional(self.served_model)} "
            f"retry_after={_format_optional(finished_fields['retry_after'])}"
        )
        if queue_refusal is not None:
            message += (
                f" queue_reason={queue_refusal.reason} wait_time={_format_optional(queue_refusal.wait_time)} "
                f"eligible_worker_threads={_format_optional(queue_refusal.eligible_worker_threads)}"
            )
        message += (
            f" first_read_wait_time={_format_optional(finished_fields['first_read_wait_time'])} "
            f"first_read_eligible_worker_threads={_format_optional(finished_fields['first_read_eligible_worker_threads'])}"
        )
        logger.bind(**finished_fields).info(message)


def _format_seconds(seconds: float | None) -> str:
    """Return ``seconds`` with millisecond precision, or ``none``."""
    return f"{seconds:.3f}" if seconds is not None else "none"


def _format_optional(value: object) -> str:
    """Return ``value`` as text, or ``none``."""
    return str(value) if value is not None else "none"


async def _wait_for_disconnect(request: Request) -> None:
    """Return once the client of ``request`` disconnects; the request body must already be read."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _run_until_disconnect[T](request: Request, awaitable: Coroutine[object, object, T]) -> asyncio.Task[T]:
    """Return the task running ``awaitable`` once it ends, cancelled when the client of ``request`` disconnects first.

    The request body must already be read.
    """
    wait_task = asyncio.create_task(awaitable)
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        await asyncio.wait({wait_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        disconnect_task.cancel()
        if not wait_task.done():
            wait_task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(wait_task, disconnect_task, return_exceptions=True)
    return wait_task


async def _settle_wait_failure(
    front_state: FrontState,
    error: ChatWaitTimeoutError | ChatDoneWithoutGenerationError | NoEligibleWorkerError | QueueUnavailableError | HordeAPIError,
    *,
    chat_request: ChatCompletionRequest,
    open_request: OpenRequest,
) -> ChatFrontError:
    """Cancel a request whose wait ended without a terminal outcome, unless the status error leaves no job to cancel,
    and return the front error that answers it.

    Args:
        front_state: The front's state.
        error: The error that ended the wait.
        chat_request: The client's request, whose models the size limit gap error reads offers for.
        open_request: The request to cancel.
    """
    request_id = open_request.request_id
    if isinstance(error, HordeAPIError):
        if _status_error_needs_cancel(error):
            await _cancel_shielded(front_state, open_request)
        return _wait_error(error, request_id=request_id)
    await _cancel_shielded(front_state, open_request)
    requested_models = chat_request.requested_models()
    chat_offers = await _size_limit_gap_chat_offers(
        front_state,
        error,
        models=requested_models,
        credentials=open_request.credentials,
    )
    return _wait_error(
        error,
        request_id=request_id,
        chat_offers=chat_offers,
        listed_model_count=len(requested_models),
    )


def _create_watcher(
    front_state: FrontState,
    *,
    open_request: OpenRequest,
    subscription: CompletionSubscription,
    deadline: float,
    chat_request: ChatCompletionRequest,
) -> ChatRequestWatcher:
    """Create the watcher of a submitted request."""
    settings = front_state.settings
    availability_mode = chat_request.horde.availability_mode if chat_request.horde is not None else ChatAvailabilityMode.WAIT
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
        availability_mode=availability_mode,
    )


def _wait_error(
    error: ChatWaitTimeoutError | ChatDoneWithoutGenerationError | NoEligibleWorkerError | QueueUnavailableError | HordeAPIError,
    *,
    request_id: str,
    chat_offers: Sequence[HordeChatModelOffer] = (),
    listed_model_count: int = 1,
) -> ChatFrontError:
    """Create the front error for a wait that ended without a terminal outcome.

    Args:
        error: The error that ended the wait.
        request_id: The Horde request id.
        chat_offers: What the online workers serving each requested model declare, for the largest limit a size
            limit gap states.
        listed_model_count: How many models the request accepts, which words the size limit gap message.
    """
    if isinstance(error, ChatDoneWithoutGenerationError):
        return _no_generation_error(request_id)
    if isinstance(error, ChatWaitTimeoutError):
        return ChatFrontError(
            status_code=HTTPStatus.REQUEST_TIMEOUT,
            message="The request was still waiting for a worker at its deadline and was cancelled.",
            code=TIMEOUT_CODE,
            metadata=ErrorMetadata(request_id=request_id, retry_after=TIMEOUT_RETRY_AFTER_SECONDS),
            headers={RETRY_AFTER_HEADER: str(TIMEOUT_RETRY_AFTER_SECONDS)},
            job_may_exist=True,
        )
    if isinstance(error, DeclarationGapError):
        if _is_size_limit_gap(error.impossible_reason):
            return _size_limit_gap_error(
                error.impossible_reason,
                request_id=request_id,
                chat_offers=chat_offers,
                listed_model_count=listed_model_count,
            )
        return _declaration_gap_error(error.impossible_reason, request_id=request_id)
    if isinstance(error, QueueUnavailableError):
        return _queue_unavailable_error(error, request_id=request_id)
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


def _queue_unavailable_error(error: QueueUnavailableError, *, request_id: str) -> ChatFrontError:
    """Create the 503 for a ``fail_fast`` request its first status read refused.

    The message states the pool that read reported: the eligible worker threads, the horde-wide wait estimate and the
    time left in the budget. Workers come and go, so it states the pool at that read and leaves retrying to the client.
    ``Retry-After`` is the wait estimate for a wait over the budget, capped at
    ``QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS``; it exceeds the time left by construction. With zero eligible
    threads the estimate does not reflect when a worker returns, so the no-eligible-worker interval applies.

    Args:
        error: The refusal with the status that decided it.
        request_id: The Horde request id.
    """
    status = error.status
    if error.reason == QueueUnavailableReason.WAIT_EXCEEDS_BUDGET and status.wait_time is not None:
        cause = "The estimated wait exceeds the time left in this request's budget."
        retry_after = min(status.wait_time, QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS)
    else:
        cause = "The online pool has zero worker threads eligible for this request, and none is processing it."
        retry_after = NO_ELIGIBLE_WORKER_RETRY_AFTER_SECONDS
    pool_facts: list[str] = []
    if status.eligible_worker_threads is not None:
        pool_facts.append(f"{status.eligible_worker_threads} eligible worker threads online")
    if status.wait_time is not None:
        pool_facts.append(f"a horde-wide estimated wait of {status.wait_time} s")
    pool_facts.append(f"{max(error.remaining_seconds, 0):.0f} s left in the budget")
    message = (
        f"{cause} The first status read reported {', '.join(pool_facts)}. The request was refused under "
        "horde.availability_mode 'fail_fast' and cancelled. Retry later, or use 'wait' to queue for the budget."
    )
    return ChatFrontError(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        message=message,
        code=QUEUE_UNAVAILABLE_CODE,
        param=AVAILABILITY_MODE_PARAM,
        metadata=ErrorMetadata(request_id=request_id, retry_after=retry_after),
        headers={RETRY_AFTER_HEADER: str(retry_after)},
        job_may_exist=True,
        queue_refusal=QueueRefusalFacts(
            reason=error.reason,
            wait_time=status.wait_time,
            eligible_worker_threads=status.eligible_worker_threads,
        ),
    )


def _declaration_gap_error(impossible_reason: ChatImpossibleReason, *, request_id: str) -> ChatFrontError:
    """Create the 404 for a request the online workers serving its model cannot take because of what they declare.

    Retrying the same request does not help until a worker declaring the missing values comes online, so the error
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


def _is_size_limit_gap(impossible_reason: ChatImpossibleReason) -> bool:
    """Return whether a declaration gap consists only of size limits the request exceeds.

    Such a request is refused for what it asks, so the front answers it as an invalid request rather than as a
    missing capability.
    """
    return (
        not impossible_reason.missing_declarations
        and bool(impossible_reason.exceeded_limits)
        and set(impossible_reason.exceeded_limits) <= DECLARED_SIZE_LIMITS.keys()
    )


def _size_limit_gap_error(
    impossible_reason: ChatImpossibleReason,
    *,
    request_id: str,
    chat_offers: Sequence[HordeChatModelOffer],
    listed_model_count: int,
) -> ChatFrontError:
    """Create the 400 for a request over the size limits every online worker serving its models declares.

    The message states the largest value the online workers declare across the requested models the model list has,
    and the limit's native name when it has none of them. ``param`` is ``max_tokens`` when the completion limit is
    exceeded, and the context length's request key when only that limit is. Workers come and go, so the message
    states what the online workers declare and leaves retrying to the client.

    Args:
        impossible_reason: The gap, with only size limits in ``exceeded_limits``.
        request_id: The Horde request id.
        chat_offers: What the online workers serving each requested model declare, for the models the model list has.
        listed_model_count: How many models the request accepts.
    """
    model_wording = "the model" if listed_model_count == 1 else "the listed models"
    sentences: list[str] = []
    for limit_name, limit_wording in DECLARED_SIZE_LIMITS.items():
        if limit_name not in impossible_reason.exceeded_limits:
            continue
        declared_values: list[int] = [
            getattr(chat_offer, limit_name) for chat_offer in chat_offers if getattr(chat_offer, limit_name) is not None
        ]
        largest_declared = max(declared_values, default=None)
        if largest_declared is None:
            sentences.append(f"No worker eligible for this request currently declares a {limit_wording} ({limit_name}) that high.")
        else:
            sentences.append(
                f"No worker eligible for this request currently declares a {limit_wording} that high; "
                f"the largest any online worker for {model_wording} declares now is {largest_declared} tokens.",
            )
    sentences.append("The request was cancelled.")
    param_limit = "max_length" if "max_length" in impossible_reason.exceeded_limits else "max_context_length"
    return ChatFrontError(
        status_code=HTTPStatus.BAD_REQUEST,
        message=" ".join(sentences),
        code=NO_WORKER_SUPPORTS_LIMIT_CODE,
        param=NATIVE_FIELD_PARAMETERS[param_limit],
        metadata=ErrorMetadata(request_id=request_id),
        job_may_exist=True,
    )


async def _size_limit_gap_chat_offers(
    front_state: FrontState,
    error: Exception,
    *,
    models: Sequence[str],
    credentials: HordeCredentials,
) -> list[HordeChatModelOffer]:
    """Return the requested models' chat entries from the model list when a wait ended on a size limit gap.

    Any requested model may serve the request, so the 400 states the largest limit across all of them. The list is
    read through the process cache. A failed read, or a gap of another kind, returns no entries and leaves the 400
    without the declared value, since the request is refused either way.

    Args:
        front_state: The process state holding the model list cache.
        error: The error that ended the wait.
        models: The models the request accepts.
        credentials: The client's credentials, used for the model list read.
    """
    if not isinstance(error, DeclarationGapError) or not _is_size_limit_gap(error.impossible_reason):
        return []
    try:
        active_models = await front_state.model_list_cache.get(front_state.horde_client, credentials)
    except (HordeAPIError, HordeUnavailableError):
        return []
    return [active_model.chat for active_model in active_models if active_model.name in models and active_model.chat is not None]


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
    request_trace = RequestTrace(
        front_metrics=front_metrics,
        streamed=streamed,
        received_at=received_at,
        requested_models=chat_request.requested_models(),
    )
    budget_seconds = settings.stream_timeout_seconds if chat_request.stream else settings.nonstream_timeout_seconds
    deadline = time.monotonic() + budget_seconds
    # A fresh key per client request: a client's own retry is a new request and is not deduplicated (ADR 0026).
    operation_key = uuid.uuid4()
    try:
        submission = await _submit(
            front_state,
            native_body=build_native_chat_body(
                chat_request,
                operation_key=operation_key,
                relay_deltas=_relays_deltas(front_state, chat_request),
            ),
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
            return await _stream_completion(
                request,
                chat_request=chat_request,
                open_request=open_request,
                request_trace=request_trace,
                submitted_at=submitted_at,
                deadline=deadline,
                inflight_slot=inflight_slot,
            )
        except BaseException:
            try:
                await _cancel_shielded(front_state, open_request)
            finally:
                inflight_slot.release()
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
        )
    except asyncio.CancelledError:
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
) -> Response:
    """Wait for a submitted request and return its completion, or the error that ended the wait.

    The caller releases the in-flight slot after this returns, so an abandoned request's cancel counts against the cap
    until it ends: the slot then bounds the Horde calls the process has open, cancels included.
    """
    front_state = _front_state(request)
    request_id = open_request.request_id
    with front_state.listener.register(request_id) as subscription:
        watcher = _create_watcher(
            front_state,
            open_request=open_request,
            subscription=subscription,
            deadline=deadline,
            chat_request=chat_request,
        )
        wait_task = await _run_until_disconnect(request, watcher.wait_until_finished())
        if wait_task.cancelled():
            logger.info(f"Client of chat request {request_id} disconnected while waiting")
            await _cancel_shielded(front_state, open_request)
            request_trace.finish_disconnected(status_code=CLIENT_CLOSED_REQUEST_STATUS, watcher=watcher)
            return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
        try:
            status = wait_task.result()
        except (ChatWaitTimeoutError, ChatDoneWithoutGenerationError, NoEligibleWorkerError, QueueUnavailableError, HordeAPIError) as error:
            wait_error = await _settle_wait_failure(front_state, error, chat_request=chat_request, open_request=open_request)
            return request_trace.error_response(wait_error, watcher=watcher)
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


class DeltaRelay:
    """Relay one streamed request's content deltas and reconcile the relayed text with its final reply.

    The first delta, sequence 1 of a generation, pins that generation. The first delta that sends the client anything
    exposes the stream. After exposure an entry of another generation, the pinned generation's ended marker or a delta
    out of sequence breaks the relay: the Horde restarts a faulted job from the beginning, and a reissued attempt is
    never spliced into a choice the client already holds part of (ADR 0024). Before exposure such entries unpin the
    generation, and the next generation's first delta can pin it again, or the request is delivered whole.

    A delta's reasoning is relayed only when the client asked for it with ``horde.include_reasoning``; otherwise it is
    dropped, and a delta carrying only reasoning advances the sequence without sending anything. Reasoning is never
    part of the reconciliation with the final reply.
    """

    def __init__(self, *, completion_id: str, created: int, model: str, include_usage: bool, include_reasoning: bool = False) -> None:
        """Create the relay of a stream that has not sent anything yet.

        Args:
            completion_id: The completion id every chunk of the stream carries.
            created: When the request was submitted, in Unix seconds.
            model: The model the client asked for, the first listed for ``models``, which the relayed chunks carry and
                the fingerprint uses when the pinned generation's first delta omits the model that serves it.
            include_usage: Whether the client asked for usage, which writes a null ``usage`` in every chunk.
            include_reasoning: Whether the client asked for reasoning with ``horde.include_reasoning``.
        """
        self.generation_id: str | None = None
        """The generation whose deltas are relayed; None before its first delta."""
        self.broken: RelayOutcome | None = None
        """Why the relay broke after exposure; None while it holds."""
        self.system_fingerprint: str | None = None
        """The fingerprint of the pinned generation's worker and model, from its first delta; None before that or
        when the delta stream does not carry the worker."""
        self._completion_id = completion_id
        self._created = created
        self._model = model
        self._served_model = model
        """The model serving the pinned generation, from its first delta, which the relayed chunks carry; ``model``
        before that or when the delta omits it."""
        self._include_usage = include_usage
        self._include_reasoning = include_reasoning
        self._exposed = False
        self._next_seq = 1
        self._relayed_parts: list[str] = []
        self._relayed_reasoning_parts: list[str] = []

    @property
    def exposed(self) -> bool:
        """Return whether a delta was sent to the client."""
        return self._exposed

    @property
    def relayed_content(self) -> str:
        """Return the text sent to the client as deltas."""
        return "".join(self._relayed_parts)

    @property
    def relayed_reasoning(self) -> str:
        """Return the reasoning sent to the client as deltas; empty unless the client asked for reasoning."""
        return "".join(self._relayed_reasoning_parts)

    def _unpin(self) -> None:
        """Mutate the relay so it does not follow a generation, before anything was sent."""
        self.generation_id = None
        self.system_fingerprint = None
        self._next_seq = 1

    def take(self, entries: Sequence[DeltaEntry]) -> list[ServerSentEvent]:
        """Return the events relaying ``entries``, read together in stream order, and set ``broken`` when one breaks
        the relay; entries after the breaking one are dropped.

        Before exposure, a generation whose ended marker is among ``entries`` is not exposed, so a reader that fell
        behind does not show a client the start of an attempt it already knows failed.
        """
        ended_generations = {entry.generation_id for entry in entries if entry.ended}
        events: list[ServerSentEvent] = []
        for entry in entries:
            if not entry.ended and entry.content is None and entry.reasoning_content is None:
                continue
            if not self.exposed:
                pinned_entry = entry.generation_id == self.generation_id
                if pinned_entry and (entry.ended or entry.seq != self._next_seq or entry.generation_id in ended_generations):
                    self._unpin()
                    continue
                if entry.generation_id != self.generation_id:
                    if entry.ended or entry.seq != 1 or entry.generation_id in ended_generations:
                        continue
                    self.generation_id = entry.generation_id
                    # Under ``models`` any listed model may serve the request, so the chunks carry the delta's model.
                    self._served_model = entry.model or self._model
                    self.system_fingerprint = build_system_fingerprint(entry.worker_id, self._served_model)
                    self._next_seq = 1
            elif entry.generation_id != self.generation_id:
                self.broken = RelayOutcome.REPLACED_ATTEMPT
            elif entry.ended:
                self.broken = RelayOutcome.ENDED
            elif entry.seq != self._next_seq:
                self.broken = RelayOutcome.GAP
            if self.broken is not None:
                break
            content = entry.content or ""
            reasoning_content = (entry.reasoning_content or "") if self._include_reasoning else ""
            self._next_seq += 1
            if not content and not reasoning_content:
                continue
            events.extend(
                build_delta_events(
                    completion_id=self._completion_id,
                    created=self._created,
                    model=self._served_model,
                    content=content,
                    first=not self._exposed,
                    include_usage=self._include_usage,
                    system_fingerprint=self.system_fingerprint,
                    reasoning_content=reasoning_content,
                ),
            )
            self._exposed = True
            self._relayed_parts.append(content)
            self._relayed_reasoning_parts.append(reasoning_content)
        return events

    def align_fingerprint(self, completion: ChatCompletionResponse) -> ChatCompletionResponse:
        """Return ``completion`` carrying the relayed chunks' ``system_fingerprint``, so one stream carries one value.

        The relay pins one generation, and another generation's entry breaks it, so a reconciled final reply normally
        comes from the pinned generation and its fingerprint is already equal. When they differ, the relayed value is
        kept: the client already holds chunks carrying it, and the text it received came from the pinned generation.
        A relay without a fingerprint leaves the completion as it is.
        """
        if self.system_fingerprint is None or completion.system_fingerprint == self.system_fingerprint:
            return completion
        return completion.model_copy(update={"system_fingerprint": self.system_fingerprint})

    def reconcile(self, completion: ChatCompletionResponse) -> RelayOutcome:
        """Return how the final reply relates to the relayed text: equal, an extension of it, or a mismatch.

        A completion with other than one choice is a mismatch, since only the first choice is relayed.
        """
        if len(completion.choices) != 1:
            return RelayOutcome.MISMATCH
        final_content = completion.choices[0].message.content or ""
        relayed_content = self.relayed_content
        if final_content == relayed_content:
            return RelayOutcome.EQUAL
        if final_content.startswith(relayed_content):
            return RelayOutcome.PREFIX
        return RelayOutcome.MISMATCH


async def _relay_deltas(
    relay: DeltaRelay,
    delta_subscription: DeltaSubscription,
    wait_task: asyncio.Task[HordeChatStatus],
    request_trace: RequestTrace,
    front_metrics: FrontMetrics,
) -> AsyncIterator[ServerSentEvent]:
    """Yield the events relaying the request's deltas until ``wait_task`` ends or the relay breaks.

    Entries still queued when ``wait_task`` ends are left; the final reply's reconciliation sends what they held.
    """
    get_task: asyncio.Task[DeltaEntry] | None = None
    try:
        while not wait_task.done() and relay.broken is None:
            if get_task is None:
                get_task = asyncio.create_task(delta_subscription.get())
            await asyncio.wait({wait_task, get_task}, return_when=asyncio.FIRST_COMPLETED)
            if not get_task.done():
                continue
            entries = [get_task.result(), *delta_subscription.take_queued()]
            get_task = None
            was_exposed = relay.exposed
            relay_events = relay.take(entries)
            if relay.exposed and not was_exposed:
                request_trace.record_first_content(relayed=True)
                front_metrics.record_relayed()
            for relay_event in relay_events:
                yield relay_event
    finally:
        if get_task is not None and not get_task.done():
            get_task.cancel()


def _relay_error(request_id: str, outcome: RelayOutcome) -> ChatFrontError:
    """Create the error event's error for a relayed stream that cannot finish with the text it sent."""
    if outcome == RelayOutcome.MISMATCH:
        message = (
            f"The final reply of request {request_id} does not continue the text already streamed, so it cannot be "
            "delivered on this stream."
        )
    else:
        message = (
            f"The worker generating request {request_id} stopped after part of the reply was streamed. A new attempt "
            "would start the reply over, which cannot continue the streamed text, so the request was cancelled."
        )
    return ChatFrontError(
        status_code=HTTPStatus.BAD_GATEWAY,
        message=message,
        code=STREAM_DIVERGED_CODE,
        metadata=ErrorMetadata(request_id=request_id),
        job_may_exist=True,
    )


async def _stream_completion(
    request: Request,
    *,
    chat_request: ChatCompletionRequest,
    open_request: OpenRequest,
    request_trace: RequestTrace,
    submitted_at: float,
    deadline: float,
    inflight_slot: InflightSlot,
) -> Response:
    """Return the server-sent event response that waits for a submitted request and then delivers it, or the error
    response that answers a refusal at its first-read decision.

    The first-read decision ([`wait_for_admission`][horde.oai.waiter.ChatRequestWatcher.wait_for_admission]) runs
    before the response opens, so a refusal there (``fail_fast``, a declaration or limit gap, a status error) has the
    HTTP status and ``Retry-After`` header the non-streamed request gets. Errors after it arrive as error events under
    the stream's 200. The request is registered with the completion listener and the delta reader before that read, so
    a signal sent after it is not lost.

    The in-flight slot is released after an abandoned request's cancel ends, so the cap counts cancelling requests. When
    the client disconnects before the event generator first runs, sse-starlette never starts it, so the response's
    background task cancels the request and releases the slot instead.

    A request [`relays_deltas`][horde.oai.mapping.relays_deltas] selects sends the worker's content deltas as they
    arrive through a [`DeltaRelay`][horde.oai.app.DeltaRelay]; once one is sent, the final reply only finishes the
    stream when it starts with the relayed text, and otherwise the stream ends with an error event.
    """
    front_state = _front_state(request)
    settings = front_state.settings
    request_id = open_request.request_id
    stream_started = False
    include_usage = bool(chat_request.stream_options and chat_request.stream_options.include_usage)
    registrations = contextlib.ExitStack()
    try:
        subscription = registrations.enter_context(front_state.listener.register(request_id))
        delta_subscription = registrations.enter_context(_register_deltas(front_state, chat_request, request_id))
        watcher = _create_watcher(
            front_state,
            open_request=open_request,
            subscription=subscription,
            deadline=deadline,
            chat_request=chat_request,
        )
        admission_task = await _run_until_disconnect(request, watcher.wait_for_admission())
        if admission_task.cancelled():
            logger.info(f"Client of streamed chat request {request_id} disconnected while waiting")
            await _cancel_shielded(front_state, open_request)
            request_trace.finish_disconnected(status_code=CLIENT_CLOSED_REQUEST_STATUS, watcher=watcher)
            registrations.close()
            inflight_slot.release()
            front_state.close_request(open_request)
            return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
        try:
            admission_task.result()
        except (ChatWaitTimeoutError, ChatDoneWithoutGenerationError, NoEligibleWorkerError, QueueUnavailableError, HordeAPIError) as error:
            wait_error = await _settle_wait_failure(front_state, error, chat_request=chat_request, open_request=open_request)
            registrations.close()
            inflight_slot.release()
            front_state.close_request(open_request)
            return request_trace.error_response(wait_error, watcher=watcher)
    except BaseException:
        registrations.close()
        raise

    def build_heartbeat() -> ServerSentEvent:
        return build_keepalive_event(watcher.latest_status)

    async def stream_events() -> AsyncIterator[ServerSentEvent]:
        nonlocal stream_started
        stream_started = True
        outcome_settled = False
        relay: DeltaRelay | None = None
        wait_task: asyncio.Task[HordeChatStatus] | None = None
        try:
            with registrations:
                if delta_subscription is not None:
                    relay = DeltaRelay(
                        completion_id=build_completion_id(request_id),
                        created=int(submitted_at),
                        model=chat_request.requested_models()[0],
                        include_usage=include_usage,
                        include_reasoning=chat_request.horde is not None and chat_request.horde.include_reasoning,
                    )
                    wait_task = asyncio.create_task(watcher.wait_until_finished())
                    async for relay_event in _relay_deltas(
                        relay,
                        delta_subscription,
                        wait_task,
                        request_trace,
                        front_state.front_metrics,
                    ):
                        yield relay_event
                    if relay.broken is not None:
                        await _cancel_shielded(front_state, open_request)
                        outcome_settled = True
                        front_state.front_metrics.record_relay_outcome(relay.broken)
                        for error_event in request_trace.error_events(_relay_error(request_id, relay.broken), watcher=watcher):
                            yield error_event
                        return
                try:
                    status = await (wait_task if wait_task is not None else watcher.wait_until_finished())
                except (ChatWaitTimeoutError, ChatDoneWithoutGenerationError, NoEligibleWorkerError, QueueUnavailableError) as error:
                    wait_error = await _settle_wait_failure(front_state, error, chat_request=chat_request, open_request=open_request)
                    for error_event in request_trace.error_events(wait_error, watcher=watcher):
                        yield error_event
                    return
                except HordeAPIError as error:
                    wait_error = await _settle_wait_failure(front_state, error, chat_request=chat_request, open_request=open_request)
                    outcome_settled = True
                    for error_event in request_trace.error_events(wait_error, watcher=watcher):
                        yield error_event
                    return
            outcome_settled = True
            if status.faulted:
                for error_event in request_trace.error_events(_faulted_error(request_id, status), watcher=watcher):
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
                await _cancel_shielded(front_state, open_request)
                for error_event in request_trace.error_events(_no_generation_error(request_id), watcher=watcher):
                    yield error_event
                return
            relayed_content: str | None = None
            relayed_reasoning = ""
            if relay is not None and relay.exposed:
                relay_outcome = relay.reconcile(completion)
                front_state.front_metrics.record_relay_outcome(relay_outcome)
                if relay_outcome == RelayOutcome.MISMATCH:
                    for error_event in request_trace.error_events(_relay_error(request_id, relay_outcome), watcher=watcher):
                        yield error_event
                    return
                relayed_content = relay.relayed_content
                relayed_reasoning = relay.relayed_reasoning
                completion = relay.align_fingerprint(completion)
            request_trace.record_completion(completion, status)
            for completion_event in build_completion_events(
                completion,
                include_usage=include_usage,
                relayed_content=relayed_content,
                relayed_reasoning=relayed_reasoning,
            ):
                yield completion_event
            request_trace.finish(
                status_code=HTTPStatus.OK,
                code=COMPLETED_CODE,
                watcher=watcher,
                estimated_usage=completion.x_horde.estimated_usage,
            )
        finally:
            try:
                if wait_task is not None and not wait_task.done():
                    wait_task.cancel()
                    with anyio.CancelScope(shield=True):
                        await asyncio.gather(wait_task, return_exceptions=True)
                if not outcome_settled:
                    if open_request.cancel_state == CancelState.NOT_ATTEMPTED:
                        logger.info(f"Client of streamed chat request {request_id} disconnected while waiting")
                    await _cancel_shielded(front_state, open_request)
            finally:
                inflight_slot.release()
            request_trace.finish_disconnected(status_code=HTTPStatus.OK, watcher=watcher)
            front_state.close_request(open_request)

    async def finish_unstarted_stream() -> None:
        try:
            if not stream_started:
                logger.info(f"Client of streamed chat request {request_id} disconnected before the stream started")
                await _cancel_shielded(front_state, open_request)
                request_trace.finish_disconnected(status_code=HTTPStatus.OK, watcher=watcher)
        finally:
            registrations.close()
            inflight_slot.release()
        front_state.close_request(open_request)

    return EventSourceResponse(
        stream_events(),
        headers=STREAM_HEADERS,
        ping=settings.heartbeat_seconds,
        ping_message_factory=build_heartbeat,
        background=BackgroundTask(finish_unstarted_stream),
    )


def _relays_deltas(front_state: FrontState, chat_request: ChatCompletionRequest) -> bool:
    """Return whether the front relays the request's content deltas: the delta reader has a Redis client and
    [`relays_deltas`][horde.oai.mapping.relays_deltas] selects the request."""
    return front_state.delta_reader.enabled and relays_deltas(chat_request)


@contextlib.contextmanager
def _register_deltas(
    front_state: FrontState,
    chat_request: ChatCompletionRequest,
    request_id: str,
) -> Iterator[DeltaSubscription | None]:
    """Context manager that registers the request with the delta reader when its deltas are relayed, and yields
    None otherwise."""
    if not _relays_deltas(front_state, chat_request):
        yield None
        return
    with front_state.delta_reader.register(request_id) as delta_subscription:
        yield delta_subscription


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
