# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for ``horde.database.functions.wp_has_valid_workers``.

``wp_has_valid_workers`` decides whether a waiting prompt is servable. It draws
on two distinct sources of truth about a request:

- A worker-availability query (``horde/database/functions.py``) that selects
  currently non-stale workers (``last_check_in`` within 300s) matching the
  request's model and parameter constraints, then re-checks each with a
  Python-side ``worker.can_generate``.
- The request's live generation state, exposed by
  ``WaitingPrompt.count_processing_gens`` (``horde/classes/base/waiting_prompt.py``),
  which counts finished, restarted, and in-flight (processing) procgens.

The verdict is memoized in Redis under ``wp_validity_{wp.id}`` with a 60s TTL.

The behavioral contracts exercised here: a request that is actively being
generated is possible even once its serving worker goes stale, and a memoized
verdict must agree with the live worker and generation state rather than pin an
outdated answer. The remaining tests cover the availability query's
model/staleness/capacity filters and the processing-count bucketing.

Every test uses ``fake_redis`` because the verdict is Redis-memoized. Availability
loads worker capabilities from their relational rows in batches. The
``_stub_model_reference`` autouse fixture pins the image model reference to a
minimal in-memory dict so ``can_generate`` and procgen construction stay
hermetic (no network dependency on the remote model reference).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from horde_sdk.generation_parameters.image.sampler_work import SamplerExecutionContractVersion

from horde.classes.base.worker import WorkerModel
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.database import functions as f
from horde.enums import UserRoleTypes
from horde.flask import db

pytestmark = pytest.mark.unit

# The requested resolution is held constant so worker ``max_pixels`` is the only
# lever for the image-branch capacity control.
_WP_WIDTH = 512
_WP_HEIGHT = 512
_HOSTED_MODEL = "stable_diffusion"


@pytest.fixture(autouse=True)
def _stub_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the image model reference to a minimal in-memory dict.

    ``ImageWorker.can_generate`` and ``ImageProcessingGeneration`` consult
    ``model_reference`` (normally fetched over the network at import time). We
    substitute a tiny reference so the tests are hermetic and deterministic.
    """
    from horde import model_reference as model_reference_module

    minimal_reference = {_HOSTED_MODEL: {"baseline": "stable diffusion 1"}}
    monkeypatch.setattr(model_reference_module.model_reference, "reference", minimal_reference)


def _validity_cache_key(wp: ImageWaitingPrompt) -> str:
    return f"wp_validity_{wp.id}"


def _make_trusted_user(make_user: Any, make_user_role: Any) -> Any:
    """Create a user trusted enough to satisfy the ``can_generate`` trust gate.

    ``ImageWorker.can_generate`` rejects a worker whose owner is untrusted when
    the request is neither ``safe_ip`` nor owned by a trusted user. Owning the
    worker (and the WP) with a trusted user keeps every non-target gate open.
    """
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    return user


def _make_image_worker(
    user: Any,
    *,
    models: tuple[str, ...] = (_HOSTED_MODEL,),
    max_pixels: int = 1024 * 1024,
    threads: int = 1,
    stale: bool = False,
    limit_max_steps: bool = False,
) -> ImageWorker:
    """Create and persist an ``ImageWorker`` hosting ``models``.

    ``WorkerModel`` rows are inserted directly rather than via ``set_models`` so
    the worker does not depend on the model appearing in the (network-sourced)
    model reference. ``stale`` backdates ``last_check_in`` past the 300s cutoff.
    """
    last_check_in = datetime.utcnow() - timedelta(seconds=400) if stale else datetime.utcnow()
    worker = ImageWorker(
        user_id=user.id,
        name=f"worker_{uuid.uuid4().hex[:12]}",
        max_pixels=max_pixels,
        threads=threads,
        last_check_in=last_check_in,
        limit_max_steps=limit_max_steps,
    )
    db.session.add(worker)
    db.session.commit()
    for model_name in models:
        db.session.add(WorkerModel(worker_id=worker.id, model=model_name))
    db.session.commit()
    return worker


def _make_image_wp(
    user: Any,
    *,
    models: tuple[str, ...] = (_HOSTED_MODEL,),
    width: int = _WP_WIDTH,
    height: int = _WP_HEIGHT,
    n: int = 1,
    sampler_name: str = "k_euler_a",
    steps: int = 10,
) -> ImageWaitingPrompt:
    """Create and persist an ``ImageWaitingPrompt`` constrained to ``models``."""
    wp = ImageWaitingPrompt(
        [],
        list(models),
        prompt="a unit-test prompt",
        user_id=user.id,
        params={
            "n": n,
            "width": width,
            "height": height,
            "steps": steps,
            "sampler_name": sampler_name,
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
    """Create and persist an ``ImageProcessingGeneration`` in a chosen state.

    A pending (in-flight) procgen has ``generation is None`` and ``faulted``
    False. Setting ``generation`` marks it completed; ``faulted`` marks it
    restarted. The state is written directly rather than via ``set_generation``
    / ``abort`` to avoid the kudos, R2 upload and webhook machinery those carry,
    none of which affects the bucketing or validity logic under test.
    """
    procgen = ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model=_HOSTED_MODEL)
    if generation is not None:
        procgen.generation = generation
    if faulted:
        procgen.faulted = True
    db.session.commit()
    return procgen


class TestInFlightImpliesPossible:
    """A request with an in-flight generation is possible even if its worker is stale."""

    def test_in_flight_procgen_survives_worker_going_stale(self, db_session, fake_redis, make_user, make_user_role):
        # The worker popped the whole request (remaining n == 0) and is now mid
        # generation, but its check-in has aged past the 300s staleness cutoff.
        # An in-flight generation means the request is still being served, so it
        # remains possible regardless of the serving worker's staleness.
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        worker = _make_image_worker(user)
        _make_procgen(wp, worker)

        # The whole request has been popped; nothing remains queued.
        wp.n = 0
        db.session.commit()

        # The worker that popped the job has now gone quiet past the stale cutoff.
        worker.last_check_in = datetime.utcnow() - timedelta(seconds=400)
        db.session.commit()

        # Ensure no cached verdict can satisfy the assertion for the wrong reason.
        fake_redis.horde_r_delete(_validity_cache_key(wp))

        assert f.wp_has_valid_workers(wp) is True


class TestStaleCacheMustNotContradictLiveState:
    """A memoized validity verdict does not contradict the live worker and generation state."""

    def test_primed_false_verdict_does_not_survive_a_valid_worker(self, db_session, fake_redis, make_user, make_user_role):
        # Construct the scenario of a "not possible" verdict memoized while no
        # worker existed, then bring the live state up to date: a fresh, capable
        # worker appears and picks up the job (processing > 0). The reported
        # verdict must reflect that live state rather than the earlier memoized
        # answer.
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)

        fake_redis.horde_r_setex(_validity_cache_key(wp), timedelta(seconds=60), 0)

        worker = _make_image_worker(user)
        _make_procgen(wp, worker)
        wp.n = 0
        db.session.commit()

        assert f.wp_has_valid_workers(wp) is True


class TestNoWorkers:
    """A request with no workers and nothing processing is not possible."""

    def test_no_workers_returns_false(self, db_session, fake_redis, make_user, make_user_role):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)

        assert f.wp_has_valid_workers(wp) is False


class TestFreshCapableWorker:
    """A fresh worker hosting the requested model makes the request possible."""

    def test_fresh_worker_makes_request_possible(self, db_session, fake_redis, make_user, make_user_role):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user)

        assert f.wp_has_valid_workers(wp) is True

    def test_availability_reports_exact_worker_and_thread_capacity(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user, threads=2)
        _make_image_worker(user, threads=3)
        _make_image_worker(user, models=("some_other_model",), threads=20)

        availability = f.get_worker_availability_for_request(wp)

        assert isinstance(availability, f.RequestWorkerAvailability)
        assert availability.worker_count == 2
        assert availability.thread_count == 5
        assert availability.is_possible is True
        # The existing boolean API and cache contract remain unchanged.
        assert f.wp_has_valid_workers(wp) is True

    def test_availability_does_not_read_each_workers_model_cache(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user)

        def fail_model_cache_read(_worker: ImageWorker) -> list[str]:
            raise AssertionError("availability performed a per-worker model-cache read")

        monkeypatch.setattr(ImageWorker, "get_model_names", fail_model_cache_read)

        availability = f.get_worker_availability_for_request(wp)

        assert availability.worker_count == 1


class TestPersistedSamplerExecutionContract:
    """Forecasting reads the same recent execution capability that pop-time dispatch uses."""

    def test_reloaded_worker_keeps_adaptive_request_possible(
        self,
        db_session,
        fake_redis,
        make_user,
        make_user_role,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user, sampler_name="k_dpm_adaptive", steps=5)
        worker = _make_image_worker(user, limit_max_steps=True)

        # Exercise the debounced path: the capability assignment happens before the base check-in
        # returns False, and the pop handler's unconditional commit is represented explicitly here.
        worker.created = datetime.utcnow() - timedelta(minutes=5)
        worker.last_check_in = datetime.utcnow()
        worker.check_in(
            max_pixels=worker.max_pixels,
            sampler_execution_contract_version=SamplerExecutionContractVersion.V1.value,
        )
        worker_id = worker.id
        wp_id = wp.id
        db.session.commit()
        db.session.remove()

        reloaded_worker = db.session.get(ImageWorker, worker_id)
        reloaded_wp = db.session.get(ImageWaitingPrompt, wp_id)
        assert reloaded_worker is not None
        assert reloaded_wp is not None
        assert reloaded_worker.sampler_execution_contract_version == SamplerExecutionContractVersion.V1.value
        assert f.wp_has_valid_workers(reloaded_wp) is True

    def test_legacy_worker_remains_fail_closed_for_adaptive_request(
        self,
        db_session,
        fake_redis,
        make_user,
        make_user_role,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user, sampler_name="k_dpm_adaptive", steps=5)
        _make_image_worker(user, limit_max_steps=True)
        wp_id = wp.id

        db.session.remove()
        reloaded_wp = db.session.get(ImageWaitingPrompt, wp_id)
        assert reloaded_wp is not None
        assert f.wp_has_valid_workers(reloaded_wp) is False


class TestWrongModelWorker:
    """A worker hosting only a different model cannot serve the request."""

    def test_worker_with_only_other_model_returns_false(self, db_session, fake_redis, make_user, make_user_role):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user, models=("some_other_model",))

        assert f.wp_has_valid_workers(wp) is False


class TestStaleCapableWorker:
    """A capable but stale worker, with nothing processing, is not possible."""

    def test_stale_worker_returns_false(self, db_session, fake_redis, make_user, make_user_role):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user, stale=True)

        assert f.wp_has_valid_workers(wp) is False


class TestImageCapacityConstraint:
    """A worker whose max_pixels is below the requested resolution cannot serve it."""

    def test_worker_below_requested_pixels_returns_false(self, db_session, fake_redis, make_user, make_user_role):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        # 256*256 < 512*512, so the ``wp.width * wp.height <= max_pixels`` filter
        # excludes this otherwise-valid worker.
        _make_image_worker(user, max_pixels=256 * 256)

        assert f.wp_has_valid_workers(wp) is False


class TestCountProcessingGensBucketing:
    """count_processing_gens buckets completed, faulted, and in-flight procgens."""

    def test_one_of_each_state_is_bucketed_correctly(self, db_session, fake_redis, make_user, make_user_role):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        worker = _make_image_worker(user)

        _make_procgen(wp, worker, generation="R2")  # completed -> finished
        _make_procgen(wp, worker, faulted=True)  # faulted -> restarted
        _make_procgen(wp, worker)  # pending -> processing

        counts = wp.count_processing_gens()

        assert counts["finished"] == 1
        assert counts["restarted"] == 1
        assert counts["processing"] == 1
