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
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_sdk.generation_parameters.image.sampler_work import SamplerExecutionContractVersion

from horde.classes.base.worker import WorkerModel
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.database import functions as f
from horde.enums import UserRoleTypes
from horde.flask import db
from tests.unit.model_reference_seed import seed_image_reference

pytestmark = pytest.mark.unit

# The requested resolution is held constant so worker ``max_pixels`` is the only
# lever for the image-branch capacity control.
_WP_WIDTH = 512
_WP_HEIGHT = 512
_HOSTED_MODEL = "stable_diffusion"


@pytest.fixture(autouse=True)
def _stub_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the image model reference to a single model.

    ``ImageWorker.can_generate`` and ``ImageProcessingGeneration`` consult
    ``model_reference`` (normally fetched over the network at import time). We
    substitute a tiny reference so the tests are hermetic and deterministic.
    """
    seed_image_reference(monkeypatch, {_HOSTED_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})


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

    def test_fresh_worker_makes_request_possible(
        self,
        db_session,
        fake_redis,
        make_user,
        make_user_role,
        monkeypatch: pytest.MonkeyPatch,
    ):
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user)

        def fail_inflight_query(_waiting_prompt: ImageWaitingPrompt) -> bool:
            raise AssertionError("fresh request feasibility queried processing generations")

        monkeypatch.setattr(f, "_waiting_prompt_has_inflight_generation", fail_inflight_query)

        assert f.wp_has_valid_workers(wp) is True

    def test_availability_reports_exact_worker_and_thread_capacity(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        wp = _make_image_wp(user)
        _make_image_worker(user, threads=2)
        _make_image_worker(user, threads=3)
        _make_image_worker(user, models=("some_other_model",), threads=20)
        pressure_samples: list[dict[str, Any]] = []
        monkeypatch.setattr(f, "record_request_assignment_pressure", lambda **sample: pressure_samples.append(sample))

        availability = f.get_worker_availability_for_request(wp)

        assert isinstance(availability, f.RequestWorkerAvailability)
        assert availability.worker_count == 2
        assert availability.thread_count == 5
        assert availability.is_possible is True
        assert pressure_samples == [
            {
                "gentype": "image",
                "evidence": "insufficient_window",
                "might_stall": False,
                "dispatch_opportunities": 0,
                "lost_opportunities": 0,
                "returned_capacity": 0,
                "active_preceding_dispatches": 0,
                "arriving_preceding_work": 0,
                "returned_work": 0,
            },
        ]
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

    def test_eligibility_query_count_does_not_scale_with_worker_count(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
    ) -> None:
        from sqlalchemy import event

        user = _make_trusted_user(make_user, make_user_role)
        target = _make_image_wp(user)
        _make_image_worker(user)

        def count_eligibility_queries() -> int:
            statement_count = 0

            def count_statement(*_args: Any, **_kwargs: Any) -> None:
                nonlocal statement_count
                statement_count += 1

            db.session.expire_all()
            event.listen(db.engine, "before_cursor_execute", count_statement)
            try:
                list(f._iter_eligible_workers_for_request(target))
            finally:
                event.remove(db.engine, "before_cursor_execute", count_statement)
            return statement_count

        one_worker_query_count = count_eligibility_queries()
        for _ in range(4):
            _make_image_worker(user)
        five_worker_query_count = count_eligibility_queries()

        assert one_worker_query_count <= 8
        assert five_worker_query_count == one_worker_query_count

    def test_availability_deduplicates_active_batched_pop(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        target = _make_image_wp(user)
        target.active = True
        competing_request = _make_image_wp(user, n=2)
        worker = _make_image_worker(user, threads=2)
        dispatch_batch_id = uuid.uuid4()
        for _ in range(2):
            ImageProcessingGeneration(
                wp_id=competing_request.id,
                worker_id=worker.id,
                model=_HOSTED_MODEL,
                dispatch_batch_id=dispatch_batch_id,
            )

        active_dispatches = f._get_active_worker_dispatches(target, [str(worker.id)])

        assert len(active_dispatches) == 1
        assert active_dispatches[0].dispatch_id == str(dispatch_batch_id)

    def test_availability_reports_only_new_compatible_demand_ahead(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        target = _make_image_wp(user)
        target.created = datetime.utcnow() - timedelta(minutes=4)
        target.extra_priority = 0
        ahead_early = _make_image_wp(user, n=2)
        ahead_early.active = True
        ahead_early.created = datetime.utcnow() - timedelta(minutes=3)
        ahead_early.extra_priority = 10
        ahead_late = _make_image_wp(user)
        ahead_late.active = True
        ahead_late.created = datetime.utcnow() - timedelta(minutes=1)
        ahead_late.extra_priority = 10
        lower_priority = _make_image_wp(user)
        lower_priority.active = True
        lower_priority.created = datetime.utcnow() - timedelta(minutes=1)
        lower_priority.extra_priority = -1
        incompatible = _make_image_wp(user, models=("other_model",))
        incompatible.active = True
        incompatible.created = datetime.utcnow() - timedelta(minutes=1)
        incompatible.extra_priority = 10
        cancelled = _make_image_wp(user)
        cancelled.active = True
        cancelled.created = datetime.utcnow() - timedelta(minutes=1)
        cancelled.extra_priority = 10
        cancelled.n = 0
        _make_image_worker(user)
        db.session.commit()

        eligible_workers = list(f._iter_eligible_workers_for_request(target))
        preceding_arrivals = f._get_preceding_arrivals(target, eligible_workers, datetime.utcnow())

        arrival_ids = {arrival.request_id for arrival in preceding_arrivals}
        assert arrival_ids == {str(ahead_early.id), str(ahead_late.id)}
        assert str(lower_priority.id) not in arrival_ids
        assert str(incompatible.id) not in arrival_ids
        assert str(cancelled.id) not in arrival_ids

    def test_arrival_scan_does_not_truncate_after_fifty_candidates(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        target = _make_image_wp(user)
        target.created = datetime.utcnow() - timedelta(minutes=4)
        target.extra_priority = 0
        worker = _make_image_worker(user)
        candidate_created_at = datetime.utcnow() - timedelta(minutes=1)
        candidates: list[ImageWaitingPrompt] = []
        for _ in range(51):
            candidate = _make_image_wp(user)
            candidate.active = True
            candidate.created = candidate_created_at
            candidate.extra_priority = 10
            candidates.append(candidate)
        db.session.commit()

        preceding_arrivals = f._get_preceding_arrivals(target, [worker], datetime.utcnow())

        assert {arrival.request_id for arrival in preceding_arrivals} == {str(candidate.id) for candidate in candidates}

    def test_arrival_scan_query_count_does_not_scale_with_candidate_count(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
    ) -> None:
        from sqlalchemy import event

        user = _make_trusted_user(make_user, make_user_role)
        target = _make_image_wp(user)
        target.created = datetime.utcnow() - timedelta(minutes=4)
        target.extra_priority = 0
        worker = _make_image_worker(user)
        candidates: list[ImageWaitingPrompt] = []
        for _ in range(51):
            candidate = _make_image_wp(user)
            candidate.active = False
            candidate.created = datetime.utcnow() - timedelta(minutes=1)
            candidate.extra_priority = 10
            candidates.append(candidate)
        candidates[0].active = True
        db.session.commit()

        def count_arrival_queries() -> int:
            statement_count = 0

            def count_statement(*_args: Any, **_kwargs: Any) -> None:
                nonlocal statement_count
                statement_count += 1

            db.session.expire_all()
            event.listen(db.engine, "before_cursor_execute", count_statement)
            try:
                f._get_preceding_arrivals(target, [worker], datetime.utcnow())
            finally:
                event.remove(db.engine, "before_cursor_execute", count_statement)
            return statement_count

        one_candidate_query_count = count_arrival_queries()
        for candidate in candidates:
            candidate.active = True
        db.session.commit()
        fifty_one_candidate_query_count = count_arrival_queries()

        assert one_candidate_query_count <= 10
        assert fifty_one_candidate_query_count == one_candidate_query_count

    def test_cached_availability_performs_no_database_or_pressure_recalculation(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        make_user_role: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user = _make_trusted_user(make_user, make_user_role)
        target = _make_image_wp(user)
        _make_image_worker(user)
        availability = f.get_worker_availability_for_request(target)

        def fail_recalculation(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("availability cache hit recalculated database or pressure state")

        monkeypatch.setattr(f, "_waiting_prompt_has_inflight_generation", fail_recalculation)
        monkeypatch.setattr(f, "_iter_eligible_workers_for_request", fail_recalculation)
        monkeypatch.setattr(f, "_get_active_worker_dispatches", fail_recalculation)
        monkeypatch.setattr(f, "_get_preceding_arrivals", fail_recalculation)
        monkeypatch.setattr(f, "get_request_assignment_pressure", fail_recalculation)
        monkeypatch.setattr(f, "record_request_assignment_pressure", fail_recalculation)

        assert f.get_worker_availability_for_request(target) == availability


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
