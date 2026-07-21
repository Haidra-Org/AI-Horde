# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adversarial-timing text workloads for the attribution and possibility oracles.

These Locust users drive the AI Horde text pipeline into two consistency
scenarios and check every response against the run-scoped ground truth in
``locustsuite.ground_truth``:

- A flapping-identity scenario in which two workers share one identity and one
  API key while declaring disjoint single-model lists, interleaving their pops so
  the shared worker's declared model list alternates rapidly. Requesters then
  constrain jobs to one real model of the pair alongside decoy models that no
  worker declares. The oracle asserts that every model a status response
  attributes to a worker was declared by that worker at some pop, and that it was
  one the requester asked for.
- A maintenance-flip scenario in which a worker pops a job, immediately enters
  maintenance while holding that job in flight, and only later submits it. Its
  paired requester asserts that a request with an in-flight generation is never
  reported as impossible.

The workloads are intended to be run with fixed per-class user counts pinned by
the entrypoint's ``test_start`` handler (see ``locustfile_attribution.py``). Their
nominal ``weight`` is only a placeholder that keeps Locust from pruning the class
before those fixed counts are applied.
"""

from __future__ import annotations

import random
import time
import uuid

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER, _config
from ..ground_truth import (
    OracleName,
    WorkerRole,
    allocate_attribution_requester,
    allocate_flapping_worker,
    allocate_maintenance_requester,
    allocate_maintenance_worker,
    declaration_registry,
    maintenance_model,
    oracle_recorder,
    pair_decoy_model,
    pair_role_model,
)
from ..helpers import (
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _pick_requestor_key,
    _record_expected,
    _safe_json,
)

# Each pair is granted a small slice of the worker key pool so churn can rotate
# through distinct owning accounts. Untrusted users are capped at three distinct
# workers, which bounds how many fresh identities one key can host.
_KEYS_PER_PAIR = 3

# A modest in-flight cap keeps each requester cycling jobs quickly rather than
# building a deep queue, so the adversarial pop/declare interleavings recur often.
_MAX_PENDING = 3

_TEXT_BRIDGE_AGENT = "KoboldAI Client:1.19.2-stress:https://github.com/koboldai/koboldai-client"


def _maintenance_worker_key(slot_index: int) -> str:
    """Return a worker key for a maintenance slot, taken from the pool's end.

    Maintenance workers own a dedicated worker each and must PUT their own
    maintenance state, so they draw keys from the opposite end of the pool from
    the flapping pairs to avoid two adversarial workers sharing one untrusted
    account (which would compete for that account's three-worker cap).
    """
    pool = _config.get("worker_api_keys", [])
    if not pool:
        return _config.get("anonymous_api_key", "0000000000")
    return pool[-(1 + (slot_index % len(pool)))]


class PairedFlappingWorker(HttpUser):
    """One member of a two-member worker pair that shares a single identity.

    Both members pop under the same worker name and owning key, each declaring a
    single model disjoint from its sibling's, so the shared worker's declared
    model list flaps between the two as the members interleave their pops. The
    declaration is recorded in the ground-truth timeline immediately before each
    pop request fires. A placeholder generation is submitted for any job popped so
    requester jobs complete quickly and the interleaving recurs at high cadence.

    The ALPHA member periodically churns the pair onto a fresh worker identity to
    exercise the first-pop path of a worker whose model cache is still empty.
    """

    # Kept > 0 so Locust does not prune the class before the entrypoint's
    # test_start handler pins the exact per-class fixed_count.
    weight = 1
    fixed_count = 0
    wait_time = between(0.05, 0.35)

    def on_start(self) -> None:
        pool = _config.get("worker_api_keys", [])
        self.assignment = allocate_flapping_worker(pool, keys_per_pair=_KEYS_PER_PAIR)
        self.model = pair_role_model(self.assignment.pair_index, self.assignment.role)
        # Only one member drives churn so the shared generation counter advances
        # once per period rather than once per member.
        self.drives_churn = self.assignment.role is WorkerRole.ALPHA

    @tag("attribution", "worker")
    @task
    def pop_and_submit(self) -> None:
        opts = self.environment.parsed_options
        state = self.assignment.pair_state
        if self.drives_churn:
            state.maybe_churn(opts.attribution_churn_period)
        worker_name = state.current_name()
        api_key = state.current_key()
        # Record before the pop fires so the timeline never lags the server's
        # view of what this worker declared for this pop.
        declaration_registry.record(worker_name, (self.model,))
        pop_payload = {
            "name": worker_name,
            "models": [self.model],
            "bridge_agent": _TEXT_BRIDGE_AGENT,
            "nsfw": True,
            "max_length": 80,
            "max_context_length": 1024,
            "softprompts": [],
            "threads": 1,
        }
        job_id = self._pop(api_key, pop_payload, state)
        if job_id is None:
            return
        self._submit_placeholder(api_key, job_id)

    def _pop(self, api_key: str, pop_payload: dict[str, object], state: object) -> str | None:
        with self.client.post(
            "/api/v2/generate/text/pop",
            json=pop_payload,
            headers=_headers(api_key),
            catch_response=True,
            name="/api/v2/generate/text/pop [attribution]",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                data = body or {}
                return data.get("id")
            if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                resp.success()
                _record_expected(
                    self.environment,
                    "POST",
                    "/api/v2/generate/text/pop [attribution]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
                # The untrusted three-worker cap means a fresh churn identity was
                # rejected: fall back to the pair's last existing identity.
                if _is_too_many_workers(body):
                    state.rollback_and_freeze()  # type: ignore[attr-defined]
                raise RescheduleTask()
            if resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment,
                    "POST",
                    "/api/v2/generate/text/pop [attribution]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
                raise RescheduleTask()
            resp.failure(f"Text pop failed: {resp.status_code}: {resp.text[:200]}")
            return None

    def _submit_placeholder(self, api_key: str, job_id: str) -> None:
        # Submit promptly so the requester's job completes and the flapping race
        # recurs; the attribution defect lives in the server-side pop-to-procgen
        # window, not in client submit timing.
        time.sleep(random.uniform(0.0, 0.1))
        submit_payload = {
            "id": job_id,
            "generation": "Placeholder generation produced by the attribution stress scenario.",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(api_key),
            catch_response=True,
            name="/api/v2/generate/text/submit [attribution]",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok or resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                return
            if resp.status_code == 429:
                resp.success()
                return
            resp.failure(f"Text submit failed: {resp.status_code}: {resp.text[:200]}")


class AttributionRequester(HttpUser):
    """Submits single-model text jobs and checks the reported worker/model pairing.

    Each request constrains generation to exactly one real model of a target pair
    plus a configurable number of decoy models that no worker ever declares. The
    request therefore names a model set whose only servable member is the pair's
    real model, so a well-behaved server records that real model. On completion
    the requester checks every generation in the final status against the
    ground-truth declaration timeline.
    """

    # Kept > 0 so Locust does not prune the class before the entrypoint's
    # test_start handler pins the exact per-class fixed_count.
    weight = 1
    fixed_count = 0
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        opts = self.environment.parsed_options
        self.api_key = _pick_requestor_key()
        self.pair_index = allocate_attribution_requester(opts.attribution_pairs)
        # Alternate which member's model is targeted so both declarations of the
        # shared identity are exercised as the constraint.
        role = random.choice(list(WorkerRole))
        real_model = pair_role_model(self.pair_index, role)
        decoys = [pair_decoy_model(self.pair_index, decoy_index) for decoy_index in range(int(opts.attribution_decoys))]
        self.requested_models = [real_model, *decoys]
        self.requested_model_set = set(self.requested_models)
        self.pending_ids: list[str] = []

    @tag("attribution", "requestor")
    @task(3)
    def submit(self) -> None:
        if len(self.pending_ids) >= _MAX_PENDING:
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": "attribution probe " + uuid.uuid4().hex[:10],
            "params": {
                "max_length": int(opts.attribution_max_length),
                "max_context_length": 512,
                "temperature": 0.7,
                "top_p": 0.9,
                "n": 1,
            },
            "models": self.requested_models,
            "trusted_workers": False,
        }
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/async [attribution]",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                req_id = (body or {}).get("id")
                if req_id:
                    self.pending_ids.append(req_id)
                return
            if resp.status_code == 429 or _is_expected_rc(body, {"KudosUpfront", "SharedKeyInsufficientKudos"}):
                resp.success()
                _record_expected(
                    self.environment,
                    "POST",
                    "/api/v2/generate/text/async [attribution]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
                raise RescheduleTask()
            resp.failure(f"Text async failed: {resp.status_code}: {resp.text[:200]}")

    @tag("attribution", "requestor")
    @task(6)
    def poll(self) -> None:
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/text/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/status/[id] [attribution]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                resp.success()
                if data.get("done") or data.get("faulted"):
                    self._check_generations(req_id, data)
                    if req_id in self.pending_ids:
                        self.pending_ids.remove(req_id)
            elif resp.status_code in (404, 410):
                if req_id in self.pending_ids:
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment,
                    "GET",
                    "/api/v2/generate/text/status/[id] [attribution]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    def _check_generations(self, req_id: str, data: dict[str, object]) -> None:
        generations = data.get("generations") or []
        if not isinstance(generations, list):
            return
        for generation in generations:
            if not isinstance(generation, dict):
                continue
            worker_name = generation.get("worker_name")
            reported_model = generation.get("model")
            worker_id = generation.get("worker_id")
            if not worker_name or not reported_model:
                # An empty recorded model is a deliberate post-fix outcome (record
                # no model rather than a wrong one) and is not an attribution
                # contradiction, so it is not treated as a violation.
                continue
            declared = declaration_registry.worker_declared_model(worker_name, reported_model)
            asked = reported_model in self.requested_model_set
            if declared and asked:
                continue
            if not declared:
                detail = f"worker '{worker_name}' reported model '{reported_model}' it never declared at any pop"
            else:
                detail = f"reported model '{reported_model}' was not among the requested models {self.requested_models}"
            oracle_recorder.record(
                self.environment,
                OracleName.ATTRIBUTION,
                detail,
                {
                    "request_id": req_id,
                    "generation_id": None,
                    "worker_id": worker_id,
                    "worker_name": worker_name,
                    "reported_model": reported_model,
                    "requested_models": self.requested_models,
                    "reported_model_declared": declared,
                    "reported_model_requested": asked,
                    "declared_timeline": declaration_registry.timeline_as_json(worker_name),
                },
            )


class MaintenanceFlipWorker(HttpUser):
    """A worker that pops a job, enters maintenance holding it, then releases it.

    Each cycle pops one job for its unique model, immediately puts itself into
    maintenance while keeping that generation in flight, holds for a bounded
    window, then exits maintenance and submits. While it is in maintenance it is
    the only worker able to serve its model, so a request with the in-flight
    generation has no currently-eligible worker: the condition under which a
    consistent server must still report the request as possible.
    """

    # Kept > 0 so Locust does not prune the class before the entrypoint's
    # test_start handler pins the exact per-class fixed_count.
    weight = 1
    fixed_count = 0
    wait_time = between(0.2, 0.6)

    def on_start(self) -> None:
        self.slot = allocate_maintenance_worker()
        self.model = maintenance_model(self.slot)
        self.api_key = _maintenance_worker_key(self.slot)
        self.worker_name = f"AttribMaint{self.slot}"
        self.worker_id: str | None = None
        self.cycles_done = 0

    @tag("attribution", "maintenance", "worker")
    @task
    def cycle(self) -> None:
        opts = self.environment.parsed_options
        cycle_cap = int(opts.maintenance_cycles)
        if cycle_cap > 0 and self.cycles_done >= cycle_cap:
            # This worker has completed its assigned cycles; idle so the request
            # side can still observe its final released state.
            time.sleep(1.0)
            return
        job_id = self._pop()
        if job_id is None:
            return
        if not self._ensure_worker_id():
            # Without the worker id the maintenance PUT cannot be addressed; submit
            # the job so it is not stranded, then retry the identity next cycle.
            self._submit(job_id)
            return
        if not self._set_maintenance(True):
            self._submit(job_id)
            return
        # Hold the popped job in flight while impossible-to-serve from any other
        # worker, giving the requester a window to observe processing > 0.
        time.sleep(random.uniform(float(opts.maintenance_hold_min), float(opts.maintenance_hold_max)))
        self._set_maintenance(False)
        self._submit(job_id)
        self.cycles_done += 1

    def _pop(self) -> str | None:
        pop_payload = {
            "name": self.worker_name,
            "models": [self.model],
            "bridge_agent": _TEXT_BRIDGE_AGENT,
            "nsfw": True,
            "max_length": 80,
            "max_context_length": 1024,
            "softprompts": [],
            "threads": 1,
        }
        with self.client.post(
            "/api/v2/generate/text/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/pop [maintenance]",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return (body or {}).get("id")
            if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                resp.success()
                _record_expected(
                    self.environment,
                    "POST",
                    "/api/v2/generate/text/pop [maintenance]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
                raise RescheduleTask()
            if resp.status_code == 429:
                resp.success()
                raise RescheduleTask()
            resp.failure(f"Maintenance pop failed: {resp.status_code}: {resp.text[:200]}")
            return None

    def _ensure_worker_id(self) -> bool:
        if self.worker_id is not None:
            return True
        with self.client.get(
            f"/api/v2/workers/name/{self.worker_name}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/workers/name/[name] [maintenance]",
        ) as resp:
            if resp.ok:
                resp.success()
                data = _safe_json(resp) or {}
                self.worker_id = data.get("id") if isinstance(data, dict) else None
                return self.worker_id is not None
            if resp.status_code == 404:
                resp.success()
                return False
            resp.failure(f"Worker lookup failed: {resp.status_code}: {resp.text[:200]}")
            return False

    def _set_maintenance(self, enabled: bool) -> bool:
        with self.client.put(
            f"/api/v2/workers/{self.worker_id}",
            json={"maintenance": enabled, "maintenance_msg": "attribution maintenance-flip scenario"},
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/workers/[id] [maintenance-flip]",
        ) as resp:
            if resp.ok:
                resp.success()
                return True
            if resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment,
                    "PUT",
                    "/api/v2/workers/[id] [maintenance-flip]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
                return False
            resp.failure(f"Maintenance toggle failed: {resp.status_code}: {resp.text[:200]}")
            return False

    def _submit(self, job_id: str) -> None:
        submit_payload = {
            "id": job_id,
            "generation": "Maintenance-flip scenario generation.",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/submit [maintenance]",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok or resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                return
            if resp.status_code == 429:
                resp.success()
                return
            resp.failure(f"Maintenance submit failed: {resp.status_code}: {resp.text[:200]}")


class MaintenanceFlipRequester(HttpUser):
    """Requests a maintenance-flip worker's model and checks the possibility flag.

    On every status poll of an outstanding request it asserts the consistency
    invariant that a request with an in-flight generation is never reported as
    impossible: a response carrying ``processing > 0`` together with
    ``is_possible`` false is recorded as a contradiction.
    """

    # Kept > 0 so Locust does not prune the class before the entrypoint's
    # test_start handler pins the exact per-class fixed_count.
    weight = 1
    fixed_count = 0
    wait_time = between(0.15, 0.5)

    def on_start(self) -> None:
        opts = self.environment.parsed_options
        self.slot = allocate_maintenance_requester()
        worker_count = max(int(opts.maintenance_workers), 1)
        # Target the model of a maintenance worker slot so requests are servable
        # only by the worker that flaps in and out of maintenance.
        self.model = maintenance_model(self.slot % worker_count)
        self.api_key = _pick_requestor_key()
        self.pending_ids: list[str] = []

    @tag("attribution", "maintenance", "requestor")
    @task(3)
    def submit(self) -> None:
        if len(self.pending_ids) >= _MAX_PENDING:
            return
        payload = {
            "prompt": "maintenance possibility probe " + uuid.uuid4().hex[:10],
            "params": {"max_length": 40, "max_context_length": 512, "temperature": 0.7, "top_p": 0.9, "n": 1},
            "models": [self.model],
            "trusted_workers": False,
        }
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/async [maintenance]",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                req_id = (body or {}).get("id")
                if req_id:
                    self.pending_ids.append(req_id)
                return
            if resp.status_code == 429 or _is_expected_rc(body, {"KudosUpfront", "SharedKeyInsufficientKudos"}):
                resp.success()
                raise RescheduleTask()
            resp.failure(f"Text async failed: {resp.status_code}: {resp.text[:200]}")

    @tag("attribution", "maintenance", "requestor")
    @task(6)
    def poll(self) -> None:
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/text/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/status/[id] [maintenance]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                resp.success()
                self._check_possibility(req_id, data)
                if data.get("done") or data.get("faulted"):
                    if req_id in self.pending_ids:
                        self.pending_ids.remove(req_id)
            elif resp.status_code in (404, 410):
                if req_id in self.pending_ids:
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment,
                    "GET",
                    "/api/v2/generate/text/status/[id] [maintenance]",
                    resp.elapsed.total_seconds() * 1000,
                    len(resp.content or b""),
                )
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    def _check_possibility(self, req_id: str, data: dict[str, object]) -> None:
        processing = data.get("processing", 0)
        is_possible = data.get("is_possible", True)
        if not isinstance(processing, int):
            return
        if processing > 0 and is_possible is False:
            oracle_recorder.record(
                self.environment,
                OracleName.POSSIBLE_CONTRADICTION,
                f"request reported is_possible=false while processing={processing}",
                {
                    "request_id": req_id,
                    "generation_id": None,
                    "model": self.model,
                    "processing": processing,
                    "is_possible": is_possible,
                    "waiting": data.get("waiting"),
                    "finished": data.get("finished"),
                    "done": data.get("done"),
                },
            )
