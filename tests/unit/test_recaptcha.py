# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test the reCAPTCHA verification of the register page, with a stand-in for Google's verification endpoint.

The verification checks whether a registration form submission came from a person. An operator tunes it with a
minimum score, and the metrics show whether it runs at all. A deployment that sets a bad threshold must keep a working
default, and an outage at Google must not count as a person passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from horde import recaptcha
from horde.recaptcha import (
    DEFAULT_MIN_SCORE,
    RecaptchaOutcome,
    min_score,
    record_recaptcha_disabled,
    refusal_message,
    verify_registration_token,
)


@dataclass
class _Metrics:
    """Record the verification counts and scores."""

    outcomes: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.outcomes.extend([attributes["horde.outcome"]] * amount)

    def record(self, value: float) -> None:
        self.scores.append(value)


@pytest.fixture(autouse=True)
def metrics(monkeypatch: pytest.MonkeyPatch) -> _Metrics:
    """Clear the threshold settings and record the metrics."""
    recorded = _Metrics()
    monkeypatch.setattr(recaptcha, "recaptcha_verifications", recorded)
    monkeypatch.setattr(recaptcha, "recaptcha_scores", SimpleNamespace(record=recorded.record))
    monkeypatch.delenv("RECAPTCHA_MIN_SCORE", raising=False)
    return recorded


def _site_verify(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any] | Exception) -> list[dict[str, Any]]:
    """Replace the verification endpoint with one that returns ``result`` or raises it, and return the calls."""
    calls: list[dict[str, Any]] = []

    def _post(url: str, **kwargs: Any) -> SimpleNamespace:
        calls.append({"url": url, **kwargs})
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    monkeypatch.setattr(recaptcha.requests, "post", _post)
    return calls


class TestVerification:
    """A submission passes only with a valid token for the register page's action and at least the minimum score."""

    @pytest.mark.parametrize(
        ("result", "outcome"),
        [
            ({"success": True, "score": 0.9, "action": "submit"}, RecaptchaOutcome.PASSED),
            ({"success": True, "score": 0.5, "action": "submit"}, RecaptchaOutcome.PASSED),
            ({"success": True, "score": 0.4, "action": "submit"}, RecaptchaOutcome.LOW_SCORE),
            ({"success": True, "score": 0.9, "action": "login"}, RecaptchaOutcome.WRONG_ACTION),
            ({"success": False, "error-codes": ["timeout-or-duplicate"]}, RecaptchaOutcome.FAILED),
            ({"success": True}, RecaptchaOutcome.PASSED),
        ],
        ids=["likely-person", "at-the-minimum", "below-the-minimum", "token-from-another-page", "reused-token", "v2-key"],
    )
    def test_the_result_from_google_decides_the_outcome(
        self, monkeypatch: pytest.MonkeyPatch, metrics: _Metrics, result: dict[str, Any], outcome: RecaptchaOutcome
    ) -> None:
        _site_verify(monkeypatch, result)

        verdict = verify_registration_token("token", "secret")

        assert verdict.outcome is outcome
        assert metrics.outcomes == [outcome.value]
        assert metrics.scores == ([result["score"]] if "score" in result else [])

    @pytest.mark.parametrize("token", ["", None])
    def test_a_form_without_a_token_fails_without_calling_google(self, monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
        """A missing token means that the reCAPTCHA script did not run in the browser, so it gets a separate outcome and message."""
        calls = _site_verify(monkeypatch, {"success": True})

        verdict = verify_registration_token(token, "secret")

        assert (verdict.outcome, verdict.passed) == (RecaptchaOutcome.MISSING_TOKEN, False)
        assert calls == []

    @pytest.mark.parametrize("error", [requests.ConnectionError("down"), requests.Timeout("slow")])
    def test_an_outage_at_google_does_not_pass(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        """An outage must not let automated registrations through while Google cannot score them."""
        _site_verify(monkeypatch, error)

        verdict = verify_registration_token("token", "secret")

        assert verdict.outcome is RecaptchaOutcome.UNAVAILABLE
        assert not verdict.passed

    def test_only_the_token_and_secret_are_sent_and_the_call_has_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The registrant's address is not shared with Google, and a slow endpoint does not hold a web worker forever."""
        calls = _site_verify(monkeypatch, {"success": True, "score": 0.9, "action": "submit"})

        verify_registration_token("token", "secret")

        [call] = calls
        assert call["data"] == {"response": "token", "secret": "secret"}
        assert call["timeout"] > 0

    def test_a_deployment_without_recaptcha_is_counted(self, metrics: _Metrics) -> None:
        """The count of disabled verifications shows an operator that reCAPTCHA is off."""
        record_recaptcha_disabled()

        assert metrics.outcomes == ["disabled"]


class TestThresholds:
    """Operators set the minimum score through the environment, and a bad value falls back to the default."""

    def test_the_default_follows_googles_suggestion(self) -> None:
        assert min_score() == DEFAULT_MIN_SCORE

    @pytest.mark.parametrize("raw_value", ["high", "1.5", "-0.1", "  "])
    def test_a_value_that_is_not_a_score_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch, raw_value: str) -> None:
        monkeypatch.setenv("RECAPTCHA_MIN_SCORE", raw_value)

        assert min_score() == DEFAULT_MIN_SCORE

    def test_a_changed_minimum_applies_to_the_next_submission(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _site_verify(monkeypatch, {"success": True, "score": 0.3, "action": "submit"})
        monkeypatch.setenv("RECAPTCHA_MIN_SCORE", "0.2")

        assert verify_registration_token("token", "secret").passed


class TestRefusalMessages:
    """Each way a form can fail the check leads to a different next step for the person, so each has a different message."""

    def test_each_refusing_outcome_has_a_distinct_message(self) -> None:
        """A deployment without reCAPTCHA does not refuse anyone, so the disabled outcome does not need a message."""
        refusing_outcomes = [outcome for outcome in RecaptchaOutcome if outcome not in {RecaptchaOutcome.PASSED, RecaptchaOutcome.DISABLED}]

        messages = [refusal_message(outcome) for outcome in refusing_outcomes]

        assert all(messages)
        assert len(set(messages)) == len(messages)

    def test_an_outage_asks_the_person_to_retry_later_rather_than_resubmit(self) -> None:
        """Resubmitting at once during an outage fails again, so the message points to waiting."""
        assert "try again in a few minutes" in refusal_message(RecaptchaOutcome.UNAVAILABLE)
