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
them set the front exports nothing.
"""

from __future__ import annotations

import math
import os

import logfire
import uvicorn
from loguru import logger

from horde.oai.app import create_app
from horde.oai.metrics import histogram_views
from horde.oai.settings import load_settings
from horde.telemetry import build_sampling_options, otel_sdk_enabled, telemetry_enabled

__all__ = [
    "FRONT_SERVICE_NAME",
    "configure_telemetry",
    "main",
]

FRONT_SERVICE_NAME: str = "horde-oai"
"""The OpenTelemetry service name of the front when ``OTEL_SERVICE_NAME`` is unset; the Flask application's is
``ai-horde``."""


def configure_telemetry() -> bool:
    """Configure Logfire's OpenTelemetry export the way the Flask application does, when the environment asks for it.

    The Flask application's ``init_telemetry_early`` also instruments Flask, so the front configures Logfire itself
    with the same variables, its own service name and the bucket boundaries of its histograms. Loguru records,
    the finished-request line included, are bridged to OpenTelemetry logs. Pyroscope profiling is not started.

    Returns:
        Whether telemetry was configured.

    Side Effects:
        Installs the global OpenTelemetry tracer and meter providers and adds a Loguru sink.
    """
    if not telemetry_enabled() or not otel_sdk_enabled():
        return False
    logfire.configure(
        send_to_logfire=False,
        console=False,
        service_name=os.environ.get("OTEL_SERVICE_NAME", FRONT_SERVICE_NAME),
        environment=os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
        sampling=build_sampling_options(),
        metrics=logfire.MetricsOptions(views=histogram_views()),
    )
    loguru_handler = logfire.loguru_handler()
    logger.add(**loguru_handler)
    return True


def main() -> None:
    """Serve the chat front until interrupted, exporting telemetry when the environment configures it."""
    configure_telemetry()
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.listen,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=math.ceil(settings.shutdown_drain_seconds),
    )


if __name__ == "__main__":
    main()
