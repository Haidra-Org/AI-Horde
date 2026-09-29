# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Define SQL expressions that compile differently per dialect, so model DDL stays valid on SQLite and PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
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
