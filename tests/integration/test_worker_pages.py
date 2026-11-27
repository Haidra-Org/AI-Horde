# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Walk ``GET /v2/workers/page`` and ``GET /v2/workers/mine`` the way front ends, worker owners and moderators do.

Workers register by popping for jobs, and owners change their settings through the API. The tests then run the jobs
that production runs on the quorum node: the worker list job (``store_worker_list``), which writes the stored rows,
and the refresh of ``worker_list_entries``, which the page query reads. The tests in one module share that module's
schema, so workers registered by earlier tests in this module stay registered. Each test narrows its pages to its own
workers with a filter unless it needs the whole list.

The orders, filters and walks of the page query are covered at the query level in
``tests/integration/test_worker_list_entries.py``. These tests cover what the endpoints add: parsing and refusals,
the caller's view, the response body and headers, availability, rate limits and metrics.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from tests.conftest import _QueryRecorder
from tests.integration.test_maintenance_threads import (
    FEW_SUSPICION_ROWS,
    INTERROGATION_PERFORMANCE_ROWS,
    MANY_SUSPICION_ROWS,
    _add_suspicion_rows,
    _create_listed_worker,
)
from tests.unit.model_reference_seed import seed_image_reference

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from werkzeug.test import TestResponse

    from tests.fixture_types import ApiUser, MakeApiUser

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
"""The client agent sent with every request."""
TEXT_MODEL: str = "elinas/chronos-70b-v2"
"""A text model advertised by registered text workers."""
IMAGE_MODEL: str = "stable_diffusion"
"""An image model advertised by registered image workers, pinned in the image reference by a fixture."""
WORKER_AGENT: str = "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen"
"""The bridge agent of registered image workers."""
STALE_CHECK_IN_AGE: timedelta = timedelta(hours=1)
"""How long ago an inactive worker last checked in. Well past the five minutes after which a worker is inactive."""
WALK_PAGE_LIMIT: int = 200
"""The most pages a walk reads before the test fails, so a cursor that does not advance cannot loop forever."""
LOWERED_FLEET_RATE_LIMIT: str = "3/minute"
"""The fleet-wide page limit in the rate limit test, low enough to reach with a few requests."""


@pytest.fixture(autouse=True)
def _unlimited_requests() -> Iterator[None]:
    """Lift the rate limits, because a walk here makes more requests a minute than one client in production would."""
    from horde.limiter import limiter

    enabled_before = limiter.enabled
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = enabled_before


@pytest.fixture(autouse=True)
def _known_image_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the image reference so an image worker can register without reaching the model reference service."""
    seed_image_reference(monkeypatch, {IMAGE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})


def _headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Client-Agent": AGENT}
    if api_key is not None:
        headers["apikey"] = api_key
    return headers


def _unique_name(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _run_list_jobs() -> None:
    """Run the quorum node's jobs that the worker lists read, as production does every few seconds and every minute.

    The worker list job writes the snapshot of the active workers and their rows. The refresh brings
    ``worker_list_entries`` in line with the registered workers and marks dirty every worker without a stored row,
    and the second run of the worker list job writes the rows of those workers, active or not.
    """
    from horde.database.threads import refresh_worker_list_entries, store_worker_list

    store_worker_list()
    refresh_worker_list_entries()
    store_worker_list()


def _register_text_worker(client: FlaskClient, owner_key: str, name: str) -> str:
    """Register a text worker the way the worker software does, by popping for a job, and return its ID."""
    response = client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": name,
            "models": [TEXT_MODEL],
            "bridge_agent": AGENT,
            "amount": 1,
            "max_context_length": 4096,
            "max_length": 512,
        },
        headers=_headers(owner_key),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return _worker_id_by_name(client, name)


def _register_image_worker(client: FlaskClient, owner_key: str, name: str) -> str:
    """Register an image worker by popping for a job, and return its ID."""
    response = client.post(
        "/api/v2/generate/pop",
        json={
            "name": name,
            "models": [IMAGE_MODEL],
            "bridge_agent": WORKER_AGENT,
            "nsfw": True,
            "amount": 1,
            "max_pixels": 262144,
            "allow_unsafe_ipaddr": True,
        },
        headers=_headers(owner_key),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return _worker_id_by_name(client, name)


def _worker_id_by_name(client: FlaskClient, name: str) -> str:
    response = client.get(f"/api/v2/workers/name/{name}", headers=_headers())
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["id"]


def _make_inactive(app: Flask, worker_id: str) -> None:
    """Move a worker's last check-in back, as if it stopped popping for jobs an hour ago."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    with app.app_context():
        db.session.query(WorkerTemplate).filter(WorkerTemplate.id == uuid.UUID(worker_id)).update(
            {WorkerTemplate.last_check_in: datetime.utcnow() - STALE_CHECK_IN_AGE},
        )
        db.session.commit()


def _with_models_sorted(row: dict[str, Any]) -> dict[str, Any]:
    """Return the row with its model names sorted.

    A worker's models are loaded without an ORDER BY, so two loads of one worker can list them in different orders.
    The order is not part of a row's content.
    """
    models = row.get("models")
    if isinstance(models, list):
        return {**row, "models": sorted(models)}
    return row


def _single_worker_response(client: FlaskClient, worker_id: str, api_key: str | None) -> dict[str, Any]:
    """Return the body of ``GET /v2/workers/{id}`` for the key, bypassing the route's 30-second cache."""
    from horde.horde_redis import horde_redis

    horde_redis.horde_r_delete(f"cached_worker_{worker_id}")
    horde_redis.horde_r_delete(f"cached_worker_{worker_id}_privileged")
    response = client.get(f"/api/v2/workers/{worker_id}", headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _stored_row_bytes(view_name: str, worker_id: str) -> bytes | None:
    """Return the stored row of the worker in the view, or None when the row store holds no row for it."""
    from horde.worker_rows import WorkerListView, read_worker_rows

    (stored_row,) = read_worker_rows(WorkerListView(view_name), [worker_id])
    return stored_row


def _remove_public_rows(worker_ids: list[str]) -> None:
    """Delete the public rows of ``worker_ids`` only, as Redis does when it loses rows."""
    from horde.horde_redis import horde_redis
    from horde.worker_rows import WorkerListView

    horde_redis.horde_r_hdel(WorkerListView.PUBLIC.rows_key, worker_ids)


def _get_page(
    client: FlaskClient,
    *,
    api_key: str | None = None,
    path: str = "/api/v2/workers/page",
    **query: Any,
) -> TestResponse:
    return client.get(path, query_string=query, headers=_headers(api_key))


def _get_mine(client: FlaskClient, api_key: str, **query: Any) -> TestResponse:
    return _get_page(client, api_key=api_key, path="/api/v2/workers/mine", **query)


def _walk(
    client: FlaskClient,
    *,
    api_key: str | None = None,
    path: str = "/api/v2/workers/page",
    **query: Any,
) -> list[dict[str, Any]]:
    """Return every page body of a walk, following next_cursor until it is null."""
    bodies: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(WALK_PAGE_LIMIT):
        page_query = dict(query)
        if cursor is not None:
            page_query["cursor"] = cursor
        response = _get_page(client, api_key=api_key, path=path, **page_query)
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        bodies.append(body)
        cursor = body["next_cursor"]
        if cursor is None:
            return bodies
    raise AssertionError("the walk did not end")


def _walked_rows(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for body in bodies for row in body["workers"]]


def _walked_ids(bodies: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in _walked_rows(bodies)]


class _RecordingCounter:
    """Stand in for a metric counter or histogram and keep what was recorded."""

    def __init__(self) -> None:
        self.additions: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.additions.append((amount, attributes))

    def record(self, amount: int, attributes: dict[str, str]) -> None:
        self.additions.append((amount, attributes))


class TestResponseBody:
    """A page is the stored rows joined as they are, so each row is the object that the single-worker route returns."""

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_a_page_row_is_the_stored_row_and_the_single_worker_details(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        as_moderator: bool,
    ) -> None:
        """The body holds the stored bytes verbatim, and they decode to the body of ``GET /v2/workers/{id}``."""
        owner = make_api_user(trusted=True, kudos=100)
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None
        token = uuid.uuid4().hex[:8]
        worker_id = _register_text_worker(client, owner.api_key, f"Verbatim {token} Scribe")
        _run_list_jobs()

        response = _get_page(client, api_key=caller_key, name_contains=token)

        stored_row = _stored_row_bytes("moderator" if as_moderator else "public", worker_id)
        assert stored_row is not None
        assert response.get_data() == b'{"workers":[' + stored_row + b'],"next_cursor":null,"total":1}'
        assert response.mimetype == "application/json"
        details = _single_worker_response(client, worker_id, caller_key)
        assert _with_models_sorted(json.loads(stored_row)) == _with_models_sorted(details)

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_an_inactive_worker_is_listed_offline_with_the_fields_of_its_own_details(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        as_moderator: bool,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None
        token = uuid.uuid4().hex[:8]
        worker_id = _register_text_worker(client, owner.api_key, f"Retired {token} Scribe")
        _run_list_jobs()
        _make_inactive(app, worker_id)
        _run_list_jobs()

        rows = _get_page(client, api_key=caller_key, name_contains=token).get_json()["workers"]

        assert [row["id"] for row in rows] == [worker_id]
        assert rows[0]["online"] is False
        assert _with_models_sorted(rows[0]) == _with_models_sorted(_single_worker_response(client, worker_id, caller_key))

    def test_a_walk_lists_every_worker_once_and_every_page_counts_the_whole_list(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        worker_ids = {_register_text_worker(client, owner.api_key, _unique_name(f"Walked {token}")) for _ in range(5)}
        _run_list_jobs()

        bodies = _walk(client, name_contains=token, limit=2)

        assert sorted(_walked_ids(bodies)) == sorted(worker_ids)
        assert [body["total"] for body in bodies] == [5, 5, 5]


class TestFullListParity:
    """The full list of active workers and the paged list with the online filter return the same rows."""

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_the_online_filter_returns_the_rows_of_the_full_list(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        as_moderator: bool,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None
        token = uuid.uuid4().hex[:8]
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name(f"Parity {token} Scribe"))
        _register_image_worker(client, owner.api_key, _unique_name(f"Parity {token} Painter"))
        offline_id = _register_text_worker(client, owner.api_key, _unique_name(f"Parity {token} Sleeper"))
        _make_inactive(app, offline_id)
        _run_list_jobs()

        full_list = client.get("/api/v2/workers", headers=_headers(caller_key)).get_json()
        walked_rows = _walked_rows(_walk(client, api_key=caller_key, name_contains=token, online="true", limit=2))

        expected_rows = [_with_models_sorted(row) for row in full_list if token in row["name"]]
        assert len(expected_rows) == 4
        assert sorted(map(_with_models_sorted, walked_rows), key=lambda row: row["id"]) == sorted(
            expected_rows,
            key=lambda row: row["id"],
        )

    def test_the_full_list_returns_a_bare_array(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        name = _unique_name("Legacy Scribe")
        _register_text_worker(client, owner.api_key, name)
        _run_list_jobs()

        response = client.get("/api/v2/workers", query_string={"name": name.upper()}, headers=_headers())

        assert response.status_code == 200
        assert [row["name"] for row in response.get_json()] == [name]

    @pytest.mark.parametrize("as_moderator", [False, True], ids=["public", "moderator"])
    def test_only_moderator_rows_of_the_full_list_are_kept_out_of_shared_caches(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        as_moderator: bool,
    ) -> None:
        caller_key = make_api_user(moderator=True).api_key if as_moderator else None

        response = client.get("/api/v2/workers", headers=_headers(caller_key))

        assert response.status_code == 200
        if as_moderator:
            assert response.headers["Cache-Control"] == "private, no-store"
        else:
            assert "Cache-Control" not in response.headers


class TestViewsAndPrivacy:
    """Each caller gets the rows of their view, and owners decide whether the public may see which workers are theirs."""

    def test_the_public_cannot_find_a_private_owners_workers_by_owner(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        private_owner = make_api_user(trusted=True, kudos=100)
        _register_text_worker(client, private_owner.api_key, _unique_name("Private Scribe"))
        _run_list_jobs()

        body = _get_page(client, owner_id=private_owner.id).get_json()

        assert body["workers"] == []
        assert body["total"] == 0, "the count alone would reveal how many workers the account runs"

    def test_an_owner_who_publishes_their_workers_is_found_by_owner(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _register_text_worker(client, owner.api_key, _unique_name("Public Scribe"))
        _run_list_jobs()
        response = client.put(f"/api/v2/users/{owner.id}", json={"public_workers": True}, headers=_headers(owner.api_key))
        assert response.status_code == 200, response.get_data(as_text=True)
        _run_list_jobs()

        body = _get_page(client, owner_id=owner.id).get_json()

        assert [row["id"] for row in body["workers"]] == [worker_id]
        assert body["workers"][0]["owner"] == owner.alias

    def test_a_moderator_finds_a_private_owners_workers_with_addresses_and_no_caching(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        private_owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        worker_id = _register_text_worker(client, private_owner.api_key, _unique_name("Reviewed Scribe"))
        _run_list_jobs()

        response = _get_page(client, api_key=moderator.api_key, owner_id=private_owner.id)

        assert [row["id"] for row in response.get_json()["workers"]] == [worker_id]
        assert "ipaddr" in response.get_json()["workers"][0]
        assert response.headers["Cache-Control"] == "private, no-store"

    def test_a_page_for_an_ordinary_key_has_no_moderator_fields_and_may_be_cached(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        _register_text_worker(client, owner.api_key, f"Plain {token} Scribe")
        _run_list_jobs()

        response = _get_page(client, api_key=owner.api_key, name_contains=token)

        rows = response.get_json()["workers"]
        assert rows
        assert not any({"ipaddr", "contact", "suspicious", "paused"} & row.keys() for row in rows)
        assert response.headers["Cache-Control"] == "public, max-age=15"

    def test_an_anonymous_page_may_be_cached_by_shared_caches(self, client: FlaskClient) -> None:
        response = _get_page(client)

        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=15"

    def test_a_mistyped_key_is_refused_rather_than_read_as_the_public_view(self, client: FlaskClient) -> None:
        response = _get_page(client, api_key="not-a-real-key")

        assert response.status_code == 401
        assert response.get_json()["rc"] == "InvalidAPIKey"


class TestRefusedRequests:
    """A malformed request is refused with a return code that a client can branch on, instead of a wrong page."""

    @pytest.mark.parametrize("limit", [0, 101, -5])
    def test_a_page_size_outside_the_range_is_refused(self, client: FlaskClient, limit: int) -> None:
        response = _get_page(client, limit=limit)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListLimit"

    def test_ordering_by_suspicion_is_refused(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        moderator = make_api_user(moderator=True)

        response = _get_page(client, api_key=moderator.api_key, sort="suspicious")

        assert response.status_code == 400

    def test_a_cursor_reused_with_another_sort_is_refused(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name("Cursor Scribe"))
        _run_list_jobs()
        name_cursor = _get_page(client, limit=1, sort="name").get_json()["next_cursor"]

        response = _get_page(client, limit=1, sort="uptime", cursor=name_cursor)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListCursor"

    def test_a_cursor_reused_with_other_filters_is_refused(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        """A walk continued under new filters would skip the matching workers before the cursor without any error."""
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name(f"Refiltered {token} Scribe"))
        _run_list_jobs()
        cursor = _get_page(client, limit=1, sort="name", name_contains=token).get_json()["next_cursor"]

        same_walk = _get_page(client, limit=1, sort="name", name_contains=token, cursor=cursor)
        other_filters = _get_page(client, limit=1, sort="name", name_contains=token, type="text", cursor=cursor)

        assert same_walk.status_code == 200
        assert other_filters.status_code == 400
        assert other_filters.get_json()["rc"] == "InvalidWorkerListCursor"

    def test_a_cursor_reused_in_another_view_or_list_is_refused(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        """The view and the list change which workers are listed, so a position from one does not apply to another."""
        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name("Crossed Scribe"))
        _run_list_jobs()
        moderator_cursor = _get_page(client, api_key=moderator.api_key, limit=1, owner_id=owner.id).get_json()["next_cursor"]
        owner_cursor = _get_mine(client, owner.api_key, limit=1).get_json()["next_cursor"]

        public_page = _get_page(client, limit=1, owner_id=owner.id, cursor=moderator_cursor)
        paged_list = _get_page(client, api_key=owner.api_key, limit=1, cursor=owner_cursor)

        assert public_page.status_code == 400
        assert public_page.get_json()["rc"] == "InvalidWorkerListCursor"
        assert paged_list.status_code == 400
        assert paged_list.get_json()["rc"] == "InvalidWorkerListCursor"

    def test_a_cursor_not_issued_by_the_server_is_refused(self, client: FlaskClient) -> None:
        response = _get_page(client, cursor="not-a-cursor")

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListCursor"

    @pytest.mark.parametrize(
        "query",
        [{"team_id": "not-a-uuid"}, {"owner_id": -1}, {"name_contains": "x" * 101}],
        ids=["team-not-uuid", "negative-owner", "name-search-longer-than-a-name"],
    )
    def test_a_malformed_filter_is_refused(self, client: FlaskClient, query: dict[str, object]) -> None:
        response = _get_page(client, **query)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "InvalidWorkerListFilter"

    def test_a_blank_text_filter_does_not_narrow_the_list(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        _register_text_worker(client, owner.api_key, _unique_name("Blank Filter Scribe"))
        _run_list_jobs()

        unfiltered = _get_page(client, limit=100).get_json()
        blank_filtered = _get_page(client, limit=100, model="", team_id=" ", name_contains=" ").get_json()

        assert blank_filtered["total"] == unfiltered["total"] > 0


class TestPageCost:
    """A page runs one statement against the database, whatever the page holds, and does not load any worker."""

    def test_an_anonymous_page_issues_one_statement_on_worker_list_entries(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        for _ in range(3):
            _register_text_worker(client, owner.api_key, _unique_name(f"Cheap {token} Scribe"))
        _run_list_jobs()

        with assert_query_count() as queries:
            response = _get_page(client, name_contains=token)

        assert len(response.get_json()["workers"]) == 3
        assert len(queries) == 1, queries.describe()
        assert queries.table_counts("SELECT") == {"worker_list_entries": 1}, queries.describe()

    def test_the_moderator_statement_count_does_not_grow_with_suspicion_rows_or_interrogation_workers(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker

        owner = make_api_user(trusted=True)
        moderator = make_api_user(moderator=True)
        with app.app_context():
            image_worker = _create_listed_worker(ImageWorker, owner_id=owner.id, suspicion_rows=FEW_SUSPICION_ROWS)
            _create_listed_worker(
                InterrogationWorker,
                owner_id=owner.id,
                suspicion_rows=FEW_SUSPICION_ROWS,
                performance_rows=INTERROGATION_PERFORMANCE_ROWS,
            )
        _run_list_jobs()
        _get_page(client, api_key=moderator.api_key, owner_id=owner.id)
        with assert_query_count() as queries_before:
            assert _get_page(client, api_key=moderator.api_key, owner_id=owner.id).get_json()["total"] == 2
        with app.app_context():
            _add_suspicion_rows(uuid.UUID(image_worker.id), MANY_SUSPICION_ROWS)
            for _ in range(2):
                _create_listed_worker(
                    InterrogationWorker,
                    owner_id=owner.id,
                    suspicion_rows=MANY_SUSPICION_ROWS,
                    performance_rows=INTERROGATION_PERFORMANCE_ROWS,
                )
        _run_list_jobs()
        _get_page(client, api_key=moderator.api_key, owner_id=owner.id)

        with assert_query_count() as queries_after:
            assert _get_page(client, api_key=moderator.api_key, owner_id=owner.id).get_json()["total"] == 4

        assert len(queries_after) == len(queries_before), f"before:\n{queries_before.describe()}\n\nafter:\n{queries_after.describe()}"
        assert "workers" not in queries_after.table_counts("SELECT"), queries_after.describe()

    def test_a_moderator_page_reports_each_workers_suspicion_row_count(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        from horde.classes.kobold.worker import TextWorker
        from horde.classes.stable.interrogation_worker import InterrogationWorker
        from horde.classes.stable.worker import ImageWorker

        owner = make_api_user(trusted=True)
        moderator = make_api_user(moderator=True)
        expected_counts: dict[str, int] = {}
        with app.app_context():
            for worker_class, suspicion_rows in [
                (ImageWorker, 0),
                (ImageWorker, MANY_SUSPICION_ROWS),
                (TextWorker, FEW_SUSPICION_ROWS),
                (InterrogationWorker, MANY_SUSPICION_ROWS),
            ]:
                listed_worker = _create_listed_worker(
                    worker_class,
                    owner_id=owner.id,
                    suspicion_rows=suspicion_rows,
                    performance_rows=INTERROGATION_PERFORMANCE_ROWS if worker_class is InterrogationWorker else 0,
                )
                expected_counts[listed_worker.id] = suspicion_rows
        _run_list_jobs()

        rows = _walked_rows(_walk(client, api_key=moderator.api_key, owner_id=owner.id, limit=2))

        assert {row["id"]: row["suspicious"] for row in rows} == expected_counts


class TestAvailability:
    """A page with some rows missing is served without them. A page that cannot be read at all is refused for a retry."""

    def test_a_worker_without_a_stored_row_is_left_out_and_still_counted(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        worker_ids = [_register_text_worker(client, owner.api_key, _unique_name(f"Gap {token} Scribe")) for _ in range(3)]
        _run_list_jobs()
        _remove_public_rows(worker_ids[:1])

        response = _get_page(client, name_contains=token)

        assert response.status_code == 200
        body = response.get_json()
        assert sorted(row["id"] for row in body["workers"]) == sorted(worker_ids[1:])
        assert body["total"] == 3

    def test_a_page_without_any_stored_row_is_refused_with_a_retry_after(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        from horde.exceptions import WorkerListUnavailable

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        worker_ids = [_register_text_worker(client, owner.api_key, _unique_name(f"Lost {token} Scribe")) for _ in range(2)]
        _run_list_jobs()
        _remove_public_rows(worker_ids)

        response = _get_page(client, name_contains=token)

        assert response.status_code == 503
        assert response.get_json()["rc"] == "WorkerListUnavailable"
        assert response.headers["Retry-After"] == str(WorkerListUnavailable.RETRY_AFTER_SECONDS)

    def test_an_empty_filtered_list_is_served_without_rows(self, client: FlaskClient) -> None:
        response = _get_page(client, name_contains=uuid.uuid4().hex)

        assert response.status_code == 200
        assert response.get_json() == {"workers": [], "next_cursor": None, "total": 0}

    def test_a_page_without_redis_is_refused_with_a_retry_after(self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
        from horde.horde_redis import horde_redis

        monkeypatch.setattr(horde_redis, "horde_r", None)

        response = _get_page(client)

        assert response.status_code == 503
        assert response.get_json()["rc"] == "WorkerListUnavailable"
        assert "Retry-After" in response.headers

    def test_a_failed_row_read_is_refused_with_a_retry_after(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import redis

        import horde.apis.v2.base as base_module

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        _register_text_worker(client, owner.api_key, _unique_name(f"Unreachable {token} Scribe"))
        _run_list_jobs()

        def _refuse_connection(*_: object) -> list[bytes | None]:
            raise redis.ConnectionError("connection refused")

        monkeypatch.setattr(base_module, "read_worker_rows", _refuse_connection)

        response = _get_page(client, name_contains=token)

        assert response.status_code == 503
        assert response.get_json()["rc"] == "WorkerListUnavailable"
        assert "Retry-After" in response.headers


class TestFleetRateLimit:
    """Both paged lists count against one limit shared by every address, on top of the per-address limit."""

    def test_page_and_mine_requests_share_one_fleet_limit(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The limit value is read on each request, so lowering it shows the shared count after a few requests."""
        import horde.apis.v2.base as base_module
        from horde.limiter import limiter

        owner = make_api_user(trusted=True, kudos=100)
        monkeypatch.setattr(base_module, "WORKER_PAGE_FLEET_RATE_LIMIT", LOWERED_FLEET_RATE_LIMIT)
        limiter.reset()
        limiter.enabled = True
        try:
            first_addresses = {"REMOTE_ADDR": "198.51.100.1"}
            other_address = {"REMOTE_ADDR": "198.51.100.2"}
            served = [
                client.get("/api/v2/workers/page", headers=_headers(), environ_base=first_addresses).status_code,
                client.get("/api/v2/workers/mine", headers=_headers(owner.api_key), environ_base=other_address).status_code,
                client.get("/api/v2/workers/page", headers=_headers(), environ_base=other_address).status_code,
            ]
            refused_page = client.get("/api/v2/workers/page", headers=_headers(), environ_base=first_addresses)
            refused_mine = client.get("/api/v2/workers/mine", headers=_headers(owner.api_key), environ_base=first_addresses)
        finally:
            limiter.enabled = False
            limiter.reset()

        assert served == [200, 200, 200]
        assert refused_page.status_code == 429
        assert refused_mine.status_code == 429


class TestSnapshotOutage:
    """When the snapshot job stops, each node rebuilds the full list from the database and must store the rebuilt list."""

    def test_a_missing_snapshot_is_rebuilt_once_and_kept_even_when_a_worker_has_a_message(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import horde.database.functions as database_functions
        from horde import horde_redis as horde_redis_module

        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        worker_id = _register_text_worker(client, owner.api_key, _unique_name("Messaged Scribe"))
        message_response = client.post(
            "/api/v2/workers/messages",
            json={"worker_id": worker_id, "message": "Please update your worker", "expiry": 12},
            headers=_headers(moderator.api_key),
        )
        assert message_response.status_code == 200, message_response.get_data(as_text=True)
        misses = _RecordingCounter()
        monkeypatch.setattr(database_functions, "worker_list_snapshot_misses", misses)
        horde_redis_module.horde_redis.horde_r.delete("worker_cache_privileged")
        horde_redis_module.horde_redis.horde_local_r.delete("worker_cache_privileged")

        first = client.get("/api/v2/workers", headers=_headers(moderator.api_key)).get_json()
        second = client.get("/api/v2/workers", headers=_headers(moderator.api_key)).get_json()

        [messaged_row] = [row for row in first if row["id"] == worker_id]
        assert messaged_row["messages"][0]["message"] == "Please update your worker"
        assert second == first
        assert misses.additions == [(1, {"horde.workers.list.view": "moderator"})], "the rebuilt list was not kept"

    def test_a_rebuilt_list_expires_with_the_local_cache_so_the_recovered_snapshot_takes_over(self, client: FlaskClient) -> None:
        from horde import horde_redis as horde_redis_module

        redis_connection = horde_redis_module.horde_redis
        redis_connection.horde_r.delete("worker_cache")
        redis_connection.horde_local_r.delete("worker_cache")

        response = client.get("/api/v2/workers", headers=_headers())

        assert response.status_code == 200
        assert 0 < redis_connection.horde_local_r.ttl("worker_cache") <= horde_redis_module.LOCAL_CACHE_MAX_LIFETIME.total_seconds()


class TestObservability:
    """Operators read the worker list traffic by endpoint, view, sort, filters and walk position."""

    def test_reads_are_counted_by_endpoint_and_walk_position_with_bounded_labels(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import horde.metrics as metrics_module

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        for _ in range(2):
            _register_text_worker(client, owner.api_key, _unique_name(f"Counted {token} Scribe"))
        _run_list_jobs()
        reads = _RecordingCounter()
        rows = _RecordingCounter()
        monkeypatch.setattr(metrics_module, "worker_list_reads", reads)
        monkeypatch.setattr(metrics_module, "worker_list_rows", rows)
        free_text_model = f"model {uuid.uuid4().hex}"

        client.get("/api/v2/workers", headers=_headers())
        first_page = _get_page(client, limit=1, name_contains=token).get_json()
        _get_page(client, limit=1, name_contains=token, cursor=first_page["next_cursor"])
        _get_page(client, limit=1, model=free_text_model)

        labels = [attributes for _, attributes in reads.additions]
        assert [(label["horde.workers.list.endpoint"], label["horde.workers.list.continued"]) for label in labels] == [
            ("full", "false"),
            ("page", "false"),
            ("page", "true"),
            ("page", "false"),
        ]
        assert labels[1]["horde.workers.list.view"] == "public"
        assert labels[1]["horde.workers.list.sort"] == "uptime"
        assert labels[1]["horde.workers.list.filters"] == "name"
        assert labels[3]["horde.workers.list.filters"] == "model"
        assert not any(free_text_model in str(label) or token in str(label) for label in labels), "a client-chosen value reached a label"
        assert [amount for amount, _ in rows.additions][1:] == [1, 1, 0]


class TestOwnersListingTheirOwnWorkers:
    """Owners check on every worker they run, including private workers and workers that dropped out."""

    def test_an_owner_lists_their_private_and_offline_workers_with_owner_fields(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        stranger = make_api_user(trusted=True, kudos=100)
        contact_response = client.put(
            f"/api/v2/users/{owner.id}",
            json={"contact": f"owner-{uuid.uuid4().hex[:8]}@example.com"},
            headers=_headers(owner.api_key),
        )
        assert contact_response.status_code == 200, contact_response.get_data(as_text=True)
        online_id = _register_text_worker(client, owner.api_key, _unique_name("Home Scribe"))
        offline_id = _register_text_worker(client, owner.api_key, _unique_name("Stalled Scribe"))
        _register_text_worker(client, stranger.api_key, _unique_name("Neighbour Scribe"))
        _run_list_jobs()
        _make_inactive(app, offline_id)
        _run_list_jobs()

        response = _get_mine(client, owner.api_key)

        rows = {row["id"]: row for row in response.get_json()["workers"]}
        assert set(rows) == {online_id, offline_id}
        assert rows[online_id]["online"] is True
        assert rows[offline_id]["online"] is False
        assert all({"owner", "messages", "ipaddr", "contact"} <= row.keys() for row in rows.values())
        assert all(row["owner"] == owner.alias for row in rows.values())
        assert not any({"paused", "suspicious"} & row.keys() for row in rows.values())
        assert response.headers["Cache-Control"] == "private, no-store"

    @pytest.mark.parametrize("online", [True, False])
    def test_the_online_filter_lists_only_the_workers_in_that_state(
        self,
        app: Flask,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        online: bool,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        online_id = _register_text_worker(client, owner.api_key, _unique_name("Running Scribe"))
        offline_id = _register_text_worker(client, owner.api_key, _unique_name("Stopped Scribe"))
        _run_list_jobs()
        _make_inactive(app, offline_id)
        _run_list_jobs()

        body = _get_mine(client, owner.api_key, online=str(online).lower()).get_json()

        assert [row["id"] for row in body["workers"]] == [online_id if online else offline_id]

    def test_a_walk_lists_every_owned_worker_once(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = {_register_text_worker(client, owner.api_key, _unique_name("Fleet Scribe")) for _ in range(5)}
        _run_list_jobs()

        bodies = _walk(client, api_key=owner.api_key, path="/api/v2/workers/mine", limit=2)

        assert sorted(_walked_ids(bodies)) == sorted(worker_ids)
        assert [body["total"] for body in bodies] == [5, 5, 5], "every page of a walk counts the whole list"

    def test_the_statement_count_does_not_grow_with_the_number_of_owned_workers(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        _register_text_worker(client, owner.api_key, _unique_name("First Scribe"))
        _register_image_worker(client, owner.api_key, _unique_name("First Painter"))
        _run_list_jobs()
        _get_mine(client, owner.api_key)
        with assert_query_count() as few_workers:
            assert _get_mine(client, owner.api_key).get_json()["total"] == 2
        for _ in range(2):
            _register_text_worker(client, owner.api_key, _unique_name("Later Scribe"))
        _register_image_worker(client, owner.api_key, _unique_name("Later Painter"))
        _run_list_jobs()
        _get_mine(client, owner.api_key)

        with assert_query_count() as many_workers:
            assert _get_mine(client, owner.api_key).get_json()["total"] == 5

        assert len(many_workers) == len(few_workers), many_workers.describe()
        assert "workers" not in many_workers.table_counts("SELECT"), many_workers.describe()

    def test_a_moderator_gets_only_the_workers_of_their_account(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        _register_text_worker(client, owner.api_key, _unique_name("Watched Scribe"))
        _run_list_jobs()

        body = _get_mine(client, moderator.api_key).get_json()

        assert body == {"workers": [], "next_cursor": None, "total": 0}

    def test_a_mistyped_key_is_refused(self, client: FlaskClient) -> None:
        response = _get_mine(client, "not-a-real-key")

        assert response.status_code == 401
        assert response.get_json()["rc"] == "InvalidAPIKey"

    def test_reads_are_counted_under_their_own_endpoint_and_view(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import horde.metrics as metrics_module

        owner: ApiUser = make_api_user(trusted=True, kudos=100)
        reads = _RecordingCounter()
        monkeypatch.setattr(metrics_module, "worker_list_reads", reads)

        _get_mine(client, owner.api_key, online="false")

        [(_, labels)] = reads.additions
        assert labels["horde.workers.list.endpoint"] == "mine"
        assert labels["horde.workers.list.view"] == "owner"
        assert labels["horde.workers.list.filters"] == "online"
