# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise prompt provenance, rollback-safe evidence, tiered retention and account wipes.

Retention skips events under moderation action, so that exemption is exercised with each retention step.

Worker report records (``user_problem_jobs``) follow the evidence retention, so their retention is exercised here too.
"""

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
    RetentionPassResult,
    RetentionPolicy,
    apply_evidence_retention,
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


def test_repeated_job_report_returns_existing_event_until_anonymized(app, make_api_user) -> None:
    """A second report for the same job returns the stored event's ID and adds no row.

    Anonymization clears the job ID, so a report after it no longer matches the unique key and records a new event.
    """
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
        db.session.get(PromptModerationEvent, first).created = datetime.utcnow() - timedelta(days=31)
        db.session.commit()
        assert apply_evidence_retention(_policy(text=None, ipaddr=None, ceiling=30)).anonymized >= 1
        after_anonymization = record_prompt_evidence(evidence)
        assert after_anonymization not in (None, first)
        db.session.expire_all()
        assert db.session.execute(select(PromptModerationEvent.id).filter_by(job_id=job_id)).scalar_one() == after_anonymization
        assert db.session.get(PromptModerationEvent, first).anonymized


def test_an_origin_that_is_not_an_address_is_kept_as_text_without_address_or_pseudonym(app, make_api_user) -> None:
    """Free text where an address belongs, as a trusted proxy's ``Proxied-For`` can carry, is kept as clipped text.

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


@pytest.fixture
def _no_prior_evidence(app) -> None:
    """Start from empty evidence and worker report tables, so a retention pass counts only the rows the test records.

    The schema belongs to this module, and no other module records worker reports, so no other module's data is
    removed.
    """
    from sqlalchemy import delete

    from horde.flask import db

    with app.app_context():
        db.session.execute(delete(PromptModerationEvent))
        db.session.execute(delete(UserProblemJobs))
        db.session.commit()


def _record_aged(app, *, user_id: int, age_days: float, **evidence: Any) -> int:
    """Record a filter rejection with every identifying field set, captured ``age_days`` ago, and return its ID."""
    from horde.flask import db

    fields: dict[str, Any] = {
        "reason": PromptModerationReason.FILTER_REJECTION,
        "submitted_prompt": "submitted",
        "moderation_prompt": "moderated",
        "effective_prompt": None,
        "proxied_account": "proxied-subject",
        "ipaddr": _unique_ip(),
        "models": ["model a"],
        **evidence,
    }
    with app.app_context():
        event_id = record_prompt_evidence(PromptEvidence(user_id=user_id, **fields))
        assert event_id is not None
        db.session.get(PromptModerationEvent, event_id).created = datetime.utcnow() - timedelta(days=age_days)
        db.session.commit()
    return event_id


def _stored(app, event_id: int) -> PromptModerationEvent:
    from horde.flask import db

    with app.app_context():
        event = db.session.get(PromptModerationEvent, event_id)
        assert event is not None
        db.session.expunge(event)
    return event


def _policy(*, text: int | None, ipaddr: int | None, ceiling: int) -> RetentionPolicy:
    return RetentionPolicy(
        text=timedelta(days=text) if text is not None else None,
        ipaddr=timedelta(days=ipaddr) if ipaddr is not None else None,
        ceiling=timedelta(days=ceiling),
    )


def _pass_result(**counts: int) -> RetentionPassResult:
    """Return a pass result with the given counts and zero for every other step."""
    return RetentionPassResult(
        **{
            "anonymized": 0,
            "text_redacted": 0,
            "ipaddr_redacted": 0,
            "problem_jobs_deleted": 0,
            "problem_job_ipaddr_redacted": 0,
            **counts,
        },
    )


@pytest.mark.usefixtures("_no_prior_evidence")
def test_text_and_address_are_removed_after_their_windows(app, make_api_user) -> None:
    """Each window removes only its own values; identity stays until the ceiling and a repeat pass changes nothing."""
    submitter = make_api_user()
    fresh = _record_aged(app, user_id=submitter.id, age_days=5)
    past_address = _record_aged(app, user_id=submitter.id, age_days=15)
    past_both = _record_aged(app, user_id=submitter.id, age_days=25)
    policy = _policy(text=20, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(text_redacted=1, ipaddr_redacted=2)
        assert apply_evidence_retention(policy) == _pass_result()

    untouched = _stored(app, fresh)
    assert untouched.submitted_prompt == "submitted"
    assert untouched.ipaddr is not None
    assert not (untouched.text_redacted or untouched.ipaddr_redacted or untouched.anonymized)

    address_removed = _stored(app, past_address)
    assert address_removed.ipaddr is None
    assert address_removed.ipaddr_redacted
    assert address_removed.submitted_prompt == "submitted"
    assert address_removed.moderation_prompt == "moderated"
    assert not address_removed.text_redacted

    both_removed = _stored(app, past_both)
    assert (both_removed.submitted_prompt, both_removed.moderation_prompt, both_removed.effective_prompt) == (None, None, None)
    assert both_removed.ipaddr is None
    assert both_removed.text_redacted and both_removed.ipaddr_redacted
    for event in (address_removed, both_removed):
        assert event.user_id == submitter.id
        assert event.proxied_account == "proxied-subject"
        assert not event.anonymized


@pytest.mark.usefixtures("_no_prior_evidence")
def test_none_windows_keep_text_and_address_until_the_ceiling(app, make_api_user) -> None:
    submitter = make_api_user()
    within_ceiling = _record_aged(app, user_id=submitter.id, age_days=29)
    past_ceiling = _record_aged(app, user_id=submitter.id, age_days=31)
    with app.app_context():
        result = apply_evidence_retention(_policy(text=None, ipaddr=None, ceiling=30))
    assert result == _pass_result(anonymized=1)
    kept = _stored(app, within_ceiling)
    assert kept.submitted_prompt == "submitted"
    assert kept.ipaddr is not None
    assert not (kept.text_redacted or kept.ipaddr_redacted)
    assert _stored(app, past_ceiling).anonymized


@pytest.mark.usefixtures("_no_prior_evidence")
def test_redaction_steps_change_one_batch_oldest_first(app, make_api_user) -> None:
    submitter = make_api_user()
    oldest, middle, newest = (_record_aged(app, user_id=submitter.id, age_days=age) for age in (50, 49, 48))
    policy = _policy(text=10, ipaddr=10, ceiling=100)
    with app.app_context():
        first_pass = apply_evidence_retention(policy, batch_size=2)
        assert first_pass == _pass_result(text_redacted=2, ipaddr_redacted=2)
        assert [_stored(app, event_id).text_redacted for event_id in (oldest, middle, newest)] == [True, True, False]
        assert [_stored(app, event_id).ipaddr_redacted for event_id in (oldest, middle, newest)] == [True, True, False]
        second_pass = apply_evidence_retention(policy, batch_size=2)
        assert second_pass == _pass_result(text_redacted=1, ipaddr_redacted=1)
        assert apply_evidence_retention(policy, batch_size=2).total == 0


@pytest.mark.usefixtures("_no_prior_evidence")
def test_anonymization_changes_one_batch_oldest_first(app, make_api_user) -> None:
    submitter = make_api_user()
    oldest, middle, newest = (_record_aged(app, user_id=submitter.id, age_days=age) for age in (50, 49, 48))
    policy = _policy(text=None, ipaddr=None, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy, batch_size=2).anonymized == 2
        assert [_stored(app, event_id).anonymized for event_id in (oldest, middle, newest)] == [True, True, False]
        assert apply_evidence_retention(policy, batch_size=2).anonymized == 1
        assert apply_evidence_retention(policy, batch_size=2).total == 0


def _record_problem_job(
    app,
    *,
    user_id: int,
    worker_id: str,
    ipaddr: str | None,
    age_days: float = 0,
    origin_text: str | None = None,
) -> int:
    """Record a worker report record captured ``age_days`` ago and return its ID."""
    from horde.flask import db

    with app.app_context():
        record = UserProblemJobs(
            user_id=user_id,
            worker_id=worker_id,
            ipaddr=ipaddr,
            origin_text=origin_text,
            job_id=str(uuid4()),
            created=datetime.utcnow() - timedelta(days=age_days),
        )
        db.session.add(record)
        db.session.commit()
        return record.id


def _synthetic_worker(app, owner_id: int, name: str) -> str:
    """Register a text worker for ``owner_id`` and return its ID."""
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = TextWorker(user_id=owner_id, name=f"{name}-{owner_id}-{uuid4().hex[:8]}", max_context_length=4096)
        db.session.add(worker)
        db.session.commit()
        return str(worker.id)


@pytest.mark.usefixtures("_no_prior_evidence")
def test_worker_reports_lose_their_address_at_the_window_and_are_deleted_at_the_ceiling(app, make_api_user) -> None:
    """Worker report records follow the evidence address window and ceiling, oldest first in bounded batches."""
    from horde.flask import db

    submitter, worker_owner = make_api_user(), make_api_user()
    worker_id = _synthetic_worker(app, worker_owner.id, "synthetic-report-worker")
    fresh = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=1)
    past_window = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=15)
    past_ceiling = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=31)
    policy = _policy(text=None, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(problem_jobs_deleted=1, problem_job_ipaddr_redacted=1)
        assert apply_evidence_retention(policy) == _pass_result()
        assert db.session.get(UserProblemJobs, fresh).ipaddr is not None
        assert db.session.get(UserProblemJobs, past_window).ipaddr is None
        assert db.session.get(UserProblemJobs, past_window).user_id == submitter.id
        assert db.session.get(UserProblemJobs, past_ceiling) is None


@pytest.mark.usefixtures("_no_prior_evidence")
def test_worker_reports_keep_their_address_until_the_ceiling_under_a_none_window(app, make_api_user) -> None:
    from horde.flask import db

    submitter, worker_owner = make_api_user(), make_api_user()
    worker_id = _synthetic_worker(app, worker_owner.id, "synthetic-report-worker")
    kept = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=29)
    oldest, newest = (
        _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=age) for age in (50, 40)
    )
    policy = _policy(text=None, ipaddr=None, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy, batch_size=1) == _pass_result(problem_jobs_deleted=1)
        assert db.session.get(UserProblemJobs, oldest) is None
        assert db.session.get(UserProblemJobs, newest) is not None
        assert apply_evidence_retention(policy, batch_size=1) == _pass_result(problem_jobs_deleted=1)
        assert apply_evidence_retention(policy, batch_size=1) == _pass_result()
        assert db.session.get(UserProblemJobs, kept).ipaddr is not None


@pytest.mark.usefixtures("_no_prior_evidence")
def test_origin_text_is_removed_with_the_address(app, make_api_user) -> None:
    """The address window clears the origin text of events and worker report records, and flags the event."""
    from horde.flask import db

    submitter, worker_owner = make_api_user(), make_api_user()
    event_id = _record_aged(app, user_id=submitter.id, age_days=15, ipaddr="client behind proxy")
    assert _stored(app, event_id).origin_text == "client behind proxy"
    worker_id = _synthetic_worker(app, worker_owner.id, "synthetic-report-worker")
    report = _record_problem_job(
        app,
        user_id=submitter.id,
        worker_id=worker_id,
        ipaddr=None,
        age_days=15,
        origin_text="client behind proxy",
    )
    policy = _policy(text=None, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(ipaddr_redacted=1, problem_job_ipaddr_redacted=1)
        assert apply_evidence_retention(policy) == _pass_result()
        assert db.session.get(UserProblemJobs, report).origin_text is None
    removed = _stored(app, event_id)
    assert removed.origin_text is None
    assert removed.ipaddr_redacted


def test_deleting_a_worker_keeps_its_worker_reports(app, make_api_user) -> None:
    """A worker report record is evidence about the reported account, so it outlives the worker that made it."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    submitter, worker_owner = make_api_user(), make_api_user()
    worker_id = _synthetic_worker(app, worker_owner.id, "synthetic-deleted-worker")
    report = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=_unique_ip())
    with app.app_context():
        db.session.get(WorkerTemplate, worker_id).delete()
        db.session.expire_all()
        assert db.session.get(WorkerTemplate, worker_id) is None
        record = db.session.get(UserProblemJobs, report)
        assert record is not None
        assert (record.user_id, str(record.worker_id)) == (submitter.id, worker_id)


def test_wipe_of_an_account_with_a_worker_keeps_its_evidence_and_worker_reports(client, app, make_api_user) -> None:
    """Wiping deletes the account's worker and keeps its events and every worker report record.

    The account's own worker reported one of the account's jobs; that record survives the worker with its worker ID.
    """
    from horde.classes.base.user import User
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    wiped, other = make_api_user(), make_api_user()
    own_worker = _synthetic_worker(app, wiped.id, "synthetic-wiped-worker")
    other_worker = _synthetic_worker(app, other.id, "synthetic-other-worker")
    (wiped_event,) = _seed(app, user_id=wiped.id)
    self_reported = _record_problem_job(app, user_id=wiped.id, worker_id=own_worker, ipaddr=_unique_ip())
    reported_by_other = _record_problem_job(app, user_id=wiped.id, worker_id=other_worker, ipaddr=_unique_ip())
    others_record = _record_problem_job(app, user_id=other.id, worker_id=other_worker, ipaddr=_unique_ip())
    with app.app_context():
        db.session.get(User, wiped.id).wipe()
        db.session.expire_all()
        assert db.session.get(PromptModerationEvent, wiped_event).user_id == wiped.id
        assert db.session.get(UserProblemJobs, reported_by_other).user_id == wiped.id
        self_reported_record = db.session.get(UserProblemJobs, self_reported)
        assert self_reported_record is not None
        assert (self_reported_record.user_id, str(self_reported_record.worker_id)) == (wiped.id, own_worker)
        assert db.session.get(UserProblemJobs, others_record) is not None
        assert db.session.get(WorkerTemplate, own_worker) is None


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
