# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Completion lease of a text generation, set by ``TextProcessingGeneration.set_job_ttl``.

The lease is the fixed allowance plus the request's ``max_length`` at the assigned model's normal-speed threshold,
floored, then tripled for an extra-slow worker, rounded up to whole seconds and capped at the request lifetime.
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy.orm import Session

from horde.classes.kobold import processing_generation as text_processing_generation
from horde.classes.kobold.processing_generation import (
    TEXT_JOB_FIXED_SECONDS,
    TEXT_JOB_MINIMUM_SECONDS,
    TextProcessingGeneration,
)
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.utils import EXTRA_SLOW_REQUEST_LIFETIME_SECONDS, REQUEST_LIFETIME_SECONDS
from tests.fixture_types import MakeUser

pytestmark = pytest.mark.unit

THRESHOLD_TOKENS_PER_SECOND = 3
"""The normal-speed threshold of the largest text models, the slowest rate the lease is sized for."""

LONGEST_REPLY_TOKENS = 16384
"""The longest ``max_length`` a text request may ask for."""


def _create_generation(
    db_session: Session,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_length: int,
    extra_slow_worker: bool = False,
) -> TextProcessingGeneration:
    """Create a text generation for a request of ``max_length`` tokens at the 3 tokens/s threshold."""
    monkeypatch.setattr(
        text_processing_generation,
        "get_minimum_text_worker_speed",
        lambda model_names: THRESHOLD_TOKENS_PER_SECOND,
    )
    user = make_user()
    worker = TextWorker(
        user_id=user.id,
        name=f"ttl_worker_{max_length}_{extra_slow_worker}",
        extra_slow_worker=extra_slow_worker,
    )
    db_session.add(worker)
    db_session.commit()
    worker.set_models(["model_ttl"])
    waiting_prompt = TextWaitingPrompt(
        worker_ids=[],
        models=["model_ttl"],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={
            "n": 1,
            "max_length": max_length,
            "max_context_length": 32768,
        },
    )
    db_session.commit()
    return TextProcessingGeneration(
        wp_id=waiting_prompt.id,
        worker_id=worker.id,
        model="model_ttl",
    )


class TestTextJobTtl:
    """The lease scales with the requested output length and keeps the shared floor."""

    def test_a_short_request_receives_the_floor(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A request whose token term falls below the floor is leased the floor."""
        generation = _create_generation(db_session, make_user, monkeypatch, max_length=80)

        assert generation.job_ttl == TEXT_JOB_MINIMUM_SECONDS

    def test_a_mid_length_request_is_leased_its_length_at_the_threshold_rate(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 1024-token request at 3 tokens/s is leased the fixed allowance plus its length at that rate."""
        generation = _create_generation(db_session, make_user, monkeypatch, max_length=1024)

        expected_ttl = math.ceil(TEXT_JOB_FIXED_SECONDS + 1024 / THRESHOLD_TOKENS_PER_SECOND)
        assert expected_ttl < REQUEST_LIFETIME_SECONDS
        assert generation.job_ttl == expected_ttl

    def test_the_longest_request_is_leased_the_request_lifetime(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 16384-token request, whose term at 3 tokens/s exceeds the request lifetime, is leased that lifetime."""
        generation = _create_generation(db_session, make_user, monkeypatch, max_length=LONGEST_REPLY_TOKENS)

        assert TEXT_JOB_FIXED_SECONDS + LONGEST_REPLY_TOKENS / THRESHOLD_TOKENS_PER_SECOND > REQUEST_LIFETIME_SECONDS
        assert generation.job_ttl == REQUEST_LIFETIME_SECONDS

    def test_an_extra_slow_worker_is_capped_at_the_extra_slow_request_lifetime(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The tripled lease of an extra-slow worker is bounded by the longer lifetime such a request is given."""
        generation = _create_generation(
            db_session,
            make_user,
            monkeypatch,
            max_length=LONGEST_REPLY_TOKENS,
            extra_slow_worker=True,
        )

        assert generation.job_ttl == EXTRA_SLOW_REQUEST_LIFETIME_SECONDS

    def test_an_extra_slow_worker_triples_the_floored_lease(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The extra-slow multiplier applies after the floor, so a short request is leased three floors."""
        generation = _create_generation(
            db_session,
            make_user,
            monkeypatch,
            max_length=80,
            extra_slow_worker=True,
        )

        assert generation.job_ttl == TEXT_JOB_MINIMUM_SECONDS * 3
