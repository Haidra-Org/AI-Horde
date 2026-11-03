# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Append-only kudos ledger: model, emission primitives, and event grouping.

Every kudos movement is recorded here as one signed posting against exactly one
balance. A single asynchronous applier (:mod:`horde.database.kudos_ledger`)
folds these postings into the materialized balance columns, so no interactive
transaction contends on the hot ``users``/``workers`` rows.

Postings produced by one business event share an ``event_id``, minted by the
:func:`kudos_event` context manager wrapping that event. Emissions outside such
a context each get their own event id.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from loguru import logger
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import and_, column, false, true

from horde.enums import (
    KudosAuditDetail,
    KudosEntryType,
    KudosIdempotencyKind,
    KudosLedgerMode,
    KudosRetentionDataset,
    KudosRetentionState,
    KudosStatEventQuarantineReason,
    KudosUnit,
)
from horde.flask import db

if TYPE_CHECKING:
    from horde.classes.base.user import User

type KudosAmount = Decimal | int | float
type KudosAuditMetadata = dict[str, object]
_json_type = JSON().with_variant(JSONB(), "postgresql")
KUDOS_STAT_ACTION_MAX_LENGTH: Final[int] = 20
KUDOS_STAT_RECORD_MAX_LENGTH: Final[int] = 30


@dataclass(frozen=True)
class _KudosEvent:
    """Grouping token shared by every posting of one business event."""

    event_id: uuid.UUID
    job_id: uuid.UUID | None = None
    wp_type: str | None = None


_current_kudos_event: ContextVar[_KudosEvent | None] = ContextVar("current_kudos_event", default=None)


@contextlib.contextmanager
def kudos_event(
    job_id: uuid.UUID | str | None = None,
    wp_type: str | None = None,
    *,
    idempotency_key: str | None = None,
) -> Iterator[_KudosEvent]:
    """Group every kudos posting emitted in the block under one event id.

    Wrap the top of a business event (a settlement, activation, transfer, ...).
    Nested calls stack: the previous event is restored on exit, so an inner
    event (e.g. a trust promotion triggered mid-settlement) stays distinct.

    Args:
        job_id: Optional procgen/job correlation id stamped on each posting.
        wp_type: Optional ``image``/``text``/``interrogation`` context.
        idempotency_key: Stable external retry key. Reusing it produces the
            same event UUID; omit it for a fresh event.
    """
    event_id = uuid.uuid4() if idempotency_key is None else kudos_event_id(idempotency_key)
    event = _KudosEvent(event_id=event_id, job_id=_coerce_uuid(job_id), wp_type=wp_type)
    token = _current_kudos_event.set(event)
    try:
        yield event
    finally:
        _current_kudos_event.reset(token)


def kudos_event_id(idempotency_key: str) -> uuid.UUID:
    """Return the stable event UUID for an externally meaningful retry key."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aihorde-kudos:{idempotency_key}")


def current_kudos_event() -> _KudosEvent | None:
    """Return the active business-event token, if one is being emitted."""
    return _current_kudos_event.get()


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


@dataclass(frozen=True)
class StyleReward:
    """The style surcharge an activation debit includes, and the style author it is owed to.

    The surcharge is collected through the requester's activation debit, and the minimum-balance floor can
    forgive part or all of that debit. The author is credited only what the debit collected, with the
    forgiven amount attributed to the surcharge first.
    """

    author_id: int
    """The user id of the style's author."""
    amount: Decimal
    """The surcharge the activation debit includes."""
    style_type: str
    """The style's type (``text`` or ``image``), which is the record the author's style count is kept under."""

    def audit_detail(self) -> KudosAuditMetadata:
        """Return the activation debit's detail entries that carry this reward to the applier.

        Returns:
            The detail entries, keyed by :class:`~horde.enums.KudosAuditDetail` members.
        """
        return {
            KudosAuditDetail.STYLE_AUTHOR_ID: self.author_id,
            KudosAuditDetail.STYLE_SURCHARGE: str(self.amount),
            KudosAuditDetail.STYLE_TYPE: self.style_type,
        }

    @classmethod
    def from_audit_detail(cls, detail: Mapping[str, object] | None) -> StyleReward | None:
        """Read the reward a posting's detail carries.

        Args:
            detail: The posting's audit detail.

        Returns:
            The reward, or None when the posting carries none.
        """
        if not detail:
            return None
        author_id = detail.get(KudosAuditDetail.STYLE_AUTHOR_ID)
        if author_id is None:
            return None
        return cls(
            author_id=int(str(author_id)),
            amount=Decimal(str(detail[KudosAuditDetail.STYLE_SURCHARGE])),
            style_type=str(detail[KudosAuditDetail.STYLE_TYPE]),
        )

    def count_outcome(self, credit: Decimal) -> None:
        """Count the part of the surcharge credited to the author and the part the floor forgave.

        Args:
            credit: The part of the surcharge the author is credited.
        """
        from horde import metrics

        attributes = {"horde.style.type": self.style_type}
        if credit > 0:
            metrics.style_author_credits.add(float(credit), attributes)
        forgiven = self.amount - credit
        if forgiven > 0:
            metrics.style_forgiven_surcharges.add(float(forgiven), attributes)

    def collected(self, forgiven: Decimal) -> Decimal:
        """Return the part of the surcharge the debit collected.

        Args:
            forgiven: The floor-forgiven amount not yet attributed to another surcharge of the same
                requester. The forgiven amount is attributed to the surcharge first, so up to the whole
                surcharge is absorbed before any of the base charge is.

        Returns:
            The surcharge minus the part of it the floor absorbed, never negative.
        """
        absorbed = min(self.amount, max(forgiven, Decimal(0)))
        return self.amount - absorbed


def _kudos_entry_type() -> Enum:
    """Create the column type every table storing a :class:`~horde.enums.KudosEntryType` uses."""
    # Production built the event tables from sql_statements/5.1.0.txt with VARCHAR(32), wider than the longest value.
    return Enum(
        KudosEntryType,
        native_enum=False,
        length=32,
        values_callable=lambda entries: [entry.value for entry in entries],
    )


def _kudos_unit() -> Enum:
    """Create the column type every table storing a :class:`~horde.enums.KudosUnit` uses."""
    # Production built kudos_stat_events from sql_statements/5.1.0.txt with VARCHAR(16), wider than the longest value.
    return Enum(KudosUnit, native_enum=False, length=16, values_callable=lambda units: [unit.value for unit in units])


class KudosLedger(db.Model):  # type: ignore[name-defined,misc]
    """One signed currency posting against one required user balance.

    ``escrow`` selects the user's evaluation balance; otherwise the posting
    targets their spendable balance. Worker display kudos and every non-currency
    counter belong exclusively to :class:`KudosStatEvent`, so a ledger row can
    never be mistaken for another unit or target type.

    ``applied`` is the per-row consumption flag of a single-consumer work queue:
    the applier claims rows still flagged unapplied, folds them, and flips the
    flag in the same transaction, so a row is folded exactly once regardless of
    id order or when its inserting transaction became visible.
    """

    __tablename__ = "kudos_ledger"
    __table_args__ = (
        CheckConstraint("amount <> 'NaN'", name="kudos_ledger_amount_not_nan"),
        # The applier claims rows still flagged unapplied (single-consumer work
        # queue). This partial index is exactly that hot working set, ordered for
        # the id-ordered claim scan; it shrinks to near-empty once the applier
        # catches up, so the scan stays cheap.
        # The predicate is spelled as sql_statements/5.1.0.txt builds it, because a partitioned index
        # attaches only an identically defined index of the pre-partitioning table.
        Index("ix_kudos_ledger_unapplied", "id", postgresql_where=text("NOT applied")),
        # The applier's evaluation drain asks, every cycle, for the distinct users holding an unapplied posting
        # of one entry type. This covers that predicate directly; without it the scan walks the whole unapplied
        # set and its heap. Existing deployments add it with sql_statements/5.1.10.txt.
        Index(
            "ix_kudos_ledger_unapplied_users",
            "entry_type",
            "user_id",
            postgresql_where=column("applied").is_(false()),
        ),
        # available_kudos() sums a payer's unapplied debits on every admission. The plain user_id index holds the
        # payer's entire history, so this covers only the pending rows. Existing deployments add it with
        # sql_statements/5.1.11.txt.
        Index("ix_kudos_ledger_unapplied_by_user", "user_id", postgresql_where=column("applied").is_(false())),
        # Retention removes whole partitions by age (ADR 42), so the table is ranged on ``created``. PostgreSQL
        # requires the partition key in every unique constraint, hence the (id, created) primary key, and range
        # pruning replaces the former index on ``created``. See ensure_kudos_event_partitions().
        {"postgresql_partition_by": "RANGE (created)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # The server defaults are the ones sql_statements/5.1.0.txt gave production, so both DDL sources agree.
    created: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        server_default=func.now(),
        nullable=False,
        primary_key=True,
    )
    # Retries look the event up in kudos_idempotency_keys, so the postings need no event_id index.
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    entry_type: Mapped[KudosEntryType] = mapped_column(_kudos_entry_type(), nullable=False)
    # Users are soft-deleted/wiped rather than removed. RESTRICT makes audit
    # ownership explicit and prevents an accidental hard delete from orphaning
    # authoritative currency history.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    user: Mapped[User] = relationship()
    escrow: Mapped[bool] = mapped_column(default=False, server_default=false(), nullable=False)
    # Kudos and counter quantities are decimal accounting values.  A binary
    # float makes replay depend on grouping and platform rounding, which is not
    # acceptable for an authoritative movement log.
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    applied: Mapped[bool] = mapped_column(default=False, server_default=false(), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    wp_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detail: Mapped[KudosAuditMetadata | None] = mapped_column(_json_type, nullable=True)


class KudosStatEventColumns:
    """Declare the counter-event columns shared by live statistic events and quarantined ones.

    Retiring a partition moves its quarantined events unchanged into ``kudos_stat_quarantine``, so both tables
    need the same column types. ``id`` and ``created`` form the key on one table and not the other, so each model
    declares them.
    """

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    entry_type: Mapped[KudosEntryType] = mapped_column(_kudos_entry_type(), nullable=False)
    # These are immutable audit references rather than ownership foreign keys.
    # Workers and teams are hard-deleted, but their counter history must survive.
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    wp_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    unit: Mapped[KudosUnit] = mapped_column(_kudos_unit(), nullable=False)
    stat_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[KudosAuditMetadata | None] = mapped_column(_json_type, nullable=True)
    applied: Mapped[bool] = mapped_column(default=False, server_default=false(), nullable=False)
    quarantined: Mapped[bool] = mapped_column(default=False, server_default=false(), nullable=False)
    quarantine_reason: Mapped[KudosStatEventQuarantineReason | None] = mapped_column(
        Enum(
            KudosStatEventQuarantineReason,
            native_enum=False,
            length=64,
            values_callable=lambda reasons: [reason.value for reason in reasons],
        ),
        nullable=True,
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KudosStatEvent(KudosStatEventColumns, db.Model):  # type: ignore[name-defined,misc]
    """Non-currency counter event projected alongside a kudos batch."""

    __tablename__ = "kudos_stat_events"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND worker_id IS NULL) OR (user_id IS NULL AND worker_id IS NOT NULL)",
            name="kudos_stat_event_exactly_one_target",
        ),
        CheckConstraint("amount <> 'NaN'", name="kudos_stat_event_amount_not_nan"),
        Index(
            "ix_kudos_stat_events_drainable",
            "id",
            postgresql_where=column("applied").is_(false()) & column("quarantined").is_(false()),
        ),
        # kudos_applier_health() aggregates the quarantined rows (count, oldest created, newest quarantined_at)
        # every applier tick. Quarantine is rare, so this stays tiny, and without it that aggregate has no usable
        # index on an unboundedly growing table. Existing deployments add it with sql_statements/5.1.10.txt.
        Index(
            "ix_kudos_stat_events_quarantined",
            "created",
            "quarantined_at",
            postgresql_where=column("quarantined").is_(true()),
        ),
        # Declared here rather than on the shared columns so the quarantine table does not inherit them.
        Index("ix_kudos_stat_events_event_id", "event_id"),
        # Partial, as sql_statements/5.1.0.txt builds them. A plain index would also hold an entry for
        # every event without that target.
        Index("ix_kudos_stat_events_user_id", "user_id", postgresql_where=column("user_id").isnot(None)),
        Index("ix_kudos_stat_events_worker_id", "worker_id", postgresql_where=column("worker_id").isnot(None)),
        Index("ix_kudos_stat_events_team_id", "team_id", postgresql_where=column("team_id").isnot(None)),
        # Partitioned by ``created`` for the same reason as kudos_ledger.
        {"postgresql_partition_by": "RANGE (created)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        server_default=func.now(),
        nullable=False,
        primary_key=True,
    )


class KudosLedgerApplierState(db.Model):  # type: ignore[name-defined,misc]
    """Single-row lag heartbeat for the kudos applier.

    ``applied_at`` is stamped every applier cycle so a stalled applier surfaces as
    growing lag rather than a silent balance freeze. It carries no correctness
    role: exactly-once folding is enforced by the per-row ``KudosLedger.applied``
    flag, not by a watermark.
    """

    __tablename__ = "kudos_ledger_applier_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KudosLedgerControl(db.Model):  # type: ignore[name-defined,misc]
    """Single-row online cutover control for shadow versus ledger ownership."""

    __tablename__ = "kudos_ledger_control"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    mode: Mapped[KudosLedgerMode] = mapped_column(
        Enum(KudosLedgerMode, native_enum=False, values_callable=lambda modes: [mode.value for mode in modes]),
        nullable=False,
        default=KudosLedgerMode.SHADOW,
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        server_default=func.now(),
        nullable=False,
    )


class KudosReservation(db.Model):  # type: ignore[name-defined,misc]
    """A single-payer hold protecting eventual ledger debits from overspend.

    Reservations are deliberately separate from ``users`` so accepting a spend
    never joins the existing users-row lock graph.  Writers serialize on one
    payer-scoped Postgres advisory lock; no operation locks a recipient.
    """

    __tablename__ = "kudos_reservations"
    __table_args__ = (
        db.UniqueConstraint("business_id", name="uq_kudos_reservations_business_id"),
        CheckConstraint("original_amount > 0", name="kudos_reservation_original_positive"),
        CheckConstraint("remaining_amount >= 0", name="kudos_reservation_remaining_nonnegative"),
        Index(
            "ix_kudos_reservations_active_user",
            "user_id",
            postgresql_where=and_(column("released_at").is_(None), column("remaining_amount") > 0),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    business_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user: Mapped[User] = relationship()
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    original_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    remaining_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KudosBalanceSnapshot(db.Model):  # type: ignore[name-defined,misc]
    """Per-user reconciliation baseline including the visible applied sums."""

    __tablename__ = "kudos_balance_snapshots"
    __table_args__ = (
        db.UniqueConstraint("snapshot_id", "user_id", name="uq_kudos_balance_snapshot_user"),
        db.Index("ix_kudos_balance_snapshots_created", "created"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user: Mapped[User] = relationship()
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    escrow: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    applied_balance_total: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)
    applied_escrow_total: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class KudosStatQuarantine(KudosStatEventColumns, db.Model):  # type: ignore[name-defined,misc]
    """Represents a quarantined statistic event moved out of a retired ``kudos_stat_events`` partition.

    Quarantined events are not folded into rollups (ADR 14 keeps them as evidence), so retention moves them here
    unchanged in the same transaction as the partition's rollup. Repair of a quarantined event still appends a new
    event, and the row here stays for review.
    """

    # ADR 7 lifetime: audit references. The user, worker and team columns carry no foreign key for the same reason
    # as on kudos_stat_events, and the row has no expiry.
    __tablename__ = "kudos_stat_quarantine"
    __table_args__ = (
        Index("ix_kudos_stat_quarantine_event_id", "event_id"),
        Index("ix_kudos_stat_quarantine_source_range", "source_range"),
    )

    # The original event id. A sequence would break the link to the event's other postings and any archive dump.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_range: Mapped[str] = mapped_column(String(128), nullable=False)


class KudosStatRollup(db.Model):  # type: ignore[name-defined,misc]
    """Represents the statistic events of one UTC day and one attribution key, folded into aggregates.

    The aggregates reproduce every statistic projection: counters and contributions fold by ``amount_sum``, user
    records by ``amount_max`` and last activity by ``created_max``. One source partition can contribute rows to a
    day another partition also covers, so readers sum across ``source_range``.
    """

    # ADR 7 lifetime: authoritative history of the statistics it replaces, with the attribution columns kept as
    # audit references. Workers and teams are hard-deleted, so none of them is a foreign key.
    __tablename__ = "kudos_stat_rollups"
    __table_args__ = (
        # A retried rollup deletes its source_range first and inserts again. The key includes nullable attribution
        # columns, so NULLs must compare equal or a retry that skipped the delete would double-count.
        UniqueConstraint(
            "day",
            "entry_type",
            "unit",
            "stat_action",
            "record",
            "wp_type",
            "user_id",
            "worker_id",
            "worker_user_id",
            "team_id",
            "source_range",
            name="uq_kudos_stat_rollups_key",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_kudos_stat_rollups_user_day", "user_id", "day"),
        Index("ix_kudos_stat_rollups_worker_day", "worker_id", "day"),
        Index("ix_kudos_stat_rollups_source_range", "source_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    entry_type: Mapped[KudosEntryType] = mapped_column(_kudos_entry_type(), nullable=False)
    unit: Mapped[KudosUnit] = mapped_column(_kudos_unit(), nullable=False)
    stat_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record: Mapped[str | None] = mapped_column(String(32), nullable=True)
    wp_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    worker_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    amount_sum: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_min: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    amount_max: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    created_max: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_range: Mapped[str] = mapped_column(String(128), nullable=False)


class KudosLedgerCheckpoint(db.Model):  # type: ignore[name-defined,misc]
    """Represents a user's cumulative applied balance and escrow totals through a retired partition's upper bound.

    Totals exclude ``RECONCILIATION`` postings, as reconciliation does. Reconciliation reads the latest checkpoint
    plus the applied online postings at or above its ``boundary``. A boundary with no contributing postings before it
    has no rows here, so the retention catalog, not this table, records which boundaries exist.
    """

    # ADR 7 lifetime: authoritative history, since it replaces the postings it sums. The key cascades because a
    # checkpoint has no meaning without its account. Accounts are wiped rather than deleted, and the RESTRICT key on
    # kudos_ledger already blocks the delete of an account with online postings.
    __tablename__ = "kudos_ledger_checkpoints"
    __table_args__ = (
        UniqueConstraint("boundary", "user_id", name="uq_kudos_ledger_checkpoints_boundary_user"),
        Index("ix_kudos_ledger_checkpoints_boundary", "boundary"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    boundary: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    balance_total: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)
    escrow_total: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)


class KudosLedgerDailyStatement(db.Model):  # type: ignore[name-defined,misc]
    """Represents one user's summed postings of one balance and entry type on one UTC day of a retired partition.

    The ledger administration command reads it for disputes older than the online window. A UTC day can straddle
    two partitions, so readers sum across ``source_range``.
    """

    # ADR 7 lifetime: authoritative history, cascading with the account for the same reason as
    # KudosLedgerCheckpoint.
    __tablename__ = "kudos_ledger_daily"
    __table_args__ = (
        UniqueConstraint("user_id", "escrow", "entry_type", "day", "source_range", name="uq_kudos_ledger_daily_key"),
        Index("ix_kudos_ledger_daily_user_day", "user_id", "day"),
        Index("ix_kudos_ledger_daily_source_range", "source_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    escrow: Mapped[bool] = mapped_column(nullable=False)
    entry_type: Mapped[KudosEntryType] = mapped_column(_kudos_entry_type(), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    amount_sum: Mapped[Decimal] = mapped_column(Numeric(30, 2), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_range: Mapped[str] = mapped_column(String(128), nullable=False)


class KudosIdempotencyKey(db.Model):  # type: ignore[name-defined,misc]
    """Represents the scoped retry key of one transfer or repair posting set, with the parties and amount it posted.

    Transfer and repair retries read this row instead of searching ``kudos_ledger`` by ``event_id``, so the check
    keeps working after the postings leave the online window.
    """

    # ADR 7 lifetime: audit references that expire. Keys live for the online window and are deleted by the
    # retention job, so neither party is a foreign key.
    __tablename__ = "kudos_idempotency_keys"
    __table_args__ = (Index("ix_kudos_idempotency_keys_created", "created"),)

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[KudosIdempotencyKind] = mapped_column(
        Enum(
            KudosIdempotencyKind,
            native_enum=False,
            length=32,
            values_callable=lambda kinds: [kind.value for kind in kinds],
        ),
        nullable=False,
    )
    source_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dest_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class KudosRetentionUnit(db.Model):  # type: ignore[name-defined,misc]
    """Represents one retention unit of an event table and the last removal step it completed.

    A unit is a whole partition, except the legacy partition, which is processed in ``created`` sub-ranges. Each
    step is one transaction ending in an update of this row, so a crash resumes the work instead of repeating it.
    A currency unit in ``CHECKPOINTED`` or a later state records the checkpoint boundary at ``unit_upper``, which
    the next unit's checkpoint chains from even when the boundary has no per-user rows.
    """

    # ADR 7 lifetime: operational state. It references partitions by name, which are not rows a key can point at.
    __tablename__ = "kudos_retention_catalog"
    __table_args__ = (
        # unit_lower is NULL for the sub-range that starts at MINVALUE, which must still be unique.
        UniqueConstraint(
            "dataset",
            "partition_name",
            "unit_lower",
            "unit_upper",
            name="uq_kudos_retention_catalog_unit",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_kudos_retention_catalog_dataset_state", "dataset", "state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset: Mapped[KudosRetentionDataset] = mapped_column(
        Enum(
            KudosRetentionDataset,
            native_enum=False,
            length=32,
            values_callable=lambda datasets: [dataset.value for dataset in datasets],
        ),
        nullable=False,
    )
    # PostgreSQL truncates identifiers to 63 bytes.
    partition_name: Mapped[str] = mapped_column(String(63), nullable=False)
    unit_lower: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unit_upper: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    state: Mapped[KudosRetentionState] = mapped_column(
        Enum(
            KudosRetentionState,
            native_enum=False,
            length=32,
            values_callable=lambda states: [state.value for state in states],
        ),
        nullable=False,
    )
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    # The unit's row count and highest id with no filter, taken in the transaction that commits the replacement. The
    # drop recounts and refuses on any difference, so rows committed after the replacement are never dropped.
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # When the current run of statistics dump failures began. The retry window is measured from it.
    first_failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_dump_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    # Failed dumps are retried only after a spacing from the last attempt, so an outage does not get one attempt per tick.
    last_dump_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The tick reference time at which the unit's partition was detached and kept for the drop grace period, or NULL
    # when it is attached or was dropped without a grace period. The drop is due once the grace has passed since it.
    detached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The state the unit had before its partition was detached, which the reattach command restores.
    state_before_detach: Mapped[KudosRetentionState | None] = mapped_column(
        Enum(
            KudosRetentionState,
            native_enum=False,
            length=32,
            values_callable=lambda states: [state.value for state in states],
        ),
        nullable=True,
    )


class KudosPartitioning:
    """Constants for the event-table partition layout."""

    DEFAULT_WIDTH: timedelta = timedelta(days=7)
    """The width of a partition the bootstrap creates."""
    LEGACY_SUFFIX: str = "_p_legacy"
    """The suffix of the partition from ``MINVALUE`` up to the first weekly partition.

    On a converted database it is the table as it was before partitioning. On a fresh database it starts empty.
    """
    NAME_FORMAT: str = "%Y%m%d"
    """The format of a partition's lower bound in its name."""
    AUTOVACUUM_STORAGE_PARAMETERS: Mapping[str, Mapping[str, str]] = {
        KudosLedger.__tablename__: {
            "autovacuum_vacuum_scale_factor": "0.0",
            "autovacuum_vacuum_threshold": "100000",
            "autovacuum_analyze_scale_factor": "0.0",
            "autovacuum_analyze_threshold": "200000",
        },
        KudosStatEvent.__tablename__: {
            "autovacuum_vacuum_scale_factor": "0.0",
            "autovacuum_vacuum_threshold": "250000",
            "autovacuum_analyze_scale_factor": "0.0",
            "autovacuum_analyze_threshold": "500000",
        },
    }
    """The autovacuum storage parameters of every partition the application creates, by parent table.

    The applier updates every row once, so autovacuum triggers on a fixed number of dead rows rather than on a fraction
    of the partition, as sql_statements/5.1.10.txt set on the unpartitioned tables. Storage parameters are per table
    and a partition does not inherit them from its parent, so each ``CREATE TABLE ... PARTITION OF`` sets them, and
    sql_statements/5.1.16.txt sets the same values on the first weekly partition.
    """


KUDOS_PARTITIONED_TABLES: Final[tuple[str, ...]] = (KudosLedger.__tablename__, KudosStatEvent.__tablename__)
"""The event tables ranged on ``created``."""
_PARTITION_RANGE_BOUNDS = re.compile(r"FROM \((MINVALUE|'[^']+')\) TO \((MAXVALUE|'[^']+')\)")


def ensure_kudos_event_partitions(*, until: datetime, width: timedelta) -> list[str]:
    """Create missing weekly partitions for both event tables up to ``until``; return names created.

    Coverage is contiguous from the UTC Monday 00:00 on or before the current real time up to ``until``. Any part of
    that span that no existing partition covers gets a new partition starting where the gap starts and ending after
    ``width`` or at the next existing partition, whichever comes first, so new partitions never overlap existing
    ones and continue the grid of whatever the migration or an earlier call created. A gap before the current week
    is left alone, because such a range was dropped by retention or lies inside the legacy partition. A table without
    partitions first gets the legacy partition, from ``MINVALUE`` to that Monday, so a fresh database has the same
    layout as a converted one and a row with an older ``created`` still has a partition. A table that is not
    partitioned yet is skipped with a warning, since inserts into it need no partition. Every partition is created
    with ``KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS``.

    Args:
        until: Create partitions until the coverage reaches this naive UTC time.
        width: The width of each new partition after the legacy one.

    Returns:
        The names of the partitions created, in creation order.

    Side Effects:
        Runs DDL in the current session transaction and does not commit, so a caller holding a transaction-scoped
        advisory lock keeps it. The caller commits.

    Concurrency:
        Takes ``SHARE UPDATE EXCLUSIVE`` on a parent before reading its bounds again, so concurrent callers create
        each partition once. Inserts are not blocked by that lock.

    Performance:
        When coverage is already complete, each table costs one ``pg_inherits`` listing and takes no lock, so the
        retention tick can call this every minute.
    """
    created_names: list[str] = []
    for table_name in KUDOS_PARTITIONED_TABLES:
        created_names.extend(_ensure_table_partitions(table_name=table_name, until=until, width=width))
    return created_names


def _ensure_table_partitions(*, table_name: str, until: datetime, width: timedelta) -> list[str]:
    if not _is_partitioned(table_name):
        logger.warning(f"{table_name} is not partitioned yet. Apply sql_statements/5.1.16.txt to partition it.")
        return []
    # Real time, never the caller's reference time: a rehearsal reference time in the future must not leave the
    # current week without a partition.
    coverage_start = _week_start(datetime.utcnow())
    existing_ranges = _partition_ranges(table_name)
    if existing_ranges and not _uncovered_ranges(existing_ranges, start=coverage_start, until=until, width=width):
        return []
    db.session.execute(text(f"LOCK TABLE {table_name} IN SHARE UPDATE EXCLUSIVE MODE"))
    existing_ranges = _partition_ranges(table_name)
    storage_parameters = _storage_parameters_clause(table_name)
    created_names: list[str] = []
    if not existing_ranges:
        legacy_name = f"{table_name}{KudosPartitioning.LEGACY_SUFFIX}"
        db.session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {legacy_name} PARTITION OF {table_name} "
                f"FOR VALUES FROM (MINVALUE) TO ('{coverage_start.isoformat(sep=' ')}') {storage_parameters}",
            ),
        )
        created_names.append(legacy_name)
        existing_ranges = [_PartitionRange(lower=None, upper=coverage_start)]
    for missing_range in _uncovered_ranges(existing_ranges, start=coverage_start, until=until, width=width):
        lower = missing_range.lower_or_minimum
        upper = missing_range.upper_or_maximum
        partition_name = f"{table_name}_p_{lower.strftime(KudosPartitioning.NAME_FORMAT)}"
        db.session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {partition_name} PARTITION OF {table_name} "
                f"FOR VALUES FROM ('{lower.isoformat(sep=' ')}') TO ('{upper.isoformat(sep=' ')}') {storage_parameters}",
            ),
        )
        created_names.append(partition_name)
    return created_names


def _storage_parameters_clause(table_name: str) -> str:
    """Return the ``WITH (...)`` clause of a new partition of ``table_name``."""
    parameters = KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS[table_name]
    return f"WITH ({', '.join(f'{parameter} = {setting}' for parameter, setting in parameters.items())})"


@dataclass(frozen=True)
class _PartitionRange:
    """Represents the ``created`` range of one partition; None stands for ``MINVALUE`` or ``MAXVALUE``."""

    lower: datetime | None
    upper: datetime | None

    @property
    def lower_or_minimum(self) -> datetime:
        return datetime.min if self.lower is None else self.lower

    @property
    def upper_or_maximum(self) -> datetime:
        return datetime.max if self.upper is None else self.upper

    def covers(self, moment: datetime) -> bool:
        return self.lower_or_minimum <= moment < self.upper_or_maximum


def _uncovered_ranges(
    existing_ranges: list[_PartitionRange],
    *,
    start: datetime,
    until: datetime,
    width: timedelta,
) -> list[_PartitionRange]:
    """Return the ranges to create so that ``[start, until)`` is covered, each at most ``width`` long.

    A new range ends at the next existing partition's lower bound when that comes before ``width``, so it never
    overlaps an existing partition, and a range starting inside an existing partition skips to that partition's end.
    """
    uncovered: list[_PartitionRange] = []
    cursor = start
    while cursor < until:
        covering_range = next((existing for existing in existing_ranges if existing.covers(cursor)), None)
        if covering_range is not None:
            if covering_range.upper is None:
                break
            cursor = covering_range.upper
            continue
        later_lowers = [existing.lower for existing in existing_ranges if existing.lower is not None and existing.lower > cursor]
        upper = min([cursor + width, *later_lowers])
        uncovered.append(_PartitionRange(lower=cursor, upper=upper))
        cursor = upper
    return uncovered


def _is_partitioned(table_name: str) -> bool:
    relation_kind = db.session.execute(
        text("SELECT relkind FROM pg_class WHERE oid = CAST(:table_name AS regclass)"),
        {"table_name": table_name},
    ).scalar_one()
    return bool(relation_kind == "p")


def _partition_ranges(table_name: str) -> list[_PartitionRange]:
    """Return the ``created`` range of every partition of ``table_name``, from one ``pg_inherits`` listing.

    Bounds come from ``pg_catalog`` because the migration's first partitions do not follow the weekly naming. A
    default partition has no range and is left out.
    """
    bound_expressions = db.session.execute(
        text(
            "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = CAST(:table_name AS regclass)",
        ),
        {"table_name": table_name},
    ).scalars()
    return [
        _PartitionRange(lower=_parse_partition_bound(bound_match.group(1)), upper=_parse_partition_bound(bound_match.group(2)))
        for bound_match in (_PARTITION_RANGE_BOUNDS.search(str(expression)) for expression in bound_expressions)
        if bound_match is not None
    ]


def _parse_partition_bound(bound: str) -> datetime | None:
    """Parse one bound of ``pg_get_expr`` output: a quoted timestamp, or ``MINVALUE``/``MAXVALUE`` as None."""
    if not bound.startswith("'"):
        return None
    return datetime.fromisoformat(bound.strip("'"))


def _week_start(moment: datetime) -> datetime:
    """Return the UTC Monday 00:00 on or before the naive UTC ``moment``."""
    return datetime(moment.year, moment.month, moment.day) - timedelta(days=moment.weekday())


_MODE_CACHE_KEY = "kudos_ledger_mode"


def get_kudos_ledger_mode() -> KudosLedgerMode:
    """Return and transaction-pin the mode, defaulting installations to shadow.

    The first read in a transaction takes the shared mode gate (an advisory
    lock, see :func:`horde.database.kudos_db.acquire_mode_pin`) and holds it
    until the caller commits; a mode transition takes the same gate
    exclusively, so every mutation that observed the old mode finishes before
    the transition can become visible, while the gate's fair queueing keeps a
    waiting transition from being starved by new pins. Later reads in the same
    transaction return the value the first read pinned without re-querying: a
    single settlement issues many mode reads, and the transaction pin already
    fixes the answer for the whole transaction. The cache is keyed on the
    transaction the pinning read ran in, so a commit or rollback (which ends
    that transaction) forces the next read to re-take the gate.
    """
    from horde.database.kudos_db import acquire_mode_pin

    session = db.session()
    cached: tuple[object, KudosLedgerMode] | None = session.info.get(_MODE_CACHE_KEY)
    active_transaction = session.get_transaction()
    if cached is not None and active_transaction is not None and cached[0] is active_transaction:
        return cached[1]
    acquire_mode_pin()
    control = session.query(KudosLedgerControl).filter_by(id=1).first()
    mode = KudosLedgerMode.SHADOW if control is None else control.mode
    session.info[_MODE_CACHE_KEY] = (session.get_transaction(), mode)
    return mode


def get_kudos_trust_threshold() -> Decimal | None:
    """Return the auto-trust escrow threshold, or ``None`` when promotion is disabled.

    Automatic trust promotion is opt-in through ``KUDOS_TRUST_THRESHOLD``. An unset
    variable disables promotion everywhere rather than raising, so the projector
    and the inline trust check agree on the disabled state.
    """
    threshold_text = os.getenv("KUDOS_TRUST_THRESHOLD")
    if threshold_text is None:
        return None
    return Decimal(threshold_text)


def set_kudos_ledger_mode(mode: KudosLedgerMode) -> None:
    """Change mutation ownership after all old-mode writers have completed."""
    # Lock order is applier -> mode gate everywhere. The applier can read the
    # mode while emitting a floor/drain posting, so reversing this order here
    # would create precisely the cross-subsystem deadlock this design is
    # intended to eliminate.
    from horde.database.kudos_db import acquire_applier_lock, acquire_mode_gate_exclusive

    # Drop any memoized mode from an earlier read in this session so the change
    # this function commits cannot be masked by a stale per-transaction cache.
    db.session().info.pop(_MODE_CACHE_KEY, None)
    acquire_applier_lock()
    acquire_mode_gate_exclusive()
    control = db.session.query(KudosLedgerControl).filter_by(id=1).first()
    if control is None:
        control = KudosLedgerControl(id=1, mode=KudosLedgerMode.SHADOW)
        db.session.add(control)
        db.session.flush()
    if control.mode == mode:
        db.session.commit()
        return
    previous_mode = control.mode
    if mode == KudosLedgerMode.SHADOW:
        # Existing writers that observed ledger mode have finished because the
        # exclusive mode gate waited for their shared pins. New
        # writers are now blocked, and the applier advisory lock prevents a
        # concurrent projector. Fold the final tail in this same transaction so
        # no old-mode posting can be reordered after a shadow inline mutation.
        from horde.database.kudos_ledger import apply_pending_kudos

        drained_rows = 0
        for _ in range(10_000):
            folded = apply_pending_kudos(commit=False, lock_already_held=True)
            if folded == 0:
                break
            drained_rows += folded
        else:
            db.session.rollback()
            raise RuntimeError("Kudos projection tail did not drain during shadow transition")
        logger.info(f"Kudos ledger->shadow transition folded {drained_rows} final-tail rows")
    control.mode = mode
    control.changed_at = datetime.utcnow()
    db.session.commit()
    logger.info(f"Kudos ledger mode changed: {previous_mode} -> {mode}")


def kudos_projection_is_async() -> bool:
    """Return whether ledger postings, rather than inline writes, own projection."""
    return get_kudos_ledger_mode() == KudosLedgerMode.LEDGER


def _finite_decimal_amount(amount: KudosAmount) -> Decimal:
    """Convert a kudos amount to a finite decimal value.

    Args:
        amount: Numeric amount supplied by a currency or statistic producer.

    Returns:
        A finite decimal representation of the amount.

    Raises:
        ValueError: The amount is NaN or positive/negative infinity.
    """
    decimal_amount = Decimal(str(amount))
    if not decimal_amount.is_finite():
        raise ValueError("Kudos amount must be finite")
    return decimal_amount


def emit_kudos_ledger_entry(
    entry_type: KudosEntryType,
    amount: KudosAmount,
    *,
    user_id: int,
    escrow: bool = False,
    detail: KudosAuditMetadata | None = None,
    force_projection: bool = False,
    commit: bool = False,
) -> KudosLedger:
    """Append one signed posting to the ledger for the current business event.

    Args:
        entry_type: The producing event's classification.
        amount: Signed currency delta applied to the target balance.
        user_id: Target user's required database id.
        escrow: Route a user posting into the evaluation escrow balance.
        detail: Optional audit metadata. The applier reads only the documented
            :class:`~horde.enums.KudosAuditDetail` keys.
        force_projection: Leave the posting unapplied even in shadow mode. This
            is reserved for projector-authored recovery/floor movements.
        commit: Commit the session after adding the row.

    Returns:
        The pending ledger row, inserted at the session's next flush. Deferring
        the flush lets one flush write every posting of a business event as a
        bulk INSERT instead of one round trip per posting; autoflush still makes
        the row visible to any same-session query issued before that point.

    Raises:
        ValueError: The amount is not finite.
    """
    decimal_amount = _finite_decimal_amount(amount)
    event = _current_kudos_event.get()
    if event is None:
        event = _KudosEvent(event_id=uuid.uuid4())
    entry = KudosLedger(
        event_id=event.event_id,
        entry_type=entry_type,
        user_id=user_id,
        escrow=escrow,
        amount=decimal_amount,
        job_id=event.job_id,
        wp_type=event.wp_type,
        detail=detail,
        # Shadow-mode rows are permanent audit records of a movement that was
        # already materialized inline.  Marking them applied prevents replay.
        applied=not (kudos_projection_is_async() or force_projection),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def emit_kudos_stat_event(
    entry_type: KudosEntryType,
    amount: KudosAmount,
    *,
    user_id: int | None = None,
    worker_id: uuid.UUID | None = None,
    worker_user_id: int | None = None,
    team_id: uuid.UUID | None = None,
    unit: KudosUnit,
    stat_action: str | None = None,
    record: str | None = None,
    detail: KudosAuditMetadata | None = None,
    commit: bool = False,
) -> KudosStatEvent:
    """Append one derived-stat projection event for the active business event.

    Args:
        entry_type: Business event classification.
        amount: Signed decimal counter delta.
        user_id: User target, mutually exclusive with ``worker_id``.
        worker_id: Worker target, mutually exclusive with ``user_id``.
        worker_user_id: Worker owner's user id retained for audit.
        team_id: Team attribution fixed at event time.
        unit: Unit denominating ``amount``.
        stat_action: Counter bucket or record-type dimension.
        record: Reserved projector discriminator or record dimension.
        detail: Optional audit metadata.
        commit: Commit the session after adding the row.

    Returns:
        The pending statistics event, inserted at the session's next flush (see
        :func:`emit_kudos_ledger_entry` for why the flush is deferred).

    Raises:
        ValueError: The target/dimensions are invalid or the amount is not finite.
    """
    if (user_id is None) == (worker_id is None):
        raise ValueError("A kudos stat event must target exactly one user or worker")
    if stat_action is not None and len(stat_action) > KUDOS_STAT_ACTION_MAX_LENGTH:
        raise ValueError(f"Kudos stat action exceeds {KUDOS_STAT_ACTION_MAX_LENGTH} characters")
    if record is not None and len(record) > KUDOS_STAT_RECORD_MAX_LENGTH:
        raise ValueError(f"Kudos stat record exceeds {KUDOS_STAT_RECORD_MAX_LENGTH} characters")
    decimal_amount = _finite_decimal_amount(amount)
    event = _current_kudos_event.get() or _KudosEvent(event_id=uuid.uuid4())
    entry = KudosStatEvent(
        event_id=event.event_id,
        entry_type=entry_type,
        user_id=user_id,
        worker_id=worker_id,
        worker_user_id=worker_user_id,
        team_id=team_id,
        job_id=event.job_id,
        wp_type=event.wp_type,
        amount=decimal_amount,
        unit=unit,
        stat_action=stat_action,
        record=record,
        detail=detail,
        applied=not kudos_projection_is_async(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry
