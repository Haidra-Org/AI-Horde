# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the reCAPTCHA token of a registration form submission.

The registration page uses reCAPTCHA v3, which does not show a puzzle. Google returns a score from 0.0 (likely
automated) to 1.0 (likely a person) and the action name set by the page. A submission passes when the token is valid,
the action is ``REGISTRATION_ACTION``, and the score is at least the minimum score. A submission that cannot be
verified does not pass, including when the verification endpoint cannot be reached.

The verification is enabled by ``RECAPTCHA_SECRET_KEY``. The minimum score comes from ``RECAPTCHA_MIN_SCORE``. Both are
read when a submission is verified, so a change to the environment applies without a code change.

Public members:

- [`verify_registration_token`][horde.recaptcha.verify_registration_token]: verify one submission.
- [`record_recaptcha_disabled`][horde.recaptcha.record_recaptcha_disabled]: count a submission without reCAPTCHA.
- [`refusal_message`][horde.recaptcha.refusal_message]: the message shown for a submission that did not pass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

import requests

from horde.logger import logger
from horde.metrics import recaptcha_scores, recaptcha_verifications

__all__ = [
    "DEFAULT_MIN_SCORE",
    "REGISTRATION_ACTION",
    "RecaptchaOutcome",
    "RecaptchaVerdict",
    "min_score",
    "record_recaptcha_disabled",
    "refusal_message",
    "verify_registration_token",
]

SITEVERIFY_URL: str = "https://www.google.com/recaptcha/api/siteverify"
"""The verification endpoint of reCAPTCHA."""
SITEVERIFY_TIMEOUT_SECONDS: float = 5.0
"""The timeout of a call to the verification endpoint."""
REGISTRATION_ACTION: str = "submit"
"""The action name set by the registration page (``data-action`` in ``register.html``)."""
DEFAULT_MIN_SCORE: float = 0.5
"""The minimum score of every registration, when ``RECAPTCHA_MIN_SCORE`` is not set. This is the default suggested by
Google."""


class RecaptchaOutcome(StrEnum):
    """Represents the result of a reCAPTCHA verification, as a metric attribute."""

    PASSED = "passed"
    """The token is valid, and the action and score are acceptable."""
    LOW_SCORE = "low_score"
    """The token is valid, and the score is below the minimum score."""
    WRONG_ACTION = "wrong_action"
    """The token is valid, and it was issued for another action."""
    MISSING_TOKEN = "missing_token"
    """The form was submitted without a token, as when the page's scripts did not run."""
    FAILED = "failed"
    """The token is expired, reused or invalid."""
    UNAVAILABLE = "unavailable"
    """The verification endpoint could not be reached or returned an error."""
    DISABLED = "disabled"
    """``RECAPTCHA_SECRET_KEY`` is not set, so the submission was not verified."""


@dataclass(frozen=True, slots=True)
class RecaptchaVerdict:
    """Represents the result of verifying one registration token."""

    outcome: RecaptchaOutcome
    """The result of the verification."""
    score: float | None = None
    """The v3 score. It is None when the endpoint did not return a score, as for a v2 key."""

    @property
    def passed(self) -> bool:
        """Whether the submission passed the minimum score."""
        return self.outcome is RecaptchaOutcome.PASSED


_REFUSAL_MESSAGES: dict[RecaptchaOutcome, str] = {
    RecaptchaOutcome.UNAVAILABLE: "The reCAPTCHA check could not be completed just now. Please try again in a few minutes.",
    RecaptchaOutcome.MISSING_TOKEN: (
        "The form was sent without a reCAPTCHA token. The check needs JavaScript, so please allow scripts on this page and try again."
    ),
    RecaptchaOutcome.FAILED: "The reCAPTCHA check expired or was not valid. Please submit the form again.",
    RecaptchaOutcome.WRONG_ACTION: "The reCAPTCHA check was not valid for this form. Please submit the form again.",
    RecaptchaOutcome.LOW_SCORE: "reCAPTCHA could not confirm that this registration came from a person. Please try again.",
}


def refusal_message(outcome: RecaptchaOutcome) -> str:
    """Return the message shown to a person whose registration form did not pass the check.

    Args:
        outcome: The outcome of a verification that did not pass.

    Returns:
        The message. It does not mention the score or the thresholds.
    """
    return _REFUSAL_MESSAGES.get(outcome, _REFUSAL_MESSAGES[RecaptchaOutcome.FAILED])


def _score_from_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning(f"{name} is not a number, so the default {default} is used")
        return default
    if not 0.0 <= value <= 1.0:
        logger.warning(f"{name} is not between 0 and 1, so the default {default} is used")
        return default
    return value


def min_score() -> float:
    """Return the minimum score of every registration, from ``RECAPTCHA_MIN_SCORE``."""
    return _score_from_env("RECAPTCHA_MIN_SCORE", DEFAULT_MIN_SCORE)


def _record(verdict: RecaptchaVerdict) -> RecaptchaVerdict:
    recaptcha_verifications.add(1, {"horde.outcome": verdict.outcome.value})
    if verdict.score is not None:
        recaptcha_scores.record(verdict.score)
    return verdict


def record_recaptcha_disabled() -> None:
    """Count a registration form submission that was not verified, because ``RECAPTCHA_SECRET_KEY`` is not set."""
    _record(RecaptchaVerdict(outcome=RecaptchaOutcome.DISABLED))


def verify_registration_token(token: str | None, secret_key: str) -> RecaptchaVerdict:
    """Verify the reCAPTCHA token of a registration form submission against the minimum score.

    The address of the registrant is not sent to Google. Each result adds one count to ``horde.recaptcha.verifications``,
    and each score is recorded in ``horde.recaptcha.score``.

    Args:
        token: The ``g-recaptcha-response`` form field.
        secret_key: The value of ``RECAPTCHA_SECRET_KEY``.

    Returns:
        The verdict.
    """
    if not token:
        return _record(RecaptchaVerdict(outcome=RecaptchaOutcome.MISSING_TOKEN))
    try:
        response = requests.post(
            SITEVERIFY_URL,
            data={"response": token, "secret": secret_key},
            timeout=SITEVERIFY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as err:
        logger.error(f"reCAPTCHA verification failed: {err}")
        return _record(RecaptchaVerdict(outcome=RecaptchaOutcome.UNAVAILABLE))
    raw_score = result.get("score")
    score = float(raw_score) if isinstance(raw_score, int | float) else None
    if not result.get("success"):
        return _record(RecaptchaVerdict(outcome=RecaptchaOutcome.FAILED, score=score))
    action = result.get("action")
    if action is not None and action != REGISTRATION_ACTION:
        return _record(RecaptchaVerdict(outcome=RecaptchaOutcome.WRONG_ACTION, score=score))
    if score is not None and score < min_score():
        return _record(RecaptchaVerdict(outcome=RecaptchaOutcome.LOW_SCORE, score=score))
    return _record(RecaptchaVerdict(outcome=RecaptchaOutcome.PASSED, score=score))
