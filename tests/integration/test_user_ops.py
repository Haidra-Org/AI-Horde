# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Semantic coverage for the user-operations endpoints.

Exercises user lookup (GET /users/<id>, /find_user), the tiered modify
permission model (admin vs moderator vs owner) on PUT /users/<id>, and kudos
transfer accounting. Mutations are verified against committed DB state via the
ORM (cache-free) rather than echoing the response back to itself.
"""

from __future__ import annotations

import pytest

AGENT = "aihorde_ci_client:1.0:(test)ci"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _user_kudos(app, user_id: int) -> float:
    from horde.database import functions as database

    with app.app_context():
        return database.find_user_by_id(user_id).kudos


# --------------------------------------------------------------------------- #
# GET /users/<id> and /find_user                                              #
# --------------------------------------------------------------------------- #


class TestUserDetails:
    def test_non_numeric_id_rejected(self, client):
        resp = client.get("/api/v2/users/db0")
        assert resp.status_code == 404
        assert resp.get_json()["rc"] == "UserNotFound"

    def test_unknown_user_id_404(self, client):
        resp = client.get("/api/v2/users/999999")
        assert resp.status_code == 404
        assert resp.get_json()["rc"] == "UserNotFound"

    def test_moderator_view_exposes_privileged_field_anon_view_does_not(self, client, api_key, make_api_user):
        target = make_api_user(kudos=50)

        # Moderator (api_key fixture) sees the mod-only evaluating_kudos field...
        mod_view = client.get(f"/api/v2/users/{target.id}", headers=_headers(api_key))
        assert mod_view.status_code == 200
        assert "evaluating_kudos" in mod_view.get_json()

        # ...an unauthenticated reader of the same user does not.
        anon_view = client.get(f"/api/v2/users/{target.id}")
        assert anon_view.status_code == 200
        assert "evaluating_kudos" not in anon_view.get_json()


class TestFindUser:
    def test_self_lookup_returns_own_identity(self, client, api_key):
        resp = client.get("/api/v2/find_user", headers=_headers(api_key))
        assert resp.status_code == 200
        body = resp.get_json()
        assert "test_user" in body["username"]
        assert "kudos" in body

    def test_missing_api_key_unauthorized(self, client):
        resp = client.get("/api/v2/find_user")
        assert resp.status_code == 401

    def test_unknown_api_key_not_found(self, client):
        resp = client.get("/api/v2/find_user", headers=_headers("no-such-key"))
        assert resp.status_code == 404
        assert resp.get_json()["rc"] == "UserNotFound"


# --------------------------------------------------------------------------- #
# PUT /users/<id> permission tiers                                            #
# --------------------------------------------------------------------------- #


class TestUserModify:
    def test_moderator_can_grant_trusted_and_it_persists(self, client, app, api_key, make_api_user):
        target = make_api_user(trusted=False)
        resp = client.put(f"/api/v2/users/{target.id}", json={"trusted": True}, headers=_headers(api_key))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["trusted"] is True

        from horde.database import functions as database

        with app.app_context():
            assert database.find_user_by_id(target.id).trusted is True

    def test_non_moderator_cannot_grant_trusted(self, client, make_api_user):
        actor = make_api_user(moderator=False, kudos=100)
        target = make_api_user(trusted=False)
        resp = client.put(f"/api/v2/users/{target.id}", json={"trusted": True}, headers=_headers(actor.api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotModerator"

    def test_kudos_modify_requires_admin_not_just_moderator(self, client, api_key, make_api_user, monkeypatch):
        # Ensure the moderator fixture user is not coincidentally listed as an admin.
        monkeypatch.setenv("ADMINS", "[]")
        target = make_api_user(kudos=0)
        resp = client.put(f"/api/v2/users/{target.id}", json={"kudos": 1000}, headers=_headers(api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotAdmin"

    def test_no_fields_is_rejected(self, client, api_key, make_api_user):
        target = make_api_user()
        resp = client.put(f"/api/v2/users/{target.id}", json={}, headers=_headers(api_key))
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "NoUserModSelected"

    def test_owner_can_set_own_public_workers(self, client, make_api_user):
        owner = make_api_user(kudos=100)
        resp = client.put(f"/api/v2/users/{owner.id}", json={"public_workers": True}, headers=_headers(owner.api_key))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["public_workers"] is True

    def test_user_cannot_modify_another_users_owner_tier_field(self, client, make_api_user):
        actor = make_api_user(moderator=False, kudos=100)
        target = make_api_user(kudos=100)
        resp = client.put(f"/api/v2/users/{target.id}", json={"public_workers": True}, headers=_headers(actor.api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotModerator"


# --------------------------------------------------------------------------- #
# POST /kudos/transfer accounting                                             #
# --------------------------------------------------------------------------- #


class TestKudosTransfer:
    def test_transfer_moves_kudos_between_accounts(self, client, app, api_key, make_api_user, settle_kudos):
        from horde.database import functions as database

        with app.app_context():
            sender = database.find_user_by_api_key(api_key)
            sender_id = sender.id
            sender_alias = sender.get_unique_alias()
        receiver = make_api_user(kudos=100)

        # Fold the seeded balances so the sufficiency check and the observed
        # starting balances see materialized values.
        settle_kudos()
        sender_before = _user_kudos(app, sender_id)
        receiver_before = _user_kudos(app, receiver.id)

        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": receiver.alias, "amount": 500},
            headers=_headers(api_key),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["transferred"] == 500

        # Semantic: conservation of kudos across the two accounts.
        settle_kudos()
        assert _user_kudos(app, sender_id) == sender_before - 500
        assert _user_kudos(app, receiver.id) == receiver_before + 500
        assert sender_alias  # sanity: alias resolved

    def test_cannot_transfer_to_self(self, client, app, api_key):
        from horde.database import functions as database

        with app.app_context():
            sender_alias = database.find_user_by_api_key(api_key).get_unique_alias()
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": sender_alias, "amount": 100},
            headers=_headers(api_key),
        )
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "KudosTransferToSelf"

    def test_negative_amount_rejected(self, client, api_key, make_api_user):
        receiver = make_api_user(kudos=100)
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": receiver.alias, "amount": -100},
            headers=_headers(api_key),
        )
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "NegativeKudosTransfer"

    def test_insufficient_kudos_rejected(self, client, api_key, make_api_user):
        receiver = make_api_user(kudos=100)
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": receiver.alias, "amount": 99_999_999},
            headers=_headers(api_key),
        )
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "KudosTransferNotEnough"
