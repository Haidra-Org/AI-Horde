# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regressions for loaded generation rows racing a different submit session."""

import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from tests.integration.test_maintenance_threads import TEXT_MODEL, _queue_text_wp
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
