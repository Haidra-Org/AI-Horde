# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for empty text generation reporting in ``TextProcessingGeneration``.

A text worker can submit a generation whose text is empty or whitespace only. The
submission is still stored and still paid for; the horde only records the event.
These tests cover the detection, the metric attributes, and the warning line and
counter call the report emits.

``_record_empty_generation`` reads plain attributes and calls no ``super()``, so it is
called with a lightweight stub as ``self`` rather than a persisted ORM graph.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from horde.classes.kobold import processing_generation as text_procgen_module
from horde.classes.kobold.processing_generation import (
    UNKNOWN_ATTRIBUTE_VALUE,
    TextProcessingGeneration,
    build_empty_generation_attributes,
    is_empty_text_generation,
)

BRIDGE_AGENT = "KoboldCppEmbedWorker:2:https://github.com/LostRuins/koboldcpp"
BRIDGE_NAME = "KoboldCppEmbedWorker"
MODEL_NAME = "koboldcpp/test-model"


class _RecordedCall:
    """One captured instrument call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


class _FakeCounter:
    """Captures ``add`` calls made on a metric counter."""

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []

    def add(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(_RecordedCall(*args, **kwargs))


class _FakeLogger:
    """Captures warning lines and discards every other level."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(message)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        pass


def _procgen_stub(
    model: str | None = MODEL_NAME,
    bridge_agent: str | None = BRIDGE_AGENT,
    prompt: str | None = "tell me a story",
) -> SimpleNamespace:
    """Build the ``self`` the empty-generation report reads.

    Args:
        model: The model the worker reported serving.
        bridge_agent: The worker's raw agent string.
        prompt: The waiting prompt's text, or None when there is none.

    Returns:
        A stub with only the attributes ``_record_empty_generation`` reads.
    """
    return SimpleNamespace(
        id="procgen-id-1",
        wp_id="wp-id-1",
        model=model,
        wp=SimpleNamespace(prompt=prompt, max_length=80, max_context_length=2048),
        worker=SimpleNamespace(id="worker-id-1", name="Test Worker", bridge_agent=bridge_agent),
    )


@pytest.fixture
def empty_generation_counter(monkeypatch: pytest.MonkeyPatch) -> _FakeCounter:
    """Replace the empty-generation counter with a capturing fake."""
    counter = _FakeCounter()
    monkeypatch.setattr(text_procgen_module, "text_empty_generations", counter)
    return counter


@pytest.fixture
def captured_logger(monkeypatch: pytest.MonkeyPatch) -> _FakeLogger:
    """Replace the module logger with a capturing fake."""
    fake_logger = _FakeLogger()
    monkeypatch.setattr(text_procgen_module, "logger", fake_logger)
    return fake_logger


class TestIsEmptyTextGeneration:
    @pytest.mark.parametrize("generation", [None, "", "   ", "\n\t  \n"])
    def test_blank_submissions_are_empty(self, generation: str | None) -> None:
        assert is_empty_text_generation(generation) is True

    @pytest.mark.parametrize("generation", ["hello", "  hello  ", "\n."])
    def test_text_submissions_are_not_empty(self, generation: str) -> None:
        assert is_empty_text_generation(generation) is False


class TestBuildEmptyGenerationAttributes:
    def test_bridge_agent_is_reduced_to_its_name(self) -> None:
        attributes = build_empty_generation_attributes(MODEL_NAME, BRIDGE_AGENT, "ok")

        assert attributes == {
            "model": MODEL_NAME,
            "bridge_agent": BRIDGE_NAME,
            "state": "ok",
        }

    def test_missing_model_and_agent_fall_back_to_unknown(self) -> None:
        attributes = build_empty_generation_attributes(None, None, "faulted")

        assert attributes == {
            "model": UNKNOWN_ATTRIBUTE_VALUE,
            "bridge_agent": UNKNOWN_ATTRIBUTE_VALUE,
            "state": "faulted",
        }

    def test_unparseable_agent_falls_back_to_unknown(self) -> None:
        attributes = build_empty_generation_attributes(MODEL_NAME, "nonsense", "ok")

        assert attributes["bridge_agent"] == UNKNOWN_ATTRIBUTE_VALUE


class TestRecordEmptyGeneration:
    def test_warning_line_carries_every_field(
        self,
        empty_generation_counter: _FakeCounter,
        captured_logger: _FakeLogger,
    ) -> None:
        stub = _procgen_stub()

        TextProcessingGeneration._record_empty_generation(stub, "ok", 7.5, 12.5)

        assert len(captured_logger.warnings) == 1
        logged_line = captured_logger.warnings[0]
        assert "\n" not in logged_line
        for expected_fragment in (
            "Empty text generation submitted",
            "procgen procgen-id-1 of wp wp-id-1",
            "by worker Test Worker (worker-id-1)",
            f"agent '{BRIDGE_AGENT}'",
            f"model '{MODEL_NAME}'",
            "state 'ok'",
            "max_length=80",
            "max_context_length=2048",
            f"prompt_characters={len(stub.wp.prompt)}",
            "things_per_sec=7.5",
            "kudos=12.5",
        ):
            assert expected_fragment in logged_line

    def test_counter_is_incremented_once_with_the_expected_attributes(
        self,
        empty_generation_counter: _FakeCounter,
        captured_logger: _FakeLogger,
    ) -> None:
        TextProcessingGeneration._record_empty_generation(_procgen_stub(), "ok", 7.5, 12.5)

        assert len(empty_generation_counter.calls) == 1
        counter_call = empty_generation_counter.calls[0]
        assert counter_call.args == (
            1,
            {"model": MODEL_NAME, "bridge_agent": BRIDGE_NAME, "state": "ok"},
        )

    def test_missing_prompt_reports_zero_characters(
        self,
        empty_generation_counter: _FakeCounter,
        captured_logger: _FakeLogger,
    ) -> None:
        TextProcessingGeneration._record_empty_generation(_procgen_stub(prompt=None), "ok", 7.5, 12.5)

        assert "prompt_characters=0" in captured_logger.warnings[0]

    def test_censored_state_reaches_both_the_line_and_the_counter(
        self,
        empty_generation_counter: _FakeCounter,
        captured_logger: _FakeLogger,
    ) -> None:
        TextProcessingGeneration._record_empty_generation(_procgen_stub(), "censored", 0.0, 0)

        assert "state 'censored'" in captured_logger.warnings[0]
        assert empty_generation_counter.calls[0].args[1]["state"] == "censored"
