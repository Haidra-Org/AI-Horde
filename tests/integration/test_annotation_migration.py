# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Upgrade-path coverage for the annotation capability migration."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name

pytestmark = pytest.mark.integration


def _migration_statements(schema_name: str) -> list[str]:
    repository_root = Path(__file__).parents[2]
    migration = (repository_root / "sql_statements/5.1.6.txt").read_text(encoding="utf-8")
    migration = migration.replace("public.workers", f'"{schema_name}".workers').replace(
        "public.interrogation_worker_annotation_types",
        f'"{schema_name}".interrogation_worker_annotation_types',
    )
    migration = "\n".join(line for line in migration.splitlines() if not line.lstrip().startswith("--"))
    return [statement.strip() for statement in migration.split(";") if statement.strip()]


def _run_migration(connection, schema_name: str) -> None:
    for statement in _migration_statements(schema_name):
        connection.exec_driver_sql(statement)


def test_annotation_capability_migration_upgrades_an_earlier_draft(pg_dsn: str) -> None:
    schema_name = new_test_schema_name("annotation_migration_test")
    create_schema(pg_dsn, schema_name)
    engine = sqlalchemy.create_engine(pg_dsn, isolation_level="AUTOCOMMIT")
    worker_id = uuid.uuid4()

    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'SET search_path TO "{schema_name}"')
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

            _run_migration(connection, schema_name)
            _run_migration(connection, schema_name)

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
                    WHERE table_schema = :schema_name
                      AND table_name = 'interrogation_worker_annotation_types'
                      AND column_name = 'annotation_type'
                    """,
                ),
                {"schema_name": schema_name},
            ).scalar_one()
            assert column == "NO"

            index = connection.execute(
                sqlalchemy.text(
                    """
                    SELECT indexrelid::regclass::text, indisunique, indisvalid
                    FROM pg_index
                    WHERE indexrelid = to_regclass(:index_name)
                    """,
                ),
                {"index_name": f"{schema_name}.idx_interrogation_worker_annotation_types_worker_type"},
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
        drop_schema(pg_dsn, schema_name)
