# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regressions for loaded generation rows racing a different submit session, and for workers that pop together."""

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from tests.integration.test_maintenance_threads import AGENT, TEXT_MODEL, _headers, _queue_text_wp
from tests.integration.test_maintenance_threads import _no_rate_limit as _no_rate_limit


def _pending_job(app, client, api_key, make_api_user):
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    request_id = _queue_text_wp(client, api_key)
    owner = make_api_user(trusted=True, kudos=100)
    with app.app_context():
        worker = TextWorker(user_id=owner.id, name="lifecycle-" + uuid.uuid4().hex)
        db.session.add(worker)
        db.session.commit()
        wp = db.session.get(TextWaitingPrompt, uuid.UUID(request_id))
        wp.n = 0
        gen = TextProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model=TEXT_MODEL)
        return wp.id, gen.id


def test_worker_fault_updates_loaded_instance_and_abort_statistic(app, client, api_key, make_api_user):
    from horde.classes.kobold.genstats import TextGenerationStatistic
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.enums import ImageGenState
    from horde.flask import db

    _, generation_id = _pending_job(app, client, api_key, make_api_user)
    with app.app_context():
        gen = db.session.get(TextProcessingGeneration, generation_id)
        gen.fault_and_restart()
        assert gen.faulted is True
        stat = db.session.query(TextGenerationStatistic).order_by(TextGenerationStatistic.id.desc()).first()
        assert stat.state == ImageGenState.FAULTED


@pytest.mark.parametrize("winner", ["fault", "success"])
def test_stale_cleanup_cannot_requeue_a_job_claimed_after_it_loaded(app, client, api_key, make_api_user, winner):
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id, generation_id = _pending_job(app, client, api_key, make_api_user)
    with app.app_context():
        gen = db.session.get(TextProcessingGeneration, generation_id)
        _ = gen.wp
        with Session(db.engine) as other:
            other.execute(
                update(TextProcessingGeneration)
                .where(TextProcessingGeneration.id == generation_id)
                .values(
                    faulted=winner == "fault",
                    generation="finished" if winner == "success" else None,
                )
            )
            other.execute(update(TextWaitingPrompt).where(TextWaitingPrompt.id == request_id).values(n=int(winner == "fault")))
            other.commit()
        gen.fault_and_restart(stale=True)
        db.session.commit()
        assert db.session.query(TextWaitingPrompt.n).filter_by(id=request_id).scalar() == int(winner == "fault")
        stored = db.session.query(TextProcessingGeneration.faulted, TextProcessingGeneration.generation).filter_by(id=generation_id).one()
        assert stored == (winner == "fault", "finished" if winner == "success" else None)


@pytest.mark.parametrize("in_flight", [False, True])
def test_maintenance_cancellation_settles_after_in_flight_work_and_never_requeues(app, client, api_key, make_api_user, in_flight):
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id, generation_id = _pending_job(app, client, api_key, make_api_user)
    with app.app_context():
        wp = db.session.get(TextWaitingPrompt, request_id)
        gen = db.session.get(TextProcessingGeneration, generation_id)
        if not in_flight:
            db.session.delete(gen)
            wp.n = 1
            db.session.commit()
        wp.abort_for_maintenance()
        assert wp.read_completion_snapshot().cancelled
        assert wp.is_completed() is (not in_flight)
        if in_flight:
            gen.fault_and_restart()
            assert wp.read_completion_snapshot().n == 0
            assert wp.is_completed()


def test_maintenance_cancellation_retains_partial_results(app, client, api_key, make_api_user):
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id, generation_id = _pending_job(app, client, api_key, make_api_user)
    with app.app_context():
        wp = db.session.get(TextWaitingPrompt, request_id)
        gen = db.session.get(TextProcessingGeneration, generation_id)
        wp.jobs = 2
        wp.n = 1
        gen.generation = "partial result"
        db.session.commit()
        wp.abort_for_maintenance()
        assert wp.is_completed()
        assert wp.read_completion_snapshot().completed_jobs == 1
        assert gen.generation == "partial result"


CONCURRENT_WORKERS: int = 4
"""Workers that pop at the same moment. Each pop uses one database connection, and the test engine's pool has five."""

CONCURRENTLY_POPPED_REQUESTS: int = 6
"""Queued requests with one job each. There are more requests than workers, so fewer jobs than workers are left for
the workers' second pops."""

CONCURRENT_POP_ROUNDS: int = 3
"""Pops sent by each worker. The first two rounds can assign every job, and the queue is empty for the last round."""

MOST_LEFTOVER_JOBS: int = 50
"""The limit on one worker's pops while it empties the queue of jobs left by earlier tests. The module queues far
fewer jobs than this."""


def test_workers_popping_at_the_same_moment_each_receive_a_different_job(app, client, api_key, make_api_user):
    """Workers that pop together through the real assignment each receive a different job, every queued job is
    assigned, and a request does not get more generations than it has jobs."""
    import threading

    from sqlalchemy import func

    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    def pop_body(worker_name):
        return {
            "name": worker_name,
            "models": [TEXT_MODEL],
            "bridge_agent": AGENT,
            "amount": 1,
            "max_context_length": 4096,
            "max_length": 512,
        }

    # Each worker pops until the queue is empty. The first pop registers the worker, and the others take the jobs
    # left queued by earlier tests of the module, so the pops below can only receive this test's requests.
    workers = []
    for _ in range(CONCURRENT_WORKERS):
        owner = make_api_user(trusted=True, kudos=100)
        worker_name = "concurrent-pop-" + uuid.uuid4().hex[:12]
        for _ in range(MOST_LEFTOVER_JOBS):
            earlier_pop = client.post("/api/v2/generate/text/pop", json=pop_body(worker_name), headers=_headers(owner.api_key))
            assert earlier_pop.status_code == 200, earlier_pop.get_data(as_text=True)
            if earlier_pop.get_json()["id"] is None:
                break
        else:
            pytest.fail("The queue still held jobs from earlier tests.")
        workers.append((worker_name, owner))
    request_ids = [_queue_text_wp(client, api_key) for _ in range(CONCURRENTLY_POPPED_REQUESTS)]
    # Every worker sends its pop of a round only when all of them are ready, so the pops scan the same queue head.
    all_ready = threading.Barrier(CONCURRENT_WORKERS, timeout=30)
    pop_responses = []
    pop_errors = []

    def pop_each_round(worker_name, owner):
        try:
            for _ in range(CONCURRENT_POP_ROUNDS):
                all_ready.wait()
                # Each thread pushes an application context of its own, and so works on a database session of its own.
                with app.app_context():
                    pop_responses.append(
                        client.post("/api/v2/generate/text/pop", json=pop_body(worker_name), headers=_headers(owner.api_key)),
                    )
        except Exception as error:
            all_ready.abort()
            pop_errors.append(error)

    poppers = [threading.Thread(target=pop_each_round, args=worker) for worker in workers]
    for popper in poppers:
        popper.start()
    for popper in poppers:
        popper.join(timeout=120)

    try:
        assert pop_errors == []
        assert [response.status_code for response in pop_responses] == [200] * (CONCURRENT_WORKERS * CONCURRENT_POP_ROUNDS)
        job_ids = [response.get_json()["id"] for response in pop_responses if response.get_json()["id"] is not None]
        assert len(job_ids) == CONCURRENTLY_POPPED_REQUESTS, job_ids
        assert len(set(job_ids)) == CONCURRENTLY_POPPED_REQUESTS, job_ids
        with app.app_context():
            generations_per_request = dict(
                db.session.query(TextProcessingGeneration.wp_id, func.count(TextProcessingGeneration.id))
                .filter(
                    TextProcessingGeneration.wp_id.in_([uuid.UUID(request_id) for request_id in request_ids]),
                    TextProcessingGeneration.fake.is_(False),
                )
                .group_by(TextProcessingGeneration.wp_id)
                .all(),
            )
            jobs_left = [
                row.n for row in db.session.query(TextWaitingPrompt.n).filter(TextWaitingPrompt.id.in_(list(generations_per_request))).all()
            ]
        assert sorted(map(str, generations_per_request)) == sorted(request_ids)
        assert set(generations_per_request.values()) == {1}, generations_per_request
        assert jobs_left == [0] * CONCURRENTLY_POPPED_REQUESTS
    finally:
        for request_id in request_ids:
            client.delete(f"/api/v2/generate/text/status/{request_id}", headers=_headers(api_key))
