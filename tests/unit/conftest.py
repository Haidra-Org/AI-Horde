# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit-test fixtures.

Unit tests run against the shared pytest dependency runtime: auto mode prefers
caller-provided local services and otherwise provisions Postgres with
testcontainers. Redis-touching unit tests still use in-process fakeredis.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy import event

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name


@pytest.fixture(scope="session")
def _pg_dsn(pg_dsn: str) -> str:
    return pg_dsn


@pytest.fixture(scope="session")
def _pg_schema(_pg_dsn: str) -> Iterator[str]:
    """Create an isolated schema for this pytest session."""
    schema_name = new_test_schema_name("horde_unit_test")
    create_schema(_pg_dsn, schema_name)
    try:
        yield schema_name
    finally:
        drop_schema(_pg_dsn, schema_name)


@pytest.fixture(scope="session")
def app(_pg_dsn: str, _pg_schema: str):
    """Flask app pointed at test Postgres with an isolated schema."""
    from horde.flask import create_app, db

    app = create_app(
        config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": _pg_dsn,
            # Keep the per-test pool small so a misbehaving test surfaces
            # contention quickly rather than masking it.
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
            raise RuntimeError(f"Test fixture safety check failed: expected current schema {_pg_schema!r}, got {current_schema!r}.")

        db.create_all()
        try:
            yield app
        finally:
            db.session.remove()


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def db_session(app) -> Iterator[Any]:
    """Per-test ORM session. Truncates all tables after each test.

    Tests are free to call ``db.session.commit()``. The truncation runs
    unconditionally afterwards, so committed state does not leak between cases.
    Slightly more expensive than a SAVEPOINT-rollback fixture but resilient
    to commits triggered deep inside production code paths.
    """
    from horde.flask import db

    with app.app_context():
        try:
            yield db.session
        finally:
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace ``horde.horde_redis.horde_redis`` connections with fakeredis.

    Yields the ``HordeRedis`` instance with its ``horde_r``, ``horde_local_r``,
    and ``all_horde_redis`` attributes patched. Use this for any test that
    exercises code reaching into ``horde_redis``.
    """
    fakeredis = pytest.importorskip("fakeredis")
    from horde import horde_redis as horde_redis_module

    fake = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(horde_redis_module.horde_redis, "horde_r", fake)
    monkeypatch.setattr(horde_redis_module.horde_redis, "horde_local_r", fake)
    monkeypatch.setattr(horde_redis_module.horde_redis, "all_horde_redis", [fake])
    yield horde_redis_module.horde_redis


@pytest.fixture
def frozen_time():
    """Yield ``freezegun.freeze_time`` so tests can pin or advance the clock."""
    freezegun = pytest.importorskip("freezegun")
    return freezegun.freeze_time


# --------------------------------------------------------------------------- #
# Query-count assertions                                                      #
# --------------------------------------------------------------------------- #


class _QueryRecorder:
    """Records SQL statements emitted within a context for later assertion."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __len__(self) -> int:
        return len(self.statements)

    def of_kind(self, prefix: str) -> list[str]:
        """Filter statements by leading SQL keyword (e.g. ``SELECT``)."""
        upper = prefix.upper()
        return [s for s in self.statements if s.lstrip().upper().startswith(upper)]


@contextlib.contextmanager
def _record_queries(engine) -> Iterator[_QueryRecorder]:
    recorder = _QueryRecorder()

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        recorder.statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        yield recorder
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)


@pytest.fixture
def assert_query_count(app):
    """Return a context manager + assertion helper for SQL-count regressions.

    Usage:
        with assert_query_count() as queries:
            user.trusted
            user.flagged
            user.moderator
        assert len(queries.of_kind("SELECT")) == 1
    """
    from horde.flask import db

    @contextlib.contextmanager
    def _cm() -> Iterator[_QueryRecorder]:
        with _record_queries(db.engine) as recorder:
            yield recorder

    return _cm


# --------------------------------------------------------------------------- #
# Object factories                                                            #
# --------------------------------------------------------------------------- #


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:12]


@pytest.fixture
def make_user(db_session):
    """Factory: build and persist a ``User`` with sensible defaults.

    Returns a callable. Any kwarg overrides a default field. The user is
    flushed (not committed) so foreign-key references resolve immediately.
    """
    from horde.classes.base.user import User

    def _make(**overrides: Any):
        suffix = _unique_suffix()
        defaults: dict[str, Any] = {
            "username": f"test_user_{suffix}",
            "oauth_id": f"oauth_{suffix}",
            "api_key": f"key_{suffix}",
        }
        defaults.update(overrides)
        user = User(**defaults)
        db_session.add(user)
        db_session.flush()
        return user

    return _make


@pytest.fixture
def make_user_role(db_session):
    """Factory: attach a ``UserRole`` to a user."""
    from horde.classes.base.user import UserRole

    def _make(user, role_type, *, value: bool = True):
        role = UserRole(user_id=user.id, user_role=role_type, value=value)
        db_session.add(role)
        db_session.flush()
        return role

    return _make
