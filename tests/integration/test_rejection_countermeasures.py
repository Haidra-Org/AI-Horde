# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise the model-path timeout on rejected prompts and the evidence text link in the moderator listing.

Repeated model rejections from one IP subject time out its address; filter rejections do not count toward it. The
suspicion and timeout Redis databases are fakeredis instances, since the test application leaves them unset.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from horde.classes.base.prompt_moderation import PromptModerationEvent, PromptModerationReason
from horde.countermeasures import CounterMeasures
from horde.database.prompt_moderation import PromptEvidence, record_prompt_evidence
from tests.fixture_types import ApiUser, MakeApiUser
from tests.integration.test_prompt_moderation import (
    EVENTS_URL,
    TEST_IP_SUBJECT_SECRET,
    _funded_submitter,
    _patch_checker,
    _unique_ip,
)
from tests.integration.test_request_parameters import IMAGE_REQUEST

TEST_TIMEOUT_THRESHOLD = 5
"""The model rejection timeout threshold the tests pin, the deployment default."""


class _CounterRecorder:
    """Stand-in for an OpenTelemetry counter that keeps the attributes of every add."""

    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, Any]]] = []

    def add(self, amount: int, attributes: dict[str, Any] | None = None) -> None:
        self.adds.append((amount, dict(attributes or {})))


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Wire fakeredis into the IP caches, pin the policy and capture the moderator notices sent."""
    import fakeredis

    from horde import countermeasures
    from horde.apis.v2 import base
    from horde.database import prompt_moderation
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    monkeypatch.setattr(countermeasures, "ip_s_r", fakeredis.FakeStrictRedis())
    monkeypatch.setattr(countermeasures, "ip_t_r", fakeredis.FakeStrictRedis())
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_IP_SUBJECT_SECRET)
    monkeypatch.setattr(prompt_moderation, "MODEL_REJECTION_TIMEOUT_THRESHOLD", TEST_TIMEOUT_THRESHOLD)
    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    sent: list[str] = []
    monkeypatch.setattr(base, "send_problem_user_notification", sent.append)
    yield sent
    limiter.enabled = previous


@pytest.fixture
def countermeasure_metric(monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
    """Replace the countermeasure counter with a recorder."""
    from horde.apis.v2 import base

    recorder = _CounterRecorder()
    monkeypatch.setattr(base, "moderation_countermeasures", recorder)
    return recorder


def _patch_model_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every image request a model rejection: the NSFW model check fires and its replacement empties the prompt."""
    _patch_checker(monkeypatch, 0, check_nsfw_model_block=True, nsfw_model_prompt_replace=None)


def _reject(client, submitter: ApiUser, ipaddr: str, times: int = 1, body: dict[str, Any] = IMAGE_REQUEST) -> None:
    """Submit ``times`` image requests from ``ipaddr`` that the patched checker rejects."""
    for _ in range(times):
        response = client.post(
            "/api/v2/generate/async",
            json=body,
            headers={"apikey": submitter.api_key},
            environ_base={"REMOTE_ADDR": ipaddr},
        )
        assert response.status_code == 400, response.get_json()


def _events(app, user_id: int) -> list[PromptModerationEvent]:
    """Return the account's events, oldest first, detached from the session."""
    from horde.flask import db

    with app.app_context():
        events = list(
            db.session.execute(
                select(PromptModerationEvent).filter_by(user_id=user_id).order_by(PromptModerationEvent.id),
            ).scalars(),
        )
        for event in events:
            db.session.expunge(event)
    return events


def test_model_rejections_past_the_threshold_time_out_the_address(
    client,
    app,
    make_api_user: MakeApiUser,
    settle_kudos,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[str],
    countermeasure_metric: _CounterRecorder,
) -> None:
    """The threshold's worth of model rejections in the window leave the address free; the next one times it out.

    Moderators get one notice carrying the pseudonym prefix and neither the prompt nor the address.
    """
    _patch_model_rejection(monkeypatch)
    submitter = _funded_submitter(make_api_user, settle_kudos)
    ipaddr = _unique_ip()
    _reject(client, submitter, ipaddr, times=TEST_TIMEOUT_THRESHOLD)
    assert CounterMeasures.retrieve_timeout(ipaddr) == 0
    assert notices == []
    assert countermeasure_metric.adds == []

    _reject(client, submitter, ipaddr)
    assert CounterMeasures.retrieve_timeout(ipaddr) > 0
    (notice,) = notices
    subject_key = CounterMeasures.ip_subject_key(CounterMeasures.ip_subject(ipaddr))
    assert subject_key is not None
    assert subject_key[:12] in notice
    assert subject_key not in notice
    assert ipaddr not in notice
    assert IMAGE_REQUEST["prompt"] not in notice
    assert f"{TEST_TIMEOUT_THRESHOLD + 1} model rejections in the past hour." in notice
    assert f"IP timeout: model rejections past {TEST_TIMEOUT_THRESHOLD} per hour time out the address." in notice
    assert "Muted for this subject for 60 minutes." in notice
    latest = _events(app, submitter.id)[-1]
    assert f"Latest: event {latest.id}." in notice
    assert f"Review: event {latest.id} in the frontpage Prompts tab." in notice
    assert countermeasure_metric.adds == [(1, {"horde.action": "timeout"})]


def test_a_timed_out_address_is_refused_before_moderation(
    client,
    app,
    make_api_user: MakeApiUser,
    settle_kudos,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[str],
) -> None:
    """Once timed out, the address's next request is refused without recording another event."""
    _patch_model_rejection(monkeypatch)
    submitter = _funded_submitter(make_api_user, settle_kudos)
    ipaddr = _unique_ip()
    _reject(client, submitter, ipaddr, times=TEST_TIMEOUT_THRESHOLD + 1)
    response = client.post(
        "/api/v2/generate/async",
        json=IMAGE_REQUEST,
        headers={"apikey": submitter.api_key},
        environ_base={"REMOTE_ADDR": ipaddr},
    )
    assert response.status_code == 403, response.get_json()
    assert len(_events(app, submitter.id)) == TEST_TIMEOUT_THRESHOLD + 1


def test_moderators_are_exempt_from_the_timeout(
    client,
    app,
    make_api_user: MakeApiUser,
    settle_kudos,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[str],
) -> None:
    """A moderator's model rejections past the threshold leave the address free and send no notice.

    Moderators experiment with the filter, so the timeout does not apply.
    """
    _patch_model_rejection(monkeypatch)
    submitter = make_api_user(moderator=True, kudos=1000)
    settle_kudos()
    ipaddr = _unique_ip()
    _reject(client, submitter, ipaddr, times=TEST_TIMEOUT_THRESHOLD + 2)
    assert CounterMeasures.retrieve_timeout(ipaddr) == 0
    assert notices == []


def test_raid_mode_times_out_the_first_model_rejection(
    client,
    app,
    make_api_user: MakeApiUser,
    settle_kudos,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[str],
) -> None:
    """Raid mode times out the address on the first model rejection, as the filter path always does."""
    from horde.classes.base import settings

    _patch_model_rejection(monkeypatch)
    monkeypatch.setattr(settings, "mode_raid", lambda: True)
    submitter = _funded_submitter(make_api_user, settle_kudos)
    ipaddr = _unique_ip()
    _reject(client, submitter, ipaddr)
    assert CounterMeasures.retrieve_timeout(ipaddr) > 0
    (notice,) = notices
    assert "1 model rejection in the past hour." in notice
    assert "IP timeout: every model rejection times out the address (raid mode)." in notice


def test_filter_rejections_do_not_count_toward_the_model_threshold(
    client,
    app,
    make_api_user: MakeApiUser,
    settle_kudos,
    monkeypatch: pytest.MonkeyPatch,
    notices: list[str],
) -> None:
    """A threshold's worth of filter rejections leaves no model rejection count, so the next model rejection is free.

    Every rejection keeps its text pending upload.
    """
    from horde import countermeasures

    submitter = _funded_submitter(make_api_user, settle_kudos)
    ipaddr = _unique_ip()
    # An opt-in replacement refused for length is a filter rejection that sets no timeout of its own, so the address
    # stays free to send the model rejection that follows.
    _patch_checker(monkeypatch, 2, check_prompt_replacement_length=False)
    _reject(client, submitter, ipaddr, times=TEST_TIMEOUT_THRESHOLD, body={**IMAGE_REQUEST, "replacement_filter": True})
    subject = CounterMeasures.ip_subject(ipaddr)
    assert countermeasures.ip_s_r.exists(f"{countermeasures.MODEL_REJECTION_COUNT_KEY_PREFIX}{subject}") == 0
    _patch_model_rejection(monkeypatch)
    _reject(client, submitter, ipaddr)
    assert CounterMeasures.retrieve_timeout(ipaddr) == 0
    assert notices == []
    events = _events(app, submitter.id)
    assert [event.reason for event in events] == ["filter_rejection"] * TEST_TIMEOUT_THRESHOLD + ["model_rejection"]
    assert {event.text_state for event in events} == {"pending"}


def test_listing_links_stored_text_and_inlines_pending_text(
    client,
    app,
    api_key: str,
    make_api_user: MakeApiUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored event carries a text link and no inline stages; a pending event carries its stages and no link."""
    from horde import r2
    from horde.flask import db

    monkeypatch.setattr(r2, "evidence_text_url", lambda event_id: f"https://evidence.invalid/{event_id}")
    submitter = make_api_user()
    with app.app_context():
        stored_id, pending_id = (
            record_prompt_evidence(
                PromptEvidence(
                    user_id=submitter.id,
                    reason=PromptModerationReason.FILTER_REJECTION,
                    submitted_prompt=prompt,
                    moderation_prompt=None,
                    effective_prompt=None,
                ),
            )
            for prompt in ("uploaded synthetic", "pending synthetic")
        )
        stored = db.session.get(PromptModerationEvent, stored_id)
        assert stored is not None
        stored.submitted_prompt = None
        stored.text_state = "stored"
        db.session.commit()
    page = client.get(EVENTS_URL, query_string={"user_id": submitter.id}, headers={"apikey": api_key})
    assert page.status_code == 200, page.get_json()
    listed = {event["id"]: event for event in page.get_json()["events"]}
    assert listed[stored_id]["text_state"] == "stored"
    assert listed[stored_id]["text_url"] == f"https://evidence.invalid/{stored_id}"
    assert listed[stored_id]["submitted_prompt"] is None
    assert listed[pending_id]["text_state"] == "pending"
    assert listed[pending_id]["text_url"] is None
    assert listed[pending_id]["submitted_prompt"] == "pending synthetic"
    assert listed[pending_id]["text_sha256"] is not None
    assert listed[pending_id]["text_chars"] == len("pending synthetic")
