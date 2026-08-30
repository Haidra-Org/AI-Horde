# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fleet-distribution guarantees for the Redis image-reference snapshot."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest
from horde_model_reference import (
    HORDE_SOURCE_ID,
    MODEL_REFERENCE_CATEGORY,
    PENDING_SOURCE_ID,
    ImageBaselineRecord,
    PrefetchStrategy,
)
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde import model_reference as model_reference_module
from horde import model_reference_snapshot as snapshot_module
from horde.model_reference import ModelReference
from horde.model_reference_snapshot import (
    PUBLISH_LOCK_KEY,
    PUBLISH_SEQUENCE_KEY,
    SNAPSHOT_KEY,
    ImageReferenceSnapshot,
    RedisImageReferenceSnapshots,
    build_snapshot,
)
from tests.unit.model_reference_seed import BOOTSTRAP_BASELINE_CATALOG, make_image_record

pytestmark = pytest.mark.unit


def _snapshot(model_name: str = "fleet-model") -> ImageReferenceSnapshot:
    baseline = ImageBaselineRecord(name=KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value)
    return build_snapshot(
        models={model_name: make_image_record(model_name, baseline.name)},
        baselines={baseline.name: baseline},
        publisher="publisher-a",
        primary_api_url="https://models.example/api",
        beta_categories={"image_generation"},
    )


def test_one_redis_document_hydrates_identical_process_views(fake_redis) -> None:
    store = RedisImageReferenceSnapshots()
    expected = _snapshot()
    fetches = 0

    def fetch() -> ImageReferenceSnapshot:
        nonlocal fetches
        fetches += 1
        return expected

    assert store.publish(fetch) == expected
    processes = [ModelReference() for _ in range(25)]

    assert all(process.refresh_from_redis() for process in processes)
    assert fetches == 1
    assert {process._snapshot_revision for process in processes} == {expected.revision}
    assert all(process.reference == expected.models for process in processes)


def test_publication_fans_out_to_independent_failover_redis_without_using_local_cache(monkeypatch) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    active = fakeredis.FakeStrictRedis()
    passive = fakeredis.FakeStrictRedis()
    local = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(snapshot_module.hr, "horde_r", active)
    monkeypatch.setattr(snapshot_module.hr, "all_horde_redis", [passive, active])
    monkeypatch.setattr(snapshot_module.hr, "horde_local_r", local)
    expected = _snapshot()

    assert RedisImageReferenceSnapshots().publish(lambda: expected) == expected

    assert active.get(SNAPSHOT_KEY) == passive.get(SNAPSHOT_KEY)
    assert active.get(PUBLISH_SEQUENCE_KEY) == passive.get(PUBLISH_SEQUENCE_KEY) == b"1"
    assert local.get(SNAPSHOT_KEY) is None


def test_delayed_publisher_cannot_regress_a_newer_passive_generation(monkeypatch) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    active = fakeredis.FakeStrictRedis()
    passive = fakeredis.FakeStrictRedis()
    newer = _snapshot("newer-passive-model")
    passive.set(SNAPSHOT_KEY, newer.model_dump_json())
    passive.set(PUBLISH_SEQUENCE_KEY, 10)
    monkeypatch.setattr(snapshot_module.hr, "horde_r", active)
    monkeypatch.setattr(snapshot_module.hr, "all_horde_redis", [passive, active])

    candidate = _snapshot("older-delayed-model")
    RedisImageReferenceSnapshots()._replicate_snapshot(candidate.model_dump_json(), sequence=1)

    assert ImageReferenceSnapshot.model_validate_json(passive.get(SNAPSHOT_KEY)) == newer
    assert passive.get(PUBLISH_SEQUENCE_KEY) == b"10"


def test_failback_advances_past_newer_passive_generation(monkeypatch) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    active = fakeredis.FakeStrictRedis()
    passive = fakeredis.FakeStrictRedis()
    active.set(PUBLISH_SEQUENCE_KEY, 3)
    passive.set(PUBLISH_SEQUENCE_KEY, 7)
    monkeypatch.setattr(snapshot_module.hr, "horde_r", active)
    monkeypatch.setattr(snapshot_module.hr, "all_horde_redis", [passive, active])
    candidate = _snapshot("post-failback-model")

    assert RedisImageReferenceSnapshots().publish(lambda: candidate) == candidate

    assert active.get(PUBLISH_SEQUENCE_KEY) == passive.get(PUBLISH_SEQUENCE_KEY) == b"8"
    assert ImageReferenceSnapshot.model_validate_json(passive.get(SNAPSHOT_KEY)) == candidate


def test_hmr_manager_disables_all_category_prefetch(monkeypatch) -> None:
    requested_strategy = None
    manager = object()

    class ManagerFactory:
        @staticmethod
        def has_instance() -> bool:
            return False

        def __new__(cls, *, prefetch_strategy: PrefetchStrategy):
            nonlocal requested_strategy
            requested_strategy = prefetch_strategy
            return manager

    monkeypatch.setattr(model_reference_module, "ModelReferenceManager", ManagerFactory)

    assert model_reference_module._get_reference_manager() is manager
    assert requested_strategy is PrefetchStrategy.NONE


def test_preexisting_hmr_manager_with_lazy_prefetch_is_rejected(monkeypatch) -> None:
    class LazyManager:
        prefetch_strategy = PrefetchStrategy.LAZY

    class ManagerFactory:
        @staticmethod
        def has_instance() -> bool:
            return True

        @staticmethod
        def get_instance() -> LazyManager:
            return LazyManager()

    monkeypatch.setattr(model_reference_module, "ModelReferenceManager", ManagerFactory)

    with pytest.raises(RuntimeError, match="expected PrefetchStrategy.NONE"):
        model_reference_module._get_reference_manager()


def test_hmr_manager_construction_with_none_performs_no_network_io() -> None:
    probe = """
from unittest.mock import patch

from horde_model_reference import PrefetchStrategy
from horde import model_reference

with (
    patch("socket.socket.connect", side_effect=AssertionError("socket connect during HMR construction")),
    patch("socket.create_connection", side_effect=AssertionError("socket create_connection during HMR construction")),
):
    manager = model_reference._get_reference_manager()

assert manager.prefetch_strategy is PrefetchStrategy.NONE
assert manager._cached_records == {}
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr


def test_existing_publication_lease_prevents_another_http_fetch(fake_redis) -> None:
    store = RedisImageReferenceSnapshots()
    fake_redis.horde_r.set(PUBLISH_LOCK_KEY, "other-publisher", ex=60)
    called = False

    def fetch() -> ImageReferenceSnapshot:
        nonlocal called
        called = True
        return _snapshot()

    assert store.publish(fetch) is None
    assert called is False


def test_publisher_that_loses_its_lease_cannot_replace_the_snapshot(fake_redis) -> None:
    store = RedisImageReferenceSnapshots()
    previous = _snapshot("previous-model")
    fake_redis.horde_r.set(SNAPSHOT_KEY, previous.model_dump_json())

    def fetch_after_lease_loss() -> ImageReferenceSnapshot:
        fake_redis.horde_r.set(PUBLISH_LOCK_KEY, "new-publisher", ex=60)
        return _snapshot("stale-model")

    assert store.publish(fetch_after_lease_loss) is None
    assert store.load() == previous


def test_failed_fetch_leaves_last_known_good_snapshot_untouched(fake_redis) -> None:
    store = RedisImageReferenceSnapshots()
    previous = _snapshot("last-known-good")
    fake_redis.horde_r.set(SNAPSHOT_KEY, previous.model_dump_json())

    def fail() -> ImageReferenceSnapshot:
        raise RuntimeError("PRIMARY unavailable")

    with pytest.raises(RuntimeError, match="PRIMARY unavailable"):
        store.publish(fail)

    assert store.load() == previous
    assert fake_redis.horde_r.get(PUBLISH_LOCK_KEY) is None


def test_authoritative_transaction_failure_leaves_last_known_good_untouched(fake_redis, monkeypatch) -> None:
    store = RedisImageReferenceSnapshots()
    previous = _snapshot("transaction-last-known-good")
    fake_redis.horde_r.set(SNAPSHOT_KEY, previous.model_dump_json())
    monkeypatch.setattr(
        store,
        "_publish_authoritative",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("Redis EXEC failed")),
    )

    with pytest.raises(ConnectionError, match="Redis EXEC failed"):
        store.publish(lambda: _snapshot("must-not-publish"))

    assert store.load() == previous


def test_lease_cleanup_failure_does_not_mask_success(fake_redis, monkeypatch) -> None:
    store = RedisImageReferenceSnapshots()
    expected = _snapshot("published-before-cleanup-failure")
    monkeypatch.setattr(
        store,
        "_release_lease",
        lambda _token: (_ for _ in ()).throw(ConnectionError("cleanup failed")),
    )

    assert store.publish(lambda: expected) == expected
    assert store.load() == expected


def test_invalid_candidate_hash_cannot_replace_last_known_good(fake_redis) -> None:
    store = RedisImageReferenceSnapshots()
    previous = _snapshot("valid-existing-model")
    fake_redis.horde_r.set(SNAPSHOT_KEY, previous.model_dump_json())
    invalid = _snapshot("invalid-candidate").model_copy(update={"revision": "sha256:not-the-content"})

    with pytest.raises(ValueError, match="hash mismatch"):
        store.publish(lambda: invalid)

    assert store.load() == previous


@pytest.mark.parametrize("empty_part", ["models", "baselines"])
def test_empty_candidate_cannot_replace_last_known_good(fake_redis, empty_part: str) -> None:
    store = RedisImageReferenceSnapshots()
    previous = _snapshot("valid-existing-model")
    fake_redis.horde_r.set(SNAPSHOT_KEY, previous.model_dump_json())
    candidate = previous.model_copy(update={empty_part: {}})
    candidate = candidate.model_copy(
        update={"revision": snapshot_module.snapshot_revision(candidate.models, candidate.baselines)},
    )

    with pytest.raises(ValueError, match="empty image"):
        store.publish(lambda: candidate)

    assert store.load() == previous


def test_consumer_rejects_corruption_and_retains_its_local_revision(fake_redis) -> None:
    process = ModelReference()
    previous = _snapshot("local-last-known-good")
    process._apply_snapshot(previous)
    corrupted = previous.model_dump(mode="json")
    corrupted["models"]["unexpected"] = corrupted["models"].pop("local-last-known-good")
    fake_redis.horde_r.set(SNAPSHOT_KEY, json.dumps(corrupted))

    assert process.refresh_from_redis() is False
    assert process._snapshot_revision == previous.revision
    assert set(process.reference or {}) == {"local-last-known-good"}


@pytest.mark.parametrize("missing", [None, "not-json"])
def test_missing_or_malformed_redis_value_does_not_clear_local_state(fake_redis, missing: str | None) -> None:
    process = ModelReference()
    previous = _snapshot("still-serving")
    process._apply_snapshot(previous)
    if missing is not None:
        fake_redis.horde_r.set(SNAPSHOT_KEY, missing)

    assert process.refresh_from_redis() is False
    assert process.reference == previous.models


class _PrimaryBackend:
    def __init__(self, *, fallback: bool = False) -> None:
        self.fallback = fallback
        self.github_fallbacks = 0

    @staticmethod
    def fetch_image_baseline_export() -> dict[str, object]:
        return BOOTSTRAP_BASELINE_CATALOG.model_dump(mode="json")

    def fetch_category(self, category: MODEL_REFERENCE_CATEGORY, *, force_refresh: bool) -> dict[str, object]:
        assert category == MODEL_REFERENCE_CATEGORY.image_generation
        assert force_refresh is True
        if self.fallback:
            self.github_fallbacks += 1
        record = make_image_record("canonical-model", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1)
        return {record.name: record.model_dump(mode="json")}

    def get_statistics(self) -> dict[str, int]:
        return {"github_fallbacks": self.github_fallbacks}


class _Manager:
    def __init__(self, backend: _PrimaryBackend, pending: object | None = None) -> None:
        self.backend = backend
        self.pending = pending

    def get_provider(self, source_id: str) -> object | None:
        assert source_id == PENDING_SOURCE_ID
        return self.pending


def test_publisher_refuses_to_replace_primary_data_with_hmr_github_fallback(monkeypatch) -> None:
    manager = _Manager(_PrimaryBackend(fallback=True))
    monkeypatch.setattr(model_reference_module, "_get_reference_manager", lambda: manager)
    monkeypatch.setattr(model_reference_module, "_beta_model_categories", set)
    monkeypatch.setattr(model_reference_module, "_image_reference_source", lambda _manager: HORDE_SOURCE_ID)

    with pytest.raises(RuntimeError, match="GitHub fallback"):
        ModelReference()._fetch_fleet_snapshot()


def test_new_model_baseline_is_refetched_after_interleaved_primary_publication(monkeypatch) -> None:
    future_baseline = ImageBaselineRecord(name="future-baseline")

    class RacingBackend(_PrimaryBackend):
        baseline_fetches = 0

        def fetch_image_baseline_export(self) -> dict[str, object]:
            self.baseline_fetches += 1
            if self.baseline_fetches == 1:
                return BOOTSTRAP_BASELINE_CATALOG.model_dump(mode="json")
            catalog = BOOTSTRAP_BASELINE_CATALOG.model_copy(
                update={"baselines": {**BOOTSTRAP_BASELINE_CATALOG.baselines, future_baseline.name: future_baseline}},
            )
            return catalog.model_dump(mode="json")

        def fetch_category(self, category: MODEL_REFERENCE_CATEGORY, *, force_refresh: bool) -> dict[str, object]:
            record = make_image_record("future-model", future_baseline.name)
            return {record.name: record.model_dump(mode="json")}

    backend = RacingBackend()
    manager = _Manager(backend)
    monkeypatch.setattr(model_reference_module, "_get_reference_manager", lambda: manager)
    monkeypatch.setattr(model_reference_module, "_beta_model_categories", set)
    monkeypatch.setattr(model_reference_module, "_image_reference_source", lambda _manager: HORDE_SOURCE_ID)

    snapshot = ModelReference()._fetch_fleet_snapshot()

    assert backend.baseline_fetches == 2
    assert snapshot.models["future-model"].baseline == future_baseline.name
    assert snapshot.baselines[future_baseline.name] == future_baseline


def test_successful_fleet_fetch_merges_pending_over_canonical(monkeypatch) -> None:
    canonical = make_image_record("shared-model", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1)
    pending = make_image_record("shared-model", KNOWN_IMAGE_GENERATION_BASELINE.flux_1)

    class CanonicalBackend(_PrimaryBackend):
        def fetch_category(self, category: MODEL_REFERENCE_CATEGORY, *, force_refresh: bool) -> dict[str, object]:
            return {canonical.name: canonical.model_dump(mode="json")}

    class PendingProvider:
        @staticmethod
        def fetch_category(category: MODEL_REFERENCE_CATEGORY, *, force_refresh: bool) -> dict[str, object]:
            return {pending.name: pending.model_dump(mode="json")}

    manager = _Manager(CanonicalBackend(), PendingProvider())
    monkeypatch.setattr(model_reference_module, "_get_reference_manager", lambda: manager)
    monkeypatch.setattr(
        model_reference_module,
        "_beta_model_categories",
        lambda: {MODEL_REFERENCE_CATEGORY.image_generation},
    )
    monkeypatch.setattr(
        model_reference_module,
        "_image_reference_source",
        lambda _manager: [PENDING_SOURCE_ID, HORDE_SOURCE_ID],
    )

    snapshot = ModelReference()._fetch_fleet_snapshot()

    assert snapshot.models["shared-model"].baseline == KNOWN_IMAGE_GENERATION_BASELINE.flux_1


def test_pending_read_failure_aborts_the_complete_publication(monkeypatch) -> None:
    class FailedPendingProvider:
        @staticmethod
        def fetch_category(category: MODEL_REFERENCE_CATEGORY, *, force_refresh: bool) -> None:
            assert category == MODEL_REFERENCE_CATEGORY.image_generation
            assert force_refresh is True
            return None

    manager = _Manager(_PrimaryBackend(), FailedPendingProvider())
    monkeypatch.setattr(model_reference_module, "_get_reference_manager", lambda: manager)
    monkeypatch.setattr(
        model_reference_module,
        "_beta_model_categories",
        lambda: {MODEL_REFERENCE_CATEGORY.image_generation},
    )
    monkeypatch.setattr(
        model_reference_module,
        "_image_reference_source",
        lambda _manager: [PENDING_SOURCE_ID, HORDE_SOURCE_ID],
    )

    with pytest.raises(RuntimeError, match="pending-model read failed"):
        ModelReference()._fetch_fleet_snapshot()


def test_consumer_state_views_change_coherently_under_concurrent_refresh(fake_redis, monkeypatch) -> None:
    process = ModelReference()
    first = _snapshot("first-model")
    second = _snapshot("second-model")
    stop = threading.Event()
    failures: list[tuple[set[str], set[str]]] = []
    monkeypatch.setattr(model_reference_module.logger, "info", lambda *args, **kwargs: None)

    def writer() -> None:
        for index in range(2_000):
            process._apply_snapshot(first if index % 2 == 0 else second)
        stop.set()

    def reader() -> None:
        while not stop.is_set():
            state = process._image_state
            if set(state.models) != set(state.model_names):
                failures.append((set(state.models), set(state.model_names)))
                stop.set()

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join(timeout=5)
    stop.set()
    reader_thread.join(timeout=5)

    assert not writer_thread.is_alive()
    assert failures == []


def test_cold_start_loser_recontends_and_takes_over_after_abandoned_lease(monkeypatch) -> None:
    process = ModelReference()
    expected = _snapshot("recovered-bootstrap-model")
    attempts = 0

    def publish(*, allow_degraded: bool = False) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            process._apply_snapshot(expected)
            return True
        return False

    monkeypatch.setattr(snapshot_module.redis_image_reference_snapshots, "is_available", lambda: True)
    monkeypatch.setattr(process, "refresh_from_redis", lambda **kwargs: False)
    monkeypatch.setattr(process, "publish_fleet_snapshot", publish)
    monkeypatch.setattr(process, "refresh_text_reference", lambda: None)
    monkeypatch.setattr(model_reference_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS", "1")
    monkeypatch.setenv("HORDE_MODEL_REFERENCE_BOOTSTRAP_TIMEOUT", "1")

    process.initialize()

    assert attempts == 2
    assert process._snapshot_revision == expected.revision


def test_cold_start_fails_immediately_without_central_redis(monkeypatch) -> None:
    process = ModelReference()
    monkeypatch.setattr(snapshot_module.redis_image_reference_snapshots, "is_available", lambda: False)

    with pytest.raises(RuntimeError, match="Central Redis is unavailable"):
        process.initialize()


def test_warm_fleet_startup_reads_redis_without_hmr_fetch(fake_redis, monkeypatch) -> None:
    process = ModelReference()
    expected = _snapshot("warm-start-model")
    fake_redis.horde_r.set(SNAPSHOT_KEY, expected.model_dump_json())
    monkeypatch.setattr(
        process,
        "publish_fleet_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("warm startup attempted an HMR fetch")),
    )
    monkeypatch.setattr(process, "refresh_text_reference", lambda: None)

    process.initialize()

    assert process.reference == expected.models
    assert process._snapshot_revision == expected.revision


def test_unknown_snapshot_fields_are_ignored_with_a_warning(monkeypatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(snapshot_module.logger, "warning", lambda message, *args, **kwargs: warnings.append(message))
    payload = _snapshot("forward-compatible-model").model_dump(mode="json")
    payload["future_field"] = {"added": "by a newer publisher"}

    loaded = ImageReferenceSnapshot.model_validate(payload)

    assert set(loaded.models) == {"forward-compatible-model"}
    assert any("future_field" in message for message in warnings)


def test_degraded_bootstrap_substitutes_fallback_sources(monkeypatch) -> None:
    class UnreachablePrimary(_PrimaryBackend):
        @staticmethod
        def fetch_image_baseline_export() -> None:
            return None

    class UnreachablePending:
        @staticmethod
        def fetch_category(category: MODEL_REFERENCE_CATEGORY, *, force_refresh: bool) -> None:
            return None

    manager = _Manager(UnreachablePrimary(fallback=True), UnreachablePending())
    monkeypatch.setattr(model_reference_module, "_get_reference_manager", lambda: manager)
    monkeypatch.setattr(
        model_reference_module,
        "_beta_model_categories",
        lambda: {MODEL_REFERENCE_CATEGORY.image_generation},
    )
    monkeypatch.setattr(
        model_reference_module,
        "_image_reference_source",
        lambda _manager: [PENDING_SOURCE_ID, HORDE_SOURCE_ID],
    )
    loader = ModelReference()

    with pytest.raises(RuntimeError):
        loader._fetch_fleet_snapshot()
    snapshot = loader._fetch_fleet_snapshot(allow_degraded=True)

    assert snapshot.degraded is True
    assert set(snapshot.models) == {"canonical-model"}
    assert snapshot.baselines == model_reference_module._packaged_baseline_catalog().baselines


class _FakeClock:
    """A monotonic clock that only advances when the bootstrap loop sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    @staticmethod
    def time() -> float:
        return time.time()


def test_cold_start_tries_degraded_bootstrap_only_after_the_strict_window(monkeypatch) -> None:
    process = ModelReference()
    expected = _snapshot("degraded-bootstrap-model").model_copy(update={"degraded": True})
    attempts: list[bool] = []

    def publish(*, allow_degraded: bool = False) -> bool:
        attempts.append(allow_degraded)
        if allow_degraded:
            process._apply_snapshot(expected)
            return True
        return False

    monkeypatch.setattr(snapshot_module.redis_image_reference_snapshots, "is_available", lambda: True)
    monkeypatch.setattr(process, "refresh_from_redis", lambda **kwargs: False)
    monkeypatch.setattr(process, "publish_fleet_snapshot", publish)
    monkeypatch.setattr(process, "refresh_text_reference", lambda: None)
    monkeypatch.setattr(model_reference_module, "time", _FakeClock())
    monkeypatch.setenv("HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS", "2")
    monkeypatch.setenv("HORDE_MODEL_REFERENCE_BOOTSTRAP_TIMEOUT", "10")

    process.initialize()

    assert attempts == [False] * 10 + [True]
    assert process._snapshot_revision == expected.revision
    assert process._image_state.degraded is True


def test_degraded_snapshot_is_reported_as_an_error_and_a_gauge(fake_redis, monkeypatch) -> None:
    errors: list[str] = []
    degraded_values: list[int] = []
    monkeypatch.setattr(model_reference_module.logger, "error", lambda message, *args, **kwargs: errors.append(message))
    monkeypatch.setattr(model_reference_module.metrics.model_reference_snapshot_degraded, "set", degraded_values.append)
    monkeypatch.setattr(model_reference_module.metrics.model_reference_snapshot_age_seconds, "set", lambda _value: None)
    process = ModelReference()
    fake_redis.horde_r.set(SNAPSHOT_KEY, _snapshot("degraded-model").model_copy(update={"degraded": True}).model_dump_json())

    assert process.refresh_from_redis() is True

    assert any("degraded" in message for message in errors)
    assert degraded_values == [1]


def test_stale_snapshot_warns_once_per_interval_and_reports_its_age(fake_redis, monkeypatch) -> None:
    warnings: list[str] = []
    ages: list[int] = []
    monkeypatch.setattr(model_reference_module.logger, "warning", lambda message, *args, **kwargs: warnings.append(message))
    monkeypatch.setattr(model_reference_module.logger, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(model_reference_module.metrics.model_reference_snapshot_age_seconds, "set", ages.append)
    monkeypatch.setattr(model_reference_module.metrics.model_reference_snapshot_degraded, "set", lambda _value: None)
    process = ModelReference()
    stale_age = model_reference_module.STALE_SNAPSHOT_SECONDS + 60
    stale = _snapshot("stale-model").model_copy(update={"published_at": int(time.time()) - stale_age})
    fake_redis.horde_r.set(SNAPSHOT_KEY, stale.model_dump_json())

    process.refresh_from_redis()
    process.refresh_from_redis()

    assert len(ages) == 2
    assert all(age >= stale_age for age in ages)
    assert len([message for message in warnings if "seconds old" in message]) == 1


def test_publisher_skips_a_fresh_healthy_snapshot(monkeypatch) -> None:
    process = ModelReference()
    process._apply_snapshot(_snapshot("fresh-model"))
    monkeypatch.setattr(
        process,
        "publish_fleet_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("a fresh snapshot must not be re-fetched")),
    )

    assert process.publish_fleet_snapshot_if_due() is True


@pytest.mark.parametrize("reason", ["degraded", "expired", "absent"])
def test_publisher_refreshes_a_degraded_expired_or_absent_snapshot(monkeypatch, reason: str) -> None:
    process = ModelReference()
    if reason == "degraded":
        process._apply_snapshot(_snapshot("degraded-model").model_copy(update={"degraded": True}))
    elif reason == "expired":
        stale_at = int(time.time()) - model_reference_module.FLEET_PUBLISH_INTERVAL_SECONDS - 1
        process._apply_snapshot(_snapshot("expired-model").model_copy(update={"published_at": stale_at}))
    published: list[bool] = []
    monkeypatch.setattr(process, "publish_fleet_snapshot", lambda **kwargs: published.append(True) or True)

    assert process.publish_fleet_snapshot_if_due() is True
    assert published == [True]
