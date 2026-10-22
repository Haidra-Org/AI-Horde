# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Upgrade-path coverage for the chat request migration."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
import sqlparse

pytestmark = pytest.mark.integration


@pytest.fixture
def migration_database(pg_dsn: str) -> Iterator[str]:
    """Provide a disposable database so the production migration can run unchanged."""
    database_name = f"chat_migration_{uuid.uuid4().hex[:12]}"
    if re.fullmatch(r"[a-z0-9_]+", database_name) is None:
        raise RuntimeError(f"Unsafe generated test database name: {database_name!r}")

    admin_engine = sqlalchemy.create_engine(pg_dsn, isolation_level="AUTOCOMMIT")
    database_url = sqlalchemy.engine.make_url(pg_dsn).set(database=database_name)
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        yield database_url.render_as_string(hide_password=False)
    finally:
        try:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        finally:
            admin_engine.dispose()


def _run_migration(connection: sqlalchemy.Connection) -> None:
    migration_path = Path(__file__).parents[2] / "sql_statements/5.1.15.txt"
    migration = migration_path.read_text(encoding="utf-8")
    for statement in sqlparse.split(migration, strip_semicolon=True):
        connection.exec_driver_sql(statement)


def test_chat_migration_adds_the_chat_columns_with_existing_rows_marked_text_and_workers_opted_out(migration_database: str) -> None:
    engine = sqlalchemy.create_engine(migration_database, isolation_level="AUTOCOMMIT")
    worker_id = uuid.uuid4()
    request_id = uuid.uuid4()

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE waiting_prompts (id UUID PRIMARY KEY, user_id INTEGER)")
            connection.exec_driver_sql("CREATE TABLE workers (id UUID PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE processing_gens (id UUID PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE worker_blacklists (id INTEGER PRIMARY KEY, worker_id UUID NOT NULL)")
            connection.exec_driver_sql("CREATE TABLE text_worker_softprompts (id INTEGER PRIMARY KEY, worker_id UUID NOT NULL)")
            connection.execute(sqlalchemy.text("INSERT INTO workers (id) VALUES (:worker_id)"), {"worker_id": worker_id})
            connection.execute(sqlalchemy.text("INSERT INTO waiting_prompts (id) VALUES (:request_id)"), {"request_id": request_id})

            _run_migration(connection)
            _run_migration(connection)

            columns = connection.execute(
                sqlalchemy.text(
                    """
                    SELECT table_name, column_name, data_type, character_maximum_length, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                        AND column_name NOT IN ('id', 'user_id')
                        AND NOT (
                            table_name IN ('worker_blacklists', 'text_worker_softprompts')
                            AND column_name = 'worker_id'
                        )
                    ORDER BY table_name, column_name
                    """,
                ),
            ).all()
            assert [tuple(column) for column in columns] == [
                ("processing_gens", "assigned_max_context_length", "integer", None, "YES"),
                ("processing_gens", "assigned_max_length", "integer", None, "YES"),
                ("processing_gens", "assigned_model", "character varying", None, "YES"),
                ("processing_gens", "charge_basis", "character varying", 16, "YES"),
                ("processing_gens", "chat_refusal_rc", "character varying", 32, "YES"),
                ("processing_gens", "completion_tokens", "integer", None, "YES"),
                ("processing_gens", "finish_reason", "character varying", 32, "YES"),
                ("processing_gens", "image_transport", "character varying", 16, "YES"),
                ("processing_gens", "message", "jsonb", None, "YES"),
                ("processing_gens", "native_finish_reason", "character varying", 32, "YES"),
                ("processing_gens", "prompt_tokens", "integer", None, "YES"),
                ("processing_gens", "reasoning_tokens", "integer", None, "YES"),
                ("processing_gens", "settled_character_count", "integer", None, "YES"),
                ("waiting_prompts", "chat_conformance", "character varying", None, "NO"),
                ("waiting_prompts", "chat_extras", "jsonb", None, "YES"),
                ("waiting_prompts", "chat_fault_rc", "character varying", 32, "YES"),
                ("waiting_prompts", "chat_fault_reason", "character varying", 1000, "YES"),
                ("waiting_prompts", "chat_messages_offloaded", "boolean", None, "NO"),
                ("waiting_prompts", "chat_required_features", "ARRAY", None, "YES"),
                ("waiting_prompts", "chat_tools_temperature", "double precision", None, "YES"),
                ("waiting_prompts", "context_adjustments", "jsonb", None, "YES"),
                ("waiting_prompts", "is_chat", "boolean", None, "NO"),
                ("waiting_prompts", "messages", "jsonb", None, "YES"),
                ("waiting_prompts", "operation_fingerprint", "character", 64, "YES"),
                ("waiting_prompts", "operation_key", "uuid", None, "YES"),
                ("workers", "accepts_chat_jobs", "boolean", None, "NO"),
                ("workers", "accepts_prompt_jobs", "boolean", None, "NO"),
                ("workers", "chat_features", "ARRAY", None, "YES"),
                ("workers", "chat_max_context_length", "integer", None, "YES"),
                ("workers", "chat_max_length", "integer", None, "YES"),
                ("workers", "chat_model_id", "character varying", None, "YES"),
                ("workers", "chat_tools_max_temperature", "double precision", None, "YES"),
                ("workers", "image_transports", "ARRAY", None, "YES"),
                ("workers", "nonconformant_replies", "integer", None, "NO"),
            ]

            existing_worker_opt_in = connection.execute(
                sqlalchemy.text("SELECT accepts_chat_jobs FROM workers WHERE id = :worker_id"),
                {"worker_id": worker_id},
            ).scalar_one()
            assert existing_worker_opt_in is False
            existing_worker_takes_text_jobs = connection.execute(
                sqlalchemy.text("SELECT accepts_prompt_jobs FROM workers WHERE id = :worker_id"),
                {"worker_id": worker_id},
            ).scalar_one()
            assert existing_worker_takes_text_jobs is True
            existing_request_is_chat = connection.execute(
                sqlalchemy.text("SELECT is_chat FROM waiting_prompts WHERE id = :request_id"),
                {"request_id": request_id},
            ).scalar_one()
            assert existing_request_is_chat is False
            existing_request_conformance = connection.execute(
                sqlalchemy.text("SELECT chat_conformance FROM waiting_prompts WHERE id = :request_id"),
                {"request_id": request_id},
            ).scalar_one()
            assert existing_request_conformance == "strict"

            operation_key_index = connection.execute(
                sqlalchemy.text(
                    """
                    SELECT pg_index.indisunique, pg_index.indisvalid, pg_get_indexdef(pg_index.indexrelid)
                    FROM pg_index
                    JOIN pg_class ON pg_class.oid = pg_index.indexrelid
                    WHERE pg_class.relname = 'ix_waiting_prompts_user_operation_key'
                    """,
                ),
            ).one()
            is_unique, is_valid, index_definition = operation_key_index
            assert is_unique is True
            assert is_valid is True
            assert "(user_id, operation_key)" in index_definition
            assert "WHERE (operation_key IS NOT NULL)" in index_definition

            for table_name in ("worker_blacklists", "text_worker_softprompts"):
                worker_id_index = connection.execute(
                    sqlalchemy.text(
                        """
                        SELECT pg_index.indisvalid, pg_get_indexdef(pg_index.indexrelid)
                        FROM pg_index
                        JOIN pg_class ON pg_class.oid = pg_index.indexrelid
                        WHERE pg_class.relname = :index_name
                        """,
                    ),
                    {"index_name": f"ix_{table_name}_worker_id"},
                ).one()
                is_valid, index_definition = worker_id_index
                assert is_valid is True
                assert f"ON public.{table_name} USING btree (worker_id)" in index_definition
    finally:
        engine.dispose()
