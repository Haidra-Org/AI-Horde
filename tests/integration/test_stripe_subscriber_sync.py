# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end coverage for the Stripe supporter cache.

These exercise ``store_stripe_members`` against the installed Stripe SDK talking
to a loopback server that speaks Stripe's wire format, so the SDK's own paging
and resource deserialization are part of what is under test. Assertions are on
the horde-visible outcome: which users land in the ``stripe_cache`` redis key,
and what monthly kudos those cached records grant.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from tests.integration._stripe_api_stub import (
    STRIPE_DEFAULT_PAGE_SIZE,
    StripeAPIStub,
    customer,
    product,
    serving_stripe_api,
    subscription,
)

pytest.importorskip("stripe")

RECOGNISED_MONTHLY_KUDOS = 75_000
SUPERIOR_PERSON_MONTHLY_KUDOS = 20_000


@contextmanager
def _sync_from_stripe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscriptions: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    products: list[dict[str, Any]],
) -> Iterator[StripeAPIStub]:
    """Run one supporter-cache refresh against a stubbed Stripe API."""
    from horde.database import threads

    if threads.stripe is None:
        pytest.skip("stripe SDK is not installed")

    with serving_stripe_api(subscriptions=subscriptions, customers=customers, products=products) as stub:
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test_loopback")
        monkeypatch.setattr(threads.stripe, "api_base", stub.api_base)
        monkeypatch.setattr(threads.stripe, "max_network_retries", 0)
        threads.store_stripe_members()
        yield stub


def _cached_supporters() -> dict[str, dict[str, Any]]:
    from horde.horde_redis import horde_redis as hr

    raw = hr.horde_r_get("stripe_cache")
    assert raw is not None, "the refresh did not write a stripe_cache entry"
    return json.loads(raw)


def _make_supporter(app: Any, *, user_id: int, username: str, contact: str | None = None) -> None:
    from horde.classes.base.user import User
    from horde.flask import db
    from horde.utils import hash_api_key

    with app.app_context():
        user = User(
            id=user_id,
            username=username,
            oauth_id=f"stripe_sync_{user_id}",
            api_key=hash_api_key(f"stripe-sync-key-{user_id}"),
            contact=contact,
        )
        db.session.add(user)
        db.session.commit()


def test_every_active_subscriber_is_cached_regardless_of_api_page_count(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supporters beyond Stripe's first response page still receive their kudos.

    Stripe returns list endpoints one page at a time. A refresh that reads only
    the page it was handed drops every supporter after the first page, so this
    uses a subscriber count spanning several pages.
    """
    subscriber_count = (STRIPE_DEFAULT_PAGE_SIZE * 2) + 5
    subscriptions = [
        subscription(
            f"sub_{index}",
            customer_id=f"cus_{index}",
            product_id="prod_recognised",
            metadata={"horde_id": f"Supporter{index}#{1000 + index}"},
        )
        for index in range(subscriber_count)
    ]
    customers = [
        customer(f"cus_{index}", email=f"supporter{index}@example.com", name=f"Supporter {index}") for index in range(subscriber_count)
    ]

    with _sync_from_stripe(
        monkeypatch,
        subscriptions=subscriptions,
        customers=customers,
        products=[product("prod_recognised", name="Recognised")],
    ):
        cached = _cached_supporters()

    expected_ids = {str(1000 + index) for index in range(subscriber_count)}
    assert set(cached) == expected_ids


def test_supporter_record_is_built_from_stripe_resource_objects(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supporter's cached record carries the fields the horde reads later.

    The SDK hands back resource objects rather than dictionaries, and the
    product name and contact details live on separate resources that must be
    fetched and merged into one record.
    """
    with _sync_from_stripe(
        monkeypatch,
        subscriptions=[
            subscription(
                "sub_solo",
                customer_id="cus_solo",
                product_id="prod_recognised",
                metadata={
                    "horde_id": "Supporter#42",
                    "alias": "The Supporter",
                    "sponsor_link": "https://example.com/supporter",
                },
            ),
        ],
        customers=[customer("cus_solo", email="supporter@example.com", name="Supporter Name")],
        products=[product("prod_recognised", name="Recognised")],
    ):
        cached = _cached_supporters()

    assert cached == {
        "42": {
            "product_name": "Recognised",
            "email": "supporter@example.com",
            "name": "Supporter Name",
            "horde_id": "Supporter#42",
            "alias": "The Supporter",
            "sponsor_link": "https://example.com/supporter",
            "status": "active",
        },
    }


def test_subscriber_is_matched_to_a_horde_account_by_contact_email(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription with no horde metadata is matched on the customer's email."""
    _make_supporter(app, user_id=4242, username="EmailMatched", contact="matched@example.com")

    with _sync_from_stripe(
        monkeypatch,
        subscriptions=[
            subscription("sub_by_email", customer_id="cus_by_email", product_id="prod_superior", metadata={}),
        ],
        customers=[customer("cus_by_email", email="matched@example.com", name="Email Matched")],
        products=[product("prod_superior", name="Superior Person")],
    ):
        cached = _cached_supporters()

    assert set(cached) == {"4242"}
    assert cached["4242"]["horde_id"] == "EmailMatched#4242"


def test_legacy_horde_metadata_key_still_identifies_the_supporter(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscriptions tagged with the older ``horde`` key resolve the same way."""
    with _sync_from_stripe(
        monkeypatch,
        subscriptions=[
            subscription(
                "sub_legacy",
                customer_id="cus_legacy",
                product_id="prod_recognised",
                metadata={"horde": "Legacy#77"},
            ),
        ],
        customers=[customer("cus_legacy", email="legacy@example.com", name="Legacy")],
        products=[product("prod_recognised", name="Recognised")],
    ):
        cached = _cached_supporters()

    assert set(cached) == {"77"}


def test_unidentifiable_and_lapsed_subscriptions_are_left_out_of_the_cache(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only subscriptions that are active and resolve to an account are cached.

    A lapsed subscription must stop granting kudos, and a subscription whose
    customer matches no horde account cannot be attributed to anyone. Neither
    may abort the refresh for the supporters that do resolve.
    """
    with _sync_from_stripe(
        monkeypatch,
        subscriptions=[
            subscription(
                "sub_cancelled",
                customer_id="cus_cancelled",
                product_id="prod_recognised",
                status="canceled",
                metadata={"horde_id": "Lapsed#11"},
            ),
            subscription("sub_stranger", customer_id="cus_stranger", product_id="prod_recognised", metadata={}),
            subscription("sub_anonymous", customer_id="cus_anonymous", product_id="prod_recognised", metadata={}),
            subscription(
                "sub_good",
                customer_id="cus_good",
                product_id="prod_recognised",
                metadata={"horde_id": "Good#12"},
            ),
        ],
        customers=[
            customer("cus_cancelled", email="lapsed@example.com", name="Lapsed"),
            customer("cus_stranger", email="nobody@example.com", name="Stranger"),
            customer("cus_anonymous", email=None, name=None),
            customer("cus_good", email="good@example.com", name="Good"),
        ],
        products=[product("prod_recognised", name="Recognised")],
    ):
        cached = _cached_supporters()

    assert set(cached) == {"12"}


def test_cached_supporters_grant_their_tier_monthly_kudos(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refreshed cache is what the monthly kudos award reads from.

    This is the behaviour the whole sync exists to serve: a supporter's product
    tier decides the kudos they are granted each month.
    """
    from horde.stripe_subs import stripe_subs

    with _sync_from_stripe(
        monkeypatch,
        subscriptions=[
            subscription(
                "sub_recognised",
                customer_id="cus_recognised",
                product_id="prod_recognised",
                metadata={"horde_id": "Recognised#21"},
            ),
            subscription(
                "sub_superior",
                customer_id="cus_superior",
                product_id="prod_superior",
                metadata={"horde_id": "Superior#22"},
            ),
        ],
        customers=[
            customer("cus_recognised", email="recognised@example.com", name="Recognised Person"),
            customer("cus_superior", email="superior@example.com", name="Superior Person"),
        ],
        products=[product("prod_recognised", name="Recognised"), product("prod_superior", name="Superior Person")],
    ):
        stripe_subs.patrons = {}
        stripe_subs.call_function()

    assert stripe_subs.get_monthly_kudos(21) == RECOGNISED_MONTHLY_KUDOS
    assert stripe_subs.get_monthly_kudos(22) == SUPERIOR_PERSON_MONTHLY_KUDOS
    assert stripe_subs.get_monthly_kudos(999) == 0
