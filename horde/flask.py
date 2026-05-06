# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import socket

from flask import Flask
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

from horde.logger import logger
from horde.redis_ctrl import ger_cache_url, is_redis_up

# expire_on_commit=False keeps ORM attributes valid across commits within a
# single request. SQLAlchemy's default (True) expires all loaded instances on
# commit, forcing a full-row re-fetch on the next attribute access, seen
# adding 100–180ms to WP.activate under pool contention.
db = SQLAlchemy(session_options={"expire_on_commit": False})
cache = Cache()
SQLITE_MODE = os.getenv("USE_SQLITE", "0") == "1"

_app_instance = None


def get_app():
    """Return the app instance for background threads that need app_context()."""
    if _app_instance is None:
        raise RuntimeError("App not created yet — call create_app() first")
    return _app_instance


# SQLAlchemy's `handle_error` event does not fire on QueuePool checkout
# timeouts (the exception is raised directly inside Pool._do_get without
# event dispatch). Subclassing is the only reliable hook.
# from sqlalchemy.exc import TimeoutError as _SAQueuePoolTimeoutError  # noqa: E402
from sqlalchemy.pool import QueuePool as _BaseQueuePool  # noqa: E402

# class _InstrumentedQueuePool(_BaseQueuePool):
#     def _do_get(self):
#         try:
#             return super()._do_get()
#         except _SAQueuePoolTimeoutError:
#             from horde import metrics

#             metrics.db_pool_timeout.add(1)
#             raise


def create_app(config=None):
    global _app_instance

    app = Flask(__name__)
    app.config.SWAGGER_UI_DOC_EXPANSION = "list"
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    # Telemetry MUST be initialised before any other extension (especially
    # Flask-Limiter) registers a `before_request`. The OTel Flask
    # instrumentation registers its own `before_request` to stash a span on
    # `environ`; if Flask-Limiter runs first and short-circuits with a 429,
    # the OTel hook never executes and the WSGI middleware logs spurious
    # "Flask environ's OpenTelemetry span missing" warnings on every
    # rate-limited response.
    # from horde.telemetry import init_telemetry_early

    # init_telemetry_early(app)

    if config:
        app.config.update(config)

    if "SQLALCHEMY_DATABASE_URI" not in app.config:
        if SQLITE_MODE:
            logger.warning("Using SQLite for database")
            app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///horde.db"
        else:
            app.config["SQLALCHEMY_DATABASE_URI"] = (
                f"postgresql://{os.getenv('POSTGRES_USER', 'postgres')}:{os.getenv('POSTGRES_PASS')}@{os.getenv('POSTGRES_URL')}"
            )
            # Prior default (pool_size=50, max_overflow=-1) let each of N replicas
            # open up to unlimited connections, easily exceeding Postgres'
            # max_connections under sustained load. Pool occupancy during
            # stress was ~100% while *active* queries were <5% of the pool,
            # i.e. connections were held across non-DB work (kudos torch,
            # redis, webhook, log formatting). Shrinking the pool surfaces
            # that inefficiency via QueuePool timeouts instead of PG refusals.
            app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
                "poolclass": _BaseQueuePool,
                "pool_size": int(os.getenv("SQLALCHEMY_POOL_SIZE", "15")),
                "max_overflow": int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "5")),
                "pool_timeout": int(os.getenv("SQLALCHEMY_POOL_TIMEOUT", "30")),
                "pool_pre_ping": os.getenv("SQLALCHEMY_POOL_PRE_PING", "0") == "1",
            }
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    db.init_app(app)

    if not SQLITE_MODE and not app.config.get("TESTING"):
        with app.app_context():
            logger.warning(f"pool size = {db.engine.pool.size()}")
    logger.init_ok("Horde Database", status="Started")

    logger.init("Flask Cache", status="Connecting")
    if is_redis_up() and not app.config.get("TESTING"):
        try:
            app.config.update(
                {
                    "CACHE_TYPE": "RedisCache",
                    "CACHE_REDIS_URL": ger_cache_url(),
                    "CACHE_DEFAULT_TIMEOUT": 300,
                },
            )
            cache.init_app(app)
            logger.init_ok("Flask Cache", status="Connected")
        except Exception as e:
            logger.error(f"Flask Cache Failed: {e}")
            app.config.update({"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})
            cache.init_app(app)
            logger.init_warn("Flask Cache", status="SimpleCache")
    else:
        app.config.update({"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300})
        cache.init_app(app)
        logger.init_warn("Flask Cache", status="SimpleCache")

    from horde.limiter import init_limiter

    init_limiter(app)

    if not app.config.get("TESTING"):
        from horde.horde_redis import horde_redis

        horde_redis.connect()

    if not app.config.get("TESTING"):
        from horde.countermeasures import init_countermeasures

        init_countermeasures()

    from horde.apis import apiv2
    from horde.routes import routes_bp

    app.register_blueprint(apiv2)
    app.register_blueprint(routes_bp)

    _register_oauth(app)

    from horde.argparser import args
    from horde.consts import HORDE_VERSION

    @app.after_request
    def after_request(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE, PATCH"
        response.headers["Access-Control-Allow-Headers"] = (
            "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, apikey, "
            "Client-Agent, X-Fields, X-Forwarded-For, Proxied-For, Proxy-Authorization"
        )
        response.headers["Horde-Node"] = f"{socket.gethostname()}:{args.port}:{HORDE_VERSION}"

        if response.content_type == "application/json":
            response.content_type = "application/json; charset=utf-8"

        return response

    if not app.config.get("TESTING"):
        from horde.classes import init_db

        init_db(app)

    if not app.config.get("TESTING"):
        from horde.database import start_background_threads

        start_background_threads()

    _app_instance = app
    return app


def _register_oauth(app):
    from flask_dance.contrib.discord import make_discord_blueprint
    from flask_dance.contrib.github import make_github_blueprint
    from flask_dance.contrib.google import make_google_blueprint

    app.secret_key = os.getenv("secret_key")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

    google_blueprint = make_google_blueprint(
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GLOOGLE_CLIENT_SECRET"),
        reprompt_consent=True,
        redirect_url="/register",
        scope=["email"],
    )
    app.register_blueprint(google_blueprint, url_prefix="/google")

    discord_blueprint = make_discord_blueprint(
        client_id=os.getenv("DISCORD_CLIENT_ID"),
        client_secret=os.getenv("DISCORD_CLIENT_SECRET"),
        scope=["identify"],
        redirect_url="/finish_dance",
    )
    app.register_blueprint(discord_blueprint, url_prefix="/discord")

    github_blueprint = make_github_blueprint(
        client_id=os.getenv("GITHUB_CLIENT_ID"),
        client_secret=os.getenv("GITHUB_CLIENT_SECRET"),
        scope=["identify"],
        redirect_url="/finish_dance",
    )
    app.register_blueprint(github_blueprint, url_prefix="/github")
