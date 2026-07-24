# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Atomic increments for kudos-derived counter dimensions."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from sqlalchemy import Numeric, Table, cast, func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from horde.flask import SQLITE_MODE, db


class CounterModel(Protocol):
    """Structural type for a mapped counter model."""

    __table__: Table


def increment_counter(model: type[CounterModel], dimensions: dict[str, object], delta: float | Decimal) -> None:
    """Atomically insert or increment one uniquely constrained counter row."""
    table = model.__table__
    value_column = table.c.value
    insert = sqlite_insert(table) if SQLITE_MODE else postgresql_insert(table)
    statement = insert.values(**dimensions, value=round(Decimal(str(delta)), 2))
    statement = statement.on_conflict_do_update(
        index_elements=[table.c[name] for name in dimensions],
        set_={"value": func.round(cast(value_column + statement.excluded.value, Numeric), 2)},
    )
    db.session.execute(statement)
