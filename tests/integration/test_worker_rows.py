# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behaviour of the worker row store in ``horde.worker_rows``.

The background jobs write, for every registered worker, the JSON bytes of its details row in each view. These tests
register workers through the API, run the jobs directly and assert on the rows the store holds.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import pytest

from tests.conftest import _QueryRecorder

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

    from tests.fixture_types import ApiUser, MakeApiUser

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
"""The client agent sent with every request."""
TEXT_MODEL: str = "elinas/chronos-70b-v2"
"""A text model advertised by registered text workers."""
IMAGE_MODELS: list[str] = [
    "Fustercluck",
    "AlbedoBase XL (SDXL)",
]
"""Image models advertised by registered image workers, known to the test model reference."""
INTERROGATION_FORMS: list[str] = [
    "caption",
    "interrogation",
]
"""Forms advertised by registered interrogation workers."""
STALE_CHECK_IN_AGE: timedelta = timedelta(hours=1)
"""How long ago an inactive worker last checked in. Well past the five minutes after which a worker is inactive."""
SMALL_ROLLING_CHUNK: int = 2
"""A rolling chunk small enough that a handful of workers needs several runs and a wrap."""
LARGE_ROLLING_CHUNK: int = 10_000
"""A rolling chunk larger than every worker registered by this module, so one run covers them all."""
PROMOTION_THRESHOLD: int = 100
"""The evaluating kudos above which the applier promotes a user to trusted in the promotion test."""
WORKER_KUDOS_AWARD: int = 25
"""The kudos credited to each worker in the applier test."""


class WorkerKind(StrEnum):
    """Represents the worker types that register through their own pop route."""

    IMAGE = "image"
    TEXT = "text"
    INTERROGATION = "interrogation"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Client-Agent": AGENT}
    if api_key is not None:
        headers["apikey"] = api_key
    return headers


def _register_worker(client: FlaskClient, app: Flask, owner: ApiUser, kind: WorkerKind = WorkerKind.TEXT) -> str:
    """Register a worker of ``kind`` owned by ``owner`` through its pop route and return the worker's ID."""
    from horde.database.functions import find_worker_id_by_name

    worker_name = f"Row Store Worker {uuid.uuid4().hex[:8]}"
    if kind is WorkerKind.IMAGE:
        pop_route = "/api/v2/generate/pop"
        pop_body: dict[str, object] = {
            "name": worker_name,
            "models": IMAGE_MODELS,
            "bridge_agent": AGENT,
            "amount": 10,
            "max_pixels": 1048576,
        }
    elif kind is WorkerKind.INTERROGATION:
        pop_route = "/api/v2/interrogate/pop"
        pop_body = {
            "name": worker_name,
            "forms": INTERROGATION_FORMS,
            "bridge_agent": AGENT,
            "max_tiles": 16,
        }
    else:
        pop_route = "/api/v2/generate/text/pop"
        pop_body = {
            "name": worker_name,
            "models": [TEXT_MODEL],
            "bridge_agent": AGENT,
            "amount": 10,
            "max_context_length": 4096,
            "max_length": 512,
        }
    response = client.post(pop_route, json=pop_body, headers=_headers(owner.api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    with app.app_context():
        worker_id_row = find_worker_id_by_name(worker_name)
        assert worker_id_row is not None, worker_name
        return str(worker_id_row.id)


def _make_inactive(app: Flask, worker_id: str) -> None:
    """Move the worker's last check-in far enough back that the worker is no longer active."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.database.functions import find_worker_by_id
    from horde.flask import db

    with app.app_context():
        worker = find_worker_by_id(worker_id)
        db.session.query(WorkerTemplate).filter(WorkerTemplate.id == worker.id).update(
            {WorkerTemplate.last_check_in: datetime.utcnow() - STALE_CHECK_IN_AGE},
        )
        db.session.commit()


def _with_models_sorted(row: dict[str, object]) -> dict[str, object]:
    """Return the row with its model names sorted.

    A worker's models are loaded without an ORDER BY, so two loads of one worker can list them in different orders.
    The order is not part of a row's content.
    """
    models = row.get("models")
    if isinstance(models, list):
        return {**row, "models": sorted(models)}
    return row


def _stored_row(view_name: str, worker_id: str) -> dict[str, object] | None:
    """Return the decoded row of the worker in the view, or None when the store holds no row for it."""
    from horde.worker_rows import WorkerListView, read_worker_rows

    (stored_row,) = read_worker_rows(WorkerListView(view_name), [worker_id])
    if stored_row is None:
        return None
    return _with_models_sorted(json.loads(stored_row))


def _single_worker_response(client: FlaskClient, worker_id: str, api_key: str | None) -> dict[str, object]:
    """Return the body of ``GET /v2/workers/{id}`` for the key, bypassing the route's 30-second cache."""
    from horde.horde_redis import horde_redis

    horde_redis.horde_r_delete(f"cached_worker_{worker_id}")
    horde_redis.horde_r_delete(f"cached_worker_{worker_id}_privileged")
    response = client.get(f"/api/v2/workers/{worker_id}", headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    return _with_models_sorted(response.get_json())


def _clear_row_store() -> None:
    """Delete every row and the rolling pass state, so a test sees only the rows its own job runs write."""
    from horde.horde_redis import horde_redis
    from horde.worker_rows import (
        WORKER_ROWS_ROLLING_PASS_STARTED_KEY,
        WORKER_ROWS_ROLLING_POSITION_KEY,
        WorkerListView,
    )

    for view in WorkerListView:
        horde_redis.horde_r_delete(view.rows_key)
    horde_redis.horde_r_delete(WORKER_ROWS_ROLLING_POSITION_KEY)
    horde_redis.horde_r_delete(WORKER_ROWS_ROLLING_PASS_STARTED_KEY)


def _all_worker_ids(app: Flask) -> list[str]:
    from horde.database.functions import get_worker_ids_after

    with app.app_context():
        return get_worker_ids_after(None, LARGE_ROLLING_CHUNK)


class TestStoredRowContent:
    """Each stored row is the object that the single-worker route returns for the same worker and view."""

    @pytest.mark.parametrize("kind", list(WorkerKind))
    def test_stored_rows_match_the_single_worker_route_in_each_view(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
        make_api_user: MakeApiUser,
        kind: WorkerKind,
    ) -> None:
        """The public row equals the anonymous response and the moderator row equals the moderator response."""
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_worker(client, app, owner, kind)

        store_worker_list()

        assert _stored_row("public", worker_id) == _single_worker_response(client, worker_id, None)
        assert _stored_row("moderator", worker_id) == _single_worker_response(client, worker_id, api_key)

    @pytest.mark.parametrize("kind", list(WorkerKind))
    def test_rows_of_a_private_owner_keep_the_owner_fields_out_of_the_public_view_only(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        kind: WorkerKind,
    ) -> None:
        """For an owner who has not made their workers public, only the owner row identifies the owner.

        No endpoint returns the owner level, so the owner row is compared with the owner-level details marshalled
        by the single-worker route's model.
        """
        from flask_restx import marshal

        from horde.apis.v2.base import models
        from horde.database.functions import find_worker_by_id
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        response = client.put(
            f"/api/v2/users/{owner.id}",
            json={"contact": f"owner-{uuid.uuid4().hex[:8]}@example.com"},
            headers=_headers(owner.api_key),
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        worker_id = _register_worker(client, app, owner, kind)

        store_worker_list()

        public_row = _stored_row("public", worker_id)
        assert public_row is not None
        assert {"owner", "ipaddr", "contact"}.isdisjoint(public_row)
        owner_row = _stored_row("owner", worker_id)
        assert owner_row is not None
        with app.app_context():
            owner_details = marshal(
                find_worker_by_id(worker_id).get_details(1),
                models.response_model_worker_details,
                skip_none=True,
            )
            expected_owner_row = _with_models_sorted(json.loads(json.dumps(owner_details)))
        assert owner_row == expected_owner_row
        assert {"owner", "messages", "ipaddr", "contact"} <= set(owner_row)
        assert {"paused", "suspicious"}.isdisjoint(owner_row)


class TestRollingTier:
    """The rolling pass rewrites every registered worker in ID order, whether or not it is active."""

    def test_rolling_pass_writes_rows_of_an_inactive_worker(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """A worker that the worker list job never sees as active still gets rows, marked offline."""
        from horde.database.threads import refresh_worker_rows_rolling

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_worker(client, app, owner)
        _make_inactive(app, worker_id)
        _clear_row_store()

        refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)

        public_row = _stored_row("public", worker_id)
        assert public_row is not None
        assert public_row["online"] is False
        assert _stored_row("moderator", worker_id) is not None
        assert _stored_row("owner", worker_id) is not None

    def test_repeated_small_chunks_cover_every_worker_and_wrap(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """Runs with a small chunk advance through every worker, wrap after the last one and start over."""
        from horde.database.threads import refresh_worker_rows_rolling
        from horde.worker_rows import read_rolling_position

        owner = make_api_user(trusted=True, kudos=100)
        for _ in range(5):
            _register_worker(client, app, owner)
        _clear_row_store()
        worker_ids = _all_worker_ids(app)
        runs_to_wrap = len(worker_ids) // SMALL_ROLLING_CHUNK + 1

        positions: list[str | None] = []
        for _ in range(runs_to_wrap):
            refresh_worker_rows_rolling(chunk_size=SMALL_ROLLING_CHUNK)
            positions.append(read_rolling_position())

        expected_positions: list[str | None] = [
            worker_ids[run_index * SMALL_ROLLING_CHUNK + SMALL_ROLLING_CHUNK - 1] for run_index in range(runs_to_wrap - 1)
        ]
        expected_positions.append(None)
        assert positions == expected_positions
        assert all(_stored_row("public", worker_id) is not None for worker_id in worker_ids)
        assert all(_stored_row("moderator", worker_id) is not None for worker_id in worker_ids)
        assert all(_stored_row("owner", worker_id) is not None for worker_id in worker_ids)

        refresh_worker_rows_rolling(chunk_size=SMALL_ROLLING_CHUNK)
        assert read_rolling_position() == worker_ids[SMALL_ROLLING_CHUNK - 1]

    def test_rolling_chunk_statements_do_not_grow_with_the_number_of_workers(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        """The workers of a chunk are loaded in bulk, so a larger chunk runs no more statements."""
        from horde.database.threads import refresh_worker_rows_rolling

        owner = make_api_user(trusted=True, kudos=100)
        _register_worker(client, app, owner, WorkerKind.TEXT)
        _register_worker(client, app, owner, WorkerKind.IMAGE)
        _register_worker(client, app, owner, WorkerKind.INTERROGATION)

        _clear_row_store()
        with assert_query_count() as fewer_workers_queries:
            refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)

        for kind in (WorkerKind.TEXT, WorkerKind.IMAGE, WorkerKind.INTERROGATION):
            _register_worker(client, app, owner, kind)
            _register_worker(client, app, owner, kind)
        _clear_row_store()
        with assert_query_count() as more_workers_queries:
            refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)

        assert len(more_workers_queries) <= len(fewer_workers_queries), more_workers_queries.describe()


class TestDirtyTier:
    """Workers marked dirty are rewritten by the next run of the worker list job."""

    def test_changing_worker_settings_refreshes_its_row_on_the_next_run(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """A worker settings change marks the worker dirty, and the next run writes the changed row."""
        from horde.database.threads import refresh_worker_rows_rolling, store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_worker(client, app, owner)
        _make_inactive(app, worker_id)
        refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)
        new_info = f"changed info {uuid.uuid4().hex[:8]}"

        response = client.put(f"/api/v2/workers/{worker_id}", json={"info": new_info}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        public_row = _stored_row("public", worker_id)
        assert public_row is not None
        assert public_row.get("info") != new_info

        store_worker_list()

        public_row = _stored_row("public", worker_id)
        assert public_row is not None
        assert public_row["info"] == new_info

    def test_changing_the_owner_refreshes_the_rows_of_every_worker_of_the_owner(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
        make_api_user: MakeApiUser,
    ) -> None:
        """A user change marks every worker of the user dirty, of every worker type."""
        from horde.database.threads import refresh_worker_rows_rolling, store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = [
            _register_worker(client, app, owner, WorkerKind.TEXT),
            _register_worker(client, app, owner, WorkerKind.INTERROGATION),
        ]
        for worker_id in worker_ids:
            _make_inactive(app, worker_id)
        refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)
        assert all("owner" not in (_stored_row("public", worker_id) or {}) for worker_id in worker_ids)

        response = client.put(f"/api/v2/users/{owner.id}", json={"public_workers": True}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        store_worker_list()

        for worker_id in worker_ids:
            public_row = _stored_row("public", worker_id)
            assert public_row is not None
            assert public_row["owner"] == owner.alias

    def test_dirty_drain_runs_no_statement_when_no_worker_is_dirty(
        self,
        app: Flask,
        assert_query_count: QueryCounter,
    ) -> None:
        """With an empty dirty set the drain returns before touching the database."""
        from horde.database.threads import _refresh_dirty_worker_rows
        from horde.horde_redis import horde_redis
        from horde.worker_rows import WORKER_ROWS_DIRTY_KEY

        horde_redis.horde_r_delete(WORKER_ROWS_DIRTY_KEY)

        with app.app_context(), assert_query_count() as queries:
            _refresh_dirty_worker_rows(set())

        assert len(queries) == 0, queries.describe()


def _inactive_worker_with_rows(
    client: FlaskClient,
    app: Flask,
    owner: ApiUser,
    kind: WorkerKind = WorkerKind.TEXT,
) -> str:
    """Register a worker, move it out of the active set and write its rows, then return its ID.

    The worker list job does not rewrite an inactive worker, so a later change shows in its rows only when a write
    site marks the worker dirty.
    """
    from horde.database.threads import refresh_worker_rows_rolling

    worker_id = _register_worker(client, app, owner, kind)
    _make_inactive(app, worker_id)
    refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)
    return worker_id


def _stored_row_of(view_name: str, worker_id: str) -> dict[str, object]:
    """Return the decoded row of the worker in the view, failing when the store holds no row for it."""
    stored_row = _stored_row(view_name, worker_id)
    assert stored_row is not None, (view_name, worker_id)
    return stored_row


class TestWriteSitesMarkWorkersDirty:
    """A change outside a worker or user edit that feeds a worker's row shows on the next run of the worker list job."""

    def test_new_and_deleted_messages_show_in_the_owner_row(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """A message sent to a worker appears in the owner row, and disappears once deleted."""
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _inactive_worker_with_rows(client, app, owner)
        message_text = f"message {uuid.uuid4().hex[:8]}"

        response = client.post(
            "/api/v2/workers/messages",
            json={"worker_id": worker_id, "message": message_text, "expiry": 12},
            headers=_headers(owner.api_key),
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        message_id = response.get_json()["id"]
        store_worker_list()

        owner_messages = _stored_row_of("owner", worker_id)["messages"]
        assert isinstance(owner_messages, list)
        assert [stored_message["message"] for stored_message in owner_messages] == [message_text]

        response = client.delete(f"/api/v2/workers/messages/{message_id}", headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        store_worker_list()

        assert _stored_row_of("owner", worker_id)["messages"] == []

    def test_team_rename_and_deletion_show_in_the_member_row(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """Renaming a team changes the name in its member's row, and deleting the team clears the team."""
        from horde.database.threads import refresh_worker_rows_rolling, store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        response = client.post(
            "/api/v2/teams",
            json={"name": f"Row Store Team {uuid.uuid4().hex[:8]}"},
            headers=_headers(owner.api_key),
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        team_id = response.get_json()["id"]
        worker_id = _register_worker(client, app, owner)
        response = client.put(f"/api/v2/workers/{worker_id}", json={"team": team_id}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        _make_inactive(app, worker_id)
        refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)
        new_team_name = f"Renamed Row Store Team {uuid.uuid4().hex[:8]}"

        response = client.patch(f"/api/v2/teams/{team_id}", json={"name": new_team_name}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        store_worker_list()

        assert _stored_row_of("public", worker_id)["team"] == {"id": team_id, "name": new_team_name}

        response = client.delete(f"/api/v2/teams/{team_id}", headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        store_worker_list()

        stored_team = _stored_row_of("public", worker_id).get("team")
        assert not isinstance(stored_team, dict) or stored_team.get("id") is None, stored_team

    def test_suspicion_reset_shows_in_the_moderator_row(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """Clearing a worker's suspicions sets ``suspicious`` back to zero in the moderator row."""
        from horde.database.functions import find_worker_by_id
        from horde.database.threads import refresh_worker_rows_rolling, store_worker_list
        from horde.suspicions import Suspicions

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_worker(client, app, owner)
        _make_inactive(app, worker_id)
        with app.app_context():
            find_worker_by_id(worker_id).report_suspicion(reason=Suspicions.WORKER_PROFANITY, formats=["a profane name"])
        refresh_worker_rows_rolling(chunk_size=LARGE_ROLLING_CHUNK)
        assert _stored_row_of("moderator", worker_id)["suspicious"] == 1

        with app.app_context():
            find_worker_by_id(worker_id).reset_suspicion()
        store_worker_list()

        assert _stored_row_of("moderator", worker_id)["suspicious"] == 0

    def test_flagging_the_owner_outside_the_user_edit_shows_in_the_row(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """``User.set_flagged`` marks the owner's workers, whatever calls it."""
        from horde.database.functions import find_user_by_id
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _inactive_worker_with_rows(client, app, owner)
        assert _stored_row_of("public", worker_id)["flagged"] is False

        with app.app_context():
            find_user_by_id(owner.id).set_flagged(True)
        store_worker_list()

        assert _stored_row_of("public", worker_id)["flagged"] is True

    def test_trust_promotion_by_the_kudos_applier_shows_in_the_row(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user promoted to trusted by the applier has ``trusted: true`` in the rows of their workers."""
        from horde.classes.base.user import User
        from horde.database.kudos_ledger import apply_pending_kudos
        from horde.database.threads import store_worker_list
        from horde.flask import db

        owner = make_api_user(kudos=100)
        worker_id = _inactive_worker_with_rows(client, app, owner, WorkerKind.IMAGE)
        assert _stored_row_of("public", worker_id)["trusted"] is False
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", str(PROMOTION_THRESHOLD))
        with app.app_context():
            db.session.query(User).filter(User.id == owner.id).update(
                {
                    User.evaluating_kudos: PROMOTION_THRESHOLD * 2,
                    User.created: datetime.utcnow() - User.PROMOTION_MIN_ACCOUNT_AGE - timedelta(days=1),
                },
            )
            db.session.commit()
            apply_pending_kudos()

        store_worker_list()

        assert _stored_row_of("public", worker_id)["trusted"] is True

    def test_applied_worker_kudos_show_in_kudos_rewards_with_one_mark_per_batch(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One applier batch folding two workers' kudos marks both workers with a single call."""
        import horde.database.kudos_ledger as kudos_ledger
        from horde.database.functions import find_worker_by_id
        from horde.database.threads import store_worker_list
        from horde.worker_rows import mark_worker_rows_dirty

        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = [_inactive_worker_with_rows(client, app, owner) for _ in range(2)]
        settle_kudos()
        kudos_before: dict[str, float] = {}
        for worker_id in worker_ids:
            stored_kudos = _stored_row_of("public", worker_id)["kudos_rewards"]
            assert isinstance(stored_kudos, int | float)
            kudos_before[worker_id] = stored_kudos
        with app.app_context():
            for worker_id in worker_ids:
                find_worker_by_id(worker_id).modify_kudos(WORKER_KUDOS_AWARD, "generated")
        marked_batches: list[set[str]] = []

        def _record_mark(marked_worker_ids: set[str]) -> None:
            marked_batches.append(set(marked_worker_ids))
            mark_worker_rows_dirty(marked_worker_ids)

        monkeypatch.setattr(kudos_ledger, "mark_worker_rows_dirty", _record_mark)

        with app.app_context():
            kudos_ledger.apply_pending_kudos()
        store_worker_list()

        assert len(marked_batches) == 1
        assert set(worker_ids) <= marked_batches[0]
        for worker_id in worker_ids:
            assert _stored_row_of("public", worker_id)["kudos_rewards"] == kudos_before[worker_id] + WORKER_KUDOS_AWARD


class TestActiveTier:
    """The worker list job rewrites active workers and promptly rewrites those that stop being active."""

    def test_worker_that_stops_being_active_is_rewritten_offline(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """A worker active on one run and inactive on the next gets ``online: false`` on that next run."""
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_worker(client, app, owner)
        store_worker_list()
        public_row = _stored_row("public", worker_id)
        assert public_row is not None
        assert public_row["online"] is True

        _make_inactive(app, worker_id)
        store_worker_list()

        for view_name in ("public", "moderator", "owner"):
            stored_row = _stored_row(view_name, worker_id)
            assert stored_row is not None
            assert stored_row["online"] is False


class TestDeletion:
    """Deleting a worker removes its rows."""

    def test_deleting_a_worker_removes_its_rows_from_every_view(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """After a deletion through the API, neither view holds a row for the worker."""
        from horde.database.threads import store_worker_list

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_worker(client, app, owner)
        store_worker_list()
        assert _stored_row("public", worker_id) is not None

        response = client.delete(f"/api/v2/workers/{worker_id}", headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)

        assert _stored_row("public", worker_id) is None
        assert _stored_row("moderator", worker_id) is None
        assert _stored_row("owner", worker_id) is None
