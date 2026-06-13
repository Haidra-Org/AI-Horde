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
from typing import TYPE_CHECKING, Any, Protocol, cast

import logfire

from horde.logger import logger as _loguru_logger

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from flask import Flask
    from opentelemetry.sdk.trace import SpanProcessor


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

    try:
        from pyroscope.otel import PyroscopeSpanProcessor

        logger.init_ok("Telemetry", status="Pyroscope span profiles")
        return [PyroscopeSpanProcessor()]
    except ImportError:
        logger.init_warn(
            "Telemetry",
            status="pyroscope-otel N/A (install telemetry-profiling group or use the telemetry image)",
        )
        return []


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
