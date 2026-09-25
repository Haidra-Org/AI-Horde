# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Endpoint characterization for retrieving a submitted request's parameters.

The status and check endpoints treat the request ID as a bearer token. The
request endpoints do not: they return the submitted prompt and parameters only
to the API key that submitted the request, in the shape of the generation input.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import inspect

from horde.classes.base.waiting_prompt import SubmittedPromptPurpose
from tests.fixture_types import MakeApiUser

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
ANON_API_KEY: str = "0000000000"

IMAGE_REQUEST: dict[str, Any] = {
    "prompt": "a horde of robots reading their own request back",
    "nsfw": True,
    "censor_nsfw": False,
    "trusted_workers": True,
    "slow_workers": False,
    "params": {"width": 512, "height": 512, "steps": 8, "cfg_scale": 1.5, "sampler_name": "k_euler_a", "n": 2, "seed": "42"},
    "models": ["stable_diffusion"],
    "allow_downgrade": False,
}

TEXT_REQUEST: dict[str, Any] = {
    "prompt": "a text request reading itself back",
    "trusted_workers": True,
    "validated_backends": False,
    "params": {"max_length": 80, "max_context_length": 1024, "n": 1},
    "models": ["elinas/chronos-70b-v2"],
}


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(autouse=True)
def _no_external_prompt_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep submissions that reach the prompt filter from calling R2 or Discord."""
    from horde.apis.v2 import base

    monkeypatch.setattr(base, "upload_prompt", lambda *_: None)
    monkeypatch.setattr(base, "send_problem_user_notification", lambda *_: None)


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Client-Agent": AGENT}
    if api_key is not None:
        headers["apikey"] = api_key
    return headers


@pytest.fixture
def image_request(
    client: FlaskClient,
    make_api_user: MakeApiUser,
    settle_kudos: Callable[[], int],
) -> Iterator[tuple[str, str]]:
    """Submit an image request as a fresh user and yield ``(request_id, submitter_key)``."""
    submitter = make_api_user(kudos=1000)
    settle_kudos()
    resp = client.post("/api/v2/generate/async", json=IMAGE_REQUEST, headers=_headers(submitter.api_key))
    assert resp.status_code == 202, resp.get_data(as_text=True)
    req_id = resp.get_json()["id"]
    try:
        yield req_id, submitter.api_key
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=_headers(submitter.api_key))


class TestImageRequestParameters:
    def test_the_submitting_key_gets_the_request_back_in_input_shape(self, client: FlaskClient, image_request) -> None:
        req_id, submitter_key = image_request

        resp = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(submitter_key))

        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["prompt"] == IMAGE_REQUEST["prompt"]
        assert body["models"] == IMAGE_REQUEST["models"]
        assert body["nsfw"] is True
        assert body["censor_nsfw"] is False
        assert body["trusted_workers"] is True
        assert body["slow_workers"] is False
        assert body["params"]["n"] == 2
        assert body["params"]["seed"] == "42"
        assert body["params"]["steps"] == 8
        assert body["params"]["cfg_scale"] == 1.5
        assert "allow_downgrade" not in body
        assert "dry_run" not in body

    def test_the_returned_request_is_accepted_when_resubmitted(self, client: FlaskClient, image_request) -> None:
        req_id, submitter_key = image_request
        body = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(submitter_key)).get_json()

        resubmitted = client.post("/api/v2/generate/async", json=body, headers=_headers(submitter_key))
        try:
            assert resubmitted.status_code == 202, resubmitted.get_data(as_text=True)
        finally:
            if resubmitted.status_code == 202:
                client.delete(f"/api/v2/generate/status/{resubmitted.get_json()['id']}", headers=_headers(submitter_key))

    def test_another_users_key_is_refused(self, client: FlaskClient, make_api_user: MakeApiUser, image_request) -> None:
        req_id, _ = image_request
        other = make_api_user()

        resp = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(other.api_key))

        assert resp.status_code == 403, resp.get_data(as_text=True)
        assert resp.get_json()["rc"] == "NotRequestOwner"

    def test_the_anonymous_key_is_refused(self, client: FlaskClient, image_request) -> None:
        req_id, _ = image_request

        resp = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(ANON_API_KEY))

        assert resp.status_code == 403, resp.get_data(as_text=True)
        assert resp.get_json()["rc"] == "AnonForbidden"

    def test_an_unknown_key_is_unauthorized(self, client: FlaskClient, image_request) -> None:
        req_id, _ = image_request

        resp = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers("not-a-real-key"))

        assert resp.status_code == 401, resp.get_data(as_text=True)
        assert resp.get_json()["rc"] == "InvalidAPIKey"

    def test_a_missing_key_is_a_client_error(self, client: FlaskClient, image_request) -> None:
        req_id, _ = image_request

        resp = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(None))

        assert 400 <= resp.status_code < 500, resp.get_data(as_text=True)

    def test_an_unknown_request_is_not_found(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        user = make_api_user()

        resp = client.get("/api/v2/generate/request/00000000-0000-0000-0000-000000000000", headers=_headers(user.api_key))

        assert resp.status_code == 404, resp.get_data(as_text=True)
        assert resp.get_json()["rc"] == "RequestNotFound"


class TestSharedKeyRequestParameters:
    def test_the_submitting_shared_key_and_its_owner_can_read_it_but_not_another_user(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        owner = make_api_user(kudos=1000)
        other = make_api_user()
        settle_kudos()
        created = client.put("/api/v2/sharedkeys", json={"kudos": 500, "name": "reader"}, headers=_headers(owner.api_key))
        assert created.status_code == 200, created.get_data(as_text=True)
        shared_key = created.get_json()["id"]

        submitted = client.post("/api/v2/generate/async", json=IMAGE_REQUEST, headers=_headers(shared_key))
        assert submitted.status_code == 202, submitted.get_data(as_text=True)
        req_id = submitted.get_json()["id"]
        try:
            as_shared = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(shared_key))
            as_owner = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(owner.api_key))
            as_other = client.get(f"/api/v2/generate/request/{req_id}", headers=_headers(other.api_key))
        finally:
            client.delete(f"/api/v2/generate/status/{req_id}", headers=_headers(owner.api_key))

        assert as_shared.status_code == 200, as_shared.get_data(as_text=True)
        assert as_owner.status_code == 200, as_owner.get_data(as_text=True)
        assert as_other.status_code == 403, as_other.get_data(as_text=True)
        assert as_shared.get_json()["prompt"] == IMAGE_REQUEST["prompt"]


class TestTextRequestParameters:
    def test_the_submitting_key_gets_the_request_back(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        submitter = make_api_user(kudos=1000)
        other = make_api_user()
        settle_kudos()
        submitted = client.post("/api/v2/generate/text/async", json=TEXT_REQUEST, headers=_headers(submitter.api_key))
        assert submitted.status_code == 202, submitted.get_data(as_text=True)
        req_id = submitted.get_json()["id"]
        try:
            as_submitter = client.get(f"/api/v2/generate/text/request/{req_id}", headers=_headers(submitter.api_key))
            as_other = client.get(f"/api/v2/generate/text/request/{req_id}", headers=_headers(other.api_key))
        finally:
            client.delete(f"/api/v2/generate/text/status/{req_id}", headers=_headers(submitter.api_key))

        assert as_submitter.status_code == 200, as_submitter.get_data(as_text=True)
        body = as_submitter.get_json()
        assert body["prompt"] == TEXT_REQUEST["prompt"]
        assert body["models"] == TEXT_REQUEST["models"]
        assert body["params"]["max_length"] == 80
        assert body["params"]["n"] == 1
        assert as_other.status_code == 403, as_other.get_data(as_text=True)
        assert as_other.get_json()["rc"] == "NotRequestOwner"

    def test_the_returned_request_is_accepted_when_resubmitted(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        submitter = make_api_user(kudos=1000)
        settle_kudos()
        submitted = client.post("/api/v2/generate/text/async", json=TEXT_REQUEST, headers=_headers(submitter.api_key))
        assert submitted.status_code == 202, submitted.get_data(as_text=True)
        req_id = submitted.get_json()["id"]
        resubmitted = None
        try:
            body = client.get(f"/api/v2/generate/text/request/{req_id}", headers=_headers(submitter.api_key)).get_json()
            resubmitted = client.post("/api/v2/generate/text/async", json=body, headers=_headers(submitter.api_key))
            assert resubmitted.status_code == 202, resubmitted.get_data(as_text=True)
        finally:
            client.delete(f"/api/v2/generate/text/status/{req_id}", headers=_headers(submitter.api_key))
            if resubmitted is not None and resubmitted.status_code == 202:
                client.delete(f"/api/v2/generate/text/status/{resubmitted.get_json()['id']}", headers=_headers(submitter.api_key))

    def test_an_unknown_request_is_not_found(self, client: FlaskClient, make_api_user: MakeApiUser) -> None:
        user = make_api_user()

        resp = client.get("/api/v2/generate/text/request/00000000-0000-0000-0000-000000000000", headers=_headers(user.api_key))

        assert resp.status_code == 404, resp.get_data(as_text=True)
        assert resp.get_json()["rc"] == "RequestNotFound"


class TestSubmittedPromptProvenance:
    @pytest.mark.parametrize("mode", ["opt_in", "education", "model", "flagged"])
    def test_a_replaced_prompt_reaches_workers_while_the_original_stays_private(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """Every replacement path stores the replaced prompt as the effective prompt.

        The original prompt is returned only by the owner-only request endpoint. The
        public status and check endpoints expose neither it nor the provenance
        field, and a row without provenance omits ``prompt`` rather than falling
        back to the effective prompt.
        """
        from horde.apis.v2 import base
        from horde.classes.base.user import User
        from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
        from horde.flask import db

        submitter = make_api_user(kudos=1000)
        settle_kudos()
        with app.app_context():
            account = db.session.get(User, submitter.id)
            if mode == "education":
                account.set_education(True)
            if mode == "flagged":
                account.set_flagged(True)
            db.session.commit()
        checker = base.prompt_checker
        monkeypatch.setattr(type(checker), "__call__", lambda *_: (2 if mode in {"opt_in", "education"} else 0, []))
        monkeypatch.setattr(checker, "check_nsfw_model_block", lambda *_: mode == "model")
        monkeypatch.setattr(checker, "apply_replacement_filter", lambda *_: "synthetic replacement")
        monkeypatch.setattr(checker, "nsfw_model_prompt_replace", lambda *_, **__: "synthetic replacement")
        payload = {**IMAGE_REQUEST, "replacement_filter": mode == "opt_in"}
        response = client.post("/api/v2/generate/async", json=payload, headers={"apikey": submitter.api_key})
        assert response.status_code == 202, response.get_json()
        request_id = response.get_json()["id"]
        retrieved = client.get(f"/api/v2/generate/request/{request_id}", headers={"apikey": submitter.api_key})
        assert retrieved.get_json()["prompt"] == payload["prompt"]
        assert retrieved.headers["Cache-Control"] == "private, no-store"
        for route in ("status", "check"):
            public_response = client.get(f"/api/v2/generate/{route}/{request_id}")
            assert public_response.status_code == 200, public_response.get_json()
            assert payload["prompt"] not in public_response.get_data(as_text=True)
            assert "submitted_prompt" not in public_response.get_data(as_text=True)
        with app.app_context():
            waiting = db.session.get(ImageWaitingPrompt, request_id)
            assert "submitted_prompt" in inspect(waiting).unloaded
            assert waiting.prompt == "synthetic replacement"
            assert waiting.gen_payload["prompt"] == "synthetic replacement"
            assert waiting.get_share_metadata()["prompt"] == "synthetic replacement"
            # Simulate a pre-migration row. Neither the serializer nor REST marshalling
            # may substitute the effective prompt for unavailable provenance.
            waiting.submitted_prompt = None
            db.session.commit()
        legacy = client.get(f"/api/v2/generate/request/{request_id}", headers={"apikey": submitter.api_key})
        assert "prompt" not in legacy.get_json()
        client.delete(f"/api/v2/generate/status/{request_id}", headers={"apikey": submitter.api_key})

    def test_a_style_changes_the_effective_prompt_but_not_the_original(
        self,
        client: FlaskClient,
        app: Flask,
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A style rewrites the effective prompt while the stored original is the submitted text."""
        from horde.apis.v2 import base, stable
        from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
        from horde.flask import db

        submitter = make_api_user(kudos=1000)
        settle_kudos()
        original_apply_style = stable.ImageAsyncGenerate.apply_style

        def apply_synthetic_style(resource) -> None:
            original_apply_style(resource)
            resource.prompt = "style prefix: " + resource.prompt

        monkeypatch.setattr(stable.ImageAsyncGenerate, "apply_style", apply_synthetic_style)
        checker = base.prompt_checker
        monkeypatch.setattr(type(checker), "__call__", lambda *_: (0, []))
        monkeypatch.setattr(checker, "check_nsfw_model_block", lambda *_: False)
        response = client.post(
            "/api/v2/generate/async",
            json={**IMAGE_REQUEST, "replacement_filter": False},
            headers={"apikey": submitter.api_key},
        )
        assert response.status_code == 202, response.get_json()
        request_id = response.get_json()["id"]
        try:
            with app.app_context():
                waiting = db.session.get(ImageWaitingPrompt, request_id)
                assert waiting.prompt == "style prefix: " + IMAGE_REQUEST["prompt"]
                original = waiting.read_privileged_submitted_prompt(purpose=SubmittedPromptPurpose.MODERATION_EVIDENCE)
                assert original == IMAGE_REQUEST["prompt"]
        finally:
            client.delete(f"/api/v2/generate/status/{request_id}", headers={"apikey": submitter.api_key})
