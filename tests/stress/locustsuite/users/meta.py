# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only meta/status browsing Locust users."""

import logging
import random

from locust import HttpUser, between, tag, task

from ..helpers import _headers, _pick_requestor_key

logger = logging.getLogger(__name__)


class MetaBrowser(HttpUser):
    """Read-only endpoints consumed by dashboards, the web UI, and clients.

    These are cache-heavy in production. We hit both the hot (same path repeatedly)
    and cold (random id / random model) variants so the response cache is exercised
    while still flushing through the underlying DB helpers sometimes.
    """

    weight = 3
    fixed_count = 0  # set via --meta-browsers in on_test_start
    wait_time = between(1, 3)

    def on_start(self):
        self.api_key = _pick_requestor_key()
        # Prime with a worker id we can fetch details for (cold path). Best-effort.
        self.worker_ids: list[str] = []
        self.user_ids: list[str] = []
        try:
            resp = self.client.get("/api/v2/workers?type=image", headers=_headers(self.api_key), name="/api/v2/workers [bootstrap]")
            if resp.ok:
                data = resp.json() or []
                self.worker_ids = [w.get("id") for w in data[:20] if w.get("id")]
            resp = self.client.get("/api/v2/users", headers=_headers(self.api_key), name="/api/v2/users [bootstrap]")
            if resp.ok:
                data = resp.json() or []
                self.user_ids = [str(u.get("id")) for u in data[:20] if u.get("id") is not None]
        except Exception as err:
            logger.debug("MetaBrowser bootstrap skipped: %s", err)

    @tag("meta")
    @task(5)
    def heartbeat(self):
        self.client.get("/api/v2/status/heartbeat", name="/api/v2/status/heartbeat [hot]")

    @tag("meta")
    @task(3)
    def models(self):
        # /status/models is @cache.cached, this is the canonical hot path.
        self.client.get("/api/v2/status/models?type=image", name="/api/v2/status/models [hot]")

    @tag("meta")
    @task(1)
    def models_cold(self):
        # Vary the query string to bypass the response cache.
        variant = random.choice(["?type=text", "?type=image&min_count=1", "?model_state=known"])
        self.client.get(f"/api/v2/status/models{variant}", name="/api/v2/status/models [cold]")

    @tag("meta")
    @task(2)
    def performance(self):
        self.client.get("/api/v2/status/performance", name="/api/v2/status/performance")

    @tag("meta")
    @task(1)
    def horde_modes(self):
        self.client.get("/api/v2/status/modes", name="/api/v2/status/modes")

    @tag("meta")
    @task(1)
    def news(self):
        self.client.get("/api/v2/status/news", name="/api/v2/status/news")

    @tag("meta")
    @task(2)
    def workers_list(self):
        self.client.get("/api/v2/workers?type=image", name="/api/v2/workers [list]")

    @tag("meta")
    @task(2)
    def teams_list(self):
        self.client.get("/api/v2/teams", name="/api/v2/teams [list]")

    @tag("meta")
    @task(1)
    def worker_single(self):
        if not self.worker_ids:
            return
        wid = random.choice(self.worker_ids)
        with self.client.get(f"/api/v2/workers/{wid}", name="/api/v2/workers/[id]", catch_response=True) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("meta")
    @task(1)
    def user_single(self):
        if not self.user_ids:
            return
        uid = random.choice(self.user_ids)
        with self.client.get(f"/api/v2/users/{uid}", name="/api/v2/users/[id]", catch_response=True) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("meta")
    @task(2)
    def find_user_self(self):
        """Hot path: identity lookup with a valid key."""
        with self.client.get(
            "/api/v2/find_user", headers=_headers(self.api_key), name="/api/v2/find_user [hot]", catch_response=True
        ) as resp:
            if resp.ok or resp.status_code in (401,):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("meta")
    @task(2)
    def stats_img_totals(self):
        self.client.get("/api/v2/stats/img/totals", name="/api/v2/stats/img/totals")

    @tag("meta")
    @task(1)
    def stats_img_models(self):
        self.client.get("/api/v2/stats/img/models", name="/api/v2/stats/img/models")

    @tag("meta")
    @task(1)
    def stats_text_totals(self):
        self.client.get("/api/v2/stats/text/totals", name="/api/v2/stats/text/totals")
