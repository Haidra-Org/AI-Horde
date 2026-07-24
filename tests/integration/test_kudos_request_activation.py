# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Endpoint characterization for request-activation kudos effects.

Submitting an image request charges the requester an up-front horde tax at
activation and seeds the request's queue priority from the requester's current
balance. The anonymous user is charged the same tax as a registered user. When
a request needs kudos up front and the requester cannot cover the estimated
cost, activation is refused and nothing is charged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.fixture_types import MakeApiUser

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
ANON_API_KEY: str = "0000000000"

SMALL_REQUEST: dict[str, object] = {
    "prompt": "a horde of small test robots",
    "nsfw": False,
    "params": {"width": 512, "height": 512, "steps": 8, "cfg_scale": 1.5, "sampler_name": "k_euler_a"},
    "models": ["stable_diffusion"],
    "allow_downgrade": False,
}

# A request whose step count exceeds the free-tier ceiling always needs kudos
# up front, so the upfront gate turns purely on whether the requester can cover it.
GATED_REQUEST: dict[str, object] = {
    "prompt": "a horde of demanding test robots",
    "nsfw": False,
    "params": {"width": 512, "height": 512, "steps": 50, "cfg_scale": 1.5, "sampler_name": "k_euler_a"},
    "models": ["stable_diffusion"],
    "allow_downgrade": False,
}

# The up-front horde tax charged at activation for a minimal single-image request.
ACTIVATION_TAX: int = 3


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


class TestActivationDebit:
    """Submitting a request charges an up-front horde tax against the requester's balance."""

    def test_registered_request_debits_the_requester(
        self, client: FlaskClient, app: Flask, api_key: str, settle_kudos: Callable[[], int]
    ) -> None:
        """A registered requester's balance drops by the activation tax when a request is submitted."""
        from horde.database import functions as database

        # The bootstrap balance is seeded through the ledger; fold it so the
        # observed starting balance is the materialized value.
        settle_kudos()
        with app.app_context():
            uid = database.find_user_by_api_key(api_key).id
            before = database.find_user_by_id(uid).kudos

        resp = client.post("/api/v2/generate/async", json=SMALL_REQUEST, headers=_headers(api_key))
        assert resp.status_code == 202, resp.get_data(as_text=True)

        settle_kudos()
        with app.app_context():
            after = database.find_user_by_id(uid).kudos
        assert after == before - ACTIVATION_TAX

    def test_anonymous_request_debits_the_anonymous_user(self, client: FlaskClient, app: Flask, settle_kudos: Callable[[], int]) -> None:
        """The anonymous user is charged the same activation tax as a registered requester."""
        from horde.database import functions as database
        from horde.flask import db

        with app.app_context():
            anon = database.find_user_by_id(0)
            # Pin the anonymous balance clear of its floor so the debit is observable.
            anon.kudos = 100
            db.session.commit()

        resp = client.post("/api/v2/generate/async", json=SMALL_REQUEST, headers=_headers(ANON_API_KEY))
        assert resp.status_code == 202, resp.get_data(as_text=True)

        settle_kudos()
        with app.app_context():
            after = database.find_user_by_id(0).kudos
        assert after == 100 - ACTIVATION_TAX


class TestPrioritySeeding:
    """A request's queue priority is derived from the requester's balance at activation."""

    def test_priority_is_seeded_from_the_requester_balance(
        self,
        client: FlaskClient,
        app: Flask,
        api_key: str,
    ) -> None:
        """A request's queue priority is seeded from the requester's balance before the tax is deducted."""
        from horde.database import functions as database

        with app.app_context():
            balance = database.find_user_by_api_key(api_key).kudos

        resp = client.post("/api/v2/generate/async", json=SMALL_REQUEST, headers=_headers(api_key))
        assert resp.status_code == 202, resp.get_data(as_text=True)
        wp_id = resp.get_json()["id"]

        with app.app_context():
            wp = database.get_wp_by_id(wp_id)
            # The queue priority is seeded from the balance held at activation time
            # (before the horde tax is deducted).
            assert wp.extra_priority == balance


class TestUpfrontGate:
    """A request needing up-front kudos is admitted only when the requester can cover the estimated cost."""

    def test_insufficient_balance_is_refused_and_charges_nothing(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        """A request needing up-front kudos is refused and charges nothing when the requester cannot cover it."""
        from horde.database import functions as database

        requester = make_api_user(kudos=0)

        resp = client.post("/api/v2/generate/async", json=GATED_REQUEST, headers=_headers(requester.api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "KudosUpfront"

        with app.app_context():
            # The refusal charges nothing.
            assert database.find_user_by_id(requester.id).kudos == 0

    def test_sufficient_balance_is_accepted(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A request needing up-front kudos is accepted when the requester can cover the estimated cost."""
        requester = make_api_user(kudos=100000)
        # The seeded balance reaches the upfront gate only once folded.
        settle_kudos()
        resp = client.post("/api/v2/generate/async", json=GATED_REQUEST, headers=_headers(requester.api_key))
        assert resp.status_code == 202, resp.get_data(as_text=True)
