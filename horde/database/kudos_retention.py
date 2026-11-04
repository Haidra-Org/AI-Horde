# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remove kudos event partitions older than the online window after committing their replacements (ADR 42).

``kudos_ledger`` and ``kudos_stat_events`` are range-partitioned by ``created``. A retention unit is one partition, or
one ``created`` sub-range of the legacy partition, and ``kudos_retention_catalog`` records each unit's state. Every
call of ``run_retention_tick`` makes at most one state transition:

- statistics: ``PENDING`` -> ``ROLLED_UP`` (day rollups and quarantine move) -> ``DUMPED`` (archive dump) ->
  ``DROPPED``. A failed dump leaves the unit ``DUMP_FAILED``, retried on later ticks for ``dump_retry_window``, and
  an operator moves it to ``DUMP_WAIVED``, which drops it without a dump. Without an archive the dump step waives it;
- currency: ``PENDING`` -> ``CHECKPOINTED`` (per-user checkpoint and daily statements) -> ``EXPORTED`` -> ``VERIFIED``
  (archive read back) -> ``DROPPED``.

With ``drop_grace`` above zero, as by default, the drop takes two transitions: the partition is detached and its
units become ``DETACHED``, and a later tick drops it once the grace has passed. ``reattach_partition`` puts a
``DETACHED`` partition back. With ``hold_drops`` set, every step except the detach and the drop runs, and drop-ready
partitions stay attached.

A unit that cannot proceed safely becomes ``BLOCKED`` with the reason in ``error`` (``RetentionBlockReason``), and an
operator returns it to ``PENDING``, or to ``CHECKPOINTED`` when its checkpoint is committed. Database work for one
transition is one transaction. Object I/O runs outside any database transaction, and the catalog row is updated after
the I/O returns, so a crash repeats the step instead of skipping it. The step that commits a unit's replacement records
the unit's row count and highest id, and the drop recounts both before removing anything.

Each archive tier has its own byte budget. The dump and the export each admit an upload only when the tier's stored
bytes plus the estimate fit, and block the unit otherwise. Nothing is evicted by a tick. With an archive configured,
every tick lists both tiers once, clears ``object_key`` on units whose object is not listed, and returns the tier sizes.
``audit_archive`` reads every object back against its manifest for a scheduled audit.

``KUDOS_RETENTION_POLICY`` is read from the environment at import, so an invalid setting fails startup. The pure
helpers (``parse_partition_bound``, ``format_source_range``, ``legacy_unit_ranges``, ``select_next_unit``,
``checkpoint_chain_continues`` and ``STEP_TABLE``) take no database and are what the step functions build on.
"""

from __future__ import annotations

import contextlib
import enum
import os
import re
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, TypedDict, cast

from loguru import logger
from sqlalchemy import Connection, CursorResult, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from horde.classes.base.kudos import (
    KudosIdempotencyKey,
    KudosLedger,
    KudosLedgerCheckpoint,
    KudosLedgerDailyStatement,
    KudosPartitioning,
    KudosRetentionUnit,
    KudosSnapshotInvalidation,
    KudosStatEvent,
    KudosStatQuarantine,
    KudosStatRollup,
    ensure_kudos_event_partitions,
)
from horde.database.kudos_db import begin_repeatable_read, release_retention_lock, try_acquire_retention_lock
from horde.enums import KudosEntryType, KudosRetentionDataset, KudosRetentionState
from horde.flask import SQLITE_MODE, db
from horde.kudos_archive import (
    ARCHIVE_MANIFEST_UNTYPED_SCHEMA_VERSION,
    ARCHIVE_PREFIX_ENV,
    ARCHIVE_REQUIRED_ENV,
    ArchiveBudgetExceeded,
    ArchiveColumn,
    ArchiveEvictionError,
    ArchiveObjectAudit,
    ArchiveTier,
    KudosArchive,
    KudosArchiveError,
    PerUserTotals,
    normalize_archive_prefix,
)

RETENTION_ENABLED_ENV: Final[str] = "KUDOS_RETENTION_ENABLED"
"""The environment variable that turns partition removal on; partitions are created and keys expire either way."""
CURRENCY_WINDOW_ENV: Final[str] = "KUDOS_RETENTION_CURRENCY_DAYS"
"""The environment variable holding the currency online window in days."""
STATISTICS_WINDOW_ENV: Final[str] = "KUDOS_RETENTION_STATISTICS_DAYS"
"""The environment variable holding the statistics online window in days."""
PARTITION_WIDTH_ENV: Final[str] = "KUDOS_PARTITION_WIDTH_DAYS"
"""The environment variable holding the width of new event partitions in days."""
ARCHIVE_BUDGET_ENV: Final[str] = "KUDOS_ARCHIVE_BUDGET_BYTES"
"""The environment variable holding the currency tier's byte budget. It is required when the archive is configured."""
ARCHIVE_STATISTICS_BUDGET_ENV: Final[str] = "KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES"
"""The environment variable holding the statistics tier's byte budget. Unset means no limit."""
DUMP_RETRY_WINDOW_ENV: Final[str] = "KUDOS_RETENTION_DUMP_RETRY_DAYS"
"""The environment variable holding how many days failed statistics dumps are retried before the unit blocks."""
DROP_DEFER_LOCK_AGE_ENV: Final[str] = "KUDOS_RETENTION_DROP_DEFER_MINUTES"
"""The environment variable holding the transaction age, in minutes, of a lock holder that defers a partition drop."""
HOLD_DROPS_ENV: Final[str] = "KUDOS_RETENTION_HOLD_DROPS"
"""The environment variable that keeps drop-ready partitions attached while every other step runs."""
DROP_GRACE_ENV: Final[str] = "KUDOS_RETENTION_DROP_GRACE_DAYS"
"""The environment variable holding how many days a detached partition is kept before it is dropped."""

DEFAULT_DROP_GRACE_DAYS: Final[int] = 7
"""The drop grace period when ``KUDOS_RETENTION_DROP_GRACE_DAYS`` is unset.

A deployment whose rendered environment lost the variable still keeps a detached partition for a reattach window.
A drop that follows the detach at once needs the variable set to 0.
"""

DEFAULT_DUMP_RETRY_DAYS: Final[int] = 7
"""How long failed statistics dumps are retried when ``KUDOS_RETENTION_DUMP_RETRY_DAYS`` is unset."""
DEFAULT_DROP_DEFER_MINUTES: Final[int] = 5
"""The lock holder transaction age that defers a drop when ``KUDOS_RETENTION_DROP_DEFER_MINUTES`` is unset."""
DROP_LOCK_TIMEOUT: Final[str] = "30s"
"""The ``lock_timeout`` of the drop connection, so a lock queue behind a long transaction cannot hang the tick."""
LOCK_NOT_AVAILABLE_SQLSTATE: Final[str] = "55P03"
"""The SQLSTATE PostgreSQL reports when ``lock_timeout`` expires."""
_DETACHED_LATE_ROWS_HINT: Final[str] = "The partition is detached, reattach it with retention reattach before unblocking"
"""The end of the detail line of a unit blocked with ``LATE_ROWS`` while its partition is detached."""
DUMP_RETRY_SPACING: Final[timedelta] = timedelta(minutes=15)
"""How long a ``DUMP_FAILED`` unit waits after its last attempt before the dump is tried again."""
STALE_UPLOAD_AGE: Final[timedelta] = timedelta(days=1)
"""How long an incomplete multipart upload stays open before a tick aborts it. It is far longer than any export."""
FUTURE_REFERENCE_REFUSAL: Final[str] = "reference time is in the future. Pass --confirm-live-data-loss on a restored copy only"
RESTORED_PARTITION_REFUSAL: Final[str] = "an attached partition has dropped catalog units"
"""The detail of a ``BLOCKED`` tick result for a dataset with an attached partition whose catalog units are ``DROPPED``.
Such a partition was restored from a backup under its original name. The catalog does not describe its rows, so the
dataset does not make a transition until an operator detaches the partition."""
"""The detail of a tick refused because its reference time is later than the real clock."""

DEFAULT_WINDOW_DAYS: Final[int] = 30
"""The online window of each dataset when its variable is unset."""
MINIMUM_WINDOW_DAYS: Final[int] = 30
"""The shortest online window; ADR 42 keeps at least 30 days of exact events for incident response."""
DEFAULT_PARTITION_WIDTH_DAYS: Final[int] = 7
"""The partition width when ``KUDOS_PARTITION_WIDTH_DAYS`` is unset."""
MINIMUM_PARTITION_WIDTH_DAYS: Final[int] = 1
"""The narrowest partition width."""
MAXIMUM_PARTITION_WIDTH_DAYS: Final[int] = 31
"""The widest partition width; width adds to the effective retention, so it stays small next to the window."""
ENABLED_VALUES: Final[frozenset[str]] = frozenset({"true", "1", "yes"})
"""The accepted spellings of an enabled flag."""
DISABLED_VALUES: Final[frozenset[str]] = frozenset({"false", "0", "no"})
"""The accepted spellings of a disabled flag."""

EXPIRE_KEYS_BATCH_SIZE: Final[int] = 5000
"""The idempotency keys one tick deletes at most."""
ARCHIVE_SIZE_RATIO: Final[Decimal] = Decimal("0.2")
"""The assumed compressed size of an archive object as a share of the partition's heap size."""
REHEARSAL_SLACK_WIDTHS: Final[int] = 4
"""How many partition widths past the longer window a rehearsal ``--as-of`` may reach."""
SOURCE_RANGE_TIME_FORMAT: Final[str] = "%Y%m%dT%H%M%S"
"""The timestamp layout inside a ``source_range``."""
SOURCE_RANGE_MINIMUM: Final[str] = "min"
"""The lower bound written in a ``source_range`` for a unit that starts at the partition's minimum."""
LEGACY_GRID_ANCHOR: Final[datetime] = datetime(1970, 1, 5)
"""A UTC Monday; legacy sub-range boundaries sit on multiples of the partition width from it, like weekly partitions."""
MAXIMUM_IDENTIFIER_LENGTH: Final[int] = 63
"""The PostgreSQL identifier length limit."""

PARTITION_PARENTS: Final[Mapping[KudosRetentionDataset, str]] = {
    KudosRetentionDataset.CURRENCY: KudosLedger.__tablename__,
    KudosRetentionDataset.STATISTICS: KudosStatEvent.__tablename__,
}
"""The partitioned parent table of each dataset."""

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_POSTGRES_TYPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_ ]*(\([0-9]+(,[0-9]+)?\))?[a-z ]*(\[\])*$")
"""A built-in type as ``format_type`` writes it, such as ``numeric(20,2)`` or ``timestamp(3) without time zone``.

A restore writes manifest types into ``CREATE TABLE``, so anything else is refused."""
_BOUND_PATTERN: Final[re.Pattern[str]] = re.compile(r"^FOR VALUES FROM \((?P<lower>[^)]*)\) TO \((?P<upper>[^)]*)\)$")
_DEFAULT_PARTITION_BOUND: Final[str] = "DEFAULT"
_MINVALUE: Final[str] = "MINVALUE"
_MAXVALUE: Final[str] = "MAXVALUE"


class RetentionBlockReason(enum.StrEnum):
    """The ``error`` text of a ``BLOCKED`` unit."""

    UNAPPLIED_ROWS = "unapplied rows"
    CHECKPOINT_CHAIN_GAP = "checkpoint chain gap"
    CURRENCY_BUDGET_EXCEEDED = "currency tier would exceed budget"
    STATISTICS_BUDGET_EXCEEDED = "statistics tier would exceed budget"
    """The dump would take the statistics tier over ``KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES``. Other statistics units
    proceed, and ``retention unblock`` retries the unit once space is freed or the budget raised."""
    ARCHIVE_MISMATCH = "archive mismatch"
    ONLINE_DRIFT = "online drift since export"
    BOUNDARY_ALREADY_CHECKPOINTED = "boundary already checkpointed"
    """Another partition's unit already holds a checkpoint at this unit's upper bound. Recomputing it would replace
    that checkpoint with this unit's rows alone."""
    LATE_ROWS = "late rows"
    """The unit's row count or highest id changed after its replacement was committed."""
    DUMP_RETRIES_EXHAUSTED = "dump retries exhausted"
    """Statistics dumps kept failing for longer than the retry window. ``retention unblock --waive-dump`` accepts
    dropping the unit without a dump."""
    ARCHIVE_OBJECT_MISSING = "archive object missing"
    """The archive object of a currency unit past its export, or of a dumped statistics unit before its drop, is
    missing. The tick's archive listing does not hold its key, or the drop found that the key or the object is
    missing. A detached partition is reattached first. ``retention unblock`` then returns a currency unit to
    ``CHECKPOINTED``, so it is exported and verified again from the rows still online, and a statistics unit to
    ``PENDING``, so it is rolled up and dumped again. ``retention unblock --waive-dump`` drops a statistics unit
    without the dump."""
    REATTACHED = "reattached by operator"
    """An operator reattached the unit's detached partition. The unit stays blocked, so the partition is not detached
    again until an operator unblocks it, which restores ``state_before_detach``."""


WAIVABLE_DUMP_BLOCK_REASONS: Final[frozenset[str]] = frozenset(
    {
        RetentionBlockReason.DUMP_RETRIES_EXHAUSTED,
        RetentionBlockReason.STATISTICS_BUDGET_EXCEEDED,
        RetentionBlockReason.ARCHIVE_OBJECT_MISSING,
    },
)
"""The block reasons of a statistics unit whose dump an operator may waive, dropping its raw rows without a copy."""

ARCHIVE_TIERS: Final[Mapping[KudosRetentionDataset, ArchiveTier]] = {
    KudosRetentionDataset.CURRENCY: ArchiveTier.CURRENCY,
    KudosRetentionDataset.STATISTICS: ArchiveTier.STATISTICS,
}
"""The archive tier each dataset's objects are written to."""

ARCHIVE_NOT_CONFIGURED: Final[str] = "archive not configured"
"""The ``error`` text of a statistics unit marked ``DUMP_FAILED`` because no archive is configured."""


RETENTION_BLOCKED_LOG_PREFIX: Final[str] = "Kudos retention blocked"
"""The start of the warning logged when a unit becomes ``BLOCKED``.

LOG CONTRACT: log-based alerting on blocked retention keys on this prefix. Changing it means changing those rules in
the same release; ``tests/unit/test_kudos_retention.py`` guards the value."""
RETENTION_DUMP_FAILED_LOG_PREFIX: Final[str] = "Kudos retention dump failed"
"""The start of the warning logged when a statistics unit becomes ``DUMP_FAILED``.

LOG CONTRACT: log-based alerting on failed statistic dumps keys on this prefix. Changing it means changing those rules
in the same release; ``tests/unit/test_kudos_retention.py`` guards the value."""


class RetentionVerificationError(Exception):
    """A rollup's totals differ from its unit's totals inside one transaction; the rollup is rolled back."""


@dataclass(frozen=True, kw_only=True)
class KudosRetentionPolicy:
    """Represents how long kudos events stay online and how the partitions and archive are sized.

    Raises:
        ValueError: A window is shorter than ``MINIMUM_WINDOW_DAYS``, the width is outside
            ``MINIMUM_PARTITION_WIDTH_DAYS`` to ``MAXIMUM_PARTITION_WIDTH_DAYS``, a budget is not positive, or the
            drop grace is negative.
    """

    enabled: bool
    """Whether eligible partitions are removed."""
    currency_window: timedelta
    """How long currency postings stay online after their partition's upper bound."""
    statistics_window: timedelta
    """How long statistic events stay online after their partition's upper bound."""
    partition_width: timedelta
    """The width of new partitions and of legacy sub-ranges."""
    currency_budget_bytes: int | None
    """The currency tier's byte budget, from ``KUDOS_ARCHIVE_BUDGET_BYTES``, or None for no limit."""
    statistics_budget_bytes: int | None = None
    """The statistics tier's byte budget, from ``KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES``, or None for no limit."""
    dump_retry_window: timedelta = timedelta(days=DEFAULT_DUMP_RETRY_DAYS)
    """How long a statistics unit's failed dump is retried, from its first failure, before the unit blocks."""
    drop_defer_lock_age: timedelta = timedelta(minutes=DEFAULT_DROP_DEFER_MINUTES)
    """A drop is deferred while another session whose transaction is older than this holds a lock on the partition."""
    hold_drops: bool = False
    """Whether drop-ready partitions stay attached. Every other step still runs, so the units wait drop-ready."""
    drop_grace: timedelta = timedelta(days=DEFAULT_DROP_GRACE_DAYS)
    """How long a detached partition is kept before it is dropped, measured with the tick's reference time. Zero drops
    it in the same transition as the detach."""

    def __post_init__(self) -> None:
        # Messages name the environment variables because operators set the policy through them.
        minimum_window = timedelta(days=MINIMUM_WINDOW_DAYS)
        for variable, window in ((CURRENCY_WINDOW_ENV, self.currency_window), (STATISTICS_WINDOW_ENV, self.statistics_window)):
            if window < minimum_window:
                raise ValueError(f"{variable} must be at least {MINIMUM_WINDOW_DAYS} days, got {window}")
        width_in_range = (
            timedelta(days=MINIMUM_PARTITION_WIDTH_DAYS) <= self.partition_width <= timedelta(days=MAXIMUM_PARTITION_WIDTH_DAYS)
        )
        if not width_in_range:
            raise ValueError(
                f"{PARTITION_WIDTH_ENV} must be {MINIMUM_PARTITION_WIDTH_DAYS} to {MAXIMUM_PARTITION_WIDTH_DAYS} days, "
                f"got {self.partition_width}",
            )
        for variable, budget in (
            (ARCHIVE_BUDGET_ENV, self.currency_budget_bytes),
            (ARCHIVE_STATISTICS_BUDGET_ENV, self.statistics_budget_bytes),
        ):
            if budget is not None and budget < 1:
                raise ValueError(f"{variable} must be a positive whole number of bytes, got {budget}")
        if self.dump_retry_window <= timedelta(0):
            raise ValueError(f"{DUMP_RETRY_WINDOW_ENV} must be a positive number of days, got {self.dump_retry_window}")
        if self.drop_defer_lock_age <= timedelta(0):
            raise ValueError(f"{DROP_DEFER_LOCK_AGE_ENV} must be a positive number of minutes, got {self.drop_defer_lock_age}")
        if self.drop_grace < timedelta(0):
            raise ValueError(f"{DROP_GRACE_ENV} must be zero or a positive number of days, got {self.drop_grace}")

    @property
    def key_lifetime(self) -> timedelta:
        """Return how long idempotency keys live: the shorter window, so a key never outlives its postings."""
        return min(self.currency_window, self.statistics_window)

    def budget_for(self, tier: ArchiveTier) -> int | None:
        """Return the byte budget of ``tier``, or None for no limit."""
        if tier == ArchiveTier.CURRENCY:
            return self.currency_budget_bytes
        return self.statistics_budget_bytes

    def window_for(self, dataset: KudosRetentionDataset) -> timedelta:
        """Return the online window of ``dataset``."""
        if dataset == KudosRetentionDataset.CURRENCY:
            return self.currency_window
        return self.statistics_window


def _parse_whole_number(variable: str, raw: str, unit: str) -> int:
    """Parse a whole number of ``unit`` from an environment value.

    Raises:
        ValueError: The value is not an integer.
    """
    try:
        return int(raw.strip())
    except ValueError as err:
        raise ValueError(f"{variable} must be a whole number of {unit}, got {raw!r}") from err


def _parse_days(environ: Mapping[str, str], variable: str, default_days: int) -> timedelta:
    """Parse a whole number of days, or return the default when the variable is unset.

    Raises:
        ValueError: The value is not an integer, or too large for a ``timedelta``.
    """
    raw = environ.get(variable)
    if raw is None:
        return timedelta(days=default_days)
    days = _parse_whole_number(variable, raw, "days")
    try:
        return timedelta(days=days)
    except OverflowError as err:
        raise ValueError(f"{variable} is out of range, got {raw!r}") from err


def _parse_minutes(environ: Mapping[str, str], variable: str, default_minutes: int) -> timedelta:
    """Parse a whole number of minutes, or return the default when the variable is unset.

    Raises:
        ValueError: The value is not an integer, or too large for a ``timedelta``.
    """
    raw = environ.get(variable)
    if raw is None:
        return timedelta(minutes=default_minutes)
    minutes = _parse_whole_number(variable, raw, "minutes")
    try:
        return timedelta(minutes=minutes)
    except OverflowError as err:
        raise ValueError(f"{variable} is out of range, got {raw!r}") from err


def _parse_switch(environ: Mapping[str, str], variable: str) -> bool:
    """Parse a true or false switch. Unset is off.

    Raises:
        ValueError: The value is not a recognized true or false spelling.
    """
    raw = environ.get(variable)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in ENABLED_VALUES:
        return True
    if normalized in DISABLED_VALUES:
        return False
    raise ValueError(f"{variable} must be true or false, got {raw!r}")


def _parse_enabled(environ: Mapping[str, str]) -> bool:
    """Parse a retention switch. Unset means off.

    Raises:
        ValueError: The value is not a recognized true or false spelling.
    """
    return _parse_switch(environ, RETENTION_ENABLED_ENV)


def load_retention_policy(environ: Mapping[str, str] = os.environ) -> KudosRetentionPolicy:
    """Create the retention policy from the operator's environment.

    Args:
        environ: Variables to read; the process environment by default.

    Returns:
        The validated policy.

    Raises:
        ValueError: A variable is malformed or out of range, ``KUDOS_ARCHIVE_PREFIX`` is not a valid key prefix, or
            the archive is configured without ``KUDOS_ARCHIVE_BUDGET_BYTES``.
    """
    raw_budget = environ.get(ARCHIVE_BUDGET_ENV)
    raw_statistics_budget = environ.get(ARCHIVE_STATISTICS_BUDGET_ENV)
    # The archive client is built on the first tick; checking its prefix here makes a bad value stop startup instead.
    normalize_archive_prefix(environ.get(ARCHIVE_PREFIX_ENV, ""))
    archive_configured = all(environ.get(variable) for variable in ARCHIVE_REQUIRED_ENV)
    if archive_configured and raw_budget is None:
        raise ValueError(f"{ARCHIVE_BUDGET_ENV} is required when the kudos archive is configured")
    return KudosRetentionPolicy(
        enabled=_parse_enabled(environ),
        currency_window=_parse_days(environ, CURRENCY_WINDOW_ENV, DEFAULT_WINDOW_DAYS),
        statistics_window=_parse_days(environ, STATISTICS_WINDOW_ENV, DEFAULT_WINDOW_DAYS),
        partition_width=_parse_days(environ, PARTITION_WIDTH_ENV, DEFAULT_PARTITION_WIDTH_DAYS),
        currency_budget_bytes=None if raw_budget is None else _parse_whole_number(ARCHIVE_BUDGET_ENV, raw_budget, "bytes"),
        statistics_budget_bytes=(
            None if raw_statistics_budget is None else _parse_whole_number(ARCHIVE_STATISTICS_BUDGET_ENV, raw_statistics_budget, "bytes")
        ),
        dump_retry_window=_parse_days(environ, DUMP_RETRY_WINDOW_ENV, DEFAULT_DUMP_RETRY_DAYS),
        drop_defer_lock_age=_parse_minutes(environ, DROP_DEFER_LOCK_AGE_ENV, DEFAULT_DROP_DEFER_MINUTES),
        hold_drops=_parse_switch(environ, HOLD_DROPS_ENV),
        drop_grace=_parse_days(environ, DROP_GRACE_ENV, DEFAULT_DROP_GRACE_DAYS),
    )


KUDOS_RETENTION_POLICY: KudosRetentionPolicy = load_retention_policy()
"""The kudos retention policy, loaded from the environment."""


class RetentionTickOutcome(enum.StrEnum):
    """What one retention tick did."""

    SKIPPED_LOCK = "skipped_lock"
    """Another process holds the retention lock."""
    IDLE = "idle"
    """Retention is disabled, or no unit is eligible or actionable."""
    STEPPED = "stepped"
    """One unit made one transition."""
    BLOCKED = "blocked"
    """One unit became ``BLOCKED``."""
    FAILED = "failed"
    """Every candidate step failed and was left to retry on the next tick."""
    REFUSED = "refused"
    """The reference time is later than the real clock and the caller did not confirm it is a restored copy."""
    DEFERRED = "deferred"
    """A drop waits because a long transaction holds a lock on the partition or its parent. The next tick retries."""


class RetentionStep(enum.StrEnum):
    """The step that moves a unit out of its current state."""

    ROLLUP = "rollup"
    DUMP = "dump"
    CHECKPOINT = "checkpoint"
    EXPORT = "export"
    VERIFY = "verify"
    DROP = "drop"


STEP_TABLE: Final[Mapping[KudosRetentionDataset, Mapping[KudosRetentionState, RetentionStep]]] = {
    KudosRetentionDataset.STATISTICS: {
        KudosRetentionState.PENDING: RetentionStep.ROLLUP,
        KudosRetentionState.ROLLED_UP: RetentionStep.DUMP,
        KudosRetentionState.DUMPED: RetentionStep.DROP,
        KudosRetentionState.DUMP_FAILED: RetentionStep.DUMP,
        KudosRetentionState.DUMP_WAIVED: RetentionStep.DROP,
        KudosRetentionState.DETACHED: RetentionStep.DROP,
    },
    KudosRetentionDataset.CURRENCY: {
        KudosRetentionState.PENDING: RetentionStep.CHECKPOINT,
        KudosRetentionState.CHECKPOINTED: RetentionStep.EXPORT,
        KudosRetentionState.EXPORTED: RetentionStep.VERIFY,
        KudosRetentionState.VERIFIED: RetentionStep.DROP,
        KudosRetentionState.DETACHED: RetentionStep.DROP,
    },
}
"""The step for each dataset and state; a state absent from a dataset's map has no step."""

STEP_TARGETS: Final[Mapping[RetentionStep, frozenset[KudosRetentionState]]] = {
    RetentionStep.ROLLUP: frozenset({KudosRetentionState.ROLLED_UP, KudosRetentionState.BLOCKED}),
    RetentionStep.DUMP: frozenset(
        {KudosRetentionState.DUMPED, KudosRetentionState.DUMP_FAILED, KudosRetentionState.DUMP_WAIVED, KudosRetentionState.BLOCKED},
    ),
    RetentionStep.CHECKPOINT: frozenset({KudosRetentionState.CHECKPOINTED, KudosRetentionState.BLOCKED}),
    RetentionStep.EXPORT: frozenset({KudosRetentionState.EXPORTED, KudosRetentionState.BLOCKED}),
    RetentionStep.VERIFY: frozenset({KudosRetentionState.VERIFIED, KudosRetentionState.BLOCKED}),
    RetentionStep.DROP: frozenset({KudosRetentionState.DETACHED, KudosRetentionState.DROPPED, KudosRetentionState.BLOCKED}),
}
"""The states each step can leave a unit in."""

DROP_READY_STATES: Final[frozenset[KudosRetentionState]] = frozenset(
    {KudosRetentionState.DUMPED, KudosRetentionState.DUMP_WAIVED, KudosRetentionState.VERIFIED, KudosRetentionState.DETACHED},
)
"""The states whose replacement is committed, so the unit's rows may be dropped. ``DETACHED`` is one of them, because
a unit reaches it only from one of the others."""

CHECKPOINT_RECORDED_STATES: Final[frozenset[KudosRetentionState]] = frozenset(
    {
        KudosRetentionState.CHECKPOINTED,
        KudosRetentionState.EXPORTED,
        KudosRetentionState.VERIFIED,
        KudosRetentionState.DETACHED,
        KudosRetentionState.DROPPED,
    },
)
"""The currency states whose checkpoint boundary at ``unit_upper`` is committed and is part of the chain."""


@dataclass(frozen=True)
class RetentionUnitRef:
    """Represents the identity of one retention unit."""

    partition_name: str
    lower: datetime | None
    upper: datetime


@dataclass(frozen=True)
class RetentionTickResult:
    """Represents the outcome of one retention tick."""

    outcome: RetentionTickOutcome
    dataset: KudosRetentionDataset | None = None
    unit: RetentionUnitRef | None = None
    from_state: KudosRetentionState | None = None
    to_state: KudosRetentionState | None = None
    detail: str = ""
    archive_tier_bytes: dict[ArchiveTier, int] | None = None
    """The stored bytes of each archive tier, listed after the step, or None without an archive or when the listing
    failed."""


class KudosRetentionHealth(TypedDict):
    """Represents the retention job's progress for metrics and the admin status command."""

    oldest_eligible_age_seconds: dict[KudosRetentionDataset, float | None]
    """Per dataset, how long the oldest partition still online has been eligible, or None when none is."""
    blocked_units: int
    """Catalog units in ``BLOCKED``."""
    blocked_units_by_reason: dict[RetentionBlockReason, int]
    """Catalog units in ``BLOCKED`` per reason, with every reason present."""
    last_step_age_seconds: float | None
    """Seconds since the most recent catalog update, or None for an empty catalog."""
    pending_units: int
    """Catalog units neither ``DROPPED`` nor ``BLOCKED``, including currency units waiting for an archive."""
    pending_units_by_dataset: dict[KudosRetentionDataset, int]
    """``pending_units`` per dataset, with both datasets present. The units of the legacy partition stay counted
    until the whole partition is dropped, so the count stays high for a whole catch-up."""
    partition_coverage_ok: bool
    """Whether each event table has a partition that contains the real current time and one that contains it plus one
    partition width. Without both, inserts fail now or within one width."""
    hold_drops: bool
    """Whether the policy holds drop-ready partitions attached."""
    held_partitions: int
    """Partitions whose units are all drop-ready while ``hold_drops`` is set, or 0 when it is not set."""
    drop_grace_days: float
    """The policy's drop grace in days, so a deploy can check the reattach window the running policy keeps."""
    detached_partitions: list[DetachedPartitionHealth]
    """Partitions whose units are ``DETACHED``, oldest detach first."""
    restored_partitions: list[RestoredPartitionHealth]
    """Attached partitions with ``DROPPED`` catalog units. Their dataset does not make a transition while one is listed."""


class RestoredPartitionHealth(TypedDict):
    """Represents one attached partition whose catalog units are ``DROPPED``."""

    dataset: KudosRetentionDataset
    partition_name: str


class DetachedPartitionHealth(TypedDict):
    """Represents one partition kept detached for the drop grace period."""

    dataset: KudosRetentionDataset
    partition_name: str
    detached_at: datetime
    """The tick reference time of the detach."""
    drop_due_at: datetime
    """When the drop falls due: ``detached_at`` plus the drop grace."""


@dataclass(frozen=True, kw_only=True)
class PartitionBounds:
    """Represents one attached partition of an event table, as read from ``pg_catalog``."""

    dataset: KudosRetentionDataset
    name: str
    lower: datetime | None
    """The inclusive lower bound, or None for ``MINVALUE``."""
    upper: datetime | None
    """The exclusive upper bound, or None for ``MAXVALUE``."""
    detach_pending: bool = False
    """Whether an interrupted ``DETACH ... CONCURRENTLY`` left the partition to be finalized."""

    @property
    def is_legacy(self) -> bool:
        """Return whether this is the pre-partitioning heap, which is processed in sub-ranges."""
        return self.name.endswith(KudosPartitioning.LEGACY_SUFFIX)


@dataclass(frozen=True, kw_only=True)
class CatalogUnit:
    """Represents one ``kudos_retention_catalog`` row."""

    id: int
    dataset: KudosRetentionDataset
    partition_name: str
    lower: datetime | None
    upper: datetime
    state: KudosRetentionState
    object_key: str | None = None
    error: str | None = None
    row_count: int | None = None
    """The unit's row count when its replacement was committed, or None before that."""
    max_id: int | None = None
    """The unit's highest id when its replacement was committed, or None before that or for an empty unit."""
    first_failed_at: datetime | None = None
    """When the current run of statistics dump failures began, or None."""
    consecutive_dump_failures: int = 0
    """Failed statistics dump attempts since the last success."""
    last_dump_attempt_at: datetime | None = None
    """When the statistics dump was last attempted, or None."""
    detached_at: datetime | None = None
    """The tick reference time at which the partition was detached for the drop grace period, or None."""
    state_before_detach: KudosRetentionState | None = None
    """The state the unit had before its partition was detached, or None."""

    @property
    def replacement_recorded(self) -> bool:
        """Return whether the step that commits the unit's replacement recorded its row count."""
        return self.row_count is not None

    @property
    def source_range(self) -> str:
        """Return the ``source_range`` that rollups, quarantine rows, statements and archive keys carry."""
        return format_source_range(partition_name=self.partition_name, lower=self.lower, upper=self.upper)

    @property
    def ref(self) -> RetentionUnitRef:
        """Return the unit's identity."""
        return RetentionUnitRef(partition_name=self.partition_name, lower=self.lower, upper=self.upper)


@dataclass(frozen=True)
class NextUnit:
    """Represents what the selection wants next: a unit to step, or a partition whose catalog rows must be created."""

    unit: CatalogUnit | None = None
    partition_needing_units: PartitionBounds | None = None


def format_source_range(*, partition_name: str, lower: datetime | None, upper: datetime) -> str:
    """Return ``<partition>/<lower or min>_<upper>`` with ``%Y%m%dT%H%M%S`` timestamps."""
    lower_text = SOURCE_RANGE_MINIMUM if lower is None else lower.strftime(SOURCE_RANGE_TIME_FORMAT)
    return f"{partition_name}/{lower_text}_{upper.strftime(SOURCE_RANGE_TIME_FORMAT)}"


def _parse_bound_value(raw: str) -> datetime | None:
    value = raw.strip()
    if value in (_MINVALUE, _MAXVALUE):
        return None
    return datetime.fromisoformat(value.strip("'"))


def parse_partition_bound(expression: str) -> tuple[datetime | None, datetime | None] | None:
    """Parse ``pg_get_expr(relpartbound)`` of a range partition into its lower and upper bounds.

    Returns:
        ``(lower, upper)``, where None stands for ``MINVALUE`` or ``MAXVALUE``; None for the default partition.

    Raises:
        ValueError: The expression is not a single-column range bound on a timestamp.
    """
    if expression.strip() == _DEFAULT_PARTITION_BOUND:
        return None
    match = _BOUND_PATTERN.match(expression.strip())
    if match is None:
        raise ValueError(f"Unrecognized partition bound {expression!r}")
    return _parse_bound_value(match["lower"]), _parse_bound_value(match["upper"])


def legacy_unit_ranges(
    *,
    earliest_created: datetime | None,
    partition_upper: datetime,
    width: timedelta,
) -> list[tuple[datetime | None, datetime]]:
    """Return the sub-ranges that process the legacy partition one partition width at a time.

    Boundaries sit on the ``LEGACY_GRID_ANCHOR`` grid. The first range starts at ``MINVALUE`` (None), so no row can fall
    below it, and the last ends at the partition's upper bound.

    Args:
        earliest_created: The partition's oldest ``created``, or None when it is empty.
        partition_upper: The partition's exclusive upper bound.
        width: The sub-range width.
    """
    if earliest_created is None or earliest_created >= partition_upper:
        return [(None, partition_upper)]
    steps_past_anchor = (earliest_created - LEGACY_GRID_ANCHOR) // width
    boundary = LEGACY_GRID_ANCHOR + (steps_past_anchor + 1) * width
    ranges: list[tuple[datetime | None, datetime]] = []
    lower: datetime | None = None
    while boundary < partition_upper:
        ranges.append((lower, boundary))
        lower = boundary
        boundary += width
    ranges.append((lower, partition_upper))
    return ranges


def _unit_sort_key(unit: CatalogUnit) -> tuple[datetime, datetime]:
    return unit.upper, unit.lower or datetime.min


def select_next_unit(
    *,
    dataset: KudosRetentionDataset,
    partitions: Sequence[PartitionBounds],
    catalog: Sequence[CatalogUnit],
    cutoff: datetime,
    archive_configured: bool,
    dump_retry_due_before: datetime | None = None,
    hold_drops: bool = False,
    detached_drop_due_before: datetime | None = None,
) -> NextUnit:
    """Return the oldest actionable unit of one dataset, or the oldest partition that needs catalog rows.

    A unit is eligible when its upper bound is before ``cutoff``. A weekly partition has one unit with the partition's
    bounds, so it is catalogued once its upper bound is before ``cutoff``. The legacy partition holds every row from
    before the swap and is catalogued as soon as it is attached, so that each of its sub-ranges becomes eligible on its
    own age instead of waiting for the partition's upper bound, the swap time, to age past the window. Its drop still
    waits for every sub-range. Units are taken in upper-bound order and skipped while they cannot move:

    - ``DROPPED`` units and ``BLOCKED`` statistics units.
    - ``DUMP_FAILED`` units whose last attempt is not before ``dump_retry_due_before``.
    - Currency units ``CHECKPOINTED`` with no archive.
    - Every drop-ready unit while ``hold_drops`` is set.
    - ``DETACHED`` units detached after ``detached_drop_due_before``.
    - Drop-ready units whose sibling sub-ranges are not all drop-ready yet.

    Statistics units do not chain, so a skipped one does not hold back
    later ones. A ``BLOCKED`` currency unit stops the dataset, because every later checkpoint chains from it. A
    drop-ready unit whose partition is no longer attached is still returned, so an interrupted drop is finished, and
    a blocked one still stops the currency dataset.

    Args:
        dataset: The dataset to select from.
        partitions: Every attached partition of both datasets.
        catalog: The dataset's catalog rows.
        cutoff: ``now`` minus the dataset's window.
        archive_configured: Whether an archive client exists for this tick.
        dump_retry_due_before: ``now`` minus ``DUMP_RETRY_SPACING``; None retries every failed dump.
        hold_drops: Whether drops are held, so no drop-ready unit is returned.
        detached_drop_due_before: ``now`` minus the drop grace. None makes every ``DETACHED`` unit due.
    """
    attached = {partition.name: partition for partition in partitions if partition.dataset == dataset}
    units_by_partition: dict[str, list[CatalogUnit]] = defaultdict(list)
    for catalog_unit in catalog:
        if catalog_unit.dataset == dataset:
            units_by_partition[catalog_unit.partition_name].append(catalog_unit)

    # Each entry sorts by (upper, lower); a partition without catalog rows sorts as its whole range.
    entries: list[tuple[tuple[datetime, datetime], CatalogUnit | PartitionBounds]] = []
    for partition in attached.values():
        if partition.upper is None:
            continue
        partition_units = units_by_partition.get(partition.name)
        if not partition_units:
            if partition.is_legacy or partition.upper < cutoff:
                entries.append(((partition.upper, partition.lower or datetime.min), partition))
            continue
        entries.extend(
            (_unit_sort_key(partition_unit), partition_unit) for partition_unit in partition_units if partition_unit.upper < cutoff
        )
    for partition_name, detached_units in units_by_partition.items():
        if partition_name in attached:
            continue
        # A unit of a detached partition that the drop recount blocked is listed too, so a blocked currency unit
        # stops the dataset whether or not its partition is attached.
        entries.extend(
            (_unit_sort_key(detached_unit), detached_unit)
            for detached_unit in detached_units
            if detached_unit.state in DROP_READY_STATES or detached_unit.state == KudosRetentionState.BLOCKED
        )
    entries.sort(key=lambda entry: entry[0])

    for _, candidate in entries:
        if isinstance(candidate, PartitionBounds):
            return NextUnit(partition_needing_units=candidate)
        if candidate.state == KudosRetentionState.DROPPED:
            continue
        if candidate.state == KudosRetentionState.BLOCKED:
            if dataset == KudosRetentionDataset.CURRENCY:
                return NextUnit()
            continue
        waiting_for_archive = (
            dataset == KudosRetentionDataset.CURRENCY and candidate.state == KudosRetentionState.CHECKPOINTED and not archive_configured
        )
        if waiting_for_archive:
            continue
        if not _dump_retry_due(candidate, dump_retry_due_before):
            continue
        if hold_drops and candidate.state in DROP_READY_STATES:
            continue
        if not _detached_drop_due(candidate, detached_drop_due_before):
            continue
        if candidate.state in DROP_READY_STATES and not _partition_drop_ready(units_by_partition[candidate.partition_name]):
            continue
        return NextUnit(unit=candidate)
    return NextUnit()


def _eligible_uppers(
    dataset: KudosRetentionDataset,
    partitions: Sequence[PartitionBounds],
    catalog: Sequence[CatalogUnit],
    cutoff: datetime,
) -> list[datetime]:
    """Return the upper bounds past ``cutoff`` of the dataset's attached partitions, by sub-range for the legacy partition.

    The legacy partition's units age individually (see ``select_next_unit``), so its backlog age is that of its oldest
    sub-range still online, and the partition bound counts only while the partition has no catalog rows yet.
    """
    legacy_uppers_by_partition: dict[str, list[datetime]] = defaultdict(list)
    for catalog_unit in catalog:
        if catalog_unit.dataset == dataset and catalog_unit.state != KudosRetentionState.DROPPED:
            legacy_uppers_by_partition[catalog_unit.partition_name].append(catalog_unit.upper)
    uppers: list[datetime] = []
    for partition in partitions:
        if partition.dataset != dataset or partition.upper is None:
            continue
        if partition.is_legacy and partition.name in legacy_uppers_by_partition:
            uppers.extend(upper for upper in legacy_uppers_by_partition[partition.name] if upper < cutoff)
        elif partition.upper < cutoff:
            uppers.append(partition.upper)
    return uppers


def _dump_retry_due(unit: CatalogUnit, due_before: datetime | None) -> bool:
    if unit.state != KudosRetentionState.DUMP_FAILED or due_before is None or unit.last_dump_attempt_at is None:
        return True
    return unit.last_dump_attempt_at <= due_before


def _detached_drop_due(unit: CatalogUnit, due_before: datetime | None) -> bool:
    if unit.state != KudosRetentionState.DETACHED or due_before is None or unit.detached_at is None:
        return True
    return unit.detached_at <= due_before


def _partition_drop_ready(partition_units: Sequence[CatalogUnit]) -> bool:
    """Return whether every unit of the partition that is not ``DROPPED`` is drop-ready.

    A detach marks every unit of the partition ``DETACHED`` together, so a ``DETACHED`` unit is drop-ready only while
    every sibling that is not ``DROPPED`` is ``DETACHED`` too. A sibling the drop recount blocked after the detach
    stops the drop.
    """
    live_states = [partition_unit.state for partition_unit in partition_units if partition_unit.state != KudosRetentionState.DROPPED]
    if KudosRetentionState.DETACHED in live_states:
        return all(live_state == KudosRetentionState.DETACHED for live_state in live_states)
    return all(live_state in DROP_READY_STATES for live_state in live_states)


def _units_by_partition(catalog: Sequence[CatalogUnit]) -> dict[tuple[KudosRetentionDataset, str], list[CatalogUnit]]:
    """Return the units that are not ``DROPPED``, grouped by dataset and partition name."""
    grouped: dict[tuple[KudosRetentionDataset, str], list[CatalogUnit]] = defaultdict(list)
    for catalog_unit in catalog:
        if catalog_unit.state != KudosRetentionState.DROPPED:
            grouped[(catalog_unit.dataset, catalog_unit.partition_name)].append(catalog_unit)
    return grouped


def drop_ready_partition_count(catalog: Sequence[CatalogUnit]) -> int:
    """Return how many partitions have units that are not ``DROPPED`` and are all drop-ready.

    While drops are held, these are the partitions the hold keeps.

    Args:
        catalog: Catalog rows of both datasets.
    """
    return sum(1 for partition_units in _units_by_partition(catalog).values() if _partition_drop_ready(partition_units))


def restored_partitions(partitions: Sequence[PartitionBounds], catalog: Sequence[CatalogUnit]) -> list[RestoredPartitionHealth]:
    """Return the attached partitions with a ``DROPPED`` catalog unit, in dataset and name order.

    A dropped partition that is restored and attached under its original name keeps its catalog rows, and the
    selection skips ``DROPPED`` units, so without this check the partition would stay attached and unaccounted.

    Args:
        partitions: Every attached partition of both datasets.
        catalog: Catalog rows of both datasets.
    """
    attached = {(partition.dataset, partition.name) for partition in partitions}
    dropped = {
        (catalog_unit.dataset, catalog_unit.partition_name) for catalog_unit in catalog if catalog_unit.state == KudosRetentionState.DROPPED
    }
    return [
        RestoredPartitionHealth(dataset=dataset, partition_name=partition_name) for dataset, partition_name in sorted(attached & dropped)
    ]


def detached_partitions(catalog: Sequence[CatalogUnit], *, drop_grace: timedelta) -> list[DetachedPartitionHealth]:
    """Return the partitions kept detached for the drop grace period, with when each drop falls due, oldest detach first.

    Args:
        catalog: Catalog rows of both datasets.
        drop_grace: The policy's drop grace.
    """
    found: list[DetachedPartitionHealth] = []
    for (dataset, partition_name), partition_units in _units_by_partition(catalog).items():
        detach_times = [
            partition_unit.detached_at
            for partition_unit in partition_units
            if partition_unit.state == KudosRetentionState.DETACHED and partition_unit.detached_at is not None
        ]
        if not detach_times:
            continue
        detached_at = min(detach_times)
        found.append(
            DetachedPartitionHealth(
                dataset=dataset,
                partition_name=partition_name,
                detached_at=detached_at,
                drop_due_at=detached_at + drop_grace,
            ),
        )
    return sorted(found, key=lambda detached: detached["detached_at"])


def checkpoint_chain_continues(
    *,
    unit_lower: datetime | None,
    previous_boundary: datetime | None,
    later_boundary_exists: bool,
) -> bool:
    """Return whether a checkpoint at a unit's upper bound may be computed from the previous checkpoint.

    The previous checkpoint must end exactly where the unit starts. Only a unit that begins at ``MINVALUE`` starts the
    chain without a previous checkpoint; every deployment's first currency partition is the legacy partition, which
    does. A checkpoint past the unit's upper bound means later checkpoints already chain from this one, and
    recomputing it would break them.

    Args:
        unit_lower: The unit's lower bound, or None for ``MINVALUE``.
        previous_boundary: The catalog boundary at the unit's lower bound (see ``checkpoint_chain_position``), or None.
        later_boundary_exists: Whether a catalog boundary after the unit's upper bound exists.
    """
    if later_boundary_exists:
        return False
    if previous_boundary is None:
        return unit_lower is None
    return previous_boundary == unit_lower


@dataclass(frozen=True, kw_only=True)
class CheckpointChainPosition:
    """Represents where a currency unit's checkpoint sits among the boundaries the catalog records."""

    previous_boundary: datetime | None
    """The unit's lower bound when a recorded unit ends there, else None."""
    later_boundary: datetime | None
    """The earliest recorded boundary after the unit's upper bound, or None."""
    same_boundary_partition: str | None
    """Another partition whose recorded unit already ends at the unit's upper bound, or None."""


def checkpoint_chain_position(*, unit: CatalogUnit, catalog: Sequence[CatalogUnit]) -> CheckpointChainPosition:
    """Return the recorded boundaries around a currency unit, read from the catalog alone.

    A boundary is recorded by a currency unit in ``CHECKPOINT_RECORDED_STATES``, whatever per-user rows it wrote. An
    empty unit does not write any ``kudos_ledger_checkpoints`` rows, so those rows cannot show where the chain ends.

    Args:
        unit: The currency unit about to be checkpointed.
        catalog: Catalog rows. Rows of other datasets and of ``unit`` itself are ignored.
    """
    recorded = [
        candidate
        for candidate in catalog
        if candidate.dataset == KudosRetentionDataset.CURRENCY and candidate.id != unit.id and candidate.state in CHECKPOINT_RECORDED_STATES
    ]
    previous_recorded = unit.lower is not None and any(candidate.upper == unit.lower for candidate in recorded)
    later_uppers = [candidate.upper for candidate in recorded if candidate.upper > unit.upper]
    same_boundary = next(
        (
            candidate.partition_name
            for candidate in recorded
            if candidate.upper == unit.upper and candidate.partition_name != unit.partition_name
        ),
        None,
    )
    return CheckpointChainPosition(
        previous_boundary=unit.lower if previous_recorded else None,
        later_boundary=min(later_uppers) if later_uppers else None,
        same_boundary_partition=same_boundary,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _quote_identifier(name: str) -> str:
    """Return a double-quoted identifier.

    Raises:
        ValueError: The name is not a plain identifier, so it did not come from the partition layout.
    """
    if len(name) > MAXIMUM_IDENTIFIER_LENGTH or not _IDENTIFIER_PATTERN.match(name):
        raise ValueError(f"Refusing unexpected identifier {name!r}")
    return f'"{name}"'


def _postgres_type_sql(postgres_type: str) -> str:
    """Return a manifest column type for use in ``CREATE TABLE``.

    Raises:
        ValueError: The type is not a built-in type as ``format_type`` writes it.
    """
    if not _POSTGRES_TYPE_PATTERN.match(postgres_type):
        raise ValueError(f"Refusing unexpected column type {postgres_type!r}")
    return postgres_type


def _scratch_column_definitions(column_types: list[ArchiveColumn]) -> str:
    """Return the ``CREATE TABLE`` column list for the manifest's columns and types.

    Raises:
        ValueError: A column name is not a plain identifier, or a type is not a built-in type.
    """
    return ", ".join(f"{_quote_identifier(column.name)} {_postgres_type_sql(column.postgres_type)}" for column in column_types)


def _quote_qualified_identifier(name: str) -> str:
    """Return ``table`` or ``schema.table`` with each part double-quoted.

    Raises:
        ValueError: The name has more than one dot, or a part is not a plain identifier.
    """
    parts = name.split(".")
    if len(parts) > 2:
        raise ValueError(f"Refusing {name!r}: expected table or schema.table")
    return ".".join(_quote_identifier(part) for part in parts)


def _range_predicate(unit: CatalogUnit) -> tuple[str, dict[str, datetime]]:
    """Return a bound-parameter ``created`` predicate restricting a partition to the unit's sub-range."""
    if unit.lower is None:
        return "created < :unit_upper", {"unit_upper": unit.upper}
    return "created >= :unit_lower AND created < :unit_upper", {"unit_lower": unit.lower, "unit_upper": unit.upper}


def _timestamp_literal(moment: datetime) -> str:
    return f"TIMESTAMP '{moment.isoformat(sep=' ')}'"


def _range_predicate_literal(unit: CatalogUnit) -> str:
    """Return the ``created`` predicate with inlined values, for ``COPY``, which takes no bound parameters."""
    if unit.lower is None:
        return f"created < {_timestamp_literal(unit.upper)}"
    return f"created >= {_timestamp_literal(unit.lower)} AND created < {_timestamp_literal(unit.upper)}"


def expire_idempotency_keys(*, before: datetime, batch_size: int) -> int:
    """Delete up to ``batch_size`` idempotency keys created before ``before``, oldest first, and commit.

    Returns:
        The keys deleted.
    """
    expired = (
        select(KudosIdempotencyKey.event_id)
        .where(KudosIdempotencyKey.created < before)
        .order_by(KudosIdempotencyKey.created)
        .limit(batch_size)
    )
    result = cast(CursorResult[object], db.session.execute(delete(KudosIdempotencyKey).where(KudosIdempotencyKey.event_id.in_(expired))))
    db.session.commit()
    return int(result.rowcount)


def discover_partitions() -> list[PartitionBounds]:
    """Return the attached range partitions of both event tables, read from ``pg_catalog``."""
    statement = text(
        "SELECT child.relname, pg_get_expr(child.relpartbound, child.oid), inherits.inhdetachpending "
        "FROM pg_inherits AS inherits JOIN pg_class AS child ON child.oid = inherits.inhrelid "
        "WHERE inherits.inhparent = to_regclass(CAST(:parent AS text))",
    )
    partitions: list[PartitionBounds] = []
    for dataset, parent in PARTITION_PARENTS.items():
        for partition_name, bound_expression, detach_pending in db.session.execute(statement, {"parent": parent}).all():
            bounds = parse_partition_bound(str(bound_expression))
            if bounds is None:
                continue
            partitions.append(
                PartitionBounds(
                    dataset=dataset,
                    name=str(partition_name),
                    lower=bounds[0],
                    upper=bounds[1],
                    detach_pending=bool(detach_pending),
                ),
            )
    return partitions


def _catalog_unit(row: KudosRetentionUnit) -> CatalogUnit:
    return CatalogUnit(
        id=row.id,
        dataset=KudosRetentionDataset(row.dataset),
        partition_name=row.partition_name,
        lower=row.unit_lower,
        upper=row.unit_upper,
        state=KudosRetentionState(row.state),
        object_key=row.object_key,
        error=row.error,
        row_count=row.row_count,
        max_id=row.max_id,
        first_failed_at=row.first_failed_at,
        consecutive_dump_failures=row.consecutive_dump_failures or 0,
        last_dump_attempt_at=row.last_dump_attempt_at,
        detached_at=row.detached_at,
        state_before_detach=None if row.state_before_detach is None else KudosRetentionState(row.state_before_detach),
    )


def load_catalog(dataset: KudosRetentionDataset | None = None) -> list[CatalogUnit]:
    """Return catalog units, of one dataset or of both, in upper-bound order."""
    statement = select(KudosRetentionUnit).order_by(KudosRetentionUnit.unit_upper, KudosRetentionUnit.id)
    if dataset is not None:
        statement = statement.where(KudosRetentionUnit.dataset == dataset)
    return [_catalog_unit(row) for row in db.session.scalars(statement).all()]


def _create_units(partition: PartitionBounds, width: timedelta) -> None:
    """Insert the ``PENDING`` catalog rows of a newly eligible partition and commit."""
    if partition.upper is None:
        raise ValueError(f"Partition {partition.name} has no finite upper bound")
    if partition.is_legacy:
        # This and every sub-range predicate on the legacy partition rely on its ``created`` index, which the
        # partitioning migration builds; the weekly partitions are small enough to scan.
        earliest = db.session.execute(text(f"SELECT min(created) FROM ONLY {_quote_identifier(partition.name)}")).scalar_one()
        ranges = legacy_unit_ranges(earliest_created=earliest, partition_upper=partition.upper, width=width)
    else:
        ranges = [(partition.lower, partition.upper)]
    now = _utcnow()
    rows = [
        {
            "dataset": partition.dataset,
            "partition_name": partition.name,
            "unit_lower": lower,
            "unit_upper": upper,
            "state": KudosRetentionState.PENDING,
            "created": now,
            "updated": now,
        }
        for lower, upper in ranges
    ]
    db.session.execute(postgres_insert(KudosRetentionUnit).values(rows).on_conflict_do_nothing())
    db.session.commit()
    logger.info(f"Kudos retention catalogued {len(rows)} units of {partition.dataset} partition {partition.name}")


@dataclass(frozen=True, kw_only=True)
class _StepContext:
    unit: CatalogUnit
    partition: PartitionBounds | None
    """The attached partition, or None when an interrupted drop already detached it."""
    policy: KudosRetentionPolicy
    archive: KudosArchive | None
    now: datetime
    """The tick's reference time."""
    lock_connection: Connection
    """The connection holding the retention lock. The drop checks it is alive before removing anything."""

    @property
    def partition_sql(self) -> str:
        return _quote_identifier(self.unit.partition_name)

    @property
    def parent_sql(self) -> str:
        return _quote_identifier(PARTITION_PARENTS[self.unit.dataset])


def _set_state(unit: CatalogUnit, state: KudosRetentionState, *, error: str | None = None, object_key: str | None = None) -> None:
    """Update one catalog row in the session's current transaction; the caller commits."""
    values: dict[str, object] = {"state": state, "error": error, "updated": _utcnow()}
    if object_key is not None:
        values["object_key"] = object_key
    db.session.execute(update(KudosRetentionUnit).where(KudosRetentionUnit.id == unit.id).values(**values))


def _stepped(context: _StepContext, to_state: KudosRetentionState, detail: str = "") -> RetentionTickResult:
    return RetentionTickResult(
        outcome=RetentionTickOutcome.STEPPED,
        dataset=context.unit.dataset,
        unit=context.unit.ref,
        from_state=context.unit.state,
        to_state=to_state,
        detail=detail,
    )


def _failed(context: _StepContext, detail: str) -> RetentionTickResult:
    logger.error(f"Kudos retention {context.unit.dataset} step failed for {context.unit.source_range}: {detail}")
    return RetentionTickResult(
        outcome=RetentionTickOutcome.FAILED,
        dataset=context.unit.dataset,
        unit=context.unit.ref,
        from_state=context.unit.state,
        detail=detail,
    )


def _block(context: _StepContext, reason: RetentionBlockReason, detail: str = "") -> RetentionTickResult:
    """Mark the unit ``BLOCKED`` with ``reason``, discarding the step's uncommitted work, and commit."""
    db.session.rollback()
    _set_state(context.unit, KudosRetentionState.BLOCKED, error=str(reason))
    db.session.commit()
    return RetentionTickResult(
        outcome=RetentionTickOutcome.BLOCKED,
        dataset=context.unit.dataset,
        unit=context.unit.ref,
        from_state=context.unit.state,
        to_state=KudosRetentionState.BLOCKED,
        detail=f"{reason}: {detail}" if detail else str(reason),
    )


def _has_unapplied_rows(context: _StepContext) -> bool:
    # On a legacy sub-range this probe is bounded by the legacy partition's ``created`` index (see ``_create_units``).
    predicate, parameters = _range_predicate(context.unit)
    pending = "applied = false"
    if context.unit.dataset == KudosRetentionDataset.STATISTICS:
        pending = "applied = false AND quarantined = false"
    statement = text(f"SELECT EXISTS (SELECT 1 FROM ONLY {context.partition_sql} WHERE {pending} AND {predicate})")
    return bool(db.session.execute(statement, parameters).scalar_one())


@dataclass(frozen=True)
class UnitRowCount:
    """Represents the rows of one unit's range, counted with no filter."""

    row_count: int
    max_id: int | None


def _count_unit_rows(unit: CatalogUnit, *, connection: Connection | None = None) -> UnitRowCount:
    """Return the count and highest id of every row in the unit's range, on ``connection`` or the ORM session."""
    predicate, parameters = _range_predicate(unit)
    statement = text(f"SELECT count(*), max(id) FROM ONLY {_quote_identifier(unit.partition_name)} WHERE {predicate}")
    counted = (db.session.execute(statement, parameters) if connection is None else connection.execute(statement, parameters)).one()
    return UnitRowCount(row_count=int(counted[0]), max_id=None if counted[1] is None else int(counted[1]))


def _record_unit_rows(context: _StepContext) -> UnitRowCount:
    """Store the unit's row count and highest id in the session's current transaction. The caller commits.

    Called in the transaction that commits the replacement, so the recorded count is exactly the rows it replaced.
    """
    counted = _count_unit_rows(context.unit)
    db.session.execute(
        update(KudosRetentionUnit)
        .where(KudosRetentionUnit.id == context.unit.id)
        .values(row_count=counted.row_count, max_id=counted.max_id),
    )
    return counted


def _recorded_row_count(unit: CatalogUnit) -> UnitRowCount | None:
    if unit.row_count is None:
        return None
    return UnitRowCount(row_count=unit.row_count, max_id=unit.max_id)


def _first_late_unit(units: Sequence[CatalogUnit], *, connection: Connection | None = None) -> tuple[CatalogUnit, str] | None:
    """Return the first unit whose rows differ from the count recorded with its replacement, with a detail line.

    A unit with no recorded count is reported too, because nothing proves its rows are the replaced rows.
    """
    for unit in units:
        if unit.state == KudosRetentionState.DROPPED:
            continue
        found = _count_unit_rows(unit, connection=connection)
        recorded = _recorded_row_count(unit)
        if recorded != found:
            recorded_text = "none" if recorded is None else f"rows={recorded.row_count} max_id={recorded.max_id}"
            return unit, f"recorded {recorded_text}, found rows={found.row_count} max_id={found.max_id}"
    return None


def _stat_rollup(context: _StepContext) -> RetentionTickResult:
    """Fold the unit's applied statistic rows into day rollups and move its quarantined rows; ``PENDING`` -> ``ROLLED_UP``."""
    begin_repeatable_read()
    if _has_unapplied_rows(context):
        return _block(context, RetentionBlockReason.UNAPPLIED_ROWS)
    predicate, parameters = _range_predicate(context.unit)
    source_parameters: dict[str, object] = {**parameters, "source_range": context.unit.source_range}
    rollups = _quote_identifier(KudosStatRollup.__tablename__)
    quarantine = _quote_identifier(KudosStatQuarantine.__tablename__)
    db.session.execute(text(f"DELETE FROM {rollups} WHERE source_range = :source_range"), source_parameters)
    db.session.execute(
        text(
            f"INSERT INTO {rollups} (day, entry_type, unit, stat_action, record, wp_type, user_id, worker_id, "
            "worker_user_id, team_id, amount_sum, row_count, amount_min, amount_max, created_max, source_range) "
            "SELECT CAST(created AS date), entry_type, unit, stat_action, record, wp_type, user_id, worker_id, "
            "worker_user_id, team_id, sum(amount), count(*), min(amount), max(amount), max(created), :source_range "
            f"FROM ONLY {context.partition_sql} WHERE applied AND NOT quarantined AND {predicate} "
            "GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10",
        ),
        source_parameters,
    )
    event_columns = ", ".join(_quote_identifier(column.name) for column in KudosStatEvent.__table__.columns)
    db.session.execute(text(f"DELETE FROM {quarantine} WHERE source_range = :source_range"), source_parameters)
    db.session.execute(
        text(
            f"INSERT INTO {quarantine} ({event_columns}, source_range) "
            f"SELECT {event_columns}, :source_range FROM ONLY {context.partition_sql} WHERE quarantined AND {predicate}",
        ),
        source_parameters,
    )
    rollup_totals = db.session.execute(
        text(f"SELECT coalesce(sum(amount_sum), 0), coalesce(sum(row_count), 0) FROM {rollups} WHERE source_range = :source_range"),
        source_parameters,
    ).one()
    unit_totals = db.session.execute(
        text(
            f"SELECT coalesce(sum(amount), 0), count(*) FROM ONLY {context.partition_sql} "
            f"WHERE applied AND NOT quarantined AND {predicate}",
        ),
        parameters,
    ).one()
    if (Decimal(rollup_totals[0]), int(rollup_totals[1])) != (Decimal(unit_totals[0]), int(unit_totals[1])):
        db.session.rollback()
        raise RetentionVerificationError(
            f"Rollup totals {tuple(rollup_totals)} differ from unit totals {tuple(unit_totals)} for {context.unit.source_range}",
        )
    _record_unit_rows(context)
    _set_state(context.unit, KudosRetentionState.ROLLED_UP)
    db.session.commit()
    return _stepped(context, KudosRetentionState.ROLLED_UP, f"{int(unit_totals[1])} rows rolled up")


def _copy_unit_sql(context: _StepContext) -> str:
    return f"SELECT * FROM ONLY {context.partition_sql} WHERE {_range_predicate_literal(context.unit)} ORDER BY id"


def _archive_unit(archive: KudosArchive, context: _StepContext, tier: ArchiveTier, allowance: _TierAllowance | None) -> str:
    """Write the unit's rows and manifest to the archive outside any session transaction; returns the object key.

    Args:
        allowance: The tier's remaining bytes for this unit, which bounds the measured upload, or None without a
            budget.

    Raises:
        ArchiveBudgetExceeded: The compressed upload grew past ``allowance``, and the object is not published.
        KudosArchiveError: An object-store request failed.
    """
    db.session.commit()
    raw_connection = db.engine.raw_connection()
    try:
        manifest = archive.put_unit(
            tier=tier,
            dataset=context.unit.dataset,
            source_range=context.unit.source_range,
            copy_sql=_copy_unit_sql(context),
            connection=raw_connection.driver_connection,
            max_compressed_bytes=None if allowance is None else allowance.remaining,
        )
    finally:
        raw_connection.rollback()
        raw_connection.close()
    key = archive.object_key(tier=tier, source_range=context.unit.source_range)
    archive.put_manifest(key, manifest)
    return key


@dataclass(frozen=True)
class _TierAllowance:
    """What one tier can still take for one unit's upload, measured once before the upload."""

    budget: int
    stored_bytes: int
    """The tier's completed objects, as listed."""
    replaced_bytes: int
    """An object already at the unit's own key, which the upload replaces and so is not counted twice."""

    @property
    def remaining(self) -> int:
        return self.budget - self.stored_bytes + self.replaced_bytes


def _tier_allowance(archive: KudosArchive, context: _StepContext, tier: ArchiveTier) -> _TierAllowance | None:
    """Measure the tier once for the unit's upload, or return None when the tier is not budgeted.

    A retry after a crash between the upload and the catalog commit finds the earlier object at the key, which the
    upload replaces.

    Raises:
        KudosArchiveError: The size check or the listing failed.
    """
    budget = context.policy.budget_for(tier)
    if budget is None:
        return None
    own_key = archive.object_key(tier=tier, source_range=context.unit.source_range)
    return _TierAllowance(
        budget=budget,
        stored_bytes=archive.tier_bytes(tier),
        replaced_bytes=archive.object_size(own_key) or 0,
    )


def _budget_refusal(allowance: _TierAllowance | None, estimate: int) -> str | None:
    """Return the detail of a budget refusal when the estimated upload exceeds ``allowance``, else None."""
    if allowance is None or estimate <= allowance.remaining:
        return None
    return f"estimate={estimate} replaced={allowance.replaced_bytes} budget={allowance.budget}"


def _stat_dump(context: _StepContext) -> RetentionTickResult:
    """Dump the unit to the archive; ``ROLLED_UP`` or ``DUMP_FAILED`` -> ``DUMPED``, ``DUMP_FAILED`` or ``BLOCKED``.

    The rollup is already committed, so a retry repeats only the dump. Failures are retried on later ticks, spaced by
    ``DUMP_RETRY_SPACING``, until the first failure is older than ``policy.dump_retry_window``, then the unit blocks
    with ``DUMP_RETRIES_EXHAUSTED``. Without an archive no dump is possible, so the unit goes to ``DUMP_WAIVED`` at
    once and is dropped on the rollup alone. A dump that would take the statistics tier over its budget is not
    uploaded, and the unit blocks with ``STATISTICS_BUDGET_EXCEEDED``.
    """
    archive = context.archive
    if archive is None:
        _set_state(context.unit, KudosRetentionState.DUMP_WAIVED, error=ARCHIVE_NOT_CONFIGURED)
        db.session.commit()
        return _stepped(context, KudosRetentionState.DUMP_WAIVED, ARCHIVE_NOT_CONFIGURED)
    estimate = _estimated_archive_bytes(context)
    db.session.commit()
    try:
        allowance = _tier_allowance(archive, context, ArchiveTier.STATISTICS)
        refusal = _budget_refusal(allowance, estimate)
        if refusal is not None:
            return _block(context, RetentionBlockReason.STATISTICS_BUDGET_EXCEEDED, refusal)
        key = _archive_unit(archive, context, ArchiveTier.STATISTICS, allowance)
    except ArchiveBudgetExceeded as err:
        return _block(context, RetentionBlockReason.STATISTICS_BUDGET_EXCEEDED, str(err))
    except KudosArchiveError as err:
        return _stat_dump_failed(context, str(err))
    _set_state(context.unit, KudosRetentionState.DUMPED, object_key=key)
    db.session.execute(
        update(KudosRetentionUnit)
        .where(KudosRetentionUnit.id == context.unit.id)
        .values(first_failed_at=None, consecutive_dump_failures=0, last_dump_attempt_at=context.now),
    )
    db.session.commit()
    return _stepped(context, KudosRetentionState.DUMPED, key)


def _stat_dump_failed(context: _StepContext, cause: str) -> RetentionTickResult:
    """Record one failed dump attempt, blocking the unit once failures have lasted longer than the retry window."""
    db.session.rollback()
    failures = context.unit.consecutive_dump_failures + 1
    first_failed_at = context.unit.first_failed_at or context.now
    retries_exhausted = context.unit.first_failed_at is not None and context.now - first_failed_at > context.policy.dump_retry_window
    detail = f"{cause} ({failures} consecutive failures since {first_failed_at.isoformat()})"
    if retries_exhausted:
        result = _block(context, RetentionBlockReason.DUMP_RETRIES_EXHAUSTED, detail)
    else:
        _set_state(context.unit, KudosRetentionState.DUMP_FAILED, error=cause)
        result = _stepped(context, KudosRetentionState.DUMP_FAILED, detail)
    db.session.execute(
        update(KudosRetentionUnit)
        .where(KudosRetentionUnit.id == context.unit.id)
        .values(first_failed_at=first_failed_at, consecutive_dump_failures=failures, last_dump_attempt_at=context.now),
    )
    db.session.commit()
    return result


def _ledger_checkpoint(context: _StepContext) -> RetentionTickResult:
    """Commit per-user checkpoints and daily statements for the unit; ``PENDING`` -> ``CHECKPOINTED``."""
    begin_repeatable_read()
    if _has_unapplied_rows(context):
        return _block(context, RetentionBlockReason.UNAPPLIED_ROWS)
    upper = context.unit.upper
    # The catalog, read in this repeatable-read snapshot, records every boundary, including those of empty units.
    position = checkpoint_chain_position(unit=context.unit, catalog=load_catalog(KudosRetentionDataset.CURRENCY))
    if position.same_boundary_partition is not None:
        return _block(
            context,
            RetentionBlockReason.BOUNDARY_ALREADY_CHECKPOINTED,
            f"boundary={upper} partition={position.same_boundary_partition}",
        )
    previous_boundary = position.previous_boundary
    chain_continues = checkpoint_chain_continues(
        unit_lower=context.unit.lower,
        previous_boundary=previous_boundary,
        later_boundary_exists=position.later_boundary is not None,
    )
    if not chain_continues:
        return _block(
            context,
            RetentionBlockReason.CHECKPOINT_CHAIN_GAP,
            f"lower={context.unit.lower} previous={previous_boundary} later={position.later_boundary}",
        )
    predicate, parameters = _range_predicate(context.unit)
    checkpoints = _quote_identifier(KudosLedgerCheckpoint.__tablename__)
    daily = _quote_identifier(KudosLedgerDailyStatement.__tablename__)
    checkpoint_parameters: dict[str, object] = {
        **parameters,
        "boundary": upper,
        "previous_boundary": previous_boundary,
        "reconciliation": KudosEntryType.RECONCILIATION.value,
        "source_range": context.unit.source_range,
    }
    # A retried checkpoint (an operator reset after a later step blocked) replaces its own boundary's rows.
    db.session.execute(text(f"DELETE FROM {checkpoints} WHERE boundary = :boundary"), checkpoint_parameters)
    db.session.execute(
        text(
            f"INSERT INTO {checkpoints} (boundary, user_id, balance_total, escrow_total) "
            "SELECT :boundary, coalesce(movement.user_id, previous.user_id), "
            "coalesce(previous.balance_total, 0) + coalesce(movement.balance_delta, 0), "
            "coalesce(previous.escrow_total, 0) + coalesce(movement.escrow_delta, 0) "
            "FROM (SELECT user_id, sum(amount) FILTER (WHERE NOT escrow) AS balance_delta, "
            "sum(amount) FILTER (WHERE escrow) AS escrow_delta "
            f"FROM ONLY {context.partition_sql} WHERE applied AND entry_type <> :reconciliation AND {predicate} "
            "GROUP BY user_id) AS movement "
            "FULL OUTER JOIN (SELECT user_id, balance_total, escrow_total "
            f"FROM {checkpoints} WHERE boundary = :previous_boundary) AS previous "
            "ON previous.user_id = movement.user_id",
        ),
        checkpoint_parameters,
    )
    db.session.execute(text(f"DELETE FROM {daily} WHERE source_range = :source_range"), checkpoint_parameters)
    db.session.execute(
        text(
            f"INSERT INTO {daily} (user_id, escrow, entry_type, day, amount_sum, row_count, source_range) "
            "SELECT user_id, escrow, entry_type, CAST(created AS date), sum(amount), count(*), :source_range "
            f"FROM ONLY {context.partition_sql} WHERE applied AND {predicate} GROUP BY 1, 2, 3, 4",
        ),
        checkpoint_parameters,
    )
    _record_unit_rows(context)
    _set_state(context.unit, KudosRetentionState.CHECKPOINTED)
    db.session.commit()
    return _stepped(context, KudosRetentionState.CHECKPOINTED)


def _estimated_archive_bytes(context: _StepContext) -> int:
    """Return the expected compressed size of the unit's archive object: its share of the partition heap times the ratio."""
    sizes = db.session.execute(
        text(
            "SELECT pg_relation_size(oid), reltuples FROM pg_class WHERE oid = to_regclass(CAST(:partition AS text))",
        ),
        {"partition": context.unit.partition_name},
    ).one()
    relation_bytes, estimated_rows = int(sizes[0]), float(sizes[1])
    share = Decimal(1)
    if context.partition is not None and context.partition.is_legacy and estimated_rows > 0:
        predicate, parameters = _range_predicate(context.unit)
        unit_rows = int(
            db.session.execute(text(f"SELECT count(*) FROM ONLY {context.partition_sql} WHERE {predicate}"), parameters).scalar_one()
        )
        share = min(Decimal(1), Decimal(unit_rows) / Decimal(estimated_rows))
    return int(Decimal(relation_bytes) * share * ARCHIVE_SIZE_RATIO)


def _ledger_export(context: _StepContext) -> RetentionTickResult:
    """Write the unit's postings to the currency tier; ``CHECKPOINTED`` -> ``EXPORTED``, or ``BLOCKED`` over budget."""
    archive = context.archive
    if archive is None:
        return _failed(context, ARCHIVE_NOT_CONFIGURED)
    estimate = _estimated_archive_bytes(context)
    db.session.commit()
    try:
        allowance = _tier_allowance(archive, context, ArchiveTier.CURRENCY)
        refusal = _budget_refusal(allowance, estimate)
        if refusal is not None:
            return _block(context, RetentionBlockReason.CURRENCY_BUDGET_EXCEEDED, refusal)
        key = _archive_unit(archive, context, ArchiveTier.CURRENCY, allowance)
    except ArchiveBudgetExceeded as err:
        return _block(context, RetentionBlockReason.CURRENCY_BUDGET_EXCEEDED, str(err))
    except KudosArchiveError as err:
        return _failed(context, str(err))
    _set_state(context.unit, KudosRetentionState.EXPORTED, object_key=key)
    db.session.commit()
    return _stepped(context, KudosRetentionState.EXPORTED, key)


def _live_currency_totals(context: _StepContext) -> tuple[int, Decimal, PerUserTotals]:
    predicate, parameters = _range_predicate(context.unit)
    totals = db.session.execute(
        text(f"SELECT count(*), coalesce(sum(amount), 0) FROM ONLY {context.partition_sql} WHERE {predicate}"),
        parameters,
    ).one()
    per_user_rows = db.session.execute(
        text(
            "SELECT user_id, coalesce(sum(amount) FILTER (WHERE NOT escrow), 0), coalesce(sum(amount) FILTER (WHERE escrow), 0) "
            f"FROM ONLY {context.partition_sql} WHERE {predicate} GROUP BY user_id",
        ),
        parameters,
    ).all()
    per_user: PerUserTotals = {int(row[0]): (Decimal(row[1]), Decimal(row[2])) for row in per_user_rows}
    return int(totals[0]), Decimal(totals[1]), per_user


def _ledger_verify(context: _StepContext) -> RetentionTickResult:
    """Read the export back and compare it with its manifest and the online rows; ``EXPORTED`` -> ``VERIFIED``."""
    archive = context.archive
    key = context.unit.object_key
    if archive is None or key is None:
        return _failed(context, ARCHIVE_NOT_CONFIGURED if archive is None else "exported unit has no object key")
    db.session.commit()
    try:
        manifest = archive.get_manifest(key)
        report = archive.verify_unit(key, manifest)
    except KudosArchiveError as err:
        return _failed(context, str(err))
    if not report.matches_manifest:
        return _block(context, RetentionBlockReason.ARCHIVE_MISMATCH, f"fields={','.join(report.mismatches)}")
    if manifest.row_count != context.unit.row_count:
        # The export must hold exactly the rows the checkpoint summed, not merely the rows online now.
        return _block(
            context,
            RetentionBlockReason.ONLINE_DRIFT,
            f"recorded={context.unit.row_count} archived={manifest.row_count}",
        )
    begin_repeatable_read()
    live_count, live_sum, live_per_user = _live_currency_totals(context)
    if (report.row_count, report.amount_sum, report.per_user_totals) != (live_count, live_sum, live_per_user):
        return _block(context, RetentionBlockReason.ONLINE_DRIFT, f"archived={report.row_count} online={live_count}")
    _set_state(context.unit, KudosRetentionState.VERIFIED)
    db.session.commit()
    return _stepped(context, KudosRetentionState.VERIFIED)


def _relation_exists(name: str) -> bool:
    statement = text("SELECT to_regclass(CAST(:name AS text)) IS NOT NULL")
    return bool(db.session.execute(statement, {"name": name}).scalar_one())


def _long_lock_holder(context: _StepContext, connection: Connection) -> str | None:
    """Return the pid and transaction age of the oldest other session locking the partition or its parent past the age.

    Returns:
        A detail line, or None when no such session exists.
    """
    statement = text(
        "SELECT activity.pid, extract(epoch FROM now() - activity.xact_start) FROM pg_locks AS locks "
        "JOIN pg_stat_activity AS activity ON activity.pid = locks.pid "
        "WHERE locks.granted AND locks.pid <> pg_backend_pid() "
        "AND locks.relation IN (to_regclass(CAST(:partition AS text)), to_regclass(CAST(:parent AS text))) "
        "AND activity.xact_start < now() - make_interval(secs => :age_seconds) "
        "ORDER BY activity.xact_start LIMIT 1",
    )
    holder = connection.execute(
        statement,
        {
            "partition": context.unit.partition_name,
            "parent": PARTITION_PARENTS[context.unit.dataset],
            "age_seconds": context.policy.drop_defer_lock_age.total_seconds(),
        },
    ).one_or_none()
    if holder is None:
        return None
    return f"pid {int(holder[0])} holds a lock in a transaction open for {float(holder[1]):.0f}s"


def _deferred(context: _StepContext, detail: str) -> RetentionTickResult:
    logger.info(f"Kudos retention deferred the drop of {context.unit.dataset} partition {context.unit.partition_name}: {detail}")
    return RetentionTickResult(
        outcome=RetentionTickOutcome.DEFERRED,
        dataset=context.unit.dataset,
        unit=context.unit.ref,
        from_state=context.unit.state,
        detail=detail,
    )


def _check_currency_archive(context: _StepContext, siblings: Sequence[CatalogUnit]) -> RetentionTickResult | None:
    """Return the outcome that stops the drop when a currency unit's object or manifest cannot be checked now, else None.

    The check compares metadata only: the object's stored size against its manifest, and the manifest's row count
    against the unit's recorded count. The digest was verified at the ``VERIFIED`` step, and re-reading a legacy week
    before every drop would cost more than it proves.
    """
    # The siblings are detached value objects. Release the ORM transaction before object-store I/O.
    db.session.commit()
    for sibling in siblings:
        if sibling.dataset != KudosRetentionDataset.CURRENCY or sibling.state == KudosRetentionState.DROPPED:
            continue
        current = replace(context, unit=sibling)
        if sibling.object_key is None:
            return _block(current, RetentionBlockReason.ARCHIVE_OBJECT_MISSING, "no object key is recorded")
        if context.archive is None:
            return _failed(current, ARCHIVE_NOT_CONFIGURED)
        try:
            size = context.archive.object_size(sibling.object_key)
            if size is None:
                return _block(current, RetentionBlockReason.ARCHIVE_OBJECT_MISSING, sibling.object_key)
            manifest = context.archive.get_manifest(sibling.object_key)
        except KudosArchiveError as err:
            return _failed(current, f"archive unavailable before drop: {err}")
        if size != manifest.compressed_bytes or manifest.row_count != sibling.row_count:
            return _block(current, RetentionBlockReason.ARCHIVE_MISMATCH, "archive metadata changed before drop")
    return None


def _holds_statistics_dump(unit: CatalogUnit) -> bool:
    """Return whether ``unit`` is a statistics unit whose drop relies on its archive dump."""
    if unit.dataset != KudosRetentionDataset.STATISTICS:
        return False
    if unit.state == KudosRetentionState.DETACHED:
        return unit.state_before_detach == KudosRetentionState.DUMPED
    return unit.state == KudosRetentionState.DUMPED


def _check_statistics_dumps(context: _StepContext, siblings: Sequence[CatalogUnit]) -> RetentionTickResult | None:
    """Return the outcome that stops the drop when a dumped statistics unit's object is missing or unreachable, else None.

    A dump can disappear after it was written, by a delete outside the horde. Eviction does not reach the dump of a
    unit that is not ``DROPPED``. A missing dump blocks the unit with ``ARCHIVE_OBJECT_MISSING``, so its raw rows are
    not dropped without a copy unless an operator waives the dump.
    """
    db.session.commit()
    for sibling in siblings:
        if not _holds_statistics_dump(sibling):
            continue
        current = replace(context, unit=sibling)
        if sibling.object_key is None:
            return _block(current, RetentionBlockReason.ARCHIVE_OBJECT_MISSING, "no object key is recorded")
        if context.archive is None:
            return _failed(current, ARCHIVE_NOT_CONFIGURED)
        try:
            size = context.archive.object_size(sibling.object_key)
        except KudosArchiveError as err:
            return _failed(current, f"archive unavailable before drop: {err}")
        if size is None:
            return _block(current, RetentionBlockReason.ARCHIVE_OBJECT_MISSING, sibling.object_key)
    return None


def _check_archive_before_drop(context: _StepContext, siblings: Sequence[CatalogUnit]) -> RetentionTickResult | None:
    """Return the outcome that stops the drop when an archive object of the partition's units fails its check, else None."""
    currency_problem = _check_currency_archive(context, siblings)
    if currency_problem is not None:
        return currency_problem
    return _check_statistics_dumps(context, siblings)


def _drop_partition(context: _StepContext) -> RetentionTickResult:
    """Detach and drop the unit's partition and mark all of its units ``DROPPED``.

    ``DETACH PARTITION ... CONCURRENTLY`` refuses to run inside a transaction block, so it runs on an autocommit
    connection after the session commits. An interrupted concurrent detach leaves the partition pending, which
    ``FINALIZE`` completes.

    With ``policy.drop_grace`` set, a partition that was never detached for the grace period is detached and kept: its
    units become ``DETACHED`` with ``detached_at`` set to the tick's reference time, and nothing is dropped. A later
    call drops a ``DETACHED`` partition once the grace has passed, and marks it ``DROPPED`` at once when its table no
    longer exists.

    Every unit's rows are recounted against the count recorded with its replacement, before the detach and again
    after it, when no row can arrive any more. A ``DETACHED`` partition is recounted again before its drop, because
    it may have been reattached and written in between. A difference blocks that unit with ``LATE_ROWS`` and nothing
    is dropped. The detach is recorded on every unit (``DETACHED``, ``detached_at``) before the recount that follows
    it, with or without a grace period, so a unit that recount blocks keeps ``detached_at``, its siblings stay
    ``DETACHED`` and the partition is not drop-ready until ``reattach_partition`` restores it.

    A session whose transaction is older than ``policy.drop_defer_lock_age`` and that holds a lock on the partition
    or its parent defers the drop, as does a ``lock_timeout`` while waiting, because the detach would otherwise wait
    for that transaction to end while the tick holds the retention lock. The drop connection is discarded rather than
    returned to the pool, so its ``lock_timeout`` does not reach other users of the pool.
    """
    if context.policy.hold_drops:
        return _failed(context, "drops are held")
    siblings = [sibling for sibling in load_catalog(context.unit.dataset) if sibling.partition_name == context.unit.partition_name]
    if not _partition_drop_ready(siblings):
        return _failed(context, "partition has units whose replacement is not committed")
    archive_problem = _check_archive_before_drop(context, siblings)
    if archive_problem is not None:
        return archive_problem
    partition_exists = _relation_exists(context.unit.partition_name)
    detach_times = [
        sibling.detached_at for sibling in siblings if sibling.state == KudosRetentionState.DETACHED and sibling.detached_at is not None
    ]
    grace_detached_at = min(detach_times) if detach_times else None
    if grace_detached_at is not None and partition_exists and grace_detached_at > context.now - context.policy.drop_grace:
        return _failed(context, f"the detached partition is not due for its drop until {grace_detached_at + context.policy.drop_grace}")
    keep_detached = partition_exists and grace_detached_at is None and context.policy.drop_grace > timedelta(0)
    if partition_exists:
        late_unit = _first_late_unit(siblings)
        if late_unit is not None:
            detail = late_unit[1] if grace_detached_at is None else f"{late_unit[1]}. {_DETACHED_LATE_ROWS_HINT}"
            return _block(replace(context, unit=late_unit[0]), RetentionBlockReason.LATE_ROWS, detail)
    db.session.commit()
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as autocommit_connection:
        try:
            autocommit_connection.execute(text(f"SET lock_timeout = '{DROP_LOCK_TIMEOUT}'"))
            lock_holder = _long_lock_holder(context, autocommit_connection)
            if lock_holder is not None:
                return _deferred(context, lock_holder)
            try:
                # The advisory lock lives and dies with this connection. A lost connection means another tick may run.
                context.lock_connection.execute(text("SELECT 1"))
            except SQLAlchemyError as err:
                return _failed(context, f"retention lock connection lost before the drop: {err}")
            try:
                if context.partition is not None:
                    detach_mode = "FINALIZE" if context.partition.detach_pending else "CONCURRENTLY"
                    autocommit_connection.execute(
                        text(f"ALTER TABLE {context.parent_sql} DETACH PARTITION {context.partition_sql} {detach_mode}"),
                    )
                if partition_exists and grace_detached_at is None:
                    # Recorded before the recount, so a unit it blocks still has ``detached_at`` and the partition can
                    # be reattached with ``reattach_partition``.
                    _record_detachment(context)
                if partition_exists:
                    detached_late_unit = _first_late_unit(siblings, connection=autocommit_connection)
                    if detached_late_unit is not None:
                        detail = f"{detached_late_unit[1]}. {_DETACHED_LATE_ROWS_HINT}"
                        return _block(replace(context, unit=detached_late_unit[0]), RetentionBlockReason.LATE_ROWS, detail)
                if not keep_detached:
                    # The recount can take a while, so the archive is checked again immediately before the removal.
                    archive_problem = _check_archive_before_drop(context, siblings)
                    if archive_problem is not None:
                        return archive_problem
                    autocommit_connection.execute(text(f"DROP TABLE IF EXISTS {context.partition_sql}"))
            except OperationalError as err:
                if getattr(err.orig, "pgcode", None) != LOCK_NOT_AVAILABLE_SQLSTATE:
                    raise
                return _deferred(context, f"lock_timeout {DROP_LOCK_TIMEOUT} expired: {err.orig}")
        finally:
            # ``SET lock_timeout`` lasts for the database session, and the pool's reset on return does not undo it, so
            # the pool discards this connection instead of handing the setting to its next user.
            autocommit_connection.invalidate()
    if keep_detached:
        return _keep_detached(context)
    db.session.execute(
        update(KudosRetentionUnit)
        .where(
            KudosRetentionUnit.dataset == context.unit.dataset,
            KudosRetentionUnit.partition_name == context.unit.partition_name,
            KudosRetentionUnit.state != KudosRetentionState.DROPPED,
        )
        .values(state=KudosRetentionState.DROPPED, error=None, updated=_utcnow()),
    )
    db.session.commit()
    logger.info(f"Kudos retention dropped {context.unit.dataset} partition {context.unit.partition_name}")
    return _stepped(context, KudosRetentionState.DROPPED, context.unit.partition_name)


def _record_detachment(context: _StepContext) -> None:
    """Mark every unit of the detached partition ``DETACHED`` at the tick's reference time, keeping each prior state, and commit."""
    db.session.execute(
        update(KudosRetentionUnit)
        .where(
            KudosRetentionUnit.dataset == context.unit.dataset,
            KudosRetentionUnit.partition_name == context.unit.partition_name,
            KudosRetentionUnit.state != KudosRetentionState.DROPPED,
        )
        .values(
            state_before_detach=KudosRetentionUnit.state,
            state=KudosRetentionState.DETACHED,
            detached_at=context.now,
            error=None,
            updated=_utcnow(),
        ),
    )
    db.session.commit()


def _keep_detached(context: _StepContext) -> RetentionTickResult:
    """Return the outcome of a detach whose partition is kept for the drop grace period."""
    drop_due_at = context.now + context.policy.drop_grace
    logger.info(
        f"Kudos retention detached {context.unit.dataset} partition {context.unit.partition_name}, kept until its drop at {drop_due_at}",
    )
    return _stepped(context, KudosRetentionState.DETACHED, context.unit.partition_name)


_STEP_FUNCTIONS: Final[Mapping[RetentionStep, Callable[[_StepContext], RetentionTickResult]]] = {
    RetentionStep.ROLLUP: _stat_rollup,
    RetentionStep.DUMP: _stat_dump,
    RetentionStep.CHECKPOINT: _ledger_checkpoint,
    RetentionStep.EXPORT: _ledger_export,
    RetentionStep.VERIFY: _ledger_verify,
    RetentionStep.DROP: _drop_partition,
}


def _next_unit(
    dataset: KudosRetentionDataset,
    partitions: Sequence[PartitionBounds],
    *,
    cutoff: datetime,
    policy: KudosRetentionPolicy,
    archive_configured: bool,
    now: datetime,
) -> CatalogUnit | None:
    """Return the dataset's next actionable unit, cataloguing newly eligible partitions on the way."""
    # Each pass catalogues at most one partition, so the bound only guards against a catalog insert that conflicts
    # without creating the row the selection expects.
    for _ in range(len(partitions) + 1):
        selection = select_next_unit(
            dataset=dataset,
            partitions=partitions,
            catalog=load_catalog(dataset),
            cutoff=cutoff,
            archive_configured=archive_configured,
            dump_retry_due_before=now - DUMP_RETRY_SPACING,
            hold_drops=policy.hold_drops,
            detached_drop_due_before=now - policy.drop_grace,
        )
        if selection.partition_needing_units is None:
            return selection.unit
        _create_units(selection.partition_needing_units, policy.partition_width)
    raise RuntimeError(f"Kudos retention could not catalogue the eligible {dataset} partitions")


def _run_step(
    unit: CatalogUnit,
    partitions: Sequence[PartitionBounds],
    *,
    policy: KudosRetentionPolicy,
    archive: KudosArchive | None,
    now: datetime,
    lock_connection: Connection,
) -> RetentionTickResult:
    partition = next(
        (candidate for candidate in partitions if candidate.dataset == unit.dataset and candidate.name == unit.partition_name),
        None,
    )
    context = _StepContext(unit=unit, partition=partition, policy=policy, archive=archive, now=now, lock_connection=lock_connection)
    step = STEP_TABLE[unit.dataset][unit.state]
    try:
        return _STEP_FUNCTIONS[step](context)
    except RetentionVerificationError as err:
        db.session.rollback()
        return _failed(context, str(err))


def _run_locked_tick(
    now: datetime,
    policy: KudosRetentionPolicy,
    archive: KudosArchive | None,
    datasets: Sequence[KudosRetentionDataset],
    lock_connection: Connection,
) -> RetentionTickResult:
    ensure_kudos_event_partitions(until=now + 2 * policy.partition_width, width=policy.partition_width)
    db.session.commit()
    expire_idempotency_keys(before=now - policy.key_lifetime, batch_size=EXPIRE_KEYS_BATCH_SIZE)
    if archive is not None:
        _abort_stale_uploads(archive)
    if not policy.enabled:
        return RetentionTickResult(outcome=RetentionTickOutcome.IDLE, detail="retention disabled")
    partitions = discover_partitions()
    restored = restored_partitions(partitions, load_catalog())
    refusals: list[RetentionTickResult] = []
    candidates: list[CatalogUnit] = []
    for dataset in datasets:
        restored_names = [entry["partition_name"] for entry in restored if entry["dataset"] == dataset]
        if restored_names:
            refusals.append(
                RetentionTickResult(
                    outcome=RetentionTickOutcome.BLOCKED,
                    dataset=dataset,
                    detail=f"{RESTORED_PARTITION_REFUSAL}: {', '.join(restored_names)}",
                ),
            )
            continue
        unit = _next_unit(
            dataset,
            partitions,
            cutoff=now - policy.window_for(dataset),
            policy=policy,
            archive_configured=archive is not None,
            now=now,
        )
        if unit is not None:
            candidates.append(unit)
    if policy.hold_drops:
        held_partitions = drop_ready_partition_count(load_catalog())
        if held_partitions:
            logger.info(f"Kudos retention holds {held_partitions} partitions")
    db.session.commit()
    if not candidates:
        if refusals:
            return refusals[0]
        return RetentionTickResult(outcome=RetentionTickOutcome.IDLE, detail="no actionable unit")
    failures: list[RetentionTickResult] = []
    # A failed or deferred step makes no transition, so the other dataset's unit still gets this tick. An unreachable
    # archive must not stall rollups behind a currency export.
    for unit in sorted(candidates, key=_unit_sort_key):
        result = _run_step(unit, partitions, policy=policy, archive=archive, now=now, lock_connection=lock_connection)
        if result.outcome not in (RetentionTickOutcome.FAILED, RetentionTickOutcome.DEFERRED):
            return result
        failures.append(result)
    if refusals:
        return refusals[0]
    return failures[0]


def _abort_stale_uploads(archive: KudosArchive) -> None:
    """Abort multipart uploads that a killed process left open, so their stored parts stop taking space."""
    try:
        aborted = archive.abort_stale_multipart_uploads(older_than=STALE_UPLOAD_AGE)
    except KudosArchiveError as err:
        # Stale parts only cost space, so the tick's retention work goes ahead.
        logger.warning(f"Kudos retention could not abort stale archive uploads: {err}")
        return
    if aborted:
        logger.info(f"Kudos retention aborted {aborted} stale archive uploads")


def run_retention_tick(
    *,
    now: datetime | None = None,
    policy: KudosRetentionPolicy = KUDOS_RETENTION_POLICY,
    archive: KudosArchive | None = None,
    dataset: KudosRetentionDataset | None = None,
    confirm_live_data_loss: bool = False,
) -> RetentionTickResult:
    """Make at most one retention transition, after creating upcoming partitions and expiring idempotency keys.

    The retention lock is a session-level advisory lock on a dedicated autocommit connection, held for the whole tick
    without an open transaction, because each step commits the ORM session and an export can take minutes.

    A reference time later than the real clock makes partitions that still receive rows eligible, so on a live
    database it drops current data. Such a tick is refused unless ``confirm_live_data_loss`` is True.

    Args:
        now: The reference time (naive UTC); the current time when None.
        policy: The retention policy.
        archive: The archive client, or None when no archive is configured.
        dataset: Step only this dataset's units, for the operator command; both datasets when None.
        confirm_live_data_loss: Accept a future ``now``, for a rehearsal on a restored copy.

    With an archive, the tick lists both tiers after the step, still under the lock, so no step writes a key between
    the listing and the catalog read. Units whose object is not listed lose their ``object_key``, and the result holds
    the tier sizes.

    Returns:
        What the tick did; ``REFUSED`` for an unconfirmed future ``now``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: A database statement failed; the session is rolled back first.
    """
    real_now = _utcnow()
    if now is None:
        now = real_now
    if now > real_now and not confirm_live_data_loss:
        return RetentionTickResult(outcome=RetentionTickOutcome.REFUSED, detail=FUTURE_REFERENCE_REFUSAL)
    if SQLITE_MODE:
        return RetentionTickResult(outcome=RetentionTickOutcome.IDLE, detail="partitioning requires PostgreSQL")
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_connection:
        if not try_acquire_retention_lock(connection=lock_connection):
            return RetentionTickResult(outcome=RetentionTickOutcome.SKIPPED_LOCK)
        try:
            datasets = list(KudosRetentionDataset) if dataset is None else [dataset]
            result = _run_locked_tick(now, policy, archive, datasets, lock_connection)
            if archive is None:
                return result
            return replace(result, archive_tier_bytes=_reconcile_archive_catalog(archive))
        except BaseException:
            db.session.rollback()
            raise
        finally:
            _release_lock_connection(lock_connection)


def units_without_archive_objects(
    listed_keys: Collection[str],
    *,
    datasets: Collection[KudosRetentionDataset] = tuple(KudosRetentionDataset),
) -> list[CatalogUnit]:
    """Return the catalog units of ``datasets`` whose ``object_key`` is not among ``listed_keys``, in upper-bound order.

    Args:
        listed_keys: Every object key of the datasets' tiers, from one listing.
        datasets: The datasets whose units are compared.
    """
    return [
        catalog_unit
        for catalog_unit in sorted(load_catalog(), key=_unit_sort_key)
        if catalog_unit.dataset in datasets and catalog_unit.object_key is not None and catalog_unit.object_key not in listed_keys
    ]


def _currency_unit_awaiting_drop(unit: CatalogUnit) -> bool:
    awaiting_states = (KudosRetentionState.EXPORTED, KudosRetentionState.VERIFIED, KudosRetentionState.DETACHED)
    return unit.dataset == KudosRetentionDataset.CURRENCY and unit.state in awaiting_states


def _reconcile_archive_catalog(archive: KudosArchive) -> dict[ArchiveTier, int] | None:
    """Update the units whose object is not in the archive, commit, and return each tier's stored bytes.

    ``archive evict`` clears the keys of the units it evicts, so a listed key without its object was removed outside
    the horde, or by an eviction that stopped before it updated the catalog. A currency unit in ``EXPORTED``,
    ``VERIFIED`` or ``DETACHED`` becomes ``BLOCKED`` with ``ARCHIVE_OBJECT_MISSING``, because its rows are still online
    and the drop would otherwise remove them without a copy. A ``DUMPED`` statistics unit whose partition is attached
    becomes ``DUMP_FAILED``, so the next ticks dump its rows again within the dump retry window. A detached one
    becomes ``BLOCKED`` with ``ARCHIVE_OBJECT_MISSING``. A ``DROPPED`` statistics unit keeps its key, so the archive
    audit keeps reporting the lost object. Every other unit loses the key. A listing failure skips the reconciliation
    for this tick.

    Returns:
        Each tier's stored bytes, or None when a listing failed.
    """
    try:
        sizes_by_tier = {tier: archive.object_sizes(tier) for tier in ArchiveTier}
    except KudosArchiveError as err:
        logger.warning(f"Kudos retention could not list the archive to reconcile object keys: {err}")
        return None
    listed_keys = {key for sizes in sizes_by_tier.values() for key in sizes}
    for missing_unit in units_without_archive_objects(listed_keys):
        if missing_unit.dataset == KudosRetentionDataset.STATISTICS and missing_unit.state == KudosRetentionState.DROPPED:
            continue
        values: dict[str, object] = {"object_key": None, "updated": _utcnow()}
        if _holds_statistics_dump(missing_unit) and missing_unit.state == KudosRetentionState.DUMPED:
            # The raw rows are still online, so the dump is repeated on the next tick, without the spacing that
            # follows a failed attempt. The retry window starts now.
            values |= {
                "state": KudosRetentionState.DUMP_FAILED,
                "error": f"{RetentionBlockReason.ARCHIVE_OBJECT_MISSING}: {missing_unit.object_key}",
                "first_failed_at": missing_unit.first_failed_at or _utcnow(),
                "last_dump_attempt_at": None,
            }
            # LOG CONTRACT: the line starts with RETENTION_DUMP_FAILED_LOG_PREFIX ("Kudos retention dump failed").
            logger.warning(
                f"{RETENTION_DUMP_FAILED_LOG_PREFIX} for {missing_unit.source_range}: the archive does not hold "
                f"{missing_unit.object_key}, so the unit is dumped again",
            )
        elif _holds_statistics_dump(missing_unit):
            values |= {"state": KudosRetentionState.BLOCKED, "error": str(RetentionBlockReason.ARCHIVE_OBJECT_MISSING)}
            # LOG CONTRACT: the line starts with RETENTION_BLOCKED_LOG_PREFIX ("Kudos retention blocked").
            logger.warning(
                f"{RETENTION_BLOCKED_LOG_PREFIX} {missing_unit.dataset} unit {missing_unit.source_range}: "
                f"{RetentionBlockReason.ARCHIVE_OBJECT_MISSING}: the archive does not hold {missing_unit.object_key} "
                f"(state {missing_unit.state})",
            )
        elif _currency_unit_awaiting_drop(missing_unit):
            # The drop would remove the only copy of these postings, so the unit stops until it is exported again.
            values |= {"state": KudosRetentionState.BLOCKED, "error": str(RetentionBlockReason.ARCHIVE_OBJECT_MISSING)}
            # LOG CONTRACT: the line starts with RETENTION_BLOCKED_LOG_PREFIX ("Kudos retention blocked").
            logger.warning(
                f"{RETENTION_BLOCKED_LOG_PREFIX} {missing_unit.dataset} unit {missing_unit.source_range}: "
                f"{RetentionBlockReason.ARCHIVE_OBJECT_MISSING}: the archive does not hold {missing_unit.object_key} "
                f"(state {missing_unit.state})",
            )
        else:
            logger.warning(
                f"Kudos retention cleared object key {missing_unit.object_key} of {missing_unit.dataset} unit {missing_unit.id} "
                f"({missing_unit.source_range}, {missing_unit.state}), because the archive does not hold that object",
            )
        db.session.execute(update(KudosRetentionUnit).where(KudosRetentionUnit.id == missing_unit.id).values(**values))
    db.session.commit()
    return {tier: sum(sizes.values()) for tier, sizes in sizes_by_tier.items()}


def _release_lock_connection(lock_connection: Connection) -> None:
    try:
        release_retention_lock(connection=lock_connection)
    except SQLAlchemyError as err:
        # PostgreSQL releases a session lock when its session ends, so a lost connection has already released it.
        logger.warning(f"Kudos retention lock release failed, the session already ended: {err}")


class RetentionLockBusyError(Exception):
    """Another process holds the retention lock, so an operator change to the catalog would race a tick."""


@contextlib.contextmanager
def operator_retention_lock() -> Iterator[None]:
    """Hold the retention lock for an operator catalog change or baseline capture.

    Raises:
        RetentionLockBusyError: A tick or another operator command holds the lock.
    """
    if SQLITE_MODE:
        yield
        return
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_connection:
        if not try_acquire_retention_lock(connection=lock_connection):
            raise RetentionLockBusyError("The kudos retention lock is held, most likely by a running tick. Retry when it finishes")
        try:
            yield
        finally:
            _release_lock_connection(lock_connection)


def parse_rehearsal_as_of(raw: str, *, policy: KudosRetentionPolicy, real_now: datetime) -> datetime:
    """Parse and validate the ``--as-of`` time of a rehearsal tick, as naive UTC.

    A rehearsal on a restored copy runs a tick as though it were a later time, so partitions become eligible without
    waiting out the window. Only a time at or after the real clock is accepted: an earlier one cannot make anything
    newly eligible and is more likely a typo.

    Args:
        raw: An ISO-8601 timestamp; one without an offset is read as UTC.
        policy: The retention policy the tick will use.
        real_now: The real current time, naive UTC.

    Raises:
        ValueError: Retention is disabled in ``policy``, the timestamp does not parse, it is before ``real_now``, or it
            is later than ``real_now`` plus the longer window, the drop grace and ``REHEARSAL_SLACK_WIDTHS`` partition
            widths.
    """
    if not policy.enabled:
        raise ValueError(f"--as-of needs retention enabled; set {RETENTION_ENABLED_ENV}=true for the rehearsal")
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError as err:
        raise ValueError(f"--as-of must be an ISO-8601 timestamp, got {raw!r}") from err
    as_of = parsed if parsed.tzinfo is None else parsed.astimezone(UTC).replace(tzinfo=None)
    if as_of < real_now:
        raise ValueError(f"--as-of {as_of.isoformat()} is before the current time {real_now.isoformat()}; a rehearsal moves time forward")
    # ensure_kudos_event_partitions creates every missing weekly partition up to as_of plus two widths in one
    # transaction, and a far-future value takes more locks than the lock table holds. A rehearsal only needs to reach
    # one partition width past the window, plus the drop grace to see a detached partition dropped, so a few widths of
    # slack is enough.
    longer_window = max(policy.currency_window, policy.statistics_window)
    latest = real_now + longer_window + policy.drop_grace + REHEARSAL_SLACK_WIDTHS * policy.partition_width
    if as_of > latest:
        raise ValueError(
            f"--as-of {as_of.isoformat()} is after the latest rehearsal time {latest.isoformat()} (now plus the longer window, "
            f"the drop grace and {REHEARSAL_SLACK_WIDTHS} partition widths). A rehearsal only needs to reach one partition width "
            "past the window and the drop grace",
        )
    return as_of


@dataclass(frozen=True, kw_only=True)
class DailyStatementLine:
    """Represents one day of a user's postings of one entry type to one balance, summed across source ranges."""

    day: date
    entry_type: str
    escrow: bool
    amount_sum: Decimal
    row_count: int


def unblock_unit(unit_id: int) -> CatalogUnit:
    """Return a ``BLOCKED`` unit to the state its next attempt starts from, clearing its error, and commit.

    A unit blocked by ``reattach_partition`` returns to ``state_before_detach``, so its partition is drop-ready again
    and the next tick detaches it again. Otherwise, a currency unit whose checkpoint is already committed returns to
    ``CHECKPOINTED``, so the next tick repeats the export and verification against that checkpoint. Recomputing the
    checkpoint would be refused once later units have chained from it. Every other unit returns to ``PENDING``, and
    each step replaces what an earlier attempt wrote.

    Args:
        unit_id: The catalog id of the unit.

    Returns:
        The unit as reset.

    Raises:
        ValueError: No unit has that id, the unit is not ``BLOCKED``, or its partition is kept detached.
        RetentionLockBusyError: A tick holds the retention lock.
    """
    with operator_retention_lock():
        row = db.session.get(KudosRetentionUnit, unit_id)
        if row is None:
            raise ValueError(f"No kudos retention unit {unit_id}")
        if row.state != KudosRetentionState.BLOCKED:
            raise ValueError(f"Kudos retention unit {unit_id} is {row.state}, not {KudosRetentionState.BLOCKED}")
        if row.detached_at is not None:
            raise ValueError(f"Kudos retention unit {unit_id} belongs to detached partition {row.partition_name}. Reattach it first")
        checkpoint_committed = row.dataset == KudosRetentionDataset.CURRENCY and row.row_count is not None
        reset_state = KudosRetentionState.CHECKPOINTED if checkpoint_committed else KudosRetentionState.PENDING
        if row.state_before_detach is not None:
            reset_state = row.state_before_detach
        row.state = reset_state
        row.state_before_detach = None
        row.error = None
        row.updated = _utcnow()
        db.session.commit()
        logger.info(f"Kudos retention unit {unit_id} ({row.dataset} {row.partition_name}) reset to {reset_state}")
        return _catalog_unit(row)


def recheckpoint_from_unit(unit_id: int) -> list[CatalogUnit]:
    """Return a blocked currency unit and every later currency unit to ``PENDING``, so their checkpoints are recomputed.

    A late row in a checkpointed unit makes its checkpoint, daily statements and recorded row count wrong, and every
    later checkpoint repeats the wrong totals. Resetting the unit alone is refused by the chain check, so the
    whole tail is reset and the next ticks recompute it in order from the unit's own rows. Each reset unit loses its
    recorded row count, object key and ``state_before_detach``, and a repeated export replaces the object at its deterministic key.
    The checkpoints at the reset units' boundaries and their daily statements are deleted in the same transaction,
    so until the chain is recomputed the latest checkpoint is the last one still valid, and reconciliation reads
    every later posting from ``kudos_ledger``.

    A blocked currency unit stops the currency dataset, so every later unit is still online. A later ``DROPPED`` unit
    means that is not so, and the reset is refused because its rows can no longer be recounted.

    Args:
        unit_id: The catalog id of the blocked currency unit.

    Returns:
        The reset units in upper-bound order, starting with the given unit.

    Raises:
        ValueError: No unit has that id, it is not a blocked currency unit, its partition is detached, or a later
            currency unit is dropped or kept detached.
        RetentionLockBusyError: A tick holds the retention lock.
    """
    with operator_retention_lock():
        row = db.session.get(KudosRetentionUnit, unit_id)
        if row is None:
            raise ValueError(f"No kudos retention unit {unit_id}")
        if row.dataset != KudosRetentionDataset.CURRENCY or row.state != KudosRetentionState.BLOCKED:
            raise ValueError(
                f"Kudos retention unit {unit_id} is a {row.state} {row.dataset} unit, "
                "and only a blocked currency unit can be recheckpointed",
            )
        if row.detached_at is not None:
            raise ValueError(f"Kudos retention unit {unit_id} belongs to detached partition {row.partition_name}. Reattach it first")
        later_rows = db.session.scalars(
            select(KudosRetentionUnit)
            .where(KudosRetentionUnit.dataset == KudosRetentionDataset.CURRENCY, KudosRetentionUnit.unit_upper > row.unit_upper)
            .order_by(KudosRetentionUnit.unit_upper),
        ).all()
        dropped = [later_row.partition_name for later_row in later_rows if later_row.state == KudosRetentionState.DROPPED]
        if dropped:
            raise ValueError(f"Kudos retention unit {unit_id} has later dropped currency units ({', '.join(dropped)}). Refusing to reset")
        detached = [later_row.partition_name for later_row in later_rows if later_row.detached_at is not None]
        if detached:
            raise ValueError(
                f"Kudos retention unit {unit_id} has later detached currency units ({', '.join(detached)}). Reattach them first",
            )
        now = _utcnow()
        reset_rows = [row, *later_rows]
        # Only a unit with a recorded row count wrote checkpoints and daily statements. A unit blocked before its
        # checkpoint may share its boundary with another partition's checkpoint, which stays.
        checkpointed_units = [_catalog_unit(reset_row) for reset_row in reset_rows if reset_row.row_count is not None]
        if checkpointed_units:
            # Catalog creation precedes checkpoint publication. Conservatively invalidate every baseline taken
            # since the earliest affected unit was catalogued. The retention lock excludes concurrent snapshots.
            # Preserve the snapshot rows themselves as incident evidence.
            db.session.add(
                KudosSnapshotInvalidation(
                    invalid_from=min(reset_row.created for reset_row in reset_rows if reset_row.row_count is not None),
                    invalid_until=now,
                ),
            )
            stale_boundaries = [checkpointed_unit.upper for checkpointed_unit in checkpointed_units]
            stale_source_ranges = [checkpointed_unit.source_range for checkpointed_unit in checkpointed_units]
            db.session.execute(delete(KudosLedgerCheckpoint).where(KudosLedgerCheckpoint.boundary.in_(stale_boundaries)))
            db.session.execute(delete(KudosLedgerDailyStatement).where(KudosLedgerDailyStatement.source_range.in_(stale_source_ranges)))
        for reset_row in reset_rows:
            reset_row.state = KudosRetentionState.PENDING
            reset_row.error = None
            reset_row.row_count = None
            reset_row.max_id = None
            reset_row.object_key = None
            reset_row.state_before_detach = None
            reset_row.updated = now
        db.session.commit()
        logger.warning(
            f"Kudos retention reset {len(reset_rows)} currency units from {row.partition_name} to pending to recompute their checkpoints",
        )
        return [_catalog_unit(reset_row) for reset_row in reset_rows]


def waive_unit_dump(unit_id: int) -> CatalogUnit:
    """Accept dropping a statistics unit without its archive dump, moving it to ``DUMP_WAIVED``, and commit.

    Only a unit without a dump qualifies: ``DUMP_FAILED``, or ``BLOCKED`` with a reason in
    ``WAIVABLE_DUMP_BLOCK_REASONS`` (retries exhausted, the statistics budget, or a dump missing from the archive). Its
    rollup is already committed, so only the raw rows are lost. The ``error`` text is kept as the record of why. A unit
    of a detached partition is refused until the partition is reattached, so the drop starts from an attached partition.

    Args:
        unit_id: The catalog id of the unit.

    Returns:
        The unit as waived.

    Raises:
        ValueError: No unit has that id, it is not a statistics unit, it is neither failed nor blocked for a waivable
            reason, or its partition is detached.
        RetentionLockBusyError: A tick holds the retention lock.
    """
    with operator_retention_lock():
        row = db.session.get(KudosRetentionUnit, unit_id)
        if row is None:
            raise ValueError(f"No kudos retention unit {unit_id}")
        if row.dataset != KudosRetentionDataset.STATISTICS:
            raise ValueError(f"Kudos retention unit {unit_id} is a {row.dataset} unit, and only statistics dumps can be waived")
        if row.detached_at is not None:
            raise ValueError(f"Kudos retention unit {unit_id} belongs to detached partition {row.partition_name}. Reattach it first")
        waivable = row.state == KudosRetentionState.BLOCKED and row.error in WAIVABLE_DUMP_BLOCK_REASONS
        if row.state != KudosRetentionState.DUMP_FAILED and not waivable:
            raise ValueError(
                f"Kudos retention unit {unit_id} is {row.state} ({row.error}), and only {KudosRetentionState.DUMP_FAILED} units and "
                f"units blocked for {' or '.join(sorted(WAIVABLE_DUMP_BLOCK_REASONS))} can be waived",
            )
        row.state = KudosRetentionState.DUMP_WAIVED
        row.updated = _utcnow()
        db.session.commit()
        logger.warning(f"Kudos retention unit {unit_id} ({row.partition_name}) dump waived, so its raw rows are dropped without a copy")
        return _catalog_unit(row)


@dataclass(frozen=True, kw_only=True)
class StatisticsEviction:
    """Represents the outcome of ``evict_statistics_dumps``."""

    deleted_keys: list[str]
    """The deleted object keys, oldest first."""
    tier_bytes: int
    """The stored bytes of the statistics tier after the eviction."""


def evict_statistics_dumps(archive: KudosArchive, *, budget_bytes: int) -> StatisticsEviction:
    """Delete the oldest statistics dumps of dropped units until the tier fits ``budget_bytes``, and clear their keys.

    The dump of a unit that is not ``DROPPED`` is kept, because its raw rows are still online and its drop relies on
    the dump. Objects that are not recorded by any unit are deleted like dropped dumps. The keys of the evicted units are cleared in
    the catalog, so the tick and the archive audit treat any other missing dump as lost. The eviction runs under the
    retention lock, because an export measures its allowance from the tier size.

    Args:
        archive: The archive client.
        budget_bytes: The byte budget of the statistics tier.

    Returns:
        The deleted keys and the stored bytes of the tier afterwards, which stay above the budget when the kept dumps
        alone exceed it.

    Raises:
        RetentionLockBusyError: A tick holds the retention lock.
        KudosArchiveError: The listing failed, before anything was deleted.
        ArchiveEvictionError: A delete failed. The keys deleted before it are cleared in the catalog first.
    """
    with operator_retention_lock():
        kept_keys = {
            catalog_unit.object_key
            for catalog_unit in load_catalog(KudosRetentionDataset.STATISTICS)
            if catalog_unit.object_key is not None and catalog_unit.state != KudosRetentionState.DROPPED
        }
        try:
            deleted_keys = archive.enforce_budget(budget_bytes=budget_bytes, kept_keys=kept_keys)
        except ArchiveEvictionError as err:
            _clear_evicted_keys(err.deleted_keys)
            raise
        _clear_evicted_keys(deleted_keys)
        return StatisticsEviction(deleted_keys=deleted_keys, tier_bytes=archive.tier_bytes(ArchiveTier.STATISTICS))


def _clear_evicted_keys(deleted_keys: Sequence[str]) -> None:
    if not deleted_keys:
        return
    db.session.execute(
        update(KudosRetentionUnit)
        .where(KudosRetentionUnit.dataset == KudosRetentionDataset.STATISTICS, KudosRetentionUnit.object_key.in_(deleted_keys))
        .values(object_key=None, updated=_utcnow()),
    )
    db.session.commit()
    logger.warning(f"Kudos retention cleared the object keys of {len(deleted_keys)} evicted statistics dumps")


def _attached_partition_names() -> set[str]:
    return {partition.name for partition in discover_partitions()}


def _partition_bound_sql(*, lower: datetime | None, upper: datetime) -> str:
    """Return the ``FOR VALUES FROM .. TO ..`` clause of a range partition. A None lower bound is ``MINVALUE``."""
    lower_sql = _MINVALUE if lower is None else f"'{lower.isoformat(sep=' ')}'"
    return f"FOR VALUES FROM ({lower_sql}) TO ('{upper.isoformat(sep=' ')}')"


def reattach_partition(unit_id: int) -> list[CatalogUnit]:
    """Attach a partition kept detached for the drop grace period back to its parent and restore its units, and commit.

    The bounds are the partition's recorded range: the lowest unit lower bound (``MINVALUE`` for the legacy
    partition) to the highest unit upper bound. Each ``DETACHED`` unit becomes ``BLOCKED`` with ``REATTACHED`` and
    keeps ``state_before_detach``, so the partition stays attached until an operator unblocks its units, and
    ``unblock_unit`` then restores that state. A unit that the drop recount blocked after the detach keeps its own
    reason and loses ``state_before_detach``, so its unblock follows the usual rule. A partition whose units were all
    blocked by that recount has no ``DETACHED`` unit and is reattached the same way. Every unit loses ``detached_at``,
    so a later drop starts a new grace period. The attach and the catalog change commit together.

    Args:
        unit_id: The catalog id of any unit of the partition.

    Returns:
        The partition's units as blocked, in upper-bound order.

    Raises:
        ValueError: No unit has that id, the partition is not kept detached, a unit of it is in another state, or its
            table no longer exists.
        RetentionLockBusyError: A tick holds the retention lock.
    """
    with operator_retention_lock():
        row = db.session.get(KudosRetentionUnit, unit_id)
        if row is None:
            raise ValueError(f"No kudos retention unit {unit_id}")
        partition_rows = db.session.scalars(
            select(KudosRetentionUnit)
            .where(KudosRetentionUnit.dataset == row.dataset, KudosRetentionUnit.partition_name == row.partition_name)
            .order_by(KudosRetentionUnit.unit_upper),
        ).all()
        live_rows = [partition_row for partition_row in partition_rows if partition_row.state != KudosRetentionState.DROPPED]
        # A detach sets ``detached_at`` on every unit, and a unit the drop recount blocks keeps it, so a partition whose
        # units are all blocked is still found.
        detached_rows = [live_row for live_row in live_rows if live_row.detached_at is not None]
        if not detached_rows:
            raise ValueError(f"Kudos retention unit {unit_id} belongs to partition {row.partition_name}, which is not kept detached")
        for live_row in live_rows:
            blocked_after_detach = live_row.state == KudosRetentionState.BLOCKED and live_row.detached_at is not None
            restorable = live_row.state == KudosRetentionState.DETACHED and live_row.state_before_detach is not None
            if not (restorable or blocked_after_detach):
                raise ValueError(
                    f"Kudos retention unit {live_row.id} of partition {row.partition_name} is {live_row.state} "
                    f"(state before detach {live_row.state_before_detach}). Refusing to reattach",
                )
        if not _relation_exists(row.partition_name):
            raise ValueError(f"Partition {row.partition_name} no longer exists, so it cannot be reattached")
        if row.partition_name not in _attached_partition_names():
            lower_bounds = [partition_row.unit_lower for partition_row in partition_rows]
            lower = None if None in lower_bounds else min(bound for bound in lower_bounds if bound is not None)
            upper = max(partition_row.unit_upper for partition_row in partition_rows)
            parent_sql = _quote_identifier(PARTITION_PARENTS[KudosRetentionDataset(row.dataset)])
            db.session.execute(
                text(
                    f"ALTER TABLE {parent_sql} ATTACH PARTITION {_quote_identifier(row.partition_name)} "
                    f"{_partition_bound_sql(lower=lower, upper=upper)}",
                ),
            )
        now = _utcnow()
        for live_row in live_rows:
            if live_row.state == KudosRetentionState.DETACHED:
                live_row.state = KudosRetentionState.BLOCKED
                live_row.error = RetentionBlockReason.REATTACHED
            else:
                live_row.state_before_detach = None
            live_row.detached_at = None
            live_row.updated = now
        db.session.commit()
        logger.warning(
            f"Kudos retention reattached {row.dataset} partition {row.partition_name} and blocked its {len(live_rows)} units "
            "until an operator unblocks them",
        )
        return [_catalog_unit(live_row) for live_row in live_rows]


def daily_statement(*, user_id: int, first_day: date, last_day: date) -> list[DailyStatementLine]:
    """Return a user's archived daily statement lines from ``first_day`` to ``last_day`` inclusive.

    A UTC day can straddle two units, so lines are summed across ``source_range``. Days still online have no lines
    until their partition is checkpointed; read those from ``kudos_ledger``.
    """
    statement = (
        select(
            KudosLedgerDailyStatement.day,
            KudosLedgerDailyStatement.entry_type,
            KudosLedgerDailyStatement.escrow,
            func.sum(KudosLedgerDailyStatement.amount_sum),
            func.sum(KudosLedgerDailyStatement.row_count),
        )
        .where(
            KudosLedgerDailyStatement.user_id == user_id,
            KudosLedgerDailyStatement.day >= first_day,
            KudosLedgerDailyStatement.day <= last_day,
        )
        .group_by(KudosLedgerDailyStatement.day, KudosLedgerDailyStatement.entry_type, KudosLedgerDailyStatement.escrow)
        .order_by(KudosLedgerDailyStatement.day, KudosLedgerDailyStatement.entry_type, KudosLedgerDailyStatement.escrow)
    )
    lines = [
        DailyStatementLine(
            day=day, entry_type=str(entry_type), escrow=bool(escrow), amount_sum=Decimal(amount_sum), row_count=int(row_count)
        )
        for day, entry_type, escrow, amount_sum, row_count in db.session.execute(statement).all()
    ]
    db.session.commit()
    return lines


def restore_archive_unit(archive: KudosArchive, key: str, *, table_name: str, append: bool = False) -> int:
    """Copy an archive object's rows into a new scratch table created from the manifest's columns and types.

    A created table takes each column's name and type from the manifest alone, so a later change to the event tables
    does not affect the restore of an older object. A version 1 manifest does not record column types and is refused. The
    copy commits on its own connection. An existing table is refused unless ``append`` is set, because a repeated
    restore would otherwise copy the rows a second time, and each restore verifies only the rows it copied.

    Args:
        archive: The archive client.
        key: The object key.
        table_name: The scratch table, unqualified or qualified as ``schema.table``.
        append: Add the rows to an existing table instead of refusing it.

    Returns:
        The rows copied.

    Raises:
        KudosArchiveError: The manifest or object could not be read, or the manifest is version 1 and does not record
            column types.
        ValueError: ``table_name`` exists and ``append`` is not set, it has more than one dot, a part of it or a
            manifest column is not a plain identifier, or a manifest column type is not a built-in type.
    """
    table_sql = _quote_qualified_identifier(table_name)
    manifest = archive.get_manifest(key)
    if manifest.column_types is None:
        raise KudosArchiveError(
            f"Archive object {key} has a format version {ARCHIVE_MANIFEST_UNTYPED_SCHEMA_VERSION} manifest, which predates "
            "recorded column types, and restore refuses it",
        )
    column_definitions = _scratch_column_definitions(manifest.column_types)
    raw_connection = db.engine.raw_connection()
    try:
        cursor = raw_connection.cursor()
        try:
            cursor.execute("SELECT to_regclass(CAST(%s AS text)) IS NOT NULL", (table_sql,))
            table_exists = bool(cast("tuple[bool]", cursor.fetchone())[0])
            if table_exists and not append:
                raise ValueError(
                    f"Scratch table {table_name} already exists. Restore into a new table, or pass append to add these rows to it",
                )
            if not table_exists:
                cursor.execute(f"CREATE TABLE {table_sql} ({column_definitions})")

        finally:
            cursor.close()
        restored = archive.restore_unit(key, table_name=table_name, connection=raw_connection.driver_connection)
        raw_connection.commit()
    finally:
        raw_connection.close()
    return restored


@dataclass(frozen=True, kw_only=True)
class MissingArchiveObject:
    """Represents a catalog unit whose archive object is not in the archive."""

    unit_id: int
    dataset: KudosRetentionDataset
    source_range: str
    state: KudosRetentionState
    object_key: str | None
    """The key the unit records, or None for a currency unit whose key was cleared."""
    problem: str
    """Why the unit is reported."""


CURRENCY_ARCHIVED_STATES: Final[frozenset[KudosRetentionState]] = frozenset(
    {KudosRetentionState.EXPORTED, KudosRetentionState.VERIFIED, KudosRetentionState.DETACHED, KudosRetentionState.DROPPED},
)
"""The currency states reached only through a committed export. Currency objects are never evicted, so a unit in one
of them with no ``object_key`` lost its object, and the tick's reconciliation already cleared the key."""


def audit_archive(
    archive: KudosArchive,
    *,
    tiers: Collection[ArchiveTier] = tuple(ArchiveTier),
    since_key: str | None = None,
) -> Iterator[ArchiveObjectAudit | MissingArchiveObject]:
    """Yield the read-back of every object of ``tiers`` in key order, then every catalog unit missing its object.

    Each object is read in full and compared with its manifest by ``KudosArchive.audit_object``. The database is
    only read: the catalog is compared with the listing, and nothing is changed. A unit is reported when its
    ``object_key`` is not listed, and a currency unit also when it is in ``CURRENCY_ARCHIVED_STATES`` without a key.

    Args:
        archive: The archive client.
        tiers: The tiers to audit, both by default.
        since_key: Audit only objects whose key sorts after this one, to resume an interrupted audit. The catalog
            comparison always covers the whole listing.

    Raises:
        KudosArchiveError: A listing failed.
    """
    listed_keys: set[str] = set()
    for tier in ArchiveTier:
        if tier not in tiers:
            continue
        tier_keys = sorted(archive.object_sizes(tier))
        listed_keys.update(tier_keys)
        for key in tier_keys:
            if since_key is not None and key <= since_key:
                continue
            yield archive.audit_object(key, tier=tier)
    datasets = [dataset for dataset, tier in ARCHIVE_TIERS.items() if tier in tiers]
    for missing_unit in units_without_archive_objects(listed_keys, datasets=datasets):
        yield MissingArchiveObject(
            unit_id=missing_unit.id,
            dataset=missing_unit.dataset,
            source_range=missing_unit.source_range,
            state=missing_unit.state,
            object_key=missing_unit.object_key,
            problem="the archive does not hold the unit's object",
        )
    if KudosRetentionDataset.CURRENCY in datasets:
        for catalog_unit in sorted(load_catalog(KudosRetentionDataset.CURRENCY), key=_unit_sort_key):
            if catalog_unit.state in CURRENCY_ARCHIVED_STATES and catalog_unit.object_key is None:
                yield MissingArchiveObject(
                    unit_id=catalog_unit.id,
                    dataset=catalog_unit.dataset,
                    source_range=catalog_unit.source_range,
                    state=catalog_unit.state,
                    object_key=None,
                    problem="the currency unit was exported but does not record an object key",
                )
    db.session.commit()


def retention_outcome_warning(result: RetentionTickResult) -> str | None:
    """Return the warning to log for a tick that blocked a unit or a dataset or failed a statistics dump, or None otherwise.

    The lines start with ``RETENTION_BLOCKED_LOG_PREFIX`` or ``RETENTION_DUMP_FAILED_LOG_PREFIX``. A unit waived by the
    dump step because no archive is configured gets the dump-failed line, once, on that transition.
    """
    source_range = (
        ""
        if result.unit is None
        else format_source_range(
            partition_name=result.unit.partition_name,
            lower=result.unit.lower,
            upper=result.unit.upper,
        )
    )
    if result.outcome == RetentionTickOutcome.BLOCKED and result.unit is None:
        return f"{RETENTION_BLOCKED_LOG_PREFIX} {result.dataset} dataset: {result.detail}"
    if result.outcome == RetentionTickOutcome.BLOCKED:
        return f"{RETENTION_BLOCKED_LOG_PREFIX} {result.dataset} unit {source_range}: {result.detail}"
    if result.to_state in (KudosRetentionState.DUMP_FAILED, KudosRetentionState.DUMP_WAIVED):
        return f"{RETENTION_DUMP_FAILED_LOG_PREFIX} for {source_range}: {result.detail}"
    return None


def dump_attempt_failed(result: RetentionTickResult) -> bool:
    """Return whether the tick's step was a statistics dump that failed, including the one that exhausted the retries.

    A unit waived because no archive is configured made no attempt, so it is not a failure here.
    """
    if result.dataset is None or result.from_state is None:
        return False
    if STEP_TABLE[result.dataset].get(result.from_state) != RetentionStep.DUMP:
        return False
    return result.to_state in (KudosRetentionState.DUMP_FAILED, KudosRetentionState.BLOCKED)


@dataclass(frozen=True, kw_only=True)
class AccountingRetentionDisclosure:
    """Represents the accounting retention facts the privacy policy states, derived from the retention policy."""

    currency_window_days: int
    """How many days currency events stay online after their partition closes."""
    statistics_window_days: int
    """How many days statistic events stay online before they are reduced to daily aggregates."""
    currency_archived: bool
    """Whether currency events are archived, without a fixed end, when they leave the database."""

    @classmethod
    def from_policy(
        cls,
        policy: KudosRetentionPolicy | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> AccountingRetentionDisclosure:
        """Create the disclosure for ``policy``, archiving when every ``KUDOS_ARCHIVE_*`` variable is set.

        Args:
            policy: The policy to disclose; ``KUDOS_RETENTION_POLICY``, read at the call, when None.
            environ: Variables to read; the process environment by default.
        """
        disclosed = KUDOS_RETENTION_POLICY if policy is None else policy
        return cls(
            currency_window_days=disclosed.currency_window.days,
            statistics_window_days=disclosed.statistics_window.days,
            currency_archived=all(environ.get(variable) for variable in ARCHIVE_REQUIRED_ENV),
        )


def retention_health(*, now: datetime | None = None, policy: KudosRetentionPolicy = KUDOS_RETENTION_POLICY) -> KudosRetentionHealth:
    """Return the retention job's progress: backlog age, unit counts, last step age, coverage, held, detached and restored partitions.

    Args:
        now: The reference time (naive UTC); the current time when None.
        policy: The retention policy whose windows define eligibility.
    """
    if now is None:
        now = _utcnow()
    partitions = [] if SQLITE_MODE else discover_partitions()
    catalog = load_catalog()
    oldest_eligible_age: dict[KudosRetentionDataset, float | None] = {}
    for dataset in KudosRetentionDataset:
        cutoff = now - policy.window_for(dataset)
        eligible_uppers = _eligible_uppers(dataset, partitions, catalog, cutoff)
        oldest_eligible_age[dataset] = (cutoff - min(eligible_uppers)).total_seconds() if eligible_uppers else None
    blocked_by_error = db.session.execute(
        select(KudosRetentionUnit.error, func.count(KudosRetentionUnit.id))
        .where(KudosRetentionUnit.state == KudosRetentionState.BLOCKED)
        .group_by(KudosRetentionUnit.error),
    ).all()
    blocked_units = sum(int(count) for _, count in blocked_by_error)
    blocked_units_by_reason = dict.fromkeys(RetentionBlockReason, 0)
    for error, count in blocked_by_error:
        if error in blocked_units_by_reason:
            blocked_units_by_reason[RetentionBlockReason(error)] = int(count)
    pending_by_dataset = db.session.execute(
        select(KudosRetentionUnit.dataset, func.count(KudosRetentionUnit.id))
        .where(KudosRetentionUnit.state.not_in([KudosRetentionState.BLOCKED, KudosRetentionState.DROPPED]))
        .group_by(KudosRetentionUnit.dataset),
    ).all()
    pending_units_by_dataset = dict.fromkeys(KudosRetentionDataset, 0)
    for pending_dataset, count in pending_by_dataset:
        pending_units_by_dataset[KudosRetentionDataset(pending_dataset)] = int(count)
    last_update = db.session.execute(select(func.max(KudosRetentionUnit.updated))).scalar_one()
    db.session.commit()
    return KudosRetentionHealth(
        oldest_eligible_age_seconds=oldest_eligible_age,
        blocked_units=blocked_units,
        blocked_units_by_reason=blocked_units_by_reason,
        last_step_age_seconds=None if last_update is None else (now - last_update).total_seconds(),
        pending_units=sum(pending_units_by_dataset.values()),
        pending_units_by_dataset=pending_units_by_dataset,
        # Coverage is about inserts arriving now, so it uses the real clock whatever reference time the caller passes.
        partition_coverage_ok=True if SQLITE_MODE else partition_coverage_ok(partitions, moment=_utcnow(), width=policy.partition_width),
        hold_drops=policy.hold_drops,
        held_partitions=drop_ready_partition_count(catalog) if policy.hold_drops else 0,
        drop_grace_days=policy.drop_grace / timedelta(days=1),
        detached_partitions=detached_partitions(catalog, drop_grace=policy.drop_grace),
        restored_partitions=restored_partitions(partitions, catalog),
    )


def partition_coverage_ok(partitions: Sequence[PartitionBounds], *, moment: datetime, width: timedelta) -> bool:
    """Return whether each event table has a partition containing ``moment`` and one containing ``moment + width``.

    Args:
        partitions: Every attached partition of both datasets.
        moment: The time inserts arrive at, naive UTC.
        width: The partition width.
    """
    for dataset in KudosRetentionDataset:
        dataset_partitions = [partition for partition in partitions if partition.dataset == dataset]
        for covered_moment in (moment, moment + width):
            if not any(_partition_contains(partition, covered_moment) for partition in dataset_partitions):
                return False
    return True


def _partition_contains(partition: PartitionBounds, moment: datetime) -> bool:
    above_lower = partition.lower is None or partition.lower <= moment
    below_upper = partition.upper is None or moment < partition.upper
    return above_lower and below_upper


__all__ = [
    "StatisticsEviction",
    "evict_statistics_dumps",
    "ARCHIVE_BUDGET_ENV",
    "ARCHIVE_STATISTICS_BUDGET_ENV",
    "ARCHIVE_TIERS",
    "WAIVABLE_DUMP_BLOCK_REASONS",
    "CURRENCY_ARCHIVED_STATES",
    "MissingArchiveObject",
    "audit_archive",
    "units_without_archive_objects",
    "RETENTION_BLOCKED_LOG_PREFIX",
    "RETENTION_DUMP_FAILED_LOG_PREFIX",
    "AccountingRetentionDisclosure",
    "retention_outcome_warning",
    "ARCHIVE_NOT_CONFIGURED",
    "CHECKPOINT_RECORDED_STATES",
    "CURRENCY_WINDOW_ENV",
    "DROP_DEFER_LOCK_AGE_ENV",
    "DROP_GRACE_ENV",
    "DROP_READY_STATES",
    "DUMP_RETRY_SPACING",
    "DUMP_RETRY_WINDOW_ENV",
    "FUTURE_REFERENCE_REFUSAL",
    "RESTORED_PARTITION_REFUSAL",
    "RestoredPartitionHealth",
    "restored_partitions",
    "HOLD_DROPS_ENV",
    "KUDOS_RETENTION_POLICY",
    "PARTITION_WIDTH_ENV",
    "RETENTION_ENABLED_ENV",
    "STATISTICS_WINDOW_ENV",
    "STEP_TABLE",
    "STEP_TARGETS",
    "CatalogUnit",
    "CheckpointChainPosition",
    "DailyStatementLine",
    "DetachedPartitionHealth",
    "KudosRetentionHealth",
    "KudosRetentionPolicy",
    "NextUnit",
    "PartitionBounds",
    "RetentionBlockReason",
    "RetentionLockBusyError",
    "RetentionStep",
    "RetentionTickOutcome",
    "RetentionTickResult",
    "RetentionUnitRef",
    "RetentionVerificationError",
    "UnitRowCount",
    "checkpoint_chain_continues",
    "checkpoint_chain_position",
    "daily_statement",
    "detached_partitions",
    "discover_partitions",
    "drop_ready_partition_count",
    "dump_attempt_failed",
    "expire_idempotency_keys",
    "format_source_range",
    "legacy_unit_ranges",
    "load_catalog",
    "load_retention_policy",
    "parse_partition_bound",
    "parse_rehearsal_as_of",
    "recheckpoint_from_unit",
    "partition_coverage_ok",
    "reattach_partition",
    "restore_archive_unit",
    "retention_health",
    "run_retention_tick",
    "select_next_unit",
    "unblock_unit",
    "waive_unit_dump",
]
