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
OVERVIEW_PATH = "/api/v2/operations/moderation"
OPERATIONS_PATHS = [OVERVIEW_PATH]


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _assert_utc_timestamp(value: str) -> None:
    assert value.endswith(("+00:00", "Z")), value
    assert datetime.fromisoformat(value).utcoffset() == timedelta(0), value


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
    """Without a configured kudos trust threshold, promotion is disabled and both promotion queues are empty."""
    from horde.classes.base.user import User
    from horde.flask import db

    monkeypatch.delenv("KUDOS_TRUST_THRESHOLD", raising=False)
    candidate = make_api_user(trusted=False)
    with app.app_context():
        user = db.session.get(User, candidate.id)
        user.created = datetime.utcnow() - timedelta(days=8)
        user.evaluating_kudos = 250
        db.session.commit()

    response = client.get(OVERVIEW_PATH, query_string={"limit": 500}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()

    assert body["promotion_threshold"] is None
    assert body["promotion_blocked_users"] == []
    assert body["promotion_eligible_users"] == []


@pytest.mark.parametrize("path", OPERATIONS_PATHS)
def test_operations_accept_maximum_limit_privately(client, api_key, path: str) -> None:
    """A page limit of 500 is accepted, and the response carries ``Cache-Control: private, no-store``."""
    response = client.get(path, query_string={"limit": 500}, headers=_headers(api_key))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    ("path", "query", "rc"),
    [(path, {"limit": limit}, "InvalidOperationsLimit") for path in OPERATIONS_PATHS for limit in (0, 501)],
)
def test_operations_reject_out_of_bounds_paging(client, api_key, path: str, query: dict[str, int], rc: str) -> None:
    """A page limit outside 1-500 is rejected."""
    response = client.get(path, query_string=query, headers=_headers(api_key))
    assert response.status_code == 400
    assert response.get_json()["rc"] == rc
