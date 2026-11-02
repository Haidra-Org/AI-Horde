# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Define SQL expressions that compile differently per dialect, so model DDL and queries stay valid on SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement


class UtcNow(FunctionElement[datetime]):
    """Represent the current naive UTC time as a server default, compiled per dialect.

    The timestamp columns hold naive UTC like ``datetime.utcnow``. PostgreSQL's bare ``now()`` would store the
    session's time zone, and ``timezone('utc', ...)`` does not exist on SQLite, whose ``CURRENT_TIMESTAMP`` is
    already UTC.
    """

    type = DateTime()
    """The SQL type of the expression, so a column default built from it is a timestamp."""
    inherit_cache = True
    """Let SQLAlchemy cache statements that use the expression, which has no parameters."""


@compiles(UtcNow)
def _compile_utc_now(element: UtcNow, compiler: SQLCompiler, **kw: Any) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(UtcNow, "postgresql")
def _compile_utc_now_postgresql(element: UtcNow, compiler: SQLCompiler, **kw: Any) -> str:
    return "timezone('utc', now())"


class TextArrayContainsAll(FunctionElement[bool]):
    """Represent whether a text array holds every element of another, compiled per dialect.

    The operands are text arrays as ``text_array_column_type`` stores them: a ``text[]`` on PostgreSQL and a JSON
    array on SQLite. A NULL operand counts as an empty array, so a NULL or empty required array is contained in any
    array, and a NULL container holds only an empty one. ``TextArrayContainsAll(chat_features, required)`` is true
    for ``["seed", "tools"]`` and ``["tools"]``, and false for ``["seed"]`` and ``["tools"]``.
    """

    type = Boolean()
    """The SQL type of the expression, so it can stand as a filter condition."""
    inherit_cache = True
    """Let SQLAlchemy cache statements that use the expression; its operands are bound or column clauses."""


@compiles(TextArrayContainsAll)
def _compile_text_array_contains_all(element: TextArrayContainsAll, compiler: SQLCompiler, **kw: Any) -> str:
    container, contained = (_sqlite_json_array(compiler.process(argument, **kw)) for argument in element.clauses.clauses)
    return (
        f"NOT EXISTS (SELECT 1 FROM json_each({contained}) AS required_element "
        f"WHERE required_element.value NOT IN (SELECT held_element.value FROM json_each({container}) AS held_element))"
    )


def _sqlite_json_array(operand: str) -> str:
    """Return SQLite text that reads ``operand`` as a JSON array, an empty one unless it holds an array.

    A JSON column stores Python None as the JSON text ``null`` unless the column sets ``none_as_null``, and
    ``json_each`` yields one row with a NULL value for ``null``. A NULL among the held elements makes every ``NOT IN``
    test NULL, so no required element would count as missing. A SQL NULL or a JSON ``null`` is therefore read as
    ``[]``.
    """
    return f"(CASE WHEN json_type({operand}) = 'array' THEN {operand} ELSE '[]' END)"


@compiles(TextArrayContainsAll, "postgresql")
def _compile_text_array_contains_all_postgresql(element: TextArrayContainsAll, compiler: SQLCompiler, **kw: Any) -> str:
    container, contained = (compiler.process(argument, **kw) for argument in element.clauses.clauses)
    return f"(coalesce({container}, '{{}}'::text[]) @> coalesce({contained}, '{{}}'::text[]))"
