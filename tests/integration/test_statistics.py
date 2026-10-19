# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Semantic coverage for the statistics / status endpoints.

These are high-traffic, read-only aggregation endpoints. The tests assert the
aggregation *contract* (documented period buckets, field types, non-negativity,
input validation) and one genuinely state-coupled invariant: the live worker
count reflects a worker that has actually checked in. Exact historical counts
depend on background compile threads not running in-process, so those are not
asserted.
"""

from __future__ import annotations

import pytest

AGENT = "aihorde_ci_client:1.0:(test)ci"
TEXT_MODEL = "elinas/chronos-70b-v2"
PERIODS = {"minute", "hour", "day", "month", "total"}


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _expire_caches() -> None:
    from horde import horde_redis as horde_redis_module

    redis_conn = horde_redis_module.horde_redis
    seen: dict[int, object] = {}
    for client in [redis_conn.horde_r, redis_conn.horde_local_r, *redis_conn.all_horde_redis]:
        if client is not None:
            seen[id(client)] = client
    for client in seen.values():
        try:
            client.flushdb()
        except Exception:
            continue


class TestImageStatsTotals:
    def test_totals_expose_all_period_buckets(self, client):
        resp = client.get("/api/v2/stats/img/totals")
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body) == PERIODS
        for period in PERIODS:
            assert set(body[period]) == {"images", "ps"}
            assert isinstance(body[period]["images"], int) and body[period]["images"] >= 0
            assert isinstance(body[period]["ps"], int) and body[period]["ps"] >= 0


class TestImageStatsModels:
    def test_invalid_model_state_rejected(self, client):
        resp = client.get("/api/v2/stats/img/models?model_state=bogus")
        assert resp.status_code == 400

    @pytest.mark.parametrize("model_state", ["known", "custom", "all"])
    def test_valid_model_states_accepted(self, client, model_state):
        resp = client.get(f"/api/v2/stats/img/models?model_state={model_state}")
        assert resp.status_code == 200


class TestTextStatsTotals:
    def test_totals_expose_request_and_token_buckets(self, client):
        resp = client.get("/api/v2/stats/text/totals")
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body) == PERIODS
        for period in PERIODS:
            assert set(body[period]) == {"requests", "tokens"}
            assert body[period]["requests"] >= 0
            assert body[period]["tokens"] >= 0


class TestHordePerformance:
    def test_performance_reports_coherent_nonnegative_counts(self, client):
        resp = client.get("/api/v2/status/performance")
        assert resp.status_code == 200
        body = resp.get_json()
        for key in (
            "queued_requests",
            "queued_text_requests",
            "worker_count",
            "thread_count",
            "text_worker_count",
            "interrogator_count",
        ):
            assert key in body, key
            assert isinstance(body[key], int)
            assert body[key] >= 0

    def test_text_worker_count_reflects_a_checked_in_worker(self, client, make_api_user):
        worker_user = make_api_user(trusted=True, kudos=100)
        pop = client.post(
            "/api/v2/generate/text/pop",
            json={
                "name": "Stats Scribe",
                "models": [TEXT_MODEL],
                "bridge_agent": AGENT,
                "amount": 10,
                "max_context_length": 4096,
                "max_length": 512,
            },
            headers=_headers(worker_user.api_key),
        )
        assert pop.status_code == 200, pop.get_data(as_text=True)

        # Flush the 300s count cache so the endpoint recomputes from live DB state.
        _expire_caches()
        body = client.get("/api/v2/status/performance").get_json()
        assert body["text_worker_count"] >= 1


class TestHordeModes:
    def test_modes_returns_boolean_flags(self, client):
        resp = client.get("/api/v2/status/modes")
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body["maintenance_mode"], bool)
        assert isinstance(body["invite_only_mode"], bool)


class TestModelsList:
    def test_models_endpoint_returns_a_list(self, client):
        resp = client.get("/api/v2/status/models")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_text_models_carry_the_chat_declarations_only_when_asked_for(self, client, request_headers):
        import time
        import uuid

        from horde.database.threads import store_available_models
        from tests.integration._fake_chat_worker import FakeChatWorker

        suffix = uuid.uuid4().hex[:8]
        chat_model = f"custom/status-chat-{suffix}"
        text_only_model = f"custom/status-text-{suffix}"
        before_registration = int(time.time()) - 1
        declarations = (
            (["seed"], ["tool_choice_enforced"]),
            (["presence_penalty", "seed"], []),
        )
        for worker_index, (chat_features, chat_guarantees) in enumerate(declarations):
            FakeChatWorker(
                client=client,
                headers=request_headers,
                name=f"CICD Status Chat Scribe {worker_index} {suffix}",
                models=[chat_model],
                pop_options={
                    "chat_features": chat_features,
                    "chat_guarantees": chat_guarantees,
                    "chat_max_context_length": 4096 * (worker_index + 1),
                },
            ).pop()
        FakeChatWorker(client=client, headers=request_headers, name=f"CICD Status Text Scribe {suffix}", models=[text_only_model]).pop(
            chat_jobs=None,
        )
        store_available_models()
        query = "/api/v2/status/models?type=text&model_state=all&min_count=0&max_count=1000"

        plain_resp = client.get(query)
        chat_resp = client.get(f"{query}&chat=true")

        assert plain_resp.status_code == 200, plain_resp.get_data(as_text=True)
        plain_entries = {entry["name"]: entry for entry in plain_resp.get_json()}
        assert {chat_model, text_only_model} <= plain_entries.keys()
        assert all("chat" not in entry for entry in plain_entries.values())

        assert chat_resp.status_code == 200, chat_resp.get_data(as_text=True)
        chat_entries = {entry["name"]: entry for entry in chat_resp.get_json()}
        assert text_only_model not in chat_entries
        chat_entry = chat_entries[chat_model]
        assert chat_entry["count"] == plain_entries[chat_model]["count"]
        assert chat_entry["jobs"] == plain_entries[chat_model]["jobs"]
        assert chat_entry["chat"]["workers"] == 2
        assert chat_entry["chat"]["max_context_length"] == 8192
        assert chat_entry["chat"]["max_length"] == 1024
        # Guarantees are declared in the same list as features, so the union carries both.
        assert chat_entry["chat"]["features"] == ["presence_penalty", "seed", "tool_choice_enforced"]
        assert chat_entry["chat"]["created"] >= before_registration

    def test_a_model_served_only_by_a_chat_only_worker_counts_no_text_threads(self, client, request_headers):
        """A chat-only worker's threads are left out of the text count, and its model is still listed for chat."""
        import uuid

        from horde.database.threads import store_available_models
        from tests.integration._fake_chat_worker import FakeChatWorker

        suffix = uuid.uuid4().hex[:8]
        chat_only_model = f"custom/status-chat-only-{suffix}"
        FakeChatWorker(
            client=client,
            headers=request_headers,
            name=f"CICD Status Chat Only Scribe {suffix}",
            models=[chat_only_model],
        ).pop(prompt_jobs=False)
        store_available_models()
        # The route caches each query string for two seconds, so this query differs from the other tests' query.
        query = "/api/v2/status/models?type=text&model_state=all&min_count=0&max_count=999"

        plain_resp = client.get(query)
        chat_resp = client.get(f"{query}&chat=true")

        assert plain_resp.status_code == 200, plain_resp.get_data(as_text=True)
        plain_entries = {entry["name"]: entry for entry in plain_resp.get_json()}
        assert plain_entries[chat_only_model]["count"] == 0
        assert chat_resp.status_code == 200, chat_resp.get_data(as_text=True)
        chat_entries = {entry["name"]: entry for entry in chat_resp.get_json()}
        assert chat_entries[chat_only_model]["count"] == 0
        assert chat_entries[chat_only_model]["chat"]["workers"] == 1

    def test_the_chat_parameter_is_refused_for_image_models(self, client):
        resp = client.get("/api/v2/status/models?type=image&chat=true")
        assert resp.status_code == 400
