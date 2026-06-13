# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validation and defensive-path Locust users."""

import uuid

from locust import HttpUser, between, tag, task

from ..config import _config
from ..helpers import _headers, _pick_requestor_key, _pick_worker_key, _random_prompt, _record_expected


class MisuseUser(HttpUser):
    """Common endpoint misuse. Exercises validation & auth rejection paths."""

    weight = 1
    fixed_count = 0  # set via --misuse-users in on_test_start
    wait_time = between(1, 3)

    def _expect_4xx(self, resp, name: str):
        if 400 <= resp.status_code < 500:
            resp.success()
            _record_expected(
                self.environment,
                resp.request_meta.get("method", "POST"),
                name,
                resp.elapsed.total_seconds() * 1000,
                len(resp.content or b""),
            )
            return
        if resp.ok:
            # The request *succeeded* - unexpected for a misuse probe, but not a bug.
            resp.success()
            return
        resp.failure(f"Server error on misuse probe: {resp.status_code}: {resp.text[:200]}")

    @tag("misuse")
    @task(3)
    def invalid_api_key(self):
        with self.client.get(
            "/api/v2/find_user",
            headers={"apikey": "this-is-not-a-real-key", "Client-Agent": _config.get("client_agent", "stress")},
            catch_response=True,
            name="/api/v2/find_user [misuse-bad-key]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/find_user [misuse-bad-key]")

    @tag("misuse", "image", "status")
    @task(2)
    def status_not_found(self):
        fake = uuid.uuid4().hex
        with self.client.get(
            f"/api/v2/generate/status/{fake}",
            catch_response=True,
            name="/api/v2/generate/status/[id] [misuse-missing]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/status/[id] [misuse-missing]")

    @tag("misuse", "text", "status")
    @task(2)
    def text_status_not_found(self):
        fake = uuid.uuid4().hex
        with self.client.get(
            f"/api/v2/generate/text/status/{fake}",
            catch_response=True,
            name="/api/v2/generate/text/status/[id] [misuse-missing]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/text/status/[id] [misuse-missing]")

    @tag("misuse", "image")
    @task(2)
    def empty_prompt(self):
        payload = {"prompt": "", "params": {"width": 512, "height": 512, "steps": 20}, "models": []}
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/generate/async [misuse-empty-prompt]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/async [misuse-empty-prompt]")

    @tag("misuse", "image")
    @task(2)
    def oversized_image(self):
        payload = {
            "prompt": _random_prompt(),
            "params": {"width": 4096, "height": 4096, "steps": 150, "cfg_scale": 25.0},
            "models": _config.get("models", []),
            "r2": True,
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/generate/async [misuse-oversized]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/async [misuse-oversized]")

    @tag("misuse", "image")
    @task(2)
    def invalid_model_name(self):
        payload = {
            "prompt": _random_prompt(),
            "params": {"width": 512, "height": 512, "steps": 20},
            "models": ["definitely_not_a_real_model_" + uuid.uuid4().hex[:6]],
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/generate/async [misuse-bad-model]",
        ) as resp:
            # The server accepts unknown models (they're just advisory). This exercises
            # the "no valid workers" response branch in the async handler.
            if resp.ok or 400 <= resp.status_code < 500:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("misuse", "image", "worker")
    @task(2)
    def worker_not_found(self):
        fake_name = f"NonexistentWorker-{uuid.uuid4().hex[:8]}"
        with self.client.get(
            f"/api/v2/workers/name/{fake_name}",
            catch_response=True,
            name="/api/v2/workers/name/[name] [misuse-missing]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/workers/name/[name] [misuse-missing]")

    @tag("misuse", "image", "worker")
    @task(1)
    def submit_unknown_job(self):
        with self.client.post(
            "/api/v2/generate/submit",
            json={"id": uuid.uuid4().hex, "generation": "R2", "state": "ok", "seed": 0},
            headers=_headers(_pick_worker_key()),
            catch_response=True,
            name="/api/v2/generate/submit [misuse-bad-id]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/submit [misuse-bad-id]")

    @tag("misuse")
    @task(1)
    def transfer_kudos_to_self(self):
        with self.client.post(
            "/api/v2/kudos/transfer",
            json={"username": "anon#0", "amount": 1},
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/kudos/transfer [misuse]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/kudos/transfer [misuse]")

    @tag("misuse", "image", "worker")
    @task(1)
    def pop_missing_fields(self):
        with self.client.post(
            "/api/v2/generate/pop",
            json={"name": "StressMissingFields"},
            headers=_headers(_pick_worker_key()),
            catch_response=True,
            name="/api/v2/generate/pop [misuse-missing-fields]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/pop [misuse-missing-fields]")
