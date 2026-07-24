<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Kudos ledger operations

This is the operator procedure for the architecture described in [Kudos accounting, projection, and
concurrency](../explanation/kudos_accounting.md). Exact schemas, mutation rules, and health fields are in the
[kudos accounting reference](../reference/kudos_accounting.md).

The kudos ledger has two online modes:

- `shadow`: legacy inline balances and counters are authoritative; matching ledger rows are retained as already-applied audit history.
- `ledger`: request transactions append postings and the database-serialized projector materializes them asynchronously.

New installations and the migration SQL default to `shadow`; moving to `ledger` is always an explicit operator action. Both currency postings and non-currency statistic events are permanent archives. The projector is serialized by a PostgreSQL transaction advisory lock, independent of Redis quorum selection, and claims bounded batches with `FOR UPDATE SKIP LOCKED`.

## Code and schema boundaries

The new accounting models use SQLAlchemy 2.x typed mappings (`Mapped` and `mapped_column`) and database-portable `Uuid`, `Enum`, and JSON types. The SQLAlchemy mypy plugin checks the model attributes and constructors. Fixed units, projector record discriminators, aggregate names, and audit-detail keys are `StrEnum` values rather than string literals.

The currency ledger has one required `users.id` foreign key with `ON DELETE RESTRICT`; users are soft-deleted/wiped, and authoritative currency history must not become orphaned. `kudos_reservations.user_id` and `kudos_balance_snapshots.user_id` are ownership foreign keys with `ON DELETE CASCADE`: those operational rows only have meaning while the user exists. IDs in `kudos_stat_events` are intentionally immutable audit references rather than ownership foreign keys. Workers and teams can be hard-deleted, but counter history must survive; adding cascading foreign keys would destroy that history and restrictive foreign keys would break supported deletion. This exception is documented in the mapped model.

PostgreSQL advisory locks and repeatable-read setup are confined to `horde.database.kudos_db` and are expressed through SQLAlchemy functions/connection options, not textual execution. Counter upserts are confined to `horde.database.kudos_counters`. Shadow-mode inline projection is likewise confined to `horde.database.kudos_legacy_projection`; business methods always emit the new events. A final cutover removes that compatibility module and its direct calls without rewriting the accounting flow.

## Pre-cutover proof

1. Deploy the new code and schema to the full fleet in `shadow` mode. Do not mix it with code that does not write the audit rows.
2. Run through a representative peak-load window. Exercise transfers, upfront image/text/interrogation admission, cancellations, trust promotion, monthly awards, and admin adjustments.
3. Inspect `uv run python tools/kudos_ledger_admin.py status`. Investigate any non-zero old queue, applier heartbeat gap, or `oldest_pending_seconds` above 30 seconds.
4. Capture a transaction-consistent baseline with `uv run python tools/kudos_ledger_admin.py snapshot`.
5. Run `uv run python tools/kudos_ledger_admin.py reconcile <snapshot-id>`. It must report no unexplained drift.
6. Switch with `uv run python tools/kudos_ledger_admin.py mode ledger`. Control-row locks wait for every transaction that observed shadow mode before changing ownership; no service freeze is required.

Monitor pending row count, oldest pending age, heartbeat age, database deadlocks, reservation age/count, transfer rejection rate, and balance-floor adjustments throughout rollout. The `/api/v2/status/heartbeat` response exposes queue health and reports `DEGRADED` once the oldest pending event exceeds 30 seconds.

## Online rollback

Keep ledger mode active and pre-drain with `uv run python tools/kudos_ledger_admin.py drain` until `pending_rows` is near zero. Reconcile against the current baseline, then run `uv run python tools/kudos_ledger_admin.py mode shadow`.

The transition takes the database applier lock followed by an exclusive control-row lock, waits for every mutation that observed ledger mode to commit, and folds the final tail in the same transaction before changing ownership. This lock order is shared with the projector, avoiding an applier/control deadlock. Active upfront reservations can span the transition because shadow-mode debits consume the same holds inline. Never roll directly back to code that does not understand reservations and shadow audit rows.

## Recovery and repair

`reconcile <snapshot-id>` is read-only and compares the materialized balances with the snapshot plus all subsequently applied currency postings. Minimum-balance forgiveness is recorded as an explicit `FLOOR_ADJUSTMENT`, so replay remains exact across separate batches. `reconcile <snapshot-id> --apply` never overwrites a balance or old history: it serializes repair runs and emits one deterministic `RECONCILIATION` posting per affected user. Re-running it before or after projection cannot duplicate a repair.

If the projector stops, leave writers in ledger mode, restore the projector, and drain; unapplied rows are durable and the database advisory lock prevents two replicas from applying them. If projection is corrupt, take a fresh snapshot for evidence, reconcile against the last known-good baseline, review the complete drift list, apply compensating postings, drain, and reconcile again. Do not edit `applied`, delete postings, or directly overwrite balances.

For database disaster recovery, restore PostgreSQL to the selected PITR/WAL point, retain the permanent ledger/stat archives and balance snapshots, start in shadow mode, reconcile, then repeat the cutover proof. Ledger pruning is disabled.

## Automated drill coverage

`tests/unit/test_kudos_safety.py` demonstrates concurrent-projector exclusion, reservation overspend prevention, transfer idempotency, final-event trust promotion, an atomic ledger-to-shadow tail drain, snapshot drift detection, idempotent compensating repair, and replay across floor adjustments. `tests/unit/test_wp_activate_deadlock.py` preserves bounded PostgreSQL deadlock retry behavior. Run those tests against PostgreSQL before every cutover or recovery exercise.
