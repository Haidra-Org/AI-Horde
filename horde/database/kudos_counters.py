# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Atomic increments for kudos-derived counter dimensions."""

from __future__ import annotations

from collections.abc import Sequence
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
    increment_counters(model, [(dimensions, Decimal(str(delta)))])


def increment_counters(model: type[CounterModel], entries: Sequence[tuple[dict[str, object], float | Decimal]]) -> None:
    """Insert-or-increment a batch of uniquely constrained counter rows in one statement.

    Every entry must carry the same dimension keys, and each dimension tuple may
    appear at most once per call: an upsert cannot touch the same row twice in
    one statement. Callers that fold per-dimension delta maps satisfy both by
    construction.
    """
    if not entries:
        return
    table = model.__table__
    value_column = table.c.value
    insert = sqlite_insert(table) if SQLITE_MODE else postgresql_insert(table)
    statement = insert.values(
        [dimensions | {"value": round(Decimal(str(delta)), 2)} for dimensions, delta in entries],
    )
    statement = statement.on_conflict_do_update(
        index_elements=[table.c[name] for name in entries[0][0]],
        set_={"value": func.round(cast(value_column + statement.excluded.value, Numeric), 2)},
    )
    db.session.execute(statement)
