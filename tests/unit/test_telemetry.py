# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verifies that ``horde.metrics`` instruments wire through to a real OTel
SDK MeterProvider after Logfire configures, and that ``init_telemetry_early``
runs cleanly on a bare Flask app (without requiring db.engine).

Locks in:

1. Metric instruments declared at module import in ``horde.metrics`` via
   ``logfire.metric_histogram`` / ``logfire.metric_counter`` resolve to real
   SDK instruments after ``logfire.configure()`` runs (Logfire's built-in
   proxy forwards ``record()`` / ``add()`` to the SDK instrument materialised
   on first use). No bespoke lazy-attribute / no-op shim required.

2. The early/late split keeps ``init_telemetry_early`` callable on a bare
   Flask app without requiring db.engine / models to exist.
"""

from __future__ import annotations

import pytest
from flask import Flask


@pytest.fixture(autouse=True)
def _isolate_telemetry_env(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    monkeypatch.delenv("PYROSCOPE_ENABLED", raising=False)


@pytest.fixture(scope="module")
def telemetry_app():
    """Bare Flask app with telemetry early-init applied."""
    from horde.telemetry import init_telemetry_early

    app = Flask("telemetry_test")
    init_telemetry_early(app)
    return app


def test_logfire_installed_real_meter_provider(telemetry_app):
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.metrics import NoOpMeterProvider

    provider = otel_metrics.get_meter_provider()
    assert not isinstance(provider, NoOpMeterProvider), (
        f"Logfire failed to install a real MeterProvider; got {type(provider).__name__}"
    )


def test_metric_instruments_record_after_init(telemetry_app):
    """A representative histogram and counter must accept record/add post-init."""
    from horde import metrics

    assert hasattr(metrics.generate_duration, "record")
    metrics.generate_duration.record(0.123, {"horde.smoke": "1"})

    assert hasattr(metrics.pop_skipped, "add")
    metrics.pop_skipped.add(1, {"horde.smoke": "1"})


def test_histogram_views_cover_all_registered_histograms(telemetry_app):
    """Every histogram declared via the bucket-profile helpers should produce
    a corresponding ``View`` so its boundaries reach the SDK."""
    from horde.metrics import _BUCKET_REGISTRY, histogram_views

    views = histogram_views()
    assert len(views) == len(_BUCKET_REGISTRY)
    view_names = {v._instrument_name for v in views}
    assert view_names == set(_BUCKET_REGISTRY)


def test_init_telemetry_early_is_idempotent(telemetry_app):
    from horde.telemetry import init_telemetry_early

    init_telemetry_early(telemetry_app)


def test_no_otel_span_missing_warning_on_404(telemetry_app, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    client = telemetry_app.test_client()
    rv = client.get("/__telemetry_smoke_404__")
    assert rv.status_code == 404
    bad = [r for r in caplog.records if "OpenTelemetry span missing" in r.getMessage()]
    assert not bad, f"Unexpected OTel span-missing warnings: {[r.getMessage() for r in bad]}"


def test_db_pool_timeout_counter_increments_on_pool_exhaustion(telemetry_app, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import TimeoutError as SAQueuePoolTimeoutError

    from horde import metrics
    from horde.flask import _InstrumentedQueuePool

    calls = []

    class _Recorder:
        def add(self, value, attrs=None):
            calls.append((value, attrs))

    monkeypatch.setattr(metrics, "db_pool_timeout", _Recorder())

    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=_InstrumentedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )

    held = engine.connect()
    try:
        with pytest.raises(SAQueuePoolTimeoutError):
            engine.connect()
    finally:
        held.close()
        engine.dispose()

    assert calls, "db_pool_timeout counter was not incremented on QueuePool TimeoutError"
    assert calls[0][0] == 1

    assert calls, "pool-timeout listener did not increment the counter"
    assert calls[0][0] == 1
