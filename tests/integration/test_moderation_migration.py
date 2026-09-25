# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the 5.1.12 DDL is additive and repeatable against a pre-change table."""

from pathlib import Path

import sqlalchemy
import sqlparse

from tests.dependency_runtime import create_schema, drop_schema, new_test_schema_name


def test_migration_is_additive_and_repeatable(pg_dsn: str) -> None:
    schema_name = new_test_schema_name("horde_5_1_12_migration")
    create_schema(pg_dsn, schema_name)
    engine = sqlalchemy.create_engine(
        pg_dsn,
        isolation_level="AUTOCOMMIT",
        connect_args={"options": f"-c search_path={schema_name}"},
    )
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("CREATE TABLE waiting_prompts (id UUID PRIMARY KEY, sharedkey_id UUID, prompt TEXT)"))
            connection.execute(
                sqlalchemy.text(
                    "INSERT INTO waiting_prompts (id, prompt) VALUES ('00000000-0000-0000-0000-000000000001', 'effective')",
                )
            )
            script = (Path(__file__).resolve().parents[2] / "sql_statements" / "5.1.12.txt").read_text()
            for _ in range(2):
                for statement in sqlparse.split(script):
                    connection.execute(sqlalchemy.text(statement))
            row = connection.execute(sqlalchemy.text("SELECT prompt, submitted_prompt FROM waiting_prompts")).one()
            assert row == ("effective", None)
            indexes = {index["name"]: index for index in sqlalchemy.inspect(connection).get_indexes("waiting_prompts")}
            assert "ix_waiting_prompts_sharedkey_id" in indexes
            sharedkey_index = indexes["ix_waiting_prompts_sharedkey_id"]
            assert sharedkey_index["column_names"] == ["sharedkey_id"]
            predicate = sharedkey_index.get("dialect_options", {}).get("postgresql_where")
            assert predicate is not None
            assert "sharedkey_id IS NOT NULL" in str(predicate)
            assert {"prompt_moderation_events", "prompt_moderation_reviews"} <= set(
                sqlalchemy.inspect(connection).get_table_names(),
            )
    finally:
        engine.dispose()
        drop_schema(pg_dsn, schema_name)
