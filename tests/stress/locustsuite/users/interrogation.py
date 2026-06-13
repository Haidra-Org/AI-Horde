# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Interrogation requestor and worker Locust users."""

import random
import string
import time

from locust import HttpUser, between, tag, task
from locust.exception import RescheduleTask

from ..config import _EXPECTED_RC_RECOVER, _INTERROGATION_FORMS, _TINY_PNG_B64
from ..helpers import (
    _headers,
    _is_expected_rc,
    _is_too_many_workers,
    _pick_requestor_key,
    _pick_worker_key,
    _record_expected,
    _safe_json,
)


class InterrogationRequester(HttpUser):
    """Submits /interrogate/async requests with a tiny 1x1 source image."""

    weight = 2
    fixed_count = 0  # set via --interrogate-requestors in on_test_start
    wait_time = between(2, 6)

    def on_start(self):
        self.pending_ids: list[str] = []
        self.api_key = _pick_requestor_key()

    @tag("interrogation", "hot", "requestor")
    @task(3)
    def interrogate_hot(self):
        payload = {
            "source_image": _TINY_PNG_B64,
            "forms": [{"name": "caption"}],
            "trusted_workers": False,
            "slow_workers": True,
        }
        self._post(payload, "/api/v2/interrogate/async [hot]")

    @tag("interrogation", "cold", "requestor")
    @task(2)
    def interrogate_cold(self):
        forms = random.sample(_INTERROGATION_FORMS, k=random.randint(1, len(_INTERROGATION_FORMS)))
        payload = {
            "source_image": _TINY_PNG_B64,
            "forms": [{"name": f} for f in forms],
            "trusted_workers": False,
            "slow_workers": True,
        }
        self._post(payload, "/api/v2/interrogate/async [cold]")

    def _post(self, payload: dict, name: str):
        with self.client.post(
            "/api/v2/interrogate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                rid = data.get("id")
                if rid:
                    self.pending_ids.append(rid)
                resp.success()
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("interrogation", "status", "requestor")
    @task(6)
    def interrogate_status(self):
        if not self.pending_ids:
            return
        rid = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/interrogate/status/{rid}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/interrogate/status/[id]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                if data.get("state") in ("done", "faulted", "cancelled"):
                    self.pending_ids.remove(rid)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(rid)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class InterrogationWorkerSimulator(HttpUser):
    """Simulates an alchemy worker: /interrogate/pop + /interrogate/submit."""

    weight = 1
    fixed_count = 0  # set via --interrogate-workers in on_test_start
    wait_time = between(2, 5)

    def create_worker_name(self):
        return f"StressAlchemist-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

    @tag("interrogation", "worker")
    @task
    def pop_and_submit_interrogation(self):
        pop_payload = {
            "name": self.worker_name,
            "forms": _INTERROGATION_FORMS,
            "amount": 1,
            "bridge_agent": "AI Horde Worker Alchemist:stress:https://github.com/Haidra-Org",
            "threads": 1,
            "max_tiles": 16,
        }
        with self.client.post(
            "/api/v2/interrogate/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/interrogate/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                    resp.success()
                    _record_expected(
                        self.environment, "POST", "/api/v2/interrogate/pop", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                    )
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    raise RescheduleTask()
                resp.failure(f"Interrogate pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            forms = data.get("forms") or []
            resp.success()
            if not forms:
                return
            form_id = forms[0].get("id")
            form_name = forms[0].get("form") or "caption"

        if not form_id:
            return
        time.sleep(random.uniform(1.0, 3.0))
        submit_payload = {
            "id": form_id,
            "result": {form_name: "stress-test-result"},
            "state": "ok",
        }
        with self.client.post(
            "/api/v2/interrogate/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/interrogate/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(
                    self.environment, "POST", "/api/v2/interrogate/submit", resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
                )
                return
            if resp.status_code == 429:
                resp.success()
                return
            resp.failure(f"Interrogate submit failed: {resp.status_code}: {resp.text[:200]}")
