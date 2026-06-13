# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Image generation requestor and worker Locust users."""

import random
import string
import time

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER, _HOT_PROMPT, _config
from ..helpers import (
    _handle_async_generate,
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _pick_requestor_key,
    _pick_worker_key,
    _random_prompt,
    _record_expected,
    _safe_json,
)


class StatusPoller(HttpUser):
    """Simulates clients polling /generate/check and /generate/status.

    This is the highest-traffic endpoint in production (~10 req/s per client).
    Exercises: wp_has_valid_workers, get_wp_queue_stats, get_request_avg, count_active_workers.
    """

    weight = 5
    fixed_count = 0  # set via --status-pollers in on_test_start
    wait_time = between(0.5, 2)

    def on_start(self):
        self.pending_ids = []
        # Seed request always uses a real requestor key if available
        self.api_key = _pick_requestor_key()
        # `_submit_request` calls `_handle_async_generate`, which raises
        # `RescheduleTask` on 429/expected-403 responses. Locust treats any
        # exception out of `on_start` as a hard error (stack trace to stderr,
        # the user is torn down), but for our seed call, a rate-limit just
        # means "don't seed pending_ids; the first @task will retry". Swallow
        # it explicitly here.
        try:
            self._submit_request()
        except RescheduleTask:
            pass

        opts = self.environment.parsed_options
        if random.random() < opts.anon_chance_poller:
            self.api_key = _config["anonymous_api_key"]
        else:
            self.api_key = _pick_requestor_key()

    def _submit_request(self):
        opts = self.environment.parsed_options
        models = _config.get("models", [])
        request_models = random.sample(models, k=random.randint(0, len(models)))
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler",
            },
            "models": request_models,
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/async [poller-seed]",
        ) as resp:
            req_id = _handle_async_generate(resp, self.environment)
            if req_id:
                self.pending_ids.append(req_id)

    @tag("image", "status")
    @task(8)
    def poll_check(self):
        """Lightweight status check: exercises the 1s-cached DB helpers."""
        if not self.pending_ids:
            self._submit_request()
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/check/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/check/[id]",
        ) as resp:
            if resp.ok:
                data = resp.json()
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                # Request expired or was pruned: normal end-of-life.
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/check/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(1.0, 3.0))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("image", "status")
    @task(2)
    def poll_status(self):
        """Full status: exercises procgen detail retrieval + R2 presigned URLs."""
        if not self.pending_ids:
            self._submit_request()
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/status/[id]",
        ) as resp:
            if resp.ok:
                data = resp.json()
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                # /status/ has its own per-IP limiter ("10 per 1 minute"); back off hard.
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/status/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(6.0, 12.0))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class RequestGenerator(HttpUser):
    """Simulates a real client: submit /generate/async, then aggressively poll
    /generate/check/<id> at ~1-3 Hz until done, escalate to /generate/status/<id>
    on completion.

    The previous implementation fire-and-forgot every request, leaving zero
    /check pressure unless --status-pollers was non-zero. With long simulated
    gen times (typical of production), this meant the check-path was wildly
    under-exercised relative to its real-world load.

    Each instance keeps up to ``--requestor-max-pending`` ids in-flight; once
    full, submission tasks short-circuit and the poll task does all the work.
    Server-side rate limits (`/check` = 10/sec/path, `/status` = 10/min/path)
    are honoured by routing all traffic through `_handle_check_response`.

    Exercises: prompt detection (PromptChecker.__call__), WP creation +
    activate, count_waiting_requests, is_ip_safe countermeasure checks,
    plus the full poll-loop hot path (wp_has_valid_workers, get_wp_queue_stats,
    get_request_avg, count_active_workers).
    """

    weight = 3
    fixed_count = 0  # set via --image-requestors in on_test_start
    # `wait_time` is overridden in on_start so it picks up CLI knobs; kept here
    # so Locust doesn't complain at class-load time.
    wait_time = between(0.2, 1.0)

    def on_start(self):
        opts = self.environment.parsed_options
        rand = random.random()
        if rand < opts.anon_chance_requester:
            self.api_key = _config["anonymous_api_key"]
        elif rand < opts.anon_chance_requester + 0.10:
            self.api_key = _pick_worker_key()
        else:
            self.api_key = _pick_requestor_key()

        models = _config.get("models", [])
        self.request_models = random.sample(models, k=random.randint(0, len(models)))
        self.pending_ids: list[str] = []
        self.max_pending: int = max(1, int(opts.requestor_max_pending))
        # Override wait_time per-instance using the configured min/max so
        # operators can tune the polling cadence without editing source.
        self.wait_time = lambda: random.uniform(opts.requestor_wait_min, opts.requestor_wait_max)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _at_capacity(self) -> bool:
        return len(self.pending_ids) >= self.max_pending

    def _submit(self, name: str, payload: dict) -> None:
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            try:
                req_id = _handle_async_generate(resp, self.environment)
            except RescheduleTask:
                # Bubble the back-off up so Locust skips ahead, but make sure
                # we don't bubble it out of `on_start`-style contexts (only
                # tasks call _submit, so this is safe here).
                raise
            if req_id:
                self.pending_ids.append(req_id)

    def _fetch_status(self, req_id: str) -> None:
        """Mirror real client: on done, fetch /status once for the full payload.

        /status is limited to 10/min/path, but each id is only fetched once
        per completion, so we stay well under the limit.
        """
        with self.client.get(
            f"/api/v2/generate/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/status/[id]",
        ) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/status/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    @tag("image", "status", "requestor")
    @task(30)
    def poll_pending(self):
        """Aggressive /check loop: the dominant traffic source.

        Weight 30 vs the submit tasks' combined weight of 8 means roughly
        ~3.75 polls per submission per user, which combined with up to
        --requestor-max-pending ids per user produces the 10s-of-Hz
        per-id polling that real clients generate.
        """
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/check/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/check/[id]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                resp.success()
                if data.get("done") or data.get("faulted"):
                    # Drop before fetching /status so we don't double-poll if
                    # the next task tick lands on this id.
                    if req_id in self.pending_ids:
                        self.pending_ids.remove(req_id)
                    if data.get("done") and self.environment.parsed_options.status_fetch_on_done:
                        self._fetch_status(req_id)
            elif resp.status_code in (404, 410):
                if req_id in self.pending_ids:
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                # /check is 10/sec/path. If we hit this we're polling a single
                # id far too aggressively. Back off briefly.
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/check/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(0.5, 1.5))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("image", "cold", "requestor")
    @task(5)
    def generate_simple(self):
        """Basic txt2img: exercises prompt detection + WP pipeline."""
        if self._at_capacity():
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler",
            },
            "models": self.request_models,
        }
        self._submit("/api/v2/generate/async [simple]", payload)

    @tag("image", "cold", "requestor")
    @task(2)
    def generate_large(self):
        """High-res request: tests resolution-based cost calculations."""
        if self._at_capacity():
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.large_gen_width,
                "height": opts.large_gen_height,
                "steps": opts.large_gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler_a",
            },
            "models": self.request_models,
        }
        self._submit("/api/v2/generate/async [large]", payload)

    @tag("image", "cold", "requestor")
    @task(1)
    def generate_multi_model(self):
        """Multi-model request: broader candidate evaluation."""
        if self._at_capacity():
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler",
            },
            "models": self.request_models,
        }
        self._submit("/api/v2/generate/async [multi-model]", payload)


class WorkerSimulator(HttpUser):
    """Simulates workers popping and submitting jobs.

    Exercises: get_sorted_wp (the big DB query), candidate evaluation loop,
    start_generation, set_generation (record_contribution, R2, webhook).
    """

    weight = 2
    fixed_count = 0  # set via --image-workers in on_test_start
    wait_time = between(1, 4)

    def create_worker_name(self):
        return f"StressWorker-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

        models = _config.get("models", [])
        self.worker_models = random.sample(models, k=random.randint(1, max(1, len(models))))

        # Check that this worker doesn't already exist (from a previous test run), and choose a new name if so.
        # The endpoint returns 200 + worker JSON if a worker by that name exists,
        # or 404 ("WorkerNotFound") when the name is free, the latter is the
        # success case for *us*, so we treat 404 as "name available".
        for _ in range(10):
            with self.client.get(
                f"/api/v2/workers/name/{self.worker_name}",
                headers=_headers(self.api_key),
                name="/api/v2/workers [check-name]",
                catch_response=True,
            ) as resp:
                if resp.status_code == 404:
                    resp.success()
                    break
                if resp.ok:
                    # Name already in use, pick a different one and retry.
                    resp.success()
                    self.worker_name = self.create_worker_name()
                    continue
                # Any other status: don't loop forever, just proceed.
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                break

    def on_stop(self):
        """Cleanup: delete the worker we created (best-effort)."""
        with self.client.get(
            f"/api/v2/workers/name/{self.worker_name}",
            headers=_headers(self.api_key),
            name="/api/v2/workers [check-name]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                resp.success()
                return
            if not resp.ok:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                return
            resp.success()
            data = _safe_json(resp) or {}
            worker_id = data.get("id") if isinstance(data, dict) else None
            if not worker_id:
                return
            with self.client.delete(
                f"/api/v2/workers/{worker_id}",
                headers=_headers(self.api_key),
                name="/api/v2/workers [delete]",
                catch_response=True,
            ) as del_resp:
                # 423 LOCKED: worker has contributions and can't be deleted, expected after load.
                if del_resp.ok or del_resp.status_code in (404, 410, 423):
                    del_resp.success()
                else:
                    del_resp.failure(f"Status {del_resp.status_code}: {del_resp.text[:200]}")

    @tag("image", "worker")
    @task
    def pop_and_submit(self):
        """Full worker loop: pop a job, then submit a fake result."""
        opts = self.environment.parsed_options
        pop_payload = {
            "name": self.worker_name,
            "models": self.worker_models,
            "bridge_agent": opts.worker_bridge_agent,
            "nsfw": True,
            "amount": 1,
            "max_pixels": opts.worker_max_pixels,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_lora": True,
        }
        with self.client.post(
            "/api/v2/generate/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                # 400 ProfaneWorkerName → unlucky random suffix; rotate name and skip.
                # 403 WorkerMaintenance  → the simulated worker has been disabled by the
                #     server for dropping jobs; rotate name so the next pop creates a
                #     fresh worker rather than hammering the disabled one.
                if resp.status_code in (400, 403) and (
                    _is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)
                ):
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    # Too-many-workers / flagged-account: this user account is saturated,
                    # switch to a *different* worker key so we can still exercise /pop under load.
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    time.sleep(random.uniform(2.0, 6.0))
                    raise RescheduleTask()
                resp.failure(f"Pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            pop_data = body or {}
            job_id = pop_data.get("id", None)
            if not job_id:
                resp.success()
                return
            resp.success()

        time.sleep(random.uniform(opts.sim_gen_time_min, opts.sim_gen_time_max))
        submit_payload = {
            "id": job_id,
            "generation": "R2",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            # 404 = the WP/procgen was pruned while we were "generating".
            # 400 with rc "InvalidJobID" = same root cause, different surface.
            # Both are realistic outcomes after long simulated gen times.
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(2.0, 6.0))
                return
            resp.failure(f"Submit failed: {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Hot/cold image payload helpers
# ---------------------------------------------------------------------------

def _hot_image_payload(opts):
    return {
        "prompt": _HOT_PROMPT,
        "nsfw": False,
        "r2": True,
        "trusted_workers": False,
        "params": {
            "width": opts.gen_width,
            "height": opts.gen_height,
            "steps": opts.gen_steps,
            "cfg_scale": opts.gen_cfg_scale,
            "sampler_name": "k_euler",
        },
        "models": _config.get("models", [])[:1],
    }


def _cold_image_payload(opts):
    models = _config.get("models", [])
    return {
        "prompt": _random_prompt() + f" seed-{random.randint(0, 10**9)}",
        "nsfw": random.random() < 0.1,
        "r2": True,
        "trusted_workers": False,
        "params": {
            "width": random.choice([512, 576, 640, 768]),
            "height": random.choice([512, 576, 640, 768]),
            "steps": random.choice([15, 20, 25, 30]),
            "cfg_scale": round(random.uniform(4.0, 10.0), 1),
            "sampler_name": random.choice(["k_euler", "k_euler_a", "k_dpmpp_2m", "k_heun"]),
        },
        "models": random.sample(models, k=random.randint(0, max(1, len(models)))) if models else [],
    }


# ---------------------------------------------------------------------------
# Hot-path variants for the existing RequestGenerator
# ---------------------------------------------------------------------------

class HotPathRequester(HttpUser):
    """Dedicated hot-path requester: identical payload every call.

    Complements ``RequestGenerator``. Isolating the hot path into its own User
    keeps the stats table readable: every row under this class is a cache hit.
    """

    weight = 1
    fixed_count = 0  # set via --hot-path-requestors in on_test_start
    wait_time = between(1, 3)

    def on_start(self):
        self.api_key = _pick_requestor_key()

    @tag("image", "hot", "requestor")
    @task(5)
    def async_hot(self):
        opts = self.environment.parsed_options
        with self.client.post(
            "/api/v2/generate/async",
            json=_hot_image_payload(opts),
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/async [hot]",
        ) as resp:
            _handle_async_generate(resp, self.environment)

    @tag("image", "cold", "requestor")
    @task(2)
    def async_cold(self):
        opts = self.environment.parsed_options
        with self.client.post(
            "/api/v2/generate/async",
            json=_cold_image_payload(opts),
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/async [cold]",
        ) as resp:
            _handle_async_generate(resp, self.environment)
