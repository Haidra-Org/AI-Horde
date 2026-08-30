# SPDX-FileCopyrightText: 2026 Haidra
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Redis distribution for the fleet-wide image model-reference snapshot.

The HMR PRIMARY remains authoritative.  Redis is only the AI-Horde control-plane
distribution point: one publisher replaces one complete, validated document and
every API process copies that same revision into local memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from typing import Any, Final, Literal
from uuid import uuid4

from horde_model_reference import ImageBaselineRecord
from horde_model_reference.model_reference_records import ImageGenerationModelRecord
from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.exceptions import WatchError

from horde.horde_redis import horde_redis as hr
from horde.logger import logger  # type: ignore[attr-defined]

SNAPSHOT_KEY: Final[str] = "image_model_reference:snapshot:v1"
PUBLISH_LOCK_KEY: Final[str] = "image_model_reference:publish_lock:v1"
PUBLISH_SEQUENCE_KEY: Final[str] = "image_model_reference:publish_sequence:v1"
SNAPSHOT_SCHEMA_VERSION: Final[Literal[1]] = 1
DEFAULT_PUBLISH_LOCK_SECONDS: Final[int] = 180
MAX_TRANSACTION_RETRIES: Final[int] = 10


class ImageReferenceSnapshot(BaseModel):
    """One immutable image-reference revision distributed to every API process."""

    # Unknown top-level fields are tolerated so a consumer built before a publisher gained a field
    # keeps loading during a rolling deploy instead of falling back to a bootstrap publication.
    model_config = ConfigDict(extra="ignore")

    schema_version: Literal[1] = SNAPSHOT_SCHEMA_VERSION
    revision: str
    published_at: int
    publisher: str
    primary_api_url: str | None
    beta_categories: list[str] = Field(default_factory=list)
    degraded: bool = False
    """Whether any source was substituted with a fallback because the PRIMARY was unreachable."""
    models: dict[str, ImageGenerationModelRecord]
    baselines: dict[str, ImageBaselineRecord]

    @model_validator(mode="before")
    @classmethod
    def _warn_on_unknown_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            unknown = set(data) - set(cls.model_fields)
            if unknown:
                logger.warning(f"Ignoring unknown image-reference snapshot fields: {sorted(unknown)}")
        return data

    def verify_revision(self) -> None:
        """Reject a Redis value whose content does not match its revision hash."""
        expected = snapshot_revision(self.models, self.baselines)
        if self.revision != expected:
            raise ValueError(f"Image-reference snapshot hash mismatch: expected {expected}, got {self.revision}")


def snapshot_revision(
    models: dict[str, ImageGenerationModelRecord],
    baselines: dict[str, ImageBaselineRecord],
) -> str:
    """Return a deterministic content hash for the request-visible snapshot."""
    payload = {
        "models": {name: model_record.model_dump(mode="json") for name, model_record in sorted(models.items())},
        "baselines": {name: baseline_record.model_dump(mode="json") for name, baseline_record in sorted(baselines.items())},
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def build_snapshot(
    *,
    models: dict[str, ImageGenerationModelRecord],
    baselines: dict[str, ImageBaselineRecord],
    publisher: str,
    primary_api_url: str | None,
    beta_categories: set[str],
    degraded: bool = False,
) -> ImageReferenceSnapshot:
    """Build and validate the complete document before any Redis mutation."""
    for name, record in models.items():
        if name != record.name:
            raise ValueError(f"Image model key {name!r} does not match record name {record.name!r}")
    for name, baseline_record in baselines.items():
        if name != baseline_record.name:
            raise ValueError(f"Image baseline key {name!r} does not match record name {baseline_record.name!r}")
    return ImageReferenceSnapshot(
        revision=snapshot_revision(models, baselines),
        published_at=int(time.time()),
        publisher=publisher,
        primary_api_url=primary_api_url,
        beta_categories=sorted(beta_categories),
        degraded=degraded,
        models=models,
        baselines=baselines,
    )


def _as_text(value: str | bytes | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _sequence_value(value: str | bytes | None) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        logger.warning("Repairing an invalid image-reference publication sequence as generation zero.")
        return 0


class RedisImageReferenceSnapshots:
    """Read and atomically publish the authoritative AI-Horde fleet snapshot."""

    @staticmethod
    def is_available() -> bool:
        return hr.horde_r is not None

    def load(self) -> ImageReferenceSnapshot | None:
        """Read directly from central Redis, deliberately bypassing the per-host cache."""
        redis_backend = hr.horde_r
        if redis_backend is None:
            return None
        raw = redis_backend.get(SNAPSHOT_KEY)
        if raw is None:
            return None
        snapshot = ImageReferenceSnapshot.model_validate_json(raw)
        snapshot.verify_revision()
        return snapshot

    def publish(
        self,
        fetch: Callable[[], ImageReferenceSnapshot],
        *,
        lock_seconds: int | None = None,
    ) -> ImageReferenceSnapshot | None:
        """Fetch under a lease and replace Redis only while this publisher still owns it.

        The compare-and-set transaction is the fencing boundary: a publisher whose
        lease expired during a slow HTTP fetch cannot overwrite a newer publisher.
        """
        redis_backend = hr.horde_r
        if redis_backend is None:
            logger.warning("Cannot publish image-reference snapshot: central Redis is unavailable.")
            return None

        token = str(uuid4())
        lease_seconds = (
            lock_seconds
            if lock_seconds is not None
            else int(os.getenv("HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS", str(DEFAULT_PUBLISH_LOCK_SECONDS)))
        )
        if lease_seconds <= 0:
            raise ValueError("The image-reference publication lease must be greater than zero seconds.")
        if not redis_backend.set(PUBLISH_LOCK_KEY, token, nx=True, ex=lease_seconds):
            return None

        try:
            candidate = fetch()
            candidate.verify_revision()
            if not candidate.models:
                raise ValueError("Refusing to publish an empty image model reference.")
            if not candidate.baselines:
                raise ValueError("Refusing to publish an empty image baseline catalog.")
            encoded = candidate.model_dump_json()
            sequence_floor = self._highest_known_sequence(redis_backend) + 1
            sequence = self._publish_authoritative(
                redis_backend,
                token=token,
                encoded=encoded,
                lease_seconds=lease_seconds,
                sequence_floor=sequence_floor,
            )
            if sequence is None:
                return None
            self._replicate_snapshot(encoded, sequence)
            return candidate
        finally:
            try:
                self._release_lease(token)
            except Exception as err:
                # The lease has a TTL; cleanup failure must not mask either a successful
                # publication or the original fetch/validation exception.
                logger.error(f"Failed to release the image-reference publication lease: {err}")

    @staticmethod
    def _highest_known_sequence(redis_backend: Any) -> int:
        """Return the greatest generation visible across the active and reachable passives."""
        highest = 0
        for server in [redis_backend, *list(hr.all_horde_redis)]:
            try:
                highest = max(highest, _sequence_value(server.get(PUBLISH_SEQUENCE_KEY)))
            except Exception as err:
                logger.error(f"Failed to read an image-reference publication sequence during fan-out: {err}")
        return highest

    @staticmethod
    def _publish_authoritative(
        redis_backend: Any,
        *,
        token: str,
        encoded: str,
        lease_seconds: int,
        sequence_floor: int,
    ) -> int | None:
        """Fence and publish on REDIS_IP, returning a generation for passive fan-out."""
        with redis_backend.pipeline() as pipe:
            for _attempt in range(MAX_TRANSACTION_RETRIES):
                try:
                    pipe.watch(PUBLISH_LOCK_KEY, PUBLISH_SEQUENCE_KEY)
                    if _as_text(pipe.get(PUBLISH_LOCK_KEY)) != token:
                        pipe.unwatch()
                        logger.warning("Discarding image-reference refresh because its publication lease expired.")
                        return None
                    sequence = max(_sequence_value(pipe.get(PUBLISH_SEQUENCE_KEY)) + 1, sequence_floor)
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, encoded)
                    pipe.set(PUBLISH_SEQUENCE_KEY, sequence)
                    pipe.expire(PUBLISH_LOCK_KEY, lease_seconds)
                    pipe.execute()
                    return sequence
                except WatchError:
                    continue
        raise RuntimeError("Image-reference publication exceeded the Redis transaction retry limit.")

    def _replicate_snapshot(self, encoded: str, sequence: int) -> None:
        """Copy the immutable document to every configured independent failover master."""
        replicas = list(hr.all_horde_redis)
        if not replicas:
            logger.warning("No REDIS_SERVERS are available for image-reference snapshot fan-out.")
            return
        for replica in replicas:
            try:
                if not self._store_replica_if_current(replica, encoded=encoded, sequence=sequence):
                    logger.error(
                        "A failover Redis server has a newer image-reference publication sequence; refusing to overwrite it.",
                    )
            except Exception as err:
                logger.error(f"Failed to replicate the image-reference snapshot to a failover Redis server: {err}")

    @staticmethod
    def _store_replica_if_current(replica: Any, *, encoded: str, sequence: int) -> bool:
        """Store only a non-stale generation so a delayed publisher cannot regress a passive."""
        with replica.pipeline() as pipe:
            for _attempt in range(MAX_TRANSACTION_RETRIES):
                try:
                    pipe.watch(PUBLISH_SEQUENCE_KEY)
                    replica_sequence = _sequence_value(pipe.get(PUBLISH_SEQUENCE_KEY))
                    if replica_sequence > sequence:
                        pipe.unwatch()
                        return False
                    if replica_sequence == sequence:
                        existing = _as_text(pipe.get(SNAPSHOT_KEY))
                        pipe.unwatch()
                        return existing == encoded
                    pipe.multi()
                    pipe.set(SNAPSHOT_KEY, encoded)
                    pipe.set(PUBLISH_SEQUENCE_KEY, sequence)
                    pipe.execute()
                    return True
                except WatchError:
                    continue
        raise RuntimeError("Image-reference replica fan-out exceeded the Redis transaction retry limit.")

    @staticmethod
    def _release_lease(token: str) -> None:
        redis_backend = hr.horde_r
        if redis_backend is None:
            return
        with redis_backend.pipeline() as pipe:
            for _attempt in range(MAX_TRANSACTION_RETRIES):
                try:
                    pipe.watch(PUBLISH_LOCK_KEY)
                    if _as_text(pipe.get(PUBLISH_LOCK_KEY)) != token:
                        pipe.unwatch()
                        return
                    pipe.multi()
                    pipe.delete(PUBLISH_LOCK_KEY)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        logger.error("Image-reference lease cleanup exceeded the Redis transaction retry limit; TTL will release it.")


redis_image_reference_snapshots = RedisImageReferenceSnapshots()
