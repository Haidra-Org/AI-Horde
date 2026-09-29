# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise prompt provenance and rollback-safe evidence capture."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from horde.classes.base.prompt_moderation import (
    MAX_ORIGIN_TEXT_CHARACTERS,
    PromptModerationEvent,
    PromptModerationReason,
)
from horde.classes.base.user import UserProblemJobs
from horde.countermeasures import CounterMeasures
from horde.database.prompt_moderation import (
    PromptEvidence,
    record_prompt_evidence,
)
from tests.fixture_types import ApiUser, MakeApiUser
from tests.integration.test_request_parameters import IMAGE_REQUEST

TEST_IP_SUBJECT_SECRET = b"integration-test deployment secret"
"""The private secret the tests key address pseudonyms with, since the test environment sets none."""


@pytest.fixture(autouse=True)
def _isolated_moderation(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from horde import countermeasures
    from horde.apis.v2 import base
    from horde.classes.base import user
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_IP_SUBJECT_SECRET)
    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    monkeypatch.setattr(base, "send_problem_user_notification", lambda *_: None)
    monkeypatch.setattr(user, "send_problem_user_notification", lambda *_: None)
    yield
    limiter.enabled = previous


def _unique_ip() -> str:
    raw = uuid4().bytes
    return f"10.{raw[0]}.{raw[1]}.{raw[2]}"


def _seed(app, *, user_id: int, count: int = 1, proxied_account: str | None = None, ipaddr: str | None = None) -> list[int]:
    """Record synthetic filter rejections and return their IDs."""
    identifiers: list[int] = []
    with app.app_context():
        for _ in range(count):
            event_id = record_prompt_evidence(
                PromptEvidence(
                    user_id=user_id,
                    reason=PromptModerationReason.FILTER_REJECTION,
                    submitted_prompt="synthetic",
                    moderation_prompt=None,
                    effective_prompt=None,
                    proxied_account=proxied_account,
                    ipaddr=ipaddr,
                ),
            )
            assert event_id is not None
            identifiers.append(event_id)
    return identifiers


def _funded_submitter(make_api_user: MakeApiUser, settle_kudos) -> ApiUser:
    """Return an account with enough settled kudos that only moderation can refuse its image request."""
    submitter = make_api_user(kudos=1000)
    settle_kudos()
    return submitter


def _patch_checker(monkeypatch: pytest.MonkeyPatch, suspicion: int, **results: Any) -> None:
    """Fix the prompt checker's suspicion score and make each named checker method return a constant.

    ``check_nsfw_model_block`` returns False unless a result is given for it.
    """
    from horde.apis.v2 import base

    checker = base.prompt_checker
    monkeypatch.setattr(type(checker), "__call__", lambda *_: (suspicion, []))
    for name, result in {"check_nsfw_model_block": False, **results}.items():
        monkeypatch.setattr(checker, name, lambda *_, result=result, **__: result)


def _rejected_event(
    client,
    app,
    submitter: ApiUser,
    body: dict[str, Any] = IMAGE_REQUEST,
    ipaddr: str | None = None,
) -> PromptModerationEvent:
    """Submit an image request the patched checker refuses and return the single event it recorded."""
    from horde.flask import db

    environ = {"REMOTE_ADDR": ipaddr} if ipaddr else {}
    response = client.post("/api/v2/generate/async", json=body, headers={"apikey": submitter.api_key}, environ_base=environ)
    assert response.status_code == 400, response.get_json()
    with app.app_context():
        event = db.session.execute(select(PromptModerationEvent).filter_by(user_id=submitter.id)).scalar_one()
        db.session.expunge(event)
    return event


def test_successful_replacement_records_no_event(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """An accepted opt-in replacement is not actionable evidence and stores no moderation event."""
    from horde.flask import db

    submitter = _funded_submitter(make_api_user, settle_kudos)
    _patch_checker(monkeypatch, 2, apply_replacement_filter="synthetic replacement")
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
    """A plain rejection of a styled prompt records the original and the styled moderation input.

    No replacement ran, so there is no prompt a worker could receive and the effective stage is null.
    """
    from horde.apis.v2 import stable

    submitter = _funded_submitter(make_api_user, settle_kudos)
    original_apply_style = stable.ImageAsyncGenerate.apply_style

    def apply_synthetic_style(resource) -> None:
        original_apply_style(resource)
        resource.prompt = "style prefix: " + resource.prompt

    monkeypatch.setattr(stable.ImageAsyncGenerate, "apply_style", apply_synthetic_style)
    _patch_checker(monkeypatch, 2)
    event = _rejected_event(client, app, submitter, {**IMAGE_REQUEST, "replacement_filter": False})
    assert event.submitted_prompt == IMAGE_REQUEST["prompt"]
    assert event.moderation_prompt == "style prefix: " + IMAGE_REQUEST["prompt"]
    assert event.effective_prompt is None
    assert event.reason == "filter_rejection"
    assert event.request_id is None


def test_overlong_replacement_prompt_is_recorded(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """An opt-in replacement refused for prompt length records the moderation input and no effective prompt."""
    submitter = _funded_submitter(make_api_user, settle_kudos)
    _patch_checker(monkeypatch, 2, check_prompt_replacement_length=False)
    event = _rejected_event(client, app, submitter, {**IMAGE_REQUEST, "replacement_filter": True})
    assert event.reason == "filter_rejection"
    assert event.moderation_prompt == IMAGE_REQUEST["prompt"]
    assert event.effective_prompt is None


def test_flagged_user_rejection_on_ordinary_model_is_recorded(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    """A flagged account whose forced replacement empties the prompt records one model rejection."""
    from horde.classes.base.user import User
    from horde.flask import db

    submitter = _funded_submitter(make_api_user, settle_kudos)
    with app.app_context():
        db.session.get(User, submitter.id).set_flagged(True)
        db.session.commit()
    _patch_checker(monkeypatch, 0, nsfw_model_prompt_replace=None)
    event = _rejected_event(client, app, submitter)
    assert event.reason == "model_rejection"
    assert event.submitted_prompt == IMAGE_REQUEST["prompt"]
    assert event.moderation_prompt == IMAGE_REQUEST["prompt"]
    assert event.effective_prompt is None


def test_model_rejection_after_filter_replacement_records_no_effective_prompt(
    client,
    app,
    make_api_user,
    settle_kudos,
    monkeypatch,
) -> None:
    """A filter replacement that produced text and a model replacement that then emptied it leave no effective prompt."""
    submitter = _funded_submitter(make_api_user, settle_kudos)
    _patch_checker(
        monkeypatch,
        2,
        apply_replacement_filter="synthetic replacement",
        check_nsfw_model_block=True,
        nsfw_model_prompt_replace=None,
    )
    event = _rejected_event(client, app, submitter, {**IMAGE_REQUEST, "replacement_filter": True})
    assert event.reason == "model_rejection"
    assert event.moderation_prompt == IMAGE_REQUEST["prompt"]
    assert event.effective_prompt is None


@pytest.mark.parametrize("model_rejection", [False, True])
def test_failed_replacement_is_recorded(client, app, make_api_user, settle_kudos, monkeypatch, model_rejection: bool) -> None:
    """A filter or model rejection records the original, no effective prompt, the address, and the requested models."""
    submitter = _funded_submitter(make_api_user, settle_kudos)
    ipaddr = _unique_ip()
    _patch_checker(
        monkeypatch,
        0 if model_rejection else 2,
        check_nsfw_model_block=model_rejection,
        apply_replacement_filter=None,
        nsfw_model_prompt_replace=None,
    )
    event = _rejected_event(client, app, submitter, ipaddr=ipaddr)
    assert event.reason == ("model_rejection" if model_rejection else "filter_rejection")
    assert event.submitted_prompt == IMAGE_REQUEST["prompt"]
    assert event.moderation_prompt == IMAGE_REQUEST["prompt"]
    assert event.effective_prompt is None
    assert event.ipaddr == ipaddr
    assert event.models == IMAGE_REQUEST["models"]


def test_worker_problem_jobs_preserve_evidence_without_duplicate_rows(app, make_api_user) -> None:
    from horde.classes.base.user import User
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    submitter = make_api_user()
    job_id, request_id = (str(uuid4()) for _ in range(2))
    with app.app_context():
        account = db.session.get(User, submitter.id)
        waiting = SimpleNamespace(
            id=request_id,
            read_privileged_submitted_prompt=lambda **_: "original",
            proxied_account=None,
            params={},
            get_model_names=lambda: ["model a", "model b"],
        )
        job = SimpleNamespace(id=job_id, wp=waiting)
        worker = TextWorker(user_id=submitter.id, name=f"synthetic-worker-{submitter.id}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        account.record_problem_job(job, "2001:db8:1:2:3:4:5:6", worker, "effective")
        account.record_problem_job(job, "2001:db8:1:2:3:4:5:6", worker, "effective")
        evidence = db.session.execute(select(PromptModerationEvent).filter_by(job_id=job_id)).scalar_one()
        assert evidence.submitted_prompt == "original"
        assert evidence.effective_prompt == "effective"
        assert evidence.moderation_prompt is None
        assert evidence.outcome == "censored"
        # IPv6 clients rotate within their /64, so the address subject is the network.
        assert evidence.ipaddr == "2001:db8:1:2::/64"
        assert evidence.ip_subject_key == CounterMeasures.ip_subject_key("2001:db8:1:2::/64")
        assert evidence.models == ["model a", "model b"]
        problem_jobs = db.session.execute(select(UserProblemJobs).filter_by(job_id=job_id)).scalars().all()
        assert {problem_job.ipaddr for problem_job in problem_jobs} == {"2001:db8:1:2::/64"}


def test_mapped_ipv4_addresses_are_recorded_as_the_ipv4_subject(app, make_api_user) -> None:
    """Evidence and worker report records captured from an IPv4-mapped address record, and key, the IPv4 address."""
    from horde.classes.base.user import User
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    submitter = make_api_user()
    job_id = str(uuid4())
    with app.app_context():
        account = db.session.get(User, submitter.id)
        waiting = SimpleNamespace(
            id=str(uuid4()),
            read_privileged_submitted_prompt=lambda **_: "original",
            proxied_account=None,
            params={},
            get_model_names=lambda: ["model a"],
        )
        worker = TextWorker(user_id=submitter.id, name=f"synthetic-mapped-worker-{submitter.id}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        account.record_problem_job(SimpleNamespace(id=job_id, wp=waiting), "::ffff:198.51.100.23", worker, "effective")
        evidence = db.session.execute(select(PromptModerationEvent).filter_by(job_id=job_id)).scalar_one()
        assert evidence.ipaddr == "198.51.100.23"
        assert evidence.ip_subject_key == CounterMeasures.ip_subject_key("198.51.100.23")
        (problem_job,) = db.session.execute(select(UserProblemJobs).filter_by(job_id=job_id)).scalars()
        assert problem_job.ipaddr == "198.51.100.23"


DISCORD_CONTENT_LIMIT = 2000
"""Discord rejects webhook content above this length, and a rejected alert is lost while its mute key is still set."""


def _report_problem_jobs(
    app,
    monkeypatch,
    *,
    submitter,
    ipaddr: str,
    prior_reports: int,
    prior_age: timedelta,
    prior_user_id: int | None = None,
    prior_subjectless: bool = False,
) -> list[str]:
    """Seed prior problem jobs, report one more as ``submitter``, and return the alerts sent.

    The prior jobs record ``ipaddr``, or no address when ``prior_subjectless`` is set.
    """
    from horde.classes.base import user as user_module
    from horde.classes.base.user import User
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    messages: list[str] = []
    monkeypatch.setattr(user_module, "send_problem_user_notification", messages.append)
    monkeypatch.setattr(user_module.hr, "horde_r_get", lambda *_: None)
    monkeypatch.setattr(user_module.hr, "horde_r_setex", lambda *_: None)
    with app.app_context():
        account = db.session.get(User, submitter.id)
        worker = TextWorker(user_id=submitter.id, name=f"alert-worker-{submitter.id}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        db.session.add_all(
            UserProblemJobs(
                user_id=prior_user_id if prior_user_id is not None else submitter.id,
                ipaddr=None if prior_subjectless else ipaddr,
                job_id=str(uuid4()),
                worker_id=worker.id,
                created=datetime.utcnow() - prior_age,
            )
            for _ in range(prior_reports)
        )
        db.session.commit()
        waiting = SimpleNamespace(
            id=str(uuid4()),
            read_privileged_submitted_prompt=lambda **_: "original submission " * 200,
            proxied_account=None,
            params={"loras": [{"name": "lora one"}, {"name": "lora two"}]},
            get_model_names=lambda: ["model a"],
        )
        account.record_problem_job(SimpleNamespace(id=str(uuid4()), wp=waiting), ipaddr, worker, "effective " * 300)
    return messages


def test_problem_job_alert_refers_to_evidence_instead_of_prompt_or_address(app, make_api_user, monkeypatch) -> None:
    """The hourly account alert carries the event ID and the review link and neither prompt text nor the address."""
    from horde.flask import db

    submitter = make_api_user()
    ipaddr = _unique_ip()
    monkeypatch.setenv("HORDE_MODERATION_FRONTPAGE_URL", "https://frontpage.example/")
    messages = _report_problem_jobs(
        app,
        monkeypatch,
        submitter=submitter,
        ipaddr=ipaddr,
        prior_reports=50,
        prior_age=timedelta(minutes=5),
    )
    assert len(messages) == 1
    message = messages[0]
    with app.app_context():
        event_id = db.session.execute(
            select(PromptModerationEvent.id).filter_by(user_id=submitter.id, reason=PromptModerationReason.WORKER_CSAM),
        ).scalar_one()
    assert message.startswith(f"CSAM censor threshold: account {submitter.alias}")
    assert "51 worker reports in the past hour (threshold 50)" in message
    assert f"event {event_id}," in message
    assert f"https://frontpage.example/admin/review?tab=prompts&event_id={event_id}" in message
    assert "LoRAs: lora one, lora two" in message
    assert "original submission" not in message
    assert "effective" not in message
    assert ipaddr not in message
    assert len(message) <= DISCORD_CONTENT_LIMIT


def test_ip_daily_alert_counts_one_day_and_carries_the_pseudonym_prefix(app, make_api_user, monkeypatch) -> None:
    """Reports older than an hour reach the IP daily threshold; the alert identifies the address by its pseudonym only."""
    submitter = make_api_user()
    prior_submitter = make_api_user()
    ipaddr = _unique_ip()
    monkeypatch.delenv("HORDE_MODERATION_FRONTPAGE_URL", raising=False)
    messages = _report_problem_jobs(
        app,
        monkeypatch,
        submitter=submitter,
        ipaddr=ipaddr,
        prior_reports=200,
        prior_age=timedelta(hours=2),
        prior_user_id=prior_submitter.id,
    )
    assert len(messages) == 1
    message = messages[0]
    subject_key = CounterMeasures.ip_subject_key(ipaddr)
    assert subject_key is not None
    assert message.startswith(f"CSAM censor threshold: IP subject {subject_key[:12]}, latest account {submitter.alias}")
    assert "201 worker reports in the past day (threshold 200)" in message
    assert "in the frontpage Prompts tab" in message
    assert ipaddr not in message


def test_subjectless_origin_past_the_ip_threshold_sends_no_ip_alert(app, make_api_user, monkeypatch) -> None:
    """Reports whose origin has no IP subject are not counted together as one address."""
    submitter = make_api_user()
    prior_submitter = make_api_user()
    messages = _report_problem_jobs(
        app,
        monkeypatch,
        submitter=submitter,
        ipaddr="not an address",
        prior_reports=60,
        prior_age=timedelta(minutes=5),
        prior_user_id=prior_submitter.id,
        prior_subjectless=True,
    )
    assert messages == []


def test_an_origin_that_is_not_an_address_records_no_address_or_pseudonym(app, make_api_user) -> None:
    """Free text where an address belongs, as a trusted proxy's ``Proxied-For`` can carry, is neither stored nor keyed.

    It is neither an address nor keyed, on the event and on the worker report record.
    """
    from horde.classes.base.user import User
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    submitter = make_api_user()
    job_id = str(uuid4())
    free_text = "client behind proxy " + "x" * MAX_ORIGIN_TEXT_CHARACTERS
    clipped = free_text[:MAX_ORIGIN_TEXT_CHARACTERS]
    (event_id,) = _seed(app, user_id=submitter.id, ipaddr=free_text)
    stored = _stored(app, event_id)
    assert (stored.ipaddr, stored.ip_subject_key, stored.origin_text) == (None, None, clipped)
    with app.app_context():
        account = db.session.get(User, submitter.id)
        waiting = SimpleNamespace(
            id=str(uuid4()),
            read_privileged_submitted_prompt=lambda **_: "original",
            proxied_account=None,
            params={},
            get_model_names=lambda: ["model a"],
        )
        worker = TextWorker(user_id=submitter.id, name=f"synthetic-free-text-worker-{submitter.id}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        account.record_problem_job(SimpleNamespace(id=job_id, wp=waiting), free_text, worker, "effective")
        evidence = db.session.execute(select(PromptModerationEvent).filter_by(job_id=job_id)).scalar_one()
        assert (evidence.ipaddr, evidence.ip_subject_key, evidence.origin_text) == (None, None, clipped)
        (problem_job,) = db.session.execute(select(UserProblemJobs).filter_by(job_id=job_id)).scalars()
        assert (problem_job.ipaddr, problem_job.origin_text) == (None, clipped)


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
    ipaddr = _unique_ip()
    request = client.post(
        "/api/v2/generate/async",
        json={**IMAGE_REQUEST, "shared": False, "params": {**IMAGE_REQUEST["params"], "n": 1}},
        headers={"apikey": submitter.api_key},
        environ_base={"REMOTE_ADDR": ipaddr},
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
            assert event.ipaddr == ipaddr
            assert event.models == IMAGE_REQUEST["models"]
    finally:
        client.delete(f"/api/v2/generate/status/{request_id}", headers={"apikey": submitter.api_key})


def test_evidence_text_is_clipped_at_the_length_limit(app, make_api_user) -> None:
    from horde.database.prompt_moderation import MAX_EVIDENCE_CHARACTERS
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
        event = db.session.get(PromptModerationEvent, event_id)
        assert event.text_truncated
        assert len(event.submitted_prompt) == MAX_EVIDENCE_CHARACTERS


def _stored(app, event_id: int) -> PromptModerationEvent:
    from horde.flask import db

    with app.app_context():
        event = db.session.get(PromptModerationEvent, event_id)
        assert event is not None
        db.session.expunge(event)
    return event


def test_database_failure_does_not_accept_rejected_prompt(client, app, make_api_user, settle_kudos, monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    from horde.database import prompt_moderation
    from horde.flask import db

    submitter = _funded_submitter(make_api_user, settle_kudos)
    errors = []

    def unavailable_connection():
        raise OperationalError("insert evidence", {"prompt": "PRIVATE_EXCEPTION_PARAMETER"}, RuntimeError("unavailable"))

    _patch_checker(monkeypatch, 2)
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
