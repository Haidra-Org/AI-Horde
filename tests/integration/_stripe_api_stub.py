# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A local stand-in for the Stripe REST API, driven by the real Stripe SDK.

The subscriber cache is only as correct as the SDK's own deserialization: the
resource objects it hands back are not dictionaries, list endpoints are paged,
and nested resources arrive as further resource objects. Hand-written mock
objects encode whatever shape the test author assumed and therefore cannot
falsify assumptions about any of that.

This serves genuine Stripe wire-format JSON over loopback HTTP so the installed
SDK performs its real deserialization, paging, and resource construction. Tests
control the dataset and assert on what the horde derives from it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from horde.database.threads import STRIPE_MAX_PAGE_SIZE

# What Stripe serves when the caller does not ask for a page size, which is what
# makes an unpaged read truncate silently rather than obviously.
STRIPE_DEFAULT_PAGE_SIZE = 10


def subscription(
    subscription_id: str,
    *,
    customer_id: str,
    product_id: str,
    status: str = "active",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a subscription in Stripe's wire format."""
    return {
        "id": subscription_id,
        "object": "subscription",
        "status": status,
        "customer": customer_id,
        "metadata": metadata if metadata is not None else {},
        "items": {
            "object": "list",
            "url": f"/v1/subscription_items?subscription={subscription_id}",
            "has_more": False,
            "data": [
                {
                    "id": f"si_{subscription_id}",
                    "object": "subscription_item",
                    "price": {
                        "id": f"price_{product_id}",
                        "object": "price",
                        "product": product_id,
                    },
                },
            ],
        },
    }


def customer(customer_id: str, *, email: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Build a customer in Stripe's wire format.

    ``email`` and ``name`` are nullable on real customers, so ``None`` is sent
    as an explicit JSON null rather than an omitted key.
    """
    return {"id": customer_id, "object": "customer", "email": email, "name": name}


def product(product_id: str, *, name: str) -> dict[str, Any]:
    """Build a product in Stripe's wire format."""
    return {"id": product_id, "object": "product", "name": name}


class StripeAPIStub:
    """Serves a fixed dataset over the subset of endpoints the horde calls."""

    def __init__(
        self,
        subscriptions: list[dict[str, Any]],
        customers: list[dict[str, Any]],
        products: list[dict[str, Any]],
    ) -> None:
        self.subscriptions = subscriptions
        self.customers = {c["id"]: c for c in customers}
        self.products = {p["id"]: p for p in products}
        self.request_paths: list[str] = []
        self._server: ThreadingHTTPServer | None = None

    @property
    def api_base(self) -> str:
        if self._server is None:
            raise RuntimeError("Stripe API stub is not serving")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def list_subscriptions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = min(int(query.get("limit", [str(STRIPE_DEFAULT_PAGE_SIZE)])[0]), STRIPE_MAX_PAGE_SIZE)
        start = 0
        if "starting_after" in query:
            cursor = query["starting_after"][0]
            start = next(i for i, s in enumerate(self.subscriptions) if s["id"] == cursor) + 1
        page = self.subscriptions[start : start + limit]
        return {
            "object": "list",
            "url": "/v1/subscriptions",
            "has_more": start + limit < len(self.subscriptions),
            "data": page,
        }

    def resolve(self, path: str, query: dict[str, list[str]]) -> dict[str, Any] | None:
        if path == "/v1/subscriptions":
            return self.list_subscriptions(query)
        if path.startswith("/v1/products/"):
            return self.products.get(path.removeprefix("/v1/products/"))
        if path.startswith("/v1/customers/"):
            return self.customers.get(path.removeprefix("/v1/customers/"))
        return None


def _build_handler(stub: StripeAPIStub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Keep-alive matters here: the SDK reuses one connection across the many
        # requests a refresh makes, and a threading server keeps a persisted
        # connection from starving any other.
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            """Silence per-request logging to stderr."""

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            stub.request_paths.append(self.path)
            body = stub.resolve(parsed.path, parse_qs(parsed.query))
            if body is None:
                payload = {"error": {"type": "invalid_request_error", "message": f"No such resource: {parsed.path}"}}
                self._respond(404, payload)
                return
            self._respond(200, body)

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


@contextmanager
def serving_stripe_api(
    *,
    subscriptions: list[dict[str, Any]],
    customers: list[dict[str, Any]],
    products: list[dict[str, Any]],
) -> Iterator[StripeAPIStub]:
    """Serve the given dataset on loopback for the duration of the context."""
    stub = StripeAPIStub(subscriptions, customers, products)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _build_handler(stub))
    server.daemon_threads = True
    stub._server = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        stub._server = None
