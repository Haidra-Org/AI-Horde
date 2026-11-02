# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy

from tests.dependency_runtime import (
    REQUIRE_TEST_DEPS_ENV,
    create_schema,
    drop_schema,
    new_test_schema_name,
    postgres_reachable,
    redis_reachable,
)
from tests.fixture_types import ApiUser, MakeApiUser
from tests.integration._fake_chat_worker import OFFLOAD_THRESHOLD_BYTES
from tests.integration._front_stack import (
    CHAT_SIGNAL_REDIS_DB,
    FAIL_FAST_GRACE_SECONDS,
    FALLBACK_POLL_SECONDS,
    LOOPBACK_HOST,
    REQUEST_BUDGET_SECONDS,
    SERVER_START_TIMEOUT_SECONDS,
    SERVER_STOP_TIMEOUT_SECONDS,
    FixedReplyWorker,
)

if TYPE_CHECKING:
    import fakeredis
    from flask import Flask
    from flask.testing import FlaskClient

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
    from horde.classes.base.kudos import KudosLedgerControl
    from horde.classes.base.settings import HordeSettings
    from horde.classes.base.user import User
    from horde.enums import KudosLedgerMode
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

    control = db.session.query(KudosLedgerControl).filter_by(id=1).first()
    if control is None:
        db.session.add(KudosLedgerControl(id=1, mode=str(KudosLedgerMode.LEDGER)))

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
def app(pg_dsn: str, _pg_schema: str) -> Iterator[Flask]:
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
def client(app: Flask) -> FlaskClient:
    """Return a test client that releases the ORM session after every request.

    The module-scoped ``app`` fixture keeps one app context pushed, and a test-client request reuses it,
    so the scoped session is never removed between requests and, with ``expire_on_commit`` off, a
    relationship loaded before a row was inserted stays stale. Production serves each request on a
    session of its own; removing the session after each call reproduces that.
    """
    from horde.flask import db

    test_client = app.test_client()
    open_request = test_client.open

    def open_and_release_session(*args: Any, **kwargs: Any) -> Any:
        try:
            return open_request(*args, **kwargs)
        finally:
            db.session.remove()

    test_client.open = open_and_release_session  # type: ignore[method-assign]
    return test_client


@pytest.fixture
def settle_kudos(app: Flask) -> Callable[[], int]:
    """Return a helper that folds all pending kudos ledger rows immediately.

    Kudos balances are applier-maintained: a movement is recorded as a ledger
    posting and materialized into ``users.kudos``/``users.evaluating_kudos``/
    ``workers.kudos`` by the applier. Call the returned helper before asserting a
    balance (or before an endpoint whose upfront gate reads the balance) so the
    assertion observes the folded result. Amounts and routing are unchanged; only
    the observation point is. The helper pushes its own app context so it can be
    called between the test's own ``app.app_context()`` blocks.

    Folding runs to quiescence: a fold can emit follow-on postings (a trusted
    user's escrow drains via an applier-emitted delta pair), so this iterates
    cycles until one does no work, up to a bounded cap.
    """
    from horde.database.kudos_ledger import apply_pending_kudos

    def _settle() -> int:
        with app.app_context():
            total = 0
            for _ in range(10):
                folded = apply_pending_kudos()
                total += folded
                if folded == 0:
                    break
            return total

    return _settle


@pytest.fixture
def log_records() -> Iterator[list[tuple[str, str]]]:
    """Capture every log record emitted during a test as its level name and message.

    Yields:
        The captured records, in the order they were logged.
    """
    from horde.logger import logger

    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    yield records
    logger.remove(handler_id)


@pytest.fixture
def request_headers(api_key: str, CIVERSION: str) -> dict[str, str]:
    return {
        "apikey": api_key,
        "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625",
    }


@pytest.fixture
def make_api_user(app: Flask) -> MakeApiUser:
    """Factory: create a registered user and return its id/api_key/username/alias.

    Used by endpoint tests that need actors at specific privilege levels (a
    non-moderator owner, a second user to receive kudos, etc.) distinct from the
    moderator+trusted ``api_key`` fixture user.
    """
    import uuid

    from horde.classes.base.user import User
    from horde.utils import generate_api_key, hash_api_key

    def _make(*, trusted: bool = False, moderator: bool = False, customizer: bool = False, kudos: int = 0) -> ApiUser:
        suffix = uuid.uuid4().hex[:8]
        username = f"user_{suffix}"
        raw_api_key = generate_api_key()
        with app.app_context():
            user = User(
                username=username,
                oauth_id=f"oauth_{suffix}",
                api_key=hash_api_key(raw_api_key),
            )
            user.create()
            if moderator:
                user.set_moderator(True)
            if trusted:
                user.set_trusted(True)
            if customizer:
                user.set_customizer(True)
            if kudos:
                user.modify_kudos(kudos, "admin")
            user.refresh_cache()
            return ApiUser(
                id=user.id,
                api_key=raw_api_key,
                username=username,
                alias=user.get_unique_alias(),
            )

    return _make


@pytest.fixture
def api_key(app: Flask) -> str:
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


@pytest.fixture(scope="session", autouse=True)
def _live_image_reference() -> None:
    """Load the live image reference once for the whole end-to-end run.

    ``create_app(TESTING=True)`` skips the Redis bootstrap, so the direct loader fills the
    process-wide reference these tests advertise and request models from.
    """
    from horde.model_reference import model_reference

    if not model_reference.reference:
        model_reference.call_function()


@pytest.fixture
def messages_offload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the messages offload, uncompressed, with a threshold small enough for this module's requests."""
    from horde.config import Config

    monkeypatch.setattr(Config, "chat_messages_offload_enabled", True)
    monkeypatch.setattr(Config, "chat_messages_offload_bytes", OFFLOAD_THRESHOLD_BYTES)
    monkeypatch.setattr(Config, "chat_messages_offload_gzip", False)


@pytest.fixture
def progress_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeStrictRedis:
    """Route the stream lease, the delta stream and the chat signals to one in-memory Redis."""
    import fakeredis

    from horde import chat_progress, chat_signal

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(chat_progress, "_client", fake)
    monkeypatch.setattr(chat_progress, "_append_script", None)
    monkeypatch.setattr(chat_signal, "_publisher", fake)
    return fake


@pytest.fixture
def lua_redis(progress_redis: fakeredis.FakeStrictRedis) -> fakeredis.FakeStrictRedis:
    """The in-memory Redis, for tests that reach the append script. fakeredis runs Lua only with ``lupa``."""
    pytest.importorskip("lupa", reason="fakeredis runs Lua scripts only with lupa installed (fakeredis[lua]).")
    return progress_redis


@pytest.fixture(scope="module")
def signal_redis_url() -> Iterator[str]:
    """Route the Horde's chat signals to the test Redis and return the URL used by the front to subscribe."""
    import redis

    from horde import chat_signal

    redis_host = os.environ.get("REDIS_IP", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    if not redis_reachable(redis_host, redis_port):
        message = f"The SDK conformance run needs the test Redis at {redis_host}:{redis_port} for chat signals."
        if os.environ.get(REQUIRE_TEST_DEPS_ENV) == "1":
            pytest.fail(message)
        pytest.skip(message)
    publisher = redis.Redis(host=redis_host, port=redis_port, db=CHAT_SIGNAL_REDIS_DB, decode_responses=True)
    previous_publisher = chat_signal._publisher
    chat_signal._publisher = publisher
    try:
        yield f"redis://{redis_host}:{redis_port}/{CHAT_SIGNAL_REDIS_DB}"
    finally:
        chat_signal._publisher = previous_publisher
        publisher.close()


@pytest.fixture(scope="module")
def owner_api_key(app: Flask) -> str:
    """Create the trusted user that owns the fake worker and sends the SDK requests, and return its API key."""
    from horde.classes.base.user import User
    from horde.utils import generate_api_key, hash_api_key

    suffix = uuid.uuid4().hex[:8]
    raw_api_key: str = generate_api_key()  # type: ignore[no-untyped-call]
    with app.app_context():
        user = User(username=f"sdk_owner_{suffix}", oauth_id=f"sdk_owner_{suffix}", api_key=hash_api_key(raw_api_key))  # type: ignore[no-untyped-call]
        user.create()  # type: ignore[no-untyped-call]
        user.set_moderator(True)  # type: ignore[no-untyped-call]
        user.modify_kudos(10000, "admin")
        user.refresh_cache()  # type: ignore[no-untyped-call]
    return raw_api_key


@pytest.fixture(scope="module")
def horde_api_root(app: Flask) -> Iterator[str]:
    """Serve the native Flask app over HTTP on an ephemeral port and return its API root."""
    from werkzeug.serving import make_server

    native_server = make_server(LOOPBACK_HOST, 0, app, threaded=True)
    server_thread = threading.Thread(target=native_server.serve_forever, name="sdk-conformance-horde", daemon=True)
    server_thread.start()
    try:
        yield f"http://{LOOPBACK_HOST}:{native_server.server_port}/api"
    finally:
        native_server.shutdown()
        server_thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        native_server.server_close()


@pytest.fixture(scope="module")
def fixed_reply_worker(app: Flask, owner_api_key: str, CIVERSION: str) -> Iterator[FixedReplyWorker]:
    """Run the fake worker for the module, registered with the Horde and its model list before the front starts.

    The native model list reads a cache that a maintenance thread refreshes in production. The fixture refreshes it
    once after the first pop, since the test app does not run maintenance threads.
    """
    from horde.database.threads import store_available_models

    worker = FixedReplyWorker(
        app=app,
        headers={"apikey": owner_api_key, "Client-Agent": f"aihorde_ci_sdk_worker:{CIVERSION}:(discord)db0#1625"},
    )
    worker.start()
    store_available_models()  # type: ignore[no-untyped-call]
    try:
        yield worker
    finally:
        worker.stop()


@pytest.fixture(scope="module")
def front_base_url(horde_api_root: str, signal_redis_url: str, fixed_reply_worker: FixedReplyWorker) -> Iterator[str]:
    """Serve the chat front with uvicorn on an ephemeral port and return its OpenAI base URL."""
    import uvicorn

    from horde.oai.app import create_app as create_front_app
    from horde.oai.settings import FrontSettings

    settings = FrontSettings(
        horde_urls=(horde_api_root,),
        listen=LOOPBACK_HOST,
        redis_url=signal_redis_url,
        nonstream_timeout_seconds=REQUEST_BUDGET_SECONDS,
        stream_timeout_seconds=REQUEST_BUDGET_SECONDS,
        fallback_poll_seconds=FALLBACK_POLL_SECONDS,
        fail_fast_grace_seconds=FAIL_FAST_GRACE_SECONDS,
        shutdown_drain_seconds=5.0,
    )
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_socket.bind((LOOPBACK_HOST, 0))
    front_port = listening_socket.getsockname()[1]
    front_server = uvicorn.Server(
        uvicorn.Config(
            create_front_app(settings),
            lifespan="on",
            log_level="warning",
            timeout_graceful_shutdown=5,
        ),
    )
    server_thread = threading.Thread(
        target=front_server.run,
        kwargs={"sockets": [listening_socket]},
        name="sdk-conformance-front",
        daemon=True,
    )
    server_thread.start()
    started_by = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    while not front_server.started:
        assert server_thread.is_alive(), "The front's uvicorn thread exited before it started serving."
        assert time.monotonic() < started_by, "The front did not start serving in time."
        time.sleep(0.05)
    try:
        yield f"http://{LOOPBACK_HOST}:{front_port}/v1"
    finally:
        front_server.should_exit = True
        server_thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        listening_socket.close()
