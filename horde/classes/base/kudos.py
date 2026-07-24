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
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import and_, column, false

from horde.enums import KudosEntryType, KudosLedgerMode, KudosUnit
from horde.flask import db

if TYPE_CHECKING:
    from horde.classes.base.user import User

type KudosAmount = Decimal | int | float
type KudosAuditMetadata = dict[str, object]
_json_type = JSON().with_variant(JSONB(), "postgresql")


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
        Index("ix_kudos_ledger_unapplied", "id", postgresql_where=column("applied").is_(false())),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    entry_type: Mapped[KudosEntryType] = mapped_column(
        Enum(KudosEntryType, native_enum=False, values_callable=lambda entries: [entry.value for entry in entries]),
        nullable=False,
    )
    # Users are soft-deleted/wiped rather than removed. RESTRICT makes audit
    # ownership explicit and prevents an accidental hard delete from orphaning
    # authoritative currency history.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    user: Mapped[User] = relationship()
    escrow: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Kudos and counter quantities are decimal accounting values.  A binary
    # float makes replay depend on grouping and platform rounding, which is not
    # acceptable for an authoritative movement log.
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    applied: Mapped[bool] = mapped_column(default=False, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    wp_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detail: Mapped[KudosAuditMetadata | None] = mapped_column(_json_type, nullable=True)


class KudosStatEvent(db.Model):  # type: ignore[name-defined,misc]
    """Non-currency counter event projected alongside a kudos batch."""

    __tablename__ = "kudos_stat_events"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND worker_id IS NULL) OR (user_id IS NULL AND worker_id IS NOT NULL)",
            name="kudos_stat_event_exactly_one_target",
        ),
        CheckConstraint("amount <> 'NaN'", name="kudos_stat_event_amount_not_nan"),
        Index("ix_kudos_stat_events_unapplied", "id", postgresql_where=column("applied").is_(false())),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    entry_type: Mapped[KudosEntryType] = mapped_column(
        Enum(KudosEntryType, native_enum=False, values_callable=lambda entries: [entry.value for entry in entries]),
        nullable=False,
    )
    # These are immutable audit references rather than ownership foreign keys.
    # Workers and teams are hard-deleted, but their counter history must survive.
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    worker_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    wp_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    unit: Mapped[KudosUnit] = mapped_column(
        Enum(KudosUnit, native_enum=False, values_callable=lambda units: [unit.value for unit in units]),
        nullable=False,
    )
    stat_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[KudosAuditMetadata | None] = mapped_column(_json_type, nullable=True)
    applied: Mapped[bool] = mapped_column(default=False, nullable=False)


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


_MODE_CACHE_KEY = "kudos_ledger_mode"


def get_kudos_ledger_mode() -> KudosLedgerMode:
    """Return and transaction-pin the mode, defaulting installations to shadow.

    The first read in a transaction takes a key-share lock on the control row and
    holds it until the caller commits; a mode transition takes an exclusive lock,
    so every mutation that observed the old mode finishes before the transition
    can become visible. Later reads in the same transaction return the value the
    first read pinned without re-querying: a single settlement issues many mode
    reads of one global hot row, and the transaction pin already fixes the answer
    for the whole transaction. The cache is keyed on the transaction the pinning
    read ran in, so a commit or rollback (which ends that transaction) forces the
    next read to re-take the lock.
    """
    session = db.session()
    cached: tuple[object, KudosLedgerMode] | None = session.info.get(_MODE_CACHE_KEY)
    active_transaction = session.get_transaction()
    if cached is not None and active_transaction is not None and cached[0] is active_transaction:
        return cached[1]
    control = session.query(KudosLedgerControl).filter_by(id=1).with_for_update(read=True, key_share=True).first()
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
    # Lock order is applier -> control everywhere. The applier can read the
    # control row while emitting a floor/drain posting, so reversing this order
    # here would create precisely the cross-subsystem deadlock this design is
    # intended to eliminate.
    from horde.database.kudos_db import acquire_applier_lock

    # Drop any memoized mode from an earlier read in this session so the change
    # this function commits cannot be masked by a stale per-transaction cache.
    db.session().info.pop(_MODE_CACHE_KEY, None)
    acquire_applier_lock()
    control = db.session.query(KudosLedgerControl).filter_by(id=1).with_for_update().first()
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
        # exclusive control-row lock waited for their key-share locks. New
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
        detail: Optional audit metadata; written but not read by the applier.
        force_projection: Leave the posting unapplied even in shadow mode. This
            is reserved for projector-authored recovery/floor movements.
        commit: Commit the session after adding the row.

    Returns:
        The persisted (flushed) ledger row.
    """
    event = _current_kudos_event.get()
    if event is None:
        event = _KudosEvent(event_id=uuid.uuid4())
    entry = KudosLedger(
        event_id=event.event_id,
        entry_type=entry_type,
        user_id=user_id,
        escrow=escrow,
        amount=Decimal(str(amount)),
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
    else:
        db.session.flush()
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
        commit: Commit instead of flushing the session.

    Returns:
        The persisted (flushed) statistics event.
    """
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
        amount=Decimal(str(amount)),
        unit=unit,
        stat_action=stat_action,
        record=record,
        detail=detail,
        applied=not kudos_projection_is_async(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return entry
