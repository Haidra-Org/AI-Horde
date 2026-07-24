# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Endpoint characterization for the kudos gift, award, and admin flows.

Exercises the HTTP surface that moves kudos between accounts: the user-to-user
transfer (with its audit log and shared-key crediting), the privileged award,
and the admin adjustment of balance and monthly entitlement. Mutations are
verified against committed database state via the ORM rather than echoing the
response back to itself. Also guards that the unreachable Kobold transfer
resource stays unregistered.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.fixture_types import MakeApiUser

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
ANON_API_KEY: str = "0000000000"


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Disable the rate limiter for the duration of a test."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    """Return request headers carrying the given API key and the test client agent."""
    return {"apikey": api_key, "Client-Agent": AGENT}


def _user_kudos(app: Flask, user_id: int) -> float:
    """Return the committed kudos balance for the user with the given id."""
    from horde.database import functions as database

    with app.app_context():
        return database.find_user_by_id(user_id).kudos


class TestKudosTransferAudit:
    """A user-to-user kudos transfer records its movement and rejects anonymous senders."""

    def test_successful_transfer_writes_an_audit_log_row(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A successful transfer writes an audit-log row capturing the sender, receiver, and amount."""
        from horde.classes.base.user import KudosTransferLog
        from horde.database import functions as database

        with app.app_context():
            sender_id = database.find_user_by_api_key(api_key).id
        receiver = make_api_user(kudos=100)

        # The sender's seeded balance must be folded before the transfer's
        # sufficiency check reads it.
        settle_kudos()
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": receiver.alias, "amount": 500},
            headers=_headers(api_key),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        with app.app_context():
            log = KudosTransferLog.query.filter_by(source_id=sender_id, dest_id=receiver.id).first()
            assert log is not None
            assert log.kudos == 500

    def test_transfer_from_anonymous_is_rejected(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        """The anonymous user cannot transfer kudos to another account."""
        receiver = make_api_user(kudos=100)
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": receiver.alias, "amount": 100},
            headers=_headers(ANON_API_KEY),
        )
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "KudosTransferFromAnon"


class TestKudosTransferToSharedKey:
    """A kudos transfer addressed to a shared key credits that key's budget."""

    def test_transfer_to_shared_key_credits_the_key_budget(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Transferring to a shared key id credits the key's budget by the transferred amount."""
        from horde.classes.base.user import UserSharedKey
        from horde.flask import db

        receiver = make_api_user(kudos=100)
        with app.app_context():
            # Legacy declarative models expose untyped implicit constructors.
            shared_key = UserSharedKey(
                id=uuid.uuid4(),
                user_id=receiver.id,
                kudos=5000,
                expiry=datetime.utcnow() + timedelta(days=1),
                name="test-shared-key",
            )
            db.session.add(shared_key)
            db.session.commit()
            shared_key_id = str(shared_key.id)

        # The sender's seeded balance must be folded before the transfer's
        # sufficiency check reads it.
        settle_kudos()
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": shared_key_id, "amount": 500},
            headers=_headers(api_key),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        with app.app_context():
            refreshed = db.session.get(UserSharedKey, uuid.UUID(shared_key_id))
            assert refreshed is not None
            assert refreshed.kudos == 5500


def _provision_privileged_user(app: Flask) -> str:
    """Return a plaintext API key for the sole award-privileged account (id 1)."""
    from horde.classes.base.user import User
    from horde.database import functions as database
    from horde.flask import db
    from horde.utils import generate_api_key, hash_api_key

    raw_api_key = generate_api_key()
    with app.app_context():
        privileged = database.find_user_by_id(1)
        if privileged is None:
            privileged = User(id=1, username="privileged_one", oauth_id="priv_one", api_key=hash_api_key(raw_api_key))
            privileged.create()
        else:
            privileged.api_key = hash_api_key(raw_api_key)
            db.session.commit()
    return raw_api_key


class TestKudosAward:
    """The privileged award endpoint credits a target only when called by the award-privileged account."""

    def test_award_rejects_non_privileged_caller(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        """A caller without award privilege is forbidden from awarding kudos."""
        actor = make_api_user(kudos=100)
        target = make_api_user(kudos=0)
        resp = client.post(
            "/api/v2/kudos/award",
            json={"username": target.alias, "amount": 5000},
            headers=_headers(actor.api_key),
        )
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotAllowedAwards"

    def test_privileged_caller_credits_target(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """The award-privileged caller credits the target's balance by the awarded amount."""
        privileged_key = _provision_privileged_user(app)
        target = make_api_user(kudos=0)
        target_before = _user_kudos(app, target.id)

        resp = client.post(
            "/api/v2/kudos/award",
            json={"username": target.alias, "amount": 5000},
            headers=_headers(privileged_key),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["awarded"] == 5000
        settle_kudos()
        assert _user_kudos(app, target.id) == target_before + 5000


class TestAdminKudosAdjust:
    """An admin editing a user credits balance directly and grants monthly entitlement immediately."""

    def test_admin_kudos_delta_credits_balance(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
        settle_kudos: Callable[[], int],
    ) -> None:
        """An admin's kudos delta on a user credits that user's balance by the delta."""
        from horde.database import functions as database

        with app.app_context():
            admin_alias = database.find_user_by_api_key(api_key).get_unique_alias()
        monkeypatch.setenv("ADMINS", json.dumps([admin_alias]))

        target = make_api_user(kudos=0)
        target_before = _user_kudos(app, target.id)

        resp = client.put(
            f"/api/v2/users/{target.id}",
            json={"kudos": 1000},
            headers=_headers(api_key),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        settle_kudos()
        assert _user_kudos(app, target.id) == target_before + 1000

    def test_admin_monthly_kudos_grant_credits_immediately(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
        settle_kudos: Callable[[], int],
    ) -> None:
        """An admin's monthly-kudos grant is stored as entitlement and credited to balance at once."""
        from horde.database import functions as database

        with app.app_context():
            admin_alias = database.find_user_by_api_key(api_key).get_unique_alias()
        monkeypatch.setenv("ADMINS", json.dumps([admin_alias]))

        target = make_api_user(kudos=0)
        target_before = _user_kudos(app, target.id)

        resp = client.put(
            f"/api/v2/users/{target.id}",
            json={"monthly_kudos": 500},
            headers=_headers(api_key),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)

        settle_kudos()
        with app.app_context():
            refreshed = database.find_user_by_id(target.id)
            assert refreshed.monthly_kudos == 500
            assert refreshed.kudos == target_before + 500


class TestDeadCodeGuard:
    """The Kobold kudos-transfer resource is dead code and stays unregistered on the API."""

    def test_kobold_kudos_transfer_is_not_registered(self, app: Flask) -> None:
        """The live transfer and award resources are registered while the Kobold transfer resource is not."""
        import horde.apis.v2  # noqa: F401  (ensures resources are registered)
        from horde.apis.v2 import base, kobold

        registered = {resource[0] for resource in base.api.resources}
        assert base.TransferKudos in registered
        assert base.AwardKudos in registered
        # The Kobold transfer resource remains dead code, reachable by no route.
        assert kobold.KoboldKudosTransfer not in registered
