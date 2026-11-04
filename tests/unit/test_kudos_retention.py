# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remove kudos event partitions past the online window only after their replacements are committed (ADR 42).

Policy parsing, source ranges, legacy sub-ranges, unit selection and the step table need no database. The database
tests create partitions with explicit bounds in 2020, below every partition the test bootstrap creates, and drive
``run_retention_tick`` with a ``now`` past those bounds plus the 30-day window.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text, update

from horde.database.kudos_retention import (
    ARCHIVE_BUDGET_ENV,
    ARCHIVE_NOT_CONFIGURED,
    ARCHIVE_STATISTICS_BUDGET_ENV,
    CURRENCY_WINDOW_ENV,
    DROP_DEFER_LOCK_AGE_ENV,
    DROP_GRACE_ENV,
    DROP_LOCK_TIMEOUT,
    DUMP_RETRY_WINDOW_ENV,
    FUTURE_REFERENCE_REFUSAL,
    HOLD_DROPS_ENV,
    PARTITION_WIDTH_ENV,
    RETENTION_ENABLED_ENV,
    STATISTICS_WINDOW_ENV,
    STEP_TABLE,
    STEP_TARGETS,
    CatalogUnit,
    KudosRetentionPolicy,
    PartitionBounds,
    RetentionBlockReason,
    RetentionStep,
    RetentionTickOutcome,
    RetentionTickResult,
    RetentionUnitRef,
    checkpoint_chain_continues,
    checkpoint_chain_position,
    dump_attempt_failed,
    format_source_range,
    legacy_unit_ranges,
    load_retention_policy,
    parse_partition_bound,
    partition_coverage_ok,
    select_next_unit,
)
from horde.enums import KudosEntryType, KudosRetentionDataset, KudosRetentionState, KudosUnit
from horde.kudos_archive import ARCHIVE_REQUIRED_ENV, ArchiveTier

if TYPE_CHECKING:
    from flask_sqlalchemy.session import Session
    from sqlalchemy.orm import scoped_session

    from tests.fixture_types import MakeUser

pytestmark = pytest.mark.unit

CURRENCY = KudosRetentionDataset.CURRENCY
STATISTICS = KudosRetentionDataset.STATISTICS
WEEK = timedelta(days=7)
MONDAY = datetime(2020, 1, 6)
"""The lower bound of the first test partition, a UTC Monday far below the bootstrap's partitions."""


def _policy(**overrides: Any) -> KudosRetentionPolicy:
    # Most tests drive a drop to DROPPED in one tick, so the policy drops right after the detach unless a test passes
    # a grace of its own.
    fields: dict[str, Any] = {
        "enabled": True,
        "currency_window": timedelta(days=30),
        "statistics_window": timedelta(days=30),
        "partition_width": WEEK,
        "currency_budget_bytes": None,
        "drop_grace": timedelta(0),
    }
    fields.update(overrides)
    return KudosRetentionPolicy(**fields)


# Policy


def test_policy_defaults() -> None:
    policy = load_retention_policy({})
    assert policy == KudosRetentionPolicy(
        enabled=False,
        currency_window=timedelta(days=30),
        statistics_window=timedelta(days=30),
        partition_width=WEEK,
        currency_budget_bytes=None,
    )


def test_policy_reads_every_variable() -> None:
    policy = load_retention_policy(
        {
            RETENTION_ENABLED_ENV: " TRUE ",
            CURRENCY_WINDOW_ENV: "90",
            STATISTICS_WINDOW_ENV: "45",
            PARTITION_WIDTH_ENV: "14",
            ARCHIVE_BUDGET_ENV: "1000",
            ARCHIVE_STATISTICS_BUDGET_ENV: "400",
        },
    )
    assert policy.enabled
    assert policy.currency_window == timedelta(days=90)
    assert policy.statistics_window == timedelta(days=45)
    assert policy.partition_width == timedelta(days=14)
    assert (policy.currency_budget_bytes, policy.statistics_budget_bytes) == (1000, 400)
    assert (policy.budget_for(ArchiveTier.CURRENCY), policy.budget_for(ArchiveTier.STATISTICS)) == (1000, 400)
    assert load_retention_policy({}).statistics_budget_bytes is None
    assert policy.key_lifetime == timedelta(days=45)
    assert policy.window_for(CURRENCY) == timedelta(days=90)
    assert policy.window_for(STATISTICS) == timedelta(days=45)


def test_policy_reads_the_dump_retry_and_drop_defer_variables() -> None:
    defaults = load_retention_policy({})
    assert (defaults.dump_retry_window, defaults.drop_defer_lock_age) == (timedelta(days=7), timedelta(minutes=5))
    policy = load_retention_policy({DUMP_RETRY_WINDOW_ENV: "3", DROP_DEFER_LOCK_AGE_ENV: "12"})
    assert (policy.dump_retry_window, policy.drop_defer_lock_age) == (timedelta(days=3), timedelta(minutes=12))


def test_policy_reads_the_hold_and_grace_variables() -> None:
    defaults = load_retention_policy({})
    assert (defaults.hold_drops, defaults.drop_grace) == (False, timedelta(days=7))
    policy = load_retention_policy({HOLD_DROPS_ENV: " Yes ", DROP_GRACE_ENV: "7"})
    assert (policy.hold_drops, policy.drop_grace) == (True, timedelta(days=7))
    assert load_retention_policy({DROP_GRACE_ENV: "0"}).drop_grace == timedelta(0)
    assert load_retention_policy({DROP_GRACE_ENV: "10"}).drop_grace == timedelta(days=10)
    with pytest.raises(ValueError, match=HOLD_DROPS_ENV):
        load_retention_policy({HOLD_DROPS_ENV: "sometimes"})
    with pytest.raises(ValueError, match=DROP_GRACE_ENV):
        load_retention_policy({DROP_GRACE_ENV: "-1"})


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({CURRENCY_WINDOW_ENV: "29"}, CURRENCY_WINDOW_ENV),
        ({STATISTICS_WINDOW_ENV: "7"}, STATISTICS_WINDOW_ENV),
        ({CURRENCY_WINDOW_ENV: "thirty"}, CURRENCY_WINDOW_ENV),
        ({STATISTICS_WINDOW_ENV: "99999999999"}, STATISTICS_WINDOW_ENV),
        ({PARTITION_WIDTH_ENV: "0"}, PARTITION_WIDTH_ENV),
        ({PARTITION_WIDTH_ENV: "32"}, PARTITION_WIDTH_ENV),
        ({RETENTION_ENABLED_ENV: "maybe"}, RETENTION_ENABLED_ENV),
        ({ARCHIVE_BUDGET_ENV: "lots"}, ARCHIVE_BUDGET_ENV),
        ({ARCHIVE_BUDGET_ENV: "0"}, ARCHIVE_BUDGET_ENV),
        ({ARCHIVE_STATISTICS_BUDGET_ENV: "0"}, ARCHIVE_STATISTICS_BUDGET_ENV),
        ({ARCHIVE_STATISTICS_BUDGET_ENV: "plenty"}, ARCHIVE_STATISTICS_BUDGET_ENV),
        (dict.fromkeys(ARCHIVE_REQUIRED_ENV, "x"), ARCHIVE_BUDGET_ENV),
        ({DUMP_RETRY_WINDOW_ENV: "0"}, DUMP_RETRY_WINDOW_ENV),
        ({DUMP_RETRY_WINDOW_ENV: "a week"}, DUMP_RETRY_WINDOW_ENV),
        ({DROP_DEFER_LOCK_AGE_ENV: "-1"}, DROP_DEFER_LOCK_AGE_ENV),
    ],
)
def test_malformed_or_short_policy_stops_load(environ: dict[str, str], variable: str) -> None:
    with pytest.raises(ValueError, match=variable):
        load_retention_policy(environ)


def test_configured_archive_with_budget_loads() -> None:
    environ = dict.fromkeys(ARCHIVE_REQUIRED_ENV, "x") | {ARCHIVE_BUDGET_ENV: "5000"}
    assert load_retention_policy(environ).currency_budget_bytes == 5000


# Pure helpers


def test_source_range_formatting() -> None:
    assert format_source_range(partition_name="kudos_ledger_p_legacy", lower=None, upper=datetime(2026, 8, 10, 12, 30, 5)) == (
        "kudos_ledger_p_legacy/min_20260810T123005"
    )
    assert format_source_range(partition_name="kudos_ledger_p_20260803", lower=datetime(2026, 8, 3), upper=datetime(2026, 8, 10)) == (
        "kudos_ledger_p_20260803/20260803T000000_20260810T000000"
    )


def test_partition_bound_parsing() -> None:
    assert parse_partition_bound("FOR VALUES FROM ('2020-01-06 00:00:00') TO ('2020-01-13 00:00:00')") == (
        datetime(2020, 1, 6),
        datetime(2020, 1, 13),
    )
    assert parse_partition_bound("FOR VALUES FROM (MINVALUE) TO ('2026-11-02 10:15:30.123456')") == (
        None,
        datetime(2026, 11, 2, 10, 15, 30, 123456),
    )
    assert parse_partition_bound("FOR VALUES FROM ('2020-01-06 00:00:00') TO (MAXVALUE)") == (datetime(2020, 1, 6), None)
    assert parse_partition_bound("DEFAULT") is None
    with pytest.raises(ValueError):
        parse_partition_bound("FOR VALUES IN (1)")


def test_legacy_sub_ranges_follow_the_partition_width_grid() -> None:
    swap = datetime(2020, 1, 29, 14, 0)
    assert legacy_unit_ranges(earliest_created=datetime(2020, 1, 8, 9), partition_upper=swap, width=WEEK) == [
        (None, datetime(2020, 1, 13)),
        (datetime(2020, 1, 13), datetime(2020, 1, 20)),
        (datetime(2020, 1, 20), datetime(2020, 1, 27)),
        (datetime(2020, 1, 27), swap),
    ]
    # A row exactly on a grid point starts the next sub-range.
    assert legacy_unit_ranges(earliest_created=datetime(2020, 1, 27), partition_upper=swap, width=WEEK) == [(None, swap)]
    assert legacy_unit_ranges(earliest_created=datetime(2020, 1, 26, 23), partition_upper=swap, width=WEEK) == [
        (None, datetime(2020, 1, 27)),
        (datetime(2020, 1, 27), swap),
    ]
    assert legacy_unit_ranges(earliest_created=datetime(2020, 1, 28), partition_upper=swap, width=WEEK) == [(None, swap)]
    assert legacy_unit_ranges(earliest_created=None, partition_upper=swap, width=WEEK) == [(None, swap)]


def _partition(dataset: KudosRetentionDataset, name: str, lower: datetime | None, upper: datetime | None) -> PartitionBounds:
    return PartitionBounds(dataset=dataset, name=name, lower=lower, upper=upper)


def _unit(
    unit_id: int,
    dataset: KudosRetentionDataset,
    name: str,
    lower: datetime | None,
    upper: datetime,
    state: KudosRetentionState,
) -> CatalogUnit:
    return CatalogUnit(id=unit_id, dataset=dataset, partition_name=name, lower=lower, upper=upper, state=state)


CUTOFF = MONDAY + 4 * WEEK


def test_selection_catalogues_the_oldest_eligible_partition_first() -> None:
    older = _partition(STATISTICS, "s_old", MONDAY, MONDAY + WEEK)
    newer = _partition(STATISTICS, "s_new", MONDAY + WEEK, MONDAY + 2 * WEEK)
    not_eligible = _partition(STATISTICS, "s_future", CUTOFF, CUTOFF + WEEK)
    selection = select_next_unit(
        dataset=STATISTICS, partitions=[newer, not_eligible, older], catalog=[], cutoff=CUTOFF, archive_configured=False
    )
    assert selection.partition_needing_units == older
    assert select_next_unit(dataset=STATISTICS, partitions=[not_eligible], catalog=[], cutoff=CUTOFF, archive_configured=False).unit is None
    assert (
        select_next_unit(dataset=CURRENCY, partitions=[older], catalog=[], cutoff=CUTOFF, archive_configured=False).partition_needing_units
        is None
    )


def test_selection_skips_dropped_and_blocked_statistics_but_stops_at_blocked_currency() -> None:
    first = _partition(STATISTICS, "s_1", MONDAY, MONDAY + WEEK)
    second = _partition(STATISTICS, "s_2", MONDAY + WEEK, MONDAY + 2 * WEEK)
    third = _partition(STATISTICS, "s_3", MONDAY + 2 * WEEK, MONDAY + 3 * WEEK)
    catalog = [
        _unit(1, STATISTICS, "s_1", MONDAY, MONDAY + WEEK, KudosRetentionState.BLOCKED),
        _unit(2, STATISTICS, "s_2", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.ROLLED_UP),
    ]
    selection = select_next_unit(
        dataset=STATISTICS, partitions=[first, second, third], catalog=catalog, cutoff=CUTOFF, archive_configured=False
    )
    assert selection.unit == catalog[1]

    currency_first = _partition(CURRENCY, "c_1", MONDAY, MONDAY + WEEK)
    currency_second = _partition(CURRENCY, "c_2", MONDAY + WEEK, MONDAY + 2 * WEEK)
    blocked = [_unit(3, CURRENCY, "c_1", MONDAY, MONDAY + WEEK, KudosRetentionState.BLOCKED)]
    selection = select_next_unit(
        dataset=CURRENCY, partitions=[currency_first, currency_second], catalog=blocked, cutoff=CUTOFF, archive_configured=True
    )
    assert selection.unit is None
    assert selection.partition_needing_units is None


def test_selection_lets_checkpointed_currency_wait_for_an_archive() -> None:
    first = _partition(CURRENCY, "c_1", MONDAY, MONDAY + WEEK)
    second = _partition(CURRENCY, "c_2", MONDAY + WEEK, MONDAY + 2 * WEEK)
    catalog = [_unit(1, CURRENCY, "c_1", MONDAY, MONDAY + WEEK, KudosRetentionState.CHECKPOINTED)]
    without_archive = select_next_unit(
        dataset=CURRENCY, partitions=[first, second], catalog=catalog, cutoff=CUTOFF, archive_configured=False
    )
    assert without_archive.partition_needing_units == second
    with_archive = select_next_unit(dataset=CURRENCY, partitions=[first, second], catalog=catalog, cutoff=CUTOFF, archive_configured=True)
    assert with_archive.unit == catalog[0]


def test_selection_drops_the_legacy_partition_only_when_every_sub_range_is_ready() -> None:
    legacy = _partition(STATISTICS, "kudos_stat_events_p_legacy", None, MONDAY + 2 * WEEK)
    dumped = _unit(1, STATISTICS, legacy.name, None, MONDAY + WEEK, KudosRetentionState.DUMPED)
    pending = _unit(2, STATISTICS, legacy.name, MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.PENDING)
    assert (
        select_next_unit(dataset=STATISTICS, partitions=[legacy], catalog=[dumped, pending], cutoff=CUTOFF, archive_configured=False).unit
        == pending
    )
    # A failed dump is retried, so it holds back the drop of its partition.
    failed = _unit(2, STATISTICS, legacy.name, MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.DUMP_FAILED)
    assert (
        select_next_unit(dataset=STATISTICS, partitions=[legacy], catalog=[dumped, failed], cutoff=CUTOFF, archive_configured=False).unit
        == failed
    )
    waived = _unit(2, STATISTICS, legacy.name, MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.DUMP_WAIVED)
    assert (
        select_next_unit(dataset=STATISTICS, partitions=[legacy], catalog=[dumped, waived], cutoff=CUTOFF, archive_configured=False).unit
        == dumped
    )


def test_selection_finishes_an_interrupted_drop_of_a_detached_partition() -> None:
    detached = _unit(1, STATISTICS, "s_detached", MONDAY, MONDAY + WEEK, KudosRetentionState.DUMPED)
    forgotten = _unit(2, STATISTICS, "s_gone", MONDAY, MONDAY + WEEK, KudosRetentionState.PENDING)
    selection = select_next_unit(dataset=STATISTICS, partitions=[], catalog=[forgotten, detached], cutoff=CUTOFF, archive_configured=False)
    assert selection.unit == detached


def test_selection_holds_a_detached_partition_whose_sibling_the_recount_blocked() -> None:
    legacy = "kudos_ledger_p_legacy"
    detached = _unit(1, CURRENCY, legacy, None, MONDAY + WEEK, KudosRetentionState.DETACHED)
    blocked = _unit(2, CURRENCY, legacy, MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.BLOCKED)
    later = _partition(CURRENCY, "c_later", MONDAY + 2 * WEEK, MONDAY + 3 * WEEK)
    # The blocked unit of the detached partition stops the currency dataset, and its sibling is not drop-ready.
    selection = select_next_unit(dataset=CURRENCY, partitions=[later], catalog=[detached, blocked], cutoff=CUTOFF, archive_configured=True)
    assert (selection.unit, selection.partition_needing_units) == (None, None)
    statistics_detached = _unit(3, STATISTICS, "s_detached", None, MONDAY + WEEK, KudosRetentionState.DETACHED)
    statistics_blocked = _unit(4, STATISTICS, "s_detached", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.BLOCKED)
    statistics_selection = select_next_unit(
        dataset=STATISTICS, partitions=[], catalog=[statistics_detached, statistics_blocked], cutoff=CUTOFF, archive_configured=False
    )
    assert statistics_selection.unit is None
    all_detached = _unit(4, STATISTICS, "s_detached", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.DETACHED)
    detached_selection = select_next_unit(
        dataset=STATISTICS, partitions=[], catalog=[statistics_detached, all_detached], cutoff=CUTOFF, archive_configured=False
    )
    assert detached_selection.unit == statistics_detached


def test_selection_skips_a_failed_dump_until_it_is_due_and_lets_later_statistics_proceed() -> None:
    from dataclasses import replace

    first = _partition(STATISTICS, "s_1", MONDAY, MONDAY + WEEK)
    second = _partition(STATISTICS, "s_2", MONDAY + WEEK, MONDAY + 2 * WEEK)
    attempted = MONDAY + 3 * WEEK
    failed = replace(_unit(1, STATISTICS, "s_1", MONDAY, MONDAY + WEEK, KudosRetentionState.DUMP_FAILED), last_dump_attempt_at=attempted)
    later = _unit(2, STATISTICS, "s_2", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.ROLLED_UP)

    def selected(due_before: datetime) -> CatalogUnit | None:
        return select_next_unit(
            dataset=STATISTICS,
            partitions=[first, second],
            catalog=[failed, later],
            cutoff=CUTOFF,
            archive_configured=True,
            dump_retry_due_before=due_before,
        ).unit

    assert selected(attempted - timedelta(minutes=1)) == later
    assert selected(attempted) == failed


def test_step_table_transitions() -> None:
    assert STEP_TABLE[STATISTICS] == {
        KudosRetentionState.PENDING: RetentionStep.ROLLUP,
        KudosRetentionState.ROLLED_UP: RetentionStep.DUMP,
        KudosRetentionState.DUMPED: RetentionStep.DROP,
        KudosRetentionState.DUMP_FAILED: RetentionStep.DUMP,
        KudosRetentionState.DUMP_WAIVED: RetentionStep.DROP,
        KudosRetentionState.DETACHED: RetentionStep.DROP,
    }
    assert STEP_TABLE[CURRENCY] == {
        KudosRetentionState.PENDING: RetentionStep.CHECKPOINT,
        KudosRetentionState.CHECKPOINTED: RetentionStep.EXPORT,
        KudosRetentionState.EXPORTED: RetentionStep.VERIFY,
        KudosRetentionState.VERIFIED: RetentionStep.DROP,
        KudosRetentionState.DETACHED: RetentionStep.DROP,
    }
    for dataset_steps in STEP_TABLE.values():
        assert KudosRetentionState.BLOCKED not in dataset_steps
        assert KudosRetentionState.DROPPED not in dataset_steps
    # Only the dump step waives a dump, and only when no archive is configured. The operator command is the other path.
    assert [step for step, targets in STEP_TARGETS.items() if KudosRetentionState.DUMP_WAIVED in targets] == [RetentionStep.DUMP]
    assert STEP_TARGETS[RetentionStep.DUMP] == {
        KudosRetentionState.DUMPED,
        KudosRetentionState.DUMP_FAILED,
        KudosRetentionState.DUMP_WAIVED,
        KudosRetentionState.BLOCKED,
    }
    assert STEP_TARGETS[RetentionStep.DROP] == {KudosRetentionState.DETACHED, KudosRetentionState.DROPPED, KudosRetentionState.BLOCKED}


def test_blocked_reasons_match_the_operator_contract() -> None:
    assert [str(reason) for reason in RetentionBlockReason] == [
        "unapplied rows",
        "checkpoint chain gap",
        "currency tier would exceed budget",
        "statistics tier would exceed budget",
        "archive mismatch",
        "online drift since export",
        "boundary already checkpointed",
        "late rows",
        "dump retries exhausted",
        "archive object missing",
        "reattached by operator",
    ]
    assert ARCHIVE_NOT_CONFIGURED == "archive not configured"


def test_checkpoint_chain() -> None:
    lower = MONDAY
    assert checkpoint_chain_continues(unit_lower=lower, previous_boundary=lower, later_boundary_exists=False)
    assert not checkpoint_chain_continues(unit_lower=lower, previous_boundary=lower - WEEK, later_boundary_exists=False)
    assert not checkpoint_chain_continues(unit_lower=lower, previous_boundary=lower, later_boundary_exists=True)
    assert checkpoint_chain_continues(unit_lower=None, previous_boundary=None, later_boundary_exists=False)
    assert not checkpoint_chain_continues(unit_lower=None, previous_boundary=lower, later_boundary_exists=False)
    assert not checkpoint_chain_continues(unit_lower=lower, previous_boundary=None, later_boundary_exists=False)


def test_checkpoint_chain_position_reads_boundaries_from_the_catalog() -> None:
    week = _unit(5, CURRENCY, "kudos_ledger_p_20200106", MONDAY, MONDAY + WEEK, KudosRetentionState.PENDING)
    # An empty legacy unit records its boundary in the catalog even though it wrote no per-user rows.
    empty_legacy = _unit(4, CURRENCY, "kudos_ledger_p_legacy", None, MONDAY, KudosRetentionState.DROPPED)
    position = checkpoint_chain_position(unit=week, catalog=[empty_legacy, week])
    assert (position.previous_boundary, position.later_boundary, position.same_boundary_partition) == (MONDAY, None, None)

    # Pending, blocked and statistics units record no currency boundary.
    unrecorded = [
        _unit(4, CURRENCY, "kudos_ledger_p_legacy", None, MONDAY, KudosRetentionState.BLOCKED),
        _unit(6, STATISTICS, "kudos_stat_events_p_legacy", None, MONDAY, KudosRetentionState.DUMPED),
        _unit(7, CURRENCY, "kudos_ledger_p_20200113", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.PENDING),
    ]
    position = checkpoint_chain_position(unit=week, catalog=[*unrecorded, week])
    assert (position.previous_boundary, position.later_boundary) == (None, None)

    later = _unit(7, CURRENCY, "kudos_ledger_p_20200113", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.CHECKPOINTED)
    assert checkpoint_chain_position(unit=week, catalog=[empty_legacy, later]).later_boundary == MONDAY + 2 * WEEK

    hand_made = _unit(8, CURRENCY, "kudos_ledger_p_other", None, MONDAY + WEEK, KudosRetentionState.DROPPED)
    assert checkpoint_chain_position(unit=week, catalog=[hand_made]).same_boundary_partition == "kudos_ledger_p_other"

    first = _unit(9, CURRENCY, "kudos_ledger_p_legacy", None, MONDAY, KudosRetentionState.PENDING)
    assert checkpoint_chain_position(unit=first, catalog=[first]).previous_boundary is None


def test_partition_coverage_needs_now_and_one_width_ahead() -> None:
    now = MONDAY + timedelta(days=3)
    covered = [
        _partition(dataset, f"{dataset}_{index}", MONDAY + index * WEEK, MONDAY + (index + 1) * WEEK)
        for dataset in KudosRetentionDataset
        for index in (0, 1)
    ]
    assert partition_coverage_ok(covered, moment=now, width=WEEK)
    assert not partition_coverage_ok(covered[:3], moment=now, width=WEEK)
    assert not partition_coverage_ok(covered, moment=now + WEEK, width=WEEK)
    unbounded = [_partition(dataset, f"{dataset}_all", None, None) for dataset in KudosRetentionDataset]
    assert partition_coverage_ok(unbounded, moment=now, width=WEEK)


def test_future_reference_time_is_refused_without_confirmation() -> None:
    from horde.database.kudos_retention import run_retention_tick

    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=40)
    result = run_retention_tick(now=future, policy=_policy())
    assert (result.outcome, result.detail) == (RetentionTickOutcome.REFUSED, FUTURE_REFERENCE_REFUSAL)


def test_dump_attempt_failures_include_the_exhausting_block() -> None:
    unit = RetentionUnitRef(partition_name="kudos_stat_events_p_20200106", lower=MONDAY, upper=MONDAY + WEEK)

    def result(outcome: RetentionTickOutcome, from_state: KudosRetentionState, to_state: KudosRetentionState) -> RetentionTickResult:
        return RetentionTickResult(outcome=outcome, dataset=STATISTICS, unit=unit, from_state=from_state, to_state=to_state)

    assert dump_attempt_failed(result(RetentionTickOutcome.STEPPED, KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMP_FAILED))
    assert dump_attempt_failed(result(RetentionTickOutcome.BLOCKED, KudosRetentionState.DUMP_FAILED, KudosRetentionState.BLOCKED))
    assert not dump_attempt_failed(result(RetentionTickOutcome.STEPPED, KudosRetentionState.DUMP_FAILED, KudosRetentionState.DUMPED))
    assert not dump_attempt_failed(result(RetentionTickOutcome.BLOCKED, KudosRetentionState.PENDING, KudosRetentionState.BLOCKED))
    assert not dump_attempt_failed(RetentionTickResult(outcome=RetentionTickOutcome.IDLE))


def test_restore_rejects_a_table_name_with_more_than_one_dot() -> None:
    from horde.database.kudos_retention import restore_archive_unit

    class _UnreadArchive:
        def get_manifest(self, key: str) -> None:
            raise AssertionError("the name is checked before the archive is read")

    with pytest.raises(ValueError, match="schema.table"):
        restore_archive_unit(_UnreadArchive(), "key", table_name="a.b.c")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unexpected identifier"):
        restore_archive_unit(_UnreadArchive(), "key", table_name="public.bad-name")  # type: ignore[arg-type]


# Database


@pytest.fixture
def make_partition(db_session: scoped_session[Session]) -> Iterator[Callable[[str, datetime | None, datetime], str]]:
    """Create partitions with explicit bounds and drop whatever is left of them afterwards.

    The bootstrap's legacy partition covers everything below the current week, so it is detached and renamed for the
    test and attached again afterwards.
    """
    held: dict[str, str] = {}
    for parent in ("kudos_ledger", "kudos_stat_events"):
        bound = db_session.execute(
            text("SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE oid = to_regclass(CAST(:name AS text))"),
            {"name": f"{parent}_p_legacy"},
        ).scalar_one_or_none()
        if bound is None:
            continue
        db_session.execute(text(f"ALTER TABLE {parent} DETACH PARTITION {parent}_p_legacy"))
        db_session.execute(text(f"ALTER TABLE {parent}_p_legacy RENAME TO {parent}_p_held"))
        held[parent] = str(bound)
    db_session.commit()
    created: list[str] = []

    def _make(parent: str, lower: datetime | None, upper: datetime) -> str:
        name = f"{parent}_p_legacy" if lower is None else f"{parent}_p_{lower:%Y%m%d}"
        lower_sql = "MINVALUE" if lower is None else f"'{lower.isoformat(sep=' ')}'"
        db_session.execute(
            text(f"CREATE TABLE {name} PARTITION OF {parent} FOR VALUES FROM ({lower_sql}) TO ('{upper.isoformat(sep=' ')}')"),
        )
        db_session.commit()
        created.append(name)
        return name

    yield _make
    db_session.rollback()
    for name in created:
        db_session.execute(text(f"DROP TABLE IF EXISTS {name}"))
    for parent, bound in held.items():
        db_session.execute(text(f"ALTER TABLE {parent}_p_held RENAME TO {parent}_p_legacy"))
        db_session.execute(text(f"ALTER TABLE {parent} ATTACH PARTITION {parent}_p_legacy {bound}"))
    db_session.commit()


SEEDED_PARTITION = "kudos_ledger_p_seeded"
"""The partition name of the dropped catalog unit that records a seeded checkpoint boundary."""


def _seed_checkpoint(db_session: scoped_session[Session], boundary: datetime, totals: dict[int, tuple[str, str]]) -> None:
    """Seed per-user checkpoint rows at ``boundary`` and the dropped catalog unit that records the boundary.

    The checkpoint chain is read from the catalog, so the rows alone, as after a hand edit, do not continue it.
    """
    from horde.classes.base.kudos import KudosLedgerCheckpoint

    for user_id, (balance, escrow) in totals.items():
        db_session.add(
            KudosLedgerCheckpoint(boundary=boundary, user_id=user_id, balance_total=Decimal(balance), escrow_total=Decimal(escrow))
        )
    _seed_catalog_unit(db_session, CURRENCY, SEEDED_PARTITION, None, boundary, KudosRetentionState.DROPPED)


def _seed_catalog_unit(
    db_session: scoped_session[Session],
    dataset: KudosRetentionDataset,
    partition_name: str,
    lower: datetime | None,
    upper: datetime,
    state: KudosRetentionState,
) -> None:
    from horde.classes.base.kudos import KudosRetentionUnit

    now = datetime.now(UTC).replace(tzinfo=None)
    db_session.add(
        KudosRetentionUnit(
            dataset=dataset,
            partition_name=partition_name,
            unit_lower=lower,
            unit_upper=upper,
            state=state,
            row_count=0,
            created=now,
            updated=now,
        ),
    )
    db_session.commit()


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def iter_chunks(self, chunk_size: int = 1024) -> Iterator[bytes]:
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]


class _FakePaginator:
    def __init__(self, store: _FakeObjectStore, operation_name: str) -> None:
        self._store = store
        self._operation_name = operation_name

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803 - boto3 keyword names
        if self._operation_name == "list_objects_v2":
            contents = [
                {"Key": key, "Size": len(payload)} for key, payload in sorted(self._store.objects.items()) if key.startswith(Prefix)
            ]
            yield {"Contents": contents}
            return
        if self._operation_name == "list_object_versions":
            yield self._store.list_versions(Prefix)
            return
        uploads = [
            {"Key": key, "UploadId": upload_id, "Initiated": initiated}
            for upload_id, (key, initiated, _) in self._store.uploads.items()
            if key.startswith(Prefix)
        ]
        yield {"Uploads": uploads}


class _FakeObjectStore:
    """In-memory stand-in for the S3 client calls ``KudosArchive`` makes, so archive steps run without a store."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, tuple[str, datetime, dict[int, bytes]]] = {}
        self.fail_uploads = False
        # Unversioned by default, refusing version listings the way a store without versioning support does.
        self.versioned = False
        self.noncurrent_versions: list[tuple[str, int]] = []
        self.delete_markers: list[str] = []

    def list_versions(self, prefix: str) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        if not self.versioned:
            raise ClientError({"Error": {"Code": "NotImplemented", "Message": "simulated"}}, "ListObjectVersions")
        current = [{"Key": key, "Size": len(payload), "IsLatest": True} for key, payload in self.objects.items() if key.startswith(prefix)]
        noncurrent = [{"Key": key, "Size": size, "IsLatest": False} for key, size in self.noncurrent_versions if key.startswith(prefix)]
        markers = [{"Key": key, "IsLatest": True} for key in self.delete_markers if key.startswith(prefix)]
        return {"Versions": current + noncurrent, "DeleteMarkers": markers}

    def _refuse_when_failing(self, operation_name: str) -> None:
        from botocore.exceptions import ClientError

        if self.fail_uploads:
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "simulated outage"}}, operation_name)

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._refuse_when_failing("CreateMultipartUpload")
        upload_id = uuid.uuid4().hex
        self.uploads[upload_id] = (str(kwargs["Key"]), datetime.now(UTC), {})
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self.uploads[str(kwargs["UploadId"])][2][int(kwargs["PartNumber"])] = bytes(kwargs["Body"])
        return {"ETag": f'"{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        key, _, parts = self.uploads.pop(str(kwargs["UploadId"]))
        self.objects[key] = b"".join(parts[number] for number in sorted(parts))
        return {}

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.uploads.pop(str(kwargs["UploadId"]), None)
        return {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._refuse_when_failing("PutObject")
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])
        return {}

    def _missing(self, key: str, operation_name: str) -> Exception:
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": "NoSuchKey", "Message": key}}, operation_name)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise self._missing(key, "GetObject")
        return {"Body": _FakeBody(self.objects[key])}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise self._missing(key, "HeadObject")
        return {"ContentLength": len(self.objects[key])}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        for entry in kwargs["Delete"]["Objects"]:
            key = str(entry["Key"])
            if self.versioned and key in self.objects:
                self.noncurrent_versions.append((key, len(self.objects[key])))
                self.delete_markers.append(key)
            self.objects.pop(key, None)
        return {}

    def get_paginator(self, operation_name: str) -> _FakePaginator:
        return _FakePaginator(self, operation_name)


def _fake_archive(store: _FakeObjectStore) -> Any:
    from horde.kudos_archive import KudosArchive

    return KudosArchive(client=store, bucket="kudos-archive-fake")


def _run_until(now: datetime, archive: Any, stop: Callable[[Any], bool], *, max_ticks: int = 20, **policy_overrides: Any) -> list[Any]:
    """Run ticks until ``stop`` accepts a result. Returns every result."""
    from horde.database.kudos_retention import run_retention_tick

    results = []
    for _ in range(max_ticks):
        results.append(run_retention_tick(now=now, policy=_policy(**policy_overrides), archive=archive))
        if stop(results[-1]):
            return results
    raise AssertionError(f"no stop after {max_ticks} ticks: {[(result.outcome, result.to_state, result.detail) for result in results]}")


def _tick(now: datetime, **policy_overrides: Any) -> Any:
    from horde.database.kudos_retention import run_retention_tick

    return run_retention_tick(now=now, policy=_policy(**policy_overrides))


def _catalog(db_session: scoped_session[Session]) -> list[Any]:
    from horde.classes.base.kudos import KudosRetentionUnit

    db_session.expire_all()
    statement = (
        select(KudosRetentionUnit)
        .where(KudosRetentionUnit.partition_name != SEEDED_PARTITION)
        .order_by(KudosRetentionUnit.unit_upper, KudosRetentionUnit.dataset)
    )
    return list(db_session.scalars(statement).all())


def _attached(db_session: scoped_session[Session], name: str) -> bool:
    statement = text("SELECT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = to_regclass(CAST(:name AS text)))")
    return bool(db_session.execute(statement, {"name": name}).scalar_one())


def _stat_event(created: datetime, amount: str, **overrides: Any) -> Any:
    from horde.classes.base.kudos import KudosStatEvent

    fields: dict[str, Any] = {
        "created": created,
        "event_id": uuid.uuid4(),
        "entry_type": KudosEntryType.STAT_RECORD,
        "user_id": 1,
        "amount": Decimal(amount),
        "unit": KudosUnit.COUNT,
        "stat_action": "image",
        "record": "request",
        "applied": True,
    }
    fields.update(overrides)
    return KudosStatEvent(**fields)


def _posting(created: datetime, user_id: int, amount: str, **overrides: Any) -> Any:
    from horde.classes.base.kudos import KudosLedger

    fields: dict[str, Any] = {
        "created": created,
        "event_id": uuid.uuid4(),
        "entry_type": KudosEntryType.GENERATION,
        "user_id": user_id,
        "escrow": False,
        "amount": Decimal(amount),
        "applied": True,
    }
    fields.update(overrides)
    return KudosLedger(**fields)


AFTER_WINDOW = MONDAY + 10 * WEEK
"""A ``now`` at which every 2020 test partition is past the 30-day window."""


def test_expire_idempotency_keys_deletes_the_oldest_batch(db_session: scoped_session[Session]) -> None:
    from horde.classes.base.kudos import KudosIdempotencyKey
    from horde.database.kudos_retention import expire_idempotency_keys
    from horde.enums import KudosIdempotencyKind

    now = datetime(2026, 11, 2)
    ages = {"oldest": 40, "old": 31, "fresh": 5}
    keys = {label: uuid.uuid4() for label in ages}
    for label, days in ages.items():
        db_session.add(
            KudosIdempotencyKey(
                event_id=keys[label],
                kind=KudosIdempotencyKind.TRANSFER,
                source_user_id=1,
                dest_user_id=2,
                amount=Decimal(5),
                outcome="OK",
                created=now - timedelta(days=days),
            ),
        )
    db_session.commit()
    assert expire_idempotency_keys(before=now - timedelta(days=30), batch_size=1) == 1
    remaining = set(db_session.scalars(select(KudosIdempotencyKey.event_id)).all())
    assert remaining == {keys["old"], keys["fresh"]}
    assert expire_idempotency_keys(before=now - timedelta(days=30), batch_size=5000) == 1
    assert set(db_session.scalars(select(KudosIdempotencyKey.event_id)).all()) == {keys["fresh"]}


def test_disabled_retention_only_expires_keys(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    result = _tick(AFTER_WINDOW, enabled=False)
    assert result.outcome == RetentionTickOutcome.IDLE
    assert _catalog(db_session) == []


def test_statistics_rollup_quarantine_and_drop(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    from horde.classes.base.kudos import KudosRetentionUnit, KudosStatQuarantine, KudosStatRollup

    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    worker_id = uuid.uuid4()
    day_one = MONDAY + timedelta(hours=3)
    day_two = MONDAY + timedelta(days=1, hours=5)
    quarantined_id = 9_000_001
    db_session.add_all(
        [
            _stat_event(day_one, "3", record="max_pixels", stat_action="image"),
            _stat_event(day_one + timedelta(hours=2), "11", record="max_pixels", stat_action="image"),
            _stat_event(day_two, "4", record="max_pixels", stat_action="image"),
            _stat_event(
                day_one,
                "7.50",
                entry_type=KudosEntryType.STAT_CONTRIBUTION,
                user_id=None,
                worker_id=worker_id,
                unit=KudosUnit.THINGS,
                stat_action="contributions",
                record=None,
            ),
            _stat_event(day_one, "99", id=quarantined_id, applied=False, quarantined=True),
        ],
    )
    db_session.commit()

    result = _tick(AFTER_WINDOW)
    assert (result.outcome, result.from_state, result.to_state) == (
        RetentionTickOutcome.STEPPED,
        KudosRetentionState.PENDING,
        KudosRetentionState.ROLLED_UP,
    )
    source_range = format_source_range(partition_name=partition, lower=MONDAY, upper=MONDAY + WEEK)
    rollups = {
        (row.day, row.record, row.worker_id): row
        for row in db_session.scalars(select(KudosStatRollup).where(KudosStatRollup.source_range == source_range)).all()
    }
    assert set(rollups) == {
        (date(2020, 1, 6), "max_pixels", None),
        (date(2020, 1, 7), "max_pixels", None),
        (date(2020, 1, 6), None, worker_id),
    }
    first_day = rollups[(date(2020, 1, 6), "max_pixels", None)]
    assert (first_day.amount_sum, first_day.row_count, first_day.amount_min, first_day.amount_max) == (
        Decimal(14),
        2,
        Decimal(3),
        Decimal(11),
    )
    assert first_day.created_max == day_one + timedelta(hours=2)
    assert rollups[(date(2020, 1, 6), None, worker_id)].amount_sum == Decimal("7.50")
    quarantined = db_session.scalars(select(KudosStatQuarantine)).all()
    assert [(row.id, row.amount, row.source_range) for row in quarantined] == [(quarantined_id, Decimal(99), source_range)]

    # A retried rollup replaces its own rows instead of adding to them.
    db_session.execute(update(KudosRetentionUnit).values(state=KudosRetentionState.PENDING))
    db_session.commit()
    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.ROLLED_UP
    db_session.expire_all()
    assert len(db_session.scalars(select(KudosStatRollup)).all()) == 3
    assert len(db_session.scalars(select(KudosStatQuarantine)).all()) == 1

    from horde.database.kudos_retention import retention_outcome_warning

    # Without an archive no dump is possible, so the unit is waived at once and logs the dump-failed line once.
    dump = _tick(AFTER_WINDOW)
    assert (dump.to_state, dump.detail) == (KudosRetentionState.DUMP_WAIVED, ARCHIVE_NOT_CONFIGURED)
    assert not dump_attempt_failed(dump)
    assert (retention_outcome_warning(dump) or "").startswith("Kudos retention dump failed for ")
    assert _catalog(db_session)[0].error == ARCHIVE_NOT_CONFIGURED
    drop = _tick(AFTER_WINDOW)
    assert retention_outcome_warning(drop) is None
    assert drop.to_state == KudosRetentionState.DROPPED
    assert not _attached(db_session, partition)
    assert db_session.execute(text("SELECT to_regclass(CAST(:name AS text))"), {"name": partition}).scalar_one() is None
    assert len(db_session.scalars(select(KudosStatRollup)).all()) == 3
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.IDLE


def test_empty_weekly_partition_is_dropped(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    states = [_tick(AFTER_WINDOW).to_state for _ in range(2)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMP_WAIVED]
    assert (_catalog(db_session)[0].row_count, _catalog(db_session)[0].max_id) == (0, None)
    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.DROPPED
    assert not _attached(db_session, partition)


def test_unapplied_row_blocks(db_session: scoped_session[Session], make_partition: Callable[..., str], make_user: MakeUser) -> None:
    make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    ledger_partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    db_session.add_all(
        [_stat_event(MONDAY + timedelta(hours=1), "1", applied=False), _posting(MONDAY + timedelta(hours=1), user.id, "5", applied=False)]
    )
    db_session.commit()

    results = [_tick(AFTER_WINDOW) for _ in range(2)]
    assert {result.dataset for result in results} == {CURRENCY, STATISTICS}
    assert all(result.outcome == RetentionTickOutcome.BLOCKED for result in results)
    assert [(unit.state, unit.error) for unit in _catalog(db_session)] == [(KudosRetentionState.BLOCKED, "unapplied rows")] * 2
    assert _attached(db_session, ledger_partition)
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.IDLE


def test_checkpoint_chain_carry_forward_and_daily_statements(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.classes.base.kudos import KudosLedgerCheckpoint, KudosLedgerDailyStatement
    from horde.database.kudos_retention import retention_health

    first = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    second = make_partition("kudos_ledger", MONDAY + WEEK, MONDAY + 2 * WEEK)
    only_first, both, only_second, checkpoint_only = make_user(), make_user(), make_user(), make_user()
    # The chain starts from a checkpoint at the first partition's lower bound, as after a dropped legacy partition.
    _seed_checkpoint(db_session, MONDAY, {both.id: ("1", "0"), checkpoint_only.id: ("20", "1")})
    day = MONDAY + timedelta(hours=6)
    db_session.add_all(
        [
            _posting(day, only_first.id, "10"),
            _posting(day, only_first.id, "4", escrow=True),
            _posting(day, both.id, "-3"),
            _posting(day, both.id, "100", entry_type=KudosEntryType.RECONCILIATION),
            _posting(day + timedelta(days=1), both.id, "2"),
            _posting(day + WEEK, both.id, "5"),
            _posting(day + WEEK, only_second.id, "8", escrow=True),
        ],
    )
    db_session.commit()

    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.CHECKPOINTED
    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.CHECKPOINTED
    # With no archive both units wait in CHECKPOINTED.
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.IDLE

    def checkpoint(boundary: datetime) -> dict[int, tuple[Decimal, Decimal]]:
        rows = db_session.scalars(select(KudosLedgerCheckpoint).where(KudosLedgerCheckpoint.boundary == boundary)).all()
        return {row.user_id: (row.balance_total, row.escrow_total) for row in rows}

    assert checkpoint(MONDAY + WEEK) == {
        only_first.id: (Decimal(10), Decimal(4)),
        both.id: (Decimal(0), Decimal(0)),
        checkpoint_only.id: (Decimal(20), Decimal(1)),
    }
    assert checkpoint(MONDAY + 2 * WEEK) == {
        only_first.id: (Decimal(10), Decimal(4)),
        both.id: (Decimal(5), Decimal(0)),
        only_second.id: (Decimal(0), Decimal(8)),
        checkpoint_only.id: (Decimal(20), Decimal(1)),
    }

    first_range = format_source_range(partition_name=first, lower=MONDAY, upper=MONDAY + WEEK)
    statements = {
        (row.user_id, row.escrow, row.entry_type, row.day): (row.amount_sum, row.row_count)
        for row in db_session.scalars(select(KudosLedgerDailyStatement).where(KudosLedgerDailyStatement.source_range == first_range)).all()
    }
    assert statements == {
        (only_first.id, False, KudosEntryType.GENERATION, date(2020, 1, 6)): (Decimal(10), 1),
        (only_first.id, True, KudosEntryType.GENERATION, date(2020, 1, 6)): (Decimal(4), 1),
        (both.id, False, KudosEntryType.GENERATION, date(2020, 1, 6)): (Decimal(-3), 1),
        (both.id, False, KudosEntryType.RECONCILIATION, date(2020, 1, 6)): (Decimal(100), 1),
        (both.id, False, KudosEntryType.GENERATION, date(2020, 1, 7)): (Decimal(2), 1),
    }
    second_range = format_source_range(partition_name=second, lower=MONDAY + WEEK, upper=MONDAY + 2 * WEEK)
    assert db_session.scalars(select(KudosLedgerDailyStatement.source_range).distinct()).all() in (
        [first_range, second_range],
        [second_range, first_range],
    )

    health = retention_health(now=AFTER_WINDOW, policy=_policy())
    assert health["pending_units"] == 2
    assert health["blocked_units"] == 0
    assert health["oldest_eligible_age_seconds"][CURRENCY] == (AFTER_WINDOW - timedelta(days=30) - (MONDAY + WEEK)).total_seconds()


def test_checkpoint_chain_gap_blocks(db_session: scoped_session[Session], make_partition: Callable[..., str], make_user: MakeUser) -> None:
    from horde.classes.base.kudos import KudosLedgerCheckpoint

    make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY - timedelta(days=2), {user.id: ("1", "0")})
    # Per-user rows at the unit's lower bound without a catalog unit ending there do not continue the chain.
    db_session.add(KudosLedgerCheckpoint(boundary=MONDAY, user_id=user.id, balance_total=Decimal(1), escrow_total=Decimal(0)))
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()
    result = _tick(AFTER_WINDOW)
    assert (result.outcome, result.to_state) == (RetentionTickOutcome.BLOCKED, KudosRetentionState.BLOCKED)
    assert [unit.error for unit in _catalog(db_session)] == ["checkpoint chain gap"]


def test_legacy_currency_partition_is_checkpointed_in_sub_ranges(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.classes.base.kudos import KudosLedgerCheckpoint, KudosLedgerDailyStatement

    swap = MONDAY + timedelta(days=9, hours=6)
    legacy = make_partition("kudos_ledger", None, swap)
    user = make_user()
    # The second day's rows straddle the grid boundary at MONDAY, so that UTC day has statements in two units.
    db_session.add_all(
        [
            _posting(MONDAY - timedelta(days=3), user.id, "1"),
            _posting(MONDAY - timedelta(hours=1), user.id, "2"),
            _posting(MONDAY + timedelta(hours=1), user.id, "4"),
            _posting(MONDAY + timedelta(days=8), user.id, "8"),
        ],
    )
    db_session.commit()

    states = [_tick(AFTER_WINDOW).to_state for _ in range(3)]
    assert states == [KudosRetentionState.CHECKPOINTED] * 3
    units = _catalog(db_session)
    assert [(unit.unit_lower, unit.unit_upper) for unit in units] == [(None, MONDAY), (MONDAY, MONDAY + WEEK), (MONDAY + WEEK, swap)]
    assert {unit.partition_name for unit in units} == {legacy}
    balances = [
        row.balance_total
        for row in db_session.scalars(
            select(KudosLedgerCheckpoint).where(KudosLedgerCheckpoint.user_id == user.id).order_by(KudosLedgerCheckpoint.boundary)
        ).all()
    ]
    assert balances == [Decimal(3), Decimal(7), Decimal(15)]
    statement_rows = db_session.scalars(select(KudosLedgerDailyStatement).where(KudosLedgerDailyStatement.user_id == user.id)).all()
    assert sum(row.amount_sum for row in statement_rows) == Decimal(15)
    assert len({row.source_range for row in statement_rows}) == 3
    # Nothing is dropped while currency waits for an archive.
    assert _attached(db_session, legacy)


@pytest.mark.object_storage
def test_currency_export_verify_and_drop(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
    object_store_ready: None,
) -> None:
    import os

    from horde.database.kudos_retention import run_retention_tick
    from horde.kudos_archive import ArchiveTier, KudosArchive

    archive = KudosArchive.from_environment(os.environ)
    assert archive is not None
    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add_all(
        [_posting(MONDAY + timedelta(hours=1), user.id, "5"), _posting(MONDAY + timedelta(hours=2), user.id, "2.5", escrow=True)]
    )
    db_session.commit()

    over_budget = run_retention_tick(now=AFTER_WINDOW, policy=_policy(currency_budget_bytes=1), archive=archive)
    assert over_budget.to_state == KudosRetentionState.CHECKPOINTED
    blocked = run_retention_tick(now=AFTER_WINDOW, policy=_policy(currency_budget_bytes=1), archive=archive)
    assert (blocked.outcome, _catalog(db_session)[0].error) == (RetentionTickOutcome.BLOCKED, "currency tier would exceed budget")

    from horde.classes.base.kudos import KudosRetentionUnit

    db_session.execute(
        update(KudosRetentionUnit)
        .where(KudosRetentionUnit.partition_name == partition)
        .values(state=KudosRetentionState.CHECKPOINTED, error=None),
    )
    db_session.commit()
    budget = archive.tier_bytes(ArchiveTier.CURRENCY) + 10_000_000
    exported = run_retention_tick(now=AFTER_WINDOW, policy=_policy(currency_budget_bytes=budget), archive=archive)
    assert exported.to_state == KudosRetentionState.EXPORTED
    key = _catalog(db_session)[0].object_key
    assert key is not None
    assert archive.get_manifest(key).per_user_totals == {str(user.id): ["5.00", "2.50"]}

    verified = run_retention_tick(now=AFTER_WINDOW, policy=_policy(currency_budget_bytes=budget), archive=archive)
    assert verified.to_state == KudosRetentionState.VERIFIED
    from horde.database.kudos_retention import restore_archive_unit

    scratch = f"kudos_restore_{uuid.uuid4().hex[:8]}"
    try:
        assert restore_archive_unit(archive, key, table_name=scratch) == 2
        restored = db_session.execute(text(f"SELECT user_id, escrow, amount FROM {scratch} ORDER BY amount")).all()
        assert [tuple(row) for row in restored] == [(user.id, True, Decimal("2.50")), (user.id, False, Decimal("5.00"))]
        db_session.commit()
        # A repeated restore into the same table would hold every row twice, so it needs append.
        with pytest.raises(ValueError, match="already exists"):
            restore_archive_unit(archive, key, table_name=scratch)
        assert restore_archive_unit(archive, key, table_name=scratch, append=True) == 2
        assert db_session.execute(text(f"SELECT count(*) FROM {scratch}")).scalar_one() == 4
    finally:
        db_session.rollback()
        db_session.execute(text(f"DROP TABLE IF EXISTS {scratch}"))
        db_session.commit()
    dropped = run_retention_tick(now=AFTER_WINDOW, policy=_policy(currency_budget_bytes=budget), archive=archive)
    assert dropped.to_state == KudosRetentionState.DROPPED
    assert not _attached(db_session, partition)


@pytest.mark.object_storage
def test_online_drift_since_export_blocks(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
    object_store_ready: None,
) -> None:
    import os

    from horde.database.kudos_retention import run_retention_tick
    from horde.kudos_archive import KudosArchive

    archive = KudosArchive.from_environment(os.environ)
    assert archive is not None
    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()
    for _ in range(2):
        run_retention_tick(now=AFTER_WINDOW, policy=_policy(), archive=archive)
    assert _catalog(db_session)[0].state == KudosRetentionState.EXPORTED
    db_session.add(_posting(MONDAY + timedelta(hours=3), user.id, "1"))
    db_session.commit()
    result = run_retention_tick(now=AFTER_WINDOW, policy=_policy(), archive=archive)
    assert result.outcome == RetentionTickOutcome.BLOCKED
    assert _catalog(db_session)[0].error == "online drift since export"
    assert _attached(db_session, partition)


def test_retention_lock_is_session_level_and_released_after_the_tick(db_session: scoped_session[Session]) -> None:
    from horde.database.kudos_db import release_retention_lock, try_acquire_retention_lock
    from horde.database.kudos_reconciliation import create_balance_snapshot
    from horde.database.kudos_retention import RetentionLockBusyError
    from horde.flask import db

    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as holder:
        assert try_acquire_retention_lock(connection=holder)
        assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.SKIPPED_LOCK
        with pytest.raises(RetentionLockBusyError):
            create_balance_snapshot()
        assert release_retention_lock(connection=holder)
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.IDLE
    create_balance_snapshot()
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as next_holder:
        # The tick released its lock, so another session takes it at once.
        assert try_acquire_retention_lock(connection=next_holder)
        assert release_retention_lock(connection=next_holder)


# Operator surface


def test_log_contract_prefixes_start_the_outcome_warnings() -> None:
    from horde.database.kudos_retention import (
        RETENTION_BLOCKED_LOG_PREFIX,
        RETENTION_DUMP_FAILED_LOG_PREFIX,
        RetentionTickResult,
        RetentionUnitRef,
        retention_outcome_warning,
    )

    # LOG CONTRACT guard: log-based alerting keys on these exact prefixes.
    assert RETENTION_BLOCKED_LOG_PREFIX == "Kudos retention blocked"
    assert RETENTION_DUMP_FAILED_LOG_PREFIX == "Kudos retention dump failed"
    unit = RetentionUnitRef(partition_name="kudos_ledger_p_20200106", lower=MONDAY, upper=MONDAY + WEEK)
    blocked = RetentionTickResult(
        outcome=RetentionTickOutcome.BLOCKED,
        dataset=CURRENCY,
        unit=unit,
        from_state=KudosRetentionState.PENDING,
        to_state=KudosRetentionState.BLOCKED,
        detail="unapplied rows",
    )
    warning = retention_outcome_warning(blocked)
    assert warning is not None
    assert warning.startswith(RETENTION_BLOCKED_LOG_PREFIX)
    assert "kudos_ledger_p_20200106/20200106T000000_20200113T000000: unapplied rows" in warning
    dump_failed = RetentionTickResult(
        outcome=RetentionTickOutcome.STEPPED,
        dataset=STATISTICS,
        unit=unit,
        from_state=KudosRetentionState.ROLLED_UP,
        to_state=KudosRetentionState.DUMP_FAILED,
        detail=ARCHIVE_NOT_CONFIGURED,
    )
    assert (retention_outcome_warning(dump_failed) or "").startswith(RETENTION_DUMP_FAILED_LOG_PREFIX)
    stepped = RetentionTickResult(outcome=RetentionTickOutcome.STEPPED, dataset=STATISTICS, unit=unit, to_state=KudosRetentionState.DUMPED)
    assert retention_outcome_warning(stepped) is None


def test_accounting_disclosure_follows_the_policy_and_archive_configuration() -> None:
    from horde.database.kudos_retention import AccountingRetentionDisclosure

    policy = _policy(currency_window=timedelta(days=60), statistics_window=timedelta(days=45))
    configured = dict.fromkeys(ARCHIVE_REQUIRED_ENV, "x")
    assert AccountingRetentionDisclosure.from_policy(policy, configured) == AccountingRetentionDisclosure(
        currency_window_days=60,
        statistics_window_days=45,
        currency_archived=True,
    )
    assert not AccountingRetentionDisclosure.from_policy(policy, {}).currency_archived


@pytest.mark.parametrize("document_format", ["html", "markdown"])
def test_privacy_document_discloses_accounting_retention(client: Any, monkeypatch: pytest.MonkeyPatch, document_format: str) -> None:
    from horde.database import kudos_retention

    monkeypatch.setattr(
        kudos_retention, "KUDOS_RETENTION_POLICY", _policy(currency_window=timedelta(days=40), statistics_window=timedelta(days=35))
    )
    for variable in ARCHIVE_REQUIRED_ENV:
        monkeypatch.setenv(variable, "x")
    document = client.get("/api/v2/documents/privacy", query_string={"format": document_format}).get_json()[document_format]
    assert "Kudos movements are kept in Our database for 40 days, then moved to Our archive" in document
    assert "Generation statistics are kept for 35 days, then reduced to daily totals." in document
    assert "by numeric identifier only" in document
    monkeypatch.delenv(ARCHIVE_REQUIRED_ENV[0])
    document = client.get("/api/v2/documents/privacy", query_string={"format": document_format}).get_json()[document_format]
    assert "Kudos movements are kept in Our database without a fixed end." in document


def test_unblock_returns_a_blocked_unit_to_pending(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    from horde.database.kudos_retention import unblock_unit

    make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "1", applied=False))
    db_session.commit()
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.BLOCKED
    unit = _catalog(db_session)[0]
    with pytest.raises(ValueError, match="No kudos retention unit"):
        unblock_unit(unit.id + 1000)
    reset = unblock_unit(unit.id)
    assert (reset.state, reset.error) == (KudosRetentionState.PENDING, None)
    with pytest.raises(ValueError, match="not blocked"):
        unblock_unit(unit.id)


def test_step_can_be_limited_to_one_dataset(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    from horde.database.kudos_retention import run_retention_tick

    make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    result = run_retention_tick(now=AFTER_WINDOW, policy=_policy(), dataset=CURRENCY)
    assert result.outcome == RetentionTickOutcome.IDLE
    assert _catalog(db_session) == []
    assert run_retention_tick(now=AFTER_WINDOW, policy=_policy(), dataset=STATISTICS).dataset == STATISTICS


def test_daily_statement_sums_a_day_across_source_ranges(db_session: scoped_session[Session], make_user: MakeUser) -> None:
    from horde.classes.base.kudos import KudosLedgerDailyStatement
    from horde.database.kudos_retention import DailyStatementLine, daily_statement

    user = make_user()
    for source_range, amount in (
        ("kudos_ledger_p_legacy/20200101T000000_20200106T120000", "3"),
        ("kudos_ledger_p_20200106/20200106T120000_20200113T000000", "4"),
    ):
        db_session.add(
            KudosLedgerDailyStatement(
                user_id=user.id,
                escrow=False,
                entry_type=KudosEntryType.GENERATION,
                day=date(2020, 1, 6),
                amount_sum=Decimal(amount),
                row_count=2,
                source_range=source_range,
            ),
        )
    db_session.add(
        KudosLedgerDailyStatement(
            user_id=user.id,
            escrow=True,
            entry_type=KudosEntryType.GENERATION,
            day=date(2020, 1, 9),
            amount_sum=Decimal(1),
            row_count=1,
            source_range="kudos_ledger_p_20200106/20200106T120000_20200113T000000",
        ),
    )
    db_session.commit()
    assert daily_statement(user_id=user.id, first_day=date(2020, 1, 6), last_day=date(2020, 1, 8)) == [
        DailyStatementLine(day=date(2020, 1, 6), entry_type="GENERATION", escrow=False, amount_sum=Decimal(7), row_count=4),
    ]
    assert len(daily_statement(user_id=user.id, first_day=date(2020, 1, 1), last_day=date(2020, 1, 31))) == 2


class _MetricRecorder:
    """Stand-in for an OpenTelemetry counter or gauge that keeps every recording."""

    def __init__(self) -> None:
        self.recordings: list[tuple[float, dict[str, str]]] = []

    def add(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self.recordings.append((amount, dict(attributes or {})))

    def set(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        self.recordings.append((amount, dict(attributes or {})))


def test_scheduled_tick_records_the_step_and_the_gauges(db_session: scoped_session[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    from horde import metrics
    from horde.database import threads

    recorders = {
        name: _MetricRecorder()
        for name in (
            "kudos_retention_steps",
            "kudos_retention_oldest_eligible_age",
            "kudos_retention_blocked_units",
            "kudos_retention_partition_coverage",
            "kudos_retention_held_partitions",
            "kudos_retention_dump_failures",
            "kudos_archive_tier_bytes",
            "kudos_archive_budget_bytes",
        )
    }
    for name, recorder in recorders.items():
        monkeypatch.setattr(metrics, name, recorder)
    monkeypatch.setattr(threads, "_kudos_archive_loaded", True)
    monkeypatch.setattr(threads, "_kudos_archive", None)

    threads.kudos_retention_tick()

    steps = recorders["kudos_retention_steps"].recordings
    assert (1, {"horde.kudos.dataset": "none", "horde.kudos.retention.step": "none", "horde.kudos.retention.outcome": "idle"}) in steps
    zero_outcomes = {
        (attributes["horde.kudos.dataset"], attributes["horde.kudos.retention.outcome"]) for amount, attributes in steps if amount == 0
    }
    assert zero_outcomes == {(str(dataset), outcome) for dataset in KudosRetentionDataset for outcome in ("blocked", "failed")}
    assert {attributes["horde.kudos.dataset"] for _, attributes in recorders["kudos_retention_oldest_eligible_age"].recordings} == {
        str(dataset) for dataset in KudosRetentionDataset
    }
    assert recorders["kudos_retention_blocked_units"].recordings == [
        (0, {"horde.kudos.block_reason": str(reason)}) for reason in RetentionBlockReason
    ]
    # The bootstrap creates partitions from the current week onward, so coverage holds.
    assert recorders["kudos_retention_partition_coverage"].recordings == [(1, {})]
    assert recorders["kudos_retention_held_partitions"].recordings == [(0, {})]
    assert recorders["kudos_retention_dump_failures"].recordings == [
        (0, {"horde.kudos.dataset": str(dataset)}) for dataset in KudosRetentionDataset
    ]
    assert recorders["kudos_archive_tier_bytes"].recordings == []
    assert recorders["kudos_archive_budget_bytes"].recordings == []


def test_scheduled_tick_records_a_budget_per_configured_tier_and_the_listed_tier_bytes(
    db_session: scoped_session[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from horde import metrics
    from horde.database import kudos_retention, threads

    recorders = {name: _MetricRecorder() for name in ("kudos_archive_tier_bytes", "kudos_archive_budget_bytes")}
    for name, recorder in recorders.items():
        monkeypatch.setattr(metrics, name, recorder)
    store = _FakeObjectStore()
    store.objects["statistics/kudos_stat_events_p_old__20200106T000000_20200113T000000.csv.zst"] = b"x" * 7
    monkeypatch.setattr(threads, "_kudos_archive_loaded", True)
    monkeypatch.setattr(threads, "_kudos_archive", _fake_archive(store))
    monkeypatch.setattr(kudos_retention, "KUDOS_RETENTION_POLICY", _policy(currency_budget_bytes=1000))

    threads.kudos_retention_tick()

    # Only the currency tier has a budget, so the statistics tier has no budget series.
    assert recorders["kudos_archive_budget_bytes"].recordings == [(1000, {"horde.kudos.archive.tier": "currency"})]
    assert recorders["kudos_archive_tier_bytes"].recordings == [
        (0, {"horde.kudos.archive.tier": "currency"}),
        (7, {"horde.kudos.archive.tier": "statistics"}),
    ]


# Rehearsal time override


def test_rehearsal_as_of_is_refused_unless_enabled_and_not_in_the_past() -> None:
    from horde.database.kudos_retention import parse_rehearsal_as_of

    real_now = datetime(2026, 11, 3, 12, 0)
    with pytest.raises(ValueError, match=RETENTION_ENABLED_ENV):
        parse_rehearsal_as_of("2027-01-01T00:00:00Z", policy=_policy(enabled=False), real_now=real_now)
    with pytest.raises(ValueError, match="before the current time"):
        parse_rehearsal_as_of("2026-11-03T11:59:59", policy=_policy(), real_now=real_now)
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_rehearsal_as_of("next tuesday", policy=_policy(), real_now=real_now)
    assert parse_rehearsal_as_of("2026-12-01T02:00:00+02:00", policy=_policy(), real_now=real_now) == datetime(2026, 12, 1)
    assert parse_rehearsal_as_of("2026-11-03T12:00:00", policy=_policy(), real_now=real_now) == real_now
    # The longer window is 45 days and the width 7, so the latest accepted time is 45 + 28 days ahead.
    wide = _policy(statistics_window=timedelta(days=45))
    latest = real_now + timedelta(days=45 + 4 * 7)
    assert parse_rehearsal_as_of(latest.isoformat(), policy=wide, real_now=real_now) == latest
    with pytest.raises(ValueError, match="one partition width past the window"):
        parse_rehearsal_as_of((latest + timedelta(seconds=1)).isoformat(), policy=wide, real_now=real_now)
    with pytest.raises(ValueError, match="latest rehearsal time"):
        parse_rehearsal_as_of("2099-01-01T00:00:00Z", policy=_policy(), real_now=real_now)
    # A drop grace extends the latest time by the grace, so a rehearsal can reach the drop of a detached partition.
    graced = _policy(statistics_window=timedelta(days=45), drop_grace=timedelta(days=10))
    assert parse_rehearsal_as_of((latest + timedelta(days=10)).isoformat(), policy=graced, real_now=real_now) == latest + timedelta(days=10)


@pytest.fixture
def current_week_partition(db_session: scoped_session[Session]) -> Iterator[datetime]:
    """Detach every bootstrap statistics partition and attach one for the current UTC week in their place.

    A future ``as_of`` makes every partition below it eligible and creates partitions past it, so the test owns the
    whole layout of ``kudos_stat_events`` and restores the bootstrap's afterwards.
    """
    parent = "kudos_stat_events"
    attached_statement = text(
        "SELECT child.relname, pg_get_expr(child.relpartbound, child.oid) FROM pg_inherits "
        "JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid WHERE pg_inherits.inhparent = to_regclass(CAST(:parent AS text))",
    )
    held = [(str(name), str(bound)) for name, bound in db_session.execute(attached_statement, {"parent": parent}).all()]
    for name, _ in held:
        db_session.execute(text(f"ALTER TABLE {parent} DETACH PARTITION {name}"))
        db_session.execute(text(f"ALTER TABLE {name} RENAME TO held_{name[-24:]}"))
    real_now = datetime.now(UTC).replace(tzinfo=None)
    monday = datetime(real_now.year, real_now.month, real_now.day) - timedelta(days=real_now.weekday())
    db_session.execute(
        text(
            f"CREATE TABLE {parent}_p_rehearsal PARTITION OF {parent} "
            f"FOR VALUES FROM ('{monday.isoformat(sep=' ')}') TO ('{(monday + WEEK).isoformat(sep=' ')}')",
        ),
    )
    db_session.commit()
    yield monday
    db_session.rollback()
    for name, _ in db_session.execute(attached_statement, {"parent": parent}).all():
        db_session.execute(text(f"DROP TABLE {name}"))
    db_session.execute(text(f"DROP TABLE IF EXISTS {parent}_p_rehearsal"))
    for name, bound in held:
        db_session.execute(text(f"ALTER TABLE held_{name[-24:]} RENAME TO {name}"))
        db_session.execute(text(f"ALTER TABLE {parent} ATTACH PARTITION {name} {bound}"))
    db_session.commit()


def test_future_as_of_makes_the_current_weekly_partition_eligible(
    db_session: scoped_session[Session],
    current_week_partition: datetime,
) -> None:
    from horde.database.kudos_retention import parse_rehearsal_as_of, run_retention_tick

    real_now = datetime.now(UTC).replace(tzinfo=None)
    assert run_retention_tick(now=real_now, policy=_policy(), dataset=STATISTICS).outcome == RetentionTickOutcome.IDLE
    assert _catalog(db_session) == []

    as_of = parse_rehearsal_as_of((real_now + timedelta(days=40)).isoformat(), policy=_policy(), real_now=real_now)
    refused = run_retention_tick(now=as_of, policy=_policy(), dataset=STATISTICS)
    assert (refused.outcome, refused.detail) == (RetentionTickOutcome.REFUSED, FUTURE_REFERENCE_REFUSAL)
    assert _catalog(db_session) == []
    result = run_retention_tick(now=as_of, policy=_policy(), dataset=STATISTICS, confirm_live_data_loss=True)
    assert (result.outcome, result.to_state) == (RetentionTickOutcome.STEPPED, KudosRetentionState.ROLLED_UP)
    assert result.unit is not None
    assert (result.unit.partition_name, result.unit.lower) == ("kudos_stat_events_p_rehearsal", current_week_partition)
    # Partition creation follows as_of too: the layout now reaches two widths past it.
    uppers = [
        parse_partition_bound(str(bound))
        for (bound,) in db_session.execute(
            text(
                "SELECT pg_get_expr(relpartbound, inhrelid) FROM pg_inherits JOIN pg_class ON pg_class.oid = inhrelid "
                "WHERE inhparent = to_regclass('kudos_stat_events')",
            ),
        ).all()
    ]
    assert max(bounds[1] for bounds in uppers if bounds is not None and bounds[1] is not None) >= as_of + 2 * WEEK


# Guards on the replacement and the drop, run against an in-memory object store


def _run(archive: Any, now: datetime = AFTER_WINDOW, **policy_overrides: Any) -> Any:
    from horde.database.kudos_retention import run_retention_tick

    return run_retention_tick(now=now, policy=_policy(**policy_overrides), archive=archive)


def _unit_of(db_session: scoped_session[Session], partition: str) -> Any:
    return next(unit for unit in _catalog(db_session) if unit.partition_name == partition)


def test_empty_first_legacy_unit_chains_into_a_populated_week_through_drop_and_reconcile(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.classes.base.kudos import KudosLedgerCheckpoint
    from horde.database.kudos_reconciliation import _applied_totals_subquery

    legacy = make_partition("kudos_ledger", None, MONDAY)
    week = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    # Still inside the window at AFTER_WINDOW, so it holds the reconciliation tail.
    make_partition("kudos_ledger", MONDAY + WEEK, AFTER_WINDOW)
    user = make_user()
    db_session.add_all(
        [
            _posting(MONDAY + timedelta(hours=1), user.id, "5"),
            _posting(MONDAY + timedelta(hours=2), user.id, "3", escrow=True),
            _posting(MONDAY + WEEK + timedelta(hours=1), user.id, "2"),
        ],
    )
    db_session.commit()

    _run_until(AFTER_WINDOW, _fake_archive(_FakeObjectStore()), lambda result: result.outcome == RetentionTickOutcome.IDLE)

    units = _catalog(db_session)
    assert [(unit.partition_name, unit.state, unit.row_count) for unit in units] == [
        (legacy, KudosRetentionState.DROPPED, 0),
        (week, KudosRetentionState.DROPPED, 2),
    ]
    assert not _attached(db_session, legacy)
    assert not _attached(db_session, week)
    checkpoints = {
        (row.boundary, row.user_id): (row.balance_total, row.escrow_total)
        for row in db_session.scalars(select(KudosLedgerCheckpoint)).all()
    }
    # The empty legacy unit wrote no rows at its boundary, and the week still chained from it.
    assert checkpoints == {(MONDAY + WEEK, user.id): (Decimal(5), Decimal(3))}
    totals_subquery = _applied_totals_subquery()
    totals = {
        int(row.user_id): (Decimal(row.balance_total), Decimal(row.escrow_total))
        for row in db_session.execute(
            select(totals_subquery.c.user_id, totals_subquery.c.balance_total, totals_subquery.c.escrow_total),
        ).all()
    }
    db_session.commit()
    assert totals[user.id] == (Decimal(7), Decimal(3))


def test_boundary_already_checkpointed_by_another_partition_blocks_without_rewriting(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.classes.base.kudos import KudosLedgerCheckpoint

    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("1", "0")})
    # A hand-made partition's unit already recorded the boundary at this partition's upper bound.
    _seed_catalog_unit(db_session, CURRENCY, "kudos_ledger_p_handmade", MONDAY, MONDAY + WEEK, KudosRetentionState.DROPPED)
    db_session.add(KudosLedgerCheckpoint(boundary=MONDAY + WEEK, user_id=user.id, balance_total=Decimal(40), escrow_total=Decimal(0)))
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()

    result = _tick(AFTER_WINDOW)
    assert result.outcome == RetentionTickOutcome.BLOCKED
    assert result.detail.startswith("boundary already checkpointed")
    assert _unit_of(db_session, partition).error == "boundary already checkpointed"
    rows = db_session.scalars(select(KudosLedgerCheckpoint).where(KudosLedgerCheckpoint.boundary == MONDAY + WEEK)).all()
    assert [(row.user_id, row.balance_total) for row in rows] == [(user.id, Decimal(40))]


def test_row_committed_after_the_rollup_blocks_the_drop(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()
    archive = _fake_archive(_FakeObjectStore())
    assert [_run(archive).to_state for _ in range(2)] == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED]
    unit = _catalog(db_session)[0]
    assert unit.row_count == 1
    assert unit.max_id is not None

    db_session.add(_stat_event(MONDAY + timedelta(hours=2), "1"))
    db_session.commit()
    result = _run(archive)
    assert result.outcome == RetentionTickOutcome.BLOCKED
    assert result.detail.startswith(f"late rows: recorded rows=1 max_id={unit.max_id}, found rows=2")
    assert (_catalog(db_session)[0].state, _catalog(db_session)[0].error) == (KudosRetentionState.BLOCKED, "late rows")
    assert _attached(db_session, partition)


def test_export_holding_rows_committed_after_the_checkpoint_blocks_verify(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()
    archive = _fake_archive(_FakeObjectStore())
    assert _run(archive).to_state == KudosRetentionState.CHECKPOINTED

    db_session.add(_posting(MONDAY + timedelta(hours=3), user.id, "1"))
    db_session.commit()
    assert _run(archive).to_state == KudosRetentionState.EXPORTED
    # The live rows match the export, but neither matches the rows the checkpoint summed.
    result = _run(archive)
    assert (result.outcome, result.detail) == (RetentionTickOutcome.BLOCKED, "online drift since export: recorded=1 archived=2")
    assert _attached(db_session, partition)


def test_failed_dumps_retry_then_block_and_an_operator_waives_the_drop(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.classes.base.kudos import KudosStatRollup
    from horde.database.kudos_retention import RETENTION_DUMP_FAILED_LOG_PREFIX, retention_outcome_warning, unblock_unit, waive_unit_dump

    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()
    store = _FakeObjectStore()
    store.fail_uploads = True
    archive = _fake_archive(store)
    assert _run(archive).to_state == KudosRetentionState.ROLLED_UP

    first = _run(archive)
    assert first.to_state == KudosRetentionState.DUMP_FAILED
    assert dump_attempt_failed(first)
    warning = retention_outcome_warning(first)
    assert warning is not None
    assert warning.startswith(RETENTION_DUMP_FAILED_LOG_PREFIX)
    assert f"(1 consecutive failures since {AFTER_WINDOW.isoformat()})" in warning
    unit = _catalog(db_session)[0]
    assert (unit.first_failed_at, unit.consecutive_dump_failures) == (AFTER_WINDOW, 1)
    with pytest.raises(ValueError, match="not blocked"):
        unblock_unit(unit.id)

    # Within the retry window the unit stays dump_failed and the failure count grows.
    second = _run(archive, AFTER_WINDOW + timedelta(days=6))
    assert second.to_state == KudosRetentionState.DUMP_FAILED
    assert "(2 consecutive failures" in second.detail
    assert _attached(db_session, partition)

    exhausted = _run(archive, AFTER_WINDOW + timedelta(days=8))
    assert (exhausted.outcome, exhausted.from_state) == (RetentionTickOutcome.BLOCKED, KudosRetentionState.DUMP_FAILED)
    assert dump_attempt_failed(exhausted)
    assert "3 consecutive failures" in exhausted.detail
    unit = _catalog(db_session)[0]
    assert (unit.state, unit.error, unit.consecutive_dump_failures) == (
        KudosRetentionState.BLOCKED,
        "dump retries exhausted",
        3,
    )
    assert _run(archive, AFTER_WINDOW + timedelta(days=8)).outcome == RetentionTickOutcome.IDLE

    assert waive_unit_dump(unit.id).state == KudosRetentionState.DUMP_WAIVED
    with pytest.raises(ValueError, match="can be waived"):
        waive_unit_dump(unit.id)
    dropped = _run(archive, AFTER_WINDOW + timedelta(days=8))
    assert dropped.to_state == KudosRetentionState.DROPPED
    assert not _attached(db_session, partition)
    assert len(db_session.scalars(select(KudosStatRollup)).all()) == 1


def test_successful_dump_retry_clears_the_failure_run(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    assert _run(archive).to_state == KudosRetentionState.ROLLED_UP
    store.fail_uploads = True
    assert _run(archive).to_state == KudosRetentionState.DUMP_FAILED
    store.fail_uploads = False
    retried = _run(archive, AFTER_WINDOW + timedelta(days=1))
    assert (retried.from_state, retried.to_state) == (KudosRetentionState.DUMP_FAILED, KudosRetentionState.DUMPED)
    unit = _catalog(db_session)[0]
    assert (unit.first_failed_at, unit.consecutive_dump_failures) == (None, 0)
    assert unit.object_key in store.objects


def test_waiving_refuses_currency_and_undumped_units(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    from horde.database.kudos_retention import waive_unit_dump

    make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    _seed_catalog_unit(db_session, CURRENCY, "kudos_ledger_p_20200106", MONDAY, MONDAY + WEEK, KudosRetentionState.CHECKPOINTED)
    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.ROLLED_UP
    statistics_unit = next(unit for unit in _catalog(db_session) if unit.dataset == STATISTICS)
    currency_unit = next(unit for unit in _catalog(db_session) if unit.dataset == CURRENCY)
    with pytest.raises(ValueError, match="only statistics dumps"):
        waive_unit_dump(currency_unit.id)
    with pytest.raises(ValueError, match="can be waived"):
        waive_unit_dump(statistics_unit.id)


def test_unblock_returns_a_checkpointed_currency_unit_to_checkpointed_and_waits_for_the_lock(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.database.kudos_db import release_retention_lock, try_acquire_retention_lock
    from horde.database.kudos_retention import RetentionLockBusyError, unblock_unit
    from horde.flask import db
    from horde.kudos_archive import ArchiveTier

    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    assert _run(archive).to_state == KudosRetentionState.CHECKPOINTED

    # Another unit's object fills the budget, so the export is refused.
    budget = 1_000_000
    own_key = archive.object_key(
        tier=ArchiveTier.CURRENCY,
        source_range=format_source_range(partition_name=partition, lower=MONDAY, upper=MONDAY + WEEK),
    )
    store.objects["currency/kudos_ledger_p_other__20191230T000000_20200106T000000.csv.zst"] = b"x" * budget
    blocked = _run(archive, currency_budget_bytes=budget)
    assert (blocked.outcome, _unit_of(db_session, partition).error) == (RetentionTickOutcome.BLOCKED, "currency tier would exceed budget")
    unit = _unit_of(db_session, partition)

    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as holder:
        assert try_acquire_retention_lock(connection=holder)
        with pytest.raises(RetentionLockBusyError, match="retention lock is held"):
            unblock_unit(unit.id)
        assert release_retention_lock(connection=holder)
    assert _unit_of(db_session, partition).state == KudosRetentionState.BLOCKED

    # The checkpoint is committed, so the unit resumes at export instead of recomputing a boundary.
    reset = unblock_unit(unit.id)
    assert (reset.state, reset.error) == (KudosRetentionState.CHECKPOINTED, None)

    # A leftover object at the unit's own key is replaced by the export, so it does not count against the budget.
    del store.objects["currency/kudos_ledger_p_other__20191230T000000_20200106T000000.csv.zst"]
    store.objects[own_key] = b"x" * budget
    exported = _run(archive, currency_budget_bytes=budget)
    assert exported.to_state == KudosRetentionState.EXPORTED
    assert len(store.objects[own_key]) < budget


def test_unblock_returns_a_currency_unit_blocked_before_its_checkpoint_to_pending(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.database.kudos_retention import unblock_unit

    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5", applied=False))
    db_session.commit()
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.BLOCKED
    assert unblock_unit(_unit_of(db_session, partition).id).state == KudosRetentionState.PENDING


def test_old_transaction_holding_a_partition_lock_defers_the_drop(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    import time

    from horde.flask import db

    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()
    archive = _fake_archive(_FakeObjectStore())
    assert [_run(archive).to_state for _ in range(2)] == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED]

    with db.engine.connect() as holder:
        holder.execute(text(f"LOCK TABLE {partition} IN ACCESS SHARE MODE"))
        time.sleep(0.05)
        deferred = _run(archive, drop_defer_lock_age=timedelta(milliseconds=10))
        assert deferred.outcome == RetentionTickOutcome.DEFERRED
        assert deferred.detail.startswith("pid ")
        assert _attached(db_session, partition)
        assert _catalog(db_session)[0].state == KudosRetentionState.DUMPED
        db_session.commit()
        holder.rollback()
    assert _run(archive, drop_defer_lock_age=timedelta(milliseconds=10)).to_state == KudosRetentionState.DROPPED
    assert not _attached(db_session, partition)


def test_restore_into_a_schema_qualified_scratch_table(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.database.kudos_retention import restore_archive_unit

    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()
    archive = _fake_archive(_FakeObjectStore())
    assert [_run(archive).to_state for _ in range(2)] == [KudosRetentionState.CHECKPOINTED, KudosRetentionState.EXPORTED]
    key = _unit_of(db_session, partition).object_key
    schema = db_session.execute(text("SELECT current_schema()")).scalar_one()
    scratch = f"kudos_restore_{uuid.uuid4().hex[:8]}"
    db_session.commit()
    try:
        assert restore_archive_unit(archive, key, table_name=f"{schema}.{scratch}") == 1
        restored = db_session.execute(text(f'SELECT user_id, amount FROM "{schema}"."{scratch}"')).all()
        assert [tuple(row) for row in restored] == [(user.id, Decimal("5.00"))]
    finally:
        db_session.rollback()
        db_session.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{scratch}"'))
        db_session.commit()


def test_statistics_budget_blocks_the_dump_without_holding_later_statistics(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import unblock_unit

    first = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    second = make_partition("kudos_stat_events", MONDAY + WEEK, MONDAY + 2 * WEEK)
    db_session.add_all([_stat_event(MONDAY + timedelta(hours=1), "2"), _stat_event(MONDAY + WEEK + timedelta(hours=1), "3")])
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    assert _run(archive, statistics_budget_bytes=1).to_state == KudosRetentionState.ROLLED_UP
    blocked = _run(archive, statistics_budget_bytes=1)
    assert (blocked.outcome, blocked.detail.startswith("statistics tier would exceed budget: estimate=")) == (
        RetentionTickOutcome.BLOCKED,
        True,
    )
    assert (_unit_of(db_session, first).state, _unit_of(db_session, first).error) == (
        KudosRetentionState.BLOCKED,
        RetentionBlockReason.STATISTICS_BUDGET_EXCEEDED,
    )
    assert not [key for key in store.objects if key.startswith("statistics/")]
    # The blocked unit does not hold the next statistics unit.
    assert _run(archive, statistics_budget_bytes=1).to_state == KudosRetentionState.ROLLED_UP
    assert _unit_of(db_session, second).state == KudosRetentionState.ROLLED_UP

    # With room in the budget, an unblocked unit rolls up again and dumps, and nothing is evicted.
    assert unblock_unit(_unit_of(db_session, first).id).state == KudosRetentionState.PENDING
    states = [_run(archive, statistics_budget_bytes=10_000_000, hold_drops=True).to_state for _ in range(3)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED, KudosRetentionState.DUMPED]
    assert len([key for key in store.objects if key.startswith("statistics/") and key.endswith(".zst")]) == 2


def test_statistics_unit_blocked_by_the_budget_can_be_waived(
    db_session: scoped_session[Session], make_partition: Callable[..., str]
) -> None:
    from horde.database.kudos_retention import waive_unit_dump

    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()
    archive = _fake_archive(_FakeObjectStore())
    states = [_run(archive, statistics_budget_bytes=1).to_state for _ in range(2)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.BLOCKED]
    waived = waive_unit_dump(_unit_of(db_session, partition).id)
    assert (waived.state, waived.error) == (KudosRetentionState.DUMP_WAIVED, RetentionBlockReason.STATISTICS_BUDGET_EXCEEDED)
    assert _run(archive, statistics_budget_bytes=1).to_state == KudosRetentionState.DROPPED


def _verified_currency_week(
    db_session: scoped_session[Session], make_partition: Callable[..., str], make_user: MakeUser
) -> tuple[str, Any, Any]:
    """Create a currency week with one posting and step it to ``VERIFIED`` with drops held. Returns the partition, store
    and archive."""
    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    states = [_run(archive, hold_drops=True).to_state for _ in range(3)]
    assert states == [KudosRetentionState.CHECKPOINTED, KudosRetentionState.EXPORTED, KudosRetentionState.VERIFIED]
    return partition, store, archive


@pytest.mark.parametrize("hold_drops", [False, True])
def test_verified_currency_unit_whose_object_vanishes_blocks_and_is_exported_again(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
    hold_drops: bool,
) -> None:
    """The drop's archive check blocks the unit, and with drops held the post-step listing blocks it instead."""
    from horde.database.kudos_retention import unblock_unit
    from horde.kudos_archive import manifest_key

    partition, store, archive = _verified_currency_week(db_session, make_partition, make_user)
    key = _unit_of(db_session, partition).object_key
    del store.objects[key]
    del store.objects[manifest_key(key)]
    _run(archive, hold_drops=hold_drops)
    unit = _unit_of(db_session, partition)
    assert (unit.state, unit.error, unit.object_key) == (KudosRetentionState.BLOCKED, RetentionBlockReason.ARCHIVE_OBJECT_MISSING, None)
    assert _attached(db_session, partition)

    assert unblock_unit(unit.id).state == KudosRetentionState.CHECKPOINTED
    states = [_run(archive).to_state for _ in range(3)]
    assert states == [KudosRetentionState.EXPORTED, KudosRetentionState.VERIFIED, KudosRetentionState.DROPPED]
    assert key in store.objects
    assert not _relation_present(db_session, partition)


def test_partition_restored_under_its_dropped_name_stops_its_dataset_until_it_is_detached(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    """A dropped currency partition restored and attached under the same name stops currency, and statistics proceed."""
    from horde.database.kudos_retention import RESTORED_PARTITION_REFUSAL, retention_health, retention_outcome_warning, run_retention_tick

    partition, _, archive = _verified_currency_week(db_session, make_partition, make_user)
    assert _run(archive).to_state == KudosRetentionState.DROPPED
    assert not _relation_present(db_session, partition)
    restored = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    assert restored == partition
    statistics_partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()

    stepped = _run(archive)
    assert (stepped.dataset, stepped.unit.partition_name, stepped.to_state) == (
        STATISTICS,
        statistics_partition,
        KudosRetentionState.ROLLED_UP,
    )
    refused = run_retention_tick(now=AFTER_WINDOW, policy=_policy(), archive=archive, dataset=CURRENCY)
    assert (refused.outcome, refused.dataset, refused.unit) == (RetentionTickOutcome.BLOCKED, CURRENCY, None)
    assert refused.detail == f"{RESTORED_PARTITION_REFUSAL}: {partition}"
    assert retention_outcome_warning(refused) == f"Kudos retention blocked currency dataset: {RESTORED_PARTITION_REFUSAL}: {partition}"
    assert retention_health(now=AFTER_WINDOW)["restored_partitions"] == [{"dataset": CURRENCY, "partition_name": partition}]
    assert _attached(db_session, partition)
    assert _unit_of(db_session, partition).state == KudosRetentionState.DROPPED

    db_session.execute(text(f"ALTER TABLE kudos_ledger DETACH PARTITION {partition}"))
    db_session.commit()
    assert retention_health(now=AFTER_WINDOW)["restored_partitions"] == []
    assert run_retention_tick(now=AFTER_WINDOW, policy=_policy(), archive=archive, dataset=CURRENCY).outcome == RetentionTickOutcome.IDLE


def test_drop_blocks_a_currency_unit_without_an_object_key(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from horde.classes.base.kudos import KudosRetentionUnit

    partition, _, archive = _verified_currency_week(db_session, make_partition, make_user)
    db_session.execute(update(KudosRetentionUnit).where(KudosRetentionUnit.partition_name == partition).values(object_key=None))
    db_session.commit()
    blocked = _run(archive)
    assert (blocked.outcome, blocked.detail) == (RetentionTickOutcome.BLOCKED, "archive object missing: no object key is recorded")
    assert _attached(db_session, partition)


def _dumped_statistics_week(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> tuple[str, Any, Any]:
    """Create a statistics week with one event and step it to ``DUMPED`` with drops held. Returns the partition, store
    and archive."""
    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "2"))
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    states = [_run(archive, hold_drops=True).to_state for _ in range(2)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED]
    return partition, store, archive


def _delete_object(store: Any, key: str) -> None:
    from horde.kudos_archive import manifest_key

    del store.objects[key]
    del store.objects[manifest_key(key)]


def test_tick_dumps_again_a_statistics_unit_whose_dump_vanished_and_reports_tier_bytes(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    """A dump removed outside the horde while the raw rows are online is repeated, and the drop then proceeds."""
    partition, store, archive = _dumped_statistics_week(db_session, make_partition)
    key = _unit_of(db_session, partition).object_key
    assert key is not None

    _delete_object(store, key)
    idle = _run(archive, hold_drops=True)
    assert idle.archive_tier_bytes == {ArchiveTier.CURRENCY: 0, ArchiveTier.STATISTICS: 0}
    unit = _unit_of(db_session, partition)
    assert (unit.state, unit.object_key) == (KudosRetentionState.DUMP_FAILED, None)
    assert unit.error == f"{RetentionBlockReason.ARCHIVE_OBJECT_MISSING}: {key}"
    assert unit.first_failed_at is not None

    redumped = _run(archive, hold_drops=True)
    assert redumped.to_state == KudosRetentionState.DUMPED
    assert _unit_of(db_session, partition).object_key == key
    assert redumped.archive_tier_bytes is not None and redumped.archive_tier_bytes[ArchiveTier.STATISTICS] > 0
    assert _run(archive).to_state == KudosRetentionState.DROPPED


@pytest.mark.parametrize("resolution", ["unblock", "waive"])
def test_drop_blocks_a_statistics_unit_whose_dump_vanished_since_the_last_listing(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    resolution: str,
) -> None:
    """The drop checks each dump, so the raw rows are kept when a dump is removed after the last tick."""
    from horde.database.kudos_retention import unblock_unit, waive_unit_dump

    partition, store, archive = _dumped_statistics_week(db_session, make_partition)
    _delete_object(store, _unit_of(db_session, partition).object_key)
    blocked = _run(archive)
    assert blocked.outcome == RetentionTickOutcome.BLOCKED
    assert blocked.detail.startswith(str(RetentionBlockReason.ARCHIVE_OBJECT_MISSING))
    assert _attached(db_session, partition)

    unit_id = _unit_of(db_session, partition).id
    if resolution == "unblock":
        assert unblock_unit(unit_id).state == KudosRetentionState.PENDING
        states = [_run(archive).to_state for _ in range(3)]
        assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED, KudosRetentionState.DROPPED]
    else:
        assert waive_unit_dump(unit_id).state == KudosRetentionState.DUMP_WAIVED
        assert _run(archive).to_state == KudosRetentionState.DROPPED
    assert not _relation_present(db_session, partition)


def test_detached_statistics_unit_whose_dump_vanished_blocks_until_reattached(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    """A dump lost during the drop grace blocks the unit, and the waiver is refused until the partition is reattached."""
    from horde.database.kudos_retention import reattach_partition, waive_unit_dump

    partition, store, archive = _dumped_statistics_week(db_session, make_partition)
    assert _run(archive, drop_grace=timedelta(days=7)).to_state == KudosRetentionState.DETACHED
    _delete_object(store, _unit_of(db_session, partition).object_key)
    _run(archive, drop_grace=timedelta(days=7))
    unit = _unit_of(db_session, partition)
    assert (unit.state, unit.error) == (KudosRetentionState.BLOCKED, RetentionBlockReason.ARCHIVE_OBJECT_MISSING)
    assert unit.detached_at is not None
    with pytest.raises(ValueError, match="Reattach it first"):
        waive_unit_dump(unit.id)
    # The grace has passed, but a blocked unit keeps the detached partition.
    _run(archive, AFTER_WINDOW + timedelta(days=8), drop_grace=timedelta(days=7))
    assert _relation_present(db_session, partition)

    reattached = reattach_partition(unit.id)
    assert [(reattached_unit.state, reattached_unit.error) for reattached_unit in reattached] == [
        (KudosRetentionState.BLOCKED, RetentionBlockReason.ARCHIVE_OBJECT_MISSING),
    ]
    assert _attached(db_session, partition)
    assert waive_unit_dump(unit.id).state == KudosRetentionState.DUMP_WAIVED


def test_dropped_statistics_unit_keeps_the_key_of_a_vanished_dump_so_the_audit_reports_it(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import MissingArchiveObject, audit_archive

    partition, store, archive = _dumped_statistics_week(db_session, make_partition)
    assert _run(archive).to_state == KudosRetentionState.DROPPED
    key = _unit_of(db_session, partition).object_key
    _delete_object(store, key)
    for _ in range(2):
        _run(archive)
        assert _unit_of(db_session, partition).object_key == key
    missing = [record for record in audit_archive(archive) if isinstance(record, MissingArchiveObject)]
    assert [(record.object_key, record.state) for record in missing] == [(key, KudosRetentionState.DROPPED)]


def test_eviction_keeps_the_dumps_of_units_not_dropped_and_clears_the_keys_it_deletes(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    """Evicted dumps are not reported as lost, and a dump whose raw rows are still online is never evicted."""
    from horde.database.kudos_retention import MissingArchiveObject, audit_archive, evict_statistics_dumps

    dropped_partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    held_partition = make_partition("kudos_stat_events", MONDAY + WEEK, MONDAY + 2 * WEEK)
    db_session.add_all([_stat_event(MONDAY + timedelta(hours=1), "2"), _stat_event(MONDAY + WEEK + timedelta(hours=1), "3")])
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    states = [_run(archive).to_state for _ in range(3)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED, KudosRetentionState.DROPPED]
    states = [_run(archive, hold_drops=True).to_state for _ in range(2)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED]
    dropped_key = _unit_of(db_session, dropped_partition).object_key
    held_key = _unit_of(db_session, held_partition).object_key

    eviction = evict_statistics_dumps(archive, budget_bytes=0)
    assert eviction.deleted_keys == [dropped_key]
    assert eviction.tier_bytes == archive.tier_bytes(ArchiveTier.STATISTICS) > 0
    assert held_key in store.objects and dropped_key not in store.objects
    assert _unit_of(db_session, dropped_partition).object_key is None
    assert _unit_of(db_session, held_partition).object_key == held_key
    assert [record for record in audit_archive(archive) if isinstance(record, MissingArchiveObject)] == []
    assert _run(archive, hold_drops=True).outcome == RetentionTickOutcome.IDLE
    assert _unit_of(db_session, held_partition).state == KudosRetentionState.DUMPED


def test_tick_aborts_multipart_uploads_older_than_a_day(db_session: scoped_session[Session]) -> None:
    store = _FakeObjectStore()
    store.uploads["stale"] = ("statistics/stale.csv.zst", datetime.now(UTC) - timedelta(days=2), {})
    store.uploads["running"] = ("statistics/running.csv.zst", datetime.now(UTC), {})
    assert _run(_fake_archive(store), enabled=False).outcome == RetentionTickOutcome.IDLE
    assert set(store.uploads) == {"running"}


def test_failed_dump_waits_out_the_spacing_while_later_statistics_proceed(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    first = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    second = make_partition("kudos_stat_events", MONDAY + WEEK, MONDAY + 2 * WEEK)
    db_session.add_all([_stat_event(MONDAY + timedelta(hours=1), "2"), _stat_event(MONDAY + WEEK + timedelta(hours=1), "3")])
    db_session.commit()
    store = _FakeObjectStore()
    store.fail_uploads = True
    archive = _fake_archive(store)

    assert _run(archive).to_state == KudosRetentionState.ROLLED_UP
    assert _run(archive).to_state == KudosRetentionState.DUMP_FAILED
    assert _unit_of(db_session, first).last_dump_attempt_at == AFTER_WINDOW
    # Inside the spacing the failed unit is skipped, so the later partition is rolled up and dumped.
    stepped = [_run(archive, AFTER_WINDOW + timedelta(minutes=minute)) for minute in (1, 2)]
    assert [(result.unit.partition_name, result.to_state) for result in stepped] == [
        (second, KudosRetentionState.ROLLED_UP),
        (second, KudosRetentionState.DUMP_FAILED),
    ]
    assert _run(archive, AFTER_WINDOW + timedelta(minutes=3)).outcome == RetentionTickOutcome.IDLE
    retried = _run(archive, AFTER_WINDOW + timedelta(minutes=15))
    assert (retried.unit.partition_name, retried.to_state) == (first, KudosRetentionState.DUMP_FAILED)
    assert _unit_of(db_session, first).consecutive_dump_failures == 2


def test_recheckpoint_cascade_recovers_a_late_row_through_the_later_units(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
) -> None:
    from sqlalchemy import func

    from horde.classes.base.kudos import KudosLedgerCheckpoint, KudosLedgerDailyStatement
    from horde.database.kudos_reconciliation import (
        StaleCheckpointsError,
        _applied_totals_subquery,
        create_balance_snapshot,
        reconcile_balances,
        stale_checkpoint_units,
    )
    from horde.database.kudos_retention import recheckpoint_from_unit

    def applied_balance_total() -> Decimal:
        totals_subquery = _applied_totals_subquery()
        totals = {
            int(row.user_id): Decimal(row.balance_total)
            for row in db_session.execute(select(totals_subquery.c.user_id, totals_subquery.c.balance_total)).all()
        }
        db_session.commit()
        return totals[user.id]

    unit_n = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    unit_next = make_partition("kudos_ledger", MONDAY + WEEK, MONDAY + 2 * WEEK)
    user = make_user()
    _seed_checkpoint(db_session, MONDAY, {user.id: ("10", "0")})
    db_session.add_all([_posting(MONDAY + timedelta(hours=1), user.id, "5"), _posting(MONDAY + WEEK + timedelta(hours=1), user.id, "2")])
    db_session.commit()

    # Without an archive both units checkpoint, so the next unit's boundary chains from unit N's.
    assert [_tick(AFTER_WINDOW).to_state for _ in range(2)] == [KudosRetentionState.CHECKPOINTED] * 2
    archive = _fake_archive(_FakeObjectStore())
    assert [_run(archive).to_state for _ in range(2)] == [KudosRetentionState.EXPORTED, KudosRetentionState.VERIFIED]

    db_session.add(_posting(MONDAY + timedelta(hours=2), user.id, "4"))
    db_session.commit()
    # Simulate a baseline created before the late-row incident was detected (also covers old software snapshots).
    snapshot_id = create_balance_snapshot()
    blocked = _run(archive)
    assert (blocked.outcome, _unit_of(db_session, unit_n).error) == (RetentionTickOutcome.BLOCKED, "late rows")
    assert _unit_of(db_session, unit_next).state == KudosRetentionState.CHECKPOINTED

    # Checkpoints N and N+1 omit the late row, so a repair would compensate for drift that does not exist.
    assert applied_balance_total() == Decimal(10 + 5 + 2)
    assert [stale_unit.partition_name for stale_unit in stale_checkpoint_units()] == [unit_n]
    db_session.commit()
    with pytest.raises(StaleCheckpointsError):
        create_balance_snapshot()
    with pytest.raises(StaleCheckpointsError, match="retention unblock --recheckpoint"):
        reconcile_balances(snapshot_id, apply_repairs=True)
    reconcile_balances(snapshot_id)

    reset = recheckpoint_from_unit(_unit_of(db_session, unit_n).id)
    assert [(unit.partition_name, unit.state, unit.row_count) for unit in reset] == [
        (unit_n, KudosRetentionState.PENDING, None),
        (unit_next, KudosRetentionState.PENDING, None),
    ]
    # The reset deleted the stale checkpoints and statements, so the tail after the seeded boundary holds the late row.
    assert db_session.execute(select(func.max(KudosLedgerCheckpoint.boundary))).scalar_one() == MONDAY
    n_range = format_source_range(partition_name=unit_n, lower=MONDAY, upper=MONDAY + WEEK)
    daily_rows = db_session.scalars(select(KudosLedgerDailyStatement).where(KudosLedgerDailyStatement.source_range == n_range)).all()
    assert daily_rows == []
    db_session.commit()
    assert applied_balance_total() == Decimal(10 + 5 + 4 + 2)
    assert stale_checkpoint_units() == []
    db_session.commit()
    for apply_repairs in (False, True):
        with pytest.raises(StaleCheckpointsError, match="rebuilt checkpoint"):
            reconcile_balances(snapshot_id, apply_repairs=apply_repairs)
    from horde.classes.base.kudos import KudosBalanceSnapshot

    assert db_session.query(KudosBalanceSnapshot).filter_by(snapshot_id=snapshot_id).count() > 0
    db_session.commit()
    assert reconcile_balances(create_balance_snapshot(), apply_repairs=True) == []

    _run_until(AFTER_WINDOW, archive, lambda result: result.outcome == RetentionTickOutcome.IDLE)
    assert [(unit.partition_name, unit.state, unit.row_count) for unit in _catalog(db_session)] == [
        (unit_n, KudosRetentionState.DROPPED, 2),
        (unit_next, KudosRetentionState.DROPPED, 1),
    ]
    balances = {
        row.boundary: row.balance_total
        for row in db_session.scalars(select(KudosLedgerCheckpoint).where(KudosLedgerCheckpoint.user_id == user.id)).all()
    }
    assert balances == {MONDAY: Decimal(10), MONDAY + WEEK: Decimal(19), MONDAY + 2 * WEEK: Decimal(21)}
    statement_sum = sum(
        row.amount_sum
        for row in db_session.scalars(select(KudosLedgerDailyStatement).where(KudosLedgerDailyStatement.source_range == n_range)).all()
    )
    assert statement_sum == Decimal(9)
    # Zero drift: the reconciliation baseline equals the seeded total plus every posting, the late one included.
    assert applied_balance_total() == Decimal(10 + 5 + 4 + 2)


def test_recheckpoint_refuses_when_a_later_currency_unit_is_dropped(db_session: scoped_session[Session]) -> None:
    from horde.database.kudos_retention import recheckpoint_from_unit

    _seed_catalog_unit(db_session, CURRENCY, "kudos_ledger_p_20200106", MONDAY, MONDAY + WEEK, KudosRetentionState.BLOCKED)
    _seed_catalog_unit(db_session, CURRENCY, "kudos_ledger_p_20200113", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.DROPPED)
    _seed_catalog_unit(db_session, STATISTICS, "kudos_stat_events_p_20200106", MONDAY, MONDAY + WEEK, KudosRetentionState.BLOCKED)
    units = {unit.partition_name: unit for unit in _catalog(db_session)}
    with pytest.raises(ValueError, match="later dropped currency units"):
        recheckpoint_from_unit(units["kudos_ledger_p_20200106"].id)
    with pytest.raises(ValueError, match="only a blocked currency unit"):
        recheckpoint_from_unit(units["kudos_stat_events_p_20200106"].id)
    assert _unit_of(db_session, "kudos_ledger_p_20200106").state == KudosRetentionState.BLOCKED


# Held drops, the drop grace period and reattaching


def test_selection_skips_held_and_not_yet_due_detached_units() -> None:
    week_one = _unit(1, STATISTICS, "s1", MONDAY, MONDAY + WEEK, KudosRetentionState.DUMPED)
    week_two = _unit(2, STATISTICS, "s2", MONDAY + WEEK, MONDAY + 2 * WEEK, KudosRetentionState.PENDING)
    partitions = [_partition(STATISTICS, "s1", MONDAY, MONDAY + WEEK), _partition(STATISTICS, "s2", MONDAY + WEEK, MONDAY + 2 * WEEK)]

    def selected(catalog: list[CatalogUnit], **options: Any) -> CatalogUnit | None:
        return select_next_unit(
            dataset=STATISTICS,
            partitions=partitions,
            catalog=catalog,
            cutoff=CUTOFF,
            archive_configured=False,
            **options,
        ).unit

    assert selected([week_one, week_two]) == week_one
    assert selected([week_one, week_two], hold_drops=True) == week_two
    assert selected([week_one], hold_drops=True) is None
    detached_at = MONDAY + 5 * WEEK
    detached = CatalogUnit(
        id=1,
        dataset=STATISTICS,
        partition_name="s1",
        lower=MONDAY,
        upper=MONDAY + WEEK,
        state=KudosRetentionState.DETACHED,
        detached_at=detached_at,
        state_before_detach=KudosRetentionState.DUMPED,
    )
    # A detached partition is no longer among the attached partitions.
    detached_selection = select_next_unit(
        dataset=STATISTICS,
        partitions=partitions[1:],
        catalog=[detached, week_two],
        cutoff=CUTOFF,
        archive_configured=False,
        detached_drop_due_before=detached_at - timedelta(seconds=1),
    )
    assert detached_selection.unit == week_two
    due_selection = select_next_unit(
        dataset=STATISTICS,
        partitions=partitions[1:],
        catalog=[detached, week_two],
        cutoff=CUTOFF,
        archive_configured=False,
        detached_drop_due_before=detached_at,
    )
    assert due_selection.unit == detached
    held_selection = select_next_unit(
        dataset=STATISTICS,
        partitions=partitions[1:],
        catalog=[detached, week_two],
        cutoff=CUTOFF,
        archive_configured=False,
        hold_drops=True,
        detached_drop_due_before=detached_at,
    )
    assert held_selection.unit == week_two


def _relation_present(db_session: scoped_session[Session], name: str) -> bool:
    return db_session.execute(text("SELECT to_regclass(CAST(:name AS text)) IS NOT NULL"), {"name": name}).scalar_one()


def _rows_through_parent(db_session: scoped_session[Session], lower: datetime, upper: datetime) -> int:
    statement = text("SELECT count(*) FROM kudos_stat_events WHERE created >= :lower AND created < :upper")
    return int(db_session.execute(statement, {"lower": lower, "upper": upper}).scalar_one())


def _waive_one_statistics_week(db_session: scoped_session[Session], make_partition: Callable[..., str], **policy_overrides: Any) -> str:
    """Create a statistics week with one row and step it to ``DUMP_WAIVED``, which is drop-ready without an archive."""
    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    db_session.add(_stat_event(MONDAY + timedelta(hours=1), "5"))
    db_session.commit()
    states = [_tick(AFTER_WINDOW, **policy_overrides).to_state for _ in range(2)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMP_WAIVED]
    return partition


def test_held_drops_keep_a_drop_ready_partition_attached_and_the_tick_idles(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import retention_health

    partition = _waive_one_statistics_week(db_session, make_partition, hold_drops=True)
    for _ in range(2):
        assert _tick(AFTER_WINDOW, hold_drops=True).outcome == RetentionTickOutcome.IDLE
    assert _attached(db_session, partition)
    assert _catalog(db_session)[0].state == KudosRetentionState.DUMP_WAIVED
    health = retention_health(now=AFTER_WINDOW, policy=_policy(hold_drops=True))
    assert (health["hold_drops"], health["held_partitions"], health["detached_partitions"]) == (True, 1, [])
    unheld = retention_health(now=AFTER_WINDOW, policy=_policy())
    assert (unheld["hold_drops"], unheld["held_partitions"]) == (False, 0)
    assert (health["drop_grace_days"], unheld["drop_grace_days"]) == (0, 0)
    # Holding the drop also holds the detach of a grace period.
    assert _tick(AFTER_WINDOW, hold_drops=True, drop_grace=timedelta(days=7)).outcome == RetentionTickOutcome.IDLE
    assert _attached(db_session, partition)
    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.DROPPED
    assert not _relation_present(db_session, partition)


GRACE = timedelta(days=7)


def test_drop_grace_detaches_first_and_drops_only_after_the_grace(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import retention_health

    partition = _waive_one_statistics_week(db_session, make_partition, drop_grace=GRACE)
    detach = _tick(AFTER_WINDOW, drop_grace=GRACE)
    assert (detach.outcome, detach.from_state, detach.to_state) == (
        RetentionTickOutcome.STEPPED,
        KudosRetentionState.DUMP_WAIVED,
        KudosRetentionState.DETACHED,
    )
    assert not _attached(db_session, partition)
    assert _relation_present(db_session, partition)
    unit = _catalog(db_session)[0]
    assert (unit.state, unit.state_before_detach, unit.detached_at) == (
        KudosRetentionState.DETACHED,
        KudosRetentionState.DUMP_WAIVED,
        AFTER_WINDOW,
    )
    health = retention_health(now=AFTER_WINDOW, policy=_policy(drop_grace=GRACE))
    assert health["drop_grace_days"] == GRACE / timedelta(days=1)
    assert health["detached_partitions"] == [
        {"dataset": STATISTICS, "partition_name": partition, "detached_at": AFTER_WINDOW, "drop_due_at": AFTER_WINDOW + GRACE},
    ]
    # A detached partition waiting out its grace is skipped, so the tick idles instead of failing.
    almost_due = _tick(AFTER_WINDOW + GRACE - timedelta(seconds=1), drop_grace=GRACE)
    assert almost_due.outcome == RetentionTickOutcome.IDLE
    assert _relation_present(db_session, partition)
    drop = _tick(AFTER_WINDOW + GRACE, drop_grace=GRACE)
    assert (drop.from_state, drop.to_state) == (KudosRetentionState.DETACHED, KudosRetentionState.DROPPED)
    assert not _relation_present(db_session, partition)
    assert _catalog(db_session)[0].state == KudosRetentionState.DROPPED


def test_reattach_restores_the_states_and_the_rows_through_the_parent(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import reattach_partition, unblock_unit

    partition = _waive_one_statistics_week(db_session, make_partition, drop_grace=GRACE)
    assert _rows_through_parent(db_session, MONDAY, MONDAY + WEEK) == 1
    assert _tick(AFTER_WINDOW, drop_grace=GRACE).to_state == KudosRetentionState.DETACHED
    assert _rows_through_parent(db_session, MONDAY, MONDAY + WEEK) == 0
    unit = _catalog(db_session)[0]
    reattached = reattach_partition(unit.id)
    assert [
        (reattached_unit.state, reattached_unit.error, reattached_unit.detached_at, reattached_unit.state_before_detach)
        for reattached_unit in reattached
    ] == [(KudosRetentionState.BLOCKED, RetentionBlockReason.REATTACHED, None, KudosRetentionState.DUMP_WAIVED)]
    assert _attached(db_session, partition)
    assert _rows_through_parent(db_session, MONDAY, MONDAY + WEEK) == 1
    with pytest.raises(ValueError, match="not kept detached"):
        reattach_partition(unit.id)
    # The reattached partition stays attached until an operator unblocks it, whatever the grace.
    later = AFTER_WINDOW + timedelta(days=1)
    assert _tick(later, drop_grace=GRACE).outcome == RetentionTickOutcome.IDLE
    assert _attached(db_session, partition)
    restored = unblock_unit(unit.id)
    assert (restored.state, restored.error, restored.state_before_detach) == (KudosRetentionState.DUMP_WAIVED, None, None)
    # Unblocked, the partition is drop-ready again, so the next tick starts a new grace period from its own time.
    assert _tick(later, drop_grace=GRACE).to_state == KudosRetentionState.DETACHED
    assert _catalog(db_session)[0].detached_at == later


def test_reattach_refuses_once_the_table_is_gone(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    from horde.database.kudos_retention import reattach_partition

    partition = _waive_one_statistics_week(db_session, make_partition, drop_grace=GRACE)
    assert _tick(AFTER_WINDOW, drop_grace=GRACE).to_state == KudosRetentionState.DETACHED
    unit = _catalog(db_session)[0]
    db_session.execute(text(f"DROP TABLE {partition}"))
    db_session.commit()
    with pytest.raises(ValueError, match="no longer exists"):
        reattach_partition(unit.id)
    # A detached partition whose table is gone is marked dropped once its drop falls due.
    assert _tick(AFTER_WINDOW + GRACE, drop_grace=GRACE).to_state == KudosRetentionState.DROPPED
    with pytest.raises(ValueError, match="not kept detached"):
        reattach_partition(unit.id)


def test_reattach_refuses_after_the_drop(db_session: scoped_session[Session], make_partition: Callable[..., str]) -> None:
    from horde.database.kudos_retention import reattach_partition

    _waive_one_statistics_week(db_session, make_partition, drop_grace=GRACE)
    assert _tick(AFTER_WINDOW, drop_grace=GRACE).to_state == KudosRetentionState.DETACHED
    assert _tick(AFTER_WINDOW + GRACE, drop_grace=GRACE).to_state == KudosRetentionState.DROPPED
    with pytest.raises(ValueError, match="not kept detached"):
        reattach_partition(_catalog(db_session)[0].id)


def test_drop_grace_is_measured_with_the_rehearsal_reference_time(
    db_session: scoped_session[Session],
    current_week_partition: datetime,
) -> None:
    from horde.database.kudos_retention import RESTORED_PARTITION_REFUSAL, run_retention_tick

    rehearsal_partition = "kudos_stat_events_p_rehearsal"
    real_now = datetime.now(UTC).replace(tzinfo=None)
    as_of = real_now + timedelta(days=40)
    policy = _policy(drop_grace=GRACE)

    def rehearsal_state() -> KudosRetentionState:
        return next(unit.state for unit in _catalog(db_session) if unit.partition_name == rehearsal_partition)

    def tick_until_idle(now: datetime) -> None:
        for _ in range(20):
            result = run_retention_tick(now=now, policy=policy, dataset=STATISTICS, confirm_live_data_loss=True)
            if result.outcome == RetentionTickOutcome.IDLE:
                return
        raise AssertionError(f"no idle tick at {now}")

    try:
        tick_until_idle(as_of)
        rehearsal_unit = next(unit for unit in _catalog(db_session) if unit.partition_name == rehearsal_partition)
        assert (rehearsal_unit.state, rehearsal_unit.detached_at) == (KudosRetentionState.DETACHED, as_of)
        # A tick at the real time is earlier than the rehearsal detach, so the grace has not even started for it.
        assert run_retention_tick(now=real_now, policy=policy, dataset=STATISTICS).outcome == RetentionTickOutcome.IDLE
        tick_until_idle(as_of + GRACE - timedelta(minutes=1))
        assert rehearsal_state() == KudosRetentionState.DETACHED
        assert _relation_present(db_session, rehearsal_partition)
        # The rehearsal also drops the current week that partition maintenance created again under its weekly name,
        # and maintenance creates it once more. That table has dropped catalog units, so the dataset then blocks.
        results = [run_retention_tick(now=as_of + GRACE, policy=policy, dataset=STATISTICS, confirm_live_data_loss=True) for _ in range(20)]
        assert rehearsal_state() == KudosRetentionState.DROPPED
        assert not _relation_present(db_session, rehearsal_partition)
        assert results[-1].outcome == RetentionTickOutcome.BLOCKED
        assert results[-1].detail.startswith(RESTORED_PARTITION_REFUSAL)
    finally:
        # Partitions the rehearsal detached are no longer attached, so the fixture's teardown does not see them.
        db_session.rollback()
        for unit in _catalog(db_session):
            if unit.state == KudosRetentionState.DETACHED and not _attached(db_session, unit.partition_name):
                db_session.execute(text(f"DROP TABLE IF EXISTS {unit.partition_name}"))
        db_session.commit()


def test_recheckpoint_resets_a_reattached_currency_unit(db_session: scoped_session[Session]) -> None:
    from horde.classes.base.kudos import KudosRetentionUnit
    from horde.database.kudos_retention import recheckpoint_from_unit

    _seed_catalog_unit(db_session, CURRENCY, "kudos_ledger_p_20200106", MONDAY, MONDAY + WEEK, KudosRetentionState.BLOCKED)
    db_session.execute(
        update(KudosRetentionUnit).values(error=RetentionBlockReason.REATTACHED, state_before_detach=KudosRetentionState.VERIFIED),
    )
    db_session.commit()
    reset = recheckpoint_from_unit(_catalog(db_session)[0].id)
    assert [(reset_unit.state, reset_unit.error, reset_unit.state_before_detach) for reset_unit in reset] == [
        (KudosRetentionState.PENDING, None, None),
    ]


# Late rows found after the detach


def _ledger_rows_through_parent(db_session: scoped_session[Session], lower: datetime, upper: datetime) -> int:
    statement = text("SELECT count(*) FROM kudos_ledger WHERE created >= :lower AND created < :upper")
    return int(db_session.execute(statement, {"lower": lower, "upper": upper}).scalar_one())


@pytest.mark.parametrize("drop_grace", [timedelta(0), GRACE], ids=["without_grace", "with_grace"])
def test_late_row_after_the_detach_leaves_the_partition_reattachable(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    drop_grace: timedelta,
) -> None:
    from sqlalchemy.orm import Session as OrmSession

    from horde.classes.base.kudos import KudosLedgerCheckpoint
    from horde.database import kudos_retention
    from horde.database.kudos_retention import reattach_partition, recheckpoint_from_unit
    from horde.flask import db

    partition = make_partition("kudos_ledger", MONDAY, MONDAY + WEEK)
    user = make_user()
    user_id = user.id
    _seed_checkpoint(db_session, MONDAY, {user_id: ("10", "0")})
    db_session.add(_posting(MONDAY + timedelta(hours=1), user_id, "5"))
    db_session.commit()
    archive = _fake_archive(_FakeObjectStore())
    states = [_run(archive, drop_grace=drop_grace).to_state for _ in range(3)]
    assert states == [KudosRetentionState.CHECKPOINTED, KudosRetentionState.EXPORTED, KudosRetentionState.VERIFIED]

    def lock_holder_writing_a_late_row(context: Any, connection: Any) -> None:
        # Committed after the recount before the detach, so only the recount after the detach finds it.
        with OrmSession(db.engine) as writer:
            writer.add(_posting(MONDAY + timedelta(hours=2), user_id, "4"))
            writer.commit()

    monkeypatch.setattr(kudos_retention, "_long_lock_holder", lock_holder_writing_a_late_row)
    blocked = _run(archive, drop_grace=drop_grace)
    monkeypatch.undo()
    assert blocked.outcome == RetentionTickOutcome.BLOCKED
    assert blocked.detail.endswith("reattach it with retention reattach before unblocking")
    unit = _unit_of(db_session, partition)
    assert (unit.state, unit.error, unit.detached_at, unit.state_before_detach) == (
        KudosRetentionState.BLOCKED,
        RetentionBlockReason.LATE_ROWS,
        AFTER_WINDOW,
        KudosRetentionState.VERIFIED,
    )
    assert not _attached(db_session, partition)
    assert _relation_present(db_session, partition)
    # The blocked unit stops the currency dataset, so the detached table is not dropped on a later pass either.
    assert _run(archive, drop_grace=drop_grace).outcome == RetentionTickOutcome.IDLE
    assert _relation_present(db_session, partition)

    reattached = reattach_partition(unit.id)
    assert [
        (reattached_unit.state, reattached_unit.error, reattached_unit.detached_at, reattached_unit.state_before_detach)
        for reattached_unit in reattached
    ] == [(KudosRetentionState.BLOCKED, RetentionBlockReason.LATE_ROWS, None, None)]
    assert _attached(db_session, partition)
    assert _ledger_rows_through_parent(db_session, MONDAY, MONDAY + WEEK) == 2
    db_session.commit()

    assert [reset_unit.state for reset_unit in recheckpoint_from_unit(unit.id)] == [KudosRetentionState.PENDING]
    _run_until(AFTER_WINDOW, archive, lambda result: result.outcome == RetentionTickOutcome.IDLE, drop_grace=drop_grace)
    if drop_grace:
        assert _unit_of(db_session, partition).state == KudosRetentionState.DETACHED
        assert _run(archive, AFTER_WINDOW + drop_grace, drop_grace=drop_grace).to_state == KudosRetentionState.DROPPED
    final = _unit_of(db_session, partition)
    assert (final.state, final.row_count) == (KudosRetentionState.DROPPED, 2)
    assert not _relation_present(db_session, partition)
    checkpoint = db_session.scalars(
        select(KudosLedgerCheckpoint).where(KudosLedgerCheckpoint.boundary == MONDAY + WEEK, KudosLedgerCheckpoint.user_id == user_id),
    ).one()
    assert checkpoint.balance_total == Decimal(10 + 5 + 4)


def test_late_row_in_a_detached_partition_blocks_its_only_unit_and_reattach_restores_it(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import reattach_partition, unblock_unit

    partition = _waive_one_statistics_week(db_session, make_partition, drop_grace=GRACE)
    assert _tick(AFTER_WINDOW, drop_grace=GRACE).to_state == KudosRetentionState.DETACHED
    # A copy of the row with a new id and event, written to the standalone table while it waits out the grace.
    db_session.execute(
        text(
            f"INSERT INTO {partition} SELECT (jsonb_populate_record(source, "
            "jsonb_build_object('id', source.id + 1000000, 'event_id', gen_random_uuid()))).* "
            f"FROM {partition} AS source",
        ),
    )
    db_session.commit()
    blocked = _tick(AFTER_WINDOW + GRACE, drop_grace=GRACE)
    assert blocked.outcome == RetentionTickOutcome.BLOCKED
    assert blocked.detail.endswith("reattach it with retention reattach before unblocking")
    unit = _catalog(db_session)[0]
    assert (unit.state, unit.error, unit.detached_at) == (KudosRetentionState.BLOCKED, RetentionBlockReason.LATE_ROWS, AFTER_WINDOW)
    assert _relation_present(db_session, partition)
    with pytest.raises(ValueError, match="Reattach it first"):
        unblock_unit(unit.id)

    reattached = reattach_partition(unit.id)
    assert [(reattached_unit.state, reattached_unit.error, reattached_unit.detached_at) for reattached_unit in reattached] == [
        (KudosRetentionState.BLOCKED, RetentionBlockReason.LATE_ROWS, None),
    ]
    assert _attached(db_session, partition)
    assert _rows_through_parent(db_session, MONDAY, MONDAY + WEEK) == 2
    db_session.commit()
    restored = unblock_unit(unit.id)
    assert (restored.state, restored.error, restored.state_before_detach) == (KudosRetentionState.PENDING, None, None)
    assert _tick(AFTER_WINDOW + GRACE, drop_grace=GRACE).to_state == KudosRetentionState.ROLLED_UP
    assert _catalog(db_session)[0].row_count == 2


def _pooled_lock_timeouts() -> set[str]:
    """Return the ``lock_timeout`` of every connection the pool holds, checking them all out at once."""
    from horde.flask import db

    connections = [db.engine.connect() for _ in range(max(db.engine.pool.checkedin(), 1))]  # type: ignore[attr-defined]
    try:
        return {str(connection.execute(text("SHOW lock_timeout")).scalar_one()) for connection in connections}
    finally:
        for connection in connections:
            connection.close()


def test_drop_connection_lock_timeout_does_not_reach_the_pool(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from horde.database import kudos_retention

    partition = _waive_one_statistics_week(db_session, make_partition)

    monkeypatch.setattr(kudos_retention, "_long_lock_holder", lambda context, connection: "pid 0 holds a lock")
    assert _tick(AFTER_WINDOW).outcome == RetentionTickOutcome.DEFERRED
    assert DROP_LOCK_TIMEOUT not in _pooled_lock_timeouts()

    def lose_the_lock_connection(context: Any, connection: Any) -> None:
        context.lock_connection.invalidate()

    monkeypatch.setattr(kudos_retention, "_long_lock_holder", lose_the_lock_connection)
    failed = _tick(AFTER_WINDOW)
    assert (failed.outcome, failed.detail.startswith("retention lock connection lost")) == (RetentionTickOutcome.FAILED, True)
    assert DROP_LOCK_TIMEOUT not in _pooled_lock_timeouts()

    monkeypatch.undo()
    assert _tick(AFTER_WINDOW).to_state == KudosRetentionState.DROPPED
    assert not _relation_present(db_session, partition)
    assert DROP_LOCK_TIMEOUT not in _pooled_lock_timeouts()


# Operator eviction and the archive audit


def _store_statistics_object(archive: Any, store: _FakeObjectStore, source_range: str, payload: bytes) -> str:
    key = archive.object_key(tier=ArchiveTier.STATISTICS, source_range=source_range)
    store.objects[key] = payload
    return key


def test_eviction_deletes_batch_by_batch_and_reports_the_keys_deleted_before_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import ClientError

    from horde import kudos_archive
    from horde.kudos_archive import ArchiveEvictionError, manifest_key

    # One object and its manifest per delete request.
    monkeypatch.setattr(kudos_archive, "ARCHIVE_DELETE_BATCH_SIZE", 2)
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    keys = [
        _store_statistics_object(archive, store, f"kudos_stat_events_p_2020010{day}/2020010{day}T000000_2020010{day + 1}T000000", b"x" * 10)
        for day in (1, 2, 3)
    ]
    for key in keys:
        store.objects[manifest_key(key)] = b"{}"
    delete_calls = 0
    original_delete = store.delete_objects

    def delete_failing_on_the_second_request(**kwargs: Any) -> dict[str, Any]:
        nonlocal delete_calls
        delete_calls += 1
        if delete_calls == 2:
            raise ClientError({"Error": {"Code": "InternalError", "Message": "simulated"}}, "DeleteObjects")
        return original_delete(**kwargs)

    monkeypatch.setattr(store, "delete_objects", delete_failing_on_the_second_request)
    with pytest.raises(ArchiveEvictionError) as raised:
        archive.enforce_budget(budget_bytes=0)
    assert raised.value.deleted_keys == [keys[0]]
    assert keys[0] not in store.objects
    assert keys[1] in store.objects

    # A delete the store reports as failed keeps that object out of the deleted keys.
    def delete_reporting_an_error(**kwargs: Any) -> dict[str, Any]:
        original_delete(**kwargs)
        return {"Errors": [{"Key": keys[1], "Code": "AccessDenied"}]}

    monkeypatch.setattr(store, "delete_objects", delete_reporting_an_error)
    with pytest.raises(ArchiveEvictionError) as reported:
        archive.enforce_budget(budget_bytes=0)
    assert reported.value.deleted_keys == []

    monkeypatch.setattr(store, "delete_objects", original_delete)
    store.objects[keys[1]] = b"x" * 10
    # The budget covers the statistics tier alone: the newest object fits and stays.
    assert archive.enforce_budget(budget_bytes=10) == [keys[1]]
    assert keys[2] in store.objects


def test_version_usage_counts_evicted_objects_as_noncurrent_bytes_and_delete_markers() -> None:
    from horde.kudos_archive import ArchiveVersionUsage, manifest_key

    store = _FakeObjectStore()
    store.versioned = True
    archive = _fake_archive(store)
    keys = [
        _store_statistics_object(archive, store, f"kudos_stat_events_p_2020010{day}/2020010{day}T000000_2020010{day + 1}T000000", b"x" * 10)
        for day in (1, 2, 3)
    ]
    for key in keys:
        store.objects[manifest_key(key)] = b"{}"
    assert archive.version_usage(ArchiveTier.STATISTICS) == ArchiveVersionUsage(noncurrent_bytes=0, delete_markers=0)

    assert archive.enforce_budget(budget_bytes=10) == keys[:2]
    # The current listing omits the evicted objects while their versions still hold storage.
    assert archive.tier_bytes(ArchiveTier.STATISTICS) == 10
    assert archive.version_usage(ArchiveTier.STATISTICS) == ArchiveVersionUsage(noncurrent_bytes=2 * 10 + 2 * 2, delete_markers=4)
    assert archive.version_usage(ArchiveTier.CURRENCY) == ArchiveVersionUsage(noncurrent_bytes=0, delete_markers=0)


def test_version_usage_is_unknown_when_the_store_does_not_list_versions() -> None:
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    _store_statistics_object(archive, store, "kudos_stat_events_p_20200101/20200101T000000_20200102T000000", b"x" * 10)
    assert archive.enforce_budget(budget_bytes=0) != []
    assert archive.version_usage(ArchiveTier.STATISTICS) is None
    assert archive.version_usage(ArchiveTier.CURRENCY) is None


def test_archive_audit_reports_matching_corrupted_and_unmanifested_objects_and_missing_catalog_objects(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
) -> None:
    from horde.database.kudos_retention import MissingArchiveObject, audit_archive
    from horde.kudos_archive import manifest_key

    partition = make_partition("kudos_stat_events", MONDAY, MONDAY + WEEK)
    lost_partition = make_partition("kudos_stat_events", MONDAY + WEEK, MONDAY + 2 * WEEK)
    db_session.add_all([_stat_event(MONDAY + timedelta(hours=1), "2"), _stat_event(MONDAY + WEEK + timedelta(hours=1), "3")])
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    states = [_run(archive, hold_drops=True).to_state for _ in range(4)]
    assert states == [KudosRetentionState.ROLLED_UP, KudosRetentionState.DUMPED] * 2
    good_key = _unit_of(db_session, partition).object_key
    lost_key = _unit_of(db_session, lost_partition).object_key
    stored = store.objects[good_key]
    corrupted_key = _store_statistics_object(
        archive, store, "kudos_stat_events_p_corrupt/20200106T000000_20200113T000000", stored[:-1] + bytes([stored[-1] ^ 0xFF])
    )
    store.objects[manifest_key(corrupted_key)] = store.objects[manifest_key(good_key)]
    unmanifested_key = _store_statistics_object(archive, store, "kudos_stat_events_p_nomanifest/20200106T000000_20200113T000000", stored)
    del store.objects[lost_key]
    del store.objects[manifest_key(lost_key)]
    # An exported currency unit without a key lost its object, because currency objects are never evicted.
    _seed_catalog_unit(db_session, CURRENCY, "kudos_ledger_p_20200106", MONDAY, MONDAY + WEEK, KudosRetentionState.DROPPED)

    records = list(audit_archive(archive))
    audits = {record.key: record for record in records if not isinstance(record, MissingArchiveObject)}
    assert list(audits) == [good_key, corrupted_key, unmanifested_key]
    assert audits[good_key].matches_manifest
    assert audits[good_key].row_count == 1
    assert not audits[corrupted_key].matches_manifest
    assert audits[unmanifested_key].problems[0].startswith("manifest unreadable")
    missing = [record for record in records if isinstance(record, MissingArchiveObject)]
    assert [(record.source_range.split("/")[0], record.object_key) for record in missing] == [
        (lost_partition, lost_key),
        ("kudos_ledger_p_20200106", None),
    ]
    # The audit changes nothing, so the lost key is still recorded.
    assert _unit_of(db_session, lost_partition).object_key == lost_key

    resumed = [
        record.key
        for record in audit_archive(archive, tiers=[ArchiveTier.STATISTICS], since_key=good_key)
        if not isinstance(record, MissingArchiveObject)
    ]
    assert resumed == [corrupted_key, unmanifested_key]
    currency_only = list(audit_archive(archive, tiers=[ArchiveTier.CURRENCY]))
    assert [(record.dataset, record.object_key) for record in currency_only if isinstance(record, MissingArchiveObject)] == [
        (CURRENCY, None)
    ]


@pytest.mark.parametrize("missing", ["manifest", "unavailable", "size"])
def test_currency_drop_requires_readable_matching_archive_metadata(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    from horde.kudos_archive import KudosArchiveError, manifest_key

    partition, store, archive = _verified_currency_week(db_session, make_partition, make_user)
    key = _unit_of(db_session, partition).object_key
    if missing == "manifest":
        del store.objects[manifest_key(key)]
    elif missing == "size":
        store.objects[key] += b"changed"
    else:

        def unavailable(key: str) -> None:
            raise KudosArchiveError("store unreachable")

        monkeypatch.setattr(archive, "object_size", unavailable)
    result = _run(archive)
    assert result.outcome in (RetentionTickOutcome.FAILED, RetentionTickOutcome.BLOCKED)
    assert _attached(db_session, partition)
    assert _relation_present(db_session, partition)


@pytest.mark.parametrize("dataset", [CURRENCY, STATISTICS])
def test_underestimated_upload_blocks_without_publishing_or_deleting_existing_object(
    db_session: scoped_session[Session],
    make_partition: Callable[..., str],
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
    dataset: KudosRetentionDataset,
) -> None:
    from horde.database import kudos_retention

    parent = "kudos_ledger" if dataset == CURRENCY else "kudos_stat_events"
    partition = make_partition(parent, MONDAY, MONDAY + WEEK)
    if dataset == CURRENCY:
        user = make_user()
        _seed_checkpoint(db_session, MONDAY, {user.id: ("0", "0")})
        db_session.add(_posting(MONDAY + timedelta(hours=1), user.id, "5"))
    else:
        db_session.add(_stat_event(MONDAY + timedelta(hours=1), "5"))
    db_session.commit()
    store = _FakeObjectStore()
    archive = _fake_archive(store)
    _run(archive)
    source_range = format_source_range(partition_name=partition, lower=MONDAY, upper=MONDAY + WEEK)
    tier = ArchiveTier.CURRENCY if dataset == CURRENCY else ArchiveTier.STATISTICS
    key = archive.object_key(tier=tier, source_range=source_range)
    store.objects[key] = b"old"
    monkeypatch.setattr(kudos_retention, "_estimated_archive_bytes", lambda context: 0)
    policy = {"currency_budget_bytes" if dataset == CURRENCY else "statistics_budget_bytes": 1}
    result = _run(archive, **policy)
    assert result.outcome == RetentionTickOutcome.BLOCKED
    assert _unit_of(db_session, partition).error == f"{dataset} tier would exceed budget"
    assert store.objects[key] == b"old"
    assert store.uploads == {}
    assert _attached(db_session, partition)
