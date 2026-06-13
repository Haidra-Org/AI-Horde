# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Text generation requestor and worker Locust users."""

import random
import string
import time
import uuid

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER, _HOT_TEXT_PROMPT, _config
from ..helpers import (
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _pick_requestor_key,
    _pick_worker_key,
    _random_prompt,
    _record_expected,
    _safe_json,
)


def _handle_async_text(resp, environment, name):
    """Common handling for POST /generate/text/async responses."""
    if resp.ok:
        body = _safe_json(resp) or {}
        resp.success()
        return body.get("id")
    body = _safe_json(resp)
    if resp.status_code == 429:
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        time.sleep(random.uniform(2.0, 6.0))
        raise RescheduleTask()
    if resp.status_code == 400 and _is_expected_rc(body, {"KudosUpfront", "SharedKeyInsufficientKudos"}):
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        raise RescheduleTask()
    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
    return None


class TextRequester(HttpUser):
    """Simulates clients submitting text generation requests and polling them.

    Exercises: /generate/text/async (TextAsyncGenerate), /generate/text/status
    (TextAsyncStatus), kobold WP creation, text kudos calculation path.
    """

    weight = 2
    fixed_count = 0  # set via --text-requestors in on_test_start
    wait_time = between(1, 4)

    def on_start(self):
        self.pending_ids: list[str] = []
        self.api_key = _pick_requestor_key()

    def _post_async(self, payload: dict, name: str):
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            req_id = _handle_async_text(resp, self.environment, name)
            if req_id:
                self.pending_ids.append(req_id)

    @tag("text", "hot", "requestor")
    @task(4)
    def text_async_hot(self):
        """Repeated identical text request: exercises hot cache path."""
        payload = {
            "prompt": _HOT_TEXT_PROMPT,
            "params": {"max_length": 80, "max_context_length": 1024, "temperature": 0.7, "top_p": 0.9},
            "models": [],
            "trusted_workers": False,
        }
        self._post_async(payload, "/api/v2/generate/text/async [hot]")

    @tag("text", "cold", "requestor")
    @task(3)
    def text_async_cold(self):
        """Randomized text request: exercises cold/WP-creation path."""
        payload = {
            "prompt": _random_prompt() + " " + uuid.uuid4().hex[:12],
            "params": {
                "max_length": random.choice([40, 80, 128, 200]),
                "max_context_length": random.choice([1024, 1536, 2048]),
                "temperature": round(random.uniform(0.3, 1.2), 2),
                "top_p": round(random.uniform(0.7, 1.0), 2),
                "top_k": random.choice([0, 40, 60]),
            },
            "models": [],
            "trusted_workers": False,
        }
        self._post_async(payload, "/api/v2/generate/text/async [cold]")

    @tag("text", "status", "requestor")
    @task(8)
    def text_status(self):
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/text/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/status/[id]",
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
                _record_expected(self.environment, "GET", "/api/v2/generate/text/status/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class TextWorkerSimulator(HttpUser):
    """Simulates text workers: /generate/text/pop + /generate/text/submit."""

    weight = 1
    fixed_count = 0  # set via --text-workers in on_test_start
    wait_time = between(1, 4)

    def create_worker_name(self):
        return f"StressTextWorker-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

    @tag("text", "worker")
    @task
    def pop_and_submit_text(self):
        opts = self.environment.parsed_options
        pop_payload = {
            "name": self.worker_name,
            "models": _config.get("models", [])[:2] or ["koboldcpp/llama-3"],
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
            name="/api/v2/generate/text/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (
                    _is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)
                ):
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/text/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/text/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    raise RescheduleTask()
                resp.failure(f"Text pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            job_id = data.get("id")
            if not job_id:
                resp.success()
                return
            resp.success()

        time.sleep(random.uniform(opts.sim_gen_time_min / 2, opts.sim_gen_time_max / 2))
        submit_payload = {
            "id": job_id,
            "generation": "Once upon a time there was a stress test that completed successfully.",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/text/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/text/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            resp.failure(f"Text submit failed: {resp.status_code}: {resp.text[:200]}")
