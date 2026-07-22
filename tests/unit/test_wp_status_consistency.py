# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the internal consistency of ``WaitingPrompt.get_status``.

``get_status`` assembles a status payload from three sources that are read at
different moments: ``self.n``, loaded with the instance; the validity verdict,
sampled by the caller and passed in; and the generation counts, read from
``processing_gens`` when the payload is built. A generation that starts in
between leaves the earlier two describing a state the counts have already moved
past.

The payload is a single answer to a single client question, so the contracts
exercised here are the ones a client can check without a second request: a
request reported as processing is reported as possible, and the outstanding,
in-flight, and finished generations never exceed the ``jobs`` the request
originally asked for.

Every test uses ``fake_redis`` because the status path reads Redis-cached
performance and worker data. The ``_stub_model_reference`` autouse fixture pins
the image model reference to a minimal in-memory dict so procgen construction
stays hermetic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from horde.classes.base.worker import WorkerModel
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.flask import db

pytestmark = pytest.mark.unit

_HOSTED_MODEL = "stable_diffusion"


@pytest.fixture(autouse=True)
def _stub_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the image model reference to a minimal in-memory dict."""
    from horde import model_reference as model_reference_module

    minimal_reference = {_HOSTED_MODEL: {"baseline": "stable diffusion 1"}}
    monkeypatch.setattr(model_reference_module.model_reference, "reference", minimal_reference)


def _make_worker(user: Any) -> ImageWorker:
    """Create and persist an ``ImageWorker`` hosting the reference model."""
    worker = ImageWorker(
        user_id=user.id,
        name=f"worker_{uuid.uuid4().hex[:12]}",
        max_pixels=1024 * 1024,
        last_check_in=datetime.utcnow(),
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=_HOSTED_MODEL))
    db.session.commit()
    return worker


def _make_wp(user: Any, *, n: int = 1) -> ImageWaitingPrompt:
    """Create and persist an ``ImageWaitingPrompt`` requesting ``n`` generations."""
    wp = ImageWaitingPrompt(
        [],
        [_HOSTED_MODEL],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={
            "n": n,
            "width": 512,
            "height": 512,
            "steps": 10,
            "sampler_name": "k_euler_a",
            "karras": True,
        },
    )
    db.session.commit()
    return wp


def _make_procgen(
    wp: ImageWaitingPrompt,
    worker: ImageWorker,
    *,
    generation: str | None = None,
    faulted: bool = False,
) -> ImageProcessingGeneration:
    """Create and persist a procgen in a chosen state.

    An in-flight procgen has ``generation is None`` and ``faulted`` False.
    Setting ``generation`` marks it completed; ``faulted`` marks it restarted.
    The state is written directly rather than via ``set_generation`` / ``abort``
    to avoid the kudos, R2 upload and webhook machinery those carry, none of
    which affects the counts under test.
    """
    procgen = ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model=_HOSTED_MODEL)
    if generation is not None:
        procgen.generation = generation
    if faulted:
        procgen.faulted = True
    db.session.commit()
    return procgen


def _status(wp: ImageWaitingPrompt, *, has_valid_workers: bool = True) -> dict[str, Any]:
    """Return ``wp.get_status`` with neutral queue and worker-population inputs.

    The queue statistics and worker population only feed the wait-time estimate,
    which none of these contracts depend on, so they are held at fixed neutral
    values.
    """
    return wp.get_status(
        request_avg=1.0,
        active_worker_count=(1, 1),
        has_valid_workers=has_valid_workers,
        wp_queue_stats=(0, 0, 0),
        lite=True,
    )


class TestPossibilityAgreesWithTheProcessingCount:
    """A request reported as processing is reported as possible."""

    def test_in_flight_generation_overrides_an_impossible_verdict(self, db_session, fake_redis, make_user):
        # The verdict passed in describes the request before its generation
        # started; the counts describe it after. A client seeing processing=1
        # alongside is_possible=false cannot act on either answer.
        user = make_user()
        wp = _make_wp(user)
        worker = _make_worker(user)
        _make_procgen(wp, worker)
        wp.n = 0
        db.session.commit()

        status = _status(wp, has_valid_workers=False)

        assert status["processing"] == 1
        assert status["is_possible"] is True

    def test_verdict_is_reported_unchanged_when_nothing_is_processing(self, db_session, fake_redis, make_user):
        # With no generation in flight there is nothing to contradict, so the
        # caller's verdict stands: a request no worker can serve stays impossible.
        user = make_user()
        wp = _make_wp(user)

        assert _status(wp, has_valid_workers=False)["is_possible"] is False
        assert _status(wp, has_valid_workers=True)["is_possible"] is True

    def test_finished_generation_does_not_force_possible(self, db_session, fake_redis, make_user):
        # A completed generation is no longer in flight, so it says nothing about
        # whether the remaining work can be served.
        user = make_user()
        wp = _make_wp(user, n=2)
        worker = _make_worker(user)
        _make_procgen(wp, worker, generation="done")
        wp.n = 1
        db.session.commit()

        status = _status(wp, has_valid_workers=False)

        assert status["finished"] == 1
        assert status["processing"] == 0
        assert status["is_possible"] is False


class TestOutstandingGenerationsNeverExceedTheRequest:
    """Waiting, processing, and finished generations stay within the requested ``jobs``."""

    def test_waiting_excludes_a_slot_already_claimed_by_a_generation(self, db_session, fake_redis, make_user):
        # `n` still holds the value read before the generation started, so it
        # counts a slot the procgen below has already claimed. Reporting both
        # would describe two generations for a request that asked for one.
        user = make_user()
        wp = _make_wp(user, n=1)
        worker = _make_worker(user)
        _make_procgen(wp, worker)

        status = _status(wp)

        assert wp.jobs == 1
        assert status["waiting"] == 0
        assert status["processing"] == 1
        assert status["waiting"] + status["processing"] + status["finished"] == wp.jobs

    def test_multi_generation_request_reports_the_unclaimed_remainder(self, db_session, fake_redis, make_user):
        user = make_user()
        wp = _make_wp(user, n=3)
        worker = _make_worker(user)
        _make_procgen(wp, worker)
        _make_procgen(wp, worker, generation="done")
        wp.n = 1
        db.session.commit()

        status = _status(wp)

        assert status["waiting"] == 1
        assert status["processing"] == 1
        assert status["finished"] == 1
        assert status["waiting"] + status["processing"] + status["finished"] == wp.jobs

    def test_faulted_generation_returns_its_slot_to_waiting(self, db_session, fake_redis, make_user):
        # A faulted generation hands its slot back to `n` for another worker and
        # is reported separately as `restarted`, so it does not consume one of
        # the request's jobs.
        user = make_user()
        wp = _make_wp(user, n=1)
        worker = _make_worker(user)
        _make_procgen(wp, worker, faulted=True)

        status = _status(wp)

        assert status["restarted"] == 1
        assert status["waiting"] == 1
        assert status["processing"] == 0

    def test_cancelled_request_reports_nothing_waiting(self, db_session, fake_redis, make_user):
        # Cancellation zeroes `n` while leaving the unclaimed jobs unstarted, so
        # waiting must follow `n` rather than the request's original size.
        user = make_user()
        wp = _make_wp(user, n=3)
        wp.n = 0
        db.session.commit()

        assert _status(wp)["waiting"] == 0
