# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from http.cookies import SimpleCookie

from flask import Blueprint, Flask

from horde.routes import routes_bp


def _oauth_test_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "shared-test-secret"

    discord_blueprint = Blueprint("discord", __name__)
    discord_blueprint.add_url_rule("/", endpoint="login", view_func=lambda: "discord login")
    app.register_blueprint(discord_blueprint, url_prefix="/discord")
    app.register_blueprint(routes_bp)
    return app


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
