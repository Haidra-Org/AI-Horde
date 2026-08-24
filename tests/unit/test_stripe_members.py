# SPDX-FileCopyrightText: 2026 Abhinav Gorrepati
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression coverage for Stripe subscription cache refreshes."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

from horde.database import threads


class StripeV15Resource:
    """Minimal Stripe v15 resource: bracket access and ``to_dict()``, but no ``get()``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def to_dict(self) -> dict[str, Any]:
        return self._data.copy()


class StripeV15ListResource:
    """A list response: iterating yields one page, ``auto_paging_iter()`` yields all.

    Mirroring that split matters because the two are what separate a truncated
    refresh from a complete one; a fake that returns a plain list cannot tell
    them apart. Broader coverage of the paging itself lives in
    ``tests/integration/test_stripe_subscriber_sync.py``.
    """

    def __init__(self, data: list[Any]) -> None:
        self._data = data

    def __iter__(self) -> Any:
        return iter(self._data)

    def auto_paging_iter(self) -> Any:
        return iter(self._data)


def test_store_stripe_members_accepts_v15_resources(monkeypatch) -> None:
    subscription = StripeV15Resource(
        {
            "items": {"data": [{"price": {"product": "prod_supporter"}}]},
            "customer": "cus_supporter",
            "metadata": {"horde_id": "Supporter#42", "alias": "Supporter", "sponsor_link": "https://example.com"},
            "status": "active",
        },
    )
    product = StripeV15Resource({"name": "Monthly Supporter"})
    customer = StripeV15Resource({"email": "supporter@example.com", "name": "Supporter Name"})
    fake_stripe = SimpleNamespace(
        api_key=None,
        Subscription=SimpleNamespace(list=lambda **params: StripeV15ListResource([subscription])),
        Product=SimpleNamespace(retrieve=lambda product_id: product),
        Customer=SimpleNamespace(retrieve=lambda customer_id: customer),
    )
    cache: dict[str, str] = {}

    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_123")
    monkeypatch.setattr(threads, "stripe", fake_stripe)
    monkeypatch.setattr(threads, "get_app", lambda: SimpleNamespace(app_context=nullcontext))
    monkeypatch.setattr(threads.hr, "horde_r_set", lambda key, value: cache.__setitem__(key, value))

    threads.store_stripe_members()

    assert fake_stripe.api_key == "sk_test_123"
    assert json.loads(cache["stripe_cache"]) == {
        "42": {
            "product_name": "Monthly Supporter",
            "email": "supporter@example.com",
            "name": "Supporter Name",
            "horde_id": "Supporter#42",
            "alias": "Supporter",
            "sponsor_link": "https://example.com",
            "status": "active",
        },
    }
