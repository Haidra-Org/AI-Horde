# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pytest
from sqlalchemy import event

from tests.dependency_runtime import (
    AUTO_DEPS_MODE,
    DEPS_MODE_ENV,
    EXTERNAL_DEPS_MODE,
    REQUIRE_TEST_DEPS_ENV,
    HordeTestRuntime,
    assert_safe_test_target,
    build_test_runtime,
    normalize_deps_mode,
    selected_object_store_tests,
)

# --------------------------------------------------------------------------- #
# Query-count assertions                                                      #
# --------------------------------------------------------------------------- #


_REPOSITORY_ROOT: str = os.path.normcase(str(Path(__file__).resolve().parents[1]))
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




def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--test-deps",
        dest="test_deps",
        action="store",
        choices=(AUTO_DEPS_MODE, EXTERNAL_DEPS_MODE),
        default=normalize_deps_mode(os.environ.get(DEPS_MODE_ENV, AUTO_DEPS_MODE)),
        help=(
            "How tests obtain Postgres/object-storage dependencies: "
            "'auto' prefers reachable external services and otherwise provisions testcontainers; "
            "'external' never provisions containers and only uses caller-provided services. "
        ),
    )


@pytest.fixture(scope="session")
def CIVERSION() -> str:
    return "0.1.1"


@pytest.fixture(scope="session")
def horde_test_runtime(request: pytest.FixtureRequest) -> Iterator[HordeTestRuntime]:
    runtime = build_test_runtime(
        deps_mode=request.config.getoption("test_deps"),
        needs_object_store=selected_object_store_tests(request),
    )

    try:
        yield runtime
    finally:
        runtime.close()


def _skip_or_fail_missing_dependency(message: str) -> None:
    if os.environ.get(REQUIRE_TEST_DEPS_ENV) == "1":
        pytest.fail(message)

    pytest.skip(message, allow_module_level=False)


@pytest.fixture(scope="session")
def pg_dsn(horde_test_runtime: HordeTestRuntime) -> str:
    dsn = horde_test_runtime.postgres_dsn
    if dsn is None:
        _skip_or_fail_missing_dependency(
            horde_test_runtime.postgres_skip_reason or "Tests require a reachable Postgres backend.",
        )

    if not horde_test_runtime.postgres_is_managed:
        assert_safe_test_target(dsn)

    return dsn


@pytest.fixture(scope="session")
def object_store_ready(horde_test_runtime: HordeTestRuntime) -> None:
    if horde_test_runtime.object_store_available:
        return

    _skip_or_fail_missing_dependency(
        horde_test_runtime.object_store_skip_reason
        or "Object-storage integration tests require a managed or external S3-compatible backend.",
    )
