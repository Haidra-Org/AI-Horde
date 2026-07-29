# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behaviour of generation webhook delivery.

A waiting prompt may carry a subscriber webhook URL; each completed generation
is then POSTed to it. Delivery is decoupled from the submit request: the
request thread materializes the payload and enqueues it on a bounded queue,
and a background sender performs the HTTP delivery with bounded retries.

The contracts exercised here are:

- A generation whose prompt has no webhook enqueues nothing.
- A generation whose prompt has a webhook enqueues one delivery carrying the
  subscriber URL and a payload identifying the request, generation and reward.
- The sender POSTs the payload to the subscriber URL.
- A subscriber that keeps failing is retried a bounded number of times and
  then abandoned without raising.
- A full queue drops the delivery without raising on the request path.
"""

from __future__ import annotations

import queue
import uuid
from typing import Any

import pytest
import requests
from sqlalchemy.orm import Session

import horde.classes.base.processing_generation as procgen_module
from horde.classes.base.user import User
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from tests.fixture_types import MakeUser

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


class _FakeResponse:
    def __init__(self, ok: bool, status_code: int) -> None:
        self.ok = ok
        self.status_code = status_code
        self.text = ""


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


class TestDelivery:
    """The sender POSTs the payload and bounds its retries."""

    def test_payload_is_posted_to_subscriber(self, monkeypatch: pytest.MonkeyPatch) -> None:
        posted: list[tuple[str, dict[str, Any]]] = []

        def fake_post(url: str, json: dict[str, Any], timeout: float) -> _FakeResponse:
            posted.append((url, json))
            return _FakeResponse(ok=True, status_code=200)

        monkeypatch.setattr(requests, "post", fake_post)

        procgen_module._deliver_webhook(WEBHOOK_URL, {"id": "gen"}, "wp", "gen")

        assert posted == [(WEBHOOK_URL, {"id": "gen"})]

    def test_persistent_failure_is_bounded_and_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts: list[str] = []

        def failing_post(url: str, json: dict[str, Any], timeout: float) -> _FakeResponse:
            attempts.append(url)
            raise ConnectionError("subscriber unreachable")

        monkeypatch.setattr(requests, "post", failing_post)

        procgen_module._deliver_webhook(WEBHOOK_URL, {"id": "gen"}, "wp", "gen")

        assert len(attempts) == 3
