# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Image generation requestor and worker Locust users."""

import random
import string
import time
from collections import deque
from collections.abc import Sequence
from itertools import cycle
from typing import Any

from horde_sdk.generation_parameters.image.constraints import SAMPLER_CONSTRAINTS
from locust import HttpUser, between, tag, task
from locust.clients import ResponseContextManager
from locust.exception import RescheduleTask

from horde.baseline_policy import baseline_violation
from horde.enums import BaselineFeature

from ..config import (
    _EXPECTED_RC_RECOVER,
    _EXTENDED_IMAGE_SAMPLERS,
    _EXTENDED_SAMPLER_SETTING_FIELDS,
    _HOT_PROMPT,
    _MODEL_BASELINES,
    _QR_CODE_EXTRA_TEXTS,
    _config,
)
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
                _record_expected(
                    self.environment, "GET", "/api/v2/generate/check/[id]", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
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
                _record_expected(
                    self.environment, "GET", "/api/v2/generate/status/[id]", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
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
                _record_expected(
                    self.environment, "GET", "/api/v2/generate/status/[id]", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
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
                _record_expected(
                    self.environment, "GET", "/api/v2/generate/check/[id]", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
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

    abstract = True
    bridge_generation = "base"
    supports_extended_samplers = False
    required_models: tuple[str, ...] = ()
    wait_time = between(1, 4)

    def create_worker_name(self):
        return f"StressWorker-{self.bridge_generation}-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

        models = _config.get("models", [])
        self.worker_models = random.sample(models, k=random.randint(1, max(1, len(models))))
        self.worker_models.extend(model for model in self.required_models if model in models and model not in self.worker_models)

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
            "bridge_agent": self.bridge_agent,
            "nsfw": True,
            "amount": 1,
            "max_pixels": opts.worker_max_pixels,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_extended_controlnet": self.supports_extended_samplers,
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
                if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "/api/v2/generate/pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    # Too-many-workers / flagged-account: this user account is saturated,
                    # switch to a *different* worker key so we can still exercise /pop under load.
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "/api/v2/generate/pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    time.sleep(random.uniform(2.0, 6.0))
                    raise RescheduleTask()
                resp.failure(f"Pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            pop_data = body or {}
            job_id = pop_data.get("id", None)
            if not job_id:
                resp.success()
                return
            job_ttl = pop_data.get("ttl")
            # Every accepted assignment must carry its concrete completion contract. Keep this strict
            # under load: bool is an int subclass in Python but is not a meaningful duration on the wire.
            if type(job_ttl) is not int or job_ttl <= 0:
                resp.failure(f"Popped job {job_id} has invalid or missing ttl: {job_ttl!r}")
                return
            incompatibilities = self._extended_payload_features(pop_data.get("payload") or {})
            if not self.supports_extended_samplers and incompatibilities:
                resp.failure(
                    "Pre-17 worker received an incompatible job containing: " + ", ".join(incompatibilities),
                )
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
                _record_expected(
                    self.environment, "POST", "/api/v2/generate/submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(
                    self.environment, "POST", "/api/v2/generate/submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
                time.sleep(random.uniform(2.0, 6.0))
                return
            resp.failure(f"Submit failed: {resp.status_code}: {resp.text[:200]}")

    @staticmethod
    def _extended_payload_features(payload: dict) -> list[str]:
        """Return bridge-17-only features present in a popped job payload."""
        features = sorted(_EXTENDED_SAMPLER_SETTING_FIELDS.intersection(payload))
        sampler_name = payload.get("sampler_name")
        if sampler_name in _EXTENDED_IMAGE_SAMPLERS:
            features.insert(0, f"sampler_name={sampler_name}")
        return features


class LegacyWorkerSimulator(WorkerSimulator):
    """A pre-17 worker, used to verify dispatch excludes extended jobs."""

    weight = 1
    fixed_count = 0
    bridge_generation = "pre17"
    required_models = ("stable_diffusion",)

    def on_start(self):
        self.bridge_agent = self.environment.parsed_options.legacy_worker_bridge_agent
        super().on_start()


class ExtendedWorkerSimulator(WorkerSimulator):
    """A bridge-17+ worker capable of consuming extended sampler jobs."""

    weight = 1
    fixed_count = 0
    bridge_generation = "17plus"
    supports_extended_samplers = True
    required_models = ("stable_diffusion", "Flux.1-Schnell fp8 (Compact)")

    def on_start(self):
        self.bridge_agent = self.environment.parsed_options.worker_bridge_agent
        super().on_start()


_SAMPLER_SETTING_VALUES = {
    "eta": 0.8,
    "s_noise": 1.05,
    "s_churn": 0.1,
    "s_tmin": 0.0,
    "s_tmax": 10.0,
    "order": 3,
}


def _sampler_setting_field(knob: object) -> str:
    knob_name = str(knob)
    return "sampler_order" if knob_name == "order" else f"sampler_{knob_name}"


def _all_settings_for_sampler(sampler_name: str) -> dict:
    """Build one valid value for every optional setting the SDK exposes."""
    constraints = SAMPLER_CONSTRAINTS[sampler_name]
    settings = {_sampler_setting_field(knob): _SAMPLER_SETTING_VALUES[str(knob)] for knob in constraints.numeric_knob_ranges}
    if constraints.solver_type_choices:
        settings["sampler_solver_type"] = str(constraints.solver_type_choices[0])
    settings["scheduler"] = "karras"
    return settings


class SamplerFeatureRequester(HttpUser):
    """Submit extended samplers with zero, some, or all optional settings."""

    weight = 1
    fixed_count = 0
    wait_time = between(1, 2)
    max_pending = 8

    def on_start(self):
        self.api_key = _pick_requestor_key()
        self.pending_ids = deque()
        profiles = [(sampler_name, _all_settings_for_sampler(sampler_name), None) for sampler_name in _EXTENDED_IMAGE_SAMPLERS]
        profiles.append(
            (
                "k_euler",
                {"scheduler": "simple", "flow_shift": 1.1},
                "Flux.1-Schnell fp8 (Compact)",
            ),
        )
        profiles.extend(
            ("uni_pc", {"scheduler": scheduler}, None)
            for scheduler in (
                "normal",
                "simple",
                "sgm_uniform",
                "exponential",
                "ddim_uniform",
                "beta",
                "linear_quadratic",
                "kl_optimal",
            )
        )
        profiles.extend(("k_euler", {"scheduler": scheduler}, "stable_diffusion") for scheduler in ("align_your_steps", "gits"))
        self.feature_cases = cycle(
            (sampler_name, mode, settings, model)
            for sampler_name, settings, model in profiles
            for mode in ("none", "subset", "all")
            if mode != "subset" or len(settings) > 1
        )

    def _cancel_oldest(self):
        if not self.pending_ids:
            return
        req_id = self.pending_ids.popleft()
        with self.client.delete(
            f"/api/v2/generate/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/status/[id] [sampler-feature-cancel]",
        ) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    def on_stop(self):
        while self.pending_ids:
            self._cancel_oldest()

    def _next_case(self) -> tuple[str, str, dict, str | None]:
        sampler_name, mode, all_settings, model = next(self.feature_cases)
        if mode == "none":
            selected_settings = {}
        elif mode == "all":
            selected_settings = all_settings
        else:
            count = random.randint(1, len(all_settings) - 1)
            names = random.sample(list(all_settings), k=count)
            selected_settings = {name: all_settings[name] for name in names}
        return sampler_name, mode, selected_settings, model

    @tag("image", "cold", "requestor", "sampler-features")
    @task
    def generate_sampler_feature(self):
        if len(self.pending_ids) >= self.max_pending:
            self._cancel_oldest()

        opts = self.environment.parsed_options
        configured_models = _config.get("models", [])
        sampler_name, mode, settings, preferred_model = self._next_case()
        while preferred_model and preferred_model not in configured_models:
            sampler_name, mode, settings, preferred_model = self._next_case()
        models = (
            [preferred_model]
            if preferred_model
            else (["stable_diffusion"] if "stable_diffusion" in configured_models else configured_models[:1])
        )
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": 1.5 if sampler_name.endswith("cfg_pp") else opts.gen_cfg_scale,
                "sampler_name": sampler_name,
                **settings,
            },
            "models": models,
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=f"/api/v2/generate/async [sampler-feature/{mode}]",
        ) as resp:
            req_id = _handle_async_generate(resp, self.environment)
            if req_id:
                self.pending_ids.append(req_id)


# The per-baseline features this workload sets, one payload edit each. `control_type` is left out
# because the service requires a source image alongside it, and this workload has no cheap way to
# attach one.
_BASELINE_FEATURES: tuple[BaselineFeature, ...] = (
    BaselineFeature.HIRES_FIX,
    BaselineFeature.TRANSPARENT,
    BaselineFeature.QR_CODE,
    BaselineFeature.FLOW_SHIFT,
    BaselineFeature.REMIX,
)

# Forbidden responses that say nothing about the baseline: the requestor ran out of upfront kudos, or
# the deployment gates who may create workers.
_NON_POLICY_FORBIDDEN_RCS: set[str] = {"KudosUpfront", "WorkerInviteOnly"}


def _apply_baseline_feature(feature: BaselineFeature, payload: dict[str, Any]) -> None:
    """Set one per-baseline feature on an async generate payload.

    Args:
        feature: The feature to set.
        payload: The async generate payload, mutated in place.

    Raises:
        ValueError: The feature is not one this workload knows how to set.
    """
    params = payload["params"]
    if feature == BaselineFeature.HIRES_FIX:
        params["hires_fix"] = True
    elif feature == BaselineFeature.TRANSPARENT:
        params["transparent"] = True
    elif feature == BaselineFeature.QR_CODE:
        params["workflow"] = "qr_code"
        params["extra_texts"] = [dict(extra_text) for extra_text in _QR_CODE_EXTRA_TEXTS]
    elif feature == BaselineFeature.FLOW_SHIFT:
        params["flow_shift"] = 1.1
    elif feature == BaselineFeature.REMIX:
        payload["source_processing"] = "remix"
    else:
        raise ValueError(f"Unhandled baseline feature: {feature}")


class BaselineFeatureRequester(HttpUser):
    """Submit per-baseline features and assert the API accepts or refuses each as the policy says.

    The expectation is computed from the service's own policy table rather than a copy of it, so this
    catches the request path reading a different baseline than the table describes, and it needs no
    edit when a baseline row changes. Models the suite has no baseline for are skipped: guessing one
    would turn an unfamiliar deployment into a failure.
    """

    weight = 1
    fixed_count = 0
    wait_time = between(1, 2)
    max_pending: int = 8

    def on_start(self) -> None:
        self.api_key: str = _pick_requestor_key()
        self.pending_ids: deque[str] = deque()
        self.known_models: list[str] = [model for model in _config.get("models", []) if model in _MODEL_BASELINES]

    def _cancel_oldest(self) -> None:
        if not self.pending_ids:
            return
        req_id = self.pending_ids.popleft()
        with self.client.delete(
            f"/api/v2/generate/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/status/[id] [baseline-feature-cancel]",
        ) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    def on_stop(self) -> None:
        while self.pending_ids:
            self._cancel_oldest()

    def _build_payload(self, model: str, features: Sequence[BaselineFeature]) -> dict[str, Any]:
        opts = self.environment.parsed_options
        payload: dict[str, Any] = {
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
            "models": [model],
        }
        for feature in features:
            _apply_baseline_feature(feature, payload)
        return payload

    def _judge(self, resp: ResponseContextManager, *, expected_rc: str | None, description: str) -> None:
        """Mark the response against the rejection the policy table predicted for it.

        Args:
            resp: The caught response to mark as a success or a failure.
            expected_rc: The return code the policy table predicted, or None where it allows the request.
            description: The baseline and features under test, for the failure message.
        """
        name = resp.request_meta.get("name", "/api/v2/generate/async")
        body = _safe_json(resp)
        response_time_ms = resp.elapsed.total_seconds() * 1000
        response_length = len(resp.content or b"")

        if resp.status_code == 429 or (resp.status_code == 403 and _is_expected_rc(body, _NON_POLICY_FORBIDDEN_RCS)):
            # Refused for a reason the policy table has no say in, so it decides nothing here.
            resp.success()
            _record_expected(self.environment, "POST", name, response_time_ms, response_length)
            time.sleep(min(float(resp.headers.get("Retry-After") or random.uniform(2.0, 6.0)), 10.0))
            raise RescheduleTask()

        actual_rc = (body or {}).get("rc")
        if expected_rc is None:
            if resp.ok:
                resp.success()
                req_id = (body or {}).get("id")
                if req_id:
                    self.pending_ids.append(req_id)
                return
            resp.failure(f"{description} should have been accepted; got {resp.status_code} rc={actual_rc}")
            return

        if resp.status_code == 400 and actual_rc == expected_rc:
            resp.success()
            _record_expected(self.environment, "POST", name, response_time_ms, response_length)
            return
        resp.failure(f"{description} should have been refused with rc {expected_rc}; got {resp.status_code} rc={actual_rc}")

    @tag("image", "cold", "requestor", "baseline-features")
    @task
    def generate_baseline_feature(self) -> None:
        if not self.known_models:
            raise RescheduleTask()
        if len(self.pending_ids) >= self.max_pending:
            self._cancel_oldest()

        model = random.choice(self.known_models)
        baseline = _MODEL_BASELINES[model]
        features = random.sample(_BASELINE_FEATURES, k=random.randint(1, 2))
        payload = self._build_payload(model, features)

        violation = baseline_violation(
            [baseline],
            params=payload["params"],
            source_processing=payload.get("source_processing"),
        )
        expected_rc = violation[0] if violation is not None else None
        description = f"{baseline} with {'+'.join(sorted(features))}"

        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=f"/api/v2/generate/async [baseline-feature/{baseline}]",
        ) as resp:
            self._judge(resp, expected_rc=expected_rc, description=description)


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
