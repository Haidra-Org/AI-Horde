# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Redis contracts for active/passive image-reference publication."""

from __future__ import annotations

import threading
import time

import pytest
import redis
from horde_model_reference import ImageBaselineRecord
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from testcontainers.redis import RedisContainer

from horde import model_reference_snapshot as snapshot_module
from horde.model_reference_snapshot import (
    PUBLISH_SEQUENCE_KEY,
    SNAPSHOT_KEY,
    ImageReferenceSnapshot,
    RedisImageReferenceSnapshots,
    build_snapshot,
)
from tests.unit.model_reference_seed import make_image_record

pytestmark = pytest.mark.integration


def _client(container: RedisContainer) -> redis.Redis:
    return redis.Redis(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(container.port)),
        decode_responses=True,
    )


def _snapshot(model_name: str) -> ImageReferenceSnapshot:
    baseline = ImageBaselineRecord(name=KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value)
    return build_snapshot(
        models={model_name: make_image_record(model_name, baseline.name)},
        baselines={baseline.name: baseline},
        publisher="redis-integration-test",
        primary_api_url="https://models.example/api",
        beta_categories={"image_generation"},
    )


def test_snapshot_survives_independent_master_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    with RedisContainer() as active_container, RedisContainer() as passive_container:
        active = _client(active_container)
        passive = _client(passive_container)
        monkeypatch.setattr(snapshot_module.hr, "horde_r", active)
        monkeypatch.setattr(snapshot_module.hr, "all_horde_redis", [passive, active])
        store = RedisImageReferenceSnapshots()
        first = _snapshot("active-era-model")

        assert store.publish(lambda: first) == first
        assert active.get(SNAPSHOT_KEY) == passive.get(SNAPSHOT_KEY)
        assert active.get(PUBLISH_SEQUENCE_KEY) == passive.get(PUBLISH_SEQUENCE_KEY) == "1"

        # Promote the independent passive to the application's authoritative endpoint.
        monkeypatch.setattr(snapshot_module.hr, "horde_r", passive)
        monkeypatch.setattr(snapshot_module.hr, "all_horde_redis", [active, passive])
        second = _snapshot("post-failover-model")

        assert store.load() == first
        assert store.publish(lambda: second) == second
        assert active.get(SNAPSHOT_KEY) == passive.get(SNAPSHOT_KEY)
        assert active.get(PUBLISH_SEQUENCE_KEY) == passive.get(PUBLISH_SEQUENCE_KEY) == "2"
        assert store.load() == second


def test_real_redis_expiry_fences_slow_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    with RedisContainer() as active_container:
        active = _client(active_container)
        monkeypatch.setattr(snapshot_module.hr, "horde_r", active)
        monkeypatch.setattr(snapshot_module.hr, "all_horde_redis", [active])
        store = RedisImageReferenceSnapshots()
        slow_started = threading.Event()
        release_slow = threading.Event()
        slow_result = []

        def slow_fetch():
            slow_started.set()
            assert release_slow.wait(timeout=5)
            return _snapshot("expired-slow-model")

        slow_thread = threading.Thread(target=lambda: slow_result.append(store.publish(slow_fetch, lock_seconds=1)))
        slow_thread.start()
        assert slow_started.wait(timeout=5)
        time.sleep(1.1)

        current = _snapshot("new-lease-model")
        assert store.publish(lambda: current, lock_seconds=5) == current
        release_slow.set()
        slow_thread.join(timeout=5)

        assert not slow_thread.is_alive()
        assert slow_result == [None]
        assert store.load() == current
