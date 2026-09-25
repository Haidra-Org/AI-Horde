# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Semantic coverage for moderator operational-review endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from horde.suspicions import Suspicions

AGENT = "aihorde_ci_client:1.0:(test)ci"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from horde.limiter import limiter

    monkeypatch.setattr("horde.classes.base.worker.send_pause_notification", lambda *_: None)

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def test_moderation_overview_requires_moderator(client, make_api_user) -> None:
    ordinary = make_api_user(moderator=False)

    missing = client.get("/api/v2/operations/moderation")
    assert missing.status_code == 400

    denied = client.get("/api/v2/operations/moderation", headers=_headers(ordinary.api_key))
    assert denied.status_code == 403
    assert denied.get_json()["rc"] == "NotModerator"


def test_moderation_overview_separates_suspicion_blocked_from_eligible_users(
    client,
    app,
    api_key,
    make_api_user,
    monkeypatch,
) -> None:
    from horde.classes.base.user import User, UserSuspicions
    from horde.classes.base.worker import WorkerModel, WorkerSuspicions
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
    blocked = make_api_user(trusted=False)
    delayed = make_api_user(trusted=False)
    worker_owner = make_api_user(trusted=False)

    with app.app_context():
        for user_id in (blocked.id, delayed.id):
            user = db.session.get(User, user_id)
            user.created = datetime.utcnow() - timedelta(days=8)
            user.evaluating_kudos = 250
        for _ in range(User.SUSPICION_THRESHOLD):
            db.session.add(UserSuspicions(user_id=blocked.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)))

        worker = TextWorker(
            user_id=worker_owner.id,
            name=f"paused-review-{worker_owner.id}",
            paused=True,
            max_context_length=4096,
            bridge_agent=AGENT,
        )
        db.session.add(worker)
        db.session.flush()
        db.session.add(WorkerModel(worker_id=worker.id, model="elinas/chronos-70b-v2"))
        db.session.add(WorkerSuspicions(worker_id=worker.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)))
        worker_id = str(worker.id)
        db.session.commit()

    response = client.get("/api/v2/operations/moderation?limit=100", headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()

    blocked_by_id = {row["id"]: row for row in body["promotion_blocked_users"]}
    delayed_by_id = {row["id"]: row for row in body["promotion_eligible_users"]}
    paused_by_id = {row["id"]: row for row in body["paused_workers"]}
    assert blocked_by_id[blocked.id]["blockers"] == ["suspicion"]
    assert blocked_by_id[blocked.id]["suspicious"] == User.SUSPICION_THRESHOLD
    assert blocked_by_id[blocked.id]["suspicion_reasons"][0]["name"] == "UNREASONABLY_FAST"
    assert delayed_by_id[delayed.id]["blockers"] == []
    assert paused_by_id[worker_id]["suspicious"] == 1
    assert paused_by_id[worker_id]["models"] == ["elinas/chronos-70b-v2"]
    assert paused_by_id[worker_id]["owner_id"] == worker_owner.id


def test_worker_suspicion_events_are_filterable_and_survive_reset(
    client,
    app,
    api_key,
    make_api_user,
    monkeypatch,
) -> None:
    from horde.classes.base.worker import WorkerSuspicionEvent
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    owner = make_api_user(trusted=False)
    with app.app_context():
        worker = TextWorker(
            user_id=owner.id,
            name=f"suspicion-audit-{owner.id}",
            max_context_length=4096,
            bridge_agent=AGENT,
        )
        db.session.add(worker)
        db.session.commit()
        worker_id = str(worker.id)
        monkeypatch.setattr("horde.classes.base.worker.send_pause_notification", lambda _message: None)

        worker.report_suspicion(
            reason=Suspicions.UNREASONABLY_FAST,
            formats=["999 > 30"],
        )
        worker.reset_suspicion()
        assert db.session.query(WorkerSuspicionEvent).filter_by(worker_id=worker.id).count() == 1
        db.session.expire(worker, ["suspicions"])
        db.session.delete(worker)
        db.session.commit()
        assert db.session.query(WorkerSuspicionEvent).filter_by(worker_id=worker_id).count() == 1

    response = client.get(
        "/api/v2/operations/worker_suspicion_events",
        query_string={"worker_id": worker_id, "suspicion_id": int(Suspicions.UNREASONABLY_FAST)},
        headers=_headers(api_key),
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    events = response.get_json()["events"]
    assert len(events) == 1
    assert events[0]["worker_id"] == worker_id
    assert events[0]["user_id"] == owner.id
    assert events[0]["reason"] == "UNREASONABLY_FAST"
    assert events[0]["detail"] == "Generation unreasonably fast (999 > 30)"


def test_worker_suspicion_events_reject_invalid_filters(client, api_key) -> None:
    invalid_worker = client.get(
        "/api/v2/operations/worker_suspicion_events?worker_id=not-a-uuid",
        headers=_headers(api_key),
    )
    assert invalid_worker.status_code == 400
    assert invalid_worker.get_json()["rc"] == "InvalidWorkerID"

    invalid_reason = client.get(
        "/api/v2/operations/worker_suspicion_events?suspicion_id=999",
        headers=_headers(api_key),
    )
    assert invalid_reason.status_code == 400
    assert invalid_reason.get_json()["rc"] == "InvalidSuspicionID"


def _make_worker(app, owner_id: int, name: str, *, paused: bool = False) -> str:
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = TextWorker(
            user_id=owner_id,
            name=name,
            paused=paused,
            max_context_length=4096,
            bridge_agent=AGENT,
        )
        db.session.add(worker)
        db.session.commit()
        return str(worker.id)


def test_repeated_non_accumulating_suspicion_records_one_event(app, make_api_user) -> None:
    """A reason the worker already carries is recorded again only when it accumulates.

    ``WORKER_PROFANITY`` reported in two separate requests leaves one history row,
    while ``UNREASONABLY_FAST`` reported in two separate requests leaves two.
    """
    from horde.classes.base.worker import WorkerSuspicionEvent, WorkerTemplate
    from horde.flask import db

    owner = make_api_user(trusted=False)
    worker_id = _make_worker(app, owner.id, f"suspicion-repeat-{owner.id}")

    for reason, formats in (
        (Suspicions.WORKER_PROFANITY, ["profane"]),
        (Suspicions.WORKER_PROFANITY, ["profane"]),
        (Suspicions.UNREASONABLY_FAST, ["999 > 30"]),
        (Suspicions.UNREASONABLY_FAST, ["999 > 30"]),
    ):
        with app.app_context():
            worker = db.session.get(WorkerTemplate, worker_id)
            worker.report_suspicion(reason=reason, formats=formats)
            db.session.commit()

    with app.app_context():
        events = db.session.query(WorkerSuspicionEvent).filter_by(worker_id=worker_id)
        assert events.filter_by(suspicion_id=int(Suspicions.WORKER_PROFANITY)).count() == 1
        assert events.filter_by(suspicion_id=int(Suspicions.UNREASONABLY_FAST)).count() == 2


def test_moderation_overview_lists_only_promotion_candidates(
    client,
    app,
    api_key,
    make_api_user,
    monkeypatch,
) -> None:
    """Promotion queues hold only untrusted registered users over the threshold for at least 7 days.

    Trusted users, users at or under the threshold, accounts younger than 7 days and
    the anonymous user appear in neither queue. A user at the suspicion threshold
    appears only in the blocked queue.
    """
    from horde.classes.base.user import User, UserSuspicions
    from horde.flask import db

    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
    qualifying = make_api_user(trusted=False)
    blocked = make_api_user(trusted=False)
    trusted = make_api_user(trusted=True)
    under_threshold = make_api_user(trusted=False)
    too_new = make_api_user(trusted=False)

    old = datetime.utcnow() - timedelta(days=8)
    seeded = {
        qualifying.id: (old, 250),
        blocked.id: (old, 250),
        trusted.id: (old, 250),
        under_threshold.id: (old, 50),
        too_new.id: (datetime.utcnow() - timedelta(days=6), 250),
    }
    with app.app_context():
        for user_id, (created, evaluating_kudos) in seeded.items():
            user = db.session.get(User, user_id)
            user.created = created
            user.evaluating_kudos = evaluating_kudos
        for _ in range(User.SUSPICION_THRESHOLD):
            db.session.add(UserSuspicions(user_id=blocked.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)))
        anon = db.session.query(User).filter_by(oauth_id="anon").one()
        anon_id = anon.id
        anon_state = (anon.created, anon.evaluating_kudos)
        anon.created = old
        anon.evaluating_kudos = 250
        db.session.commit()

    try:
        response = client.get("/api/v2/operations/moderation?limit=500", headers=_headers(api_key))
    finally:
        with app.app_context():
            anon = db.session.get(User, anon_id)
            anon.created, anon.evaluating_kudos = anon_state
            db.session.commit()

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    eligible_ids = {row["id"] for row in body["promotion_eligible_users"]}
    blocked_ids = {row["id"] for row in body["promotion_blocked_users"]}

    assert qualifying.id in eligible_ids
    assert qualifying.id not in blocked_ids
    assert blocked.id in blocked_ids
    assert blocked.id not in eligible_ids
    for excluded in (trusted.id, under_threshold.id, too_new.id, anon_id):
        assert excluded not in eligible_ids
        assert excluded not in blocked_ids


def test_promotion_queues_match_check_for_trust(
    client,
    app,
    api_key,
    make_api_user,
    monkeypatch,
) -> None:
    """The overview's promotion queues are defined by ``User.check_for_trust``.

    A seeded account is in the eligible queue exactly when ``check_for_trust`` would
    promote it now, and in the blocked queue exactly when it would not promote it now
    but would once its suspicions were cleared. The overview may narrow its candidate
    query however it likes, but the queues it returns must be the method's decisions. Each ``check_for_trust`` call runs inside a
    savepoint that is rolled back, and its session commits are reduced to flushes so
    the savepoint holds every write it makes.
    """
    from horde.classes.base.user import User, UserSuspicions
    from horde.flask import db

    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
    qualifying = make_api_user(trusted=False)
    blocked = make_api_user(trusted=False)
    trusted = make_api_user(trusted=True)
    under_threshold = make_api_user(trusted=False)
    too_new = make_api_user(trusted=False)

    old = datetime.utcnow() - timedelta(days=8)
    seeded = {
        qualifying.id: (old, 250),
        blocked.id: (old, 250),
        trusted.id: (old, 250),
        under_threshold.id: (old, 50),
        too_new.id: (datetime.utcnow() - timedelta(days=6), 250),
    }
    with app.app_context():
        for user_id, (created, evaluating_kudos) in seeded.items():
            user = db.session.get(User, user_id)
            user.created = created
            user.evaluating_kudos = evaluating_kudos
        for _ in range(User.SUSPICION_THRESHOLD):
            db.session.add(UserSuspicions(user_id=blocked.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)))
        anon = db.session.query(User).filter_by(oauth_id="anon").one()
        anon_id = anon.id
        anon_state = (anon.created, anon.evaluating_kudos)
        anon.created = old
        anon.evaluating_kudos = 250
        db.session.commit()
    seeded_ids = {*seeded, anon_id}

    def promotes(user_id: int, *, clear_suspicion: bool) -> bool:
        with app.app_context():
            user = db.session.get(User, user_id)
            was_trusted = user.trusted
            savepoint = db.session.begin_nested()
            try:
                with monkeypatch.context() as patch:
                    # set_trusted and project_trust_promotion commit; a commit would
                    # end the outer transaction and persist past the savepoint.
                    patch.setattr(db.session, "commit", db.session.flush)
                    if clear_suspicion:
                        db.session.query(UserSuspicions).filter_by(user_id=user_id).delete()
                        db.session.expire(user, ["suspicions"])
                    user.check_for_trust()
                    db.session.flush()
                    db.session.expire(user, ["roles"])
                    now_trusted = user.trusted
            finally:
                savepoint.rollback()
            db.session.expire_all()
            restored = db.session.get(User, user_id)
            assert restored.trusted == was_trusted
            if clear_suspicion:
                assert len(restored.suspicions) == (User.SUSPICION_THRESHOLD if user_id == blocked.id else 0)
            db.session.rollback()
            return now_trusted and not was_trusted

    try:
        promoted_now = {user_id for user_id in seeded_ids if promotes(user_id, clear_suspicion=False)}
        promoted_if_cleared = {user_id for user_id in seeded_ids if promotes(user_id, clear_suspicion=True)}
        response = client.get("/api/v2/operations/moderation?limit=500", headers=_headers(api_key))
    finally:
        with app.app_context():
            anon = db.session.get(User, anon_id)
            anon.created, anon.evaluating_kudos = anon_state
            db.session.commit()

    assert promoted_now
    assert promoted_if_cleared - promoted_now
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    eligible_ids = {row["id"] for row in body["promotion_eligible_users"]} & seeded_ids
    blocked_ids = {row["id"] for row in body["promotion_blocked_users"]} & seeded_ids

    assert eligible_ids == promoted_now
    assert blocked_ids == promoted_if_cleared - promoted_now


def test_moderation_overview_without_trust_threshold_disables_promotion_queues(
    client,
    app,
    api_key,
    make_api_user,
    monkeypatch,
) -> None:
    """Without a configured kudos trust threshold, promotion is disabled and only paused workers list."""
    from horde.classes.base.user import User
    from horde.flask import db

    monkeypatch.delenv("KUDOS_TRUST_THRESHOLD", raising=False)
    candidate = make_api_user(trusted=False)
    worker_owner = make_api_user(trusted=False)
    with app.app_context():
        user = db.session.get(User, candidate.id)
        user.created = datetime.utcnow() - timedelta(days=8)
        user.evaluating_kudos = 250
        db.session.commit()
    worker_id = _make_worker(app, worker_owner.id, f"paused-no-threshold-{worker_owner.id}", paused=True)

    response = client.get("/api/v2/operations/moderation?limit=500", headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()

    assert body["promotion_enabled"] is False
    assert body["promotion_threshold"] is None
    assert body["promotion_blocked_users"] == []
    assert body["promotion_eligible_users"] == []
    assert worker_id in {row["id"] for row in body["paused_workers"]}


def test_worker_suspicion_events_expire_after_retention(app) -> None:
    """Worker suspicion events older than 90 days are pruned and younger events are kept."""
    import uuid

    from horde.classes.base.worker import WorkerSuspicionEvent
    from horde.database.prompt_moderation import prune_moderation_evidence
    from horde.flask import db

    worker_id = str(uuid.uuid4())
    now = datetime.utcnow()
    with app.app_context():
        expired = WorkerSuspicionEvent(
            created=now - timedelta(days=91),
            worker_id=worker_id,
            worker_name="retention-expired",
            user_id=1,
            suspicion_id=int(Suspicions.UNREASONABLY_FAST),
            detail="expired",
        )
        retained = WorkerSuspicionEvent(
            created=now - timedelta(days=89),
            worker_id=worker_id,
            worker_name="retention-retained",
            user_id=1,
            suspicion_id=int(Suspicions.UNREASONABLY_FAST),
            detail="retained",
        )
        db.session.add_all([expired, retained])
        db.session.commit()
        expired_id, retained_id = expired.id, retained.id

        assert prune_moderation_evidence() >= 1
        db.session.expire_all()

        assert db.session.get(WorkerSuspicionEvent, expired_id) is None
        assert db.session.get(WorkerSuspicionEvent, retained_id) is not None


def test_worker_suspicion_events_require_moderator(client, make_api_user) -> None:
    """Worker suspicion history refuses requests without a key or from a non-moderator."""
    ordinary = make_api_user(moderator=False)

    missing = client.get("/api/v2/operations/worker_suspicion_events")
    assert missing.status_code == 400

    denied = client.get("/api/v2/operations/worker_suspicion_events", headers=_headers(ordinary.api_key))
    assert denied.status_code == 403
    assert denied.get_json()["rc"] == "NotModerator"


@pytest.mark.parametrize("path", ["/api/v2/operations/moderation", "/api/v2/operations/worker_suspicion_events"])
def test_operations_responses_are_not_cacheable(client, api_key, path: str) -> None:
    """Moderator operations responses carry ``Cache-Control: private, no-store``."""
    response = client.get(path, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("path", ["/api/v2/operations/moderation", "/api/v2/operations/worker_suspicion_events"])
@pytest.mark.parametrize("limit", [0, 501])
def test_operations_reject_out_of_bounds_limit(client, api_key, path: str, limit: int) -> None:
    """A page limit outside 1-500 is rejected."""
    response = client.get(path, query_string={"limit": limit}, headers=_headers(api_key))
    assert response.status_code == 400
    assert response.get_json()["rc"] == "InvalidOperationsLimit"


@pytest.mark.parametrize("path", ["/api/v2/operations/moderation", "/api/v2/operations/worker_suspicion_events"])
def test_operations_accept_maximum_limit(client, api_key, path: str) -> None:
    """A page limit of 500 is accepted."""
    response = client.get(path, query_string={"limit": 500}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)


def test_worker_suspicion_events_reject_non_positive_cursor(client, api_key) -> None:
    """A ``before_id`` below 1 is rejected."""
    response = client.get(
        "/api/v2/operations/worker_suspicion_events",
        query_string={"before_id": 0},
        headers=_headers(api_key),
    )
    assert response.status_code == 400
    assert response.get_json()["rc"] == "InvalidOperationsCursor"


def test_worker_suspicion_events_paginate_and_filter_by_user(client, app, api_key, make_api_user) -> None:
    """Events page newest first through ``next_cursor``, and ``user_id`` restricts results to that owner."""
    from horde.classes.base.worker import WorkerSuspicionEvent
    from horde.flask import db

    owner = make_api_user(trusted=False)
    other_owner = make_api_user(trusted=False)
    worker_id = _make_worker(app, owner.id, f"paginated-events-{owner.id}")
    other_worker_id = _make_worker(app, other_owner.id, f"filtered-events-{other_owner.id}")

    with app.app_context():
        seeded = [
            WorkerSuspicionEvent(
                worker_id=worker_id,
                worker_name="paginated",
                user_id=owner.id,
                suspicion_id=int(Suspicions.UNREASONABLY_FAST),
                detail=f"event {index}",
            )
            for index in range(3)
        ]
        other = WorkerSuspicionEvent(
            worker_id=other_worker_id,
            worker_name="filtered",
            user_id=other_owner.id,
            suspicion_id=int(Suspicions.UNREASONABLY_FAST),
            detail="other owner",
        )
        db.session.add_all([*seeded, other])
        db.session.commit()
        seeded_ids = sorted((event.id for event in seeded), reverse=True)
        other_id = other.id

    first = client.get(
        "/api/v2/operations/worker_suspicion_events",
        query_string={"worker_id": worker_id, "limit": 2},
        headers=_headers(api_key),
    )
    assert first.status_code == 200, first.get_data(as_text=True)
    first_body = first.get_json()
    assert [event["id"] for event in first_body["events"]] == seeded_ids[:2]
    assert first_body["next_cursor"] == seeded_ids[1]

    second = client.get(
        "/api/v2/operations/worker_suspicion_events",
        query_string={"worker_id": worker_id, "limit": 2, "before_id": first_body["next_cursor"]},
        headers=_headers(api_key),
    )
    assert second.status_code == 200, second.get_data(as_text=True)
    second_body = second.get_json()
    assert [event["id"] for event in second_body["events"]] == seeded_ids[2:]
    assert second_body["next_cursor"] is None

    filtered = client.get(
        "/api/v2/operations/worker_suspicion_events",
        query_string={"user_id": other_owner.id},
        headers=_headers(api_key),
    )
    assert filtered.status_code == 200, filtered.get_data(as_text=True)
    assert [event["id"] for event in filtered.get_json()["events"]] == [other_id]
