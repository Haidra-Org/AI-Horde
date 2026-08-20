# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Confirmation that documented input constraints are actually enforced.

Each test drives a request just over a documented boundary and asserts the
specific rejection (status + rc), so a regression that silently drops a check
(letting oversized/abusive input through to the DB or downstream) is caught.
"""

from __future__ import annotations

import pytest

AGENT = "aihorde_ci_client:1.0:(test)ci"
TEXT_MODEL = "elinas/chronos-70b-v2"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _make_worker(client, api_key: str, name: str) -> str:
    client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": name,
            "models": [TEXT_MODEL],
            "bridge_agent": AGENT,
            "amount": 10,
            "max_context_length": 4096,
            "max_length": 512,
        },
        headers=_headers(api_key),
    )
    resp = client.get(f"/api/v2/workers/name/{name}", headers=_headers(api_key))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["id"]


class TestWorkerInfoLimits:
    def test_info_over_1000_chars_rejected(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        wid = _make_worker(client, owner.api_key, "Info Limit Worker")
        resp = client.put(f"/api/v2/workers/{wid}", json={"info": "x" * 1001}, headers=_headers(owner.api_key))
        # Rejected as a 400 either by the RESTX schema (maxLength, shape:
        # {"errors": ...}) or the app-layer length guard (rc TooLongWorkerName).
        assert resp.status_code == 400, resp.get_data(as_text=True)[:300]
        body = resp.get_json()
        assert body.get("rc") == "TooLongWorkerName" or "errors" in body, body

    def test_profane_info_rejected(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        wid = _make_worker(client, owner.api_key, "Profane Info Worker")
        resp = client.put(f"/api/v2/workers/{wid}", json={"info": "fuck this"}, headers=_headers(owner.api_key))
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "ProfaneWorkerInfo"

    def test_rename_to_existing_name_rejected(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        _make_worker(client, owner.api_key, "First Worker Name")
        second = _make_worker(client, owner.api_key, "Second Worker Name")
        resp = client.put(f"/api/v2/workers/{second}", json={"name": "First Worker Name"}, headers=_headers(owner.api_key))
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "WorkerNameAlreadyExists"


class TestUsernameLimits:
    def test_username_over_30_chars_rejected(self, client, make_api_user):
        user = make_api_user(kudos=100)
        resp = client.put(f"/api/v2/users/{user.id}", json={"username": "x" * 31}, headers=_headers(user.api_key))
        assert resp.status_code == 400, resp.get_data(as_text=True)[:300]
        body = resp.get_json()
        assert body.get("rc") == "TooLongUserName" or "errors" in body, body


class TestKudosTransferBoundaries:
    def test_transfer_to_anonymous_rejected(self, client, api_key):
        resp = client.post(
            "/api/v2/kudos/transfer",
            json={"username": "Anonymous#0", "amount": 100},
            headers=_headers(api_key),
        )
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "KudosTransferToAnon"


class TestImageExtraSourceImages:
    def test_more_than_five_extra_source_images_rejected(self, client, api_key):
        # The validate() guard caps extra_source_images at 5 before any download.
        payload = {
            "prompt": "boundary probe",
            "models": ["stable_diffusion"],
            "source_image": "ZmFrZQ==",
            "source_processing": "remix",
            "extra_source_images": [{"image": "ZmFrZQ==", "strength": 1} for _ in range(6)],
            "params": {"width": 512, "height": 512},
        }
        resp = client.post("/api/v2/generate/async", json=payload, headers=_headers(api_key))
        # Either the count guard (rc TooManyExtraSourceImages.) or schema validation
        # rejects it, both are 4xx, never a 500.
        assert 400 <= resp.status_code < 500, resp.get_data(as_text=True)[:300]


class TestEffectiveStyleConstraints:
    def test_flow_shift_is_validated_against_the_styles_models(self, client, api_key):
        headers = _headers(api_key)
        style_resp = client.post(
            "/api/v2/styles/image",
            json={
                "name": "effective flow model",
                "prompt": "flow style {p}###{np}",
                "params": {
                    "width": 512,
                    "height": 512,
                    "steps": 8,
                    "sampler_name": "k_euler",
                    "flow_shift": 1,
                },
                "models": ["Flux.1-Schnell fp8 (Compact)"],
            },
            headers=headers,
        )
        assert style_resp.status_code == 200, style_resp.get_data(as_text=True)
        style_id = style_resp.get_json()["id"]

        try:
            request_resp = client.post(
                "/api/v2/generate/async",
                json={
                    "prompt": "style constraint probe",
                    "params": {"width": 512, "height": 512},
                    "models": ["stable_diffusion"],
                    "style": style_id,
                },
                headers=headers,
            )
            assert request_resp.status_code == 202, request_resp.get_data(as_text=True)
            client.delete(f"/api/v2/generate/status/{request_resp.get_json()['id']}", headers=headers)
        finally:
            client.delete(f"/api/v2/styles/image/{style_id}", headers=headers)


class TestImageEmptyModelsDefault:
    def test_empty_models_list_defaults_to_stable_diffusion(self, app, client, api_key):
        # The parser default only covers an absent "models" key; an explicit [] used to create a WP
        # with no model constraint, which could be popped and recorded with a blank model name.
        headers = _headers(api_key)
        resp = client.post(
            "/api/v2/generate/async",
            json={
                "prompt": "empty models probe",
                "models": [],
                "params": {"width": 512, "height": 512},
            },
            headers=headers,
        )
        assert resp.status_code == 202, resp.get_data(as_text=True)
        wp_id = resp.get_json()["id"]

        try:
            from horde.classes.base.waiting_prompt import WPModels

            with app.app_context():
                from horde.flask import db

                rows = db.session.query(WPModels.model).filter(WPModels.wp_id == wp_id).all()
            assert [row.model for row in rows] == ["stable_diffusion"]
        finally:
            client.delete(f"/api/v2/generate/status/{wp_id}", headers=headers)
