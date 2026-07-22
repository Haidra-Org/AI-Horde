# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queue-pressure simulation users for the text generation pipeline.

This module models three cooperating populations whose interaction places the
text waiting-prompt queue under controlled, phase-driven pressure so that the
lock and session behaviour of the pop/submit/activate paths can be measured:

- ``BacklogRequester`` submits text waiting prompts constrained to models that no
  worker in the run declares. Such prompts activate (``active = true``) but can
  never be served, so they accumulate as an ever-growing queued backlog. The
  10s priority-bump job walks every active ``n > 0`` prompt under
  ``FOR UPDATE SKIP LOCKED``, and the pop candidate query scans and sorts the
  whole waiting-prompt set; both therefore scale with this backlog. Inflation is
  bounded by a target size tracked in shared process state so the backlog is
  driven to a set depth and then held rather than growing without limit.
- ``ServingWorker`` runs text workers that declare a common served-model set and
  continuously pop then submit, providing genuine pop concurrency and the
  per-candidate ``FOR UPDATE`` page locks whose hold time the hypothesis targets.
- ``ServedRequester`` submits a modest, servable workload and polls its status.
  Its read latency is measured separately from worker write latency so that
  "requester reads degrade" can be told apart from "worker writes degrade".

Every request is given a distinct Locust name (``[qp] pop``/``submit``/
``async ...``/``status ...``) so per-endpoint latency series can be recovered
from the Locust CSV history.

The population runs through three timed phases within a single run: a baseline
with workers serving and only light served traffic, a pressure phase during
which the backlog is inflated toward the target, and a relief phase during which
inflation stops so recovery can be observed. Phase membership is derived from the
run's elapsed wall-clock time against durations configured at test start.
"""

from __future__ import annotations

import random
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from locust import HttpUser, constant_pacing, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER, _config
from ..helpers import (
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _record_expected,
    _safe_json,
)


class Phase(Enum):
    """The three phases a queue-pressure run moves through in order."""

    BASELINE = "baseline"
    PRESSURE = "pressure"
    RELIEF = "relief"


@dataclass
class _QueuePressureState:
    """Process-wide coordination state shared by every queue-pressure user.

    A single Locust worker process runs all users as cooperative greenlets, so a
    plain object guarded by a lock is sufficient to coordinate the collective
    backlog target across the ``BacklogRequester`` population without any
    cross-process concern.
    """

    run_start: float = 0.0
    baseline_s: float = 60.0
    pressure_s: float = 150.0
    relief_s: float = 60.0

    backlog_target: int = 3000
    backlog_models: list[str] = field(default_factory=list)
    backlog_keys: list[str] = field(default_factory=list)
    # Per-key count of prompts believed to be in flight for that key, used to
    # steer new prompts toward keys that have not yet hit the per-user concurrency
    # cap (untuned users default to 30 concurrent waiting prompts).
    backlog_key_inflight: dict[str, int] = field(default_factory=dict)
    backlog_per_key_cap: int = 28
    backlog_created: int = 0

    served_models: list[str] = field(default_factory=list)
    gen_time_min: float = 0.5
    gen_time_max: float = 2.5
    served_max_length: int = 32
    worker_max_length: int = 512
    worker_max_context_length: int = 2048

    lock: threading.Lock = field(default_factory=threading.Lock)


qp_state = _QueuePressureState()


def configure_queue_pressure(
    *,
    baseline_s: float,
    pressure_s: float,
    relief_s: float,
    backlog_target: int,
    backlog_models: list[str],
    backlog_keys: list[str],
    backlog_per_key_cap: int,
    served_models: list[str],
    gen_time_min: float,
    gen_time_max: float,
    served_max_length: int,
    worker_max_length: int,
    worker_max_context_length: int,
) -> None:
    """Reset and populate the shared state at the start of a run.

    Called once from the locustfile's ``test_start`` handler so the CLI-derived
    parameters are guaranteed to be parsed before any user spawns.
    """
    qp_state.run_start = time.time()
    qp_state.baseline_s = baseline_s
    qp_state.pressure_s = pressure_s
    qp_state.relief_s = relief_s
    qp_state.backlog_target = backlog_target
    qp_state.backlog_models = list(backlog_models)
    qp_state.backlog_keys = list(backlog_keys)
    qp_state.backlog_key_inflight = {key: 0 for key in backlog_keys}
    qp_state.backlog_per_key_cap = backlog_per_key_cap
    qp_state.backlog_created = 0
    qp_state.served_models = list(served_models)
    qp_state.gen_time_min = gen_time_min
    qp_state.gen_time_max = gen_time_max
    qp_state.served_max_length = served_max_length
    qp_state.worker_max_length = worker_max_length
    qp_state.worker_max_context_length = worker_max_context_length


def current_phase() -> Phase:
    """Return the phase the run is currently in based on elapsed wall-clock time."""
    elapsed = time.time() - qp_state.run_start
    if elapsed < qp_state.baseline_s:
        return Phase.BASELINE
    if elapsed < qp_state.baseline_s + qp_state.pressure_s:
        return Phase.PRESSURE
    return Phase.RELIEF


def _claim_backlog_key() -> str | None:
    """Reserve capacity on a backlog key, or ``None`` if the target is reached.

    Returns the chosen key with its in-flight tally pre-incremented so concurrent
    greenlets do not all pile onto the same key. The reservation is released via
    ``_release_backlog_key`` when a create attempt fails, keeping the tally an
    honest proxy for the live backlog (unserved prompts never drain on their own).
    """
    with qp_state.lock:
        if qp_state.backlog_created >= qp_state.backlog_target:
            return None
        candidates = [key for key in qp_state.backlog_keys if qp_state.backlog_key_inflight.get(key, 0) < qp_state.backlog_per_key_cap]
        if not candidates:
            return None
        key = min(candidates, key=lambda k: qp_state.backlog_key_inflight.get(k, 0))
        qp_state.backlog_key_inflight[key] += 1
        qp_state.backlog_created += 1
        return key


def _release_backlog_key(key: str, *, mark_full: bool) -> None:
    """Undo a claim after a failed create; optionally mark the key as saturated."""
    with qp_state.lock:
        qp_state.backlog_created = max(0, qp_state.backlog_created - 1)
        if mark_full:
            # The user's concurrency cap was hit; park the key at the cap so no
            # further attempts are steered to it this run.
            qp_state.backlog_key_inflight[key] = qp_state.backlog_per_key_cap
        else:
            qp_state.backlog_key_inflight[key] = max(0, qp_state.backlog_key_inflight.get(key, 1) - 1)


class BacklogRequester(HttpUser):
    """Inflates the queued text backlog with prompts no worker can serve.

    Only submits during the pressure phase and only until the shared backlog
    target is reached, so the backlog is driven to a controlled depth and then
    held. Prompts are constrained to decoy models and to a short ``max_length``
    so they remain valid, activate, and sit in the queue indefinitely without
    ever matching a serving worker.
    """

    fixed_count = 0  # set via --qp-backlog-requestors in the locustfile test_start
    # A tight pacing keeps the inflation rate high without a per-request sleep so
    # the backlog can reach the thousands within the pressure window.
    wait_time = constant_pacing(0.2)

    @tag("qp", "async", "backlog")
    @task
    def inflate_backlog(self) -> None:
        if current_phase() is not Phase.PRESSURE:
            return
        key = _claim_backlog_key()
        if key is None:
            return
        decoy_models = qp_state.backlog_models or ["qp-backlog-unserved"]
        payload = {
            "prompt": "queue pressure backlog " + uuid.uuid4().hex,
            "params": {
                "max_length": min(24, qp_state.served_max_length),
                "max_context_length": 1024,
                "n": 1,
            },
            "models": [random.choice(decoy_models)],
            "trusted_workers": False,
            "slow_workers": True,
        }
        succeeded = False
        try:
            with self.client.post(
                "/api/v2/generate/text/async",
                json=payload,
                headers=_headers(key),
                catch_response=True,
                name="[qp] async backlog",
            ) as resp:
                body = _safe_json(resp)
                if resp.ok:
                    resp.success()
                    succeeded = True
                    return
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "[qp] async backlog", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    return
                if resp.status_code == 400 and _is_expected_rc(body, {"TooManyPrompts", "KudosUpfront", "SharedKeyInsufficientKudos"}):
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "[qp] async backlog", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    # A concurrency-cap rejection means this key is saturated.
                    if _is_expected_rc(body, {"TooManyPrompts"}):
                        _release_backlog_key(key, mark_full=True)
                        succeeded = True  # release already handled; skip the generic release below
                    return
                resp.failure(f"backlog async failed: {resp.status_code}: {resp.text[:200]}")
        finally:
            if not succeeded:
                _release_backlog_key(key, mark_full=False)


class ServingWorker(HttpUser):
    """Text worker that continuously pops and submits servable jobs.

    Declares the common served-model set, so it competes for the servable subset
    of the queue while paging past the unserved backlog the pop query must still
    scan and sort. After a successful pop it waits a short simulated generation
    time and submits, mirroring a fast real worker's cadence.
    """

    fixed_count = 0  # set via --qp-workers in the locustfile test_start
    # Pop promptly; the realistic think time between jobs is the simulated
    # generation time applied after a successful pop, not a fixed task wait.
    wait_time = constant_pacing(0.1)

    def on_start(self) -> None:
        self.worker_name = f"QPWorker-{''.join(random.choices(string.ascii_lowercase, k=6))}"
        keys = _config.get("worker_api_keys", [])
        # Each worker owns a stable key for its lifetime so worker identity does
        # not churn mid-run and confound the pop attribution.
        self.api_key = random.choice(keys) if keys else _config.get("anonymous_api_key", "0000000000")

    @tag("qp", "pop", "submit", "worker")
    @task
    def pop_and_submit(self) -> None:
        served = qp_state.served_models or ["koboldcpp/qp-served"]
        pop_payload = {
            "name": self.worker_name,
            "models": served,
            "bridge_agent": "KoboldAI Client:1.19.2-stress:https://github.com/koboldai/koboldai-client",
            "nsfw": True,
            "max_length": qp_state.worker_max_length,
            "max_context_length": qp_state.worker_max_context_length,
            "softprompts": [],
            "threads": 1,
        }
        with self.client.post(
            "/api/v2/generate/text/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="[qp] pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                    resp.success()
                    _record_expected(self.environment, "POST", "[qp] pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(self.environment, "POST", "[qp] pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    raise RescheduleTask()
                resp.failure(f"pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            job_id = data.get("id")
            resp.success()
            if not job_id:
                return

        time.sleep(random.uniform(qp_state.gen_time_min, qp_state.gen_time_max))

        submit_payload = {
            "id": job_id,
            "generation": "Once upon a time there was a queue pressure run that completed a job.",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="[qp] submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(self.environment, "POST", "[qp] submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", "[qp] submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            resp.failure(f"submit failed: {resp.status_code}: {resp.text[:200]}")


class ServedRequester(HttpUser):
    """Submits a modest servable workload and polls its status.

    Its prompts declare the served-model set so the workers can complete them;
    its request latency (async create and status read) is the control series that
    isolates read-path degradation from the worker write path.
    """

    fixed_count = 0  # set via --qp-served-requestors in the locustfile test_start
    wait_time = constant_pacing(1.0)

    def on_start(self) -> None:
        self.pending_ids: list[str] = []
        keys = _config.get("requestor_api_keys", [])
        self.api_key = random.choice(keys) if keys else _config.get("anonymous_api_key", "0000000000")

    @tag("qp", "async", "served")
    @task(2)
    def submit_served(self) -> None:
        served = qp_state.served_models or ["koboldcpp/qp-served"]
        payload = {
            "prompt": "queue pressure served " + uuid.uuid4().hex[:10],
            "params": {
                "max_length": qp_state.served_max_length,
                "max_context_length": 1024,
                "n": 1,
            },
            "models": [random.choice(served)],
            "trusted_workers": False,
            "slow_workers": True,
        }
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="[qp] async served",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                req_id = (body or {}).get("id")
                if req_id:
                    self.pending_ids.append(req_id)
                return
            if resp.status_code == 429 or (resp.status_code == 400 and _is_expected_rc(body, {"TooManyPrompts", "KudosUpfront"})):
                resp.success()
                _record_expected(
                    self.environment, "POST", "[qp] async served", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
                return
            resp.failure(f"served async failed: {resp.status_code}: {resp.text[:200]}")

    @tag("qp", "status", "served")
    @task(3)
    def poll_served(self) -> None:
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/text/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="[qp] status served",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment, "GET", "[qp] status served", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
            else:
                resp.failure(f"served status failed: {resp.status_code}: {resp.text[:200]}")
