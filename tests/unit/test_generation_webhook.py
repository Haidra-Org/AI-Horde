# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behaviour of generation and interrogation webhook delivery.

A waiting prompt or an interrogation may carry a subscriber webhook URL. Each
completed generation or interrogation form is then POSTed to it. Delivery is
decoupled from the submit request: the request thread materializes the payload
and enqueues it on a bounded queue, and a background sender performs the HTTP
delivery with bounded retries.

The contracts exercised here are:

- A generation whose prompt has no webhook enqueues nothing.
- A generation whose prompt has a webhook enqueues one delivery carrying the
  subscriber URL and a payload identifying the request, generation and reward.
- An interrogation form enqueues on the same queue instead of posting inline.
- The sender POSTs the payload to the subscriber URL once.
- A subscriber that keeps failing is retried a bounded number of times and
  then abandoned without raising.
- A URL resolving to a private address, a plain http URL and a redirect are
  refused once, with no connection to the refused target and no retry.
- A full queue drops the delivery without raising on the request path.
"""

from __future__ import annotations

import functools
import json
import queue
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

import horde.classes.base.processing_generation as procgen_module
from horde import outbound
from horde.classes.base.user import User
from horde.classes.stable.interrogation import InterrogationForms
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.outbound import HTTP_SCHEMES
from tests.fixture_types import MakeUser
from tests.local_http_server import (
    TEST_SERVER_ADDRESS,
    RecordedServer,
    allow_test_server_or_public,
    redirect_to,
    resolver,
    send_body,
    serve_recorded,
)

pytestmark = pytest.mark.unit

WEBHOOK_URL = "http://subscriber.example/hook"

WebhookDelivery = tuple[str, dict[str, Any], str, str]


@pytest.fixture
def isolated_queue(monkeypatch: pytest.MonkeyPatch) -> queue.Queue[WebhookDelivery]:
    """Swap in a fresh delivery queue and keep the background sender off it.

    The tests assert on queue contents, so the module's shared queue (which a
    previously started sender thread may be draining) is replaced per test.
    """
    fresh: queue.Queue[WebhookDelivery] = queue.Queue(maxsize=4)
    monkeypatch.setattr(procgen_module, "_webhook_queue", fresh)
    monkeypatch.setattr(procgen_module, "_ensure_webhook_sender", lambda: None)
    return fresh


def _build_completed_generation(db_session: Session, requester: User, *, webhook: str | None) -> ImageProcessingGeneration:
    worker = ImageWorker(name=f"webhook_worker_{uuid.uuid4().hex[:8]}", user_id=requester.id)
    db_session.add(worker)
    db_session.flush()
    wp = ImageWaitingPrompt(
        worker_ids=[],
        models=["stable_diffusion"],
        prompt="a test robot",
        user_id=requester.id,
        params={"width": 512, "height": 512, "steps": 8, "sampler_name": "k_euler_a"},
        webhook=webhook,
    )
    db_session.flush()
    procgen = ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="stable_diffusion")
    # A directly-stored (non-R2) result keeps payload assembly free of object
    # storage access.
    procgen.generation = "base64imagedata"
    db_session.flush()
    return procgen


class TestEnqueue:
    """The request path only enqueues; queue contents describe the delivery."""

    def test_prompt_without_webhook_enqueues_nothing(
        self, db_session: Session, make_user: MakeUser, isolated_queue: queue.Queue[WebhookDelivery]
    ) -> None:
        procgen = _build_completed_generation(db_session, make_user(), webhook=None)

        procgen.send_webhook(kudos=10)

        assert isolated_queue.empty()

    def test_prompt_with_webhook_enqueues_payload(
        self, db_session: Session, make_user: MakeUser, isolated_queue: queue.Queue[WebhookDelivery]
    ) -> None:
        procgen = _build_completed_generation(db_session, make_user(), webhook=WEBHOOK_URL)

        procgen.send_webhook(kudos=10)

        url, data, wp_id, procgen_id = isolated_queue.get_nowait()
        assert url == WEBHOOK_URL
        assert data["request"] == str(procgen.wp.id)
        assert data["id"] == str(procgen.id)
        assert data["kudos"] == 10
        assert data["worker_id"] == str(procgen.worker.id)
        assert wp_id == str(procgen.wp.id)
        assert procgen_id == str(procgen.id)

    def test_full_queue_drops_without_raising(
        self, db_session: Session, make_user: MakeUser, isolated_queue: queue.Queue[WebhookDelivery]
    ) -> None:
        procgen = _build_completed_generation(db_session, make_user(), webhook=WEBHOOK_URL)
        while not isolated_queue.full():
            isolated_queue.put_nowait(("occupied", {}, "", ""))

        procgen.send_webhook(kudos=10)

        assert isolated_queue.qsize() == isolated_queue.maxsize


class TestInterrogationEnqueue:
    """An interrogation form's webhook goes on the generation webhook queue, off the submit request."""

    def test_form_with_webhook_enqueues_payload(self, isolated_queue: queue.Queue[WebhookDelivery]) -> None:
        interrogation = SimpleNamespace(id="interrogation-id", webhook=WEBHOOK_URL)
        form = SimpleNamespace(
            id="form-id",
            interrogation=interrogation,
            get_details=lambda: {"form": "caption", "state": "done", "result": {"caption": "a"}, "worker_id": "w"},
        )

        InterrogationForms.send_webhook(form, 3)  # type: ignore[arg-type]  # the method reads only these attributes

        url, data, request_id, form_id = isolated_queue.get_nowait()
        assert url == WEBHOOK_URL
        assert data == {
            "form": "caption",
            "state": "done",
            "result": {"caption": "a"},
            "worker_id": "w",
            "request": "interrogation-id",
            "id": "form-id",
            "kudos": 3,
        }
        assert (request_id, form_id) == ("interrogation-id", "form-id")


SUBSCRIBER_HOST = "subscriber.example"
"""The made-up subscriber host the delivery tests resolve to the local server."""


@pytest.fixture
def subscriber(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordedServer]:
    """Run a local subscriber and let the sender reach it over plain http by name.

    The sender accepts only https URLs resolving to public addresses. The local server is plain http on loopback,
    so the fixture widens the scheme set and resolves the subscriber's name to the server.
    """
    with serve_recorded() as recorded_server:
        monkeypatch.setattr(procgen_module, "WEBHOOK_SCHEMES", HTTP_SCHEMES)
        monkeypatch.setattr(
            procgen_module,
            "send_public_request",
            functools.partial(
                outbound.send_public_request,
                resolve=resolver({SUBSCRIBER_HOST: [TEST_SERVER_ADDRESS], "internal.example": ["10.0.0.5"]}),
                address_allowed=allow_test_server_or_public,
            ),
        )
        yield recorded_server


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the address of every connection the sender opens, while still opening it."""
    opened: list[str] = []
    open_bound_response = outbound._open_bound_response

    def recording_open(scheme: str, host: str, port: int, request_target: str, address: str, **kwargs: Any) -> Any:
        opened.append(address)
        return open_bound_response(scheme, host, port, request_target, address, **kwargs)

    monkeypatch.setattr(outbound, "_open_bound_response", recording_open)
    return opened


class TestDelivery:
    """The sender POSTs the payload through the address guard and bounds its retries."""

    def test_payload_is_posted_to_subscriber_once(self, subscriber: RecordedServer) -> None:
        subscriber.respond = send_body(b"")

        procgen_module._deliver_webhook(f"http://{SUBSCRIBER_HOST}:{subscriber.port}/hook", {"id": "gen"}, "wp", "gen")

        assert [recorded.method for recorded in subscriber.requests] == ["POST"]
        assert json.loads(subscriber.requests[0].body) == {"id": "gen"}

    def test_persistent_failure_is_bounded_and_does_not_raise(self, subscriber: RecordedServer) -> None:
        subscriber.respond = send_body(b"", status=500)

        procgen_module._deliver_webhook(f"http://{SUBSCRIBER_HOST}:{subscriber.port}/hook", {"id": "gen"}, "wp", "gen")

        assert len(subscriber.requests) == procgen_module.WEBHOOK_ATTEMPTS

    def test_a_name_resolving_to_a_private_address_is_refused_without_an_attempt(
        self,
        subscriber: RecordedServer,
        connections: list[str],
    ) -> None:
        procgen_module._deliver_webhook(f"http://internal.example:{subscriber.port}/hook", {"id": "gen"}, "wp", "gen")

        assert connections == []
        assert subscriber.requests == []

    def test_a_private_address_literal_is_refused_once_without_an_attempt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        connections: list[str],
    ) -> None:
        sends: list[str] = []
        guarded_send = outbound.send_public_request

        def counting_send(url: str, **kwargs: Any) -> Any:
            sends.append(url)
            return guarded_send(url, **kwargs)

        monkeypatch.setattr(procgen_module, "send_public_request", counting_send)

        procgen_module._deliver_webhook("https://10.0.0.5/hook", {"id": "gen"}, "wp", "gen")

        assert sends == ["https://10.0.0.5/hook"]
        assert connections == []

    def test_a_plain_http_url_is_refused_without_an_attempt(self, connections: list[str]) -> None:
        procgen_module._deliver_webhook("http://93.184.216.34/hook", {"id": "gen"}, "wp", "gen")

        assert connections == []

    def test_a_redirect_is_not_followed_or_retried(self, subscriber: RecordedServer) -> None:
        subscriber.respond = redirect_to(f"http://{SUBSCRIBER_HOST}:{subscriber.port}/elsewhere", status=307)

        procgen_module._deliver_webhook(f"http://{SUBSCRIBER_HOST}:{subscriber.port}/hook", {"id": "gen"}, "wp", "gen")

        assert [recorded.method for recorded in subscriber.requests] == ["POST"]
