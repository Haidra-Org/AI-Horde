# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Async SQLAlchemy engine and Grid-owned table mappings."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings

metadata = sa.MetaData()

# ── Den ledger ──
# Durable, append-only record of den (work units) earned per completed job.
# The on-chain settlement bot rolls this up by wallet (or by worker→user
# mapping) over a period and pays out AIPG. Before this table, den was
# computed and sent to the worker but never persisted — so the payout system
# had nothing to pay against. This is grid_api-owned (not part of the horde
# schema) and created idempotently in init_database().
den_events_table = sa.Table(
    "grid_den_events",
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("job_id", UUID(as_uuid=True), index=True),
    sa.Column("worker_id", UUID(as_uuid=True), index=True),
    # Best-effort wallet captured at worker connect. May be empty; settlement
    # can fall back to resolving the worker's user→wallet at payout time.
    sa.Column("wallet_address", sa.String(64), index=True),
    sa.Column("model", sa.String(255)),
    sa.Column("den", sa.Float, default=0),
    sa.Column("output_tokens", sa.Integer, default=0),
    sa.Column("created", sa.DateTime, default=datetime.utcnow, index=True),
)


# ── Engine + session factory ──

_engine = None
_session_factory = None


async def init_database():
    """Initialize the async engine and session factory."""
    global _engine, _session_factory
    settings = get_settings()
    _engine = create_async_engine(
        settings.async_database_url,
        pool_size=20,
        max_overflow=10,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    # Create grid_api-owned tables idempotently. checkfirst + an explicit
    # table list means we only ever touch tables we own — never the horde
    # schema, which is managed separately. Safe to run on every boot.
    #
    # v2 tables: Alembic is the canonical migration path (alembic upgrade
    # head), but create_all here keeps a fresh boot working without a manual
    # step — identical DDL, checkfirst, grid_-namespaced only.
    from .v2.schema import metadata as v2_metadata

    async with _engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: metadata.create_all(
                sync_conn, tables=[den_events_table], checkfirst=True
            )
        )
        await conn.run_sync(lambda sync_conn: v2_metadata.create_all(sync_conn, checkfirst=True))


async def close_database():
    """Dispose of the engine connection pool."""
    global _engine
    if _engine:
        await _engine.dispose()


async def get_session() -> AsyncSession:
    """FastAPI dependency that yields an async session."""
    async with _session_factory() as session:
        yield session


async def new_session() -> AsyncSession:
    """Create a standalone session (for use outside of Depends)."""
    return _session_factory()
