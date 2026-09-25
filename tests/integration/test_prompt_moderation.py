# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise prompt provenance, rollback-safe evidence, and moderator boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from horde.classes.base.prompt_moderation import PromptModerationEvent, PromptModerationReason, PromptModerationReview
from horde.database.prompt_moderation import PromptEvidence, record_prompt_evidence
from tests.integration.test_request_parameters import IMAGE_REQUEST

EVENTS_URL = "/api/v2/operations/moderation/prompts"


@pytest.fixture(autouse=True)
def _isolated_moderation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from horde.apis.v2 import base
    from horde.classes.base import user, worker
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    monkeypatch.setattr(base, "send_problem_user_notification", lambda *_: None)
    monkeypatch.setattr(user, "send_problem_user_notification", lambda *_: None)
    monkeypatch.setattr(worker, "send_pause_notification", lambda *_: None)
    yield
    limiter.enabled = previous


def test_successful_replacement_records_no_event(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """An accepted opt-in replacement is not actionable evidence and stores no moderation event."""
    from horde.apis.v2 import base
    from horde.flask import db

    submitter = make_api_user(kudos=1000)
    settle_kudos()
    checker = base.prompt_checker
    monkeypatch.setattr(type(checker), "__call__", lambda *_: (2, []))
    monkeypatch.setattr(checker, "check_nsfw_model_block", lambda *_: False)
    monkeypatch.setattr(checker, "apply_replacement_filter", lambda *_: "synthetic replacement")
    response = client.post(
        "/api/v2/generate/async",
        json={**IMAGE_REQUEST, "replacement_filter": True},
        headers={"apikey": submitter.api_key},
    )
    assert response.status_code == 202, response.get_json()
    try:
        with app.app_context():
            assert db.session.query(PromptModerationEvent).filter_by(user_id=submitter.id).count() == 0
    finally:
        client.delete(f"/api/v2/generate/status/{response.get_json()['id']}", headers={"apikey": submitter.api_key})


def test_style_stage_is_distinct_from_submission(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """A rejected styled prompt records the original, the styled moderation input, and no request.

    Nothing rewrites the styled prompt after moderation rejects it, so the
    effective prompt equals the moderation prompt.
    """
    from horde.apis.v2 import base, stable
    from horde.flask import db

    submitter = make_api_user(kudos=1000)
    settle_kudos()
    original_apply_style = stable.ImageAsyncGenerate.apply_style

    def apply_synthetic_style(resource) -> None:
        original_apply_style(resource)
        resource.prompt = "style prefix: " + resource.prompt

    monkeypatch.setattr(stable.ImageAsyncGenerate, "apply_style", apply_synthetic_style)
    checker = base.prompt_checker
    monkeypatch.setattr(type(checker), "__call__", lambda *_: (2, []))
    monkeypatch.setattr(checker, "check_nsfw_model_block", lambda *_: False)
    response = client.post(
        "/api/v2/generate/async",
        json={**IMAGE_REQUEST, "replacement_filter": False},
        headers={"apikey": submitter.api_key},
    )
    assert response.status_code == 400, response.get_json()
    with app.app_context():
        event = db.session.execute(select(PromptModerationEvent).filter_by(user_id=submitter.id)).scalar_one()
        assert event.submitted_prompt == IMAGE_REQUEST["prompt"]
        assert event.moderation_prompt == "style prefix: " + IMAGE_REQUEST["prompt"]
        assert event.effective_prompt == event.moderation_prompt
        assert event.reason == "filter_rejection"
        assert event.request_id is None


def test_overlong_replacement_prompt_is_recorded(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """An opt-in replacement refused for prompt length records one unmodified filter rejection."""
    from horde.apis.v2 import base
    from horde.flask import db

    submitter = make_api_user(kudos=1000)
    settle_kudos()
    checker = base.prompt_checker
    monkeypatch.setattr(type(checker), "__call__", lambda *_: (2, []))
    monkeypatch.setattr(checker, "check_prompt_replacement_length", lambda *_: False)
    response = client.post(
        "/api/v2/generate/async",
        json={**IMAGE_REQUEST, "replacement_filter": True},
        headers={"apikey": submitter.api_key},
    )
    assert response.status_code == 400, response.get_json()
    with app.app_context():
        event = db.session.execute(select(PromptModerationEvent).filter_by(user_id=submitter.id)).scalar_one()
        assert event.reason == "filter_rejection"
        assert event.moderation_prompt == IMAGE_REQUEST["prompt"]
        assert event.effective_prompt == event.moderation_prompt


def test_flagged_user_rejection_on_ordinary_model_is_recorded(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """A flagged account whose forced replacement empties the prompt records one model rejection."""
    from horde.apis.v2 import base
    from horde.classes.base.user import User
    from horde.flask import db

    submitter = make_api_user(kudos=1000)
    settle_kudos()
    with app.app_context():
        db.session.get(User, submitter.id).set_flagged(True)
        db.session.commit()
    checker = base.prompt_checker
    monkeypatch.setattr(type(checker), "__call__", lambda *_: (0, []))
    monkeypatch.setattr(checker, "check_nsfw_model_block", lambda *_: False)
    monkeypatch.setattr(checker, "nsfw_model_prompt_replace", lambda *_, **__: None)
    response = client.post("/api/v2/generate/async", json=IMAGE_REQUEST, headers={"apikey": submitter.api_key})
    assert response.status_code == 400, response.get_json()
    with app.app_context():
        event = db.session.execute(select(PromptModerationEvent).filter_by(user_id=submitter.id)).scalar_one()
        assert event.reason == "model_rejection"
        assert event.submitted_prompt == IMAGE_REQUEST["prompt"]


def test_evidence_survives_rollback_and_review_is_separate(client, app, api_key, make_api_user) -> None:
    from horde.classes.base.user import User
    from horde.flask import db

    submitter = make_api_user()
    with app.app_context():
        account = db.session.get(User, submitter.id)
        old_name = account.username
        account.username = "uncommitted-change"
        db.session.flush()
        event_id = record_prompt_evidence(
            PromptEvidence(
                user_id=submitter.id,
                reason=PromptModerationReason.FILTER_REJECTION,
                submitted_prompt="original",
                moderation_prompt="styled original",
                effective_prompt=None,
            )
        )
        assert event_id is not None
        db.session.rollback()
        assert db.session.get(User, submitter.id).username == old_name
        assert db.session.get(PromptModerationEvent, event_id).submitted_prompt == "original"

    assert client.get(EVENTS_URL).status_code == 400
    assert client.get(EVENTS_URL, headers={"apikey": submitter.api_key}).status_code == 403
    assert client.patch(f"{EVENTS_URL}/{event_id}", json={"status": "reviewed"}, headers={"apikey": submitter.api_key}).status_code == 403
    headers = {"apikey": api_key}
    page = client.get(EVENTS_URL, query_string={"user_id": submitter.id, "status": "pending"}, headers=headers)
    assert page.status_code == 200, page.get_json()
    assert page.headers["Cache-Control"] == "private, no-store"
    assert [event["id"] for event in page.get_json()["events"]] == [event_id]
    review = client.patch(f"{EVENTS_URL}/{event_id}", json={"status": "dismissed", "note": "synthetic false positive"}, headers=headers)
    assert review.status_code == 200, review.get_json()
    assert review.headers["Cache-Control"] == "private, no-store"
    assert client.get(EVENTS_URL, query_string={"user_id": submitter.id, "status": "pending"}, headers=headers).get_json()["events"] == []
    with app.app_context():
        evidence = db.session.get(PromptModerationEvent, event_id)
        disposition = db.session.get(PromptModerationReview, event_id)
        assert evidence.submitted_prompt == "original"
        assert disposition.status == "dismissed"
        assert disposition.reviewer_id == review.get_json()["reviewer_id"]


def test_worker_problem_jobs_preserve_evidence_without_duplicate_rows(app, make_api_user) -> None:
    from horde.classes.base.user import User
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    submitter = make_api_user()
    job_id, request_id = (str(uuid4()) for _ in range(2))
    with app.app_context():
        account = db.session.get(User, submitter.id)
        waiting = SimpleNamespace(id=request_id, read_privileged_submitted_prompt=lambda **_: "original", proxied_account=None, params={})
        job = SimpleNamespace(id=job_id, wp=waiting)
        worker = TextWorker(user_id=submitter.id, name=f"synthetic-worker-{submitter.id}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        account.record_problem_job(job, "192.0.2.1", worker, "effective")
        account.record_problem_job(job, "192.0.2.1", worker, "effective")
        evidence = db.session.execute(select(PromptModerationEvent).filter_by(job_id=job_id)).scalar_one()
        assert evidence.submitted_prompt == "original"
        assert evidence.effective_prompt == "effective"
        assert evidence.moderation_prompt is None
        assert evidence.outcome == "censored"


def test_problem_job_alert_references_evidence_without_prompt_text(app, make_api_user, monkeypatch) -> None:
    """A threshold-crossing problem-job alert carries the event ID and review link, never prompt text."""
    from horde.classes.base import user as user_module
    from horde.classes.base.user import User, UserProblemJobs
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    notifications: list[str] = []
    monkeypatch.setattr(user_module, "send_problem_user_notification", notifications.append)
    submitter = make_api_user()
    job_id, request_id = (str(uuid4()) for _ in range(2))
    submitted, effective = "PRIVATE_SUBMITTED_PROMPT", "PRIVATE_EFFECTIVE_PROMPT"
    ipaddr = "192.0.2.2"
    with app.app_context():
        account = db.session.get(User, submitter.id)
        worker = TextWorker(user_id=submitter.id, name=f"synthetic-worker-{submitter.id}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        # The hourly account threshold notifies once the count, including the reported job, exceeds 50.
        db.session.add_all(
            UserProblemJobs(user_id=submitter.id, ipaddr=ipaddr, job_id=str(uuid4()), worker_id=worker.id, proxied_account=None)
            for _ in range(50)
        )
        db.session.commit()
        waiting = SimpleNamespace(id=request_id, read_privileged_submitted_prompt=lambda **_: submitted, proxied_account=None, params={})
        account.record_problem_job(SimpleNamespace(id=job_id, wp=waiting), ipaddr, worker, effective)
        event_id = db.session.execute(select(PromptModerationEvent.id).filter_by(job_id=job_id)).scalar_one()
    assert len(notifications) == 1
    message = notifications[0]
    assert f"Moderation event: {event_id}" in message
    assert f"/api/v2/operations/moderation/prompts?user_id={submitter.id}" in message
    assert submitted not in message
    assert effective not in message


def test_repeated_job_report_returns_existing_event(app, make_api_user) -> None:
    """A second report for the same job returns the stored event's ID and adds no row."""
    from horde.flask import db

    submitter = make_api_user()
    job_id = str(uuid4())
    evidence = PromptEvidence(
        user_id=submitter.id,
        reason=PromptModerationReason.WORKER_CSAM,
        submitted_prompt="original",
        moderation_prompt=None,
        effective_prompt="effective",
        job_id=job_id,
    )
    with app.app_context():
        first = record_prompt_evidence(evidence)
        second = record_prompt_evidence(evidence)
        assert first is not None
        assert second == first
        assert db.session.query(PromptModerationEvent).filter_by(job_id=job_id).count() == 1


def test_event_listing_exposes_only_documented_fields(client, app, api_key, make_api_user) -> None:
    """Each listed event carries exactly the documented field allowlist and no credentials."""
    submitter = make_api_user()
    with app.app_context():
        event_id = record_prompt_evidence(
            PromptEvidence(
                user_id=submitter.id,
                reason=PromptModerationReason.FILTER_REJECTION,
                submitted_prompt="original",
                moderation_prompt="original",
                effective_prompt="original",
            )
        )
    page = client.get(EVENTS_URL, query_string={"user_id": submitter.id}, headers={"apikey": api_key})
    assert page.status_code == 200, page.get_json()
    (event,) = page.get_json()["events"]
    assert event["id"] == event_id
    assert set(event) == {
        "id",
        "created",
        "user_id",
        "request_id",
        "job_id",
        "worker_id",
        "proxied_account",
        "reason",
        "outcome",
        "submitted_prompt",
        "moderation_prompt",
        "effective_prompt",
        "text_truncated",
        "status",
        "reviewer_id",
        "reviewed_at",
        "note",
    }
    assert "apikey" not in event


def test_worker_csam_submission_records_event(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """An image job submitted with csam censorship metadata records one worker_csam event for that job and worker."""
    from horde.classes.stable import processing_generation, waiting_prompt
    from horde.classes.stable.processing_generation import ImageProcessingGeneration
    from horde.flask import db

    # Nothing is transferred here, so presigned URLs need no object storage.
    def presigned_url(procgen_id: str, shared: bool = False) -> str:
        return f"https://example.invalid/{procgen_id}"

    monkeypatch.setattr(waiting_prompt, "generate_procgen_upload_url", presigned_url)
    monkeypatch.setattr(processing_generation, "generate_procgen_download_url", presigned_url)
    submitter = make_api_user(kudos=1000)
    worker_owner = make_api_user(trusted=True)
    settle_kudos()
    request = client.post(
        "/api/v2/generate/async",
        json={**IMAGE_REQUEST, "shared": False, "params": {**IMAGE_REQUEST["params"], "n": 1}},
        headers={"apikey": submitter.api_key},
    )
    assert request.status_code == 202, request.get_json()
    request_id = request.get_json()["id"]
    try:
        pop = client.post(
            "/api/v2/generate/pop",
            json={
                "name": f"synthetic-image-worker-{worker_owner.id}",
                "models": IMAGE_REQUEST["models"],
                "bridge_agent": "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
                "nsfw": True,
                "amount": 1,
                "max_pixels": 4194304,
                "allow_unsafe_ipaddr": True,
            },
            headers={"apikey": worker_owner.api_key},
        )
        assert pop.status_code == 200, pop.get_json()
        job_id = pop.get_json()["id"]
        assert job_id is not None, pop.get_json()
        with app.app_context():
            job = db.session.get(ImageProcessingGeneration, job_id)
            assert str(job.wp_id) == request_id
            worker_id = str(job.worker_id)
        submit = client.post(
            "/api/v2/generate/submit",
            json={
                "id": job_id,
                "generation": "R2",
                "state": "ok",
                "seed": 0,
                "gen_metadata": [{"type": "censorship", "value": "csam"}],
            },
            headers={"apikey": worker_owner.api_key},
        )
        assert submit.status_code == 200, submit.get_json()
        with app.app_context():
            event = db.session.execute(select(PromptModerationEvent).filter_by(user_id=submitter.id)).scalar_one()
            assert event.reason == "worker_csam"
            assert event.outcome == "censored"
            assert event.job_id == job_id
            assert event.worker_id == worker_id
            assert event.request_id == request_id
    finally:
        client.delete(f"/api/v2/generate/status/{request_id}", headers={"apikey": submitter.api_key})


def test_retention_and_bounded_text(app, make_api_user) -> None:
    from horde.classes.base.prompt_moderation import PromptReviewStatus
    from horde.database.prompt_moderation import MAX_EVIDENCE_CHARACTERS, prune_moderation_evidence, review_prompt_event
    from horde.flask import db

    submitter = make_api_user()
    with app.app_context():
        event_id = record_prompt_evidence(
            PromptEvidence(
                user_id=submitter.id,
                reason=PromptModerationReason.FILTER_REJECTION,
                submitted_prompt="x" * (MAX_EVIDENCE_CHARACTERS + 1),
                moderation_prompt=None,
                effective_prompt=None,
            )
        )
        retained_id = record_prompt_evidence(
            PromptEvidence(
                user_id=submitter.id,
                reason=PromptModerationReason.FILTER_REJECTION,
                submitted_prompt="within retention",
                moderation_prompt=None,
                effective_prompt=None,
            )
        )
        assert review_prompt_event(event_id=event_id, reviewer_id=submitter.id, status=PromptReviewStatus.REVIEWED, note="test")
        event = db.session.get(PromptModerationEvent, event_id)
        assert event.text_truncated
        assert len(event.submitted_prompt) == MAX_EVIDENCE_CHARACTERS
        event.created = datetime.utcnow() - timedelta(days=31)
        db.session.get(PromptModerationEvent, retained_id).created = datetime.utcnow() - timedelta(days=29)
        db.session.commit()
        assert prune_moderation_evidence() >= 1
        db.session.expire_all()
        assert db.session.get(PromptModerationEvent, event_id) is None
        assert db.session.get(PromptModerationReview, event_id) is None
        assert db.session.get(PromptModerationEvent, retained_id).submitted_prompt == "within retention"


@pytest.mark.parametrize("model_rejection", [False, True])
def test_failed_replacement_is_recorded(client, app, make_api_user, settle_kudos, monkeypatch, model_rejection: bool) -> None:
    from horde.apis.v2 import base
    from horde.flask import db

    submitter = make_api_user(kudos=1000)
    settle_kudos()
    checker = base.prompt_checker
    monkeypatch.setattr(type(checker), "__call__", lambda *_: (0 if model_rejection else 2, []))
    monkeypatch.setattr(checker, "check_nsfw_model_block", lambda *_: model_rejection)
    monkeypatch.setattr(checker, "apply_replacement_filter", lambda *_: None)
    monkeypatch.setattr(checker, "nsfw_model_prompt_replace", lambda *_, **__: None)
    response = client.post("/api/v2/generate/async", json=IMAGE_REQUEST, headers={"apikey": submitter.api_key})
    assert response.status_code == 400, response.get_json()
    with app.app_context():
        event = db.session.execute(select(PromptModerationEvent).filter_by(user_id=submitter.id)).scalar_one()
        assert event.reason == ("model_rejection" if model_rejection else "filter_rejection")
        assert event.submitted_prompt == IMAGE_REQUEST["prompt"]
        assert event.effective_prompt is None


def test_database_failure_does_not_accept_rejected_prompt(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    from horde.apis.v2 import base
    from horde.database import prompt_moderation
    from horde.flask import db

    submitter = make_api_user(kudos=1000)
    settle_kudos()
    errors = []

    def unavailable_connection():
        raise OperationalError("insert evidence", {"prompt": "PRIVATE_EXCEPTION_PARAMETER"}, RuntimeError("unavailable"))

    monkeypatch.setattr(type(base.prompt_checker), "__call__", lambda *_: (2, []))
    monkeypatch.setattr(prompt_moderation.logger, "error", lambda message, *arguments: errors.append(message.format(*arguments)))
    with app.app_context():
        monkeypatch.setattr(db.engine, "begin", unavailable_connection)
    response = client.post(
        "/api/v2/generate/async",
        json={**IMAGE_REQUEST, "replacement_filter": False},
        headers={"apikey": submitter.api_key},
    )
    assert response.status_code == 400, response.get_json()
    assert errors == ["Prompt moderation evidence write failed (OperationalError)"]


def test_prompt_event_pagination_filters_and_invalid_review(client, app, api_key, make_api_user) -> None:
    submitter = make_api_user()
    with app.app_context():
        identifiers = [
            record_prompt_evidence(
                PromptEvidence(
                    user_id=submitter.id,
                    reason=PromptModerationReason.FILTER_REJECTION,
                    submitted_prompt=f"original {number}",
                    moderation_prompt=None,
                    effective_prompt=None,
                )
            )
            for number in range(3)
        ]
    headers = {"apikey": api_key}
    query = {"user_id": submitter.id, "reason": "filter_rejection", "outcome": "rejected", "limit": 2}
    first = client.get(EVENTS_URL, query_string=query, headers=headers).get_json()
    assert [event["id"] for event in first["events"]] == identifiers[:0:-1]
    second = client.get(EVENTS_URL, query_string={**query, "before_id": first["next_cursor"]}, headers=headers).get_json()
    assert [event["id"] for event in second["events"]] == identifiers[:1]
    assert second["next_cursor"] is None
    future = client.get(EVENTS_URL, query_string={**query, "since": "2100-01-01T00:00:00Z"}, headers=headers)
    assert future.get_json()["events"] == []
    for invalid in ({"limit": 101}, {"since": "2026-01-01"}, {"before_id": 0}, {"status": "unknown"}):
        assert client.get(EVENTS_URL, query_string=invalid, headers=headers).status_code == 400
    for invalid in ({"status": None}, {"status": "reviewed", "note": "x" * 2001}):
        assert client.patch(f"{EVENTS_URL}/{identifiers[0]}", json=invalid, headers=headers).status_code == 400
    assert client.patch(f"{EVENTS_URL}/0", json={"status": "reviewed"}, headers=headers).status_code == 404
