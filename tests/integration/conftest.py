# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name, postgres_reachable

TEST_USER_BOOTSTRAP_PAYLOAD = {
    "username": "test_user",
    "oauth_id": "ci_test_user",
    "moderator": True,
    "trusted": True,
    "kudos": 10000,
}


def _seed_core_rows() -> None:
    """Seed minimum rows expected by API code paths in tests.

    Integration tests do not rely on cron/stored-procedure setup; they only
    need schema objects plus baseline records for anonymous user and settings.
    """
    from horde.classes.base.settings import HordeSettings
    from horde.classes.base.user import User
    from horde.flask import db
    from horde.utils import hash_api_key

    anon = db.session.query(User).filter_by(oauth_id="anon").first()
    if not anon:
        anon = User(
            id=0,
            username="Anonymous",
            oauth_id="anon",
            api_key=hash_api_key("0000000000"),
            public_workers=True,
            concurrency=500,
        )
        db.session.add(anon)

    settings = HordeSettings.query.first()
    if not settings:
        db.session.add(HordeSettings())

    db.session.commit()


@pytest.fixture(scope="module")
def _pg_schema(pg_dsn: str) -> Iterator[str]:
    # Schema-level isolation is enough for the current in-process API tests.
    # Once we start exercising init_db()'s pg_cron-backed stored procedures,
    # those tests should move to per-session databases instead: cron jobs and
    # extension state are database-global, not schema-local.
    schema_name = new_test_schema_name("horde_integration_test")
    create_schema(pg_dsn, schema_name)
    try:
        yield schema_name
    finally:
        drop_schema(pg_dsn, schema_name)


@pytest.fixture(scope="module")
def app(pg_dsn: str, _pg_schema: str):
    from horde.flask import create_app, db

    app = create_app(
        config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": pg_dsn,
            "SQLALCHEMY_ENGINE_OPTIONS": {
                "pool_size": 5,
                "max_overflow": 0,
                "connect_args": {"options": f"-c search_path={_pg_schema}"},
            },
        },
    )

    with app.app_context():
        current_schema = db.session.execute(sqlalchemy.text("SELECT current_schema()")).scalar_one()
        if current_schema != _pg_schema:
            raise RuntimeError(
                f"Integration fixture safety check failed: expected current schema {_pg_schema!r}, got {current_schema!r}.",
            )

        db.create_all()
        _seed_core_rows()
        try:
            yield app
        finally:
            try:
                db.session.remove()
            except sqlalchemy.exc.OperationalError:
                if postgres_reachable(pg_dsn):
                    raise


@pytest.fixture(scope="module", autouse=True)
def _fake_redis_backend(app) -> Iterator[None]:
    """Provide Redis handles for API paths that touch cache helpers.

    Prefer fakeredis for hermetic tests. If fakeredis is unavailable, fall
    back to a real Redis connection configured via environment variables.
    """
    try:
        import fakeredis
    except ModuleNotFoundError:
        from horde import horde_redis as horde_redis_module

        redis_conn = horde_redis_module.horde_redis
        redis_conn.connect()
        if redis_conn.horde_r is None:
            pytest.skip(
                "Integration tests require either fakeredis, an external Redis backend, or automatic provisioning in auto mode.",
                allow_module_level=False,
            )
        _reset_redis_state()
        yield
        return

    from horde import horde_redis as horde_redis_module

    redis_conn = horde_redis_module.horde_redis
    fake = fakeredis.FakeStrictRedis()

    old_horde_r = redis_conn.horde_r
    old_horde_local_r = redis_conn.horde_local_r
    old_all_horde_redis = redis_conn.all_horde_redis

    redis_conn.horde_r = fake
    redis_conn.horde_local_r = fake
    redis_conn.all_horde_redis = [fake]
    _reset_redis_state()

    try:
        yield
    finally:
        redis_conn.horde_r = old_horde_r
        redis_conn.horde_local_r = old_horde_local_r
        redis_conn.all_horde_redis = old_all_horde_redis


def _reset_redis_state() -> None:
    from horde import horde_redis as horde_redis_module

    redis_conn = horde_redis_module.horde_redis
    seen_clients: set[int] = set()
    clients = [redis_conn.horde_r, redis_conn.horde_local_r, *redis_conn.all_horde_redis]

    for redis_client in clients:
        if redis_client is None or id(redis_client) in seen_clients:
            continue
        seen_clients.add(id(redis_client))

        try:
            redis_client.flushdb()
        except Exception:
            continue


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def request_headers(api_key: str, CIVERSION: str) -> dict[str, str]:
    return {
        "apikey": api_key,
        "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625",
    }


@pytest.fixture
def make_api_user(app):
    """Factory: create a registered user and return its id/api_key/username/alias.

    Used by endpoint tests that need actors at specific privilege levels (a
    non-moderator owner, a second user to receive kudos, etc.) distinct from the
    moderator+trusted ``api_key`` fixture user.
    """
    import uuid
    from types import SimpleNamespace

    from horde.classes.base.user import User
    from horde.utils import generate_api_key, hash_api_key

    def _make(*, trusted: bool = False, moderator: bool = False, kudos: int = 0):
        suffix = uuid.uuid4().hex[:8]
        username = f"user_{suffix}"
        raw_api_key = generate_api_key()
        with app.app_context():
            user = User(username=username, oauth_id=f"oauth_{suffix}", api_key=hash_api_key(raw_api_key))
            user.create()
            if moderator:
                user.set_moderator(True)
            if trusted:
                user.set_trusted(True)
            if kudos:
                user.modify_kudos(kudos, "admin")
            user.refresh_cache()
            return SimpleNamespace(
                id=user.id,
                api_key=raw_api_key,
                username=username,
                alias=user.get_unique_alias(),
            )

    return _make


@pytest.fixture
def api_key(app) -> str:
    """Create or refresh the integration test user and return a plaintext API key."""
    from horde.classes.base.user import User
    from horde.database import functions as database
    from horde.flask import db
    from horde.utils import generate_api_key, hash_api_key

    username = TEST_USER_BOOTSTRAP_PAYLOAD["username"]
    oauth_id = TEST_USER_BOOTSTRAP_PAYLOAD["oauth_id"]
    moderator = TEST_USER_BOOTSTRAP_PAYLOAD["moderator"]
    trusted = TEST_USER_BOOTSTRAP_PAYLOAD["trusted"]
    kudos = TEST_USER_BOOTSTRAP_PAYLOAD["kudos"]

    provisioned_api_key = generate_api_key()
    hashed_api_key = hash_api_key(provisioned_api_key)

    with app.app_context():
        user = database.find_user_by_oauth_id(oauth_id)
        if user:
            user.username = username
            user.api_key = hashed_api_key
            db.session.commit()
        else:
            user = User(username=username, oauth_id=oauth_id, api_key=hashed_api_key)
            user.create()

        user.set_moderator(moderator)
        if not moderator:
            user.set_trusted(trusted)
        if isinstance(kudos, int) and user.kudos != kudos:
            user.modify_kudos(kudos - user.kudos, "admin")
        user.refresh_cache()

    return provisioned_api_key
