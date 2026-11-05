# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Statement and transaction budgets for the generation hot path.

Each test drives endpoints through the Flask test client against PostgreSQL, so the
counts include authentication, argument parsing and every helper the endpoint calls.
A budget is an upper bound on each statement kind and on database transactions. An
added query or commit in one of these operations fails here, and the assertion
message lists every statement and transaction with the application frames that
issued it.

Cold means the first call for a new requester or worker, with the response cache
and the Redis caches filled by request handlers empty. The values that the quorum
node's threads keep in Redis are stored, as they are in production. Warm means a
repeat call against rows, relationships and caches that already exist. Redis is the
in-process fake from the integration conftest.

Transactions are counted from the engine's connection events. A transaction that
was closed with the session, rather than committed, is a rollback. A commit with no
statements still checks a connection out of the pool. psycopg2 starts a server
transaction only with the first statement, so that commit sends nothing to the server.

Admission with a source image is not covered. It uploads to object storage, and no
fixture here provides a stand-in for that upload.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from tests.conftest import _QueryRecorder
from tests.fixture_types import ApiUser, MakeApiUser

pytestmark = pytest.mark.integration

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
"""The client agent sent with every request."""

IMAGE_MODEL: str = "stable_diffusion"
"""The image model requested and served in these tests."""

TEXT_MODEL: str = "elinas/chronos-70b-v2"
"""The text model requested and served in these tests."""

IMAGE_WORKER_AGENT: str = "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen"
"""A bridge agent that can serve the image request below."""

JOB_DURATION: timedelta = timedelta(seconds=120)
"""How long each submitted job appears to have run.

A job submitted moments after its pop reports an implausible speed, and the worker is
then reported for suspicion. The normal submit path does not include that report.
"""

SKIPPED_CANDIDATES_FEW: int = 1
"""Queued requests a worker skips in the smaller scaling case."""

SKIPPED_CANDIDATES_MANY: int = 5
"""Queued requests a worker skips in the larger scaling case; all of them fit on one page."""

IMAGE_REQUEST: dict[str, Any] = {
    "prompt": "a horde of robots counting their own queries",
    "nsfw": True,
    "censor_nsfw": False,
    "trusted_workers": True,
    "params": {
        "width": 512,
        "height": 512,
        "steps": 8,
        "cfg_scale": 1.5,
        "sampler_name": "k_euler_a",
        "n": 1,
    },
    "models": [IMAGE_MODEL],
    "allow_downgrade": False,
}
"""A single-image request that the image worker below can take."""

TEXT_REQUEST: dict[str, Any] = {
    "prompt": "a text request counting its own queries",
    "trusted_workers": True,
    "validated_backends": False,
    "params": {
        "max_length": 80,
        "max_context_length": 1024,
        "n": 1,
    },
    "models": [TEXT_MODEL],
}
"""A single-generation text request that the text worker below can take."""


@dataclass(frozen=True)
class StatementBudget:
    """Represents the most database work one operation may do.

    Statement bounds are per kind. Transaction bounds count commits and rollbacks
    separately, because a rollback here is usually a read-only transaction closed
    with the session.
    """

    selects: int
    """The most SELECT statements, including lock and advisory-lock reads."""
    inserts: int
    """The most INSERT statements."""
    updates: int
    """The most UPDATE statements, including those flushed by a later query."""
    deletes: int
    """The most DELETE statements."""
    commits: int
    """The most committed database transactions."""
    rollbacks: int
    """The most database transactions ended by a rollback or by closing the session."""

    def overruns(self, queries: _QueryRecorder) -> list[str]:
        """Return a description of every bound that ``queries`` exceeds.

        Args:
            queries: The work recorded for one operation.

        Returns:
            One line per exceeded bound; empty when the work fits the budget.
        """
        kind_counts = queries.kind_counts()
        measured = {
            "SELECT": (kind_counts["SELECT"], self.selects),
            "INSERT": (kind_counts["INSERT"], self.inserts),
            "UPDATE": (kind_counts["UPDATE"], self.updates),
            "DELETE": (kind_counts["DELETE"], self.deletes),
            # Any other statement kind, such as SAVEPOINT, has no allowance of its own.
            "all statements": (len(queries), self.selects + self.inserts + self.updates + self.deletes),
            "commits": (len(queries.commits), self.commits),
            "rollbacks": (len(queries.rollbacks), self.rollbacks),
        }
        return [f"{label}: {count} > {bound}" for label, (count, bound) in measured.items() if count > bound]


IMAGE_ADMISSION_COLD = StatementBudget(selects=12, inserts=4, updates=2, deletes=0, commits=2, rollbacks=1)
"""Keeps image admission at two commits: the prepared inactive request with its model rows and payload, then the
activation with its price, source references and charge.

The whole admission runs in one session, so the requester and its roles are read once. The rollback closes
the read-only worker check. Two of the SELECTs count active workers, which the warm case reads from Redis. The
UPDATEs are both in activation; preparation is one INSERT per row.
"""

IMAGE_ADMISSION_WARM = StatementBudget(selects=10, inserts=4, updates=2, deletes=0, commits=2, rollbacks=1)
"""Keeps a repeat image admission by the same requester from adding reads once the active worker count is cached."""

TEXT_ADMISSION = StatementBudget(selects=9, inserts=4, updates=2, deletes=0, commits=2, rollbacks=0)
"""Keeps text admission at two commits: the prepared inactive request with its model rows and payload, and activation
with its charge.

Cold and warm admissions match, because text admission does not read the active worker count.
"""

IMAGE_POP_EMPTY_COLD = StatementBudget(selects=25, inserts=2, updates=3, deletes=1, commits=3, rollbacks=1)
"""Bounds a new image worker's first pop on an empty queue: registration, model declaration and the check-in."""

IMAGE_POP_EMPTY_WARM = StatementBudget(selects=10, inserts=0, updates=1, deletes=0, commits=1, rollbacks=1)
"""Bounds the most frequent pop, an existing image worker on an empty queue; the one commit is the early check-in."""

IMAGE_POP_MATCH_WARM = StatementBudget(selects=14, inserts=1, updates=2, deletes=0, commits=2, rollbacks=1)
"""Bounds an existing image worker's pop that returns a job: the check-in, then the job with its model and lease.

The job row, its model and its lease are written under one commit; the candidate page is unlocked by that commit.
"""

IMAGE_POP_MATCH_COLD = StatementBudget(selects=29, inserts=3, updates=4, deletes=1, commits=4, rollbacks=1)
"""Bounds a new image worker's first pop that returns a job, which adds registration to the warm case."""

TEXT_POP_EMPTY_COLD = StatementBudget(selects=25, inserts=2, updates=3, deletes=1, commits=3, rollbacks=1)
"""Bounds a new text worker's first pop on an empty queue: registration, model declaration and the check-in."""

TEXT_POP_EMPTY_WARM = StatementBudget(selects=9, inserts=0, updates=1, deletes=0, commits=1, rollbacks=1)
"""Bounds the most frequent pop, an existing text worker on an empty queue; the one commit is the early check-in."""

TEXT_POP_MATCH_WARM = StatementBudget(selects=15, inserts=1, updates=2, deletes=0, commits=2, rollbacks=1)
"""Bounds an existing text worker's pop that returns a job: the check-in, then the job with its model and lease.

The job row, its model and its lease are written under one commit; the candidate page is unlocked by that commit.
"""

TEXT_POP_MATCH_COLD = StatementBudget(selects=31, inserts=3, updates=4, deletes=1, commits=4, rollbacks=1)
"""Bounds a new text worker's first pop that returns a job, which adds registration to the warm case."""

SUBMIT = StatementBudget(selects=8, inserts=7, updates=3, deletes=0, commits=5, rollbacks=0)
"""Keeps a successful image or text submit at five commits, for a worker's first job and a repeat alike.

The commits are the fulfilment statistics, the completion with its settlement, the worker
performance sample, the request completion with the reservation release, and the generation
statistic.
"""

IMAGE_CHECK_COLD = StatementBudget(selects=9, inserts=0, updates=0, deletes=0, commits=0, rollbacks=1)
"""Keeps the first image check read-only; it also evaluates worker availability and counts active workers."""

IMAGE_CHECK_WARM = StatementBudget(selects=3, inserts=0, updates=0, deletes=0, commits=0, rollbacks=1)
"""Bounds an image check after the response cache expired, with worker availability and counts cached in Redis."""

IMAGE_CHECK_CACHED = StatementBudget(selects=0, inserts=0, updates=0, deletes=0, commits=0, rollbacks=0)
"""Keeps an image check inside the one-second response cache away from the database."""

IMAGE_STATUS_COLD = StatementBudget(selects=9, inserts=0, updates=0, deletes=0, commits=0, rollbacks=1)
"""Keeps the first full image status read-only; it also evaluates worker availability and counts active workers."""

IMAGE_STATUS_WARM = StatementBudget(selects=4, inserts=0, updates=0, deletes=0, commits=0, rollbacks=1)
"""Bounds a repeat full image status, with worker availability and counts cached in Redis.

It reads the request's models once more than the warm check, to look for a legacy model name.
"""

TEXT_STATUS_COLD = StatementBudget(selects=7, inserts=0, updates=0, deletes=0, commits=0, rollbacks=1)
"""Keeps the first text status read-only; it also evaluates worker availability."""

TEXT_STATUS_WARM = StatementBudget(selects=3, inserts=0, updates=0, deletes=0, commits=0, rollbacks=1)
"""Bounds a repeat text status, with worker availability cached in Redis."""


@pytest.fixture(autouse=True)
def _isolated_hot_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Disable the rate limiter and replace calls that would leave the process.

    Nothing is transferred in these tests, so presigned URLs need no object storage.
    """
    from horde.apis.v2 import base
    from horde.classes.stable import processing_generation, waiting_prompt
    from horde.limiter import limiter

    def presigned_url(procgen_id: str, shared: bool = False) -> str:
        return f"https://example.invalid/{procgen_id}"

    monkeypatch.setattr(waiting_prompt, "generate_procgen_upload_url", presigned_url)
    monkeypatch.setattr(processing_generation, "generate_procgen_download_url", presigned_url)
    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    monkeypatch.setattr(base, "send_problem_user_notification", lambda *_: None)
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@dataclass(frozen=True)
class _Actors:
    """Represents the requester and the worker owner used by one test."""

    requester: ApiUser
    """A funded user who submits the requests."""
    worker_owner: ApiUser
    """A trusted user who runs the workers, so trusted-only requests reach them and no IP check runs."""


@pytest.fixture
def actors(make_api_user: MakeApiUser, settle_kudos: Callable[[], int]) -> _Actors:
    """Return a funded requester and a trusted worker owner with settled balances."""
    requester = make_api_user(kudos=10000)
    worker_owner = make_api_user(trusted=True)
    settle_kudos()
    return _Actors(requester=requester, worker_owner=worker_owner)


class _RequestTracker:
    """Cancels every request admitted during a test, so later pops in the module see an empty queue."""

    def __init__(self, client: FlaskClient) -> None:
        self._client: FlaskClient = client
        """The test client that sends the cancellations."""
        self._status_paths: list[tuple[str, str]] = []
        """Each admitted request's status path and the API key that submitted it."""

    def admitted(self, status_path: str, api_key: str) -> None:
        """Remember a request to cancel through its status path."""
        self._status_paths.append((status_path, api_key))

    def cancel_all(self) -> None:
        """Cancel every remembered request; a finished request is left as it is."""
        for status_path, api_key in self._status_paths:
            self._client.delete(status_path, headers=_headers(api_key))


@pytest.fixture
def tracker(client: FlaskClient) -> Iterator[_RequestTracker]:
    """Yield a tracker that cancels the test's requests at teardown."""
    request_tracker = _RequestTracker(client)
    yield request_tracker
    request_tracker.cancel_all()


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _worker_name() -> str:
    return f"budget-worker-{uuid.uuid4().hex[:12]}"


def _image_pop_body(worker_name: str) -> dict[str, Any]:
    return {
        "name": worker_name,
        "models": [IMAGE_MODEL],
        "bridge_agent": IMAGE_WORKER_AGENT,
        "nsfw": True,
        "amount": 1,
        "max_pixels": 4194304,
        "allow_unsafe_ipaddr": True,
    }


def _text_pop_body(worker_name: str) -> dict[str, Any]:
    return {
        "name": worker_name,
        "models": [TEXT_MODEL],
        "bridge_agent": AGENT,
        "amount": 1,
        "max_context_length": 4096,
        "max_length": 512,
    }


def _reset_caches_to_cold() -> None:
    """Empty the Redis and response caches, then store the values the quorum node's threads keep in Redis.

    In production those threads rewrite the prioritized queues, queue positions and
    average worker performances every few seconds, so a request handler reads them from
    Redis rather than querying for them. Caches filled by the request handlers stay empty.
    """
    from horde import horde_redis as horde_redis_module
    from horde.database import functions
    from horde.database.threads import store_prioritized_wp_queue, store_worker_performances
    from horde.flask import cache

    redis_client = horde_redis_module.horde_redis.horde_r
    assert redis_client is not None
    redis_client.flushdb()
    cache.clear()
    store_prioritized_wp_queue()
    store_worker_performances()
    # Each process rereads the stored queue at most once per second; this makes the next read take the new copy.
    for wp_type in functions._wp_queue_positions_time:
        functions._wp_queue_positions_time[wp_type] = 0.0


def _backdate_job(job_id: str) -> None:
    """Move a job's start back by ``JOB_DURATION`` so its submit reports a plausible speed."""
    from sqlalchemy import update

    from horde.classes.base.processing_generation import ProcessingGeneration
    from horde.flask import db

    db.session.execute(
        update(ProcessingGeneration).where(ProcessingGeneration.id == job_id).values(start_time=datetime.utcnow() - JOB_DURATION),
    )
    db.session.commit()
    db.session.remove()


def _measure(assert_query_count: QueryCounter, send: Callable[[], TestResponse]) -> tuple[TestResponse, _QueryRecorder]:
    with assert_query_count() as queries:
        response = send()
    return response, queries


def _assert_within(queries: _QueryRecorder, budget: StatementBudget) -> None:
    overruns = budget.overruns(queries)
    assert not overruns, "\n".join([*overruns, queries.describe()])


def _admit_image(client: FlaskClient, tracker: _RequestTracker, requester: ApiUser) -> TestResponse:
    response = client.post("/api/v2/generate/async", json=IMAGE_REQUEST, headers=_headers(requester.api_key))
    assert response.status_code == 202, response.get_data(as_text=True)
    tracker.admitted(f"/api/v2/generate/status/{response.get_json()['id']}", requester.api_key)
    return response


def _admit_text(client: FlaskClient, tracker: _RequestTracker, requester: ApiUser) -> TestResponse:
    response = client.post("/api/v2/generate/text/async", json=TEXT_REQUEST, headers=_headers(requester.api_key))
    assert response.status_code == 202, response.get_data(as_text=True)
    tracker.admitted(f"/api/v2/generate/text/status/{response.get_json()['id']}", requester.api_key)
    return response


def _pop(client: FlaskClient, path: str, body: dict[str, Any], owner: ApiUser) -> TestResponse:
    response = client.post(path, json=body, headers=_headers(owner.api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


def _pop_image(client: FlaskClient, worker_name: str, owner: ApiUser) -> TestResponse:
    return _pop(client, "/api/v2/generate/pop", _image_pop_body(worker_name), owner)


def _pop_text(client: FlaskClient, worker_name: str, owner: ApiUser) -> TestResponse:
    return _pop(client, "/api/v2/generate/text/pop", _text_pop_body(worker_name), owner)


def test_image_admission_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """A source-free image admission returning 202 stays within its cold and warm budgets."""
    _pop_image(client, _worker_name(), actors.worker_owner)
    _reset_caches_to_cold()

    _, cold = _measure(assert_query_count, lambda: _admit_image(client, tracker, actors.requester))
    _, warm = _measure(assert_query_count, lambda: _admit_image(client, tracker, actors.requester))

    _assert_within(cold, IMAGE_ADMISSION_COLD)
    _assert_within(warm, IMAGE_ADMISSION_WARM)


def test_text_admission_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """A text admission returning 202 stays within its budget, cold and warm."""
    _pop_text(client, _worker_name(), actors.worker_owner)
    _reset_caches_to_cold()

    _, cold = _measure(assert_query_count, lambda: _admit_text(client, tracker, actors.requester))
    _, warm = _measure(assert_query_count, lambda: _admit_text(client, tracker, actors.requester))

    _assert_within(cold, TEXT_ADMISSION)
    _assert_within(warm, TEXT_ADMISSION)


def test_image_pop_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """Image pops with and without a job, by a new and an existing worker, stay within their budgets."""
    worker_name = _worker_name()
    _reset_caches_to_cold()
    empty_cold_response, empty_cold = _measure(assert_query_count, lambda: _pop_image(client, worker_name, actors.worker_owner))
    empty_warm_response, empty_warm = _measure(assert_query_count, lambda: _pop_image(client, worker_name, actors.worker_owner))
    _admit_image(client, tracker, actors.requester)
    match_warm_response, match_warm = _measure(assert_query_count, lambda: _pop_image(client, worker_name, actors.worker_owner))
    _admit_image(client, tracker, actors.requester)
    new_worker_name = _worker_name()
    _reset_caches_to_cold()
    match_cold_response, match_cold = _measure(assert_query_count, lambda: _pop_image(client, new_worker_name, actors.worker_owner))

    assert empty_cold_response.get_json()["id"] is None
    assert empty_warm_response.get_json()["id"] is None
    assert match_warm_response.get_json()["id"] is not None, match_warm_response.get_json()
    assert match_cold_response.get_json()["id"] is not None, match_cold_response.get_json()
    _assert_within(empty_cold, IMAGE_POP_EMPTY_COLD)
    _assert_within(empty_warm, IMAGE_POP_EMPTY_WARM)
    _assert_within(match_warm, IMAGE_POP_MATCH_WARM)
    _assert_within(match_cold, IMAGE_POP_MATCH_COLD)


def test_text_pop_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """Text pops with and without a job, by a new and an existing worker, stay within their budgets."""
    worker_name = _worker_name()
    _reset_caches_to_cold()
    empty_cold_response, empty_cold = _measure(assert_query_count, lambda: _pop_text(client, worker_name, actors.worker_owner))
    empty_warm_response, empty_warm = _measure(assert_query_count, lambda: _pop_text(client, worker_name, actors.worker_owner))
    _admit_text(client, tracker, actors.requester)
    match_warm_response, match_warm = _measure(assert_query_count, lambda: _pop_text(client, worker_name, actors.worker_owner))
    _admit_text(client, tracker, actors.requester)
    new_worker_name = _worker_name()
    _reset_caches_to_cold()
    match_cold_response, match_cold = _measure(assert_query_count, lambda: _pop_text(client, new_worker_name, actors.worker_owner))

    assert empty_cold_response.get_json()["id"] is None
    assert empty_warm_response.get_json()["id"] is None
    assert match_warm_response.get_json()["id"] is not None, match_warm_response.get_json()
    assert match_cold_response.get_json()["id"] is not None, match_cold_response.get_json()
    _assert_within(empty_cold, TEXT_POP_EMPTY_COLD)
    _assert_within(empty_warm, TEXT_POP_EMPTY_WARM)
    _assert_within(match_warm, TEXT_POP_MATCH_WARM)
    _assert_within(match_cold, TEXT_POP_MATCH_COLD)


def test_image_pop_statements_do_not_grow_with_skipped_candidates(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """A pop that skips five queued requests on one page issues as many statements as one that skips one.

    The worker's blacklist rejects each request after the page is loaded, so a per-candidate
    query would show up as growth here.
    """
    pop_body = {**_image_pop_body(_worker_name()), "blacklist": ["robots"]}
    _pop(client, "/api/v2/generate/pop", pop_body, actors.worker_owner)
    for _ in range(SKIPPED_CANDIDATES_FEW):
        _admit_image(client, tracker, actors.requester)
    few_response, few = _measure(assert_query_count, lambda: _pop(client, "/api/v2/generate/pop", pop_body, actors.worker_owner))
    for _ in range(SKIPPED_CANDIDATES_MANY - SKIPPED_CANDIDATES_FEW):
        _admit_image(client, tracker, actors.requester)
    many_response, many = _measure(assert_query_count, lambda: _pop(client, "/api/v2/generate/pop", pop_body, actors.worker_owner))

    assert few_response.get_json()["skipped"]["blacklist"] == SKIPPED_CANDIDATES_FEW
    assert many_response.get_json()["skipped"]["blacklist"] == SKIPPED_CANDIDATES_MANY
    assert len(many) == len(few), many.describe()
    assert len(many.transactions) == len(few.transactions), many.describe()


def _submit_after_pop(
    client: FlaskClient,
    assert_query_count: QueryCounter,
    *,
    pop: Callable[[], TestResponse],
    submit_path: str,
    generation: str,
    owner: ApiUser,
) -> _QueryRecorder:
    job_id = pop().get_json()["id"]
    assert job_id is not None
    _backdate_job(job_id)
    submit_body = {"id": job_id, "generation": generation, "state": "ok", "seed": 0}
    response, queries = _measure(assert_query_count, lambda: client.post(submit_path, json=submit_body, headers=_headers(owner.api_key)))
    assert response.status_code == 200, response.get_data(as_text=True)
    return queries


def test_image_submit_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """A successful image submit stays within its budget for a worker's first and a repeat job."""
    worker_name = _worker_name()
    for _ in range(2):
        _admit_image(client, tracker, actors.requester)
        queries = _submit_after_pop(
            client,
            assert_query_count,
            pop=lambda: _pop_image(client, worker_name, actors.worker_owner),
            submit_path="/api/v2/generate/submit",
            generation="R2",
            owner=actors.worker_owner,
        )
        _assert_within(queries, SUBMIT)


def test_text_submit_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """A successful text submit stays within its budget for a worker's first and a repeat job."""
    worker_name = _worker_name()
    for _ in range(2):
        _admit_text(client, tracker, actors.requester)
        queries = _submit_after_pop(
            client,
            assert_query_count,
            pop=lambda: _pop_text(client, worker_name, actors.worker_owner),
            submit_path="/api/v2/generate/text/submit",
            generation="test ",
            owner=actors.worker_owner,
        )
        _assert_within(queries, SUBMIT)


def test_image_check_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """An image check stays within its cold budget, its second read is a response cache hit, and a later read is warm."""
    from horde.flask import cache

    _pop_image(client, _worker_name(), actors.worker_owner)
    request_id = _admit_image(client, tracker, actors.requester).get_json()["id"]
    _reset_caches_to_cold()

    check_path = f"/api/v2/generate/check/{request_id}"
    headers = _headers(actors.requester.api_key)

    _, cold = _measure(assert_query_count, lambda: client.get(check_path, headers=headers))
    _, cached = _measure(assert_query_count, lambda: client.get(check_path, headers=headers))
    cache.clear()
    _, warm = _measure(assert_query_count, lambda: client.get(check_path, headers=headers))

    _assert_within(cold, IMAGE_CHECK_COLD)
    _assert_within(cached, IMAGE_CHECK_CACHED)
    _assert_within(warm, IMAGE_CHECK_WARM)


def test_image_status_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """The full image status stays within its budget on the first and a repeat read."""
    _pop_image(client, _worker_name(), actors.worker_owner)
    request_id = _admit_image(client, tracker, actors.requester).get_json()["id"]
    _reset_caches_to_cold()

    status_path = f"/api/v2/generate/status/{request_id}"
    headers = _headers(actors.requester.api_key)

    _, cold = _measure(assert_query_count, lambda: client.get(status_path, headers=headers))
    _, warm = _measure(assert_query_count, lambda: client.get(status_path, headers=headers))

    _assert_within(cold, IMAGE_STATUS_COLD)
    _assert_within(warm, IMAGE_STATUS_WARM)


def test_text_status_stays_within_budget(
    client: FlaskClient,
    actors: _Actors,
    tracker: _RequestTracker,
    assert_query_count: QueryCounter,
) -> None:
    """The text status stays within its budget on the first and a repeat read."""
    _pop_text(client, _worker_name(), actors.worker_owner)
    request_id = _admit_text(client, tracker, actors.requester).get_json()["id"]
    _reset_caches_to_cold()

    status_path = f"/api/v2/generate/text/status/{request_id}"
    headers = _headers(actors.requester.api_key)

    _, cold = _measure(assert_query_count, lambda: client.get(status_path, headers=headers))
    _, warm = _measure(assert_query_count, lambda: client.get(status_path, headers=headers))

    _assert_within(cold, TEXT_STATUS_COLD)
    _assert_within(warm, TEXT_STATUS_WARM)
