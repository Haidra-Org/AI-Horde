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

import sys
from types import ModuleType

import pytest
from flask import Flask
from opentelemetry.trace import Status, StatusCode


@pytest.fixture(autouse=True)
def _isolate_telemetry_env(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    monkeypatch.delenv("AI_HORDE_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("PYROSCOPE_ENABLED", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_SAMPLE_RATE", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_SLOW_THRESHOLD_SECONDS", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_PROMOTION_COUNT", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_PROMOTION_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_PROMOTION_MAX_PER_MINUTE", raising=False)
    monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_ALWAYS_INCLUDE", raising=False)


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
    assert not isinstance(provider, NoOpMeterProvider), f"Logfire failed to install a real MeterProvider; got {type(provider).__name__}"


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


def test_request_estimate_validation_records_accuracy_and_coverage(telemetry_app, monkeypatch):
    from horde import metrics

    recorded_errors = []
    validations = []
    coverage = []

    class _HistogramRecorder:
        def record(self, value, attributes):
            recorded_errors.append((value, attributes))

    class _CounterRecorder:
        def __init__(self, destination):
            self.destination = destination

        def add(self, value, attributes):
            self.destination.append((value, attributes))

    monkeypatch.setattr(metrics, "request_estimate_absolute_error", _HistogramRecorder())
    monkeypatch.setattr(metrics, "request_estimate_validations", _CounterRecorder(validations))
    monkeypatch.setattr(metrics, "request_estimate_quantile_coverage", _CounterRecorder(coverage))

    metrics.record_request_estimate_validation(
        estimator="candidate-v1",
        gentype="image",
        phase="start",
        predicted_p50_seconds=100,
        predicted_p90_seconds=200,
        observed_seconds=150,
    )

    assert recorded_errors == [
        (
            50,
            {
                "horde.estimator": "candidate-v1",
                "horde.gentype": "image",
                "horde.phase": "start",
            },
        ),
    ]
    assert validations[0][1]["horde.result"] == "within_tolerance"
    assert coverage[0][1]["horde.result"] == "covered"


def test_request_estimate_validation_rejects_invalid_quantiles(telemetry_app):
    from horde import metrics

    with pytest.raises(ValueError, match="p90"):
        metrics.record_request_estimate_validation(
            estimator="candidate-v1",
            gentype="text",
            phase="completion",
            predicted_p50_seconds=100,
            predicted_p90_seconds=90,
            observed_seconds=95,
        )


def test_request_assignment_pressure_records_bounded_evidence(telemetry_app, monkeypatch):
    from horde import metrics

    recorded: dict[str, list[tuple[float, dict[str, object]]]] = {
        "dispatch": [],
        "lost": [],
        "returned_capacity": [],
        "active": [],
        "arriving_work": [],
        "returned_work": [],
        "lost_share": [],
        "samples": [],
    }

    class _HistogramRecorder:
        def __init__(self, destination):
            self.destination = destination

        def record(self, value, attributes):
            self.destination.append((value, attributes))

    class _CounterRecorder:
        def add(self, value, attributes):
            recorded["samples"].append((value, attributes))

    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_dispatch_opportunities",
        _HistogramRecorder(recorded["dispatch"]),
    )
    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_lost_opportunities",
        _HistogramRecorder(recorded["lost"]),
    )
    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_returned_capacity",
        _HistogramRecorder(recorded["returned_capacity"]),
    )
    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_active_preceding",
        _HistogramRecorder(recorded["active"]),
    )
    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_arriving_work",
        _HistogramRecorder(recorded["arriving_work"]),
    )
    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_returned_work",
        _HistogramRecorder(recorded["returned_work"]),
    )
    monkeypatch.setattr(
        metrics,
        "request_assignment_pressure_lost_share",
        _HistogramRecorder(recorded["lost_share"]),
    )
    monkeypatch.setattr(metrics, "request_assignment_pressure_samples", _CounterRecorder())

    metrics.record_request_assignment_pressure(
        gentype="image",
        evidence="arrival_outpaces_drain",
        might_stall=True,
        dispatch_opportunities=4,
        lost_opportunities=3,
        returned_capacity=2,
        active_preceding_dispatches=1,
        arriving_preceding_work=7.5,
        returned_work=2.5,
    )

    expected_histogram_attributes = {
        "horde.gentype": "image",
        "horde.might_stall": True,
    }
    assert recorded["dispatch"] == [(4, expected_histogram_attributes)]
    assert recorded["lost"] == [(3, expected_histogram_attributes)]
    assert recorded["returned_capacity"] == [(2, expected_histogram_attributes)]
    assert recorded["active"] == [(1, expected_histogram_attributes)]
    assert recorded["arriving_work"] == [(7.5, expected_histogram_attributes)]
    assert recorded["returned_work"] == [(2.5, expected_histogram_attributes)]
    assert recorded["lost_share"] == [(0.75, expected_histogram_attributes)]
    assert recorded["samples"] == [
        (
            1,
            {
                **expected_histogram_attributes,
                "horde.evidence": "arrival_outpaces_drain",
            },
        ),
    ]


def test_request_assignment_pressure_rejects_impossible_counts(telemetry_app):
    from horde import metrics

    with pytest.raises(ValueError, match="Lost opportunities"):
        metrics.record_request_assignment_pressure(
            gentype="text",
            evidence="target_opportunity_seen",
            might_stall=False,
            dispatch_opportunities=1,
            lost_opportunities=2,
            returned_capacity=0,
            active_preceding_dispatches=0,
            arriving_preceding_work=0,
            returned_work=0,
        )

    with pytest.raises(ValueError, match="Unknown assignment-pressure evidence"):
        metrics.record_request_assignment_pressure(
            gentype="text",
            evidence="request-id-would-be-unbounded",
            might_stall=False,
            dispatch_opportunities=0,
            lost_opportunities=0,
            returned_capacity=0,
            active_preceding_dispatches=0,
            arriving_preceding_work=0,
            returned_work=0,
        )


@pytest.mark.parametrize(
    ("predicted_stall", "expired_without_start", "expected_result"),
    [
        (True, True, "true_positive"),
        (True, False, "false_positive"),
        (False, True, "false_negative"),
        (False, False, "true_negative"),
    ],
)
def test_request_stall_validation_records_confusion_outcome(
    telemetry_app,
    monkeypatch,
    predicted_stall,
    expired_without_start,
    expected_result,
):
    from horde import metrics

    calls = []

    class _Recorder:
        def add(self, value, attributes):
            calls.append((value, attributes))

    monkeypatch.setattr(metrics, "request_stall_validations", _Recorder())
    metrics.record_request_stall_validation(
        estimator="candidate-v1",
        gentype="text",
        predicted_stall=predicted_stall,
        expired_without_start=expired_without_start,
    )

    assert calls[0][1]["horde.result"] == expected_result


def test_init_telemetry_early_is_idempotent(telemetry_app):
    from horde.telemetry import init_telemetry_early

    init_telemetry_early(telemetry_app)


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, False),
        ({"AI_HORDE_TELEMETRY_ENABLED": "true"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, True),
        ({"PYROSCOPE_ENABLED": "true"}, True),
        ({"OTEL_SDK_DISABLED": "true"}, False),
        ({"OTEL_SDK_DISABLED": "true", "AI_HORDE_TELEMETRY_ENABLED": "true"}, False),
        ({"OTEL_SDK_DISABLED": "true", "PYROSCOPE_ENABLED": "true"}, True),
    ],
)
def test_telemetry_enabled_activation_matrix(monkeypatch, environment, expected):
    from horde.telemetry import telemetry_enabled

    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    assert telemetry_enabled() is expected


def test_profiling_only_initializes_pyroscope_without_logfire(monkeypatch):
    from horde import telemetry

    pyroscope_calls = []
    monkeypatch.setattr(telemetry, "_initialized_early", False)
    monkeypatch.setattr(
        telemetry,
        "_init_pyroscope",
        lambda *, span_profiles_enabled: pyroscope_calls.append(span_profiles_enabled) or [],
    )
    monkeypatch.setattr(
        telemetry.logfire,
        "configure",
        lambda **_kwargs: pytest.fail("Logfire must not initialize in profiling-only mode"),
    )
    monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    telemetry.init_telemetry_early(Flask("profiling-only"))

    assert pyroscope_calls == [False]


@pytest.mark.parametrize("span_profiles", [None, "false"])
def test_pyroscope_span_processor_is_disabled_by_default(monkeypatch, span_profiles):
    from horde import telemetry

    configure_calls = []
    pyroscope_module = ModuleType("pyroscope")
    pyroscope_module.__path__ = []
    pyroscope_module.configure = lambda **kwargs: configure_calls.append(kwargs)

    class UnexpectedSpanProcessor:
        def __init__(self):
            raise AssertionError("PyroscopeSpanProcessor must remain disabled")

    pyroscope_otel_module = ModuleType("pyroscope.otel")
    pyroscope_otel_module.PyroscopeSpanProcessor = UnexpectedSpanProcessor
    monkeypatch.setitem(sys.modules, "pyroscope", pyroscope_module)
    monkeypatch.setitem(sys.modules, "pyroscope.otel", pyroscope_otel_module)
    monkeypatch.setattr(telemetry.logger, "init_ok", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
    if span_profiles is None:
        monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES", raising=False)
    else:
        monkeypatch.setenv("PYROSCOPE_SPAN_PROFILES", span_profiles)

    assert telemetry._init_pyroscope() == []
    assert len(configure_calls) == 1


def test_pyroscope_span_processor_requires_explicit_opt_in(monkeypatch):
    from horde import telemetry

    pyroscope_module = ModuleType("pyroscope")
    pyroscope_module.__path__ = []
    pyroscope_module.configure = lambda **_kwargs: None

    class FakeSpanProcessor:
        pass

    pyroscope_otel_module = ModuleType("pyroscope.otel")
    pyroscope_otel_module.PyroscopeSpanProcessor = FakeSpanProcessor
    monkeypatch.setitem(sys.modules, "pyroscope", pyroscope_module)
    monkeypatch.setitem(sys.modules, "pyroscope.otel", pyroscope_otel_module)
    monkeypatch.setattr(telemetry.logger, "init_ok", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
    monkeypatch.setenv("PYROSCOPE_SPAN_PROFILES", "true")
    monkeypatch.setenv("PYROSCOPE_SPAN_PROFILES_SAMPLE_RATE", "1")

    processors = telemetry._init_pyroscope()

    assert len(processors) == 1
    assert isinstance(processors[0], telemetry._TraceRatioSpanProcessor)
    assert isinstance(processors[0]._processor, FakeSpanProcessor)


def test_profiling_only_skips_span_processor_when_otel_sdk_is_disabled(monkeypatch):
    from horde import telemetry

    configure_calls = []
    pyroscope_module = ModuleType("pyroscope")
    pyroscope_module.__path__ = []
    pyroscope_module.configure = lambda **kwargs: configure_calls.append(kwargs)

    class UnexpectedSpanProcessor:
        def __init__(self):
            raise AssertionError("Span correlation cannot run without the OTel SDK")

    pyroscope_otel_module = ModuleType("pyroscope.otel")
    pyroscope_otel_module.PyroscopeSpanProcessor = UnexpectedSpanProcessor
    monkeypatch.setitem(sys.modules, "pyroscope", pyroscope_module)
    monkeypatch.setitem(sys.modules, "pyroscope.otel", pyroscope_otel_module)
    monkeypatch.setattr(telemetry.logger, "init_ok", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(telemetry.logger, "init_warn", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setenv("PYROSCOPE_ENABLED", "true")
    monkeypatch.setenv("PYROSCOPE_SPAN_PROFILES", "true")

    assert telemetry._init_pyroscope(span_profiles_enabled=False) == []
    assert len(configure_calls) == 1


@pytest.mark.parametrize("raw_rate", [None, "invalid", "1.1", "-0.1"])
def test_pyroscope_span_profile_sample_rate_fails_safe(monkeypatch, raw_rate):
    from horde import telemetry

    monkeypatch.setattr(telemetry.logger, "init_warn", lambda *_args, **_kwargs: None, raising=False)
    if raw_rate is None:
        monkeypatch.delenv("PYROSCOPE_SPAN_PROFILES_SAMPLE_RATE", raising=False)
    else:
        monkeypatch.setenv("PYROSCOPE_SPAN_PROFILES_SAMPLE_RATE", raw_rate)

    assert telemetry._pyroscope_span_profile_sample_rate() == 0.10


def test_pyroscope_span_processor_uses_deterministic_trace_ratio():
    from horde.telemetry import _TRACE_ID_SPACE, _TraceRatioSpanProcessor

    calls = []

    class RecordingSpanProcessor:
        def on_start(self, span, parent_context=None):
            calls.append(("start", span.context.trace_id, parent_context))

        def on_end(self, span):
            calls.append(("end", span.context.trace_id))

        def shutdown(self):
            calls.append(("shutdown",))

        def force_flush(self, timeout_millis=30000):
            calls.append(("flush", timeout_millis))
            return True

    class Context:
        def __init__(self, trace_id, span_id=1):
            self.trace_id = trace_id
            self.span_id = span_id

    class Span:
        def __init__(self, trace_id):
            self.context = Context(trace_id)
            self.name = "test"
            self.parent = None
            self.attributes = {}
            self.status = Status(StatusCode.UNSET)
            self.start_time = 0
            self.end_time = 0

    processor = _TraceRatioSpanProcessor(RecordingSpanProcessor(), 0.5)
    selected = Span((_TRACE_ID_SPACE // 2) - 1)
    rejected = Span(_TRACE_ID_SPACE // 2)

    processor.on_start(selected, "parent")
    processor.on_start(rejected, "parent")
    processor.on_end(selected)
    processor.on_end(rejected)
    assert processor.force_flush(123)
    processor.shutdown()

    assert calls == [
        ("start", selected.context.trace_id, "parent"),
        ("end", selected.context.trace_id),
        ("flush", 123),
        ("shutdown",),
    ]


class _RecordingPyroscopeProcessor:
    def __init__(self):
        self.calls = []

    def on_start(self, span, parent_context=None):
        self.calls.append(("start", span.context.span_id))

    def on_end(self, span):
        self.calls.append(("end", span.context.span_id))

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class _TestSpan:
    def __init__(
        self,
        span_id,
        *,
        name="critical.operation",
        duration_seconds=0.0,
        attributes=None,
        error=False,
        parent=None,
    ):
        self.context = type("Context", (), {"trace_id": (1 << 128) - 1, "span_id": span_id})()
        self.name = name
        self.parent = parent
        self.attributes = attributes or {}
        self.status = Status(StatusCode.ERROR if error else StatusCode.UNSET)
        self.start_time = 0
        self.end_time = int(duration_seconds * 1_000_000_000)


def test_pyroscope_span_processor_always_includes_forced_and_matching_root_spans():
    from horde.telemetry import _TraceRatioSpanProcessor

    delegate = _RecordingPyroscopeProcessor()
    processor = _TraceRatioSpanProcessor(
        delegate,
        0,
        always_include_operations="critical.operation,another.operation",
    )
    forced = _TestSpan(1, name="ordinary", attributes={"pyroscope.span_profile.force": True})
    matching = _TestSpan(2)

    for span in (forced, matching):
        processor.on_start(span)
        processor.on_end(span)

    assert delegate.calls == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]


def test_pyroscope_span_key_does_not_use_high_cardinality_http_target():
    from horde.telemetry import _TraceRatioSpanProcessor

    delegate = _RecordingPyroscopeProcessor()
    processor = _TraceRatioSpanProcessor(delegate, 0, promotion_count=1)
    outlier = _TestSpan(
        1,
        name="GET request",
        duration_seconds=2,
        attributes={"http.target": "/users/first-user-id"},
    )
    recurrence = _TestSpan(
        2,
        name="GET request",
        attributes={"http.target": "/users/second-user-id"},
    )

    processor.on_start(outlier)
    processor.on_end(outlier)
    processor.on_start(recurrence)
    processor.on_end(recurrence)

    assert delegate.calls == [("start", 2), ("end", 2)]


@pytest.mark.parametrize("outlier", [{"duration_seconds": 1.0}, {"error": True}])
def test_pyroscope_outlier_arms_follow_up_span_profiles(outlier):
    from horde.telemetry import _TraceRatioSpanProcessor

    delegate = _RecordingPyroscopeProcessor()
    processor = _TraceRatioSpanProcessor(delegate, 0, promotion_count=2)
    first = _TestSpan(1, **outlier)
    processor.on_start(first)
    processor.on_end(first)

    for span_id in (2, 3, 4):
        span = _TestSpan(span_id)
        processor.on_start(span)
        processor.on_end(span)

    assert delegate.calls == [("start", 2), ("end", 2), ("start", 3), ("end", 3)]


def test_pyroscope_adaptive_promotions_are_rate_limited():
    from horde.telemetry import _TraceRatioSpanProcessor

    now = [0.0]
    delegate = _RecordingPyroscopeProcessor()
    processor = _TraceRatioSpanProcessor(
        delegate,
        0,
        promotion_count=3,
        promotion_max_per_minute=1,
        clock=lambda: now[0],
    )
    outlier = _TestSpan(1, duration_seconds=2)
    processor.on_start(outlier)
    processor.on_end(outlier)

    for span_id in (2, 3):
        span = _TestSpan(span_id)
        processor.on_start(span)
        processor.on_end(span)
    now[0] = 60
    third = _TestSpan(4)
    processor.on_start(third)
    processor.on_end(third)

    assert delegate.calls == [("start", 2), ("end", 2), ("start", 4), ("end", 4)]


def test_pyroscope_child_spans_do_not_create_profile_series():
    from horde.telemetry import _TraceRatioSpanProcessor

    delegate = _RecordingPyroscopeProcessor()
    processor = _TraceRatioSpanProcessor(delegate, 1)
    local_parent = type("Parent", (), {"is_remote": False})()
    child = _TestSpan(1, parent=local_parent)

    processor.on_start(child)
    processor.on_end(child)

    assert delegate.calls == []


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
