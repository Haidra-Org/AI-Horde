---
title: "Kudos ledger operations"
summary: "Operator procedures for the kudos ledger: mode cutover, health checks, rollback, and recovery."
topics: [kudos, accounting, operations]
order: 10
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Kudos ledger operations

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [accounting](../topics.md#accounting), [kudos](../topics.md#kudos), [operations](../topics.md#operations)
<!-- END GENERATED: topics -->

Operator procedures for the architecture described in [Kudos accounting, projection, and
concurrency](../explanation/kudos_accounting.md). Exact schemas, mutation rules, and health fields are in the
[kudos accounting reference](../reference/kudos_accounting.md).

Preconditions for every procedure below: a PostgreSQL primary, the whole fleet running kudos-ledger code, and shell
access to a host that can run `tools/kudos_ledger_admin.py` against the production database. End state: `ledger` mode
active with the projector draining within its lag budget, or `shadow` mode restored with the final ledger tail
folded.

The kudos ledger has two online modes:

- `shadow`: inline balances and counters are authoritative; matching ledger rows are retained as already-applied
  audit history.
- `ledger`: request transactions append postings and the database-serialized projector materializes them
  asynchronously.

Fresh installations and the migration SQL default to `shadow`; moving to `ledger` is always an explicit operator
action. Both currency postings and non-currency statistic events are permanent archives. The projector is serialized
by a PostgreSQL transaction advisory lock, independent of Redis quorum selection, and claims bounded batches with
`FOR UPDATE SKIP LOCKED`.

## Code and schema boundaries

Accounting tables, their foreign-key lifetimes, and the typed enums are specified in the reference
[data model](../reference/kudos_accounting.md#data-model). Two boundaries matter while operating the system:

- Currency history cannot be orphaned. `kudos_ledger.user_id` is `ON DELETE RESTRICT`, so a user wipe that would
  strand postings fails rather than deleting them. `kudos_stat_events` carries immutable audit IDs instead of
  ownership foreign keys, so hard-deleting a worker or team leaves its counter history intact.
- Mode-specific behavior lives in `horde/database/kudos_legacy_projection.py`; business methods emit the same events
  in both modes. Advisory locks and repeatable-read setup are confined to `horde/database/kudos_db.py`, and counter
  upserts to `horde/database/kudos_counters.py`. A final cutover deletes the compatibility module and its call sites
  without rewriting the accounting flow.

## Pre-cutover proof

1. Deploy the ledger code and schema to the whole fleet in `shadow` mode. Do not mix it with code that does not write
   the audit rows. Verify: `uv run python tools/kudos_ledger_admin.py status` reports `"mode": "shadow"` and a
   non-null `heartbeat_seconds`, which shows the projector process is running.
2. Run through a representative peak-load window. Exercise transfers, upfront image/text/interrogation admission,
   cancellations, trust promotion, monthly awards, and admin adjustments. Verify: `kudos_ledger` and
   `kudos_stat_events` both gain rows for each exercised path, and every row carries `applied = true`, which is what
   shadow mode writes.
3. Inspect `uv run python tools/kudos_ledger_admin.py status`. Investigate any non-zero pending queue, applier
   heartbeat gap, or `oldest_pending_seconds` above 30 seconds before continuing.
4. Capture a transaction-consistent baseline with `uv run python tools/kudos_ledger_admin.py snapshot`. Verify: the
   command prints `snapshot_id`; use that value as `<snapshot-id>` below.
5. Run `uv run python tools/kudos_ledger_admin.py reconcile <snapshot-id>`. Verify: the `drifts` array is empty.
   Investigate every drift entry before continuing.
6. Switch with `uv run python tools/kudos_ledger_admin.py mode ledger`. The exclusive mode-gate advisory lock waits
   for every transaction that observed shadow mode before ownership changes, so no service freeze is required.
   Verify: `status` reports `"mode": "ledger"`, and `pending_rows` rises and then falls as the projector folds.
   Reverse with [online rollback](#online-rollback).

Monitor pending row count, oldest pending age, heartbeat age, database deadlocks, reservation age and count,
transfer rejection rate, and balance-floor adjustments throughout rollout, using the quorum node's applier
telemetry (`horde.kudos.*` metrics) or `tools/kudos_ledger_admin.py status`. The per-node
`/api/v2/status/heartbeat` response reports only node-local health; it deliberately carries no applier-queue
signal, since load balancer health checks consume it and a shared-database signal would fail every node at once.

## Online rollback

Keep ledger mode active and pre-drain with `uv run python tools/kudos_ledger_admin.py drain` until `pending_rows` is
near zero. Reconcile against the latest baseline, then run `uv run python tools/kudos_ledger_admin.py mode shadow`.
Verify: `status` reports `"mode": "shadow"` and `pending_rows` is zero, since the transition folds whatever tail the
pre-drain left.

The transition takes the applier advisory lock followed by the exclusive mode-gate advisory lock, waits for every
mutation that observed ledger mode to commit, and folds the final tail in the same transaction before changing
ownership. `set_kudos_ledger_mode` uses the same lock order as the projector, so an applier/mode-gate deadlock cannot
form. Active upfront reservations can span the transition because shadow-mode debits consume the same holds inline.
Rolling forward again means repeating the [pre-cutover proof](#pre-cutover-proof). Never roll directly back to code
that does not understand reservations and shadow audit rows.

## Recovery and repair

`reconcile <snapshot-id>` is read-only and compares the materialized balances with the snapshot plus all subsequently
applied currency postings. Minimum-balance forgiveness is recorded as an explicit `FLOOR_ADJUSTMENT`, so replay
remains exact across separate batches. `reconcile <snapshot-id> --apply` never overwrites a balance or old history:
it serializes repair runs and emits one deterministic `RECONCILIATION` posting per affected user. Re-running it
before or after projection cannot duplicate a repair, so a repair has no reversal step and needs none. A repair that
was itself wrong is corrected by a further compensating posting.

If the projector stops, leave writers in ledger mode, restore the projector, and drain; unapplied rows are durable
and the database advisory lock prevents two replicas from applying them. Verify: `pending_rows` returns to zero and
`heartbeat_seconds` stays within the projector interval. If projection is corrupt, take a fresh snapshot for
evidence, reconcile against the last known-good baseline, review the complete drift list, apply compensating
postings, drain, and reconcile again. Verify: the second `reconcile` returns an empty `drifts` array. Do not edit
`applied`, delete postings, or directly overwrite balances; none of those can be undone, and they destroy the
evidence a later reconciliation needs.

Treat every new `horde.kudos.applier.quarantined` alert as an incident even when `pending_rows` is draining normally.
`quarantined_rows` is the total retained evidence, not an unresolved-incident gauge, so it remains non-zero after
review. Use the counter alert and newest `quarantined_at` value to identify new incidents. Inspect the retained
evidence before deciding on a repair:

```sql
SELECT id, event_id, created, quarantine_reason, entry_type,
       user_id, worker_id, worker_user_id, team_id,
       unit, stat_action, record, amount
FROM kudos_stat_events
WHERE quarantined
ORDER BY id;
```

Determine whether the producer emitted an invalid shape or a referenced user disappeared. Fix the producer/projector
first. Then repair any missing materialized counters with an explicit, reviewed compensating event or reconciliation
procedure appropriate to that counter. Retain the quarantined rows and their reason as evidence; do not merely clear
`quarantined` or set `applied`, because either action can duplicate a partially repaired business event or silently
discard it.

For database disaster recovery, restore PostgreSQL to the selected PITR/WAL point, retain the permanent ledger and
stat archives and the balance snapshots, start in shadow mode, reconcile, then repeat the cutover proof. Ledger
pruning is disabled: `prune_applied_kudos_ledger` returns zero without deleting anything.

## Automated drill coverage

`tests/unit/test_kudos_safety.py` covers concurrent-projector exclusion, reservation overspend prevention, transfer
idempotency, final-event trust promotion, an atomic ledger-to-shadow tail drain, snapshot drift detection,
idempotent compensating repair, and replay across floor adjustments. `tests/unit/test_wp_activate_deadlock.py`
covers bounded PostgreSQL deadlock retry behavior. Run both against PostgreSQL before every cutover or recovery
exercise.
