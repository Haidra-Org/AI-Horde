# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify how a worker's pop scans the queue and claims the request it assigns.

The scan reads candidate pages without row locks, leaving out the requests already read. A request that passes the
eligibility rules is claimed with a row lock, and the claim succeeds only if the request still waits for a worker.
The tests drive the pop resource against PostgreSQL with the eligibility verdicts and the assignment stubbed, so each
test sets which requests are eligible and checks which request is claimed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from horde.apis.v2.kobold import TextJobPop
from horde.apis.v2.stable import ImageJobPop
from horde.classes.base.user import User
from horde.classes.base.waiting_prompt import WaitingPrompt, WPTrickedWorkers
from horde.classes.base.worker import EligibilitySkipReason, EligibilityVerdict, WorkerModel
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.database import functions as database
from horde.database import text_functions as text_database
from horde.flask import db

pytestmark = pytest.mark.unit

_IMAGE_MODEL = "stable_diffusion"
"""The model of every image request and image worker in these tests."""

_TEXT_MODEL = "pop-scan-text-model"
"""The model of every text request and text worker in these tests."""

DEEP_QUEUE_LENGTH: int = 300
"""The number of queued requests placed ahead of the end of the scan by the deep-queue tests.

It is more than the first three image page sizes combined (10 + 50 + 200), so the scan reaches a repeated page size.
"""

DEEP_SCAN_STATEMENT_BUDGET: int = 20
"""The statement limit for a pop that reads ``DEEP_QUEUE_LENGTH`` ineligible requests, set to the measured value.

Each page with rows is one candidate SELECT and three collection loads. An image pop through this queue reads an
empty priority page and four general pages, which is 17 statements. The other three read the worker's models and
its owner's trust, and the worker's messages.
"""


@dataclass
class _EligibilityStub:
    """Represents eligibility verdicts that accept only the listed requests, recording each request evaluated."""

    eligible_ids: Collection[uuid.UUID]
    """The requests accepted by the stub."""
    before_verdict: Callable[[Any], None] | None = None
    """Called with each request before its verdict is returned."""
    evaluated_ids: list[uuid.UUID] = field(default_factory=list)
    """The id of each request evaluated, in evaluation order."""

    def __call__(self, waiting_prompt: Any, request_inputs: Any, worker_inputs: Any) -> EligibilityVerdict:
        self.evaluated_ids.append(waiting_prompt.id)
        if self.before_verdict is not None:
            self.before_verdict(waiting_prompt)
        if waiting_prompt.id in self.eligible_ids:
            return EligibilityVerdict.accepted()
        return EligibilityVerdict.refused(EligibilitySkipReason.MODELS)


@dataclass
class _AssignmentStub:
    """Represents an assignment that records the claimed request and commits, as a real assignment does."""

    assignments: list[tuple[uuid.UUID, bool]] = field(default_factory=list)
    """Each assigned request's id and whether it was selected from the priority queue."""

    def __call__(self, waiting_prompt: Any, *, selected_from_priority_queue: bool = False) -> dict[str, Any]:
        self.assignments.append((waiting_prompt.id, selected_from_priority_queue))
        db.session.commit()
        job_id = f"job-{waiting_prompt.id}"
        return {"id": job_id, "ids": [job_id]}


@contextmanager
def _other_session() -> Iterator[Session]:
    """Yield a session on its own connection, standing in for another worker's pop or a queue writer."""
    session = Session(db.engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _make_image_worker(owner: User) -> ImageWorker:
    worker = ImageWorker(
        user_id=owner.id,
        name=f"pop-scan-{uuid.uuid4().hex[:12]}",
        max_pixels=512 * 512,
        last_check_in=datetime.utcnow(),
        speed=1_000_000,
        nsfw=True,
        allow_unsafe_ipaddr=True,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=_IMAGE_MODEL))
    db.session.commit()
    return worker


def _make_text_worker(owner: User) -> TextWorker:
    worker = TextWorker(
        user_id=owner.id,
        name=f"pop-scan-{uuid.uuid4().hex[:12]}",
        max_length=512,
        max_context_length=4096,
        last_check_in=datetime.utcnow(),
        speed=100,
        nsfw=True,
        allow_unsafe_ipaddr=True,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=_TEXT_MODEL))
    db.session.commit()
    return worker


def _queue_requests(
    make_request: Callable[[], WaitingPrompt],
    count: int,
    *,
    extra_priority: int = 0,
    first_created: datetime | None = None,
) -> list[WaitingPrompt]:
    """Queue ``count`` active requests at one priority, each created a second after the one before.

    Args:
        make_request: Builds one inactive request.
        count: How many requests to queue.
        extra_priority: The queue priority of every request.
        first_created: The creation time of the first request. By default, an hour ago.

    Returns:
        The requests, in queue order.
    """
    created = first_created or datetime.utcnow() - timedelta(hours=1)
    requests = []
    for index in range(count):
        request = make_request()
        request.active = True
        request.expiry = datetime.utcnow() + timedelta(minutes=30)
        request.extra_priority = extra_priority
        request.created = created + timedelta(seconds=index)
        requests.append(request)
    db.session.commit()
    return requests


def _image_request_factory(requester: User) -> Callable[[], WaitingPrompt]:
    def make_request() -> WaitingPrompt:
        return ImageWaitingPrompt(
            worker_ids=[],
            models=[_IMAGE_MODEL],
            prompt="pop scan test",
            user_id=requester.id,
            params={"n": 1, "width": 512, "height": 512, "steps": 10, "sampler_name": "k_euler_a"},
        )

    return make_request


def _text_request_factory(requester: User) -> Callable[[], WaitingPrompt]:
    def make_request() -> WaitingPrompt:
        request = TextWaitingPrompt(
            worker_ids=[],
            models=[_TEXT_MODEL],
            prompt="pop scan test",
            user_id=requester.id,
            params={"n": 1, "max_length": 80, "max_context_length": 2048},
        )
        request.validated_backends = False
        return request

    return make_request


def _prepare_pop(
    pop: Any,
    *,
    owner: User,
    worker: Any,
    model_name: str,
    eligibility: _EligibilityStub,
    monkeypatch: pytest.MonkeyPatch,
    priority_usernames: Sequence[str] = (),
) -> _AssignmentStub:
    """Set up a pop resource with validation and check-in done, and its verdicts and assignment stubbed.

    Returns:
        The stub that records the assignment.
    """
    pop.args = SimpleNamespace(priority_usernames=list(priority_usernames), models=[model_name], amount=1)
    pop.user = owner
    pop.worker = worker
    pop.models = [model_name]
    pop.blacklist = []
    pop.skipped = {}
    monkeypatch.setattr(pop, "validate", lambda: None)
    monkeypatch.setattr(pop, "check_in", lambda: None)
    monkeypatch.setattr(worker, "can_generate_with_eligibility_inputs", eligibility)
    assignment = _AssignmentStub()
    monkeypatch.setattr(pop, "start_worker", assignment)
    return assignment


def _run_pop(app: Any, pop: Any) -> dict[str, Any]:
    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, status = pop._post_inner()
    assert status == 200
    pop_response: dict[str, Any] = response
    return pop_response


def _after_general_page(pop: Any, page: int, action: Callable[[list[Any]], None], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``action`` with the rows of general scan page ``page`` once the pop has read them."""
    read_page = pop.get_sorted_wp

    def read_page_then_act(priority_user_ids: Any = None, *, page_size: int | None = None) -> list[Any]:
        page_rows: list[Any] = read_page(priority_user_ids, page_size=page_size)
        if priority_user_ids is None and pop.wp_page == page:
            action(page_rows)
        return page_rows

    monkeypatch.setattr(pop, "get_sorted_wp", read_page_then_act)


def test_two_pops_cannot_claim_the_same_request_and_the_second_takes_the_next_one(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While another session holds a request's claim, a pop's claim of it returns None and the pop assigns the next
    eligible request."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    first, second = _queue_requests(_image_request_factory(requester), 2)
    eligibility = _EligibilityStub(eligible_ids={first.id, second.id})
    pop = ImageJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)

    with _other_session() as other_pop_session:
        other_pop_session.query(ImageWaitingPrompt).filter(ImageWaitingPrompt.id == first.id).with_for_update().one()
        assert database.claim_first_waiting_prompt([first], worker_id=worker.id) is None
        db.session.rollback()
        _run_pop(app, pop)

    assert assignment.assignments == [(second.id, False)]
    assert eligibility.evaluated_ids == [first.id, second.id]


@pytest.mark.parametrize(
    "change",
    ["drained", "faulted", "expired", "deactivated", "tricked"],
)
def test_a_request_changed_between_the_scan_and_the_claim_is_not_assigned(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A request that stops waiting for this worker after the scan read it is passed over at the claim, and the pop
    assigns the next eligible request."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    changed, fallback = _queue_requests(_image_request_factory(requester), 2)
    column_changes: dict[str, dict[Any, Any]] = {
        "drained": {ImageWaitingPrompt.n: 0},
        "faulted": {ImageWaitingPrompt.faulted: True},
        "expired": {ImageWaitingPrompt.expiry: datetime.utcnow() - timedelta(seconds=1)},
        "deactivated": {ImageWaitingPrompt.active: False},
    }

    def change_request(waiting_prompt: Any) -> None:
        if waiting_prompt.id != changed.id:
            return
        with _other_session() as writer_session:
            if change == "tricked":
                writer_session.add(WPTrickedWorkers(wp_id=changed.id, worker_id=worker.id))
            else:
                writer_session.execute(update(ImageWaitingPrompt).where(ImageWaitingPrompt.id == changed.id).values(column_changes[change]))
            writer_session.commit()

    eligibility = _EligibilityStub(eligible_ids={changed.id, fallback.id}, before_verdict=change_request)
    pop = ImageJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)

    _run_pop(app, pop)

    assert assignment.assignments == [(fallback.id, False)]


@pytest.mark.parametrize("moved_rows", ["every_row", "alternate_rows"])
def test_a_priority_change_between_pages_neither_skips_nor_repeats_a_request(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    moved_rows: str,
) -> None:
    """When the priority thread raises ``extra_priority`` between two pages of a scan, for every queued request or
    for only some of them, the pop still evaluates every request exactly once."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    requests = _queue_requests(_image_request_factory(requester), 70)
    moved_ids = [request.id for index, request in enumerate(requests) if moved_rows == "every_row" or index % 2 == 1]
    eligibility = _EligibilityStub(eligible_ids=set())
    pop = ImageJobPop()
    _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)

    def raise_priority(_page_rows: list[Any]) -> None:
        with _other_session() as priority_thread_session:
            priority_thread_session.execute(
                update(ImageWaitingPrompt)
                .where(ImageWaitingPrompt.id.in_(moved_ids))
                .values({ImageWaitingPrompt.extra_priority: ImageWaitingPrompt.extra_priority + 50}),
            )
            priority_thread_session.commit()

    _after_general_page(pop, 0, raise_priority, monkeypatch)

    _run_pop(app, pop)

    assert len(eligibility.evaluated_ids) == len(set(eligibility.evaluated_ids))
    assert set(eligibility.evaluated_ids) == {request.id for request in requests}


def test_a_pop_through_a_deep_queue_without_a_match_takes_few_statements_and_no_row_locks(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    assert_query_count: Any,
) -> None:
    """A pop that refuses every request in a deep queue reads it in a few growing pages and does not lock any of its rows,
    so another session can lock a request already read by the scan."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    requests = _queue_requests(_image_request_factory(requester), DEEP_QUEUE_LENGTH)
    eligibility = _EligibilityStub(eligible_ids=set())
    pop = ImageJobPop()
    _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)
    locked_by_other_session: list[uuid.UUID] = []

    def lock_a_read_row(page_rows: list[Any]) -> None:
        with _other_session() as other_pop_session:
            locked_row = (
                other_pop_session.query(ImageWaitingPrompt.id)
                .filter(ImageWaitingPrompt.id == page_rows[0].id)
                .with_for_update(nowait=True)
                .one()
            )
            locked_by_other_session.append(locked_row.id)

    _after_general_page(pop, 0, lock_a_read_row, monkeypatch)

    with assert_query_count() as queries:
        response = _run_pop(app, pop)

    # The recorder records statements from every connection. The other session's lock is the one NOWAIT statement.
    pop_statements = [statement for statement in queries.statements if "NOWAIT" not in statement.upper()]
    assert response["ids"] == []
    assert locked_by_other_session == [requests[0].id]
    assert sorted(map(str, eligibility.evaluated_ids)) == sorted(str(request.id) for request in requests)
    assert not [statement for statement in pop_statements if "FOR UPDATE" in statement.upper()], queries.describe()
    # The priority page and the general pages of 10, 50, 200 and the remaining 40 requests, with the other session's
    # lock read of the same table.
    assert queries.table_counts("SELECT")["waiting_prompts"] == 6, queries.describe()
    assert len(pop_statements) <= DEEP_SCAN_STATEMENT_BUDGET, queries.describe()


def test_a_request_only_this_worker_can_serve_is_found_deep_in_the_queue(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan's depth is not limited, so a request behind many requests refused by the worker is still assigned."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    _queue_requests(_image_request_factory(requester), DEEP_QUEUE_LENGTH, extra_priority=100)
    (deep_request,) = _queue_requests(_image_request_factory(requester), 1)
    eligibility = _EligibilityStub(eligible_ids={deep_request.id})
    pop = ImageJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)

    _run_pop(app, pop)

    assert assignment.assignments == [(deep_request.id, False)]
    assert len(eligibility.evaluated_ids) == DEEP_QUEUE_LENGTH + 1


@pytest.mark.parametrize("priority_request_eligible", [True, False], ids=["eligible", "refused"])
def test_a_priority_users_request_is_evaluated_first_and_once(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    priority_request_eligible: bool,
) -> None:
    """A priority user's request is evaluated before the general queue, even at the lowest priority, is assigned as
    selected from the priority queue, and is not evaluated again by the general scan."""
    owner, requester, priority_user = make_user(), make_user(), make_user()
    worker = _make_image_worker(owner)
    (general_request,) = _queue_requests(_image_request_factory(requester), 1, extra_priority=1000)
    (priority_request,) = _queue_requests(_image_request_factory(priority_user), 1, extra_priority=-100)
    eligible_ids = {general_request.id, priority_request.id} if priority_request_eligible else {general_request.id}
    eligibility = _EligibilityStub(eligible_ids=eligible_ids)
    pop = ImageJobPop()
    assignment = _prepare_pop(
        pop,
        owner=owner,
        worker=worker,
        model_name=_IMAGE_MODEL,
        eligibility=eligibility,
        monkeypatch=monkeypatch,
        priority_usernames=[priority_user.get_unique_alias()],
    )

    _run_pop(app, pop)

    assert eligibility.evaluated_ids[0] == priority_request.id
    assert eligibility.evaluated_ids.count(priority_request.id) == 1
    if priority_request_eligible:
        assert assignment.assignments == [(priority_request.id, True)]
    else:
        assert assignment.assignments == [(general_request.id, False)]


def test_a_text_pop_reads_a_deep_queue_once_and_finds_the_request_at_its_end(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    assert_query_count: Any,
) -> None:
    """The text pop pages through a deep queue without row locks, evaluates each request once, and claims the one
    eligible request at the end of the queue."""
    owner, requester = make_user(), make_user()
    worker = _make_text_worker(owner)
    refused_requests = _queue_requests(_text_request_factory(requester), DEEP_QUEUE_LENGTH, extra_priority=100)
    (deep_request,) = _queue_requests(_text_request_factory(requester), 1)
    eligibility = _EligibilityStub(eligible_ids={deep_request.id})
    pop = TextJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_TEXT_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)

    with assert_query_count() as queries:
        _run_pop(app, pop)

    assert assignment.assignments == [(deep_request.id, False)]
    assert sorted(map(str, eligibility.evaluated_ids)) == sorted(str(request.id) for request in [*refused_requests, deep_request])
    scan_and_claim_locks = [statement for statement in queries.statements if "FOR UPDATE" in statement.upper()]
    assert len(scan_and_claim_locks) == 1, queries.describe()
    # The priority page, the general pages of 3, 10, 50, 200 and the remaining 38 requests, and the claim.
    assert queries.table_counts("SELECT")["waiting_prompts"] == 7, queries.describe()


def test_a_text_claim_keeps_the_scanned_request_and_its_chat_images_expression(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    assert_query_count: Any,
) -> None:
    """The text claim returns the instance loaded by the scan, does not reload its columns, and leaves the
    ``carries_chat_images`` value computed by the scan in place."""
    owner, requester = make_user(), make_user()
    worker = _make_text_worker(owner)
    (request,) = _queue_requests(_text_request_factory(requester), 1)
    request.messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}]}]
    db.session.commit()
    (scanned,) = text_database.get_sorted_text_wp_filtered_to_worker(worker, [_TEXT_MODEL])
    assert scanned.carries_chat_images is True

    with assert_query_count() as queries:
        claimed = database.claim_first_waiting_prompt([scanned], worker_id=worker.id)

    assert claimed is scanned
    assert claimed.carries_chat_images is True
    (claim_statement,) = [statement for statement in queries.statements if "FOR UPDATE" in statement.upper()]
    assert "waiting_prompts.prompt" not in claim_statement, claim_statement
    assert "waiting_prompts.wp_type IN" in claim_statement, claim_statement
    assert "FOR UPDATE OF waiting_prompts SKIP LOCKED" in claim_statement, claim_statement
    db.session.commit()


@dataclass
class _ClaimMissRecorder:
    """Represents the claim-miss counter, recording each amount added."""

    amounts: list[int] = field(default_factory=list)
    """Each amount added, in order."""

    def add(self, amount: int, attributes: Any = None) -> None:
        self.amounts.append(amount)


def _lock_rows(session: Session, waiting_prompts: Sequence[WaitingPrompt]) -> None:
    """Lock the rows of ``waiting_prompts`` in ``session``, standing in for other pops' claims."""
    session.query(ImageWaitingPrompt.id).filter(ImageWaitingPrompt.id.in_([wp.id for wp in waiting_prompts])).with_for_update().all()


@pytest.mark.parametrize(
    ("eligible_count", "locked_count", "claim_statements"),
    [(5, 3, 1), (database.POP_CLAIM_BATCH_SIZE + 1, database.POP_CLAIM_BATCH_SIZE, 2)],
    ids=["within_one_batch", "past_a_full_batch"],
)
def test_requests_locked_by_other_pops_cost_one_claim_statement_per_batch(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    assert_query_count: Any,
    eligible_count: int,
    locked_count: int,
    claim_statements: int,
) -> None:
    """With the first eligible requests locked by other sessions, the pop assigns the first free one with one claim
    statement per batch of eligible requests, and counts each locked request as a claim miss."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    requests = _queue_requests(_image_request_factory(requester), eligible_count)
    eligibility = _EligibilityStub(eligible_ids={request.id for request in requests})
    pop = ImageJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)
    claim_misses = _ClaimMissRecorder()
    monkeypatch.setattr("horde.apis.v2.base.pop_claim_misses", claim_misses)

    with _other_session() as other_pops_session:
        _lock_rows(other_pops_session, requests[:locked_count])
        with assert_query_count() as queries:
            _run_pop(app, pop)

    assert assignment.assignments == [(requests[locked_count].id, False)]
    assert sum(claim_misses.amounts) == locked_count
    claims = [statement for statement in queries.statements if "SKIP LOCKED" in statement.upper()]
    assert len(claims) == claim_statements, queries.describe()


def test_a_refused_assignment_claims_again_from_the_requests_after_the_claimed_one(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the first eligible request locked by another session, the claim takes the second. When its assignment is
    refused, the pop claims again from the requests after it and assigns the third."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    locked, refused, assigned = _queue_requests(_image_request_factory(requester), 3)
    eligibility = _EligibilityStub(eligible_ids={locked.id, refused.id, assigned.id})
    pop = ImageJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)
    started_ids: list[uuid.UUID] = []

    def refuse_first_assignment(waiting_prompt: Any, *, selected_from_priority_queue: bool = False) -> dict[str, Any] | None:
        started_ids.append(waiting_prompt.id)
        if len(started_ids) == 1:
            db.session.commit()
            return None
        return assignment(waiting_prompt, selected_from_priority_queue=selected_from_priority_queue)

    monkeypatch.setattr(pop, "start_worker", refuse_first_assignment)
    claim_misses = _ClaimMissRecorder()
    monkeypatch.setattr("horde.apis.v2.base.pop_claim_misses", claim_misses)

    with _other_session() as other_pop_session:
        _lock_rows(other_pop_session, [locked])
        _run_pop(app, pop)

    assert started_ids == [refused.id, assigned.id]
    assert assignment.assignments == [(assigned.id, False)]
    assert claim_misses.amounts == [1]


def test_a_pop_whose_eligible_requests_are_all_locked_reads_on_and_returns_no_job(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every eligible request is locked by another session, the pop counts a miss for each, reads the next page,
    and returns the empty response at the end of the queue."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    first_page_size = database.POP_SCAN_PAGE_SIZES[0]
    requests = _queue_requests(_image_request_factory(requester), first_page_size + 2)
    eligibility = _EligibilityStub(eligible_ids={request.id for request in requests})
    pop = ImageJobPop()
    assignment = _prepare_pop(pop, owner=owner, worker=worker, model_name=_IMAGE_MODEL, eligibility=eligibility, monkeypatch=monkeypatch)
    claim_misses = _ClaimMissRecorder()
    monkeypatch.setattr("horde.apis.v2.base.pop_claim_misses", claim_misses)

    with _other_session() as other_pops_session:
        _lock_rows(other_pops_session, requests)
        response = _run_pop(app, pop)

    assert response["ids"] == []
    assert assignment.assignments == []
    assert sorted(map(str, eligibility.evaluated_ids)) == sorted(str(request.id) for request in requests)
    assert sum(claim_misses.amounts) == len(requests)


def test_a_claim_reads_the_jobs_left_and_expiry_as_committed_now(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """A claim takes ``n``, ``expiry`` and ``extra_priority`` as committed after the scan, so an assignment does not
    write back the scan's stale values and does not shorten an expiry extended after the scan."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    (request,) = _queue_requests(_image_request_factory(requester), 1)
    request.n = 3
    # Shorter than the default request lifetime, so an assignment that kept the scan's expiry would extend it to the
    # default and store that in place of the later expiry.
    request.expiry = datetime.utcnow() + timedelta(minutes=5)
    db.session.commit()
    (scanned,) = database.get_sorted_wp_filtered_to_worker(worker, [_IMAGE_MODEL], [])
    later_expiry = datetime.utcnow() + timedelta(hours=2)
    with _other_session() as writer_session:
        writer_session.execute(
            update(ImageWaitingPrompt)
            .where(ImageWaitingPrompt.id == request.id)
            .values({ImageWaitingPrompt.n: 2, ImageWaitingPrompt.expiry: later_expiry, ImageWaitingPrompt.extra_priority: 7}),
        )
        writer_session.commit()

    claimed = database.claim_first_waiting_prompt([scanned], worker_id=worker.id)

    assert claimed is scanned
    assert (claimed.n, claimed.expiry, claimed.extra_priority) == (2, later_expiry, 7)
    claimed.n -= 1
    claimed.refresh(worker, commit=False)
    db.session.commit()
    with _other_session() as reader_session:
        stored = reader_session.query(ImageWaitingPrompt.n, ImageWaitingPrompt.expiry).filter(ImageWaitingPrompt.id == request.id).one()
    assert (stored.n, stored.expiry) == (1, later_expiry)


def test_a_fake_generation_commits_the_tricked_worker_with_the_generation(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """A paused worker's fake generation and its tricked-worker row are committed in one transaction, so the same
    worker's next claim of the request returns None."""
    owner, requester = make_user(), make_user()
    worker = _make_image_worker(owner)
    (request,) = _queue_requests(_image_request_factory(requester), 1)
    (scanned,) = database.get_sorted_wp_filtered_to_worker(worker, [_IMAGE_MODEL], [])
    claimed = database.claim_first_waiting_prompt([scanned], worker_id=worker.id)
    assert claimed is not None
    committed_rows: list[tuple[bool, bool]] = []

    def record_committed_rows(committing_session: Session) -> None:
        fake_generation_rows = committing_session.query(ImageProcessingGeneration.id).filter(
            ImageProcessingGeneration.wp_id == request.id,
            ImageProcessingGeneration.fake.is_(True),
        )
        tricked_rows = committing_session.query(WPTrickedWorkers.id).filter(
            WPTrickedWorkers.wp_id == request.id,
            WPTrickedWorkers.worker_id == worker.id,
        )
        committed_rows.append((fake_generation_rows.count() > 0, tricked_rows.count() > 0))

    session = db.session()
    event.listen(session, "before_commit", record_committed_rows)
    try:
        claimed.fake_generation(worker)
    finally:
        event.remove(session, "before_commit", record_committed_rows)

    # Each pair is what one commit makes visible to other sessions: a fake generation and a tricked-worker row.
    assert (True, False) not in committed_rows, committed_rows
    assert committed_rows[-1] == (True, True), committed_rows
    assert database.claim_first_waiting_prompt([claimed], worker_id=worker.id) is None
    db.session.rollback()


@contextmanager
def _captured_scan_statements() -> Iterator[list[tuple[str, Any]]]:
    """Yield a list that receives each pop scan statement sent to the database, with its parameters."""
    captured: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if "ORDER BY waiting_prompts.extra_priority DESC" in statement:
            captured.append((statement, parameters))

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        yield captured
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)


def _scan_plan(statement: str, parameters: Any) -> str:
    connection = db.session.connection()
    connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
    plan_rows = connection.exec_driver_sql(f"EXPLAIN {statement}", parameters).scalars().all()
    db.session.rollback()
    return "\n".join(plan_rows)


@pytest.mark.parametrize("gentype", ["image", "text"])
def test_the_pop_scan_reads_the_partial_queue_index(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    gentype: str,
) -> None:
    """The scan's liveness clauses match the predicate of ``ix_waiting_prompts_active_queue``, so PostgreSQL can read
    the queue through that index, on the first page and on a page that leaves out requests already read."""
    owner, requester = make_user(), make_user()
    if gentype == "image":
        worker: Any = _make_image_worker(owner)
        requests = _queue_requests(_image_request_factory(requester), 3)

        def read_page(excluded_ids: Collection[Any]) -> list[Any]:
            return database.get_sorted_wp_filtered_to_worker(worker, [_IMAGE_MODEL], [], excluded_ids=excluded_ids)

    else:
        worker = _make_text_worker(owner)
        requests = _queue_requests(_text_request_factory(requester), 3)

        def read_page(excluded_ids: Collection[Any]) -> list[Any]:
            return text_database.get_sorted_text_wp_filtered_to_worker(worker, [_TEXT_MODEL], excluded_ids=excluded_ids)

    with _captured_scan_statements() as scan_statements:
        read_page(())
        read_page({requests[0].id})
    db.session.rollback()

    assert len(scan_statements) == 2
    for statement, parameters in scan_statements:
        assert "ix_waiting_prompts_active_queue" in _scan_plan(statement, parameters)
