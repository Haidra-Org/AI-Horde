# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operational controls for the kudos ledger cutover, reconciliation, retention and archive."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime

# This operational CLI runs per-invocation inside the app container (docker exec),
# inheriting the OTLP endpoint env var. Disabling the OTEL SDK before importing
# horde keeps these short-lived cutover/reconciliation commands from paying
# exporter setup and blocking on shutdown-flush retries against telemetry
# backends. setdefault leaves OTEL_SDK_DISABLED as the operator override, so
# exporting it =false before invoking re-enables telemetry. Must precede the
# horde imports: horde.flask imports horde.telemetry at import time and metric
# objects initialize when horde.metrics is imported.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from horde.classes.base.kudos import (  # noqa: E402
    get_kudos_ledger_mode,
    set_kudos_ledger_mode,
)
from horde.database.kudos_ledger import apply_pending_kudos, kudos_applier_health  # noqa: E402
from horde.database.kudos_reconciliation import (  # noqa: E402
    StaleCheckpointsError,
    create_balance_snapshot,
    reconcile_balances,
    stale_checkpoint_units,
)
from horde.database.kudos_retention import (  # noqa: E402
    KUDOS_RETENTION_POLICY,
    MissingArchiveObject,
    RetentionLockBusyError,
    audit_archive,
    daily_statement,
    load_catalog,
    operator_retention_lock,
    parse_rehearsal_as_of,
    reattach_partition,
    recheckpoint_from_unit,
    restore_archive_unit,
    retention_health,
    run_retention_tick,
    unblock_unit,
    waive_unit_dump,
)
from horde.enums import KudosLedgerMode, KudosRetentionDataset, KudosRetentionState  # noqa: E402
from horde.flask import create_app  # noqa: E402
from horde.kudos_archive import ArchiveEvictionError, ArchiveTier, KudosArchive, KudosArchiveError  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    mode = commands.add_parser("mode")
    mode.add_argument("value", choices=[str(item) for item in KudosLedgerMode])
    commands.add_parser("snapshot")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("snapshot_id", type=uuid.UUID)
    reconcile.add_argument("--apply", action="store_true", help="Emit compensating postings after reporting drift")
    drain = commands.add_parser("drain")
    drain.add_argument("--max-cycles", type=int, default=10000)
    retention = commands.add_parser("retention", help="Inspect and drive kudos event retention (ADR 42)")
    retention_commands = retention.add_subparsers(dest="retention_command", required=True)
    retention_commands.add_parser("status", help="Report retention health and every unit not yet dropped")
    step = retention_commands.add_parser("step", help="Run one retention tick, as the scheduler does")
    step.add_argument(
        "--dataset",
        choices=[str(item) for item in KudosRetentionDataset],
        help="Consider only this dataset's units; partitions are still created and keys still expire for both",
    )
    step.add_argument(
        "--as-of",
        dest="as_of",
        help=(
            "Run the tick as though it were this ISO-8601 time (UTC when no offset is given), which must not be "
            "earlier than the current time nor later than the current time plus the longer window plus four partition "
            "widths. It exists to rehearse partition removal on a restored copy: every "
            "eligibility, key-expiry and partition-creation decision moves to that time, so partitions are created up "
            "to as-of plus two partition widths and idempotency keys expire relative to as-of. Against a live "
            "production database it removes data early, so a later time than now also needs "
            "--confirm-live-data-loss. Requires KUDOS_RETENTION_ENABLED=true. With KUDOS_RETENTION_DROP_GRACE_DAYS set, "
            "a detached partition is dropped only by a tick whose as-of is at least the grace past the detaching tick's."
        ),
    )
    step.add_argument(
        "--confirm-live-data-loss",
        dest="confirm_live_data_loss",
        action="store_true",
        help=(
            "Accept an --as-of later than the current time. Use it only on a restored copy: it makes the partitions "
            "that still receive rows eligible for removal."
        ),
    )
    unblock = retention_commands.add_parser(
        "unblock",
        help="Return a blocked unit to pending, or to checkpointed when its currency checkpoint is committed",
    )
    unblock.add_argument("unit_id", type=int)
    unblock_mode = unblock.add_mutually_exclusive_group()
    unblock_mode.add_argument(
        "--waive-dump",
        dest="waive_dump",
        action="store_true",
        help=(
            "Instead, accept dropping a statistics unit without its archive dump. Applies to a dump_failed unit or "
            "one blocked for dump retries exhausted. The raw rows are then lost and only the rollups remain."
        ),
    )
    unblock_mode.add_argument(
        "--recheckpoint",
        dest="recheckpoint",
        action="store_true",
        help=(
            "Instead, return a blocked currency unit and every later currency unit to pending, so their checkpoints, "
            "daily statements and row counts are recomputed in order. Refused when a later currency unit is dropped."
        ),
    )
    reattach = retention_commands.add_parser(
        "reattach",
        help=(
            "Attach a partition kept detached for the drop grace period back to its parent and return its units to the "
            "state they had before the detach. Set KUDOS_RETENTION_HOLD_DROPS=true first, or the next tick detaches it again"
        ),
    )
    reattach.add_argument("unit_id", type=int, help="The catalog id of any unit of the partition")
    statement = commands.add_parser("statement", help="Report a user's archived daily statement")
    statement.add_argument("user_id", type=int)
    statement.add_argument("--from", dest="first_day", type=date.fromisoformat, required=True, help="First UTC day, YYYY-MM-DD")
    statement.add_argument("--to", dest="last_day", type=date.fromisoformat, required=True, help="Last UTC day, YYYY-MM-DD")
    archive = commands.add_parser("archive", help="Read the kudos archive")
    archive_commands = archive.add_subparsers(dest="archive_command", required=True)
    restore = archive_commands.add_parser("restore", help="Copy an archive object into a scratch table")
    restore.add_argument("key")
    restore.add_argument("--into", dest="table_name", required=True, help="Scratch table, created by the restore")
    restore.add_argument(
        "--append",
        action="store_true",
        help="Add the rows to an existing table. A repeated restore into the same table then holds every row twice",
    )
    evict = archive_commands.add_parser(
        "evict",
        help="Delete the oldest statistics dumps until the tier fits a byte budget. Currency objects are never evicted",
    )
    evict.add_argument("--tier", required=True, choices=[str(ArchiveTier.STATISTICS)])
    evict.add_argument("--budget-bytes", dest="budget_bytes", type=int, required=True)
    audit = archive_commands.add_parser(
        "audit",
        help=(
            "Read every archive object back against its manifest and compare the catalog with the listing. Prints one "
            "JSON line per object and per missing object, then a summary, and exits 1 on any problem"
        ),
    )
    audit.add_argument("--tier", choices=[str(tier) for tier in ArchiveTier], help="Audit one tier (default: both)")
    audit.add_argument("--since-key", dest="since_key", help="Audit only objects whose key sorts after this one")
    return parser


def _archive_command(options: argparse.Namespace, archive_client: KudosArchive) -> None:
    """Run one ``archive`` subcommand and print its output."""
    if options.archive_command == "restore":
        try:
            restored = restore_archive_unit(archive_client, options.key, table_name=options.table_name, append=options.append)
        except ValueError as err:
            raise SystemExit(str(err)) from err
        restore_output: dict[str, object] = {"key": options.key, "table": options.table_name, "rows": restored, "appended": options.append}
        if options.append:
            restore_output["note"] = "The rows were added to the table. Rows it held before are kept, so compare counts by hand"
        print(json.dumps(restore_output))
        return
    if options.archive_command == "evict":
        try:
            # Eviction changes the tier size an export measures its allowance from, so it runs under the same lock.
            with operator_retention_lock():
                deleted = archive_client.enforce_budget(budget_bytes=options.budget_bytes)
        except RetentionLockBusyError as err:
            raise SystemExit(str(err)) from err
        except ArchiveEvictionError as err:
            print(json.dumps({"tier": options.tier, "budget_bytes": options.budget_bytes, "deleted": err.deleted_keys, "error": str(err)}))
            raise SystemExit(1) from err
        print(json.dumps({"tier": options.tier, "budget_bytes": options.budget_bytes, "deleted": deleted}))
        return
    tiers = list(ArchiveTier) if options.tier is None else [ArchiveTier(options.tier)]
    audited_objects = 0
    problems = 0
    last_key = None
    for record in audit_archive(archive_client, tiers=tiers, since_key=options.since_key):
        if isinstance(record, MissingArchiveObject):
            problems += 1
            print(json.dumps({"kind": "missing_object", **asdict(record)}, default=str))
            continue
        audited_objects += 1
        last_key = record.key
        if not record.matches_manifest:
            problems += 1
        print(json.dumps({"kind": "object", "matches_manifest": record.matches_manifest, **asdict(record)}, default=str))
    summary = {"objects": audited_objects, "problems": problems, "last_key": last_key, "tiers": [str(tier) for tier in tiers]}
    print(json.dumps({"kind": "summary", **summary}))
    if problems:
        raise SystemExit(1)


def _tier_bytes_or_error(archive: KudosArchive) -> dict[str, object]:
    """Return each tier's archived bytes, or the listing error text, so status stays readable when the store is down."""
    sizes: dict[str, object] = {}
    for tier in ArchiveTier:
        try:
            sizes[str(tier)] = archive.tier_bytes(tier)
        except KudosArchiveError as error:
            sizes[str(tier)] = {"error": str(error)}
    return sizes


def _retention_command(options: argparse.Namespace) -> dict[str, object]:
    """Run one ``retention`` subcommand and return its JSON document."""
    if options.retention_command == "status":
        archive = KudosArchive.from_environment()
        units = [unit for unit in load_catalog() if unit.state != KudosRetentionState.DROPPED]
        return {
            **retention_health(),
            "archive_configured": archive is not None,
            "archive_tier_bytes": None if archive is None else _tier_bytes_or_error(archive),
            "units": [{**asdict(unit), "source_range": unit.source_range} for unit in units],
        }
    if options.retention_command == "step":
        dataset = None if options.dataset is None else KudosRetentionDataset(options.dataset)
        real_now = datetime.now(UTC).replace(tzinfo=None)
        as_of = real_now
        if options.as_of is not None:
            try:
                as_of = parse_rehearsal_as_of(options.as_of, policy=KUDOS_RETENTION_POLICY, real_now=real_now)
            except ValueError as err:
                raise SystemExit(str(err)) from err
        result = run_retention_tick(
            now=as_of,
            archive=KudosArchive.from_environment(),
            dataset=dataset,
            confirm_live_data_loss=options.confirm_live_data_loss,
        )
        return {**asdict(result), "as_of": as_of, "rehearsal": options.as_of is not None}
    try:
        if options.retention_command == "reattach":
            return {"reattached": [asdict(unit) for unit in reattach_partition(options.unit_id)]}
        if options.waive_dump:
            return asdict(waive_unit_dump(options.unit_id))
        if options.recheckpoint:
            return {"reset": [asdict(unit) for unit in recheckpoint_from_unit(options.unit_id)]}
        return asdict(unblock_unit(options.unit_id))
    except (ValueError, RetentionLockBusyError) as err:
        raise SystemExit(str(err)) from err


def main() -> None:
    """Run the selected ledger administration command.

    ``retention step`` can drop an event partition whose replacement is committed; every other command leaves event
    rows in place.
    """
    options = _parser().parse_args()
    app = create_app()
    with app.app_context():
        if options.command == "status":
            print(
                json.dumps(
                    {
                        "mode": str(get_kudos_ledger_mode()),
                        **kudos_applier_health(),
                    },
                    indent=2,
                ),
            )
            return
        if options.command == "mode":
            set_kudos_ledger_mode(KudosLedgerMode(options.value))
            print(json.dumps({"mode": options.value}))
            return
        if options.command == "snapshot":
            try:
                snapshot_id = create_balance_snapshot()
            except (StaleCheckpointsError, RetentionLockBusyError) as err:
                raise SystemExit(str(err)) from err
            print(json.dumps({"snapshot_id": str(snapshot_id)}))
            return
        if options.command == "reconcile":
            try:
                drifts = reconcile_balances(options.snapshot_id, apply_repairs=options.apply)
            except StaleCheckpointsError as err:
                raise SystemExit(str(err)) from err
            stale_units = [asdict(stale_unit) for stale_unit in stale_checkpoint_units()]
            print(
                json.dumps(
                    {
                        "drifts": [asdict(item) for item in drifts],
                        "repairs_emitted": options.apply,
                        "stale_checkpoint_units": stale_units,
                        "drift_reliable": not stale_units,
                    },
                    default=str,
                    indent=2,
                ),
            )
            return
        if options.command == "drain":
            total = 0
            for _ in range(options.max_cycles):
                folded = apply_pending_kudos()
                total += folded
                if folded == 0:
                    break
            print(json.dumps({"folded": total, **kudos_applier_health()}, indent=2))
            return
        if options.command == "retention":
            print(json.dumps(_retention_command(options), default=str, indent=2))
            return
        if options.command == "statement":
            lines = daily_statement(user_id=options.user_id, first_day=options.first_day, last_day=options.last_day)
            print(json.dumps({"user_id": options.user_id, "lines": [asdict(line) for line in lines]}, default=str, indent=2))
            return
        if options.command == "archive":
            archive_client = KudosArchive.from_environment()
            if archive_client is None:
                raise SystemExit("The kudos archive is not configured; set every KUDOS_ARCHIVE_* variable")
            _archive_command(options, archive_client)


if __name__ == "__main__":
    main()
