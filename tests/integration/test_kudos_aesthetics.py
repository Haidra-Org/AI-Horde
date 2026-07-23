# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Endpoint characterization for the aesthetics rating reward.

Rating the images of a completed, publicly shared request awards the requester
kudos: five per rating. The award can never exceed what the request cost, so it
is capped at the request's consumed kudos minus one.

This drives the real ``/generate/rate`` endpoint. The rating relay to the
external ratings service is outside this suite's contract and is stubbed to
succeed so the kudos award is observed deterministically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.fixture_types import MakeApiUser

AGENT: str = "aihorde_ci_client:1.0:(test)ci"


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Disable the rate limiter for the duration of a test."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
def _stub_ratings_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the outbound relay to the external ratings service to always succeed."""
    import horde.apis.v2.stable as stable_module

    monkeypatch.setattr(stable_module.requests, "post", lambda *a, **k: SimpleNamespace(ok=True, status_code=200))


def _build_completed_shared_wp(app: Flask, requester_id: int, *, consumed_kudos: int) -> tuple[str, str]:
    """Create a completed, publicly shared waiting prompt and return its id and generation id."""
    from horde.classes.stable.processing_generation import ImageProcessingGeneration
    from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
    from horde.classes.stable.worker import ImageWorker
    from horde.flask import db

    with app.app_context():
        worker = ImageWorker(name=f"worker_{uuid.uuid4().hex[:8]}", user_id=requester_id)
        db.session.add(worker)
        db.session.commit()

        wp = ImageWaitingPrompt(
            worker_ids=[],
            models=["stable_diffusion"],
            prompt="a shared test robot",
            user_id=requester_id,
            params={"width": 512, "height": 512, "steps": 8, "sampler_name": "k_euler_a", "n": 1},
        )
        wp.shared = True
        wp.n = 0
        wp.consumed_kudos = consumed_kudos
        db.session.commit()

        procgen = ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="stable_diffusion")
        procgen.generation = "R2"
        procgen.seed = 0
        db.session.commit()
        return str(wp.id), str(procgen.id)


def _requester_kudos(app: Flask, user_id: int) -> float:
    """Return the committed kudos balance for the user with the given id."""
    from horde.database import functions as database

    with app.app_context():
        return database.find_user_by_id(user_id).kudos


class TestAestheticsReward:
    """Rating a completed, publicly shared request's images awards the requester kudos."""

    def test_reward_is_five_kudos_per_rating(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        _stub_ratings_server: None,
    ) -> None:
        """Each rating submitted for a shared request awards the requester five kudos."""
        requester = make_api_user(kudos=1000)
        wp_id, procgen_id = _build_completed_shared_wp(app, requester.id, consumed_kudos=100)
        before = _requester_kudos(app, requester.id)

        resp = client.post(
            f"/api/v2/generate/rate/{wp_id}",
            json={"ratings": [{"id": procgen_id, "rating": 8}]},
            headers={"Client-Agent": AGENT},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["reward"] == 5
        assert _requester_kudos(app, requester.id) == before + 5

    def test_reward_is_capped_at_consumed_kudos_minus_one(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        _stub_ratings_server: None,
    ) -> None:
        """The rating reward is capped at the request's consumed kudos minus one."""
        requester = make_api_user(kudos=1000)
        # One rating would award 5, but the request only consumed 3 kudos.
        wp_id, procgen_id = _build_completed_shared_wp(app, requester.id, consumed_kudos=3)
        before = _requester_kudos(app, requester.id)

        resp = client.post(
            f"/api/v2/generate/rate/{wp_id}",
            json={"ratings": [{"id": procgen_id, "rating": 8}]},
            headers={"Client-Agent": AGENT},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["reward"] == 2
        assert _requester_kudos(app, requester.id) == before + 2
