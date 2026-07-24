# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed database primitives used by kudos accounting.

PostgreSQL advisory locks and transaction isolation are intentionally confined
to this module. Callers express the accounting operation they need without raw
SQL, dialect function names, or lock-key literals.
"""

from __future__ import annotations

from sqlalchemy import func, select

from horde.flask import SQLITE_MODE, db

KUDOS_APPLIER_LOCK = 0x4B55444F  # "KUDO"
KUDOS_PAYER_LOCK_NAMESPACE = 0x4B554452  # "KUDR"
KUDOS_RECONCILIATION_LOCK = 0x4B55445245434F  # "KUDRECO"


def try_acquire_applier_lock() -> bool:
    """Try to own the kudos projector for the current transaction."""
    if SQLITE_MODE:
        return True
    statement = select(func.pg_try_advisory_xact_lock(KUDOS_APPLIER_LOCK))
    return bool(db.session.execute(statement).scalar_one())


def acquire_applier_lock() -> None:
    """Wait to own the kudos projector for the current transaction."""
    _acquire_transaction_lock(KUDOS_APPLIER_LOCK)


def acquire_payer_lock(user_id: int) -> None:
    """Serialize accounting admission for one payer in this transaction."""
    if SQLITE_MODE:
        return
    statement = select(func.pg_advisory_xact_lock(KUDOS_PAYER_LOCK_NAMESPACE, user_id))
    db.session.execute(statement).scalar_one()


def acquire_reconciliation_lock() -> None:
    """Serialize compensating-repair emission in this transaction."""
    _acquire_transaction_lock(KUDOS_RECONCILIATION_LOCK)


def begin_repeatable_read() -> None:
    """Start a clean repeatable-read ORM transaction for an online snapshot."""
    session = db.session()
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("Snapshot and reconciliation commands require a clean ORM session")
    if session.in_transaction():
        session.rollback()
    if not SQLITE_MODE:
        # SQLAlchemy must set isolation before the DBAPI transaction begins.
        # Acquiring the connection with execution options does that without a
        # textual SET TRANSACTION statement.
        session.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def _acquire_transaction_lock(lock_key: int) -> None:
    """Acquire one PostgreSQL transaction-scoped advisory lock."""
    if SQLITE_MODE:
        return
    statement = select(func.pg_advisory_xact_lock(lock_key))
    db.session.execute(statement).scalar_one()
