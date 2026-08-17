# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Upgrade-path coverage for the annotation capability migration."""

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
    database_name = f"annotation_migration_{uuid.uuid4().hex[:12]}"
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
    migration_path = Path(__file__).parents[2] / "sql_statements/5.1.6.txt"
    migration = migration_path.read_text(encoding="utf-8")
    for statement in sqlparse.split(migration, strip_semicolon=True):
        connection.exec_driver_sql(statement)


def test_annotation_capability_migration_upgrades_an_earlier_draft(migration_database: str) -> None:
    engine = sqlalchemy.create_engine(migration_database, isolation_level="AUTOCOMMIT")
    worker_id = uuid.uuid4()

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("CREATE TABLE workers (id UUID PRIMARY KEY)")
            connection.execute(sqlalchemy.text("INSERT INTO workers (id) VALUES (:worker_id)"), {"worker_id": worker_id})
            connection.exec_driver_sql(
                """
                CREATE TABLE interrogation_worker_annotation_types (
                    id SERIAL PRIMARY KEY,
                    worker_id UUID NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
                    annotation_type VARCHAR(64)
                )
                """,
            )
            connection.execute(
                sqlalchemy.text(
                    """
                    INSERT INTO interrogation_worker_annotation_types (worker_id, annotation_type)
                    VALUES (:worker_id, 'canny'), (:worker_id, 'canny'), (:worker_id, NULL)
                    """,
                ),
                {"worker_id": worker_id},
            )

            _run_migration(connection)
            _run_migration(connection)

            rows = connection.execute(
                sqlalchemy.text(
                    "SELECT annotation_type FROM interrogation_worker_annotation_types ORDER BY annotation_type",
                ),
            ).scalars()
            assert list(rows) == ["canny"]

            column = connection.execute(
                sqlalchemy.text(
                    """
                    SELECT is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'interrogation_worker_annotation_types'
                      AND column_name = 'annotation_type'
                    """,
                ),
            ).scalar_one()
            assert column == "NO"

            index = connection.execute(
                sqlalchemy.text(
                    """
                    SELECT indexrelid::regclass::text, indisunique, indisvalid
                    FROM pg_index
                    WHERE indexrelid = to_regclass('public.idx_interrogation_worker_annotation_types_worker_type')
                    """,
                ),
            ).one()
            assert index[1:] == (True, True)

            connection.exec_driver_sql("SET enable_seqscan = off")
            plan = connection.execute(
                sqlalchemy.text(
                    """
                    EXPLAIN (COSTS OFF)
                    SELECT annotation_type
                    FROM interrogation_worker_annotation_types
                    WHERE worker_id = :worker_id
                    """,
                ),
                {"worker_id": worker_id},
            ).scalars()
            assert "idx_interrogation_worker_annotation_types_worker_type" in "\n".join(plan)
    finally:
        engine.dispose()
