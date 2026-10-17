# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's telemetry configuration from the environment."""

from __future__ import annotations

from typing import Any

import logfire
import pytest
from loguru import logger

from horde.oai import __main__ as front_main
from horde.oai.metrics import NATIVE_CALL_DURATION_NAME, QUEUE_WAIT_NAME, REQUEST_DURATION_NAME


@pytest.fixture
def telemetry_environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Return a monkeypatch with every variable that turns telemetry on removed."""
    for variable in (
        "OTEL_SDK_DISABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "AI_HORDE_TELEMETRY_ENABLED",
        "PYROSCOPE_ENABLED",
    ):
        monkeypatch.delenv(variable, raising=False)
    return monkeypatch


def test_telemetry_is_not_configured_without_the_environment(telemetry_environment: pytest.MonkeyPatch) -> None:
    configure_calls: list[dict[str, Any]] = []
    telemetry_environment.setattr(logfire, "configure", lambda **kwargs: configure_calls.append(kwargs))

    assert front_main.configure_telemetry() is False
    assert configure_calls == []


def test_telemetry_is_configured_from_an_otlp_endpoint_as_horde_oai(telemetry_environment: pytest.MonkeyPatch) -> None:
    configure_calls: list[dict[str, Any]] = []
    added_sinks: list[dict[str, Any]] = []
    telemetry_environment.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4318")
    telemetry_environment.setattr(logfire, "configure", lambda **kwargs: configure_calls.append(kwargs))
    telemetry_environment.setattr(logfire, "loguru_handler", lambda: {"sink": print})
    telemetry_environment.setattr(logger, "add", lambda **kwargs: added_sinks.append(kwargs))

    assert front_main.configure_telemetry() is True
    (configure_kwargs,) = configure_calls
    assert configure_kwargs["service_name"] == "horde-oai"
    assert configure_kwargs["send_to_logfire"] is False
    configured_views = {view._instrument_name for view in configure_kwargs["metrics"].views}
    assert {NATIVE_CALL_DURATION_NAME, REQUEST_DURATION_NAME, QUEUE_WAIT_NAME} <= configured_views
    assert added_sinks == [{"sink": print}]


def test_sdk_disabled_keeps_telemetry_off(telemetry_environment: pytest.MonkeyPatch) -> None:
    configure_calls: list[dict[str, Any]] = []
    telemetry_environment.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4318")
    telemetry_environment.setenv("OTEL_SDK_DISABLED", "true")
    telemetry_environment.setattr(logfire, "configure", lambda **kwargs: configure_calls.append(kwargs))

    assert front_main.configure_telemetry() is False
    assert configure_calls == []
