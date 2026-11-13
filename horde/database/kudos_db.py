# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed database primitives used by kudos accounting.

PostgreSQL advisory locks and transaction isolation are intentionally confined
to this module. Callers express the accounting operation they need without raw
SQL, dialect function names, or lock-key literals.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Final

from sqlalchemy import Connection, func, select

from horde.flask import SQLITE_MODE, db

KUDOS_APPLIER_LOCK = 0x4B55444F  # "KUDO"
KUDOS_PAYER_LOCK_NAMESPACE = 0x4B554452  # "KUDR"
KUDOS_RECONCILIATION_LOCK = 0x4B55445245434F  # "KUDRECO"
KUDOS_MODE_GATE_LOCK = 0x4B55444D  # "KUDM"
KUDOS_RETENTION_LOCK = 0x4B5544524554  # "KUDRET"

RETENTION_LOCK_RETRY_INTERVAL: Final[timedelta] = timedelta(milliseconds=250)
"""How long ``acquire_retention_lock_within`` sleeps between attempts.

A tick without a step releases the lock in well under a second. With a few attempts per second, the lock is taken soon
after that release.
"""


def try_acquire_applier_lock() -> bool:
    """Try to own the kudos projector for the current transaction."""
    if SQLITE_MODE:
        return True
    statement = select(func.pg_try_advisory_xact_lock(KUDOS_APPLIER_LOCK))
    return bool(db.session.execute(statement).scalar_one())


def try_acquire_retention_lock(*, connection: Connection | None = None) -> bool:
    """Try to own the kudos retention job for the database session of ``connection``.

    The lock is session-level, unlike the other kudos locks. A retention tick commits several transactions and can
    spend minutes on one archive export, so a transaction-scoped lock would need a connection left idle in transaction
    for that long, which ``idle_in_transaction_session_timeout`` terminates where it is set. The job takes this lock
    on a dedicated autocommit connection and releases it with ``release_retention_lock``; closing the connection
    releases it too.

    Args:
        connection: The connection whose database session holds the lock; the ORM session's connection when None.

    Returns:
        True when the session now holds the lock.
    """
    if SQLITE_MODE:
        return True
    statement = select(func.pg_try_advisory_lock(KUDOS_RETENTION_LOCK))
    if connection is None:
        return bool(db.session.execute(statement).scalar_one())
    return bool(connection.execute(statement).scalar_one())


def acquire_retention_lock_within(*, connection: Connection, wait: timedelta) -> bool:
    """Try to own the kudos retention job for the session of ``connection``, retrying until ``wait`` has passed.

    A job that needs the lock only briefly uses this, so that a tick holding the lock for a moment does not make the job
    skip its run. The lock is session-level, as with ``try_acquire_retention_lock``.

    Args:
        connection: A dedicated autocommit connection whose database session takes the lock.
        wait: How long to keep retrying. Zero makes a single attempt.

    Returns:
        True when the session holds the lock, False when another session held it for the whole wait.
    """
    deadline = time.monotonic() + wait.total_seconds()
    while not try_acquire_retention_lock(connection=connection):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(RETENTION_LOCK_RETRY_INTERVAL.total_seconds(), remaining))
    return True


def release_retention_lock(*, connection: Connection | None = None) -> bool:
    """Release the session-level kudos retention lock taken by ``try_acquire_retention_lock``.

    Args:
        connection: The connection whose database session holds the lock; the ORM session's connection when None.

    Returns:
        True when the session held the lock.
    """
    if SQLITE_MODE:
        return True
    statement = select(func.pg_advisory_unlock(KUDOS_RETENTION_LOCK))
    if connection is None:
        return bool(db.session.execute(statement).scalar_one())
    return bool(connection.execute(statement).scalar_one())


def acquire_applier_lock() -> None:
    """Wait to own the kudos projector for the current transaction."""
    _acquire_transaction_lock(KUDOS_APPLIER_LOCK)


def acquire_payer_lock(user_id: int) -> None:
    """Serialize accounting admission for one payer in this transaction."""
    if SQLITE_MODE:
        return
    statement = select(func.pg_advisory_xact_lock(KUDOS_PAYER_LOCK_NAMESPACE, user_id))
    db.session.execute(statement).scalar_one()


def acquire_mode_pin() -> None:
    """Pin the observed ledger mode for the current transaction.

    The mode gate is a shared/exclusive advisory lock rather than a control-row
    lock because heavyweight locks queue fairly: a pin requested while a
    transition is waiting queues behind that waiter. A row-level ``FOR KEY
    SHARE`` acquired against a queued ``FOR UPDATE`` instead takes the
    no-conflict fast path, so gapless writer traffic starves the transition.
    """
    if SQLITE_MODE:
        return
    statement = select(func.pg_advisory_xact_lock_shared(KUDOS_MODE_GATE_LOCK))
    db.session.execute(statement).scalar_one()


def acquire_mode_gate_exclusive() -> None:
    """Wait out every pinned-mode transaction and block new pins.

    Shared holders present when this call queues drain normally; pin requests
    arriving afterwards wait behind it, so acquisition is bounded by the
    longest in-flight mutation transaction rather than by writer arrival rate.
    """
    _acquire_transaction_lock(KUDOS_MODE_GATE_LOCK)


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
