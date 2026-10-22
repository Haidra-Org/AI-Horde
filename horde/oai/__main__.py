# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run the chat front with uvicorn: ``python -m horde.oai``.

Settings come from the ``HORDE_OAI_*`` environment variables (see ``horde.oai.settings``).
``HORDE_OAI_FORWARDED_ALLOW_IPS`` is passed to uvicorn, which then takes the client address from the trusted
listener's ``X-Forwarded-For``; that address is the per-source admission key and the ``Proxied-For`` the Horde sees.
``HORDE_OAI_SHUTDOWN_DRAIN_SECONDS`` bounds how long a clean stop waits for open requests before cancelling them.

Telemetry export reads the variables the Flask application reads (see ``horde.telemetry``): an
``OTEL_EXPORTER_OTLP_*`` endpoint or ``AI_HORDE_TELEMETRY_ENABLED`` turns it on, ``OTEL_SDK_DISABLED=true`` turns it
off, and ``OTEL_SERVICE_NAME``, ``DEPLOYMENT_ENVIRONMENT`` and ``OTEL_TRACES_SAMPLER_ARG`` set the resource and
sampling. The service name defaults to [`FRONT_SERVICE_NAME`][horde.oai.__main__.FRONT_SERVICE_NAME]. With none of
them set the front exports nothing. Configured, the front records one server span per request (health checks
excluded) and a client span per call to the horde, carrying the trace context into the horde's own spans.
``PYROSCOPE_ENABLED=true`` starts continuous profiling through the horde's ``PYROSCOPE_*`` variables, with the
same service name as the front's telemetry, so the front's profiles are kept apart from the horde's.
"""

from __future__ import annotations

import math
import os

import logfire
import uvicorn
from fastapi import FastAPI
from loguru import logger

from horde.oai.app import create_app
from horde.oai.metrics import histogram_views
from horde.oai.settings import load_settings
from horde.telemetry import build_sampling_options, init_pyroscope, otel_sdk_enabled, telemetry_enabled

__all__ = [
    "FRONT_SERVICE_NAME",
    "HEALTH_URL_PATTERN",
    "configure_telemetry",
    "instrument_app",
    "main",
]

FRONT_SERVICE_NAME: str = "horde-oai"
"""The OpenTelemetry service name of the front when ``OTEL_SERVICE_NAME`` is unset; the Flask application's is
``ai-horde``."""

HEALTH_URL_PATTERN: str = "healthz"
"""Requests whose URL matches this pattern get no server span: the listener's and the container's health checks
poll ``/healthz`` every few seconds and would otherwise outnumber the chat requests in the trace store."""


def configure_telemetry() -> bool:
    """Configure Logfire's OpenTelemetry export the way the Flask application does, when the environment asks for it.

    The Flask application's ``init_telemetry_early`` also instruments Flask, so the front configures Logfire itself
    with the same variables, its own service name and the bucket boundaries of its histograms. Loguru records,
    the finished-request line included, are bridged to OpenTelemetry logs. Pyroscope profiling starts through the
    horde's ``init_pyroscope`` under the front's service name, also when ``OTEL_SDK_DISABLED=true`` leaves the SDK
    off, and its span processors join the tracer provider when span profiles are on.

    httpx is instrumented here, before the application creates its clients, so every call to the horde is a client
    span that carries the trace context (``traceparent``) the horde's Flask instrumentation continues; the server
    spans are added by [`instrument_app`][horde.oai.__main__.instrument_app] once the application exists.

    Returns:
        Whether the OpenTelemetry SDK was configured.

    Side Effects:
        Starts Pyroscope when ``PYROSCOPE_ENABLED=true``, installs the global OpenTelemetry tracer and meter
        providers, instruments httpx and adds a Loguru sink.
    """
    if not telemetry_enabled():
        return False
    sdk_enabled = otel_sdk_enabled()
    span_processors = init_pyroscope(span_profiles_enabled=sdk_enabled, default_service_name=FRONT_SERVICE_NAME)
    if not sdk_enabled:
        return False
    logfire.configure(
        send_to_logfire=False,
        console=False,
        service_name=os.environ.get("OTEL_SERVICE_NAME", FRONT_SERVICE_NAME),
        environment=os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
        sampling=build_sampling_options(),
        metrics=logfire.MetricsOptions(views=histogram_views()),
        additional_span_processors=span_processors or None,
    )
    loguru_handler = logfire.loguru_handler()
    logger.add(**loguru_handler)
    logfire.instrument_httpx()
    return True


def instrument_app(app: FastAPI) -> None:
    """Record one server span per request on the application, health checks excluded.

    Streamed responses keep their span open until the stream ends, so the span's duration is the whole request.
    Headers and bodies are not captured: a bearer key, a prompt and a completion never reach the trace store.

    Args:
        app: The application ``create_app`` built.

    Side Effects:
        Adds the OpenTelemetry ASGI middleware to the application.
    """
    logfire.instrument_fastapi(app, excluded_urls=HEALTH_URL_PATTERN)


def main() -> None:
    """Serve the chat front until interrupted, exporting telemetry when the environment configures it."""
    telemetry_configured = configure_telemetry()
    settings = load_settings()
    app = create_app(settings)
    if telemetry_configured:
        instrument_app(app)
    uvicorn.run(
        app,
        host=settings.listen,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=math.ceil(settings.shutdown_drain_seconds),
    )


if __name__ == "__main__":
    main()
