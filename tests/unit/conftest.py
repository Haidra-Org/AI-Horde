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
import os
import re
import ssl
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pycurl
import pytest
import sqlalchemy
import trustme
from sqlalchemy import event

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name
from tests.fixture_types import MakeUser, MakeUserRole

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from flask_sqlalchemy.session import Session
    from sqlalchemy.orm import scoped_session

    from horde.classes.base.user import User, UserRole
    from horde.enums import UserRoleTypes


@pytest.fixture
def hook_tls_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ssl.SSLContext:
    """Trust a temporary CA for hook.example, with no external revocation service."""
    certificate_authority = trustme.CA()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certificate_authority.issue_cert("hook.example").configure_cert(server_context)
    ca_path = tmp_path / "hook-ca.pem"
    certificate_authority.cert_pem.write_to_path(str(ca_path))
    monkeypatch.setattr("horde.outbound.certifi.where", lambda: str(ca_path))
    # Schannel requires revocation data by default. The temporary CA does not have a CRL/OCSP endpoint.
    # This fixture permits unavailable revocation data while retaining certificate and hostname checks.
    real_curl = pycurl.Curl

    def test_curl() -> pycurl.Curl:
        curl = real_curl()
        curl.setopt(pycurl.SSL_OPTIONS, pycurl.SSLOPT_REVOKE_BEST_EFFORT)
        return curl

    monkeypatch.setattr(pycurl, "Curl", test_curl)
    return server_context


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
def app(_pg_dsn: str, _pg_schema: str) -> Iterator[Flask]:
    """Flask app pointed at test Postgres with an isolated schema."""
    from horde.classes.base.kudos import KudosPartitioning, ensure_kudos_event_partitions
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
        ensure_kudos_event_partitions(
            until=datetime.utcnow() + 2 * KudosPartitioning.DEFAULT_WIDTH,
            width=KudosPartitioning.DEFAULT_WIDTH,
        )
        db.session.commit()
        try:
            yield app
        finally:
            db.session.remove()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def db_session(app: Flask) -> Iterator[scoped_session[Session]]:
    """Per-test ORM session. Truncates all tables after each test.

    Tests are free to call ``db.session.commit()``. The truncation runs
    unconditionally afterwards, so committed state does not leak between cases.
    Slightly more expensive than a SAVEPOINT-rollback fixture but resilient
    to commits triggered deep inside production code paths.
    """
    from horde.classes.base.kudos import KudosLedgerControl
    from horde.enums import KudosLedgerMode
    from horde.flask import db

    with app.app_context():
        try:
            db.session.add(KudosLedgerControl(id=1, mode=str(KudosLedgerMode.LEDGER)))
            db.session.commit()
            yield db.session
        finally:
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()


@pytest.fixture
def settle_kudos(db_session: scoped_session[Session]) -> Callable[[], int]:
    """Return a helper that folds all pending kudos ledger rows immediately.

    Kudos balances are applier-maintained: a movement is recorded as a ledger
    posting and materialized into ``users.kudos``/``users.evaluating_kudos``/
    ``workers.kudos`` by the applier. Call the returned helper before asserting a
    balance so the assertion observes the folded result. Amounts and routing are
    unchanged; only the observation point is.

    Folding runs to quiescence: a fold can emit follow-on postings (a trusted
    user's escrow drains via an applier-emitted delta pair), so this iterates
    cycles until one does no work, up to a bounded cap.
    """
    from horde.database.kudos_ledger import apply_pending_kudos

    def _settle() -> int:
        total = 0
        for _ in range(10):
            folded = apply_pending_kudos()
            total += folded
            if folded == 0:
                break
        return total

    return _settle


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


_REPOSITORY_ROOT: str = os.path.normcase(str(Path(__file__).resolve().parents[2]))
"""The checkout root; recorded origins are paths relative to it."""

_APPLICATION_SOURCE_PREFIX: str = os.path.join(_REPOSITORY_ROOT, "horde") + os.sep
"""Frames in files under this prefix are application code and are kept as a statement's origin."""

_ORIGIN_FRAME_DEPTH: int = 4
"""How many application frames each origin keeps, innermost first.

Four frames reach from a shared lookup helper back to the endpoint method that called it.
"""

_WRITE_KINDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
    },
)
"""Statement kinds that change rows; a commit without any of them wrote nothing."""

_FIRST_TABLE_PATTERN: re.Pattern[str] = re.compile(r'\b(?:FROM|INTO|UPDATE)\s+"?([A-Za-z_][A-Za-z0-9_]*)', re.IGNORECASE)
"""The first table after FROM, INTO or UPDATE.

For a SELECT with a scalar subquery in its column list this is the subquery's table, which is close enough
for a per-table breakdown.
"""


class _TransactionOutcome(StrEnum):
    """How a recorded database transaction ended."""

    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"


@dataclass(frozen=True)
class _RecordedStatement:
    """Represents one SQL statement sent to the database and the application frames that issued it."""

    sql: str
    """The statement text as sent to the driver."""
    origin: tuple[str, ...]
    """Application frames active when the statement ran, innermost first, as ``path:line function``."""

    @property
    def kind(self) -> str:
        """Return the statement's leading SQL keyword in upper case, such as ``SELECT``."""
        words = self.sql.split(maxsplit=1)
        return words[0].upper() if words else ""

    @property
    def table(self) -> str | None:
        """Return the first table named after FROM, INTO or UPDATE, or ``None`` when there is none."""
        match = _FIRST_TABLE_PATTERN.search(self.sql)
        return match.group(1).lower() if match else None


@dataclass
class _RecordedTransaction:
    """Represents one database transaction on one connection, from its begin to its commit or rollback."""

    begin_origin: tuple[str, ...]
    """Application frames active when the transaction began."""
    statement_kinds: list[str] = field(default_factory=list)
    """The kind of each statement run inside the transaction, in order."""
    outcome: _TransactionOutcome | None = None
    """How the transaction ended, or ``None`` while it is still open."""
    end_origin: tuple[str, ...] = ()
    """Application frames active when the transaction ended."""

    @property
    def wrote(self) -> bool:
        """Return whether the transaction ran an INSERT, UPDATE or DELETE."""
        return any(kind in _WRITE_KINDS for kind in self.statement_kinds)


def _application_origin() -> tuple[str, ...]:
    """Return up to ``_ORIGIN_FRAME_DEPTH`` application frames of the current call stack, innermost first."""
    origin: list[str] = []
    frame = sys._getframe(1)
    while frame is not None and len(origin) < _ORIGIN_FRAME_DEPTH:
        filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if filename.startswith(_APPLICATION_SOURCE_PREFIX):
            relative_path = os.path.relpath(filename, _REPOSITORY_ROOT).replace(os.sep, "/")
            origin.append(f"{relative_path}:{frame.f_lineno} {frame.f_code.co_name}")
        frame = frame.f_back
    return tuple(origin)


def _format_origin(origin: tuple[str, ...]) -> str:
    """Return an origin as one line, innermost frame first, or a marker when no application frame was active."""
    return " <- ".join(origin) if origin else "(outside horde)"


class _QueryRecorder:
    """Records SQL statements and database transactions emitted within a context for later assertion.

    Transactions are counted from the engine's connection events, so a session commit or rollback
    that did not open a database transaction is not counted. Closing a session with an open
    transaction ends that transaction with a rollback.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        """Statement text in execution order."""
        self.recorded_statements: list[_RecordedStatement] = []
        """Statements in execution order with the application frames that issued them."""
        self.transactions: list[_RecordedTransaction] = []
        """Database transactions in the order they began."""

    def __len__(self) -> int:
        return len(self.statements)

    def of_kind(self, prefix: str) -> list[str]:
        """Filter statements by leading SQL keyword (e.g. ``SELECT``)."""
        upper = prefix.upper()
        return [s for s in self.statements if s.lstrip().upper().startswith(upper)]

    def kind_counts(self) -> Counter[str]:
        """Return the number of statements of each leading SQL keyword."""
        return Counter(recorded.kind for recorded in self.recorded_statements)

    def table_counts(self, kind: str) -> Counter[str]:
        """Return the number of statements of ``kind`` against each table.

        Args:
            kind: A leading SQL keyword such as ``SELECT``.

        Returns:
            The count for each first table named in the statements of that kind.
        """
        upper = kind.upper()
        return Counter(recorded.table or "(no table)" for recorded in self.recorded_statements if recorded.kind == upper)

    def ended_with(self, outcome: _TransactionOutcome) -> list[_RecordedTransaction]:
        """Return the transactions that ended with ``outcome``, in the order they began."""
        return [transaction for transaction in self.transactions if transaction.outcome == outcome]

    @property
    def commits(self) -> list[_RecordedTransaction]:
        """Return the committed transactions."""
        return self.ended_with(_TransactionOutcome.COMMIT)

    @property
    def rollbacks(self) -> list[_RecordedTransaction]:
        """Return the transactions that were rolled back, including read-only ones closed with the session."""
        return self.ended_with(_TransactionOutcome.ROLLBACK)

    @property
    def commits_without_writes(self) -> list[_RecordedTransaction]:
        """Return the committed transactions that did not run an INSERT, UPDATE or DELETE."""
        return [transaction for transaction in self.commits if not transaction.wrote]

    def describe(self) -> str:
        """Return a multi-line summary of the recorded work for assertion messages and reports."""
        kinds = ", ".join(f"{kind} {count}" for kind, count in self.kind_counts().most_common())
        selects = ", ".join(f"{table} {count}" for table, count in self.table_counts("SELECT").most_common())
        lines = [
            f"{len(self)} statements: {kinds}",
            f"{len(self.transactions)} transactions: {len(self.commits)} commits "
            f"({len(self.commits_without_writes)} without writes), {len(self.rollbacks)} rollbacks",
            f"SELECT by table: {selects}",
            "statements:",
        ]
        lines.extend(
            f"  {recorded.kind} {recorded.table or ''} at {_format_origin(recorded.origin)}" for recorded in self.recorded_statements
        )
        lines.append("transactions:")
        lines.extend(
            f"  {transaction.outcome or 'OPEN'} [{' '.join(transaction.statement_kinds)}] "
            f"began at {_format_origin(transaction.begin_origin)}; ended at {_format_origin(transaction.end_origin)}"
            for transaction in self.transactions
        )
        return "\n".join(lines)


@contextlib.contextmanager
def _record_queries(engine) -> Iterator[_QueryRecorder]:
    recorder = _QueryRecorder()
    # Keyed by connection so work on a second connection, such as an independent session,
    # is attributed to its own transaction.
    open_transactions: dict[int, _RecordedTransaction] = {}

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        recorded = _RecordedStatement(sql=statement, origin=_application_origin())
        recorder.statements.append(statement)
        recorder.recorded_statements.append(recorded)
        transaction = open_transactions.get(id(conn))
        if transaction is not None:
            transaction.statement_kinds.append(recorded.kind)

    def _on_begin(conn) -> None:
        transaction = _RecordedTransaction(begin_origin=_application_origin())
        open_transactions[id(conn)] = transaction
        recorder.transactions.append(transaction)

    def _end(conn, outcome: _TransactionOutcome) -> None:
        transaction = open_transactions.pop(id(conn), None)
        if transaction is None:
            # The transaction began before recording started; record its end on its own.
            transaction = _RecordedTransaction(begin_origin=())
            recorder.transactions.append(transaction)
        transaction.outcome = outcome
        transaction.end_origin = _application_origin()

    def _on_commit(conn) -> None:
        _end(conn, _TransactionOutcome.COMMIT)

    def _on_rollback(conn) -> None:
        _end(conn, _TransactionOutcome.ROLLBACK)

    listeners = (
        ("before_cursor_execute", _on_execute),
        ("begin", _on_begin),
        ("commit", _on_commit),
        ("rollback", _on_rollback),
    )
    for event_name, listener in listeners:
        event.listen(engine, event_name, listener)
    try:
        yield recorder
    finally:
        for event_name, listener in listeners:
            event.remove(engine, event_name, listener)


@pytest.fixture
def assert_query_count(app):
    """Return a context manager + assertion helper for SQL-count regressions.

    The recorder also counts database transactions and keeps the application frames behind each
    statement and transaction; ``queries.describe()`` lists them for an assertion message.

    Usage:
        with assert_query_count() as queries:
            user.trusted
            user.flagged
            user.moderator
        assert len(queries.of_kind("SELECT")) == 1
        assert len(queries.commits) == 0, queries.describe()
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
def make_user(db_session: scoped_session[Session]) -> MakeUser:
    """Factory: build and persist a ``User`` with sensible defaults.

    Returns a callable. Any kwarg overrides a default field. The user is
    flushed (not committed) so foreign-key references resolve immediately.
    """
    from horde.classes.base.user import User

    def _make(**overrides: Any) -> User:
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
def make_user_role(db_session: scoped_session[Session]) -> MakeUserRole:
    """Factory: attach a ``UserRole`` to a user."""
    from horde.classes.base.user import UserRole

    def _make(user: User, role_type: UserRoleTypes, *, value: bool = True) -> UserRole:
        role = UserRole(user_id=user.id, user_role=role_type, value=value)
        db_session.add(role)
        db_session.flush()
        return role

    return _make
