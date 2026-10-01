# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the moderation DDL is additive, repeatable, and builds the same tables as the ORM models."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
import sqlparse

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name

MIGRATION_PATH = Path(__file__).resolve().parents[2] / "sql_statements" / "5.1.12.txt"
"""The migration that creates the moderation tables in production."""
WAITING_PROMPTS_STUB = "CREATE TABLE waiting_prompts (id UUID PRIMARY KEY, sharedkey_id UUID, prompt TEXT)"
"""The pre-change columns of ``waiting_prompts`` the migration alters or indexes."""
WORKERS_STUB = "CREATE TABLE workers (id UUID PRIMARY KEY)"
"""The key ``user_problem_jobs.worker_id`` referenced before the migration."""
USER_PROBLEM_JOBS_STUB = (
    "CREATE TABLE user_problem_jobs (id INTEGER PRIMARY KEY, ipaddr VARCHAR(39) NOT NULL, created TIMESTAMP NOT NULL, "
    "worker_id UUID NOT NULL REFERENCES workers (id) ON DELETE CASCADE)"
)
"""The pre-change columns of ``user_problem_jobs`` the migration alters or indexes.

The worker key is unnamed, as ``db.create_all()`` built it, so PostgreSQL gives it the default name the migration drops.
"""
MODERATION_TABLES = ("prompt_moderation_events", "prompt_moderation_notes")
"""The tables the migration creates, which the ORM models must build identically."""


def _schema_engine(pg_dsn: str, schema_name: str) -> sqlalchemy.Engine:
    return sqlalchemy.create_engine(
        pg_dsn,
        isolation_level="AUTOCOMMIT",
        connect_args={"options": f"-c search_path={schema_name}"},
    )


def _apply_migration(connection: sqlalchemy.Connection) -> None:
    for statement in sqlparse.split(MIGRATION_PATH.read_text()):
        connection.execute(sqlalchemy.text(statement))


def test_moderation_migration_is_additive_and_repeatable(pg_dsn: str) -> None:
    schema_name = new_test_schema_name("horde_moderation_migration")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text(WAITING_PROMPTS_STUB))
            connection.execute(sqlalchemy.text(WORKERS_STUB))
            connection.execute(sqlalchemy.text(USER_PROBLEM_JOBS_STUB))
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO waiting_prompts (id, prompt) VALUES ('00000000-0000-0000-0000-000000000001', 'effective')",
                )
            )
            for _ in range(2):
                _apply_migration(connection)
            row = connection.execute(sqlalchemy.text("SELECT prompt, submitted_prompt FROM waiting_prompts")).one()
            assert row == ("effective", None)
            indexes = {index["name"]: index for index in sqlalchemy.inspect(connection).get_indexes("waiting_prompts")}
            assert "ix_waiting_prompts_sharedkey_id" in indexes
            sharedkey_index = indexes["ix_waiting_prompts_sharedkey_id"]
            assert sharedkey_index["column_names"] == ["sharedkey_id"]
            predicate = sharedkey_index.get("dialect_options", {}).get("postgresql_where")
            assert predicate is not None
            assert "sharedkey_id IS NOT NULL" in str(predicate)
            inspector = sqlalchemy.inspect(connection)
            tables = set(inspector.get_table_names())
            assert {"prompt_moderation_events", "prompt_moderation_notes"} <= tables
            columns = {column["name"]: column for column in inspector.get_columns("prompt_moderation_events")}
            assert {"ipaddr", "ip_subject_key", "models"} <= set(columns)
            # Retention removes identity at the ceiling, so the account column accepts null.
            assert columns["user_id"]["nullable"]
            for flag in ("text_redacted", "ipaddr_redacted", "anonymized"):
                assert not columns[flag]["nullable"], flag
                assert str(columns[flag]["default"]).lower() == "false", flag
            event_indexes = {index["name"]: index for index in inspector.get_indexes("prompt_moderation_events")}
            assert {
                "ix_prompt_moderation_created",
                "ix_prompt_moderation_user_id",
                "ix_prompt_moderation_ip_subject_key_id",
                "ix_prompt_moderation_unkeyed_ipaddr_id",
                "ix_prompt_moderation_proxied_account_id",
                "ix_prompt_moderation_worker_id",
            } <= set(event_indexes)
            assert event_indexes["ix_prompt_moderation_proxied_account_id"]["column_names"] == ["proxied_account", "id"]
            for index_name, flag in (
                ("ix_prompt_moderation_text_pending", "text_redacted"),
                ("ix_prompt_moderation_ipaddr_pending", "ipaddr_redacted"),
                ("ix_prompt_moderation_anonymize_pending", "anonymized"),
            ):
                retention_index = event_indexes[index_name]
                assert retention_index["column_names"] == ["created", "id"], index_name
                retention_predicate = str(retention_index.get("dialect_options", {}).get("postgresql_where"))
                assert f"NOT {flag}" in retention_predicate, (index_name, retention_predicate)
            text_state_index = event_indexes["ix_prompt_moderation_text_state_pending"]
            assert text_state_index["column_names"] == ["id"]
            assert "'pending'" in str(text_state_index.get("dialect_options", {}).get("postgresql_where"))
            assert not columns["text_state"]["nullable"]
            assert columns["text_sha256"]["nullable"] and columns["text_chars"]["nullable"]
            assert "ix_prompt_moderation_notes_event_id" in {index["name"] for index in inspector.get_indexes("prompt_moderation_notes")}
            problem_job_columns = {column["name"]: column for column in inspector.get_columns("user_problem_jobs")}
            # Retention removes a worker report's address before it deletes the report.
            assert problem_job_columns["ipaddr"]["nullable"]
            problem_job_indexes = {index["name"]: index for index in inspector.get_indexes("user_problem_jobs")}
            pending_index = problem_job_indexes["ix_user_problem_jobs_ipaddr_pending"]
            assert pending_index["column_names"] == ["created", "id"]
            assert problem_job_columns["origin_text"]["nullable"]
            pending_predicate = str(pending_index.get("dialect_options", {}).get("postgresql_where"))
            assert "ipaddr IS NOT NULL" in pending_predicate
            assert "origin_text IS NOT NULL" in pending_predicate
            # Deleting a worker keeps the reports it made, which are evidence about other accounts.
            assert inspector.get_foreign_keys("user_problem_jobs") == []
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


def test_events_captured_before_the_text_location_columns_are_queued_for_upload(pg_dsn: str) -> None:
    """Adding the text location to an existing events table marks each existing event pending, so its text is uploaded."""
    schema_name = new_test_schema_name("horde_moderation_text_state")
    create_schema(pg_dsn, schema_name)
    engine = _schema_engine(pg_dsn, schema_name)
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text(WAITING_PROMPTS_STUB))
            connection.execute(sqlalchemy.text(WORKERS_STUB))
            connection.execute(sqlalchemy.text(USER_PROBLEM_JOBS_STUB))
            _apply_migration(connection)
            connection.execute(
                sqlalchemy.text(
                    "ALTER TABLE prompt_moderation_events DROP COLUMN text_state, DROP COLUMN text_sha256, DROP COLUMN text_chars",
                ),
            )
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO prompt_moderation_events (user_id, reason, outcome, submitted_prompt) "
                    "VALUES (1, 'filter_rejection', 'rejected', 'captured earlier')",
                ),
            )
            _apply_migration(connection)
            row = connection.execute(
                sqlalchemy.text("SELECT text_state, text_sha256, text_chars, submitted_prompt FROM prompt_moderation_events"),
            ).one()
            assert tuple(row) == ("pending", None, None, "captured earlier")
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                connection.execute(sqlalchemy.text("UPDATE prompt_moderation_events SET text_state = 'other'"))
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)


ABSENT = "<absent>"
"""The value reported for an item one schema has and the other lacks."""


def _catalog(connection: sqlalchemy.Connection, schema_name: str) -> dict[str, Any]:
    """Return every column attribute, constraint and index of the moderation tables, as PostgreSQL renders them.

    The catalog functions normalize how each DDL source spelled a type, default, constraint or predicate, so only the
    schema qualifier differs between two schemas and is removed.
    """
    items: dict[str, Any] = {}
    for table in MODERATION_TABLES:
        relation = {"table": table}
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
            items[f"{table} column {name} server default"] = default
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


@pytest.fixture(scope="module")
def _moderation_catalogs(pg_dsn: str) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Build the moderation tables from the migration and from the ORM models in two schemas and return both catalogs."""
    from horde.classes.base.prompt_moderation import PromptModerationEvent, PromptModerationNote

    migration_schema = new_test_schema_name("horde_moderation_parity_sql")
    orm_schema = new_test_schema_name("horde_moderation_parity_orm")
    engines: list[sqlalchemy.Engine] = []
    for schema_name in (migration_schema, orm_schema):
        create_schema(pg_dsn, schema_name)
        engines.append(_schema_engine(pg_dsn, schema_name))
    migration_engine, orm_engine = engines
    try:
        with migration_engine.connect() as connection:
            connection.execute(sqlalchemy.text(WAITING_PROMPTS_STUB))
            connection.execute(sqlalchemy.text(WORKERS_STUB))
            connection.execute(sqlalchemy.text(USER_PROBLEM_JOBS_STUB))
            _apply_migration(connection)
            migration_catalog = _catalog(connection, migration_schema)
        with orm_engine.connect() as connection:
            for table in (PromptModerationEvent.__table__, PromptModerationNote.__table__):
                table.create(connection)
            orm_catalog = _catalog(connection, orm_schema)
        yield migration_catalog, orm_catalog
    finally:
        for engine in engines:
            engine.dispose()
        for schema_name in (migration_schema, orm_schema):
            drop_schema(pg_dsn, schema_name)


def test_orm_models_build_the_same_moderation_tables_as_the_migration(
    _moderation_catalogs: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    """Columns, types, nullability, server defaults, keys, constraints and indexes agree between the two DDL sources.

    Behavior tests build their schema from the models while production runs the migration, so a difference here means
    the tests exercise a schema production does not have.
    """
    migration_catalog, orm_catalog = _moderation_catalogs
    assert any(" index " in item for item in migration_catalog), "the catalog query found no indexes"
    differences = [
        f"{item}: migration {migration_catalog.get(item, ABSENT)!r}, ORM {orm_catalog.get(item, ABSENT)!r}"
        for item in sorted(migration_catalog.keys() | orm_catalog.keys())
        if migration_catalog.get(item, ABSENT) != orm_catalog.get(item, ABSENT)
    ]
    assert not differences, "\n".join(differences)
