# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise span-profile correlation against the real optional dependencies."""

from __future__ import annotations

from collections import Counter

import pyroscope
from opentelemetry.sdk.trace import TracerProvider
from pyroscope.otel import PyroscopeSpanProcessor

from horde.telemetry import _TraceRatioSpanProcessor


def test_real_pyroscope_processor_correlates_real_sdk_root_span(monkeypatch) -> None:
    """Verify the real Pyroscope processor accepts the wrapped SDK lifecycle."""
    added_tags: list[tuple[str, str]] = []
    removed_tags: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pyroscope,
        "add_thread_tag",
        lambda key, value: added_tags.append((key, value)),
    )
    monkeypatch.setattr(
        pyroscope,
        "remove_thread_tag",
        lambda key, value: removed_tags.append((key, value)),
    )

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(
        _TraceRatioSpanProcessor(
            PyroscopeSpanProcessor(),
            0,
            always_include_operations="critical.operation",
        ),
    )
    tracer = tracer_provider.get_tracer(__name__)

    with tracer.start_as_current_span("critical.operation"):
        pass
    tracer_provider.shutdown()

    # Pyroscope may add correlation fields across compatible releases.  Verify
    # the fields our integration relies on while allowing additive metadata.
    added_tag_names = {tag_name for tag_name, _tag_value in added_tags}
    assert {"span_id", "span_name"} <= added_tag_names
    assert Counter(removed_tags) == Counter(added_tags)
