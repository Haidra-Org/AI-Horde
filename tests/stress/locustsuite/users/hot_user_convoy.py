# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hot-user lock-convoy simulation users for the text generation pipeline.

This module drives contention onto a small set of hot ``users`` rows so the
tuple-lock queues and ``FOR NO KEY UPDATE`` waits the production forensics
implicated can be reproduced and measured against a multi-instance rig. The
production pathology is a lock convoy on the anon requester row (``users.id = 0``)
and a few heavy accounts, formed where request activation and generation
settlement both take a ``FOR NO KEY UPDATE`` on the requester and worker-owner
rows. The populations below are weighted toward that shape:

- ``AnonRequester`` submits text requests with the shared anonymous API key, so
  every activation debits ``users.id = 0``. It is the largest population,
  mirroring the production anon share, and requests are amplified with ``n > 1``
  so a single activation seeds many generations that each settle back onto the
  same anon row when a worker submits them.
- ``HeavyProxyRequester`` carries one registered key each and keeps many requests
  concurrently in flight, standing in for the service-account "proxy" requesters
  that concentrate load onto a handful of registered rows.
- ``ConvoyWorker`` pops and submits the served models continuously. Each submit
  settles onto both the worker-owner row and the requester row (the dual-row
  ``FOR NO KEY UPDATE``), providing the write concurrency that turns hot-row
  reads into a queue.
- ``StatusCheckPoller`` polls status/check endpoints, riding the edge's client
  backend and its short micro-cache rather than the worker backend, so read-path
  degradation can be told apart from worker write degradation.
- ``KudosTransfer`` moves kudos between two fixed registered accounts at a slow
  trickle, exercising the solvency-gate read plus dual balance writes.

Every request carries a distinct Locust name (``[hc] async anon`` / ``pop`` /
``submit`` / ``status`` / ...) so per-endpoint latency can be recovered from the
CSV history. The run moves through three timed phases: a moderate baseline, a
pressure phase that maximises concurrent anon activations and settlement bursts,
and a relief phase during which new work stops so recovery can be observed. Phase
membership is derived from elapsed wall-clock time against durations set at test
start.
"""

from __future__ import annotations

import collections
import random
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from locust import HttpUser, constant_pacing, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER
from ..helpers import (
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _record_expected,
    _safe_json,
)

_ANON_API_KEY = "0000000000"
# Expected 4xx rcs the requester paths hit under load and hot-row contention;
# recorded as expected rather than counted as failures so the failure table
# stays a signal for genuine breakage.
_EXPECTED_ASYNC_RC = {"TooManyPrompts", "KudosUpfront", "SharedKeyInsufficientKudos", "AnonWidth", "MaxLength"}
_EXPECTED_SUBMIT_RC = {"InvalidJobID", "InvalidProcGen", "DuplicateGeneration"}
_EXPECTED_KUDOS_RC = {"KudosValidationError", "KudosTransferToSelf", "InvalidTargetUsername", "NotEnoughKudos"}


class Phase(Enum):
    """The three phases a hot-user convoy run moves through in order."""

    BASELINE = "baseline"
    PRESSURE = "pressure"
    RELIEF = "relief"


@dataclass
class _ConvoyState:
    """Process-wide coordination state shared by every convoy user.

    A single Locust worker process runs all users as cooperative greenlets, so a
    plain object guarded by a lock coordinates the shared recent-id ring and the
    kudos-account registry without any cross-process concern.
    """

    run_start: float = 0.0
    baseline_s: float = 60.0
    pressure_s: float = 180.0
    relief_s: float = 60.0

    served_models: list[str] = field(default_factory=list)
    heavy_keys: list[str] = field(default_factory=list)
    worker_keys: list[str] = field(default_factory=list)
    kudos_keys: list[str] = field(default_factory=list)

    gen_time_min: float = 0.2
    gen_time_max: float = 1.0
    max_length: int = 24
    max_context_length: int = 1024
    # Generation fan-out per request per phase: a larger ``n`` in the pressure
    # phase multiplies the settlements that land on the hot requester row from a
    # single (rate-limited) activation.
    n_baseline: int = 2
    n_pressure: int = 6
    # Ceiling on requests a HeavyProxyRequester keeps in flight before it pauses
    # creating and only polls, mirroring the untuned 30-concurrent-prompt cap.
    heavy_max_pending: int = 20

    # Bounded ring of recently created request ids that StatusCheckPoller samples,
    # so many pollers hit the same status paths and ride the edge micro-cache.
    recent_ids: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=256))
    # Registry of resolved "name#id" identifiers for the kudos accounts, so the
    # two KudosTransfer users can address each other once both have started.
    kudos_usernames: list[str] = field(default_factory=list)

    lock: threading.Lock = field(default_factory=threading.Lock)


convoy_state = _ConvoyState()


def configure_convoy(
    *,
    baseline_s: float,
    pressure_s: float,
    relief_s: float,
    served_models: list[str],
    heavy_keys: list[str],
    worker_keys: list[str],
    kudos_keys: list[str],
    gen_time_min: float,
    gen_time_max: float,
    max_length: int,
    max_context_length: int,
    n_baseline: int,
    n_pressure: int,
    heavy_max_pending: int,
) -> None:
    """Reset and populate the shared state at the start of a run.

    Called once from the locustfile's ``test_start`` handler so the CLI-derived
    parameters are parsed before any user spawns.
    """
    convoy_state.run_start = time.time()
    convoy_state.baseline_s = baseline_s
    convoy_state.pressure_s = pressure_s
    convoy_state.relief_s = relief_s
    convoy_state.served_models = list(served_models)
    convoy_state.heavy_keys = list(heavy_keys)
    convoy_state.worker_keys = list(worker_keys)
    convoy_state.kudos_keys = list(kudos_keys)
    convoy_state.gen_time_min = gen_time_min
    convoy_state.gen_time_max = gen_time_max
    convoy_state.max_length = max_length
    convoy_state.max_context_length = max_context_length
    convoy_state.n_baseline = n_baseline
    convoy_state.n_pressure = n_pressure
    convoy_state.heavy_max_pending = heavy_max_pending
    convoy_state.recent_ids.clear()
    convoy_state.kudos_usernames = []


def current_phase() -> Phase:
    """Return the phase the run is currently in based on elapsed wall-clock time."""
    elapsed = time.time() - convoy_state.run_start
    if elapsed < convoy_state.baseline_s:
        return Phase.BASELINE
    if elapsed < convoy_state.baseline_s + convoy_state.pressure_s:
        return Phase.PRESSURE
    return Phase.RELIEF


def _phase_n() -> int:
    """Return the generation fan-out for the current phase."""
    return convoy_state.n_pressure if current_phase() is Phase.PRESSURE else convoy_state.n_baseline


def _remember_id(request_id: str) -> None:
    with convoy_state.lock:
        convoy_state.recent_ids.append(request_id)


def _sample_recent_id() -> str | None:
    with convoy_state.lock:
        if not convoy_state.recent_ids:
            return None
        return random.choice(convoy_state.recent_ids)


def _served_model() -> str:
    return random.choice(convoy_state.served_models or ["koboldcpp/hc-served"])


def _async_payload(n: int) -> dict:
    """Build a small, valid text async payload the convoy workers can serve."""
    return {
        "prompt": "hot user convoy " + uuid.uuid4().hex,
        "params": {
            "max_length": convoy_state.max_length,
            "max_context_length": convoy_state.max_context_length,
            "n": n,
        },
        "models": [_served_model()],
        "trusted_workers": False,
        "slow_workers": True,
    }


def _submit_async(user: HttpUser, api_key: str, name: str, remember: bool) -> None:
    """POST a text async request, folding expected rejections into the stats.

    On success the request id is optionally added to the shared recent-id ring so
    pollers can reference it. Rate-limit and hot-row rejections are recorded under
    the ``[expected]`` name rather than failing the run.
    """
    with user.client.post(
        "/api/v2/generate/text/async",
        json=_async_payload(_phase_n()),
        headers=_headers(api_key),
        catch_response=True,
        name=name,
    ) as resp:
        body = _safe_json(resp)
        if resp.ok:
            resp.success()
            request_id = (body or {}).get("id")
            if request_id and remember:
                _remember_id(request_id)
            return
        if resp.status_code == 429 or (resp.status_code == 400 and _is_expected_rc(body, _EXPECTED_ASYNC_RC)):
            resp.success()
            _record_expected(user.environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            return
        resp.failure(f"async failed: {resp.status_code}: {resp.text[:200]}")


def _poll_status(user: HttpUser, api_key: str, request_id: str, name: str, pending: list[str] | None) -> None:
    """GET a text status, treating completion and 404/410 as terminal."""
    with user.client.get(
        f"/api/v2/generate/text/status/{request_id}",
        headers=_headers(api_key),
        catch_response=True,
        name=name,
    ) as resp:
        if resp.ok:
            data = _safe_json(resp) or {}
            if (data.get("done") or data.get("faulted")) and pending is not None and request_id in pending:
                pending.remove(request_id)
            resp.success()
            return
        if resp.status_code in (404, 410):
            if pending is not None and request_id in pending:
                pending.remove(request_id)
            resp.success()
            return
        if resp.status_code == 429:
            resp.success()
            _record_expected(user.environment, "GET", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            return
        resp.failure(f"status failed: {resp.status_code}: {resp.text[:200]}")


class AnonRequester(HttpUser):
    """Submits amplified anonymous text requests that debit ``users.id = 0``.

    The largest population by design. Every activation locks the anon requester
    row, and the ``n``-amplified generations each settle back onto it when a
    ConvoyWorker submits, so this class supplies the bulk of the hot-row write
    pressure. It also polls a shared recent id so the status read path is exercised
    from the anon identity too.
    """

    fixed_count = 0  # set via --hc-anon-requestors in the locustfile test_start
    wait_time = constant_pacing(0.5)

    @tag("hc", "async", "anon")
    @task(4)
    def submit_anon(self) -> None:
        if current_phase() is Phase.RELIEF:
            return
        _submit_async(self, _ANON_API_KEY, "[hc] async anon", remember=True)

    @tag("hc", "status", "anon")
    @task(1)
    def poll_anon(self) -> None:
        request_id = _sample_recent_id()
        if request_id is None:
            return
        _poll_status(self, _ANON_API_KEY, request_id, "[hc] status anon", pending=None)


class HeavyProxyRequester(HttpUser):
    """Registered "service account" that keeps many requests concurrently in flight.

    Concentrates load onto a single registered row per key, mirroring the proxy
    accounts that carry disproportionate concurrency in production. It pauses
    creating new work once its pending set reaches the cap and only polls, so it
    holds a steady in-flight population rather than growing without bound.
    """

    fixed_count = 0  # set via --hc-heavy-requestors in the locustfile test_start
    wait_time = constant_pacing(0.3)

    def on_start(self) -> None:
        self.pending_ids: list[str] = []
        keys = convoy_state.heavy_keys
        self.api_key = random.choice(keys) if keys else _ANON_API_KEY

    @tag("hc", "async", "heavy")
    @task(3)
    def submit_heavy(self) -> None:
        if current_phase() is Phase.RELIEF or len(self.pending_ids) >= convoy_state.heavy_max_pending:
            return
        with self.client.post(
            "/api/v2/generate/text/async",
            json=_async_payload(_phase_n()),
            headers=_headers(self.api_key),
            catch_response=True,
            name="[hc] async heavy",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                request_id = (body or {}).get("id")
                if request_id:
                    self.pending_ids.append(request_id)
                    _remember_id(request_id)
                return
            if resp.status_code == 429 or (resp.status_code == 400 and _is_expected_rc(body, _EXPECTED_ASYNC_RC)):
                resp.success()
                _record_expected(
                    self.environment, "POST", "[hc] async heavy", resp.elapsed.total_seconds() * 1000, len(resp.content or b""),
                )
                return
            resp.failure(f"heavy async failed: {resp.status_code}: {resp.text[:200]}")

    @tag("hc", "status", "heavy")
    @task(2)
    def poll_heavy(self) -> None:
        if not self.pending_ids:
            return
        request_id = random.choice(self.pending_ids)
        _poll_status(self, self.api_key, request_id, "[hc] status heavy", pending=self.pending_ids)


class ConvoyWorker(HttpUser):
    """Text worker that continuously pops and submits the served models.

    Each successful submit settles onto both the worker-owner row and the
    requester row, taking the paired ``FOR NO KEY UPDATE`` the convoy hypothesis
    targets. A large worker population against anon-owned generations concentrates
    those settlements onto ``users.id = 0``.
    """

    fixed_count = 0  # set via --hc-workers in the locustfile test_start
    wait_time = constant_pacing(0.1)

    def on_start(self) -> None:
        self.worker_name = f"HCWorker-{''.join(random.choices(string.ascii_lowercase, k=8))}"
        keys = convoy_state.worker_keys
        self.api_key = random.choice(keys) if keys else _ANON_API_KEY

    @tag("hc", "pop", "submit", "worker")
    @task
    def pop_and_submit(self) -> None:
        pop_payload = {
            "name": self.worker_name,
            "models": convoy_state.served_models or ["koboldcpp/hc-served"],
            "bridge_agent": "KoboldAI Client:1.19.2-stress:https://github.com/koboldai/koboldai-client",
            "nsfw": True,
            "max_length": 512,
            "max_context_length": 2048,
            "softprompts": [],
            "threads": 1,
        }
        with self.client.post(
            "/api/v2/generate/text/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="[hc] pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                    resp.success()
                    _record_expected(self.environment, "POST", "[hc] pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(self.environment, "POST", "[hc] pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    raise RescheduleTask()
                resp.failure(f"pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            job_id = data.get("id")
            resp.success()
            if not job_id:
                return

        time.sleep(random.uniform(convoy_state.gen_time_min, convoy_state.gen_time_max))

        submit_payload = {
            "id": job_id,
            "generation": "Once upon a time there was a hot user convoy run that settled a job.",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="[hc] submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, _EXPECTED_SUBMIT_RC):
                resp.success()
                _record_expected(self.environment, "POST", "[hc] submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", "[hc] submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            resp.failure(f"submit failed: {resp.status_code}: {resp.text[:200]}")


class StatusCheckPoller(HttpUser):
    """Polls status/check endpoints, riding the edge client backend and micro-cache.

    Half its calls target a shared recent request id (so many pollers converge on
    the same cacheable status path) and half a cacheable meta endpoint. Its latency
    is the read-path control series that isolates cache-fronted reads from the
    hot-row write path.
    """

    fixed_count = 0  # set via --hc-status-pollers in the locustfile test_start
    wait_time = constant_pacing(0.5)

    def on_start(self) -> None:
        keys = convoy_state.heavy_keys
        self.api_key = random.choice(keys) if keys else _ANON_API_KEY

    @tag("hc", "status", "poller")
    @task(3)
    def poll_shared(self) -> None:
        request_id = _sample_recent_id()
        if request_id is None:
            return
        _poll_status(self, _ANON_API_KEY, request_id, "[hc] status poll", pending=None)

    @tag("hc", "status", "meta")
    @task(1)
    def poll_meta(self) -> None:
        with self.client.get(
            "/api/v2/status/models?type=text",
            headers=_headers(self.api_key),
            catch_response=True,
            name="[hc] status models",
        ) as resp:
            if resp.ok or resp.status_code == 429:
                resp.success()
                return
            resp.failure(f"status models failed: {resp.status_code}: {resp.text[:200]}")


class KudosTransfer(HttpUser):
    """Transfers kudos between two fixed registered accounts at a slow trickle.

    Each user resolves its own ``name#id`` at start and registers it so the pair
    can address each other. Every transfer exercises the solvency-gate read plus
    the dual balance writes, adding a second, distinct hot-row write path alongside
    generation settlement.
    """

    fixed_count = 0  # set via --hc-kudos-users in the locustfile test_start
    wait_time = constant_pacing(1.0)

    def on_start(self) -> None:
        keys = convoy_state.kudos_keys
        self.api_key = random.choice(keys) if keys else None
        self.username: str | None = None
        if self.api_key is None:
            return
        with self.client.get(
            "/api/v2/find_user",
            headers=_headers(self.api_key),
            catch_response=True,
            name="[hc] find_user",
        ) as resp:
            if resp.ok:
                self.username = (_safe_json(resp) or {}).get("username")
                if self.username:
                    with convoy_state.lock:
                        if self.username not in convoy_state.kudos_usernames:
                            convoy_state.kudos_usernames.append(self.username)
                resp.success()
            else:
                resp.failure(f"find_user failed: {resp.status_code}: {resp.text[:200]}")

    @tag("hc", "kudos")
    @task
    def transfer(self) -> None:
        if self.api_key is None or self.username is None:
            return
        with convoy_state.lock:
            targets = [name for name in convoy_state.kudos_usernames if name != self.username]
        if not targets:
            return
        with self.client.post(
            "/api/v2/kudos/transfer",
            json={"username": random.choice(targets), "amount": 1},
            headers=_headers(self.api_key),
            catch_response=True,
            name="[hc] kudos transfer",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 429 or (resp.status_code == 400 and _is_expected_rc(body, _EXPECTED_KUDOS_RC)):
                resp.success()
                _record_expected(
                    self.environment, "POST", "[hc] kudos transfer", resp.elapsed.total_seconds() * 1000, len(resp.content or b""),
                )
                return
            resp.failure(f"kudos transfer failed: {resp.status_code}: {resp.text[:200]}")
