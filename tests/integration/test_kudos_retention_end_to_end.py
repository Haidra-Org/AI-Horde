# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retire the legacy event partitions end to end and keep reconciliation, idempotency and projection working (ADR 42).

One scenario runs against PostgreSQL and the S3-compatible archive bucket the test runtime provisions. Activity is
emitted through the production paths and moved 45 days into the past, so it lands in the legacy partitions. The
retention job then runs with a ``now`` just past the legacy upper bound plus the 30-day window, which makes the legacy
partitions the only eligible ones.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select, text

if TYPE_CHECKING:
    from flask import Flask

pytestmark = [pytest.mark.integration, pytest.mark.object_storage]

SETTLEMENT_KUDOS = 40
RAW_THINGS = 2_000_000
TRANSFER_AMOUNT = 25
STARTING_KUDOS = 1000
IDEMPOTENCY_KEY = "e2e-retention-transfer"
MAX_TICKS = 50


def _legacy_upper(partitions: list[Any]) -> datetime:
    from horde.enums import KudosRetentionDataset

    uppers = {partition.dataset: partition.upper for partition in partitions if partition.is_legacy}
    assert set(uppers) == set(KudosRetentionDataset), f"bootstrap legacy partitions missing: {uppers}"
    assert uppers[KudosRetentionDataset.CURRENCY] == uppers[KudosRetentionDataset.STATISTICS]
    return uppers[KudosRetentionDataset.CURRENCY]


def _backdate_recent_rows(*, since: datetime, created: datetime) -> None:
    """Move every event row written at or after ``since`` to ``created``; the update moves rows between partitions."""
    from horde.flask import db

    for table in ("kudos_ledger", "kudos_stat_events"):
        db.session.execute(text(f"UPDATE {table} SET created = :created WHERE created >= :since"), {"created": created, "since": since})
    db.session.commit()


def _legacy_currency_totals(legacy_upper: datetime) -> dict[int, tuple[Decimal, Decimal]]:
    from horde.flask import db

    rows = db.session.execute(
        text(
            "SELECT user_id, coalesce(sum(amount) FILTER (WHERE NOT escrow), 0), coalesce(sum(amount) FILTER (WHERE escrow), 0) "
            "FROM kudos_ledger WHERE applied AND entry_type <> 'RECONCILIATION' AND created < :upper GROUP BY user_id",
        ),
        {"upper": legacy_upper},
    ).all()
    return {int(row[0]): (Decimal(row[1]), Decimal(row[2])) for row in rows}


def _materialized(owner_id: int, requester_id: int, worker_id: uuid.UUID) -> dict[str, Any]:
    from horde.classes.base.user import User
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    db.session.expire_all()
    owner = db.session.get(User, owner_id)
    requester = db.session.get(User, requester_id)
    worker = db.session.get(WorkerTemplate, worker_id)
    assert owner is not None and requester is not None and worker is not None
    return {
        "owner_kudos": owner.kudos,
        "owner_evaluating_kudos": owner.evaluating_kudos,
        "requester_kudos": requester.kudos,
        "worker_kudos": worker.kudos,
        "worker_contributions": worker.contributions,
        "worker_fulfilments": worker.fulfilments,
    }


def test_legacy_partitions_retire_with_reconciliation_idempotency_and_projection_intact(
    app: Flask,
    make_api_user: Callable[..., Any],
    settle_kudos: Callable[[], int],
    object_store_ready: None,
) -> None:
    from horde.classes.base.kudos import KudosIdempotencyKey, KudosLedger, KudosStatEvent, kudos_event_id
    from horde.classes.base.user import User
    from horde.classes.base.worker import WorkerTemplate
    from horde.database.functions import transfer_kudos
    from horde.database.kudos_reconciliation import create_balance_snapshot, reconcile_balances
    from horde.database.kudos_retention import (
        DailyStatementLine,
        KudosRetentionPolicy,
        RetentionTickOutcome,
        daily_statement,
        discover_partitions,
        expire_idempotency_keys,
        load_catalog,
        restore_archive_unit,
        run_retention_tick,
    )
    from horde.enums import (
        KudosEntryType,
        KudosIdempotencyKind,
        KudosRetentionDataset,
        KudosRetentionState,
        KudosStatEventQuarantineReason,
        KudosUnit,
    )
    from horde.flask import db
    from horde.kudos_archive import ArchiveManifest, ArchiveTier, KudosArchive, manifest_key

    archive = KudosArchive.from_environment(os.environ)
    assert archive is not None, "the object-storage runtime did not configure KUDOS_ARCHIVE_*"
    owner_ref = make_api_user(kudos=STARTING_KUDOS)
    requester_ref = make_api_user(kudos=STARTING_KUDOS)

    with app.app_context():
        # a. Activity 45 days back, through the production emitters, plus a hand-written transfer and quarantined row.
        legacy_upper = _legacy_upper(discover_partitions())
        db.session.commit()
        real_now = datetime.utcnow()
        old = real_now - timedelta(days=45)
        assert old + timedelta(days=3) < legacy_upper
        owner = db.session.get(User, owner_ref.id)
        requester = db.session.get(User, requester_ref.id)
        assert owner is not None and requester is not None
        worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:8]}", user_id=owner.id)
        db.session.add(worker)
        db.session.commit()
        worker_id = worker.id
        _backdate_recent_rows(since=real_now - timedelta(hours=1), created=old)

        emitted_at = datetime.utcnow()
        worker.record_contribution(raw_things=RAW_THINGS, kudos=SETTLEMENT_KUDOS, things_per_sec=1)
        requester.record_usage(raw_things=RAW_THINGS, kudos=SETTLEMENT_KUDOS, usage_type="image")
        db.session.commit()
        _backdate_recent_rows(since=emitted_at, created=old + timedelta(days=1))

        transfer_event_id = kudos_event_id(f"transfer:{owner.id}:{IDEMPOTENCY_KEY}")
        transfer_created = old + timedelta(days=2)
        db.session.add_all(
            [
                KudosLedger(
                    created=transfer_created,
                    event_id=transfer_event_id,
                    entry_type=KudosEntryType.TRANSFER,
                    user_id=owner.id,
                    escrow=False,
                    amount=Decimal(-TRANSFER_AMOUNT),
                ),
                KudosLedger(
                    created=transfer_created,
                    event_id=transfer_event_id,
                    entry_type=KudosEntryType.TRANSFER,
                    user_id=requester.id,
                    escrow=False,
                    amount=Decimal(TRANSFER_AMOUNT),
                ),
                KudosIdempotencyKey(
                    event_id=transfer_event_id,
                    kind=KudosIdempotencyKind.TRANSFER,
                    source_user_id=owner.id,
                    dest_user_id=requester.id,
                    amount=Decimal(TRANSFER_AMOUNT),
                    outcome="OK",
                    created=transfer_created,
                ),
            ],
        )
        quarantined_event_id = uuid.uuid4()
        db.session.add(
            KudosStatEvent(
                created=transfer_created,
                event_id=quarantined_event_id,
                entry_type=KudosEntryType.STAT_RECORD,
                user_id=owner.id,
                amount=Decimal(1),
                unit=KudosUnit.COUNT,
                stat_action="image",
                record="request",
                applied=False,
                quarantined=True,
                quarantine_reason=next(iter(KudosStatEventQuarantineReason)),
                quarantined_at=transfer_created,
            ),
        )
        db.session.commit()

    settle_kudos()
    with app.app_context():
        worker = db.session.get(WorkerTemplate, worker_id)
        assert worker is not None
        worker.record_contribution(raw_things=RAW_THINGS, kudos=SETTLEMENT_KUDOS, things_per_sec=1)
        db.session.commit()
    settle_kudos()

    with app.app_context():
        unapplied = db.session.execute(text("SELECT count(*) FROM kudos_ledger WHERE NOT applied")).scalar_one()
        assert unapplied == 0
        before_drop = _materialized(owner_ref.id, requester_ref.id, worker_id)
        expected_checkpoint = _legacy_currency_totals(legacy_upper)
        assert set(expected_checkpoint) >= {owner_ref.id, requester_ref.id}
        legacy_currency_rows = int(
            db.session.execute(text("SELECT count(*) FROM kudos_ledger WHERE created < :upper"), {"upper": legacy_upper}).scalar_one(),
        )
        expected_rollups = {
            (row[0], row[1]): (Decimal(row[2]), int(row[3]))
            for row in db.session.execute(
                text(
                    "SELECT user_id, worker_id, sum(amount), count(*) FROM kudos_stat_events "
                    "WHERE applied AND NOT quarantined AND created < :upper GROUP BY user_id, worker_id",
                ),
                {"upper": legacy_upper},
            ).all()
        }
        assert any(worker_key == worker_id for _, worker_key in expected_rollups)
        expected_owner_lines = [
            DailyStatementLine(day=row[0], entry_type=str(row[1]), escrow=bool(row[2]), amount_sum=Decimal(row[3]), row_count=int(row[4]))
            for row in db.session.execute(
                text(
                    "SELECT CAST(created AS date), entry_type, escrow, sum(amount), count(*) FROM kudos_ledger "
                    "WHERE user_id = :user_id AND created < :upper GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
                ),
                {"user_id": owner_ref.id, "upper": legacy_upper},
            ).all()
        ]
        assert len({line.day for line in expected_owner_lines}) >= 2
        db.session.commit()

        # b. A snapshot taken while every row is online reconciles cleanly.
        snapshot_id = create_balance_snapshot()
        assert reconcile_balances(snapshot_id) == []

        # c. Retention retires both legacy partitions.
        for prefix in ("currency/kudos_ledger_p_legacy__", "statistics/kudos_stat_events_p_legacy__"):
            stale = [
                entry["Key"]
                for page in archive._client.get_paginator("list_objects_v2").paginate(Bucket=archive.bucket, Prefix=prefix)
                for entry in page.get("Contents", [])
            ]
            if stale:
                archive._client.delete_objects(Bucket=archive.bucket, Delete={"Objects": [{"Key": key} for key in stale]})
        policy = KudosRetentionPolicy(
            enabled=True,
            currency_window=timedelta(days=30),
            statistics_window=timedelta(days=30),
            partition_width=timedelta(days=7),
            archive_budget_bytes=1024 * 1024 * 1024,
        )
        tick_now = legacy_upper + timedelta(days=30, hours=1)
        outcomes: list[Any] = []
        for _ in range(MAX_TICKS):
            # The legacy upper bound is the current week's start, so the reference time is in the future. The test
            # database is the restored-copy case the confirmation exists for.
            result = run_retention_tick(now=tick_now, policy=policy, archive=archive, confirm_live_data_loss=True)
            outcomes.append(result)
            if result.outcome == RetentionTickOutcome.IDLE:
                break
            assert result.outcome == RetentionTickOutcome.STEPPED, result
        assert outcomes[-1].outcome == RetentionTickOutcome.IDLE, f"retention did not settle in {MAX_TICKS} ticks"

        assert not [partition.name for partition in discover_partitions() if partition.is_legacy]
        catalog = load_catalog()
        legacy_units = [unit for unit in catalog if unit.partition_name.endswith("_p_legacy")]
        assert {unit.dataset for unit in legacy_units} == set(KudosRetentionDataset)
        assert {unit.state for unit in legacy_units} == {KudosRetentionState.DROPPED}
        assert all(unit.upper <= legacy_upper for unit in catalog), "a weekly partition was retired too"

        rollups = {
            (row[0], row[1]): (Decimal(row[2]), int(row[3]))
            for row in db.session.execute(
                text("SELECT user_id, worker_id, sum(amount_sum), sum(row_count) FROM kudos_stat_rollups GROUP BY user_id, worker_id"),
            ).all()
        }
        assert rollups == expected_rollups
        quarantined = db.session.execute(
            text("SELECT event_id, source_range FROM kudos_stat_quarantine WHERE event_id = :event_id"),
            {"event_id": quarantined_event_id},
        ).all()
        assert len(quarantined) == 1
        assert quarantined[0][1].startswith("kudos_stat_events_p_legacy/")

        checkpoint = {
            int(row[0]): (Decimal(row[1]), Decimal(row[2]))
            for row in db.session.execute(
                text("SELECT user_id, balance_total, escrow_total FROM kudos_ledger_checkpoints WHERE boundary = :boundary"),
                {"boundary": legacy_upper},
            ).all()
        }
        assert checkpoint == expected_checkpoint
        daily_users = set(db.session.scalars(text("SELECT DISTINCT user_id FROM kudos_ledger_daily")).all())
        assert daily_users >= {owner_ref.id, requester_ref.id}

        currency_units = [unit for unit in legacy_units if unit.dataset == KudosRetentionDataset.CURRENCY]
        currency_keys = [archive.object_key(tier=ArchiveTier.CURRENCY, source_range=unit.source_range) for unit in currency_units]
        listed = {
            entry["Key"]
            for page in archive._client.get_paginator("list_objects_v2").paginate(
                Bucket=archive.bucket, Prefix="currency/kudos_ledger_p_legacy__"
            )
            for entry in page.get("Contents", [])
        }
        # The legacy partition is archived one sub-range per object, each with its manifest.
        assert listed == set(currency_keys) | {manifest_key(key) for key in currency_keys}
        manifests: list[ArchiveManifest] = [archive.get_manifest(key) for key in currency_keys]
        assert sum(manifest.row_count for manifest in manifests) == legacy_currency_rows
        assert archive.tier_bytes(ArchiveTier.CURRENCY) > 0
        assert db.session.execute(select(KudosIdempotencyKey).where(KudosIdempotencyKey.event_id == transfer_event_id)).first() is None
        db.session.commit()

        # d. Reconciliation, idempotency, statements and restore after the drop.
        assert reconcile_balances(snapshot_id) == []
        assert _materialized(owner_ref.id, requester_ref.id, worker_id) == before_drop

        # The tick's ``now`` aged the hand-written key out; a key still inside its lifetime replays as idempotent.
        db.session.add(
            KudosIdempotencyKey(
                event_id=transfer_event_id,
                kind=KudosIdempotencyKind.TRANSFER,
                source_user_id=owner_ref.id,
                dest_user_id=requester_ref.id,
                amount=Decimal(TRANSFER_AMOUNT),
                outcome="OK",
                created=real_now - timedelta(days=10),
            ),
        )
        db.session.commit()
        transfers_before = int(db.session.execute(text("SELECT count(*) FROM kudos_ledger WHERE entry_type = 'TRANSFER'")).scalar_one())
        owner = db.session.get(User, owner_ref.id)
        requester = db.session.get(User, requester_ref.id)
        assert owner is not None and requester is not None
        replay = transfer_kudos(owner, requester, TRANSFER_AMOUNT, idempotency_key=IDEMPOTENCY_KEY)
        assert replay == [TRANSFER_AMOUNT, "OK", None, True]
        assert (
            int(db.session.execute(text("SELECT count(*) FROM kudos_ledger WHERE entry_type = 'TRANSFER'")).scalar_one())
            == transfers_before
        )
        assert expire_idempotency_keys(before=datetime.utcnow(), batch_size=5000) >= 1
        fresh = transfer_kudos(owner, requester, TRANSFER_AMOUNT, idempotency_key=IDEMPOTENCY_KEY)
        assert fresh == [TRANSFER_AMOUNT, "OK", None, False]
        assert (
            int(db.session.execute(text("SELECT count(*) FROM kudos_ledger WHERE entry_type = 'TRANSFER'")).scalar_one())
            == transfers_before + 2
        )

        assert (
            daily_statement(user_id=owner_ref.id, first_day=old.date() - timedelta(days=1), last_day=legacy_upper.date())
            == expected_owner_lines
        )
        assert daily_statement(user_id=owner_ref.id, first_day=date(2000, 1, 1), last_day=old.date() - timedelta(days=1)) == []

        scratch = f"kudos_restore_{uuid.uuid4().hex[:8]}"
        try:
            restored = sum(restore_archive_unit(archive, key, table_name=scratch) for key in currency_keys)
            assert restored == legacy_currency_rows
        finally:
            db.session.rollback()
            db.session.execute(text(f"DROP TABLE IF EXISTS {scratch}"))
            db.session.commit()

        # e. The projector keeps folding into the remaining partitions.
        worker = db.session.get(WorkerTemplate, worker_id)
        assert worker is not None
        worker.record_contribution(raw_things=RAW_THINGS, kudos=SETTLEMENT_KUDOS, things_per_sec=1)
        db.session.commit()
    settle_kudos()
    with app.app_context():
        after = _materialized(owner_ref.id, requester_ref.id, worker_id)
        assert after["worker_kudos"] == before_drop["worker_kudos"] + SETTLEMENT_KUDOS
        assert after["worker_contributions"] > before_drop["worker_contributions"]
        owner_total_before = before_drop["owner_kudos"] + before_drop["owner_evaluating_kudos"] - TRANSFER_AMOUNT
        assert after["owner_kudos"] + after["owner_evaluating_kudos"] > owner_total_before
        assert after["requester_kudos"] == before_drop["requester_kudos"] + TRANSFER_AMOUNT
        assert db.session.execute(text("SELECT count(*) FROM kudos_ledger WHERE NOT applied")).scalar_one() == 0
