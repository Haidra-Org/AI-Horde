# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Logfire / OpenTelemetry telemetry wiring.

Two-phase initialisation:

* :func:`init_telemetry_early` runs Logfire's ``configure`` plus all
  instrumentations that hook Flask itself (``instrument_flask``, the loguru
  bridge, ``RequestsInstrumentor``) and starts Pyroscope. It MUST run as the
  first statement inside ``create_app()``, before any other extension
  registers a ``before_request`` callback. This guarantees OTel's
  ``_before_request`` hook runs before Flask-Limiter's rate-limit check, so
  the span is stashed in ``environ[_ENVIRON_SPAN_KEY]`` even on requests
  short-circuited with HTTP 429.

* :func:`init_telemetry_late` runs the instrumentations that need the
  fully-built app (``instrument_sqlalchemy`` needs ``db.engine``;
  ``instrument_redis`` is grouped with it for symmetry).

Metric *instruments* live in :mod:`horde.metrics` and are declared as plain
module-level constants using ``logfire.metric_histogram`` /
``logfire.metric_counter``. Those calls return Logfire proxy instruments that
defer real SDK instrument creation until the first ``record()`` / ``add()``,
so they're safe to construct at import time. Custom histogram bucket
boundaries are configured here through ``logfire.MetricsOptions(views=...)``.

OTLP export is fully driven by standard env vars
(``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT``,
``OTEL_SERVICE_NAME``, ``OTEL_TRACES_SAMPLER_ARG``, …). Logfire auto-wires a
``PeriodicExportingMetricReader`` for the metrics endpoint when
``send_to_logfire=False`` (see logfire ``_internal/config.py`` ~line 1199).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast, override

import logfire
from loguru import logger as _loguru_logger
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import StatusCode

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from flask import Flask
class _HordeLogger(Protocol):
    """Subset of the loguru logger augmented with Horde's custom INIT levels.

    ``horde.logger`` binds ``init_ok`` / ``init_warn`` / ``init_err`` onto the
    loguru ``Logger`` class at import time via ``partialmethod``; those dynamic
    attributes are invisible to static analysis, so this Protocol re-declares
    the subset used in this module.
    """

    def init_ok(self, message: str, *, status: str) -> None: ...
    def init_warn(self, message: str, *, status: str) -> None: ...
    def init_err(self, message: str, *, status: str) -> None: ...
    def add(self, sink: Any, **kwargs: Any) -> int: ...


logger: _HordeLogger = cast("_HordeLogger", _loguru_logger)

_initialized_early = False
_initialized_late = False
_DEFAULT_PYROSCOPE_SPAN_PROFILE_SAMPLE_RATE: float = 0.10
_DEFAULT_PYROSCOPE_SLOW_THRESHOLD_SECONDS: float = 1.0
_DEFAULT_PYROSCOPE_PROMOTION_COUNT: int = 5
_DEFAULT_PYROSCOPE_PROMOTION_WINDOW_SECONDS: float = 300.0
_DEFAULT_PYROSCOPE_PROMOTION_MAX_PER_MINUTE: int = 60
_TRACE_ID_SPACE: int = 1 << 128
_PYROSCOPE_FORCE_ATTRIBUTE: str = "pyroscope.span_profile.force"
_HTTP_ROUTE_ATTRIBUTE: str = "http.route"


@dataclass(frozen=True, slots=True)
class _SpanIdentity:
    """Represents a span's process-local identity."""

    trace_id: int
    span_id: int


@dataclass(slots=True)
class _Promotion:
    """Represents adaptive correlation allowance for one root-span operation."""

    remaining: int
    expires_at: float


class _TraceRatioSpanProcessor(SpanProcessor):
    """Bounded hybrid sampler for high-cardinality span-profile correlation.

    Trace IDs are uniformly distributed 128-bit values. Selecting by a fixed
    threshold provides representative baseline coverage. Explicitly marked
    critical spans bypass the baseline, while an unsampled slow/error span arms
    a small number of matching follow-up spans. A token bucket bounds those
    adaptive promotions during an incident storm.
    """

    def __init__(
        self,
        processor: SpanProcessor,
        sample_rate: float,
        *,
        slow_threshold_seconds: float = 1.0,
        promotion_count: int = 5,
        promotion_window_seconds: float = 300.0,
        promotion_max_per_minute: int = 60,
        always_include_operations: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._processor: SpanProcessor = processor
        self._threshold: int = int(sample_rate * _TRACE_ID_SPACE)
        self._slow_threshold_ns: int = int(slow_threshold_seconds * 1_000_000_000)
        self._promotion_count: int = promotion_count
        self._promotion_window_seconds: float = promotion_window_seconds
        self._promotion_max_per_minute: int = promotion_max_per_minute
        self._always_include: frozenset[str] = frozenset(
            operation.strip()
            for operation in always_include_operations.split(",")
            if operation.strip()
        )
        self._clock: Callable[[], float] = clock
        self._lock: threading.Lock = threading.Lock()
        self._selected_spans: set[_SpanIdentity] = set()
        self._promotions: dict[str, _Promotion] = {}
        self._promotion_tokens: float = float(promotion_max_per_minute)
        self._last_token_refill: float = clock()

    def _baseline_selected(self, span: Span) -> bool:
        span_context = span.context
        return span_context is not None and span_context.trace_id < self._threshold

    @staticmethod
    def _is_root(span: Span | ReadableSpan) -> bool:
        return span.parent is None or span.parent.is_remote

    @staticmethod
    def _span_key(span: Span | ReadableSpan) -> str:
        attributes = span.attributes or {}
        route = attributes.get(_HTTP_ROUTE_ATTRIBUTE)
        return str(route) if route else span.name

    def _explicitly_selected(self, span: Span) -> bool:
        attributes = span.attributes or {}
        forced = attributes.get(_PYROSCOPE_FORCE_ATTRIBUTE)
        if forced is True or (isinstance(forced, str) and forced.lower() in ("1", "true", "yes")):
            return True
        if not self._always_include:
            return False
        route = attributes.get(_HTTP_ROUTE_ATTRIBUTE)
        operation = str(route) if route else span.name
        return operation in self._always_include

    def _consume_promotion(self, span: Span) -> bool:
        with self._lock:
            if not self._promotions:
                return False
            key = self._span_key(span)
            promotion = self._promotions.get(key)
            if promotion is None:
                return False
            now = self._clock()
            if promotion.remaining <= 0 or now >= promotion.expires_at:
                self._promotions.pop(key, None)
                return False

            refill_rate = self._promotion_max_per_minute / 60
            self._promotion_tokens = min(
                float(self._promotion_max_per_minute),
                self._promotion_tokens + ((now - self._last_token_refill) * refill_rate),
            )
            self._last_token_refill = now
            if self._promotion_tokens < 1:
                return False

            self._promotion_tokens -= 1
            if promotion.remaining == 1:
                self._promotions.pop(key, None)
            else:
                promotion.remaining -= 1
            return True

    def _arm_promotion(self, key: str) -> None:
        if self._promotion_count <= 0 or self._promotion_max_per_minute <= 0:
            return
        now = self._clock()
        with self._lock:
            if key not in self._promotions and len(self._promotions) >= 256:
                oldest_key = min(
                    self._promotions,
                    key=lambda promotion_key: self._promotions[promotion_key].expires_at,
                )
                self._promotions.pop(oldest_key, None)
            current_promotion = self._promotions.get(key)
            self._promotions[key] = _Promotion(
                remaining=max(
                    current_promotion.remaining if current_promotion else 0,
                    self._promotion_count,
                ),
                expires_at=max(
                    current_promotion.expires_at if current_promotion else 0.0,
                    now + self._promotion_window_seconds,
                ),
            )

    def _is_outlier(self, span: ReadableSpan) -> bool:
        if span.status.status_code is StatusCode.ERROR:
            return True
        start_time = span.start_time
        end_time = span.end_time
        if start_time is None or end_time is None:
            return False
        return end_time - start_time >= self._slow_threshold_ns

    @override
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if not self._is_root(span):
            return
        span_context = span.context
        if span_context is None:
            return
        selected = self._baseline_selected(span)
        if not selected:
            selected = self._explicitly_selected(span)
        if not selected:
            selected = self._consume_promotion(span)
        if selected:
            with self._lock:
                self._selected_spans.add(
                    _SpanIdentity(trace_id=span_context.trace_id, span_id=span_context.span_id),
                )
            self._processor.on_start(span, parent_context)

    @override
    def on_end(self, span: ReadableSpan) -> None:
        if not self._is_root(span):
            return
        span_context = span.context
        if span_context is None:
            return
        with self._lock:
            identity = _SpanIdentity(trace_id=span_context.trace_id, span_id=span_context.span_id)
            selected = identity in self._selected_spans
            self._selected_spans.discard(identity)
        if selected:
            self._processor.on_end(span)
        elif self._is_outlier(span):
            self._arm_promotion(self._span_key(span))

    @override
    def shutdown(self) -> None:
        self._processor.shutdown()

    @override
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._processor.force_flush(timeout_millis))


def telemetry_enabled() -> bool:
    """Return ``True`` when telemetry should be activated for this process.

    Telemetry is opt-in. It activates only when an OTLP endpoint is configured
    (the deployments Ansible role sets ``OTEL_EXPORTER_OTLP_ENDPOINT`` whenever
    observability is enabled) or when ``AI_HORDE_TELEMETRY_ENABLED`` is set
    explicitly (handy for local console/no-export debugging). The standard
    ``OTEL_SDK_DISABLED=true`` remains an absolute off switch that overrides
    both.

    The dependency surface and image are always telemetry-capable; this gate
    only governs runtime activation so a bare ``python server.py`` stays inert
    by default.
    """
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    if os.environ.get("AI_HORDE_TELEMETRY_ENABLED", "").lower() in ("1", "true", "yes"):
        return True
    return any(
        os.environ.get(var)
        for var in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        )
    )


def init_telemetry_early(app: Flask) -> None:
    """Configure Logfire and instrument Flask + outbound HTTP + loguru.

    Must be invoked before any other ``before_request`` registration so OTel's
    span-creation hook runs first; otherwise Flask-Limiter (and any other
    short-circuiting before_request) can suppress span creation and trigger
    spurious "Flask environ's OpenTelemetry span missing" warnings.
    """
    global _initialized_early
    if _initialized_early:
        return
    _initialized_early = True

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        logger.init_warn("Telemetry", status="Disabled")
        return

    span_processors = _init_pyroscope()

    sampling = _build_sampling_options()

    from horde.metrics import histogram_views

    logfire.configure(
        send_to_logfire=False,
        console=False,
        service_name=os.environ.get("OTEL_SERVICE_NAME", "ai-horde"),
        environment=os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
        sampling=sampling,
        metrics=logfire.MetricsOptions(views=histogram_views()),
        additional_span_processors=span_processors or None,
    )

    logfire.instrument_flask(app)
    logger.init_ok("Telemetry", status="Flask")

    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
        logger.init_ok("Telemetry", status="Requests")
    except ImportError:
        logger.init_warn(
            "Telemetry",
            status="Requests N/A (pip install opentelemetry-instrumentation-requests)",
        )
    except Exception as err:
        logger.init_warn("Telemetry", status=f"Requests: {err}")

    # Bridge loguru → OTel logs so every record carries trace_id/span_id.
    loguru_handler = logfire.loguru_handler()
    if isinstance(loguru_handler, dict):
        logger.add(**loguru_handler)
    else:
        logger.add(loguru_handler)
    logger.init_ok("Telemetry", status="Loguru")

    logger.init_ok("Telemetry", status="Early ready")


def init_telemetry_late(app: Flask) -> None:
    """Instrument SQLAlchemy and Redis once the app is fully constructed."""
    global _initialized_late
    if _initialized_late:
        return
    _initialized_late = True

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return

    from horde.flask import db

    with app.app_context():
        logfire.instrument_sqlalchemy(engine=db.engine)
    logger.init_ok("Telemetry", status="SQLAlchemy")

    if os.environ.get("OTEL_INSTRUMENT_REDIS", "true").lower() not in ("false", "0"):
        try:
            logfire.instrument_redis()
            logger.init_ok("Telemetry", status="Redis")
        except Exception as err:
            logger.init_warn("Telemetry", status=f"Redis: {err}")

    logger.init_ok("Telemetry", status="Late ready")


def init_telemetry(app: Flask) -> None:
    """Backwards-compatible single-call init (early + late)."""
    init_telemetry_early(app)
    init_telemetry_late(app)


def _build_sampling_options() -> logfire.SamplingOptions:
    """Return ``logfire.SamplingOptions`` honouring ``OTEL_TRACES_SAMPLER_ARG``.

    Defaults to ``1.0`` (record everything) so local-deploy / dev get full
    fidelity; production overrides via env (typically 0.10). The Alloy
    tail-sampler then promotes 100% of errors / slow traces from this
    head-sampled set, so error visibility is preserved at any ratio.
    """
    try:
        ratio = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "1.0"))
    except ValueError:
        ratio = 1.0
    ratio = max(0.0, min(1.0, ratio))
    return logfire.SamplingOptions(head=ratio)


def _init_pyroscope() -> list[SpanProcessor]:
    """Start continuous profiling and optionally enable span correlation.

    ``PyroscopeSpanProcessor`` adds per-root-span identifiers to profiling
    samples. Keep it behind its own explicit gate so ordinary continuous
    profiling does not create an unbounded series for every request.
    """
    if os.environ.get("PYROSCOPE_ENABLED", "").lower() != "true":
        return []

    try:
        import pyroscope

        pyroscope.configure(
            application_name=os.environ.get("OTEL_SERVICE_NAME", "ai-horde"),
            server_address=os.environ.get("PYROSCOPE_SERVER_ADDRESS", "http://localhost:4040"),
            tags={
                "environment": os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
            },
            tenant_id=os.environ.get("PYROSCOPE_TENANT_ID"),
        )
        logger.init_ok("Telemetry", status="Pyroscope")
    except ImportError:
        logger.init_warn(
            "Telemetry",
            status="Pyroscope N/A (install telemetry-profiling group or use the telemetry image)",
        )
        return []
    except Exception as err:
        logger.init_err("Telemetry", status=f"Pyroscope: {err}")
        return []

    if os.environ.get("PYROSCOPE_SPAN_PROFILES", "").lower() != "true":
        logger.init_ok("Telemetry", status="Pyroscope span profiles disabled")
        return []

    try:
        from pyroscope.otel import PyroscopeSpanProcessor

        processor = PyroscopeSpanProcessor()
        sample_rate = _pyroscope_span_profile_sample_rate()
        processor = cast(
            "SpanProcessor",
            _TraceRatioSpanProcessor(
                processor,
                sample_rate,
                slow_threshold_seconds=_pyroscope_env_float(
                    "PYROSCOPE_SPAN_PROFILES_SLOW_THRESHOLD_SECONDS",
                    _DEFAULT_PYROSCOPE_SLOW_THRESHOLD_SECONDS,
                ),
                promotion_count=_pyroscope_env_int(
                    "PYROSCOPE_SPAN_PROFILES_PROMOTION_COUNT",
                    _DEFAULT_PYROSCOPE_PROMOTION_COUNT,
                ),
                promotion_window_seconds=_pyroscope_env_float(
                    "PYROSCOPE_SPAN_PROFILES_PROMOTION_WINDOW_SECONDS",
                    _DEFAULT_PYROSCOPE_PROMOTION_WINDOW_SECONDS,
                ),
                promotion_max_per_minute=_pyroscope_env_int(
                    "PYROSCOPE_SPAN_PROFILES_PROMOTION_MAX_PER_MINUTE",
                    _DEFAULT_PYROSCOPE_PROMOTION_MAX_PER_MINUTE,
                ),
                always_include_operations=os.environ.get(
                    "PYROSCOPE_SPAN_PROFILES_ALWAYS_INCLUDE",
                    "",
                ),
            ),
        )
        logger.init_ok("Telemetry", status=f"Pyroscope span profiles (baseline {sample_rate:.2%})")
        return [processor]
    except ImportError:
        logger.init_warn(
            "Telemetry",
            status="pyroscope-otel N/A (install telemetry-profiling group or use the telemetry image)",
        )
        return []


def _pyroscope_span_profile_sample_rate() -> float:
    raw_rate = os.environ.get(
        "PYROSCOPE_SPAN_PROFILES_SAMPLE_RATE",
        str(_DEFAULT_PYROSCOPE_SPAN_PROFILE_SAMPLE_RATE),
    )
    try:
        sample_rate = float(raw_rate)
        if 0 <= sample_rate <= 1:
            return sample_rate
    except ValueError:
        pass

    logger.init_warn(
        "Telemetry",
        status=(
            f"Invalid PYROSCOPE_SPAN_PROFILES_SAMPLE_RATE={raw_rate!r}; "
            f"using {_DEFAULT_PYROSCOPE_SPAN_PROFILE_SAMPLE_RATE}"
        ),
    )
    return _DEFAULT_PYROSCOPE_SPAN_PROFILE_SAMPLE_RATE


def _pyroscope_env_float(
    environment_variable: str,
    default: float,
) -> float:
    """Return a non-negative floating-point environment setting."""
    raw_value = os.environ.get(environment_variable, str(default))
    try:
        value = float(raw_value)
        if value >= 0:
            return value
    except ValueError:
        pass
    logger.init_warn(
        "Telemetry",
        status=f"Invalid {environment_variable}={raw_value!r}; using {default}",
    )
    return default


def _pyroscope_env_int(environment_variable: str, default: int) -> int:
    """Return a non-negative integer environment setting."""
    raw_value = os.environ.get(environment_variable, str(default))
    try:
        value = int(raw_value)
        if value >= 0:
            return value
    except ValueError:
        pass
    logger.init_warn(
        "Telemetry",
        status=f"Invalid {environment_variable}={raw_value!r}; using {default}",
    )
    return default


def get_traceparent() -> str | None:
    """Capture the current W3C traceparent string from the active span context."""
    from opentelemetry import trace
    from opentelemetry.trace import format_span_id, format_trace_id

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.trace_id:
        return f"00-{format_trace_id(ctx.trace_id)}-{format_span_id(ctx.span_id)}-{ctx.trace_flags:02x}"
    return None


def pyroscope_tag(**tags: str) -> AbstractContextManager[None]:
    """Context manager applying low-cardinality Pyroscope tags (no-op if unavailable).

    Callers must only pass bounded tag keys/values (endpoint family, job
    type, etc.), never raw user/worker IDs.
    """
    try:
        import pyroscope
    except ImportError:
        from contextlib import nullcontext

        return nullcontext()
    return cast("AbstractContextManager[None]", pyroscope.tag_wrapper(tags))
