# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistence + backwards-compatibility coverage for advertised annotation types.

An alchemy worker's advertised ``annotation_types`` are stored for display and
moderation only. Pop-time matching stays on the live pop payload (fail-closed),
so these tests assert:

- check_in persists the advertised set and updates it on the next check-in;
- worker details expose the set additively;
- a legacy alchemist (no annotation_types anywhere) keeps working unchanged.
"""

from __future__ import annotations

import pytest

AGENT = "aihorde_ci_client:1.0:(test)ci"


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
    """Flush the (fake) redis caches so a details read reflects committed DB state
    rather than the snapshot taken at check-in time."""
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


def _worker_details_by_name(client, api_key: str, worker_name: str) -> dict:
    _expire_caches()
    resp = client.get(f"/api/v2/workers/name/{worker_name}", headers=_headers(api_key))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _alchemy_pop(client, api_key: str, pop_dict: dict):
    return client.post("/api/v2/interrogate/pop", json=pop_dict, headers=_headers(api_key))


class TestAnnotationTypePersistence:
    def test_check_in_persists_and_updates_the_advertised_set(self, client, api_key):
        worker_name = "AnnotationPersistWorker"

        first = _alchemy_pop(
            client,
            api_key,
            {
                "name": worker_name,
                "forms": ["caption"],
                "annotation_types": ["canny", "depth"],
                "bridge_agent": AGENT,
                "max_tiles": 96,
            },
        )
        assert first.status_code == 200, first.get_data(as_text=True)

        details = _worker_details_by_name(client, api_key, worker_name)
        assert "annotation_types" in details
        assert set(details["annotation_types"]) == {"canny", "depth"}
        # Additive only: the legacy forms key still exists and keeps its shape.
        assert set(details["forms"]) == {"caption"}

        # A subsequent check-in with a different set replaces (not accumulates) it.
        second = _alchemy_pop(
            client,
            api_key,
            {
                "name": worker_name,
                "forms": ["caption"],
                "annotation_types": ["seg"],
                "bridge_agent": AGENT,
                "max_tiles": 96,
            },
        )
        assert second.status_code == 200, second.get_data(as_text=True)

        details = _worker_details_by_name(client, api_key, worker_name)
        assert set(details["annotation_types"]) == {"seg"}

    def test_absurdly_long_advertised_list_is_capped(self, app, client, api_key):
        # The pop endpoint already validates advertised types against the known set, so the
        # internal 100-entry abuse cap is exercised directly on the model here.
        worker_name = "AnnotationCapWorker"
        resp = _alchemy_pop(
            client,
            api_key,
            {"name": worker_name, "forms": ["caption"], "bridge_agent": AGENT, "max_tiles": 96},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.flask import db

        with app.app_context():
            worker = db.session.query(InterrogationWorker).filter_by(name=worker_name).first()
            assert worker is not None
            worker.set_annotation_types([f"type_{i}" for i in range(250)])
            names = worker.get_annotation_type_names()

        assert len(names) == 100


class TestLegacyAlchemistBackwardsCompatibility:
    def test_legacy_alchemist_pops_a_queued_form_and_raises_nothing(self, client, api_key):
        """A legacy alchemist (no annotation_types anywhere, only legacy forms) still receives a
        queued legacy form and its check-in/details flow raises nothing."""
        async_dict = {
            "forms": [
                {"name": "caption"},
            ],
            "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
        }
        async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=_headers(api_key))
        assert async_req.status_code < 400, async_req.get_data(as_text=True)
        req_id = async_req.get_json()["id"]

        worker_name = "LegacyAlchemist"
        try:
            pop_dict = {
                "name": worker_name,
                "forms": ["caption", "strip_background"],
                "bridge_agent": AGENT,
                "max_tiles": 96,
            }
            pop_req = _alchemy_pop(client, api_key, pop_dict)
            assert pop_req.status_code == 200, pop_req.get_data(as_text=True)
            pop_results = pop_req.get_json()
            # The legacy worker still gets the queued caption job with no annotation gating.
            assert pop_results.get("forms"), pop_results
            assert pop_results["forms"][0]["form"] == "caption", pop_results

            # Details flow raises nothing and reports an empty (but present) annotation set.
            details = _worker_details_by_name(client, api_key, worker_name)
            assert details["annotation_types"] == []
            assert "caption" in details["forms"]
        finally:
            client.delete(f"/api/v2/interrogate/status/{req_id}", headers=_headers(api_key))
