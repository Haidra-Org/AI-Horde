# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the kudos retention migration converts the production event tables and matches the ORM models."""

from __future__ import annotations

import re
import threading
import time as clock
from collections.abc import Callable, Iterator, Mapping
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
import sqlparse

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "sql_statements" / "5.1.16.txt"
"""The hand-applied migration under test."""
GEXEC_LINE = re.compile(r"^\\gexec[ \t]*$", re.MULTILINE)
"""A psql ``\\gexec`` line, which ends the query whose result rows are run as statements."""
PSQL_VARIABLE_DEFINED_TEST = re.compile(r"^\\if :\{\?(\w+)\}$")
"""A psql ``\\if :{?name}`` line, which tests whether the variable is set."""
PSQL_SET_COMMAND = re.compile(r"^\\set (\w+) (\S+)$")
"""A psql ``\\set name value`` line."""
USERS_STUB = "CREATE TABLE users (id INTEGER PRIMARY KEY)"
"""The key ``kudos_ledger.user_id`` and the new statement tables reference."""
KUDOS_LEDGER_STUB = """
CREATE TABLE kudos_ledger (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created TIMESTAMP NOT NULL DEFAULT now(),
    event_id UUID NOT NULL,
    entry_type VARCHAR(32) NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    escrow BOOLEAN NOT NULL DEFAULT FALSE,
    amount NUMERIC(20, 2) NOT NULL CHECK (amount <> 'NaN'),
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    job_id UUID NULL,
    wp_type VARCHAR(20) NULL,
    detail JSONB NULL
)
"""
"""``kudos_ledger`` as sql_statements/5.1.0.txt builds it."""
KUDOS_LEDGER_INDEXES = (
    "CREATE INDEX ix_kudos_ledger_created ON kudos_ledger (created)",
    "CREATE INDEX ix_kudos_ledger_user_id ON kudos_ledger (user_id)",
    "CREATE INDEX ix_kudos_ledger_unapplied ON kudos_ledger (id) WHERE NOT applied",
    "CREATE INDEX ix_kudos_ledger_unapplied_users ON kudos_ledger (entry_type, user_id) WHERE applied IS FALSE",
    "CREATE INDEX ix_kudos_ledger_unapplied_by_user ON kudos_ledger (user_id) WHERE applied IS FALSE",
)
"""The production indexes of ``kudos_ledger``, from 5.1.0, 5.1.10 and 5.1.11."""
KUDOS_STAT_EVENTS_STUB = """
CREATE TABLE kudos_stat_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created TIMESTAMP NOT NULL DEFAULT now(),
    event_id UUID NOT NULL,
    entry_type VARCHAR(32) NOT NULL,
    user_id INTEGER NULL,
    worker_user_id INTEGER NULL,
    worker_id UUID NULL,
    team_id UUID NULL,
    job_id UUID NULL,
    wp_type VARCHAR(20) NULL,
    amount NUMERIC(20, 2) NOT NULL CHECK (amount <> 'NaN'),
    unit VARCHAR(16) NOT NULL,
    stat_action VARCHAR(32) NULL,
    record VARCHAR(32) NULL,
    detail JSONB NULL,
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    quarantined BOOLEAN NOT NULL DEFAULT FALSE,
    quarantine_reason VARCHAR(64) NULL,
    quarantined_at TIMESTAMP WITHOUT TIME ZONE NULL,
    CONSTRAINT kudos_stat_event_exactly_one_target CHECK (
        (user_id IS NOT NULL AND worker_id IS NULL)
        OR (user_id IS NULL AND worker_id IS NOT NULL)
    )
)
"""
"""``kudos_stat_events`` as sql_statements/5.1.0.txt and 5.1.9.txt build it."""
KUDOS_STAT_EVENTS_INDEXES = (
    "CREATE INDEX ix_kudos_stat_events_drainable ON kudos_stat_events (id) WHERE applied IS FALSE AND quarantined IS FALSE",
    "CREATE INDEX ix_kudos_stat_events_quarantined ON kudos_stat_events (created, quarantined_at) WHERE quarantined IS TRUE",
    "CREATE INDEX ix_kudos_stat_events_event_id ON kudos_stat_events (event_id)",
    "CREATE INDEX ix_kudos_stat_events_user_id ON kudos_stat_events (user_id) WHERE user_id IS NOT NULL",
    "CREATE INDEX ix_kudos_stat_events_worker_id ON kudos_stat_events (worker_id) WHERE worker_id IS NOT NULL",
    "CREATE INDEX ix_kudos_stat_events_team_id ON kudos_stat_events (team_id) WHERE team_id IS NOT NULL",
)
"""The production indexes of ``kudos_stat_events``, from 5.1.0, 5.1.9 and 5.1.10."""
CREATE_ALL_KUDOS_LEDGER_STUB = """
CREATE TABLE kudos_ledger (
    id BIGSERIAL NOT NULL,
    created TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    event_id UUID NOT NULL,
    entry_type VARCHAR(20) NOT NULL,
    user_id INTEGER NOT NULL,
    escrow BOOLEAN NOT NULL,
    amount NUMERIC(20, 2) NOT NULL,
    applied BOOLEAN NOT NULL,
    job_id UUID,
    wp_type VARCHAR(20),
    detail JSONB,
    PRIMARY KEY (id),
    CONSTRAINT kudos_ledger_amount_not_nan CHECK (amount <> 'NaN'),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE RESTRICT
)
"""
"""``kudos_ledger`` as db.create_all() built it from the models before partitioning, which is how dev was built.

The current models describe the partitioned table, so their metadata cannot build this shape and it is written out.
"""
CREATE_ALL_KUDOS_LEDGER_INDEXES = (
    "CREATE INDEX ix_kudos_ledger_created ON kudos_ledger (created)",
    "CREATE INDEX ix_kudos_ledger_event_id ON kudos_ledger (event_id)",
    "CREATE INDEX ix_kudos_ledger_user_id ON kudos_ledger (user_id)",
    "CREATE INDEX ix_kudos_ledger_unapplied ON kudos_ledger (id) WHERE applied IS false",
    "CREATE INDEX ix_kudos_ledger_unapplied_users ON kudos_ledger (entry_type, user_id) WHERE applied IS false",
    "CREATE INDEX ix_kudos_ledger_unapplied_by_user ON kudos_ledger (user_id) WHERE applied IS false",
)
"""The indexes db.create_all() built on ``kudos_ledger`` before partitioning."""
CREATE_ALL_KUDOS_STAT_EVENTS_STUB = """
CREATE TABLE kudos_stat_events (
    id BIGSERIAL NOT NULL,
    created TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    event_id UUID NOT NULL,
    entry_type VARCHAR(20) NOT NULL,
    user_id INTEGER,
    worker_user_id INTEGER,
    worker_id UUID,
    team_id UUID,
    job_id UUID,
    wp_type VARCHAR(20),
    amount NUMERIC(20, 2) NOT NULL,
    unit VARCHAR(6) NOT NULL,
    stat_action VARCHAR(32),
    record VARCHAR(32),
    detail JSONB,
    applied BOOLEAN NOT NULL,
    quarantined BOOLEAN NOT NULL,
    quarantine_reason VARCHAR(64),
    quarantined_at TIMESTAMP WITHOUT TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT kudos_stat_event_exactly_one_target CHECK (
        (user_id IS NOT NULL AND worker_id IS NULL) OR (user_id IS NULL AND worker_id IS NOT NULL)
    ),
    CONSTRAINT kudos_stat_event_amount_not_nan CHECK (amount <> 'NaN')
)
"""
"""``kudos_stat_events`` as db.create_all() built it from the models before partitioning."""
CREATE_ALL_KUDOS_STAT_EVENTS_INDEXES = (
    "CREATE INDEX ix_kudos_stat_events_drainable ON kudos_stat_events (id) WHERE applied IS false AND quarantined IS false",
    "CREATE INDEX ix_kudos_stat_events_quarantined ON kudos_stat_events (created, quarantined_at) WHERE quarantined IS true",
    "CREATE INDEX ix_kudos_stat_events_event_id ON kudos_stat_events (event_id)",
    "CREATE INDEX ix_kudos_stat_events_user_id ON kudos_stat_events (user_id)",
    "CREATE INDEX ix_kudos_stat_events_worker_id ON kudos_stat_events (worker_id)",
    "CREATE INDEX ix_kudos_stat_events_team_id ON kudos_stat_events (team_id)",
)
"""The indexes db.create_all() built on ``kudos_stat_events`` before partitioning."""
TABLE_SHAPES = {
    "production": (USERS_STUB, KUDOS_LEDGER_STUB, *KUDOS_LEDGER_INDEXES, KUDOS_STAT_EVENTS_STUB, *KUDOS_STAT_EVENTS_INDEXES),
    "create_all": (
        USERS_STUB,
        CREATE_ALL_KUDOS_LEDGER_STUB,
        *CREATE_ALL_KUDOS_LEDGER_INDEXES,
        CREATE_ALL_KUDOS_STAT_EVENTS_STUB,
        *CREATE_ALL_KUDOS_STAT_EVENTS_INDEXES,
    ),
}
"""The DDL of each pre-partitioning shape the migration converts: the 5.1.0 shape and the create_all shape."""
PARENT_INDEXES = {
    "kudos_ledger": (
        "ix_kudos_ledger_unapplied",
        "ix_kudos_ledger_unapplied_users",
        "ix_kudos_ledger_unapplied_by_user",
        "ix_kudos_ledger_user_id",
    ),
    "kudos_stat_events": (
        "ix_kudos_stat_events_drainable",
        "ix_kudos_stat_events_quarantined",
        "ix_kudos_stat_events_event_id",
        "ix_kudos_stat_events_user_id",
        "ix_kudos_stat_events_worker_id",
        "ix_kudos_stat_events_team_id",
    ),
}
"""The partitioned indexes of each parent besides its primary key."""
SEED_ROWS = (
    "INSERT INTO users (id) VALUES (1), (2)",
    # A transfer inside the key window, one outside it, and a repair that posts to both balances.
    "INSERT INTO kudos_ledger (created, event_id, entry_type, user_id, escrow, amount, applied) VALUES "
    "((now() AT TIME ZONE 'UTC') - interval '1 day', '00000000-0000-0000-0000-00000000000a', 'TRANSFER', 1, false, -10, true), "
    "((now() AT TIME ZONE 'UTC') - interval '1 day', '00000000-0000-0000-0000-00000000000a', 'TRANSFER', 2, false, 10, true), "
    "((now() AT TIME ZONE 'UTC') - interval '40 days', '00000000-0000-0000-0000-00000000000b', 'TRANSFER', 1, false, -3, true), "
    "((now() AT TIME ZONE 'UTC') - interval '40 days', '00000000-0000-0000-0000-00000000000b', 'TRANSFER', 2, false, 3, true), "
    "((now() AT TIME ZONE 'UTC') - interval '2 days', '00000000-0000-0000-0000-00000000000c', 'RECONCILIATION', 1, false, 5, true), "
    "((now() AT TIME ZONE 'UTC') - interval '2 days', '00000000-0000-0000-0000-00000000000c', 'RECONCILIATION', 1, true, 2, true), "
    "((now() AT TIME ZONE 'UTC') - interval '1 hour', '00000000-0000-0000-0000-00000000000d', 'AWARD', 2, false, 7, false)",
    "INSERT INTO kudos_stat_events (created, event_id, entry_type, user_id, amount, unit, applied, quarantined) VALUES "
    "(now() AT TIME ZONE 'UTC', gen_random_uuid(), 'STAT_RECORD', 1, 1, 'count', true, false), "
    "(now() AT TIME ZONE 'UTC', gen_random_uuid(), 'STAT_RECORD', 1, 1, 'count', false, false), "
    "(now() AT TIME ZONE 'UTC', gen_random_uuid(), 'STAT_RECORD', 2, 1, 'count', false, true)",
)
"""Rows that exist before the migration: applied, unapplied and quarantined events, and keyed postings."""
EVENT_TABLES = ("kudos_ledger", "kudos_stat_events")
"""The tables the migration partitions."""
NEW_TABLES = (
    "kudos_snapshot_invalidations",
    "kudos_stat_rollups",
    "kudos_stat_quarantine",
    "kudos_ledger_checkpoints",
    "kudos_ledger_daily",
    "kudos_idempotency_keys",
    "kudos_retention_catalog",
)
"""The tables the migration creates."""
ABSENT = "<absent>"
"""The value reported for an item one schema has and the other lacks."""


def _schema_engine(pg_dsn: str, schema_name: str) -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(
        pg_dsn,
        isolation_level="AUTOCOMMIT",
        connect_args={"options": f"-c search_path={schema_name}"},
    )


def _apply_migration(
    connection: sqlalchemy.Connection,
    *,
    variables: Mapping[str, str] | None = None,
    before_statement: Callable[[str], None] | None = None,
) -> list[str]:
    """Run the migration as psql does, including its ``\\gexec`` statements, and return its ``\\echo`` lines.

    psql sends the query that ends at a ``\\gexec`` line and then runs every value it returns as a statement of its
    own, outside any transaction block. ``variables`` are the ``-v name=value`` options of the psql command line.
    ``before_statement`` is called with each statement before it is sent, so a test can act at a point of the file.
    """
    script, echoed_lines = _expand_psql_variables(MIGRATION_PATH.read_text(), dict(variables or {}))
    segments = GEXEC_LINE.split(script)
    for segment_index, segment in enumerate(segments):
        statements = [statement for statement in sqlparse.split(segment) if sqlparse.format(statement, strip_comments=True).strip()]
        generating_query = statements.pop() if segment_index < len(segments) - 1 else None
        for statement in statements:
            if before_statement is not None:
                before_statement(statement)
            connection.execute(sqlalchemy.text(statement))
        if generating_query is None:
            continue
        for generated_row in connection.execute(sqlalchemy.text(generating_query)).all():
            for generated_statement in generated_row:
                if before_statement is not None:
                    before_statement(generated_statement)
                connection.execute(sqlalchemy.text(generated_statement))
    return echoed_lines


def _expand_psql_variables(script: str, variables: dict[str, str]) -> tuple[str, list[str]]:
    """Apply the psql ``\\if :{?name}``, ``\\else``, ``\\endif``, ``\\set`` and ``\\echo`` lines of ``script``.

    Returns the script without those lines and with every ``:name`` of a set variable replaced by its value, and the
    ``\\echo`` output. Any other meta-command except ``\\gexec`` fails the test, so the migration cannot use one this
    emulation does not run as psql would.
    """

    def interpolate(line: str) -> str:
        for variable_name, variable_value in variables.items():
            line = re.sub(rf"(?<![:\w]):{variable_name}\b", variable_value, line)
        return line

    kept_lines: list[str] = []
    echoed_lines: list[str] = []
    branch_taken: list[bool] = []
    for line in script.splitlines():
        stripped = line.strip()
        defined_test = PSQL_VARIABLE_DEFINED_TEST.match(stripped)
        if defined_test is not None:
            branch_taken.append(defined_test.group(1) in variables)
            continue
        if stripped == "\\else":
            branch_taken[-1] = not branch_taken[-1]
            continue
        if stripped == "\\endif":
            branch_taken.pop()
            continue
        if not all(branch_taken):
            continue
        set_command = PSQL_SET_COMMAND.match(stripped)
        if set_command is not None:
            variables[set_command.group(1)] = set_command.group(2)
            continue
        if stripped.startswith("\\echo "):
            echoed_lines.append(interpolate(stripped.removeprefix("\\echo ")))
            continue
        assert not stripped.startswith("\\") or GEXEC_LINE.match(stripped), f"unsupported psql meta-command: {stripped}"
        kept_lines.append(line if stripped.startswith("--") else interpolate(line))
    assert not branch_taken, "unterminated \\if"
    return "\n".join(kept_lines) + "\n", echoed_lines


def _build_tables(connection: sqlalchemy.Connection, shape: str) -> None:
    for statement in TABLE_SHAPES[shape]:
        connection.execute(sqlalchemy.text(statement))


def _scalar(connection: sqlalchemy.Connection, query: str, **parameters: Any) -> Any:
    return connection.execute(sqlalchemy.text(query), parameters).scalar_one()


def _partition_bounds(connection: sqlalchemy.Connection, table: str) -> dict[str, str]:
    rows = connection.execute(
        sqlalchemy.text(
            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = CAST(:table AS regclass)",
        ),
        {"table": table},
    )
    return {name: bound for name, bound in rows}


def _weekly_partition_bounds(connection: sqlalchemy.Connection, table: str, swap_ts: Any) -> dict[str, str]:
    """Return the bound of each weekly partition the migration creates from swap_ts, by partition name.

    PostgreSQL renders a bound with trailing zeros of the fraction removed, so it renders the expected text too.
    """
    rows = connection.execute(
        sqlalchemy.text(
            "SELECT week_index, CAST(CAST(:swap_ts AS timestamp) + week_index * interval '7 days' AS text), "
            "CAST(CAST(:swap_ts AS timestamp) + (week_index + 1) * interval '7 days' AS text) "
            "FROM generate_series(0, :last_week_index) AS week_index",
        ),
        {"swap_ts": swap_ts, "last_week_index": MIGRATION_WEEK_COUNT - 1},
    )
    return {
        f"{table}_p_{swap_ts + timedelta(weeks=week_index):%Y%m%d}": f"FOR VALUES FROM ('{lower_text}') TO ('{upper_text}')"
        for week_index, lower_text, upper_text in rows
    }


@pytest.fixture(params=tuple(TABLE_SHAPES))
def converted_schema(request: pytest.FixtureRequest, pg_dsn: str) -> Iterator[sqlalchemy.Connection]:
    """Yield a connection to a throwaway schema holding one pre-partitioning shape after the migration ran twice.

    Every test using it runs for both shapes, so the create_all shape is held to the same post-conditions. The
    connection is the session the migration ran in, so its temporary ``kudos_swap_parameters`` is readable.
    """
    schema_name = new_test_schema_name("horde_kudos_retention_migration")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            _build_tables(connection, request.param)
            for statement in SEED_ROWS:
                connection.execute(sqlalchemy.text(statement))
            for _ in range(2):
                _apply_migration(connection)
            yield connection
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_conversion_ranges_the_table_on_created_around_the_swap_timestamp(converted_schema: sqlalchemy.Connection, table: str) -> None:
    connection = converted_schema
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST(:table AS regclass)", table=table) == "p"
    assert _scalar(connection, "SELECT pg_get_partkeydef(CAST(:table AS regclass))", table=table) == "RANGE (created)"
    swap_ts = _scalar(connection, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = :table", table=table)
    # PostgreSQL renders a bound with trailing zeros of the fraction removed, so it renders the expected text too.
    swap_text = _scalar(connection, "SELECT CAST(CAST(:swap_ts AS timestamp) AS text)", swap_ts=swap_ts)
    first_upper = _scalar(connection, "SELECT CAST(CAST(:swap_ts AS timestamp) + interval '7 days' AS text)", swap_ts=swap_ts)
    # A Monday 00:00 at least a day away leaves the validation and the swap a day or more before the CHECK bites.
    assert swap_ts.weekday() == 0 and swap_ts.time() == time(0)
    assert swap_ts - _scalar(connection, "SELECT now() AT TIME ZONE 'UTC'") >= timedelta(hours=23)
    assert swap_ts - _scalar(connection, "SELECT now() AT TIME ZONE 'UTC'") <= timedelta(days=8)
    weekly_bounds = _weekly_partition_bounds(connection, table, swap_ts)
    assert weekly_bounds[f"{table}_p_{swap_ts:%Y%m%d}"] == f"FOR VALUES FROM ('{swap_text}') TO ('{first_upper}')"
    assert _partition_bounds(connection, table) == {
        f"{table}_p_legacy": f"FOR VALUES FROM (MINVALUE) TO ('{swap_text}')",
        **weekly_bounds,
    }
    for relation in (table, f"{table}_p_legacy"):
        primary_key = _scalar(
            connection,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:relation AS regclass) AND contype = 'p'",
            relation=relation,
        )
        assert primary_key == "PRIMARY KEY (id, created)", relation
    # The scan-free attach relied on this CHECK. The parent must not keep it, or later partitions reject every row.
    checks = set(
        connection.execute(
            sqlalchemy.text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:table AS regclass) AND contype = 'c'"),
            {"table": table},
        ).scalars(),
    )
    assert f"{table}_legacy_range" not in checks
    amount_check = "kudos_ledger_amount_not_nan" if table == "kudos_ledger" else "kudos_stat_event_amount_not_nan"
    assert amount_check in checks


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_conversion_attaches_every_existing_index_to_a_partitioned_index(converted_schema: sqlalchemy.Connection, table: str) -> None:
    connection = converted_schema
    attached = dict(
        connection.execute(
            sqlalchemy.text(
                "SELECT parent.relname, child.relname FROM pg_index pi JOIN pg_class parent ON parent.oid = pi.indexrelid "
                "JOIN pg_inherits h ON h.inhparent = pi.indexrelid JOIN pg_class child ON child.oid = h.inhrelid "
                "JOIN pg_index ci ON ci.indexrelid = child.oid "
                "WHERE pi.indrelid = CAST(:table AS regclass) AND ci.indrelid = CAST(:legacy AS regclass)",
            ),
            {"table": table, "legacy": f"{table}_p_legacy"},
        ).all(),
    )
    expected = {f"{table}_pkey": f"{table}_p_legacy_pkey"}
    if table == "kudos_stat_events":
        # Built for the retention job's created sub-ranges on the legacy partition, and attached to nothing.
        legacy_created = _scalar(connection, "SELECT pg_get_indexdef(CAST('ix_kudos_stat_events_legacy_created' AS regclass))")
        assert legacy_created.endswith(".kudos_stat_events_p_legacy USING btree (created)")
        assert not _scalar(connection, "SELECT relispartition FROM pg_class WHERE relname = 'ix_kudos_stat_events_legacy_created'")
    for index_name in PARENT_INDEXES[table]:
        expected[index_name] = index_name.replace(f"ix_{table}_", f"ix_{table}_p_legacy_")
    assert attached == expected
    # Range pruning replaces ix_kudos_ledger_created on the parent. It stays on the legacy partition for the
    # created sub-ranges. The create_all shape's event_id index is dropped, and no normalization index is left over.
    legacy_indexes = set(
        connection.execute(
            sqlalchemy.text("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = CAST(:legacy AS regclass)"),
            {"legacy": f"{table}_p_legacy"},
        ).scalars(),
    )
    local_indexes = {"ix_kudos_ledger_p_legacy_created"} if table == "kudos_ledger" else {"ix_kudos_stat_events_legacy_created"}
    assert legacy_indexes == set(expected.values()) | local_indexes
    invalid = connection.execute(
        sqlalchemy.text("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid = CAST(:table AS regclass) AND NOT indisvalid"),
        {"table": table},
    ).all()
    assert invalid == []
    predicate = _scalar(connection, "SELECT pg_get_indexdef(CAST('ix_kudos_ledger_unapplied' AS regclass))")
    assert predicate.endswith("WHERE (NOT applied)")


def test_conversion_keeps_rows_and_their_queue_state(converted_schema: sqlalchemy.Connection) -> None:
    connection = converted_schema
    assert _scalar(connection, "SELECT count(*) FROM kudos_ledger") == 7
    assert _scalar(connection, "SELECT count(*) FROM kudos_ledger WHERE NOT applied") == 1
    assert _scalar(connection, "SELECT count(*) FROM ONLY kudos_ledger_p_legacy") == 7
    assert _scalar(connection, "SELECT count(*) FROM kudos_stat_events WHERE applied IS FALSE AND quarantined IS FALSE") == 1
    assert _scalar(connection, "SELECT count(*) FROM kudos_stat_events WHERE quarantined IS TRUE") == 1


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_inserts_after_the_swap_route_by_created_and_continue_the_sequence(converted_schema: sqlalchemy.Connection, table: str) -> None:
    connection = converted_schema
    previous_maximum = _scalar(connection, f"SELECT max(id) FROM {table}")
    swap_ts = _scalar(connection, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = :table", table=table)
    if table == "kudos_ledger":
        columns, values, defaulted_flag = "event_id, entry_type, user_id, amount", "gen_random_uuid(), 'AWARD', 1, 1", "escrow"
    else:
        columns = "event_id, entry_type, user_id, amount, unit"
        values, defaulted_flag = "gen_random_uuid(), 'STAT_RECORD', 1, 1, 'count'", "quarantined"
    landed = connection.execute(
        sqlalchemy.text(
            f"INSERT INTO {table} (created, {columns}) VALUES "
            f"(CAST(:swap_ts AS timestamp) + interval '1 hour', {values}), "
            f"(CAST(:swap_ts AS timestamp) - interval '400 days', {values}) "
            f"RETURNING id, tableoid::regclass::text, {defaulted_flag} IS FALSE",
        ),
        {"swap_ts": swap_ts},
    ).all()
    assert [partition for _, partition, _ in landed] == [f"{table}_p_{swap_ts:%Y%m%d}", f"{table}_p_legacy"]
    assert all(row_id > previous_maximum for row_id, _, _ in landed)
    assert all(defaulted for _, _, defaulted in landed)
    # The parent owns the sequence, so dropping the legacy partition does not take the parent's default with it.
    assert _scalar(connection, "SELECT pg_get_serial_sequence(:table, 'id')", table=table) is not None


def test_conversion_moves_the_user_key_to_the_parent(converted_schema: sqlalchemy.Connection) -> None:
    definition = _scalar(
        converted_schema,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST('kudos_ledger' AS regclass) AND contype = 'f'",
    )
    assert definition == "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT"


def test_backfill_keys_the_transfers_and_repairs_inside_the_window(converted_schema: sqlalchemy.Connection) -> None:
    keys = converted_schema.execute(
        sqlalchemy.text(
            "SELECT event_id::text, kind, source_user_id, dest_user_id, amount, outcome FROM kudos_idempotency_keys ORDER BY event_id"
        ),
    ).all()
    assert [tuple(key) for key in keys] == [
        ("00000000-0000-0000-0000-00000000000a", "transfer", 1, 2, 10, "OK"),
        ("00000000-0000-0000-0000-00000000000c", "repair", None, 1, 7, "OK"),
    ]


OLDER_TRANSFER_EVENT_ID = "00000000-0000-0000-0000-0000000000e0"
"""A transfer 60 days old: outside the default key window and inside a 90 day one."""


@pytest.mark.parametrize(
    ("variables", "expected_window_days", "older_transfer_keyed"),
    [({}, "30", False), ({"key_lifetime_days": "90"}, "90", True)],
    ids=["default_window", "key_lifetime_days_90"],
)
def test_backfill_window_follows_the_key_lifetime_variable(
    pg_dsn: str,
    variables: dict[str, str],
    expected_window_days: str,
    older_transfer_keyed: bool,
) -> None:
    """``-v key_lifetime_days`` widens the backfill, so a retry inside a longer key lifetime still finds its key."""
    schema_name = new_test_schema_name("horde_kudos_retention_migration_keys")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            _build_tables(connection, "production")
            for statement in SEED_ROWS:
                connection.execute(sqlalchemy.text(statement))
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO kudos_ledger (created, event_id, entry_type, user_id, escrow, amount, applied) VALUES "
                    f"((now() AT TIME ZONE 'UTC') - interval '60 days', '{OLDER_TRANSFER_EVENT_ID}', 'TRANSFER', 1, false, -4, true), "
                    f"((now() AT TIME ZONE 'UTC') - interval '60 days', '{OLDER_TRANSFER_EVENT_ID}', 'TRANSFER', 2, false, 4, true)",
                ),
            )
            echoed_lines = _apply_migration(connection, variables=variables)
            keyed_event_ids = set(connection.execute(sqlalchemy.text("SELECT event_id::text FROM kudos_idempotency_keys")).scalars())
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)
    assert f"Backfilling the idempotency keys of the last {expected_window_days} days" in echoed_lines
    assert (OLDER_TRANSFER_EVENT_ID in keyed_event_ids) is older_transfer_keyed
    # The 40 day old seed transfer follows the same window.
    assert ("00000000-0000-0000-0000-00000000000b" in keyed_event_ids) is older_transfer_keyed


def test_conversion_stops_before_committing_when_a_legacy_index_does_not_match(pg_dsn: str) -> None:
    """An index the normalization does not cover still stops the swap with a clear error."""
    schema_name = new_test_schema_name("horde_kudos_retention_mismatch")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            _build_tables(connection, "production")
            connection.execute(sqlalchemy.text("DROP INDEX ix_kudos_stat_events_quarantined"))
            connection.execute(sqlalchemy.text("CREATE INDEX ix_kudos_stat_events_quarantined ON kudos_stat_events (created)"))
            with pytest.raises(sqlalchemy.exc.InternalError, match="kudos_stat_events_p_legacy has no valid index matching"):
                _apply_migration(connection)
            connection.execute(sqlalchemy.text("ROLLBACK"))
            assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_stat_events' AS regclass)") == "r"
            assert _scalar(connection, "SELECT to_regclass('kudos_stat_events_p_legacy')") is None
            assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "p"
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


MIGRATION_WEEK_COUNT = 8
"""The number of weekly partitions the migration creates from swap_ts."""
NEXT_SWAP_TS = "SELECT date_trunc('week', (now() AT TIME ZONE 'UTC') + interval '24 hours' + interval '7 days' - interval '1 microsecond')"
"""The swap_ts a run of the migration computes now."""


@pytest.fixture
def unconverted_schema(pg_dsn: str) -> Iterator[sqlalchemy.Connection]:
    """Yield a connection to a throwaway schema holding the production shape without seeded rows."""
    schema_name = new_test_schema_name("horde_kudos_retention_rerun")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            _build_tables(connection, "production")
            yield connection
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


def _add_validated_legacy_range(connection: sqlalchemy.Connection, table: str, swap_ts: Any) -> None:
    """Leave the table as a run that failed after validating its legacy range CHECK leaves it."""
    connection.execute(
        sqlalchemy.text(f"ALTER TABLE {table} ADD CONSTRAINT {table}_legacy_range CHECK (created < CAST(:swap_ts AS timestamp)) NOT VALID"),
        {"swap_ts": swap_ts},
    )
    connection.execute(sqlalchemy.text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {table}_legacy_range"))


def test_rerun_takes_the_swap_timestamp_from_an_existing_legacy_range(unconverted_schema: sqlalchemy.Connection) -> None:
    """A rerun in a new session attaches at the bound the existing CHECK enforces, not at a newly computed Monday.

    The CHECK's bound is a week after the Monday a run computes now, so it differs from the computed one and stays in
    the future whatever the day of the week.
    """
    connection = unconverted_schema
    original_swap_ts = _scalar(connection, f"SELECT ({NEXT_SWAP_TS}) + interval '7 days'")
    _add_validated_legacy_range(connection, "kudos_ledger", original_swap_ts)
    for _ in range(2):
        _apply_migration(connection)
    assert _scalar(connection, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = 'kudos_ledger'") == original_swap_ts
    swap_text = _scalar(connection, "SELECT CAST(CAST(:swap_ts AS timestamp) AS text)", swap_ts=original_swap_ts)
    first_upper = _scalar(connection, "SELECT CAST(CAST(:swap_ts AS timestamp) + interval '7 days' AS text)", swap_ts=original_swap_ts)
    weekly_bounds = _weekly_partition_bounds(connection, "kudos_ledger", original_swap_ts)
    assert weekly_bounds[f"kudos_ledger_p_{original_swap_ts:%Y%m%d}"] == f"FOR VALUES FROM ('{swap_text}') TO ('{first_upper}')"
    assert _partition_bounds(connection, "kudos_ledger") == {
        "kudos_ledger_p_legacy": f"FOR VALUES FROM (MINVALUE) TO ('{swap_text}')",
        **weekly_bounds,
    }
    # The table without a leftover CHECK still takes the Monday computed now.
    computed_swap_ts = _scalar(connection, NEXT_SWAP_TS)
    assert _scalar(connection, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = 'kudos_stat_events'") == computed_swap_ts


def test_rerun_refuses_a_swap_after_the_existing_legacy_range_has_passed(unconverted_schema: sqlalchemy.Connection) -> None:
    """A leftover CHECK whose bound has passed already rejects new rows, so the swap refuses to run."""
    connection = unconverted_schema
    passed_swap_ts = _scalar(connection, f"SELECT ({NEXT_SWAP_TS}) - interval '14 days'")
    _add_validated_legacy_range(connection, "kudos_ledger", passed_swap_ts)
    with pytest.raises(sqlalchemy.exc.InternalError, match="swap_ts for kudos_ledger has passed"):
        _apply_migration(connection)
    connection.execute(sqlalchemy.text("ROLLBACK"))
    assert _scalar(connection, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = 'kudos_ledger'") == passed_swap_ts
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "r"
    assert _scalar(connection, "SELECT to_regclass('kudos_ledger_p_legacy')") is None


def test_rerun_refuses_a_legacy_range_it_cannot_read(unconverted_schema: sqlalchemy.Connection) -> None:
    """A constraint under the legacy range name with another expression stops the run before any swap."""
    connection = unconverted_schema
    connection.execute(sqlalchemy.text("ALTER TABLE kudos_ledger ADD CONSTRAINT kudos_ledger_legacy_range CHECK (amount IS NOT NULL)"))
    with pytest.raises(sqlalchemy.exc.InternalError, match="cannot read swap_ts from kudos_ledger_legacy_range"):
        _apply_migration(connection)
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "r"


LEDGER_SWAP_START = "START TRANSACTION;"
"""The statement that opens the ``kudos_ledger`` swap, the first transaction block of the file."""
UNAPPLIED_POSTING = (
    "INSERT INTO kudos_ledger (event_id, entry_type, user_id, amount) VALUES ('00000000-0000-0000-0000-0000000000e1', 'AWARD', 1, 4)"
)
"""A posting as a request writes it, unapplied and created now."""


def _second_session(pg_dsn: str, connection: sqlalchemy.Connection) -> tuple[sqlalchemy.Engine, sqlalchemy.Connection]:
    """Return an engine and a connection to the schema of ``connection``, for a second session in the test."""
    schema_name = _scalar(connection, "SELECT current_schema()")
    engine = _schema_engine(pg_dsn, schema_name)
    return engine, engine.connect()


def _wait_for_lock_wait(observer: sqlalchemy.Connection, backend_pid: int, *, timeout_seconds: float = 5) -> None:
    """Return once the backend ``backend_pid`` waits for a lock, or fail the test after ``timeout_seconds``."""
    deadline = clock.monotonic() + timeout_seconds
    while clock.monotonic() < deadline:
        if _scalar(observer, "SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted", pid=backend_pid):
            return
        clock.sleep(0.01)
    pytest.fail(f"backend {backend_pid} did not wait for a lock within {timeout_seconds} seconds")


def _run_migration_in_thread(
    connection: sqlalchemy.Connection,
    before_statement: Callable[[str], None],
) -> tuple[threading.Thread, list[BaseException]]:
    """Start the migration on ``connection`` in a thread and return the thread and the list its error is added to."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            _apply_migration(connection, before_statement=before_statement)
        except BaseException as err:  # noqa: BLE001 - asserted in the test thread
            errors.append(err)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _wait_for_flag(flag: list[bool], thread: threading.Thread, errors: list[BaseException]) -> None:
    """Return once ``flag`` has an entry, or fail the test when ``thread`` ended first or 30 seconds passed."""
    deadline = clock.monotonic() + 30
    while not flag and clock.monotonic() < deadline and thread.is_alive():
        clock.sleep(0.01)
    assert flag, errors


def test_ledger_swap_waits_for_the_applier_lock_instead_of_deadlocking(pg_dsn: str, unconverted_schema: sqlalchemy.Connection) -> None:
    """An applier cycle that claims ledger rows and then updates users finishes, and the swap commits after it.

    The applier holds its advisory lock and a row lock on ``kudos_ledger`` when the swap starts. The swap waits for the
    advisory lock before it locks ``users``, so the update of ``users`` by the applier does not wait for the swap.
    """
    from horde.database.kudos_db import KUDOS_APPLIER_LOCK

    connection = unconverted_schema
    connection.execute(sqlalchemy.text("INSERT INTO users (id) VALUES (1)"))
    connection.execute(sqlalchemy.text(UNAPPLIED_POSTING))
    migration_pid = _scalar(connection, "SELECT pg_backend_pid()")
    applier_engine, applier = _second_session(pg_dsn, connection)
    applier_lock_taken: list[bool] = []

    def start_the_applier_cycle(statement: str) -> None:
        if statement.strip() != LEDGER_SWAP_START or applier_lock_taken:
            return
        applier.execute(sqlalchemy.text("BEGIN"))
        applier_lock_taken.append(_scalar(applier, "SELECT pg_try_advisory_xact_lock(:key)", key=KUDOS_APPLIER_LOCK))
        applier.execute(sqlalchemy.text("SELECT id FROM kudos_ledger WHERE NOT applied FOR UPDATE SKIP LOCKED")).all()

    try:
        migration_thread, migration_errors = _run_migration_in_thread(connection, start_the_applier_cycle)
        try:
            _wait_for_flag(applier_lock_taken, migration_thread, migration_errors)
            assert applier_lock_taken == [True]
            _wait_for_lock_wait(applier, migration_pid)
            applier.execute(sqlalchemy.text("UPDATE users SET id = id WHERE id = 1"))
            applier.execute(sqlalchemy.text("COMMIT"))
        finally:
            migration_thread.join(timeout=30)
    finally:
        applier.close()
        applier_engine.dispose()
    assert not migration_thread.is_alive()
    assert not migration_errors, migration_errors
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "p"


def test_ledger_swap_gives_up_before_a_request_deadlocks_with_it(pg_dsn: str, unconverted_schema: sqlalchemy.Connection) -> None:
    """A request that inserted a posting and then updates users completes, and the swap fails and is rerun.

    The swap holds the ``users`` lock and waits for the lock of the request on ``kudos_ledger`` when the request
    updates ``users``. The lock wait of the swap ends before ``deadlock_timeout``, so the swap fails with
    ``lock_not_available`` and the request is not chosen as the deadlock victim.
    """
    connection = unconverted_schema
    connection.execute(sqlalchemy.text("INSERT INTO users (id) VALUES (1)"))
    migration_pid = _scalar(connection, "SELECT pg_backend_pid()")
    request_engine, request = _second_session(pg_dsn, connection)
    request_started: list[bool] = []

    def start_the_request(statement: str) -> None:
        if statement.strip() != LEDGER_SWAP_START or request_started:
            return
        request.execute(sqlalchemy.text("BEGIN"))
        request.execute(sqlalchemy.text(UNAPPLIED_POSTING))
        request_started.append(True)

    try:
        migration_thread, migration_errors = _run_migration_in_thread(connection, start_the_request)
        try:
            _wait_for_flag(request_started, migration_thread, migration_errors)
            _wait_for_lock_wait(request, migration_pid)
            request.execute(sqlalchemy.text("UPDATE users SET id = id WHERE id = 1"))
            request.execute(sqlalchemy.text("COMMIT"))
        finally:
            migration_thread.join(timeout=30)
    finally:
        request.close()
        request_engine.dispose()
    assert not migration_thread.is_alive()
    assert len(migration_errors) == 1
    assert isinstance(migration_errors[0], sqlalchemy.exc.OperationalError)
    assert getattr(migration_errors[0].orig, "pgcode", None) == "55P03"
    connection.execute(sqlalchemy.text("ROLLBACK"))
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "r"
    _apply_migration(connection)
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "p"
    assert _scalar(connection, "SELECT count(*) FROM kudos_ledger") == 1


LEDGER_KEY_BUILD_START = "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS kudos_ledger_p_legacy_pkey"
"""The start of the concurrent build of the ``kudos_ledger`` legacy partition key."""
OLDER_TRANSACTION_SECONDS = 6
"""Seconds the older transaction stays open after the build starts to wait for it, longer than any lock timeout in the file."""
INVALID_INDEXES_IN_SCHEMA = """
SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relnamespace = CAST(current_schema() AS regnamespace) AND NOT i.indisvalid
"""


def test_index_build_waits_for_an_older_transaction_on_another_table_instead_of_failing(
    pg_dsn: str, unconverted_schema: sqlalchemy.Connection
) -> None:
    """A concurrent build outlasts a transaction that started before it, and the run then completes.

    Before it marks the index valid, the build waits for every transaction with an older snapshot, here one that only
    reads ``users``. A lock timeout on the build would fail it after the whole table was read and leave an INVALID
    index.
    """
    connection = unconverted_schema
    migration_pid = _scalar(connection, "SELECT pg_backend_pid()")
    older_engine, older = _second_session(pg_dsn, connection)
    older_transaction_open: list[bool] = []

    def open_the_older_transaction(statement: str) -> None:
        if not statement.startswith(LEDGER_KEY_BUILD_START) or older_transaction_open:
            return
        older.execute(sqlalchemy.text("BEGIN ISOLATION LEVEL REPEATABLE READ"))
        older.execute(sqlalchemy.text("SELECT count(*) FROM users")).all()
        older_transaction_open.append(True)

    try:
        migration_thread, migration_errors = _run_migration_in_thread(connection, open_the_older_transaction)
        try:
            _wait_for_flag(older_transaction_open, migration_thread, migration_errors)
            _wait_for_lock_wait(older, migration_pid)
            clock.sleep(OLDER_TRANSACTION_SECONDS)
            assert migration_thread.is_alive(), migration_errors
            older.execute(sqlalchemy.text("COMMIT"))
        finally:
            migration_thread.join(timeout=30)
    finally:
        older.close()
        older_engine.dispose()
    assert not migration_thread.is_alive()
    assert not migration_errors, migration_errors
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "p"
    assert _scalar(connection, INVALID_INDEXES_IN_SCHEMA) == 0


def test_ledger_swap_refuses_a_deadlock_timeout_at_or_below_its_lock_timeout(unconverted_schema: sqlalchemy.Connection) -> None:
    """The lock waits of the swap must end before the deadlock check of a waiting request, so a short deadlock_timeout stops the run."""
    connection = unconverted_schema
    connection.execute(sqlalchemy.text("SET deadlock_timeout = '500ms'"))
    with pytest.raises(sqlalchemy.exc.InternalError, match="deadlock_timeout is 500ms"):
        _apply_migration(connection)
    connection.execute(sqlalchemy.text("ROLLBACK"))
    assert _scalar(connection, "SELECT relkind FROM pg_class WHERE oid = CAST('kudos_ledger' AS regclass)") == "r"


def _catalog(connection: sqlalchemy.Connection, schema_name: str) -> dict[str, Any]:
    """Return every column attribute, constraint, index and partition key of the compared tables.

    The catalog functions normalize how each DDL source spelled a type, default, constraint or predicate, so only the
    schema qualifier differs between two schemas and is removed.
    """
    items: dict[str, Any] = {}
    for table in (*EVENT_TABLES, *NEW_TABLES):
        relation = {"table": table}
        items[f"{table} partition key"] = connection.execute(
            sqlalchemy.text("SELECT pg_get_partkeydef(CAST(:table AS regclass))"),
            relation,
        ).scalar_one()
        for name, type_name, not_null, default in connection.execute(
            sqlalchemy.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:table AS regclass) AND a.attnum > 0 AND NOT a.attisdropped",
            ),
            relation,
        ):
            items[f"{table} column {name} type"] = type_name
            items[f"{table} column {name} not null"] = not_null
            items[f"{table} column {name} server default"] = None if default is None else default.replace(f"{schema_name}.", "")
        for name, definition in connection.execute(
            sqlalchemy.text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:table AS regclass)"),
            relation,
        ):
            items[f"{table} constraint {name}"] = definition.replace(f"{schema_name}.", "")
        for name, definition in connection.execute(
            sqlalchemy.text(
                "SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = CAST(:table AS regclass)",
            ),
            relation,
        ):
            items[f"{table} index {name}"] = definition.replace(f"{schema_name}.", "")
    return items


@pytest.fixture(scope="module", params=tuple(TABLE_SHAPES))
def _retention_catalogs(request: pytest.FixtureRequest, pg_dsn: str) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Build the compared tables from the migration and from the ORM models in two schemas and return both catalogs."""
    from horde.classes.base.kudos import (
        KudosIdempotencyKey,
        KudosLedger,
        KudosLedgerCheckpoint,
        KudosLedgerDailyStatement,
        KudosRetentionUnit,
        KudosSnapshotInvalidation,
        KudosStatEvent,
        KudosStatQuarantine,
        KudosStatRollup,
    )

    migration_schema = new_test_schema_name("horde_kudos_retention_parity_sql")
    orm_schema = new_test_schema_name("horde_kudos_retention_parity_orm")
    engines: list[sqlalchemy.Engine] = []
    for schema_name in (migration_schema, orm_schema):
        create_schema(pg_dsn, schema_name)
        engines.append(_schema_engine(pg_dsn, schema_name))
    migration_engine, orm_engine = engines
    try:
        with migration_engine.connect() as connection:
            _build_tables(connection, request.param)
            _apply_migration(connection)
            migration_catalog = _catalog(connection, migration_schema)
        with orm_engine.connect() as connection:
            connection.execute(sqlalchemy.text(USERS_STUB))
            for model in (
                KudosSnapshotInvalidation,
                KudosLedger,
                KudosStatEvent,
                KudosStatRollup,
                KudosStatQuarantine,
                KudosLedgerCheckpoint,
                KudosLedgerDailyStatement,
                KudosIdempotencyKey,
                KudosRetentionUnit,
            ):
                model.__table__.create(connection)
            orm_catalog = _catalog(connection, orm_schema)
        yield migration_catalog, orm_catalog
    finally:
        for engine in engines:
            engine.dispose()
        for schema_name in (migration_schema, orm_schema):
            drop_schema(pg_dsn, schema_name)


def test_orm_models_build_the_same_tables_as_the_migration(_retention_catalogs: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """Columns, types, nullability, server defaults, keys, constraints, indexes and partition keys agree.

    Behavior tests build their schema from the models while production runs the migration over the tables
    sql_statements/5.1.0.txt built, so a difference here means the tests exercise a schema production does not have.
    """
    migration_catalog, orm_catalog = _retention_catalogs
    assert any(" index " in item for item in migration_catalog), "the catalog query found no indexes"
    differences = [
        f"{item}: migration {migration_catalog.get(item, ABSENT)!r}, ORM {orm_catalog.get(item, ABSENT)!r}"
        for item in sorted(migration_catalog.keys() | orm_catalog.keys())
        if migration_catalog.get(item, ABSENT) != orm_catalog.get(item, ABSENT)
    ]
    assert not differences, "\n".join(differences)


def test_partition_bootstrap_continues_from_the_migrated_layout(app: Any, converted_schema: sqlalchemy.Connection) -> None:
    """The application's weekly partitions start at the last migrated week's upper bound and follow the Monday grid."""
    from datetime import datetime

    from horde.classes.base.kudos import KudosPartitioning, ensure_kudos_event_partitions
    from horde.flask import db

    schema_name = _scalar(converted_schema, "SELECT current_schema()")
    migrated_upper = _scalar(
        converted_schema,
        "SELECT swap_ts + :week_count * interval '7 days' FROM kudos_swap_parameters WHERE table_name = 'kudos_ledger'",
        week_count=MIGRATION_WEEK_COUNT,
    )
    with app.app_context():
        try:
            db.session.execute(sqlalchemy.text(f"SET LOCAL search_path TO {schema_name}"))
            created = ensure_kudos_event_partitions(
                until=migrated_upper + 2 * KudosPartitioning.DEFAULT_WIDTH,
                width=KudosPartitioning.DEFAULT_WIDTH,
            )
            bounds = _partition_bounds(db.session.connection(), "kudos_ledger")
        finally:
            db.session.rollback()
    next_upper = migrated_upper + timedelta(days=7)
    assert f"kudos_ledger_p_{migrated_upper:%Y%m%d}" in created
    assert bounds[f"kudos_ledger_p_{migrated_upper:%Y%m%d}"] == f"FOR VALUES FROM ('{migrated_upper}') TO ('{next_upper}')"
    assert all(datetime.fromisoformat(name[-8:]).weekday() == 0 for name in created if not name.endswith("_p_legacy"))


def _storage_parameters(connection: sqlalchemy.Connection, relation: str) -> list[str]:
    reloptions = _scalar(connection, "SELECT reloptions FROM pg_class WHERE oid = CAST(:relation AS regclass)", relation=relation)
    return sorted(reloptions or [])


def _expected_storage_parameters(table: str) -> list[str]:
    from horde.classes.base.kudos import KudosPartitioning

    return sorted(f"{parameter}={setting}" for parameter, setting in KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS[table].items())


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_weekly_partitions_keep_the_autovacuum_storage_parameters(converted_schema: sqlalchemy.Connection, table: str) -> None:
    """Every weekly partition of the migration gets the fixed dead-row thresholds sql_statements/5.1.10.txt set on the table."""
    swap_ts = _scalar(converted_schema, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = :table", table=table)
    assert _storage_parameters(converted_schema, f"{table}_p_{swap_ts:%Y%m%d}") == _expected_storage_parameters(table)
    for partition_name in _weekly_partition_bounds(converted_schema, table, swap_ts):
        assert _storage_parameters(converted_schema, partition_name) == _expected_storage_parameters(table), partition_name


def test_rerun_after_retention_dropped_the_legacy_partition_leaves_the_tables_unchanged(converted_schema: sqlalchemy.Connection) -> None:
    """Once retention dropped <table>_p_legacy, the whole file reruns in a new session without changing either parent."""
    schema_name = _scalar(converted_schema, "SELECT current_schema()")
    for table in EVENT_TABLES:
        converted_schema.execute(sqlalchemy.text(f"ALTER TABLE {table} DETACH PARTITION {table}_p_legacy"))
        converted_schema.execute(sqlalchemy.text(f"DROP TABLE {table}_p_legacy"))
    catalog_before = _catalog(converted_schema, schema_name)
    bounds_before = {table: _partition_bounds(converted_schema, table) for table in EVENT_TABLES}
    with converted_schema.engine.connect() as new_session:
        _apply_migration(new_session)
        catalog_after = _catalog(new_session, schema_name)
        bounds_after = {table: _partition_bounds(new_session, table) for table in EVENT_TABLES}
    assert catalog_after == catalog_before
    assert bounds_after == bounds_before
    assert all(f"{table}_p_legacy" not in bounds_after[table] for table in EVENT_TABLES)


def test_rerun_creates_only_the_missing_weeks(converted_schema: sqlalchemy.Connection) -> None:
    """A rerun recreates a missing week and leaves every existing partition alone, including a wider one the application made.

    The application creates partitions of ``KUDOS_PARTITION_WIDTH_DAYS``, so one of its partitions can span two of the
    migration's weeks. The rerun skips both weeks instead of failing on the overlap.
    """
    expected_bounds: dict[str, dict[str, str]] = {}
    for table in EVENT_TABLES:
        swap_ts = _scalar(converted_schema, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = :table", table=table)
        missing_week = f"{table}_p_{swap_ts + timedelta(weeks=3):%Y%m%d}"
        wide_lower = swap_ts + timedelta(weeks=MIGRATION_WEEK_COUNT - 2)
        wide_upper = swap_ts + timedelta(weeks=MIGRATION_WEEK_COUNT)
        replaced_weeks = (f"{table}_p_{wide_lower:%Y%m%d}", f"{table}_p_{wide_lower + timedelta(weeks=1):%Y%m%d}")
        for dropped_week in (missing_week, *replaced_weeks):
            converted_schema.execute(sqlalchemy.text(f"ALTER TABLE {table} DETACH PARTITION {dropped_week}"))
            converted_schema.execute(sqlalchemy.text(f"DROP TABLE {dropped_week}"))
        wide_partition = f"{table}_p_{wide_lower:%Y%m%d}"
        converted_schema.execute(
            sqlalchemy.text(f"CREATE TABLE {wide_partition} PARTITION OF {table} FOR VALUES FROM ('{wide_lower}') TO ('{wide_upper}')"),
        )
        expected_bounds[table] = _partition_bounds(converted_schema, table)
        expected_bounds[table][missing_week] = _weekly_partition_bounds(converted_schema, table, swap_ts)[missing_week]
    with converted_schema.engine.connect() as new_session:
        _apply_migration(new_session)
        bounds_after = {table: _partition_bounds(new_session, table) for table in EVENT_TABLES}
        for table in EVENT_TABLES:
            swap_ts = _scalar(new_session, "SELECT swap_ts FROM kudos_swap_parameters WHERE table_name = :table", table=table)
            recreated_week = f"{table}_p_{swap_ts + timedelta(weeks=3):%Y%m%d}"
            assert _storage_parameters(new_session, recreated_week) == _expected_storage_parameters(table)
            wide_partition = f"{table}_p_{swap_ts + timedelta(weeks=MIGRATION_WEEK_COUNT - 2):%Y%m%d}"
            assert _storage_parameters(new_session, wide_partition) == []
    assert bounds_after == expected_bounds


@pytest.fixture
def model_schema(pg_dsn: str) -> Iterator[str]:
    """Yield a throwaway schema holding the event tables as the ORM models build them, without partitions."""
    from horde.classes.base.kudos import KudosLedger, KudosStatEvent

    schema_name = new_test_schema_name("horde_kudos_partition_maintenance")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text(USERS_STUB))
            connection.execute(sqlalchemy.text("INSERT INTO users (id) VALUES (1)"))
            for model in (KudosLedger, KudosStatEvent):
                model.__table__.create(connection)
        yield schema_name
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


def _insert_now_partition(connection: sqlalchemy.Connection, table: str) -> str:
    if table == "kudos_ledger":
        columns, values = "event_id, entry_type, user_id, amount", "gen_random_uuid(), 'AWARD', 1, 1"
    else:
        columns, values = "event_id, entry_type, user_id, amount, unit", "gen_random_uuid(), 'STAT_RECORD', 1, 1, 'count'"
    return str(
        connection.execute(
            sqlalchemy.text(
                f"INSERT INTO {table} (created, {columns}) VALUES (now() AT TIME ZONE 'UTC', {values}) RETURNING tableoid::regclass::text",
            ),
        ).scalar_one(),
    )


def test_partition_maintenance_restores_the_current_week_and_leaves_older_gaps(app: Any, model_schema: str) -> None:
    """A dropped current week comes back with the storage parameters, and a gap before the current week stays open.

    The older gap stands for a week retention dropped, which must not be recreated empty.
    """
    from datetime import datetime

    from horde.classes.base.kudos import KudosPartitioning, ensure_kudos_event_partitions
    from horde.flask import db

    width = KudosPartitioning.DEFAULT_WIDTH
    with app.app_context():
        try:
            db.session.execute(sqlalchemy.text(f"SET LOCAL search_path TO {model_schema}"))
            connection = db.session.connection()
            real_now = _scalar(connection, "SELECT now() AT TIME ZONE 'UTC'")
            week_start = datetime(real_now.year, real_now.month, real_now.day) - timedelta(days=real_now.weekday())
            bootstrapped = ensure_kudos_event_partitions(until=real_now + 2 * width, width=width)
            for table in EVENT_TABLES:
                current_week = f"{table}_p_{week_start:%Y%m%d}"
                assert current_week in bootstrapped
                for created_name in (name for name in bootstrapped if name.startswith(f"{table}_p_")):
                    assert _storage_parameters(connection, created_name) == _expected_storage_parameters(table), created_name
                # Replace the legacy partition with one surviving older week, leaving the week before the current one
                # uncovered, then drop the current week.
                connection.execute(sqlalchemy.text(f"DROP TABLE {table}_p_legacy"))
                older_week = week_start - 2 * width
                connection.execute(
                    sqlalchemy.text(
                        f"CREATE TABLE {table}_p_{older_week:%Y%m%d} PARTITION OF {table} "
                        f"FOR VALUES FROM ('{older_week.isoformat(sep=' ')}') TO ('{(older_week + width).isoformat(sep=' ')}')",
                    ),
                )
                connection.execute(sqlalchemy.text(f"DROP TABLE {current_week}"))
                with pytest.raises(sqlalchemy.exc.IntegrityError, match="no partition"):
                    with connection.begin_nested():
                        _insert_now_partition(connection, table)
            refilled = ensure_kudos_event_partitions(until=real_now + 2 * width, width=width)
            assert refilled == [f"{table}_p_{week_start:%Y%m%d}" for table in EVENT_TABLES]
            for table in EVENT_TABLES:
                current_week = f"{table}_p_{week_start:%Y%m%d}"
                assert _insert_now_partition(connection, table) == current_week
                assert _storage_parameters(connection, current_week) == _expected_storage_parameters(table)
                bounds = _partition_bounds(connection, table)
                assert f"{table}_p_{week_start - width:%Y%m%d}" not in bounds
                assert f"{table}_p_{week_start - 2 * width:%Y%m%d}" in bounds
            # Complete coverage costs one listing and does not create anything.
            assert ensure_kudos_event_partitions(until=real_now + 2 * width, width=width) == []
        finally:
            db.session.rollback()


def _partition_shape(connection: sqlalchemy.Connection, partition: str) -> dict[str, Any]:
    """Return a partition's columns, constraints, indexes and storage parameters, with its name as ``<partition>``.

    Each index is listed with the parent index it is attached to, so an index ``ATTACH PARTITION`` created because no
    copied index matched, or a copied index left unattached, shows as a difference.
    """

    def normalized(definition: str) -> str:
        return definition.replace(partition, "<partition>")

    columns = connection.execute(
        sqlalchemy.text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:partition AS regclass) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
        ),
        {"partition": partition},
    ).all()
    constraints = connection.execute(
        sqlalchemy.text(
            "SELECT contype, pg_get_constraintdef(oid), conparentid <> 0 FROM pg_constraint "
            "WHERE conrelid = CAST(:partition AS regclass) AND contype <> 'n' ORDER BY 1, 2",
        ),
        {"partition": partition},
    ).all()
    indexes = connection.execute(
        sqlalchemy.text(
            "SELECT parent_index.relname, pg_get_indexdef(x.indexrelid) FROM pg_index x "
            "LEFT JOIN pg_inherits i ON i.inhrelid = x.indexrelid LEFT JOIN pg_class parent_index ON parent_index.oid = i.inhparent "
            "WHERE x.indrelid = CAST(:partition AS regclass)",
        ),
        {"partition": partition},
    ).all()
    return {
        "columns": [tuple(column) for column in columns],
        "constraints": [tuple(constraint) for constraint in constraints],
        "indexes": sorted(
            (parent_index, re.sub(r"INDEX \S+ ON", "INDEX ON", normalized(definition))) for parent_index, definition in indexes
        ),
        "storage_parameters": _storage_parameters(connection, partition),
    }


@pytest.mark.parametrize("table", EVENT_TABLES)
def test_created_partition_matches_one_created_with_partition_of(app: Any, model_schema: str, table: str) -> None:
    """A partition built standalone and attached has what ``CREATE TABLE ... PARTITION OF`` gives, and the expected bound."""
    from datetime import datetime

    from horde.classes.base.kudos import KudosPartitioning, ensure_kudos_event_partitions
    from horde.flask import db

    width = KudosPartitioning.DEFAULT_WIDTH
    storage_parameters = ", ".join(
        f"{parameter} = {setting}" for parameter, setting in KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS[table].items()
    )
    with app.app_context():
        try:
            db.session.execute(sqlalchemy.text(f"SET LOCAL search_path TO {model_schema}"))
            connection = db.session.connection()
            real_now = _scalar(connection, "SELECT now() AT TIME ZONE 'UTC'")
            week_start = datetime(real_now.year, real_now.month, real_now.day) - timedelta(days=real_now.weekday())
            created = [
                name for name in ensure_kudos_event_partitions(until=real_now + 2 * width, width=width) if name.startswith(f"{table}_p_")
            ]
            reference_lower = week_start + 52 * width
            connection.execute(
                sqlalchemy.text(
                    f"CREATE TABLE {table}_reference PARTITION OF {table} FOR VALUES FROM ('{reference_lower.isoformat(sep=' ')}') "
                    f"TO ('{(reference_lower + width).isoformat(sep=' ')}') WITH ({storage_parameters})",
                ),
            )
            reference_shape = _partition_shape(connection, f"{table}_reference")
            created_shapes = {name: _partition_shape(connection, name) for name in created}
            bounds = _partition_bounds(connection, table)
        finally:
            db.session.rollback()
    assert created[0] == f"{table}_p_legacy"
    assert f"{table}_p_{week_start:%Y%m%d}" in created
    assert len(reference_shape["indexes"]) == len(PARENT_INDEXES[table]) + 1
    assert all(parent_index is not None for parent_index, _ in reference_shape["indexes"])
    if table == "kudos_ledger":
        assert ("f", "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT", True) in reference_shape["constraints"]
    for created_name, created_shape in created_shapes.items():
        assert created_shape == reference_shape, created_name
    assert bounds[f"{table}_p_legacy"] == f"FOR VALUES FROM (MINVALUE) TO ('{week_start}')"
    for created_name in created[1:]:
        lower = datetime.strptime(created_name[-8:], KudosPartitioning.NAME_FORMAT)
        assert bounds[created_name] == f"FOR VALUES FROM ('{lower}') TO ('{lower + width}')"


def _insert_through_parent(connection: sqlalchemy.Connection, table: str) -> str:
    """Insert a row created now through the parent with a short ``lock_timeout`` and return the partition it landed in."""
    connection.execute(sqlalchemy.text("SET lock_timeout = '1s'"))
    return _insert_now_partition(connection, table)


def test_partition_creation_does_not_wait_for_a_reader_of_the_parent(app: Any, model_schema: str, pg_dsn: str) -> None:
    """A long repeatable-read transaction that read the parent holds ``ACCESS SHARE``, which the attach does not wait for."""
    from horde.classes.base.kudos import KudosPartitioning, ensure_kudos_event_partitions
    from horde.flask import db

    width = KudosPartitioning.DEFAULT_WIDTH
    reader_engine = _schema_engine(pg_dsn, model_schema).execution_options(isolation_level="REPEATABLE READ")
    try:
        with reader_engine.connect() as reader, app.app_context():
            for table in EVENT_TABLES:
                reader.execute(sqlalchemy.text(f"SELECT count(*) FROM {table}")).scalar_one()
            try:
                db.session.execute(sqlalchemy.text(f"SET LOCAL search_path TO {model_schema}"))
                real_now = _scalar(db.session.connection(), "SELECT now() AT TIME ZONE 'UTC'")
                started = clock.monotonic()
                created = ensure_kudos_event_partitions(until=real_now + 2 * width, width=width)
                elapsed = clock.monotonic() - started
            finally:
                db.session.rollback()
            reader.rollback()
    finally:
        reader_engine.dispose()
    assert {f"{table}_p_legacy" for table in EVENT_TABLES} <= set(created)
    assert elapsed < 1.0


def test_partition_creation_defers_on_lock_timeout_and_retries(
    app: Any, model_schema: str, pg_dsn: str, log_records: list[tuple[str, str]]
) -> None:
    """A lock the attach cannot get within the timeout defers that partition to the next call without blocking inserts.

    An open transaction that wrote to ``users`` holds ``ROW EXCLUSIVE`` on it, which conflicts with the
    ``SHARE ROW EXCLUSIVE`` the attach of a ``kudos_ledger`` partition takes for the foreign key. Inserts into an
    existing partition through the parent proceed while the attach waits.
    """
    from horde.classes.base.kudos import KudosPartitioning, ensure_kudos_event_partitions
    from horde.flask import db

    width = KudosPartitioning.DEFAULT_WIDTH
    lock_timeout_seconds = float(KudosPartitioning.CREATE_LOCK_TIMEOUT.removesuffix("s"))
    schema_engine = _schema_engine(pg_dsn, model_schema)
    holder_engine = schema_engine.execution_options(isolation_level="READ COMMITTED")
    inserted_partitions: list[str] = []
    inserter_errors: list[BaseException] = []

    def insert_while_the_attach_waits() -> None:
        try:
            with schema_engine.connect() as inserter:
                deadline = clock.monotonic() + lock_timeout_seconds
                while clock.monotonic() < deadline:
                    waiting_attaches = _scalar(
                        inserter,
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND query LIKE '%ATTACH PARTITION%'",
                    )
                    if waiting_attaches:
                        inserted_partitions.append(_insert_through_parent(inserter, "kudos_ledger"))
                        return
                    clock.sleep(0.05)
        except BaseException as err:  # noqa: BLE001 - re-raised in the test thread below
            inserter_errors.append(err)

    try:
        with app.app_context(), holder_engine.connect() as holder:
            try:
                db.session.execute(sqlalchemy.text(f"SET LOCAL search_path TO {model_schema}"))
                real_now = _scalar(db.session.connection(), "SELECT now() AT TIME ZONE 'UTC'")
                ensure_kudos_event_partitions(until=real_now + 2 * width, width=width)
                db.session.commit()
                holder.execute(sqlalchemy.text("INSERT INTO users (id) VALUES (2)"))
                inserter_thread = threading.Thread(target=insert_while_the_attach_waits)
                inserter_thread.start()
                db.session.execute(sqlalchemy.text(f"SET LOCAL search_path TO {model_schema}"))
                started = clock.monotonic()
                deferred_call = ensure_kudos_event_partitions(until=real_now + 3 * width, width=width)
                elapsed = clock.monotonic() - started
                inserter_thread.join()
                assert not inserter_errors, inserter_errors
                assert len(deferred_call) == 1 and deferred_call[0].startswith("kudos_stat_events_p_")
                deferred_partition = deferred_call[0].replace("kudos_stat_events_p_", "kudos_ledger_p_")
                standalone_left = _scalar(db.session.connection(), "SELECT to_regclass(:name) IS NOT NULL", name=deferred_partition)
                holder.rollback()
                retried_call = ensure_kudos_event_partitions(until=real_now + 3 * width, width=width)
                retried_bounds = _partition_bounds(db.session.connection(), "kudos_ledger")
            finally:
                db.session.rollback()
    finally:
        schema_engine.dispose()
    assert lock_timeout_seconds <= elapsed < lock_timeout_seconds + 2
    assert inserted_partitions and inserted_partitions[0].startswith("kudos_ledger_p_")
    assert any(level == "WARNING" and deferred_partition in message for level, message in log_records)
    assert standalone_left is False
    assert retried_call == [deferred_partition]
    assert deferred_partition in retried_bounds
