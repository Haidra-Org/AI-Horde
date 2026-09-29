# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise prompt provenance, rollback-safe evidence, tiered retention, account wipes, and the moderator evidence endpoints.

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
    PromptModerationNote,
    PromptModerationReason,
)
from horde.classes.base.user import UserProblemJobs
from horde.countermeasures import CounterMeasures
from horde.database.prompt_moderation import (
    EVENT_COLUMN_RETENTION,
    PromptEvidence,
    RetentionFate,
    RetentionPassResult,
    RetentionPolicy,
    apply_evidence_retention,
    get_prompt_events,
    record_prompt_evidence,
)
from tests.fixture_types import ApiUser, MakeApiUser
from tests.integration.test_request_parameters import IMAGE_REQUEST

EVENTS_URL = "/api/v2/operations/moderation/prompts"
"""The moderator evidence listing."""
UNLISTED_EVENT_COLUMNS = frozenset({"job_id"})
"""Event columns the listing omits; the job ID is internal deduplication state."""
LISTING_ONLY_EVENT_KEYS = frozenset({"notes"})
"""Listed event keys that are not event columns."""
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


def test_evidence_survives_rollback_and_notes_are_separate(client, app, api_key, make_api_user) -> None:
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

    notes_url = f"{EVENTS_URL}/{event_id}/notes"
    assert client.get(EVENTS_URL).status_code == 400
    assert client.get(EVENTS_URL, headers={"apikey": submitter.api_key}).status_code == 403
    assert client.post(notes_url, json={"note": "x"}, headers={"apikey": submitter.api_key}).status_code == 403
    headers = {"apikey": api_key}
    note = client.post(notes_url, json={"note": "synthetic false positive"}, headers=headers)
    assert note.status_code == 201, note.get_json()
    assert note.headers["Cache-Control"] == "private, no-store"
    assert set(note.get_json()) == {"id", "author_id", "note", "created"}
    page = client.get(EVENTS_URL, query_string={"user_id": submitter.id}, headers=headers)
    assert page.status_code == 200, page.get_json()
    assert page.headers["Cache-Control"] == "private, no-store"
    (event,) = page.get_json()["events"]
    assert event["notes"] == [note.get_json()]
    with app.app_context():
        assert db.session.get(PromptModerationEvent, event_id).submitted_prompt == "original"
        stored = db.session.get(PromptModerationNote, note.get_json()["id"])
        assert stored.event_id == event_id
        assert stored.author_id == note.get_json()["author_id"]


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


def test_event_listing_exposes_documented_fields_with_utc_offsets(client, app, api_key, make_api_user, monkeypatch) -> None:
    """Each listed event carries exactly the documented field allowlist, and stored naive UTC times carry a zero offset.

    The page also reports the retention windows in force, with null for a window kept until the ceiling.
    """
    from horde.database import prompt_moderation

    monkeypatch.setattr(prompt_moderation, "MODERATION_RETENTION_POLICY", _policy(text=None, ipaddr=12, ceiling=90))
    submitter = make_api_user()
    (event_id,) = _seed(app, user_id=submitter.id)
    headers = {"apikey": api_key}
    note = client.post(f"{EVENTS_URL}/{event_id}/notes", json={"note": "checked"}, headers=headers)
    assert note.status_code == 201, note.get_json()
    page = client.get(EVENTS_URL, query_string={"user_id": submitter.id}, headers=headers)
    assert page.status_code == 200, page.get_json()
    assert set(page.get_json()) == {"events", "next_cursor", "retention"}
    assert page.get_json()["retention"] == {"text_days": None, "ipaddr_days": 12, "ceiling_days": 90}
    (event,) = page.get_json()["events"]
    assert event["id"] == event_id
    assert (event["text_redacted"], event["ipaddr_redacted"], event["anonymized"]) == (False, False, False)
    assert set(event) == {
        "id",
        "created",
        "user_id",
        "request_id",
        "worker_id",
        "proxied_account",
        "ipaddr",
        "ip_subject_key",
        "origin_text",
        "models",
        "reason",
        "outcome",
        "submitted_prompt",
        "moderation_prompt",
        "effective_prompt",
        "text_truncated",
        "text_redacted",
        "ipaddr_redacted",
        "anonymized",
        "notes",
    }
    for value in (event["created"], event["notes"][0]["created"]):
        assert value.endswith(("+00:00", "Z")), value
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0), value


def test_event_listing_and_api_model_carry_every_column_except_the_unlisted(app, make_api_user) -> None:
    """A new event column fails this until it is listed and documented, or declared unlisted.

    The listing and the API model are explicit field lists, so a column never reaches moderators by accident, and one
    meant for them is never silently missing.
    """
    from horde.apis.v2.base import models

    listed_columns = set(PromptModerationEvent.__table__.columns.keys()) - UNLISTED_EVENT_COLUMNS
    expected = listed_columns | LISTING_ONLY_EVENT_KEYS
    submitter = make_api_user()
    _seed(app, user_id=submitter.id)
    with app.app_context():
        (listed,) = get_prompt_events(limit=1, user_id=submitter.id)["events"]
    assert set(listed) == expected
    assert set(models.response_model_prompt_moderation_event) == expected


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
def test_anonymization_removes_identity_and_keeps_the_record_and_its_notes(client, app, api_key, make_api_user) -> None:
    """Past the ceiling an event keeps its reason, outcome, models, capture time, reporting worker and notes.

    A note written on the anonymized event is stored, and a later pass keeps it.
    """
    from horde.flask import db

    submitter = make_api_user()
    worker_id = str(uuid4())
    expired = _record_aged(
        app,
        user_id=submitter.id,
        age_days=31,
        reason=PromptModerationReason.WORKER_CSAM,
        effective_prompt="effective",
        request_id=str(uuid4()),
        job_id=str(uuid4()),
        worker_id=worker_id,
    )
    kept = _record_aged(app, user_id=submitter.id, age_days=1)
    with app.app_context():
        # Capture stores an address or an origin text, never both; setting both covers every removable column.
        db.session.get(PromptModerationEvent, expired).origin_text = "client behind proxy"
        db.session.commit()
    headers = {"apikey": api_key}
    kept_note = client.post(f"{EVENTS_URL}/{kept}/notes", json={"note": "unrelated"}, headers=headers)
    assert kept_note.status_code == 201, kept_note.get_json()
    before = _stored(app, expired)
    removable_fates = (RetentionFate.TEXT, RetentionFate.IPADDR, RetentionFate.IDENTITY)
    for column, fate in EVENT_COLUMN_RETENTION.items():
        if fate in removable_fates:
            assert getattr(before, column) is not None, column
    policy = _policy(text=20, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(anonymized=1)
        assert apply_evidence_retention(policy).total == 0
        assert db.session.get(PromptModerationNote, kept_note.get_json()["id"]) is not None

    after = _stored(app, expired)
    for identifying in (
        "user_id",
        "proxied_account",
        "ipaddr",
        "request_id",
        "job_id",
        "submitted_prompt",
        "moderation_prompt",
        "effective_prompt",
    ):
        assert getattr(after, identifying) is None, identifying
    assert after.text_redacted and after.ipaddr_redacted and after.anonymized
    for column, fate in EVENT_COLUMN_RETENTION.items():
        if fate in removable_fates:
            assert getattr(after, column) is None, column
        elif fate == RetentionFate.KEPT:
            assert getattr(after, column) == getattr(before, column), column
        else:
            assert getattr(after, column) is True, column
    assert (after.reason, after.outcome, after.models, after.created, after.text_truncated, after.worker_id) == (
        "worker_csam",
        "censored",
        ["model a"],
        before.created,
        before.text_truncated,
        worker_id,
    )
    assert _stored(app, kept).user_id == submitter.id

    (listed,) = client.get(EVENTS_URL, query_string={"worker_id": worker_id}, headers=headers).get_json()["events"]
    assert listed["id"] == expired
    assert listed["user_id"] is None
    assert listed["ip_subject_key"] is None
    assert (listed["text_redacted"], listed["ipaddr_redacted"], listed["anonymized"]) == (True, True, True)
    assert listed["notes"] == []


@pytest.mark.usefixtures("_no_prior_evidence")
def test_a_note_on_an_anonymized_event_is_stored(client, app, api_key, make_api_user) -> None:
    """An anonymized event takes a note, and later passes keep the note and change nothing else."""
    from horde.flask import db

    submitter = make_api_user()
    worker_id = str(uuid4())
    expired = _record_aged(app, user_id=submitter.id, age_days=31, worker_id=worker_id)
    policy = _policy(text=20, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(anonymized=1)
    headers = {"apikey": api_key}
    late_note = client.post(f"{EVENTS_URL}/{expired}/notes", json={"note": "after anonymization"}, headers=headers)
    assert late_note.status_code == 201, late_note.get_json()
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result()
        assert db.session.get(PromptModerationNote, late_note.get_json()["id"]) is not None
    (listed,) = client.get(EVENTS_URL, query_string={"worker_id": worker_id}, headers=headers).get_json()["events"]
    assert listed["anonymized"]
    assert listed["notes"] == [late_note.get_json()]


def _flag(app, user_id: int) -> None:
    """Flag the account, as a moderator does."""
    from horde.classes.base.user import User
    from horde.flask import db

    with app.app_context():
        user = db.session.get(User, user_id)
        user.set_flagged(True)
        db.session.expire_all()
        assert db.session.get(User, user_id).flagged


def _add_suspicions(app, user_id: int, count: int) -> None:
    """Give the account ``count`` distinct suspicions."""
    from horde.classes.base.user import UserSuspicions
    from horde.flask import db

    with app.app_context():
        for suspicion_id in range(count):
            db.session.add(UserSuspicions(user_id=user_id, suspicion_id=suspicion_id))
        db.session.commit()


def _make_suspicious(app, user_id: int) -> None:
    """Give the account the suspicions ``User.is_suspicious`` needs to report it suspicious."""
    from horde.classes.base.user import User
    from horde.flask import db

    _add_suspicions(app, user_id, User.SUSPICION_THRESHOLD)
    with app.app_context():
        assert db.session.get(User, user_id).is_suspicious()


@pytest.mark.usefixtures("_no_prior_evidence")
@pytest.mark.parametrize("action", ["note", "flagged", "suspicious"])
def test_events_under_moderation_action_are_skipped_by_every_retention_step(client, app, api_key, make_api_user, action: str) -> None:
    """An event with a note, or of a flagged or suspicious account, keeps every value past every window and the ceiling.

    An unactioned event of the same age is anonymized in the same pass, so the pass did run.
    """
    submitter, bystander = make_api_user(), make_api_user()
    actioned = _record_aged(app, user_id=submitter.id, age_days=400)
    unactioned = _record_aged(app, user_id=bystander.id, age_days=400)
    if action == "note":
        note = client.post(f"{EVENTS_URL}/{actioned}/notes", json={"note": "under review"}, headers={"apikey": api_key})
        assert note.status_code == 201, note.get_json()
    elif action == "flagged":
        _flag(app, submitter.id)
    else:
        _make_suspicious(app, submitter.id)
    before = _stored(app, actioned)
    policy = _policy(text=10, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(anonymized=1)
        assert apply_evidence_retention(policy) == _pass_result()
    after = _stored(app, actioned)
    for column in EVENT_COLUMN_RETENTION:
        assert getattr(after, column) == getattr(before, column), column
    assert after.user_id == submitter.id
    assert after.submitted_prompt == "submitted"
    assert after.ipaddr is not None
    assert not (after.text_redacted or after.ipaddr_redacted or after.anonymized)
    assert _stored(app, unactioned).anonymized


@pytest.mark.usefixtures("_no_prior_evidence")
def test_a_trusted_account_at_the_suspicion_threshold_is_not_exempt(app, make_api_user) -> None:
    """``User.is_suspicious`` never reports a trusted account, so its events and worker reports follow every window."""
    from horde.classes.base.user import User
    from horde.flask import db

    trusted = make_api_user(trusted=True)
    _add_suspicions(app, trusted.id, User.SUSPICION_THRESHOLD)
    with app.app_context():
        user = db.session.get(User, trusted.id)
        assert len(user.suspicions) >= User.SUSPICION_THRESHOLD
        assert not user.is_suspicious()
    event = _record_aged(app, user_id=trusted.id, age_days=400)
    worker_id = _synthetic_worker(app, make_api_user().id, "synthetic-report-worker")
    report = _record_problem_job(app, user_id=trusted.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=400)
    with app.app_context():
        assert apply_evidence_retention(_policy(text=10, ipaddr=10, ceiling=30)) == _pass_result(
            anonymized=1,
            problem_jobs_deleted=1,
        )
        assert db.session.get(UserProblemJobs, report) is None
    assert _stored(app, event).anonymized


@pytest.mark.usefixtures("_no_prior_evidence")
@pytest.mark.parametrize("action", ["flagged", "suspicious"])
def test_worker_reports_of_an_account_under_moderation_action_survive_the_ceiling(app, make_api_user, action: str) -> None:
    """A worker report record of a flagged or suspicious account keeps its address and row past both windows."""
    from horde.flask import db

    submitter, bystander, worker_owner = make_api_user(), make_api_user(), make_api_user()
    worker_id = _synthetic_worker(app, worker_owner.id, "synthetic-report-worker")
    ipaddr = _unique_ip()
    past_window = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=ipaddr, age_days=15)
    past_ceiling = _record_problem_job(app, user_id=submitter.id, worker_id=worker_id, ipaddr=ipaddr, age_days=400)
    unactioned = _record_problem_job(app, user_id=bystander.id, worker_id=worker_id, ipaddr=_unique_ip(), age_days=400)
    if action == "flagged":
        _flag(app, submitter.id)
    else:
        _make_suspicious(app, submitter.id)
    policy = _policy(text=None, ipaddr=10, ceiling=30)
    with app.app_context():
        assert apply_evidence_retention(policy) == _pass_result(problem_jobs_deleted=1)
        assert apply_evidence_retention(policy) == _pass_result()
        for record in (past_window, past_ceiling):
            stored = db.session.get(UserProblemJobs, record)
            assert stored is not None
            assert (stored.user_id, stored.ipaddr) == (submitter.id, ipaddr)
        assert db.session.get(UserProblemJobs, unactioned) is None


def _listed_ids(app, **filters: Any) -> set[int]:
    """Return the IDs of the events the filters select; an ``ipaddr`` filter is parsed as the API parses it."""
    if "ipaddr" in filters:
        filters["ip_subject"] = CounterMeasures.parse_ip_subject(filters.pop("ipaddr"))
    with app.app_context():
        return {event["id"] for event in get_prompt_events(limit=100, **filters)["events"]}


@pytest.mark.usefixtures("_no_prior_evidence")
def test_address_redaction_keeps_events_in_address_filters(app, make_api_user) -> None:
    """Past the address window the address, and another address in its IPv6 /64, still find the event by its pseudonym."""
    submitter = make_api_user()
    worker_id = str(uuid4())
    proxied_account = f"proxied-{uuid4()}"
    common = {
        "user_id": submitter.id,
        "age_days": 15,
        "worker_id": worker_id,
        "proxied_account": proxied_account,
    }
    ipv4 = _unique_ip()
    ipv4_event = _record_aged(app, ipaddr=ipv4, **common)
    ipv6_event = _record_aged(app, ipaddr="2001:db8:5:6::1", **common)
    ipv6_sibling = "2001:db8:5:6::ffff"
    now = datetime.utcnow()
    retained_filters: list[dict[str, Any]] = [
        {"user_id": submitter.id},
        {"proxied_account": proxied_account},
        {"worker_id": worker_id},
        {"since": now - timedelta(days=16), "until": now - timedelta(days=14)},
    ]
    assert _listed_ids(app, ipaddr=ipv4) == {ipv4_event}
    assert _listed_ids(app, ipaddr=ipv6_sibling) == {ipv6_event}
    with app.app_context():
        assert apply_evidence_retention(_policy(text=None, ipaddr=10, ceiling=30)).ipaddr_redacted == 2
    assert _stored(app, ipv4_event).ipaddr is None
    assert _listed_ids(app, ipaddr=ipv4) == {ipv4_event}
    assert _listed_ids(app, ipaddr=f"::ffff:{ipv4}") == {ipv4_event}
    assert _listed_ids(app, ipaddr=ipv6_sibling) == {ipv6_event}
    assert _listed_ids(app, ipaddr="2001:db8:5:6::/64") == {ipv6_event}
    for filters in retained_filters:
        assert _listed_ids(app, **filters) == {ipv4_event, ipv6_event}, filters


@pytest.mark.usefixtures("_no_prior_evidence")
def test_events_without_a_pseudonym_match_by_address_until_it_is_removed(app, make_api_user, monkeypatch) -> None:
    """An event captured without a usable secret has no pseudonym, so only its address matches the address filter."""
    from horde import countermeasures

    submitter = make_api_user()
    ipaddr = _unique_ip()
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", None)
    unkeyed = _record_aged(app, user_id=submitter.id, age_days=15, ipaddr=ipaddr)
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_IP_SUBJECT_SECRET)
    keyed = _record_aged(app, user_id=submitter.id, age_days=1, ipaddr=ipaddr)
    assert _stored(app, unkeyed).ip_subject_key is None
    assert _listed_ids(app, ipaddr=ipaddr) == {unkeyed, keyed}
    with app.app_context():
        assert apply_evidence_retention(_policy(text=None, ipaddr=10, ceiling=30)).ipaddr_redacted == 1
    assert _listed_ids(app, ipaddr=ipaddr) == {keyed}


@pytest.mark.usefixtures("_no_prior_evidence")
def test_anonymization_removes_events_from_subject_filters_only(app, make_api_user) -> None:
    """Past the ceiling the account and proxied-account filters no longer find the event; worker and time filters do."""
    submitter = make_api_user()
    worker_id = str(uuid4())
    proxied_account = f"proxied-{uuid4()}"
    expired = _record_aged(app, user_id=submitter.id, age_days=31, worker_id=worker_id, proxied_account=proxied_account)
    now = datetime.utcnow()
    subject_filters: list[dict[str, Any]] = [
        {"user_id": submitter.id},
        {"proxied_account": proxied_account},
    ]
    retained_filters: list[dict[str, Any]] = [
        {"worker_id": worker_id},
        {"since": now - timedelta(days=32)},
        {"until": now - timedelta(days=30)},
    ]
    for filters in subject_filters + retained_filters:
        assert _listed_ids(app, **filters) == {expired}, filters
    with app.app_context():
        assert apply_evidence_retention(_policy(text=None, ipaddr=None, ceiling=30)).anonymized == 1
    for filters in subject_filters:
        assert _listed_ids(app, **filters) == set(), filters
    for filters in retained_filters:
        assert _listed_ids(app, **filters) == {expired}, filters


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


def test_wipe_keeps_the_accounts_evidence_and_notes(client, app, api_key, make_api_user) -> None:
    """Wiping an account leaves its live and anonymized events, their notes and their account in place."""
    from horde.classes.base.user import User
    from horde.flask import db

    wiped, other = make_api_user(), make_api_user()
    anonymized_event = _record_aged(app, user_id=wiped.id, age_days=31)
    with app.app_context():
        apply_evidence_retention(_policy(text=None, ipaddr=None, ceiling=30))
    assert _stored(app, anonymized_event).anonymized
    wiped_events = _seed(app, user_id=wiped.id, count=2)
    (other_event,) = _seed(app, user_id=other.id)
    note = client.post(f"{EVENTS_URL}/{wiped_events[0]}/notes", json={"note": "about the account"}, headers={"apikey": api_key})
    assert note.status_code == 201, note.get_json()
    with app.app_context():
        db.session.get(User, wiped.id).wipe()
        db.session.expire_all()
        assert db.session.get(User, wiped.id).is_wiped
        for event_id in wiped_events:
            assert db.session.get(PromptModerationEvent, event_id).user_id == wiped.id
        assert db.session.get(PromptModerationNote, note.get_json()["id"]) is not None
        assert db.session.get(PromptModerationEvent, other_event).user_id == other.id
        assert db.session.get(PromptModerationEvent, anonymized_event).anonymized
    assert set(wiped_events) <= _listed_ids(app, user_id=wiped.id)


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


@pytest.mark.usefixtures("_no_prior_evidence")
def test_the_retention_pass_and_the_privacy_document_read_the_policy_when_they_run(client, app, make_api_user, monkeypatch) -> None:
    """The scheduled pass applies the policy in force at the call, which is the policy the privacy document states."""
    from horde.database import prompt_moderation, threads

    submitter = make_api_user()
    past_window = _record_aged(app, user_id=submitter.id, age_days=8)
    assert _stored(app, past_window).ipaddr is not None
    monkeypatch.setattr(prompt_moderation, "MODERATION_RETENTION_POLICY", _policy(text=None, ipaddr=7, ceiling=300))
    threads.apply_moderation_retention()
    redacted = _stored(app, past_window)
    assert redacted.ipaddr is None
    assert redacted.ipaddr_redacted
    assert not redacted.anonymized
    document = client.get("/api/v2/documents/privacy", query_string={"format": "markdown"}).get_json()["markdown"]
    assert "the IP address is removed after 7 days," in document
    assert "After 300 days, a record that is not subject to moderation action is anonymized" in document


@pytest.mark.parametrize("document_format", ["html", "markdown"])
def test_privacy_document_discloses_the_configured_retention(client, api_key, monkeypatch, document_format: str) -> None:
    """The privacy document states the periods the moderator listing reports for the same policy, including a none window."""
    from horde.database import prompt_moderation

    monkeypatch.setattr(prompt_moderation, "MODERATION_RETENTION_POLICY", _policy(text=45, ipaddr=None, ceiling=200))
    response = client.get("/api/v2/documents/privacy", query_string={"format": document_format})
    assert response.status_code == 200, response.get_json()
    document = response.get_json()[document_format]
    assert "Moderation records" in document
    assert "are not removed when an Account is deleted" in document
    assert "The Prompt text is removed after 45 days, and the IP address is kept with the record," in document
    assert "unless the record is subject to moderation action or We are required to retain it" in document
    assert "After 200 days, a record that is not subject to moderation action is anonymized" in document
    for recorded in (
        "Your Account",
        "any proxied account",
        "Your IP address",
        "a pseudonym of Your IP address",
        "the request and job identifiers",
        "the reporting Worker",
        "the requested models",
        "the Prompt",
        "any notes moderators add",
    ):
        assert recorded in document, recorded
    assert "The pseudonym of the IP address is a keyed hash" in document
    assert "the reporting Worker and any moderator notes are kept" in document
    assert "The reporting Worker identifies who made the report, not You." in document
    assert "Your Account automatically receives a suspicion mark and Your IP address is placed in a temporary timeout" in document
    assert "A human moderator reviews the records and decides any further action." in document
    assert "the same 200-day period, with the same exception for moderation action." in document
    listing = client.get(EVENTS_URL, query_string={"limit": 1}, headers={"apikey": api_key})
    assert listing.status_code == 200, listing.get_json()
    retention = listing.get_json()["retention"]
    assert retention == {"text_days": 45, "ipaddr_days": None, "ceiling_days": 200}
    for subject, days in (("The Prompt text", retention["text_days"]), ("the IP address", retention["ipaddr_days"])):
        period = f"removed after {days} days" if days is not None else "kept with the record"
        assert f"{subject} is {period}," in document
    assert f"After {retention['ceiling_days']} days, a record" in document


@pytest.mark.parametrize("document_format", ["html", "markdown"])
def test_privacy_document_links_the_source_code(client, monkeypatch, document_format: str) -> None:
    """The document links the configured source repository, which defaults to the AI-Horde repository."""
    import os

    from horde import vars as horde_vars
    from horde.apis.v2 import base

    assert horde_vars.horde_repository == os.getenv("HORDE_REPOSITORY", "https://github.com/Haidra-Org/AI-Horde")
    document = client.get("/api/v2/documents/privacy", query_string={"format": document_format}).get_json()[document_format]
    assert "The Service is open source." in document
    assert horde_vars.horde_repository in document
    configured = "https://example.invalid/horde-source"
    monkeypatch.setattr(base, "horde_repository", configured)
    document = client.get("/api/v2/documents/privacy", query_string={"format": document_format}).get_json()[document_format]
    assert configured in document
    if document_format == "html":
        assert f'<a href="{configured}">{configured}</a>' in document


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


def test_prompt_event_pagination_filters_and_invalid_notes(client, app, api_key, make_api_user) -> None:
    submitter = make_api_user()
    ipaddr = _unique_ip()
    identifiers = _seed(app, user_id=submitter.id, count=3, proxied_account="proxied-listing", ipaddr=ipaddr)
    headers = {"apikey": api_key}
    query = {"user_id": submitter.id, "limit": 2}
    first = client.get(EVENTS_URL, query_string=query, headers=headers).get_json()
    assert [event["id"] for event in first["events"]] == identifiers[:0:-1]
    second = client.get(EVENTS_URL, query_string={**query, "before_id": first["next_cursor"]}, headers=headers).get_json()
    assert [event["id"] for event in second["events"]] == identifiers[:1]
    assert second["next_cursor"] is None
    by_address = client.get(EVENTS_URL, query_string={"ipaddr": ipaddr}, headers=headers).get_json()
    assert [event["id"] for event in by_address["events"]] == identifiers[::-1]
    subject_key = CounterMeasures.ip_subject_key(ipaddr)
    assert {event["ip_subject_key"] for event in by_address["events"]} == {subject_key}
    by_key = client.get(EVENTS_URL, query_string={"ip_subject_key": subject_key}, headers=headers).get_json()
    assert [event["id"] for event in by_key["events"]] == identifiers[::-1]
    by_event = client.get(EVENTS_URL, query_string={"event_id": identifiers[1]}, headers=headers).get_json()
    assert [event["id"] for event in by_event["events"]] == [identifiers[1]]
    blank_address = client.get(EVENTS_URL, query_string={**query, "ipaddr": "  "}, headers=headers).get_json()
    assert [event["id"] for event in blank_address["events"]] == identifiers[:0:-1]
    by_proxied = client.get(
        EVENTS_URL,
        query_string={"user_id": submitter.id, "proxied_account": "proxied-listing"},
        headers=headers,
    ).get_json()
    assert len(by_proxied["events"]) == 3
    future = client.get(EVENTS_URL, query_string={**query, "since": "2100-01-01T00:00:00Z"}, headers=headers)
    assert future.get_json()["events"] == []
    for invalid, rc in (
        ({"limit": 101}, "InvalidOperationsLimit"),
        ({"before_id": 0}, "InvalidOperationsCursor"),
        ({"since": "2026-01-01"}, "InvalidModerationTimeRange"),
        ({"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"}, "InvalidModerationTimeRange"),
        ({"ipaddr": "not an address"}, "InvalidModerationAddressFilter"),
        ({"ipaddr": "198.51.100.0/24"}, "InvalidModerationAddressFilter"),
        ({"ipaddr": "2001:db8::/48"}, "InvalidModerationAddressFilter"),
        ({"ip_subject_key": "not a pseudonym"}, "InvalidModerationAddressFilter"),
        ({"ip_subject_key": "A" * 64}, "InvalidModerationAddressFilter"),
        ({"event_id": 0}, "InvalidModerationEventID"),
    ):
        response = client.get(EVENTS_URL, query_string=invalid, headers=headers)
        assert response.status_code == 400, invalid
        assert response.get_json()["rc"] == rc, invalid
    notes_url = f"{EVENTS_URL}/{identifiers[0]}/notes"
    for invalid in ({}, {"note": None}, {"note": ""}, {"note": " "}, {"note": "x" * 2001}, {"note": "nul \x00"}):
        response = client.post(notes_url, json=invalid, headers=headers)
        assert response.status_code == 400, invalid
        assert response.get_json()["rc"] == "InvalidModerationNote", invalid
    missing = client.post(f"{EVENTS_URL}/0/notes", json={"note": "gone"}, headers=headers)
    assert missing.status_code == 404
    assert missing.get_json()["rc"] == "ModerationEventNotFound"


def test_moderator_limits_count_per_key(client, app, api_key, make_api_user) -> None:
    from horde.limiter import limiter

    other = make_api_user()
    limiter.enabled = True
    try:
        headers = {"apikey": api_key}
        for _ in range(120):
            assert client.get(EVENTS_URL, query_string={"limit": 1}, headers=headers).status_code == 200
        limited = client.get(EVENTS_URL, query_string={"limit": 1}, headers={**headers, "Origin": "http://localhost:4277"})
        assert limited.status_code == 429
        # A cross-origin client can read how long to wait.
        assert limited.headers["Access-Control-Allow-Origin"] == "*"
        exposed = {name.strip() for name in limited.headers["Access-Control-Expose-Headers"].split(",")}
        assert {"Retry-After", "X-RateLimit-Reset"} <= exposed
        assert int(limited.headers["Retry-After"]) > 0
        # Another key has its own count.
        assert client.get(EVENTS_URL, headers={"apikey": other.api_key}).status_code == 403
        # Notes on every event draw on one write budget: the count follows the key and the resource, not the event.
        first, second = _seed(app, user_id=other.id, count=2)
        for index in range(30):
            event_id = first if index % 2 else second
            response = client.post(f"{EVENTS_URL}/{event_id}/notes", json={"note": "limit"}, headers=headers)
            assert response.status_code == 201, response.get_json()
        for event_id in (first, second):
            assert client.post(f"{EVENTS_URL}/{event_id}/notes", json={"note": "limit"}, headers=headers).status_code == 429
    finally:
        limiter.enabled = False
        limiter.reset()
