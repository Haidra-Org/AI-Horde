# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run-scoped ground-truth registry and consistency oracle for the attribution suite.

The attribution workload drives the AI Horde text pipeline through deliberately
adversarial timings: several simulated workers share a single identity while
declaring disjoint model lists, and requesters constrain jobs to individual
models. To decide whether any API response is self-consistent, the checks need a
single source of truth for what each worker actually declared and when. This
module holds that source of truth for the lifetime of one Locust process.

Two registries are maintained:

- A declaration timeline mapping each worker name to the ordered list of model
  declarations it made at pop time. A response that attributes a model to a
  worker is consistent only if that worker declared the model at some pop.
- Pair and maintenance slot allocators that let independent Locust greenlets
  self-organize into the fixed adversarial roles the scenarios require.

The oracle records a violation two ways: it fires a Locust request-event failure
under a stable, distinctly named pseudo-endpoint so the violation surfaces in the
standard statistics output, and it appends a structured evidence record to a
JSONL file so a downstream checker can gate a run and inspect the exact timing
that produced the inconsistency.

All shared state is guarded by a ``gevent`` lock. Locust greenlets are
cooperatively scheduled, so contention is only possible across the I/O yield
points inside these helpers, but the lock keeps the compound read-modify-write
operations (timeline appends, slot allocation, evidence writes) atomic regardless
of where a greenlet yields.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import gevent.lock

if TYPE_CHECKING:
    from locust.env import Environment


# ---------------------------------------------------------------------------
# Naming: model and worker identities are derived deterministically from a slot
# index so that independent greenlets agree on identities without coordination.
# ---------------------------------------------------------------------------

_MODEL_NAMESPACE = "attrib"


class WorkerRole(StrEnum):
    """The two members that share a single flapping worker identity.

    Each member declares exactly one model, disjoint from its sibling's, so the
    shared worker's declared model list flaps between the two as the members
    interleave their pops.
    """

    ALPHA = "alpha"
    BETA = "beta"


def pair_role_model(pair_index: int, role: WorkerRole) -> str:
    """Return the single model a pair member declares for the given role."""
    return f"{_MODEL_NAMESPACE}/pair{pair_index}-{role.value}"


def pair_decoy_model(pair_index: int, decoy_index: int) -> str:
    """Return a model that no worker ever declares.

    Requesters add these to a job's constraint list so that the recorded model
    can be checked against the declaration timeline: a response attributing a
    decoy model to any worker is unambiguously inconsistent, because no worker
    ever declared it at any pop.
    """
    return f"{_MODEL_NAMESPACE}/pair{pair_index}-decoy{decoy_index}"


def maintenance_model(slot_index: int) -> str:
    """Return the unique model served only by one maintenance-flip worker."""
    return f"{_MODEL_NAMESPACE}/maint{slot_index}"


# ---------------------------------------------------------------------------
# Declaration timeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelDeclaration:
    """A single pop-time declaration of a worker's model list."""

    wall_time: float
    models: tuple[str, ...]


class DeclarationRegistry:
    """Records, per worker name, the timeline of model lists declared at pop."""

    def __init__(self) -> None:
        self._timelines: dict[str, list[ModelDeclaration]] = {}
        self._lock = gevent.lock.RLock()

    def record(self, worker_name: str, models: tuple[str, ...], wall_time: float | None = None) -> None:
        """Append a declaration for ``worker_name`` made at ``wall_time``."""
        entry = ModelDeclaration(wall_time=wall_time if wall_time is not None else time.time(), models=models)
        with self._lock:
            self._timelines.setdefault(worker_name, []).append(entry)

    def worker_declared_model(self, worker_name: str, model: str) -> bool:
        """Return whether ``worker_name`` ever declared ``model`` at any pop."""
        with self._lock:
            timeline = self._timelines.get(worker_name)
            if not timeline:
                return False
            return any(model in entry.models for entry in timeline)

    def timeline_as_json(self, worker_name: str) -> list[list[object]]:
        """Return the worker's declaration timeline as JSON-serializable rows."""
        with self._lock:
            timeline = self._timelines.get(worker_name, [])
            return [[entry.wall_time, list(entry.models)] for entry in timeline]


declaration_registry = DeclarationRegistry()


# ---------------------------------------------------------------------------
# Pair coordination: the two members of a pair share one PairState instance so
# they agree on the current worker name and owning API key even as the identity
# churns. Only the ALPHA member advances the churn; BETA follows.
# ---------------------------------------------------------------------------


class PairState:
    """Shared, mutable identity for the two members of a flapping worker pair.

    The pair's models are fixed for the whole run so requesters can target them
    stably. The worker *name* and owning key advance on churn to exercise the
    fresh-worker first-pop path (an empty model cache). Untrusted users are
    capped at three distinct workers, so churn rotates through the pair's
    assigned keys; when the cap is nonetheless reached the pair freezes on its
    last-created identity rather than failing.
    """

    def __init__(self, pair_index: int, api_keys: list[str]) -> None:
        self._pair_index = pair_index
        self._api_keys = api_keys
        self._generation = 0
        self._frozen = False
        self._last_churn_at = time.time()
        self._lock = gevent.lock.RLock()

    @property
    def pair_index(self) -> int:
        return self._pair_index

    def current_name(self) -> str:
        with self._lock:
            return f"AttribFlapPair{self._pair_index}-g{self._generation}"

    def current_key(self) -> str:
        with self._lock:
            return self._api_keys[self._generation % len(self._api_keys)]

    def maybe_churn(self, churn_period: float) -> None:
        """Advance to a fresh worker identity if the churn period has elapsed.

        A churn period of zero (or a frozen pair) disables churn entirely.
        """
        if churn_period <= 0:
            return
        with self._lock:
            if self._frozen:
                return
            if (time.time() - self._last_churn_at) < churn_period:
                return
            self._generation += 1
            self._last_churn_at = time.time()

    def rollback_and_freeze(self) -> None:
        """Revert to the previous identity and stop churning.

        Called when creating a fresh identity was rejected (for example the
        untrusted three-worker cap). The previous generation's worker already
        exists, so the pair keeps operating on it.
        """
        with self._lock:
            if self._generation > 0:
                self._generation -= 1
            self._frozen = True


# ---------------------------------------------------------------------------
# Slot allocators: hand out deterministic, monotonically increasing indices so
# that a spawning greenlet knows which pair and role it belongs to.
# ---------------------------------------------------------------------------


@dataclass
class _SlotAllocators:
    flapping_worker_seq: int = 0
    attribution_requester_seq: int = 0
    maintenance_worker_seq: int = 0
    maintenance_requester_seq: int = 0
    pair_states: dict[int, PairState] = field(default_factory=dict)
    lock: gevent.lock.RLock = field(default_factory=gevent.lock.RLock)


_allocators = _SlotAllocators()


def reset_run_state() -> None:
    """Clear all run-scoped registries and counters for a fresh test run."""
    global _allocators
    _allocators = _SlotAllocators()
    declaration_registry._timelines.clear()  # noqa: SLF001 - deliberate run reset


@dataclass(frozen=True)
class FlappingAssignment:
    """The identity a single ``PairedFlappingWorker`` greenlet should assume."""

    pair_index: int
    role: WorkerRole
    pair_state: PairState


def allocate_flapping_worker(pair_key_pool: list[str], keys_per_pair: int) -> FlappingAssignment:
    """Assign the next flapping-worker greenlet to a pair and role.

    Consecutive greenlets fill ``(pair, ALPHA)`` then ``(pair, BETA)`` before
    advancing to the next pair, so every pair gets exactly its two members. Each
    pair is granted a disjoint slice of the worker key pool for churn rotation.
    """
    with _allocators.lock:
        seq = _allocators.flapping_worker_seq
        _allocators.flapping_worker_seq += 1
        pair_index = seq // 2
        role = WorkerRole.ALPHA if (seq % 2 == 0) else WorkerRole.BETA
        pair_state = _allocators.pair_states.get(pair_index)
        if pair_state is None:
            keys = _slice_keys(pair_key_pool, pair_index, keys_per_pair)
            pair_state = PairState(pair_index=pair_index, api_keys=keys)
            _allocators.pair_states[pair_index] = pair_state
        return FlappingAssignment(pair_index=pair_index, role=role, pair_state=pair_state)


def allocate_attribution_requester(pair_count: int) -> int:
    """Assign the next requester greenlet to a target pair index (round-robin)."""
    with _allocators.lock:
        seq = _allocators.attribution_requester_seq
        _allocators.attribution_requester_seq += 1
    return seq % max(pair_count, 1)


def allocate_maintenance_worker() -> int:
    """Assign the next maintenance-worker greenlet a unique slot index."""
    with _allocators.lock:
        slot = _allocators.maintenance_worker_seq
        _allocators.maintenance_worker_seq += 1
    return slot


def allocate_maintenance_requester() -> int:
    """Assign the next maintenance-requester greenlet a slot index."""
    with _allocators.lock:
        slot = _allocators.maintenance_requester_seq
        _allocators.maintenance_requester_seq += 1
    return slot


def _slice_keys(pool: list[str], pair_index: int, keys_per_pair: int) -> list[str]:
    """Return a stable, non-empty slice of ``pool`` for the given pair.

    Falls back to the whole pool when it is too small to give each pair a
    disjoint slice, so the harness still runs (churn simply reuses keys).
    """
    if not pool:
        return [""]
    if keys_per_pair <= 0:
        return [pool[pair_index % len(pool)]]
    start = (pair_index * keys_per_pair) % len(pool)
    keys = [pool[(start + offset) % len(pool)] for offset in range(min(keys_per_pair, len(pool)))]
    return keys


# ---------------------------------------------------------------------------
# Oracle: violation recording
# ---------------------------------------------------------------------------


class OracleName(StrEnum):
    """Stable request-event names under which oracle violations are reported."""

    ATTRIBUTION = "oracle:attribution"
    POSSIBLE_CONTRADICTION = "oracle:possible_contradiction"


_ORACLE_REQUEST_TYPE = "ORACLE"


class OracleRecorder:
    """Records oracle violations to Locust statistics and a JSONL evidence file."""

    def __init__(self) -> None:
        self._evidence_path: Path | None = None
        self._lock = gevent.lock.RLock()

    def configure(self, evidence_path: str | None) -> None:
        """Set the JSONL evidence path and truncate any prior file at that path."""
        with self._lock:
            if not evidence_path:
                self._evidence_path = None
                return
            self._evidence_path = Path(evidence_path)
            self._evidence_path.parent.mkdir(parents=True, exist_ok=True)
            # Truncate so each run's evidence stands alone and the checker never
            # counts violations carried over from an earlier run.
            self._evidence_path.write_text("", encoding="utf-8")

    def record(
        self,
        environment: Environment,
        oracle: OracleName,
        detail: str,
        evidence: dict[str, object],
    ) -> None:
        """Fire a Locust failure for ``oracle`` and append an evidence record."""
        environment.events.request.fire(
            request_type=_ORACLE_REQUEST_TYPE,
            name=str(oracle),
            response_time=0.0,
            response_length=0,
            exception=OracleViolation(f"{oracle}: {detail}"),
            context={},
        )
        record = {"oracle": str(oracle), "wall_time": time.time(), "detail": detail, **evidence}
        with self._lock:
            if self._evidence_path is None:
                return
            with self._evidence_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")


class OracleViolation(Exception):
    """Raised as the exception payload of an oracle failure request event."""


oracle_recorder = OracleRecorder()
