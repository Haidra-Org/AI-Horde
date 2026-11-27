# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only meta/status browsing Locust users."""

import logging
import random

from locust import HttpUser, between, tag, task

from ..helpers import _accept_rate_limit, _headers, _pick_requestor_key, _pick_worker_key

logger = logging.getLogger(__name__)

WORKER_PAGE_SIZE = 25
"""Page size requested by a front end that shows the worker list one screen at a time."""

WORKER_WALK_PAGE_SIZE = 100
"""Page size of a full walk, the largest the endpoint allows.

A walk sends its pages back to back, and the paged list allows 90 requests a minute per address. At 25 rows a page, a
walk of a few thousand workers uses that whole allowance by itself.
"""

MAX_WALK_PAGES = 400
"""The largest number of pages in one walk, which is 40,000 workers at the walk page size above.

A walk that needs more pages is logged as a walk that does not end.
"""


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
        self.worker_owner_key = _pick_worker_key()
        # Prime with a worker id we can fetch details for (cold path). Best-effort.
        self.worker_ids: list[str] = []
        self.worker_names: list[str] = []
        self.user_ids: list[str] = []
        try:
            resp = self.client.get("/api/v2/workers?type=image", headers=_headers(self.api_key), name="/api/v2/workers [bootstrap]")
            if resp.ok:
                data = resp.json() or []
                self.worker_ids = [w.get("id") for w in data[:20] if w.get("id")]
                self.worker_names = [w.get("name") for w in data[:20] if w.get("name")]
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

    @tag("meta", "image")
    @task(2)
    def sampler_constraints(self):
        """Published client contract for sampler settings and pricing."""
        self.client.get(
            "/api/v2/status/sampler_constraints",
            headers=_headers(self.api_key),
            name="/api/v2/status/sampler_constraints [hot]",
        )

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
    @task(3)
    def workers_page_first(self):
        """Hot path: the first screen of the worker list, as a front end loads it on every visit."""
        name = "/api/v2/workers/page [first]"
        with self.client.get(
            f"/api/v2/workers/page?type=image&limit={WORKER_PAGE_SIZE}",
            name=name,
            catch_response=True,
        ) as resp:
            self._expect_worker_list_page(resp, name)

    def _expect_worker_list_page(self, resp, name: str) -> None:
        """Pass a 200 and record a 429 as rate limited. Fail any other status.

        The failure message includes ``Status <code>`` so the smoke checker sorts it by status. Without it, a refusal
        would be counted as a crash.
        """
        if _accept_rate_limit(resp, self.environment, name):
            return
        if resp.ok:
            resp.success()
            return
        resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("meta")
    @task(1)
    def workers_page_walk(self):
        """Walk the whole list by name.

        Under the name order, only a rename moves a worker, so a repeated worker means the cursor is broken. Under a
        count order, a worker whose count changes during the walk can be listed twice. Fail the request if a page
        repeats a worker or is empty while it has a cursor. Log a warning if the walk does not end within
        ``MAX_WALK_PAGES`` pages. A 429 ends the walk without a failure.
        """
        name = "/api/v2/workers/page [walk]"
        seen_ids: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_WALK_PAGES):
            params = {"limit": WORKER_WALK_PAGE_SIZE, "sort": "name"}
            if cursor is not None:
                params["cursor"] = cursor
            with self.client.get(
                "/api/v2/workers/page",
                params=params,
                name=name,
                catch_response=True,
            ) as resp:
                if _accept_rate_limit(resp, self.environment, name):
                    return
                if not resp.ok:
                    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                    return
                body = resp.json()
                page_ids = [row["id"] for row in body["workers"]]
                repeated = seen_ids.intersection(page_ids)
                if repeated:
                    resp.failure(f"Walk listed {len(repeated)} worker(s) twice")
                    return
                seen_ids.update(page_ids)
                cursor = body["next_cursor"]
                if cursor is not None and not page_ids:
                    resp.failure("Empty page with a cursor to follow")
                    return
                resp.success()
            if cursor is None:
                return
        logger.warning("Worker list walk did not end within %d pages", MAX_WALK_PAGES)

    @tag("meta")
    @task(1)
    def workers_page_search(self):
        """Search the worker list by part of a name, as a person who remembers only part of the name does."""
        if not self.worker_names:
            return
        name = random.choice(self.worker_names)
        start = random.randrange(max(len(name) - 3, 1))
        request_name = "/api/v2/workers/page [name search]"
        with self.client.get(
            "/api/v2/workers/page",
            params={"name_contains": name[start : start + 4], "limit": WORKER_PAGE_SIZE},
            name=request_name,
            catch_response=True,
        ) as resp:
            self._expect_worker_list_page(resp, request_name)

    @tag("meta")
    @task(1)
    def workers_mine(self):
        """Read the first page of the caller's workers, as an owner's dashboard does when it polls.

        Fail the request on any status other than 200 or 429. The worker keys of the stress setup are valid accounts.
        """
        name = "/api/v2/workers/mine [first]"
        with self.client.get(
            f"/api/v2/workers/mine?limit={WORKER_PAGE_SIZE}",
            headers=_headers(self.worker_owner_key),
            name=name,
            catch_response=True,
        ) as resp:
            self._expect_worker_list_page(resp, name)

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
