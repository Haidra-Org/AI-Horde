# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat front's OpenTelemetry instruments.

[`FrontMetrics`][horde.oai.metrics.FrontMetrics] creates every instrument from one OpenTelemetry ``Meter`` and records
through ``record_*`` methods, each called where the lifecycle learns the fact. The application takes the meter from the
global meter provider ([`get_front_meter`][horde.oai.metrics.get_front_meter]); a test passes a meter of its own
provider with an in-memory reader instead. [`histogram_views`][horde.oai.metrics.histogram_views] returns the bucket
boundaries of the front's histograms for ``logfire.configure`` or an SDK ``MeterProvider``.

Instrument names follow the Horde's ``horde.<area>.<measure>`` style under ``horde.oai.``. Attributes are bounded: call
types, outcomes, wake sources, HTTP statuses, error codes and the served model id, which the Horde's model reference
bounds. Addresses, keys, prompts, client titles and request ids are never attributes.

Settlement lag between delivery and the native terminal state is not measured: the front does not observe the
Horde's settlement.

Importing this module creates no instrument and needs no configured provider. The global meter provider's proxy
binds instruments created before ``logfire.configure`` runs once it does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Protocol

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from horde.metrics import BUCKETS_REQUEST_LIFECYCLE_SECONDS, BUCKETS_SECONDS

__all__ = [
    "METER_NAME",
    "CancelOutcome",
    "FrontMetrics",
    "InflightCounts",
    "MetricAttribute",
    "NativeCallOutcome",
    "NativeCallType",
    "RelayOutcome",
    "get_front_meter",
    "histogram_views",
]

METER_NAME: str = "horde.oai"
"""The instrumentation scope of the front's meter."""

BUCKETS_BODY_BYTES: tuple[int, ...] = (
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
)
"""Request body size bucket boundaries in bytes, from a one-line prompt to the largest native chat body."""

ADMITTED_NAME: str = "horde.oai.requests.admitted"
"""Counter: chat requests that took an in-flight slot."""

REFUSED_NAME: str = "horde.oai.requests.refused"
"""Counter: chat requests refused before an in-flight slot, by status and code."""

ERRORS_NAME: str = "horde.oai.requests.errors"
"""Counter: admitted chat requests answered with an error status before any header was sent."""

STREAM_FAILURES_NAME: str = "horde.oai.stream.failures"
"""Counter: streamed requests that ended in an error event after their 200 headers."""

INFLIGHT_NAME: str = "horde.oai.inflight"
"""Observable gauge: requests holding an in-flight slot."""

INFLIGHT_SOURCES_NAME: str = "horde.oai.inflight.sources"
"""Observable gauge: distinct sources holding at least one in-flight slot."""

INFLIGHT_SOURCE_MAX_NAME: str = "horde.oai.inflight.source_max"
"""Observable gauge: the most in-flight slots one source holds."""

BODY_BYTES_NAME: str = "horde.oai.request.body_bytes"
"""Histogram: the size of each chat request body received whole."""

NATIVE_CALL_DURATION_NAME: str = "horde.oai.native.duration"
"""Histogram: native call latency from taking a call slot to the answer, by call type and outcome."""

QUEUE_WAIT_NAME: str = "horde.oai.queue_wait"
"""Histogram: seconds from a request's creation to a generation's assignment, as the Horde reports it."""

FIRST_CONTENT_NAME: str = "horde.oai.first_content.duration"
"""Histogram: seconds from the submit acknowledgement to the first content sent, by whether it was a relayed delta."""

REQUEST_DURATION_NAME: str = "horde.oai.request.duration"
"""Histogram: seconds from an admitted request's arrival to its end."""

WAKES_NAME: str = "horde.oai.wakes"
"""Counter: wakes of waiting requests, by source, whether the wake found the request finished, and whether it found
the request done without a generation."""

SUBMIT_OUTCOME_UNKNOWN_NAME: str = "horde.oai.submit.outcome_unknown"
"""Counter: requests answered with ``submit_outcome: unknown``."""

SUBMIT_RECOVERIES_NAME: str = "horde.oai.submit.recoveries"
"""Counter: lost submits recovered by re-sending, by whether the Horde answered with an existing claim."""

CLIENT_RETRY_HINTS_NAME: str = "horde.oai.client_retry_hints"
"""Counter: chat requests carrying ``x-stainless-retry-count`` above zero."""

COMPLETIONS_NAME: str = "horde.oai.completions"
"""Counter: completions delivered, by model and whether the usage was estimated."""

EMPTY_RETURNS_NAME: str = "horde.oai.completions.empty"
"""Counter: completions holding a generation without visible text, by model."""

CANCELS_NAME: str = "horde.oai.cancels"
"""Counter: cancels sent, by outcome."""

RELAYED_NAME: str = "horde.oai.stream.relayed"
"""Counter: streamed requests that sent at least one content delta before their final reply."""

RELAY_OUTCOMES_NAME: str = "horde.oai.stream.relay_outcomes"
"""Counter: how relayed streams ended, by outcome."""

HISTOGRAM_BUCKETS: Mapping[str, tuple[float, ...]] = {
    BODY_BYTES_NAME: BUCKETS_BODY_BYTES,
    NATIVE_CALL_DURATION_NAME: BUCKETS_SECONDS,
    QUEUE_WAIT_NAME: BUCKETS_REQUEST_LIFECYCLE_SECONDS,
    FIRST_CONTENT_NAME: BUCKETS_REQUEST_LIFECYCLE_SECONDS,
    REQUEST_DURATION_NAME: BUCKETS_REQUEST_LIFECYCLE_SECONDS,
}
"""The bucket boundaries of each front histogram; native calls take seconds, waits take minutes."""


class MetricAttribute(StrEnum):
    """The attribute keys the front's instruments carry."""

    CALL_TYPE = "horde.call_type"
    OUTCOME = "horde.outcome"
    STATUS_CODE = "horde.status_code"
    CODE = "horde.code"
    STREAMED = "horde.streamed"
    WAKE_SOURCE = "horde.wake_source"
    FOUND_FINISHED = "horde.found_finished"
    FOUND_DONE_WITHOUT_GENERATION = "horde.found_done_without_generation"
    MODEL = "horde.model"
    ESTIMATED_USAGE = "horde.estimated_usage"
    EXISTING_CLAIM = "horde.existing_claim"
    RELAYED = "horde.relayed"


class NativeCallType(StrEnum):
    """The kinds of native Horde call the front makes."""

    SUBMIT = "submit"
    RECOVERY = "recovery"
    STATUS = "status"
    CANCEL = "cancel"
    MODELS = "models"


class NativeCallOutcome(StrEnum):
    """How a native Horde call ended."""

    OK = "ok"
    """The Horde answered below 400."""
    REFUSED = "refused"
    """The Horde answered 400 or above."""
    UNREACHABLE = "unreachable"
    """No root accepted the connection, or the connection failed after the call was sent."""
    ABANDONED = "abandoned"
    """The caller stopped waiting, such as at the end of the request's budget."""


class CancelOutcome(StrEnum):
    """How a cancel sent to the Horde ended."""

    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    NOT_STARTED = "not_started"
    """No cancel slot became free in time, so the cancel was not sent and is retried later."""
    ALREADY_GONE = "already_gone"
    """The Horde answered 404: the request no longer exists, such as after its expiry, so the cancel is not retried."""


class RelayOutcome(StrEnum):
    """How a stream that relayed content deltas ended."""

    EQUAL = "equal"
    """The final reply's content is the relayed text."""
    PREFIX = "prefix"
    """The final reply's content extends the relayed text; the rest was sent as one content chunk."""
    MISMATCH = "mismatch"
    """The final reply's content does not start with the relayed text; the stream ended with an error event."""
    REPLACED_ATTEMPT = "replaced_attempt"
    """A delta of another generation arrived; the stream ended with an error event and the request was cancelled."""
    ENDED = "ended"
    """The relayed generation's ended marker arrived; the stream ended with an error event and the request was
    cancelled."""
    GAP = "gap"
    """A delta of the relayed generation arrived out of sequence; the stream ended with an error event and the request
    was cancelled."""


class InflightCounts(Protocol):
    """The in-flight counts the gauges observe."""

    @property
    def inflight(self) -> int:
        """Return how many requests hold a slot."""
        ...

    @property
    def source_count(self) -> int:
        """Return how many distinct sources hold at least one slot."""
        ...

    @property
    def largest_source_inflight(self) -> int:
        """Return the most slots one source holds."""
        ...


def get_front_meter() -> Meter:
    """Return the front's meter from the global meter provider, which ``logfire.configure`` installs."""
    return metrics.get_meter(METER_NAME)


def histogram_views() -> list[View]:
    """Return SDK views giving each front histogram its explicit bucket boundaries."""
    return [
        View(
            instrument_name=instrument_name,
            aggregation=ExplicitBucketHistogramAggregation(boundaries=list(boundaries)),
        )
        for instrument_name, boundaries in HISTOGRAM_BUCKETS.items()
    ]


class FrontMetrics:
    """Record the chat front's metrics on the instruments of one meter.

    Concurrency:
        OpenTelemetry instruments are safe to record from any task or thread.
    """

    def __init__(self, meter: Meter) -> None:
        """Create the instruments.

        Args:
            meter: The meter the instruments belong to.
        """
        self._meter = meter
        self._admitted = meter.create_counter(ADMITTED_NAME, unit="1", description="Chat requests that took an in-flight slot")
        self._refused = meter.create_counter(
            REFUSED_NAME,
            unit="1",
            description="Chat requests refused before an in-flight slot, by status and code",
        )
        self._errors = meter.create_counter(
            ERRORS_NAME,
            unit="1",
            description="Admitted chat requests answered with an error status before headers",
        )
        self._stream_failures = meter.create_counter(
            STREAM_FAILURES_NAME,
            unit="1",
            description="Streamed requests ending in an error event after their 200 headers",
        )
        self._body_bytes = meter.create_histogram(BODY_BYTES_NAME, unit="By", description="Size of each chat request body")
        self._native_call_duration = meter.create_histogram(
            NATIVE_CALL_DURATION_NAME,
            unit="s",
            description="Native Horde call latency by call type and outcome",
        )
        self._queue_wait = meter.create_histogram(
            QUEUE_WAIT_NAME,
            unit="s",
            description="Seconds from request creation to generation assignment, as the Horde reports it",
        )
        self._first_content = meter.create_histogram(
            FIRST_CONTENT_NAME,
            unit="s",
            description="Seconds from the submit acknowledgement to the first content sent",
        )
        self._request_duration = meter.create_histogram(
            REQUEST_DURATION_NAME,
            unit="s",
            description="Seconds from an admitted request's arrival to its end",
        )
        self._wakes = meter.create_counter(WAKES_NAME, unit="1", description="Wakes of waiting requests by source")
        self._submit_outcome_unknown = meter.create_counter(
            SUBMIT_OUTCOME_UNKNOWN_NAME,
            unit="1",
            description="Requests answered with an unknown submit outcome",
        )
        self._submit_recoveries = meter.create_counter(
            SUBMIT_RECOVERIES_NAME,
            unit="1",
            description="Lost submits recovered by re-sending, by whether the key held an existing claim",
        )
        self._client_retry_hints = meter.create_counter(
            CLIENT_RETRY_HINTS_NAME,
            unit="1",
            description="Chat requests carrying a client SDK retry count above zero",
        )
        self._completions = meter.create_counter(
            COMPLETIONS_NAME,
            unit="1",
            description="Completions delivered by model and whether the usage was estimated",
        )
        self._empty_returns = meter.create_counter(
            EMPTY_RETURNS_NAME,
            unit="1",
            description="Completions holding a generation without visible text, by model",
        )
        self._cancels = meter.create_counter(CANCELS_NAME, unit="1", description="Cancels sent to the Horde by outcome")
        self._relayed = meter.create_counter(
            RELAYED_NAME,
            unit="1",
            description="Streamed requests that sent content deltas before their final reply",
        )
        self._relay_outcomes = meter.create_counter(
            RELAY_OUTCOMES_NAME,
            unit="1",
            description="How streams that relayed content deltas ended, by outcome",
        )

    def observe_inflight(self, inflight_counts: InflightCounts) -> None:
        """Mutate the meter by registering the in-flight gauges, which read ``inflight_counts`` at each collection.

        Call it once per meter: the SDK keeps the callbacks of the first registration of a gauge name.
        """

        def observe_inflight(_options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(inflight_counts.inflight)]

        def observe_source_count(_options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(inflight_counts.source_count)]

        def observe_largest_source(_options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(inflight_counts.largest_source_inflight)]

        self._meter.create_observable_gauge(
            INFLIGHT_NAME,
            callbacks=[observe_inflight],
            unit="1",
            description="Requests holding an in-flight slot",
        )
        self._meter.create_observable_gauge(
            INFLIGHT_SOURCES_NAME,
            callbacks=[observe_source_count],
            unit="1",
            description="Distinct sources holding at least one in-flight slot",
        )
        self._meter.create_observable_gauge(
            INFLIGHT_SOURCE_MAX_NAME,
            callbacks=[observe_largest_source],
            unit="1",
            description="The most in-flight slots one source holds",
        )

    def record_admitted(self, *, streamed: bool) -> None:
        """Record a chat request that took an in-flight slot."""
        self._admitted.add(1, {MetricAttribute.STREAMED: streamed})

    def record_refusal(self, *, status_code: int, code: str | None) -> None:
        """Record a chat request refused before it took an in-flight slot."""
        self._refused.add(1, {MetricAttribute.STATUS_CODE: int(status_code), MetricAttribute.CODE: code or ""})

    def record_error(self, *, status_code: int, code: str | None, streamed: bool) -> None:
        """Record an admitted chat request answered with an error status before any header was sent."""
        self._errors.add(
            1,
            {
                MetricAttribute.STATUS_CODE: int(status_code),
                MetricAttribute.CODE: code or "",
                MetricAttribute.STREAMED: streamed,
            },
        )

    def record_stream_failure(self, *, code: str | None) -> None:
        """Record a streamed request that ended in an error event after its 200 headers."""
        self._stream_failures.add(1, {MetricAttribute.CODE: code or ""})

    def record_body_bytes(self, body_bytes: int) -> None:
        """Record the size of a chat request body received whole."""
        self._body_bytes.record(body_bytes)

    def record_native_call(self, *, call_type: NativeCallType, outcome: NativeCallOutcome, duration_seconds: float) -> None:
        """Record one native Horde call; the histogram's count is the call count."""
        self._native_call_duration.record(
            duration_seconds,
            {MetricAttribute.CALL_TYPE: str(call_type), MetricAttribute.OUTCOME: str(outcome)},
        )

    def record_queue_wait(self, *, queue_wait_seconds: float, model: str) -> None:
        """Record a generation's wait from request creation to assignment, as the Horde reported it."""
        self._queue_wait.record(queue_wait_seconds, {MetricAttribute.MODEL: model})

    def record_first_content(self, *, duration_seconds: float, streamed: bool, relayed: bool = False) -> None:
        """Record the time from the submit acknowledgement to the first content sent.

        Args:
            duration_seconds: The time from the submit acknowledgement to the first content.
            streamed: Whether the request is streamed.
            relayed: Whether the first content was a delta relayed while the worker generated, rather than the final
                reply.
        """
        self._first_content.record(
            duration_seconds,
            {
                MetricAttribute.STREAMED: streamed,
                MetricAttribute.RELAYED: relayed,
            },
        )

    def record_request_duration(self, *, duration_seconds: float, streamed: bool, status_code: int, code: str) -> None:
        """Record an admitted request's time from arrival to its end.

        Args:
            duration_seconds: The time from arrival to the end.
            streamed: Whether the request was streamed.
            status_code: The HTTP status sent, 200 for every stream whose headers went out.
            code: The error code the request ended with, or the success or disconnect code.
        """
        self._request_duration.record(
            duration_seconds,
            {
                MetricAttribute.STREAMED: streamed,
                MetricAttribute.STATUS_CODE: int(status_code),
                MetricAttribute.CODE: code,
            },
        )

    def record_wake(self, *, wake_source: str, found_finished: bool, found_done_without_generation: bool) -> None:
        """Record one wake of a waiting request.

        Args:
            wake_source: What caused the wake.
            found_finished: Whether the read found the request finished.
            found_done_without_generation: Whether the read found the request done, not faulted and without a finished
                generation, which the wait does not treat as finished.
        """
        self._wakes.add(
            1,
            {
                MetricAttribute.WAKE_SOURCE: str(wake_source),
                MetricAttribute.FOUND_FINISHED: found_finished,
                MetricAttribute.FOUND_DONE_WITHOUT_GENERATION: found_done_without_generation,
            },
        )

    def record_submit_outcome_unknown(self) -> None:
        """Record a request answered with ``submit_outcome: unknown``."""
        self._submit_outcome_unknown.add(1)

    def record_submit_recovery(self, *, existing_claim: bool) -> None:
        """Record a lost submit recovered by re-sending it; ``existing_claim`` is the claim match."""
        self._submit_recoveries.add(1, {MetricAttribute.EXISTING_CLAIM: existing_claim})

    def record_client_retry_hint(self) -> None:
        """Record a chat request carrying ``x-stainless-retry-count`` above zero."""
        self._client_retry_hints.add(1)

    def record_completion(self, *, model: str, estimated_usage: bool, has_empty_return: bool) -> None:
        """Record a delivered completion and, when one of its generations holds no visible text, an empty return."""
        self._completions.add(1, {MetricAttribute.MODEL: model, MetricAttribute.ESTIMATED_USAGE: estimated_usage})
        if has_empty_return:
            self._empty_returns.add(1, {MetricAttribute.MODEL: model})

    def record_cancel(self, outcome: CancelOutcome) -> None:
        """Record how a cancel sent to the Horde ended."""
        self._cancels.add(1, {MetricAttribute.OUTCOME: str(outcome)})

    def record_relayed(self) -> None:
        """Record a streamed request that sent its first content delta."""
        self._relayed.add(1)

    def record_relay_outcome(self, outcome: RelayOutcome) -> None:
        """Record how a stream that relayed content deltas ended."""
        self._relay_outcomes.add(1, {MetricAttribute.OUTCOME: str(outcome)})
