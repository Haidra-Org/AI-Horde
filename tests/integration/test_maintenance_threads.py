# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coverage for the periodic maintenance routines in ``horde.database.threads``.

These run on background timers in production and are otherwise untested. A crash
or incorrect prune here is silent (stale caches, leaked rows, or wrongly deleted
work), so the tests exercise them directly against a seeded DB: the prune routine
must delete only what is expired, and the cache-builders must run without raising
and populate the documented redis keys with the live state.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

AGENT = "aihorde_ci_client:1.0:(test)ci"
TEXT_MODEL = "elinas/chronos-70b-v2"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _queue_text_wp(client, api_key: str) -> str:
    resp = client.post(
        "/api/v2/generate/text/async",
        json={
            "prompt": "maintenance probe",
            "trusted_workers": True,
            "validated_backends": False,
            "max_length": 80,
            "max_context_length": 1024,
            "models": [TEXT_MODEL],
        },
        headers=_headers(api_key),
    )
    assert resp.status_code < 400, resp.get_data(as_text=True)
    return resp.get_json()["id"]


def _redis_get(key: str):
    from horde import horde_redis as horde_redis_module

    return horde_redis_module.horde_redis.horde_r.get(key)


def _refresh_text_queue_position_cache() -> None:
    from horde.database import functions

    functions._wp_queue_positions_cache["text"] = {}
    functions._wp_queue_positions_time["text"] = 0


class TestCheckWaitingPrompts:
    def test_prunes_expired_keeps_fresh(self, client, app, api_key, monkeypatch):
        from datetime import datetime, timedelta

        from horde import metrics
        from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
        from horde.database import threads
        from horde.flask import db

        outcomes = Mock()
        expiry_times = Mock()
        stall_validations = []
        monkeypatch.setattr(threads, "request_outcomes", outcomes)
        monkeypatch.setattr(threads, "request_time_to_expiry", expiry_times)
        monkeypatch.setattr(
            metrics,
            "record_request_stall_validation",
            lambda **kwargs: stall_validations.append(kwargs),
        )

        expired_id = _queue_text_wp(client, api_key)
        fresh_id = _queue_text_wp(client, api_key)
        threads.store_prioritized_wp_queue()
        _refresh_text_queue_position_cache()
        forecast_response = client.get(f"/api/v2/generate/text/status/{expired_id}", headers=_headers(api_key))
        assert forecast_response.status_code == 200

        # Age the first prompt past its expiry.
        with app.app_context():
            expired = db.session.query(TextWaitingPrompt).filter_by(id=expired_id).one()
            expired.expiry = datetime.utcnow() - timedelta(hours=1)
            db.session.commit()

        threads.check_waiting_prompts()
        from horde.request_scheduling import wait_for_scheduling_forecast_events

        assert wait_for_scheduling_forecast_events()

        with app.app_context():
            assert db.session.query(TextWaitingPrompt).filter_by(id=expired_id).first() is None, "expired WP was not pruned"
            assert db.session.query(TextWaitingPrompt).filter_by(id=fresh_id).first() is not None, "fresh WP was wrongly pruned"

        expected_attributes = {"horde.gentype": "text", "horde.outcome": "expired_unstarted"}
        expiry_times.record.assert_called_once()
        assert expiry_times.record.call_args.args[1] == expected_attributes
        outcomes.add.assert_called_once_with(1, expected_attributes)
        assert len(stall_validations) == 1
        assert stall_validations[0]["expired_without_start"] is True

    def test_cancelled_request_is_not_recorded_as_expired(self, client, app, api_key, monkeypatch):
        from datetime import datetime, timedelta

        from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
        from horde.database import threads
        from horde.enums import RequestTerminalOutcome
        from horde.flask import db

        request_id = _queue_text_wp(client, api_key)
        response = client.delete(f"/api/v2/generate/text/status/{request_id}", headers=_headers(api_key))
        assert response.status_code == 200

        with app.app_context():
            cancelled = db.session.query(TextWaitingPrompt).filter_by(id=request_id).one()
            assert cancelled.terminal_outcome == RequestTerminalOutcome.CANCELLED.value
            cancelled.expiry = datetime.utcnow() - timedelta(hours=1)
            db.session.commit()

        outcomes = Mock()
        expiry_times = Mock()
        monkeypatch.setattr(threads, "request_outcomes", outcomes)
        monkeypatch.setattr(threads, "request_time_to_expiry", expiry_times)

        threads.check_waiting_prompts()

        outcomes.add.assert_not_called()
        expiry_times.record.assert_not_called()


class TestCacheBuilders:
    def test_store_prioritized_wp_queue_populates_cache(self, client, api_key):
        from horde.database.threads import store_prioritized_wp_queue

        request_id = _queue_text_wp(client, api_key)
        store_prioritized_wp_queue()  # must not raise

        cached = _redis_get("text_wp_cache")
        assert cached is not None, "text_wp_cache was not populated"
        parsed = json.loads(cached)
        assert isinstance(parsed, list)
        # The queued prompt should appear in the prioritized cache.
        assert len(parsed) >= 1
        assert all("id" in entry and "things" in entry for entry in parsed)

        positions = json.loads(_redis_get("text_wp_queue_positions"))
        queue_index, queued_tokens, _queued_jobs = positions[request_id]
        previous_tokens = next(
            (position[1] for position in positions.values() if position[0] == queue_index - 1),
            0,
        )
        assert queued_tokens - previous_tokens == 80

        _refresh_text_queue_position_cache()
        status_response = client.get(f"/api/v2/generate/text/status/{request_id}", headers=_headers(api_key))
        assert status_response.status_code == 200
        assert status_response.get_json()["wait_time"] == round(queued_tokens)
        from horde import horde_redis as horde_redis_module
        from horde.request_scheduling import wait_for_scheduling_forecast_events

        assert wait_for_scheduling_forecast_events()
        forecast = horde_redis_module.horde_redis.horde_r.hget(
            f"request_scheduling_forecast:{request_id}",
            "start_forecast",
        )
        assert forecast is not None

    def test_forecast_failure_does_not_block_status_response(self, client, api_key, monkeypatch):
        from horde.classes.base import waiting_prompt
        from horde.database import threads

        request_id = _queue_text_wp(client, api_key)
        threads.store_prioritized_wp_queue()
        _refresh_text_queue_position_cache()
        monkeypatch.setattr(
            waiting_prompt,
            "store_scheduling_forecast",
            Mock(side_effect=RuntimeError("forecast backend unavailable")),
        )

        response = client.get(f"/api/v2/generate/text/status/{request_id}", headers=_headers(api_key))

        assert response.status_code == 200
        assert response.get_json()["waiting"] == 1

    def test_store_worker_list_reflects_active_worker(self, client, make_api_user):
        from horde.database.threads import store_worker_list

        worker_user = make_api_user(trusted=True, kudos=100)
        client.post(
            "/api/v2/generate/text/pop",
            json={
                "name": "Thread Cache Scribe",
                "models": [TEXT_MODEL],
                "bridge_agent": AGENT,
                "amount": 10,
                "max_context_length": 4096,
                "max_length": 512,
            },
            headers=_headers(worker_user.api_key),
        )

        store_worker_list()  # must not raise

        cached = _redis_get("worker_cache")
        assert cached is not None, "worker_cache was not populated"
        names = [w.get("name") for w in json.loads(cached)]
        assert "Thread Cache Scribe" in names


class TestAssignMonthlyKudos:
    def test_runs_without_crashing_on_populated_db(self, client, api_key, make_api_user):
        """Smoke: the monthly-kudos sweep must not crash when eligible users
        (moderators, monthly-kudos holders) exist. Exact grant amounts are
        date-gated and covered at the model level, not here."""
        from horde.database.threads import assign_monthly_kudos

        make_api_user(kudos=100)  # ensure at least one extra user row exists
        assign_monthly_kudos()  # must not raise
