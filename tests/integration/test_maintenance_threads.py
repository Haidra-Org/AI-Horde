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
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from tests.conftest import _QueryRecorder

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

    from horde.classes.base.worker import WorkerTemplate
    from tests.fixture_types import MakeApiUser

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

AGENT = "aihorde_ci_client:1.0:(test)ci"
TEXT_MODEL = "elinas/chronos-70b-v2"

FEW_SUSPICION_ROWS: int = 3
"""Suspicion rows held by a worker with a handful of reports."""
MANY_SUSPICION_ROWS: int = 1000
"""Suspicion rows held by a heavily reported worker. Large enough that loading them as ORM objects would be visible in
the session, and small enough for a quick insert."""
INTERROGATION_PERFORMANCE_ROWS: int = 2
"""Performance records given to each interrogation worker, so its performance text is computed from a non-empty list."""


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


def _mark_faulted_after_repeated_faults_and_commit(waiting_prompt) -> bool:
    """Apply the repeated-fault limit to ``waiting_prompt`` and commit, as the submit route's fault commit does."""
    from horde.flask import db

    request_faulted = waiting_prompt.mark_faulted_after_repeated_faults()
    db.session.commit()
    return request_faulted


@dataclass(frozen=True)
class _ListedWorker:
    """Represents a worker created for the worker list tests."""

    id: str
    """The worker's ID, in the form used by the single-worker route."""
    name: str
    """The worker's unique name, used as its key in the cached worker lists."""


def _add_suspicion_rows(worker_id: object, row_count: int) -> None:
    """Insert ``row_count`` suspicion rows for the worker in one statement, without creating ORM objects."""
    from sqlalchemy import insert

    from horde.classes.base.worker import WorkerSuspicions
    from horde.flask import db
    from horde.suspicions import Suspicions

    if row_count == 0:
        return
    db.session.execute(
        insert(WorkerSuspicions),
        [
            {
                "worker_id": worker_id,
                "suspicion_id": int(Suspicions.UNREASONABLY_FAST),
            }
            for _ in range(row_count)
        ],
    )
    db.session.commit()


def _create_listed_worker(
    worker_class: type[WorkerTemplate],
    *,
    owner_id: int,
    suspicion_rows: int,
    performance_rows: int = 0,
) -> _ListedWorker:
    """Create an active worker of ``worker_class`` owned by ``owner_id`` with the given suspicion and performance rows.

    Must run inside an app context. The worker checks in at creation, so the worker list includes it.
    """
    from horde.classes.base.worker import WorkerPerformance
    from horde.flask import db

    worker = worker_class(
        name=f"Suspicion Count Worker {uuid.uuid4().hex[:8]}",
        user_id=owner_id,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add_all(WorkerPerformance(worker_id=worker.id, performance=1.5) for _ in range(performance_rows))
    db.session.commit()
    _add_suspicion_rows(worker.id, suspicion_rows)
    return _ListedWorker(id=str(worker.id), name=worker.name)


def _record_worker_list_statements(app: Flask, assert_query_count: QueryCounter) -> _QueryRecorder:
    """Return the statements run to build the public and moderator details of every active worker.

    The loading runs in a fresh app context, as the worker list job does, so no worker is already in the session.
    """
    from horde.database.functions import get_active_workers_for_details

    with app.app_context(), assert_query_count() as queries:
        for worker in get_active_workers_for_details():
            worker.get_details()
            worker.get_details(2)
    return queries


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

    def test_retry_limit_records_fault_when_request_is_faulted(self, client, app, api_key, make_api_user):
        from datetime import datetime

        from horde.classes.kobold.processing_generation import TextProcessingGeneration
        from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
        from horde.classes.kobold.worker import TextWorker
        from horde.database import threads
        from horde.enums import RequestTerminalOutcome
        from horde.flask import db

        request_id = _queue_text_wp(client, api_key)
        worker_user = make_api_user(trusted=True, kudos=100)
        with app.app_context():
            worker = TextWorker(user_id=worker_user.id, name="maintenance-fault-worker")
            db.session.add(worker)
            db.session.commit()
            for _ in range(3):
                procgen = TextProcessingGeneration(
                    wp_id=request_id,
                    worker_id=worker.id,
                    model=TEXT_MODEL,
                )
                procgen.faulted = True
            db.session.commit()

        before_fault = datetime.utcnow()
        threads.check_waiting_prompts()
        after_fault = datetime.utcnow()

        with app.app_context():
            faulted = db.session.query(TextWaitingPrompt).filter_by(id=request_id).one()
            assert faulted.faulted is True
            assert faulted.terminal_outcome == RequestTerminalOutcome.FAULTED.value
            assert before_fault <= faulted.terminal_recorded_at <= after_fault

        response = client.delete(f"/api/v2/generate/text/status/{request_id}", headers=_headers(api_key))
        assert response.status_code == 200
        with app.app_context():
            faulted = db.session.query(TextWaitingPrompt).filter_by(id=request_id).one()
            assert faulted.terminal_outcome == RequestTerminalOutcome.FAULTED.value

    def test_a_request_at_the_faulted_generations_limit_stays_open_and_one_more_fault_faults_it_once(
        self,
        client,
        app,
        api_key,
        make_api_user,
    ):
        """The thread and the submit route apply one limit; the request faults only past it, and only once."""
        from horde.classes.base.waiting_prompt import FAULTED_GENERATIONS_LIMIT
        from horde.classes.kobold.processing_generation import TextProcessingGeneration
        from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
        from horde.classes.kobold.worker import TextWorker
        from horde.database import threads
        from horde.flask import db

        request_id = _queue_text_wp(client, api_key)
        worker_user = make_api_user(trusted=True, kudos=100)
        with app.app_context():
            worker = TextWorker(user_id=worker_user.id, name="maintenance-fault-limit-worker")
            db.session.add(worker)
            db.session.commit()
            worker_id = worker.id
            for _ in range(FAULTED_GENERATIONS_LIMIT):
                procgen = TextProcessingGeneration(wp_id=request_id, worker_id=worker_id, model=TEXT_MODEL)
                procgen.faulted = True
            db.session.commit()

        threads.check_waiting_prompts()

        with app.app_context():
            waiting_prompt = db.session.query(TextWaitingPrompt).filter_by(id=request_id).one()
            assert waiting_prompt.faulted is False
            assert _mark_faulted_after_repeated_faults_and_commit(waiting_prompt) is False
            procgen = TextProcessingGeneration(wp_id=request_id, worker_id=worker_id, model=TEXT_MODEL)
            procgen.faulted = True
            db.session.commit()
            assert _mark_faulted_after_repeated_faults_and_commit(waiting_prompt) is True
            assert _mark_faulted_after_repeated_faults_and_commit(waiting_prompt) is False
            assert db.session.query(TextWaitingPrompt).filter_by(id=request_id).one().faulted is True

        response = client.delete(f"/api/v2/generate/text/status/{request_id}", headers=_headers(api_key))
        assert response.status_code == 200


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

    def test_store_worker_list_matches_per_worker_details_with_bounded_queries(self, client, app, make_api_user):
        """The job serializes an eager-loaded worker set, so the number of SELECTs it issues must not grow with the
        number of active workers, and both caches must still hold exactly what ``get_details(0)`` / ``get_details(2)``
        produce."""
        from datetime import date, datetime

        from sqlalchemy import event

        from horde.classes.base.user import User
        from horde.database import functions
        from horde.database.threads import store_worker_list
        from horde.flask import db

        def _json_serial(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        def _register(worker_user, name):
            resp = client.post(
                "/api/v2/generate/text/pop",
                json={
                    "name": name,
                    "models": [TEXT_MODEL],
                    "bridge_agent": AGENT,
                    "amount": 10,
                    "max_context_length": 4096,
                    "max_length": 512,
                },
                headers=_headers(worker_user.api_key),
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)

        def _run_counting_statements() -> int:
            statements = []

            def _record(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

            with app.app_context():
                engine = db.engine
            event.listen(engine, "before_cursor_execute", _record)
            try:
                store_worker_list()
            finally:
                event.remove(engine, "before_cursor_execute", _record)
            return len(statements)

        # One owner exposes their workers publicly, one does not, so both branches of
        # the owner/messages visibility rule are exercised.
        public_owner = make_api_user(trusted=True, kudos=100)
        private_owner = make_api_user(trusted=True, kudos=100)
        with app.app_context():
            db.session.query(User).filter_by(id=public_owner.id).one().set_public_workers(True)
            db.session.commit()

        _register(public_owner, "Bounded Scribe 1")
        selects_with_one_worker = _run_counting_statements()

        _register(public_owner, "Bounded Scribe 2")
        _register(private_owner, "Bounded Scribe 3")
        _register(private_owner, "Bounded Scribe 4")
        selects_with_four_workers = _run_counting_statements()

        assert selects_with_four_workers <= selects_with_one_worker, (
            f"store_worker_list issued {selects_with_four_workers} SELECTs for 4 workers "
            f"vs {selects_with_one_worker} for 1: per-worker lazy loads are back"
        )

        cached_public = {w["name"]: w for w in json.loads(_redis_get("worker_cache"))}
        cached_privileged = {w["name"]: w for w in json.loads(_redis_get("worker_cache_privileged"))}
        with app.app_context():
            workers = functions.get_active_workers()
            expected_public = {w.name: json.loads(json.dumps(w.get_details(0), default=_json_serial)) for w in workers}
            expected_privileged = {w.name: json.loads(json.dumps(w.get_details(2), default=_json_serial)) for w in workers}

        ours = {f"Bounded Scribe {i}" for i in range(1, 5)}
        assert ours <= set(cached_public) and ours <= set(cached_privileged)
        for name in ours:
            assert cached_public[name] == expected_public[name], name
            assert cached_privileged[name] == expected_privileged[name], name
        # Public/private owners are both present so the owner/messages visibility rule is covered by the comparison.
        assert "owner" in cached_public["Bounded Scribe 1"]
        assert "owner" not in cached_public["Bounded Scribe 3"]

    def test_store_worker_performances_populates_both_caches(self, client):
        from horde.database.threads import store_worker_performances

        store_worker_performances()  # must not raise

        for key in ("worker_performances_avg_cache", "text_worker_performances_avg_cache"):
            cached = _redis_get(key)
            assert cached is not None, f"{key} was not populated"
            float(cached)

    def test_store_available_models_query_count_is_bounded(self, client, api_key, make_api_user):
        """``store_available_models`` scans every queued prompt and every model; the SELECTs it issues must not grow
        with the queue."""
        from sqlalchemy import event

        from horde.database.threads import store_available_models
        from horde.flask import db

        def _run_counting_selects() -> int:
            select_statements = []

            def _record(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    select_statements.append(statement)

            with client.application.app_context():
                engine = db.engine
            event.listen(engine, "before_cursor_execute", _record)
            try:
                store_available_models()
            finally:
                event.remove(engine, "before_cursor_execute", _record)
            return len(select_statements)

        # A registered (idle) worker so the model is listed with a worker count; registering happens via an empty pop.
        worker_user = make_api_user(trusted=True, kudos=100)
        client.post(
            "/api/v2/generate/text/pop",
            json={
                "name": "Models Cache Scribe",
                "models": [TEXT_MODEL],
                "bridge_agent": AGENT,
                "amount": 10,
                "max_context_length": 4096,
                "max_length": 512,
            },
            headers=_headers(worker_user.api_key),
        )

        _queue_text_wp(client, api_key)
        selects_with_one_prompt = _run_counting_selects()

        for _ in range(3):
            _queue_text_wp(client, api_key)
        selects_with_four_prompts = _run_counting_selects()

        assert selects_with_four_prompts <= selects_with_one_prompt, (
            f"store_available_models issued {selects_with_four_prompts} SELECTs for 4 queued prompts "
            f"vs {selects_with_one_prompt} for 1: per-prompt lazy loads are back"
        )

        cached_models = {m["name"]: m for m in json.loads(_redis_get("models_cache"))}
        assert TEXT_MODEL in cached_models
        assert cached_models[TEXT_MODEL]["count"] >= 1
        assert cached_models[TEXT_MODEL]["jobs"] == 4


class TestWorkerListSuspicionCounts:
    """The bulk worker list reports suspicion counts from SQL instead of loading every suspicion row."""

    def test_moderator_worker_list_reports_each_workers_suspicion_row_count(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """The privileged worker list reports, for workers of every type, the number of suspicion rows of each worker."""
        from horde.classes.kobold.worker import TextWorker
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True)
        worker_specs: list[tuple[type[WorkerTemplate], int]] = [
            (ImageWorker, 0),
            (ImageWorker, FEW_SUSPICION_ROWS),
            (ImageWorker, MANY_SUSPICION_ROWS),
            (TextWorker, FEW_SUSPICION_ROWS),
            (InterrogationWorker, 0),
            (InterrogationWorker, MANY_SUSPICION_ROWS),
        ]
        expected_counts: dict[str, int] = {}
        with app.app_context():
            for worker_class, suspicion_rows in worker_specs:
                listed_worker = _create_listed_worker(
                    worker_class,
                    owner_id=owner.id,
                    suspicion_rows=suspicion_rows,
                    performance_rows=INTERROGATION_PERFORMANCE_ROWS if worker_class is InterrogationWorker else 0,
                )
                expected_counts[listed_worker.name] = suspicion_rows

        store_worker_list()

        cached_privileged = {w["name"]: w for w in json.loads(_redis_get("worker_cache_privileged"))}
        reported_counts = {name: cached_privileged[name]["suspicious"] for name in expected_counts}
        assert reported_counts == expected_counts

    def test_single_worker_route_reports_the_suspicion_row_count_to_moderators(
        self,
        app: Flask,
        client: FlaskClient,
        api_key: str,
        make_api_user: MakeApiUser,
    ) -> None:
        """A worker loaded outside the bulk loader still reports its suspicion row count to a moderator."""
        from horde.classes.stable.worker import ImageWorker

        owner = make_api_user(trusted=True)
        with app.app_context():
            listed_worker = _create_listed_worker(ImageWorker, owner_id=owner.id, suspicion_rows=FEW_SUSPICION_ROWS)

        response = client.get(f"/api/v2/workers/{listed_worker.id}", headers=_headers(api_key))

        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()["suspicious"] == FEW_SUSPICION_ROWS

    def test_worker_list_leaves_suspicion_rows_unloaded(self, app: Flask, make_api_user: MakeApiUser) -> None:
        """Building the public and moderator details from the bulk loader does not load any worker's suspicion rows."""
        import sqlalchemy

        from horde.classes.base.worker import WorkerSuspicions
        from horde.classes.kobold.worker import TextWorker
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker
        from horde.database.functions import get_active_workers_for_details
        from horde.flask import db

        owner = make_api_user(trusted=True)
        with app.app_context():
            created_names = {
                _create_listed_worker(worker_class, owner_id=owner.id, suspicion_rows=FEW_SUSPICION_ROWS).name
                for worker_class in (ImageWorker, TextWorker, InterrogationWorker)
            }

        with app.app_context():
            workers = get_active_workers_for_details()
            for worker in workers:
                worker.get_details()
                worker.get_details(2)
            created_workers = [worker for worker in workers if worker.name in created_names]
            loaded_suspicion_rows = [obj for obj in db.session.identity_map.values() if isinstance(obj, WorkerSuspicions)]

            assert len(created_workers) == len(created_names)
            assert all("suspicions" not in sqlalchemy.inspect(worker).dict for worker in created_workers)
            assert loaded_suspicion_rows == []

    def test_worker_list_statement_count_does_not_grow_with_suspicion_rows_or_interrogation_workers(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        """The statements run to build every worker's details stay the same when a worker gains suspicion rows and
        when more interrogation workers check in."""
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker

        owner = make_api_user(trusted=True)
        with app.app_context():
            image_worker = _create_listed_worker(ImageWorker, owner_id=owner.id, suspicion_rows=FEW_SUSPICION_ROWS)
            _create_listed_worker(
                InterrogationWorker,
                owner_id=owner.id,
                suspicion_rows=FEW_SUSPICION_ROWS,
                performance_rows=INTERROGATION_PERFORMANCE_ROWS,
            )
        queries_before = _record_worker_list_statements(app, assert_query_count)

        with app.app_context():
            _add_suspicion_rows(uuid.UUID(image_worker.id), MANY_SUSPICION_ROWS)
            for _ in range(2):
                _create_listed_worker(
                    InterrogationWorker,
                    owner_id=owner.id,
                    suspicion_rows=MANY_SUSPICION_ROWS,
                    performance_rows=INTERROGATION_PERFORMANCE_ROWS,
                )
        queries_after = _record_worker_list_statements(app, assert_query_count)

        assert len(queries_after) == len(queries_before), f"before:\n{queries_before.describe()}\n\nafter:\n{queries_after.describe()}"


class TestAssignMonthlyKudos:
    def test_runs_without_crashing_on_populated_db(self, client, api_key, make_api_user):
        """Smoke: the monthly-kudos sweep must not crash when eligible users
        (moderators, monthly-kudos holders) exist. Exact grant amounts are
        date-gated and covered at the model level, not here."""
        from horde.database.threads import assign_monthly_kudos

        make_api_user(kudos=100)  # ensure at least one extra user row exists
        assign_monthly_kudos()  # must not raise
