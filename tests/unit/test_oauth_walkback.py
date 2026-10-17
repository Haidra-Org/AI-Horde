# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from http.cookies import SimpleCookie

import pytest
from flask import Blueprint, Flask

from horde.flask import _register_oauth
from horde.routes import OAUTH_RETURN_TO_SESSION_KEY, routes_bp, sanitise_return_path

BACKSLASH = chr(92)
"""A literal backslash, spelled by code point so the test values show no escape sequences."""


OAUTH_PROVIDERS = (
    "discord",
    "github",
    "google",
)
"""The providers whose login start routes accept a `return_to` segment."""


def _oauth_test_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "shared-test-secret"

    for provider in OAUTH_PROVIDERS:
        provider_blueprint = Blueprint(provider, __name__)
        provider_blueprint.add_url_rule("/", endpoint="login", view_func=lambda: "provider login")
        app.register_blueprint(provider_blueprint, url_prefix=f"/{provider}")
    app.register_blueprint(routes_bp)
    return app


def _walkback_location(start_path: str) -> str:
    client = _oauth_test_app().test_client()
    login_response = client.get(start_path)
    assert login_response.status_code == 302
    callback_response = client.get("/finish_dance")
    assert callback_response.status_code == 302
    return callback_response.headers["Location"]


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
@pytest.mark.parametrize(
    "return_to",
    [
        "register",
        "transfer",
    ],
)
def test_walkback_returns_to_a_plain_page(provider: str, return_to: str) -> None:
    assert _walkback_location(f"/{provider}/{return_to}") == f"/{return_to}"


@pytest.mark.parametrize("provider", OAUTH_PROVIDERS)
@pytest.mark.parametrize(
    "return_to",
    [
        pytest.param(BACKSLASH + "evil.example", id="raw-backslash"),
        pytest.param("%5Cevil.example", id="encoded-backslash"),
        pytest.param("%5c%5cevil.example", id="encoded-double-backslash"),
        pytest.param(BACKSLASH + "%5Cevil.example", id="mixed-backslash"),
        pytest.param("%255Cevil.example", id="double-encoded-backslash"),
        pytest.param("evil.example%5C", id="trailing-backslash"),
        pytest.param("%252F%252Fevil.example", id="double-encoded-protocol-relative"),
        pytest.param("http:evil.example", id="scheme"),
        pytest.param("javascript:alert(1)", id="javascript-scheme"),
        pytest.param("%09evil.example", id="tab"),
        pytest.param("%0D%0ASet-Cookie=x", id="crlf"),
        pytest.param("%7Fevil.example", id="delete"),
        pytest.param("%250Aevil.example", id="double-encoded-newline"),
    ],
)
def test_walkback_refuses_an_off_origin_target(provider: str, return_to: str) -> None:
    assert _walkback_location(f"/{provider}/{return_to}") == "/"


@pytest.mark.parametrize(
    ("stored_target", "expected_location"),
    [
        pytest.param("/register", "/register", id="plain-page"),
        pytest.param("/" + BACKSLASH + "evil.example", "/", id="backslash"),
        pytest.param("/%5Cevil.example", "/", id="encoded-backslash"),
        pytest.param("//evil.example", "/", id="protocol-relative"),
        pytest.param("https://evil.example", "/", id="absolute-url"),
        pytest.param("/http:evil.example", "/", id="scheme"),
        pytest.param("/\tevil.example", "/", id="tab"),
        pytest.param("", "/", id="empty"),
        pytest.param(["/register"], "/", id="not-a-string"),
    ],
)
def test_walkback_sanitises_a_target_stored_before_sanitising(stored_target: object, expected_location: str) -> None:
    client = _oauth_test_app().test_client()
    with client.session_transaction() as oauth_session:
        oauth_session[OAUTH_RETURN_TO_SESSION_KEY] = stored_target

    callback_response = client.get("/finish_dance")
    assert callback_response.status_code == 302
    assert callback_response.headers["Location"] == expected_location


@pytest.mark.parametrize(
    "value",
    [
        "register",
        "/register",
        "%255Cevil.example",
        "//evil.example",
    ],
)
def test_sanitising_a_return_path_twice_is_stable(value: str) -> None:
    once = sanitise_return_path(value)
    assert sanitise_return_path(once) == once


def test_oauth_walkback_survives_a_different_app_instance() -> None:
    first_app = _oauth_test_app()
    second_app = _oauth_test_app()

    login_response = first_app.test_client().get("/discord/register")
    assert login_response.status_code == 302

    session_cookie = SimpleCookie()
    session_cookie.load(login_response.headers["Set-Cookie"])
    cookie_name = first_app.config["SESSION_COOKIE_NAME"]

    callback_client = second_app.test_client()
    callback_client.set_cookie("localhost", cookie_name, session_cookie[cookie_name].value)

    callback_response = callback_client.get("/finish_dance")
    assert callback_response.status_code == 302
    assert callback_response.headers["Location"] == "/register"

    # The target is single-use, so a stale callback cannot replay it.
    replay_response = callback_client.get("/finish_dance")
    assert replay_response.headers["Location"] == "/"


def test_stale_github_oauth_state_returns_to_registration(monkeypatch) -> None:
    monkeypatch.setenv("secret_key", "shared-test-secret")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "github-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "github-client-secret")

    app = Flask(__name__)
    _register_oauth(app)
    client = app.test_client()

    login_response = client.get("/github/github", base_url="https://localhost")
    assert login_response.status_code == 302
    assert app.blueprints["github"].scope == ["read:user"]

    stale_callback = client.get(
        "/github/github/authorized?code=test-code&state=stale-state",
        base_url="https://localhost",
    )
    assert stale_callback.status_code == 302
    assert stale_callback.headers["Location"] == "/register"

    with client.session_transaction() as oauth_session:
        assert "github_oauth_state" not in oauth_session
