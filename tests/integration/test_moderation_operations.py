# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Semantic coverage for moderator operational-review endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from horde.suspicions import Suspicions

if TYPE_CHECKING:
    from horde.database.prompt_moderation import RetentionPolicy

AGENT = "aihorde_ci_client:1.0:(test)ci"
OVERVIEW_PATH = "/api/v2/operations/moderation"
EVENTS_PATH = "/api/v2/operations/moderation/worker_suspicion_events"
OPERATIONS_PATHS = [OVERVIEW_PATH, EVENTS_PATH]


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


def _assert_utc_timestamp(value: str) -> None:
    assert value.endswith(("+00:00", "Z")), value
    assert datetime.fromisoformat(value).utcoffset() == timedelta(0), value


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


def test_suspicion_descriptions_have_no_placeholders() -> None:
    from horde.database.moderation import suspicion_description

    for reason in Suspicions:
        description = suspicion_description(reason)
        assert description, reason
        assert "{" not in description and "}" not in description, description
        assert "()" not in description and "[]" not in description, description
        assert description == description.strip(), description
    assert suspicion_description(Suspicions.WORKER_PROFANITY) == "Discovered profanity in worker name"
    assert suspicion_description(Suspicions.UNREASONABLY_FAST) == "Generation unreasonably fast"


@pytest.mark.parametrize("path", OPERATIONS_PATHS)
def test_operations_require_moderator(client, make_api_user, path: str) -> None:
    """Operations endpoints refuse requests without a key or from a non-moderator."""
    ordinary = make_api_user(moderator=False)

    missing = client.get(path)
    assert missing.status_code == 400

    denied = client.get(path, headers=_headers(ordinary.api_key))
    assert denied.status_code == 403
    assert denied.get_json()["rc"] == "NotModerator"


def test_promotion_queues_match_check_for_trust(
    client,
    app,
    api_key,
    make_api_user,
    monkeypatch,
) -> None:
    """The overview's promotion queues are the decisions of ``User.check_for_trust``.

    An account is in the eligible queue exactly when ``check_for_trust`` would promote it
    now, and in the blocked queue exactly when it would promote it only once its suspicions
    were cleared. The fractional threshold and the accounts on either side of it and of the
    suspicion threshold check the SQL filter at its boundaries. A trusted account, accounts
    at or under the threshold, one younger than seven days, and the anonymous user are in
    neither queue. Each queue is ordered by evaluating kudos, highest first.

    Each ``check_for_trust`` call runs inside a savepoint that is rolled back, and its
    session commits are reduced to flushes so the savepoint holds every write it makes.
    """
    from horde.classes.base.user import User, UserSuspicions
    from horde.flask import db

    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100.5")
    qualifying = make_api_user(trusted=False)
    just_over = make_api_user(trusted=False)
    near_suspicious = make_api_user(trusted=False)
    blocked = make_api_user(trusted=False)
    trusted = make_api_user(trusted=True)
    at_integer_floor = make_api_user(trusted=False)
    under_threshold = make_api_user(trusted=False)
    too_new = make_api_user(trusted=False)

    old = datetime.utcnow() - timedelta(days=8)
    seeded = {
        qualifying.id: (old, 250),
        just_over.id: (old, 101),
        near_suspicious.id: (old, 250),
        blocked.id: (old, 250),
        trusted.id: (old, 250),
        at_integer_floor.id: (old, 100),
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
        for _ in range(User.SUSPICION_THRESHOLD - 1):
            db.session.add(UserSuspicions(user_id=near_suspicious.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)))
        anon = db.session.query(User).filter_by(oauth_id="anon").one()
        anon_id = anon.id
        anon_state = (anon.created, anon.evaluating_kudos)
        anon.created = old
        anon.evaluating_kudos = 250
        db.session.commit()
    seeded_ids = {*seeded, anon_id}
    suspicion_counts = {blocked.id: User.SUSPICION_THRESHOLD, near_suspicious.id: User.SUSPICION_THRESHOLD - 1}

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
                assert len(restored.suspicions) == suspicion_counts.get(user_id, 0)
            db.session.rollback()
            return now_trusted and not was_trusted

    try:
        promoted_now = {user_id for user_id in seeded_ids if promotes(user_id, clear_suspicion=False)}
        promoted_if_cleared = {user_id for user_id in seeded_ids if promotes(user_id, clear_suspicion=True)}
        response = client.get(OVERVIEW_PATH, query_string={"limit": 500}, headers=_headers(api_key))
    finally:
        with app.app_context():
            anon = db.session.get(User, anon_id)
            anon.created, anon.evaluating_kudos = anon_state
            db.session.commit()

    assert promoted_now == {qualifying.id, just_over.id, near_suspicious.id}
    assert promoted_if_cleared - promoted_now == {blocked.id}
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body["promotion_threshold"] == 100.5
    assert body["suspicion_threshold"] == User.SUSPICION_THRESHOLD
    eligible_rows = {row["id"]: row for row in body["promotion_eligible_users"]}
    blocked_rows = {row["id"]: row for row in body["promotion_blocked_users"]}
    assert eligible_rows.keys() & seeded_ids == promoted_now
    assert blocked_rows.keys() & seeded_ids == promoted_if_cleared - promoted_now
    for queue in (body["promotion_eligible_users"], body["promotion_blocked_users"]):
        ranks = [(-row["evaluating_kudos"], row["id"]) for row in queue]
        assert ranks == sorted(ranks)

    blocked_row = blocked_rows[blocked.id]
    assert blocked_row["suspicious"] == User.SUSPICION_THRESHOLD
    assert blocked_row["suspicion_reasons"] == [
        {
            "id": int(Suspicions.UNREASONABLY_FAST),
            "name": "UNREASONABLY_FAST",
            "description": "Generation unreasonably fast",
            "count": User.SUSPICION_THRESHOLD,
        },
    ]
    for row in (blocked_row, eligible_rows[qualifying.id]):
        _assert_utc_timestamp(row["last_active"])


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

    response = client.get(OVERVIEW_PATH, query_string={"limit": 500}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()

    assert body["promotion_threshold"] is None
    assert body["promotion_blocked_users"] == []
    assert body["promotion_eligible_users"] == []
    assert worker_id in {row["id"] for row in body["paused_workers"]}


@pytest.mark.parametrize("path", OPERATIONS_PATHS)
def test_operations_accept_maximum_limit_privately(client, api_key, path: str) -> None:
    """A page limit of 500 is accepted, and the response carries ``Cache-Control: private, no-store``."""
    response = client.get(path, query_string={"limit": 500}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    ("path", "query", "rc"),
    [
        *((path, {"limit": limit}, "InvalidOperationsLimit") for path in OPERATIONS_PATHS for limit in (0, 501)),
        (EVENTS_PATH, {"before_id": 0}, "InvalidOperationsCursor"),
    ],
)
def test_operations_reject_out_of_bounds_paging(client, api_key, path: str, query: dict[str, int], rc: str) -> None:
    """A page limit outside 1-500 or a ``before_id`` below 1 is rejected."""
    response = client.get(path, query_string=query, headers=_headers(api_key))
    assert response.status_code == 400
    assert response.get_json()["rc"] == rc


def test_worker_suspicion_events_survive_reset_and_deletion(client, app, api_key, make_api_user) -> None:
    """A reported suspicion stays listed by worker after the suspicion is reset and the worker deleted."""
    from horde.classes.base.worker import WorkerSuspicionEvent, WorkerTemplate
    from horde.flask import db

    owner = make_api_user(trusted=False)
    worker_id = _make_worker(app, owner.id, f"suspicion-audit-{owner.id}")
    with app.app_context():
        worker = db.session.get(WorkerTemplate, worker_id)
        worker.report_suspicion(
            reason=Suspicions.UNREASONABLY_FAST,
            formats=["999 > 30"],
        )
        worker.reset_suspicion()
        assert db.session.query(WorkerSuspicionEvent).filter_by(worker_id=worker_id).count() == 1
        db.session.expire(worker, ["suspicions"])
        db.session.delete(worker)
        db.session.commit()
        assert db.session.query(WorkerSuspicionEvent).filter_by(worker_id=worker_id).count() == 1

    response = client.get(EVENTS_PATH, query_string={"worker_id": worker_id}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    events = response.get_json()["events"]
    assert len(events) == 1
    assert set(events[0]) == {"id", "created", "worker_id", "worker_name", "user_id", "suspicion_id", "reason", "detail"}
    assert events[0]["worker_id"] == worker_id
    assert events[0]["worker_name"] == f"suspicion-audit-{owner.id}"
    assert events[0]["user_id"] == owner.id
    assert events[0]["suspicion_id"] == int(Suspicions.UNREASONABLY_FAST)
    assert events[0]["reason"] == "UNREASONABLY_FAST"
    assert events[0]["detail"] == "Generation unreasonably fast (999 > 30)"


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


def _seed_suspicion_events(app, worker_id: str, ages_in_days: list[int], *, user_id: int = 1) -> list[int]:
    """Insert one history row per age, captured that many days ago, and return their IDs in order."""
    from horde.classes.base.worker import WorkerSuspicionEvent
    from horde.flask import db

    now = datetime.utcnow()
    with app.app_context():
        events = [
            WorkerSuspicionEvent(
                created=now - timedelta(days=age),
                worker_id=worker_id,
                worker_name=f"retention-{age}",
                user_id=user_id,
                suspicion_id=int(Suspicions.UNREASONABLY_FAST),
                detail=f"{age} days old",
            )
            for age in ages_in_days
        ]
        db.session.add_all(events)
        db.session.commit()
        return [event.id for event in events]


def _surviving_suspicion_events(app, event_ids: list[int]) -> list[int]:
    from horde.classes.base.worker import WorkerSuspicionEvent
    from horde.flask import db

    with app.app_context():
        rows = db.session.query(WorkerSuspicionEvent.id).filter(WorkerSuspicionEvent.id.in_(event_ids))
        return sorted(event_id for (event_id,) in rows)


def _history_retention_policy(worker_suspicion_days: int | None) -> RetentionPolicy:
    """Return a policy that touches only evidence older than the maximum ceiling, so other tests' evidence stays."""
    from horde.database.prompt_moderation import RetentionPolicy

    return RetentionPolicy(
        text=None,
        ipaddr=None,
        ceiling=timedelta(days=365),
        worker_suspicion=timedelta(days=worker_suspicion_days) if worker_suspicion_days is not None else None,
    )


def test_worker_suspicion_history_is_kept_without_a_window(app) -> None:
    """Without ``HORDE_WORKER_SUSPICION_RETENTION_DAYS`` a retention pass deletes no history row, however old."""
    import uuid

    from horde.classes.base.worker import WorkerSuspicionEvent
    from horde.database.prompt_moderation import apply_evidence_retention
    from horde.flask import db

    event_ids = _seed_suspicion_events(app, str(uuid.uuid4()), [2000, 400, 1])
    try:
        with app.app_context():
            assert apply_evidence_retention(_history_retention_policy(None)).worker_suspicion_deleted == 0
        assert _surviving_suspicion_events(app, event_ids) == sorted(event_ids)
    finally:
        with app.app_context():
            db.session.query(WorkerSuspicionEvent).filter(WorkerSuspicionEvent.id.in_(event_ids)).delete()
            db.session.commit()


def test_worker_suspicion_window_deletes_past_window_rows_in_bounded_batches(app) -> None:
    """A set window deletes at most one batch of past-window rows per pass and keeps younger rows."""
    import uuid

    from horde.database.prompt_moderation import apply_evidence_retention

    expired_ids = _seed_suspicion_events(app, str(uuid.uuid4()), [93, 92, 91])
    retained_ids = _seed_suspicion_events(app, str(uuid.uuid4()), [89])
    policy = _history_retention_policy(90)
    with app.app_context():
        assert apply_evidence_retention(policy, batch_size=2).worker_suspicion_deleted == 2
        passes = 1
        while apply_evidence_retention(policy, batch_size=2).worker_suspicion_deleted:
            passes += 1
            assert passes < 100, "the retention pass did not drain past-window history"
    assert _surviving_suspicion_events(app, expired_ids) == []
    assert _surviving_suspicion_events(app, retained_ids) == retained_ids


def test_worker_suspicion_window_keeps_history_of_a_suspicious_owner(app, make_api_user) -> None:
    """Past-window history whose owning account is suspicious is under moderation action and is kept."""
    import uuid

    from horde.classes.base.user import User, UserSuspicions
    from horde.database.prompt_moderation import apply_evidence_retention
    from horde.flask import db

    owner = make_api_user(trusted=False)
    with app.app_context():
        db.session.add_all(
            UserSuspicions(user_id=owner.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)) for _ in range(User.SUSPICION_THRESHOLD)
        )
        db.session.commit()
    kept_ids = _seed_suspicion_events(app, str(uuid.uuid4()), [93], user_id=owner.id)
    policy = _history_retention_policy(90)
    with app.app_context():
        passes = 0
        while apply_evidence_retention(policy).worker_suspicion_deleted:
            passes += 1
            assert passes < 100, "the retention pass did not drain past-window history"
    assert _surviving_suspicion_events(app, kept_ids) == kept_ids


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

    first = client.get(EVENTS_PATH, query_string={"worker_id": worker_id, "limit": 2}, headers=_headers(api_key))
    assert first.status_code == 200, first.get_data(as_text=True)
    first_body = first.get_json()
    assert [event["id"] for event in first_body["events"]] == seeded_ids[:2]
    assert first_body["next_cursor"] == seeded_ids[1]

    second = client.get(
        EVENTS_PATH,
        query_string={"worker_id": worker_id, "limit": 2, "before_id": first_body["next_cursor"]},
        headers=_headers(api_key),
    )
    assert second.status_code == 200, second.get_data(as_text=True)
    second_body = second.get_json()
    assert [event["id"] for event in second_body["events"]] == seeded_ids[2:]
    assert second_body["next_cursor"] is None

    filtered = client.get(EVENTS_PATH, query_string={"user_id": other_owner.id}, headers=_headers(api_key))
    assert filtered.status_code == 200, filtered.get_data(as_text=True)
    assert [event["id"] for event in filtered.get_json()["events"]] == [other_id]


def _make_typed_worker(
    app,
    owner_id: int,
    worker_type: str,
    name: str,
    *,
    checked_in: datetime,
    suspicions: list[Suspicions],
    models: tuple[str, ...] = (),
) -> str:
    """Create a paused worker of one API type with the given check-in time, current suspicion reasons, and models."""
    from horde.classes.base.worker import WorkerModel, WorkerSuspicions
    from horde.classes.kobold.worker import TextWorker
    from horde.classes.stable.interrogation_worker import InterrogationWorker
    from horde.classes.stable.worker import ImageWorker
    from horde.flask import db

    classes = {"image": ImageWorker, "text": TextWorker, "interrogation": InterrogationWorker}
    with app.app_context():
        worker = classes[worker_type](user_id=owner_id, name=name, paused=True, bridge_agent=AGENT)
        db.session.add(worker)
        db.session.commit()
        worker.last_check_in = checked_in
        db.session.add_all(WorkerSuspicions(worker_id=worker.id, suspicion_id=int(reason)) for reason in suspicions)
        db.session.add_all(WorkerModel(worker_id=worker.id, model=model) for model in models)
        db.session.commit()
        return str(worker.id)


def _paused(client, api_key: str, **query: object) -> dict:
    response = client.get(OVERVIEW_PATH, query_string={"limit": 500, **query}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_paused_workers_filter_sort_and_total(client, app, api_key, make_api_user) -> None:
    from uuid import uuid4

    marker = uuid4().hex[:8]
    first_owner, second_owner = make_api_user(), make_api_user()
    now = datetime.utcnow()
    ids = {
        "text_online": _make_typed_worker(
            app,
            first_owner.id,
            "text",
            f"b-{marker}-text-online",
            checked_in=now,
            suspicions=[Suspicions.UNREASONABLY_FAST, Suspicions.UNREASONABLY_FAST],
            models=("elinas/chronos-70b-v2",),
        ),
        "text_offline": _make_typed_worker(
            app,
            second_owner.id,
            "text",
            f"a-{marker}-text-offline",
            checked_in=now - timedelta(hours=1),
            suspicions=[Suspicions.WORKER_PROFANITY],
        ),
        "image": _make_typed_worker(app, second_owner.id, "image", f"c-{marker}-image", checked_in=now, suspicions=[]),
        "interrogation": _make_typed_worker(
            app,
            first_owner.id,
            "interrogation",
            f"d-{marker}-interrogation",
            checked_in=now - timedelta(hours=2),
            suspicions=[Suspicions.UNREASONABLY_FAST],
        ),
    }

    def listed(body: dict) -> set[str]:
        return {worker["id"] for worker in body["paused_workers"]} & set(ids.values())

    everything = _paused(client, api_key)
    assert listed(everything) == set(ids.values())
    assert everything["paused_workers_total"] == len(everything["paused_workers"])
    text_online = next(worker for worker in everything["paused_workers"] if worker["id"] == ids["text_online"])
    _assert_utc_timestamp(text_online["last_check_in"])
    assert text_online["suspicious"] == 2
    assert text_online["models"] == ["elinas/chronos-70b-v2"]
    assert text_online["owner_id"] == first_owner.id

    online_text = _paused(client, api_key, worker_type="text", online="true")
    assert listed(online_text) == {ids["text_online"]}
    assert all(worker["type"] == "text" and worker["online"] for worker in online_text["paused_workers"])
    offline = _paused(client, api_key, online="false")
    assert listed(offline) == {ids["text_offline"], ids["interrogation"]}
    assert not any(worker["online"] for worker in offline["paused_workers"])
    several_types = _paused(client, api_key, worker_type=["image", "interrogation"])
    assert listed(several_types) == {ids["image"], ids["interrogation"]}
    by_reason = _paused(client, api_key, suspicion_id=[int(Suspicions.WORKER_PROFANITY), int(Suspicions.UNREASONABLY_FAST)])
    assert listed(by_reason) == {ids["text_online"], ids["text_offline"], ids["interrogation"]}
    assert listed(_paused(client, api_key, suspicion_id=int(Suspicions.WORKER_PROFANITY))) == {ids["text_offline"]}

    # The total counts every match while the list stops at the limit.
    capped = _paused(client, api_key, limit=1)
    assert len(capped["paused_workers"]) == 1
    assert capped["paused_workers_total"] == everything["paused_workers_total"] >= 4

    by_name = [worker["name"] for worker in _paused(client, api_key, sort="name")["paused_workers"]]
    assert by_name == sorted(by_name)
    by_owner = _paused(client, api_key, sort="owner")["paused_workers"]
    owners = [worker["owner"].rsplit("#", 1)[0] for worker in by_owner]
    assert owners == sorted(owners)
    by_suspicion = [worker["suspicious"] for worker in _paused(client, api_key, sort="suspicion")["paused_workers"]]
    assert by_suspicion == sorted(by_suspicion, reverse=True)
    by_check_in = [datetime.fromisoformat(worker["last_check_in"]) for worker in _paused(client, api_key)["paused_workers"]]
    assert by_check_in == sorted(by_check_in, reverse=True)

    for path, invalid, rc in (
        (OVERVIEW_PATH, {"worker_type": "unknown"}, None),
        (OVERVIEW_PATH, {"sort": "unknown"}, None),
        (OVERVIEW_PATH, {"suspicion_id": 999}, "InvalidSuspicionID"),
        (OVERVIEW_PATH, {"online": "maybe"}, None),
        (EVENTS_PATH, {"worker_id": "not-a-uuid"}, "InvalidWorkerID"),
    ):
        response = client.get(path, query_string=invalid, headers=_headers(api_key))
        assert response.status_code == 400, invalid
        if rc is not None:
            assert response.get_json()["rc"] == rc, invalid
