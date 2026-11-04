---
title: "Kudos ledger operations"
summary: "Operator procedures for the kudos ledger: mode cutover, health checks, rollback, recovery, partition conversion, and event retention."
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

- `shadow`: inline balances and counters are authoritative. Matching ledger rows are retained as already-applied
  audit history.
- `ledger`: request transactions append postings and the database-serialized projector materializes them
  asynchronously.

Fresh installations and the migration SQL default to `shadow`. Moving to `ledger` is always an explicit operator
action. Currency postings and non-currency statistic events stay online for the retention window, and
[retention](#enabling-retention) replaces older partitions with summaries and an archive. The projector is serialized
by a PostgreSQL transaction advisory lock, independent of Redis quorum selection, and claims bounded batches with
`FOR UPDATE SKIP LOCKED`.

## Code and schema boundaries

Accounting tables, their foreign-key lifetimes, and the typed enums are specified in the reference
[data model](../reference/kudos_accounting.md#data-model). Two boundaries matter while operating the system:

- Currency history cannot be orphaned. `kudos_ledger.user_id` is `ON DELETE RESTRICT`, so a user wipe that would
  strand postings fails rather than deleting them. `kudos_stat_events` stores immutable audit IDs instead of
  ownership foreign keys, so hard-deleting a worker or team leaves its counter history intact.
- Mode-specific behavior lives in `horde/database/kudos_legacy_projection.py`. Business methods emit the same events
  in both modes. Advisory locks and repeatable-read setup are confined to `horde/database/kudos_db.py`, and counter
  upserts to `horde/database/kudos_counters.py`. A final cutover deletes the compatibility module and its call sites
  without rewriting the accounting flow.

## Pre-cutover proof

1. Deploy the ledger code and schema to the whole fleet in `shadow` mode. Do not mix it with code that does not write
   the audit rows. Verify: `uv run python tools/kudos_ledger_admin.py status` reports `"mode": "shadow"` and a
   non-null `heartbeat_seconds`, which shows the projector process is running.
2. Run through a representative peak-load window. Exercise transfers, upfront image/text/interrogation admission,
   cancellations, trust promotion, monthly awards, and admin adjustments. Verify: `kudos_ledger` and
   `kudos_stat_events` both gain rows for each exercised path, and every row has `applied = true`, which is what
   shadow mode writes.
3. Inspect `uv run python tools/kudos_ledger_admin.py status`. Investigate any non-zero pending queue, applier
   heartbeat gap, or `oldest_pending_seconds` above 30 seconds before continuing.
4. Capture a transaction-consistent baseline with `uv run python tools/kudos_ledger_admin.py snapshot`. Verify: the
   command prints `snapshot_id`. Use that value as `<snapshot-id>` below.
5. Run `uv run python tools/kudos_ledger_admin.py reconcile <snapshot-id>`. Verify: the `drifts` array is empty.
   Investigate every drift entry before continuing.
6. Switch with `uv run python tools/kudos_ledger_admin.py mode ledger`. The exclusive mode-gate advisory lock waits
   for every transaction that observed shadow mode before ownership changes, so no service freeze is required.
   Verify: `status` reports `"mode": "ledger"`, and `pending_rows` rises and then falls as the projector folds.
   Reverse with [online rollback](#online-rollback).

Monitor pending row count, oldest pending age, heartbeat age, database deadlocks, reservation age and count,
transfer rejection rate, and balance-floor adjustments throughout rollout, using the quorum node's applier
telemetry (`horde.kudos.*` metrics) or `tools/kudos_ledger_admin.py status`. The per-node
`/api/v2/status/heartbeat` response reports only node-local health. It does not include an applier-queue
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

`reconcile --apply` exits with an error while a currency retention unit is blocked with `late rows` or
`online drift since export`, because the checkpoints then omit rows. The read-only `reconcile` output lists those
units in `stale_checkpoint_units` and sets `drift_reliable` to `false`. Run
`retention unblock <unit-id> --recheckpoint` on the first listed unit, then reconcile again.

### Stopped projector

If the projector stops, leave writers in ledger mode, restore the projector, and drain. Unapplied rows are durable
and the database advisory lock prevents two replicas from applying them. Verify: `pending_rows` returns to zero and
`heartbeat_seconds` stays within the projector interval.

### Incorrect projection

If projection is corrupt, take a fresh snapshot for
evidence, reconcile against the last known-good baseline, review the complete drift list, apply compensating
postings, drain, and reconcile again. Verify: the second `reconcile` returns an empty `drifts` array. Do not edit
`applied`, delete postings, or directly overwrite balances. These changes cannot be undone and destroy the
evidence a later reconciliation needs.

### Quarantined events

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

Quarantined rows from partitions that retention has dropped are in `kudos_stat_quarantine`, with the same columns
and the `source_range` they came from. Run the same query against that table to see them.

Determine whether the producer emitted an invalid shape or a referenced user disappeared. Fix the producer/projector
first. Then repair any missing materialized counters with an explicit, reviewed compensating event or reconciliation
procedure appropriate to that counter. Retain the quarantined rows and their reason as evidence. Do not merely clear
`quarantined` or set `applied`, because either action can duplicate a partially repaired business event or silently
discard it.

### Database recovery

For database disaster recovery, restore PostgreSQL to the selected PITR/WAL point, retain the ledger and stat
history, the retention tables and the balance snapshots, start in shadow mode, reconcile, then repeat the cutover
proof. The retention catalog, checkpoints and partitions are restored together, so the retention job continues from
the restored catalog. Postings from partitions dropped before the restore point exist only in the archive. Read them
with [restoring an archived partition](#restoring-an-archived-partition-into-a-scratch-table).

## Converting the event tables to partitions

`sql_statements/5.1.16.txt` adds the retention tables, backfills idempotency keys, and turns `kudos_ledger` and
`kudos_stat_events` into tables range-partitioned on `created`. The partition layout is in the
[reference](../reference/kudos_accounting.md#partition-layout).

Preconditions: the previous schema is applied, the application release that maps the partitioned tables is not yet
deployed, the `psql` client can reach the primary, and no long-running dump or backup (`pg_dump`, a base backup) is
running or scheduled to start during the run. A dump holds `ACCESS SHARE` on every table for its whole duration, so
the swap cannot take its exclusive lock within the 5 second `lock_timeout` and fails. End state: both event tables
are partitioned, each with a
`<table>_p_legacy` partition and eight weekly partitions from `swap_ts`, and the retention tables exist and are empty except for
the backfilled keys.

1. Apply the file with psql in one session:

   ```sh
   psql "<database-url>" -v ON_ERROR_STOP=1 -f sql_statements/5.1.16.txt
   ```

   When `KUDOS_RETENTION_CURRENCY_DAYS` or `KUDOS_RETENTION_STATISTICS_DAYS` is raised above 30, add
   `-v key_lifetime_days=<days>` with the shorter of the two windows. Idempotency keys live for that shorter window,
   and the file backfills keys only for transfers and repairs inside `key_lifetime_days`, 30 when the option is not
   passed. A retry of an older transfer inside the key lifetime would otherwise find no key and post again. The file
   prints `Backfilling the idempotency keys of the last <days> days`. Check that the number is the key lifetime.
   The option needs psql 11 or later.

   The file uses `\gexec`, so only psql can run it. Do not pass `--single-transaction`, because
   `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block. The swap reads its timestamp from a temporary
   table and calls temporary functions, so the whole file must run in one session. Steps 2 and 3 happen while the file
   runs.

2. Watch the online index builds. Before each swap, the file builds a unique `(id, created)` index concurrently
   on each table (`kudos_ledger_p_legacy_pkey`, `kudos_stat_events_p_legacy_pkey`). It also builds
   `ix_kudos_stat_events_legacy_created` on `kudos_stat_events`, which the retention job uses to read the legacy
   partition by sub-range. Each build reads the whole table and does not block writes. Verify progress from another
   session:

   ```sql
   SELECT relid::regclass, index_relid::regclass, phase, blocks_done, blocks_total
   FROM pg_stat_progress_create_index;
   ```

   A failed concurrent build leaves an `INVALID` index. Drop it with `DROP INDEX CONCURRENTLY <index-name>` before
   rerunning the file.

3. Watch the range CHECK and the swap. For each table, the file computes `swap_ts`, the
   first UTC Monday 00:00 at least 24 hours away. It then adds `<table>_legacy_range CHECK (created < swap_ts)` and
   validates it with one scan that does not block writes. The swap transaction renames the table to
   `<table>_p_legacy`, creates the partitioned parent, and attaches the old table and its indexes without a scan or
   copy. It holds an exclusive lock for a short time. The `kudos_ledger` swap first locks `users` in
   `SHARE ROW EXCLUSIVE` mode, the order application transactions take the two locks in, so the swap cannot deadlock
   with them. Writes to `users` wait until the swap commits. The swap fails if it cannot take a lock within the
   5 second `lock_timeout`, and then the file is rerun as described in
   [rerunning after a failure](#rerunning-after-a-failure).

   The swap window runs from the moment `swap_ts` is set until `swap_ts`. The CHECK rejects new rows with
   `created` at or after `swap_ts` until the swap commits, so the window must outlast the validation and the swap,
   whatever the table size and the clock skew between application hosts. Rows written between the swap and `swap_ts`
   go into the legacy partition, which keeps them for at most one extra partition width. `swap_ts` falls on a
   Monday so the first weekly partition is an ordinary week on the same grid as later partitions. Verify: psql exits
   with status 0 after the second swap.

4. Confirm the layout for each table with the catalog queries that the migration test uses:

   ```sql
   SELECT relkind FROM pg_class WHERE oid = CAST('<table>' AS regclass);              -- p
   SELECT pg_get_partkeydef(CAST('<table>' AS regclass));                             -- RANGE (created)
   SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
   FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
   WHERE i.inhparent = CAST('<table>' AS regclass);                                   -- legacy and eight weekly
   SELECT pg_get_constraintdef(oid) FROM pg_constraint
   WHERE conrelid = CAST('<table>' AS regclass) AND contype = 'p';                    -- PRIMARY KEY (id, created)
   SELECT indexrelid::regclass FROM pg_index
   WHERE indrelid = CAST('<table>' AS regclass) AND NOT indisvalid;                   -- no rows
   SELECT pg_get_serial_sequence('<table>', 'id');                                    -- not null
   ```

   The legacy bound is `FOR VALUES FROM (MINVALUE) TO ('<swap_ts>')`, the first weekly bound starts at the same
   timestamp, and the eight weeks follow each other without a gap. The file creates eight weeks because the previous
   release does not create partitions. After a rollback to it, inserts keep finding a partition until eight weeks
   after `swap_ts`, and the new release's retention tick creates the later weeks once it runs again.

5. Deploy the application release in this order: stop every replica of the previous release, run the file again with
   the same command, including any `-v key_lifetime_days`, and only then let replicas of the new release take traffic. The conversion steps skip the
   converted tables. The key backfill adds the keys of transfers and repairs made by the previous release since the
   first run. The new release looks up idempotency keys only in `kudos_idempotency_keys`, so a keyed transfer that
   the previous release committed after the last backfill would post a second time when its client retries against
   the new release. Verify: `SELECT kind, count(*) FROM kudos_idempotency_keys GROUP BY kind` returns rows when
   transfers or repairs happened inside the backfill window.

6. Check the cutover once the new release takes traffic. Verify:
   `uv run python tools/kudos_ledger_admin.py retention status` returns JSON with `"blocked_units": 0`, and the
   duplicate-transfer query does not return any rows:

   ```sql
   SELECT event_id FROM kudos_ledger WHERE entry_type = 'TRANSFER' GROUP BY event_id HAVING count(*) > 2;
   ```

   One transfer posts two rows under one `event_id`, so a returned `event_id` was posted more than once. Reverse the
   extra posting with a counter-transfer between the same two users.

### Rerunning after a failure

Every statement in the file is idempotent, so a failed run is repeated from the top in a new session once the cause
is fixed. Pass the same `-v key_lifetime_days` as the first run, so the rerun backfills the same window. If the table is still unconverted and `<table>_legacy_range` exists from the earlier session, the rerun
reads `swap_ts` back from that CHECK, so the CHECK and the swap use the same timestamp. Without the CHECK, the new
session computes a new `swap_ts`. If the CHECK has a form the file cannot read, the rerun stops with
`cannot read swap_ts from <table>_legacy_range`. Drop the CHECK and rerun in a new session, which adds and validates
it again:

```sql
ALTER TABLE <table> DROP CONSTRAINT IF EXISTS <table>_legacy_range;
```

If the swap has not committed by `swap_ts`, inserts into that table fail, and the swap stops with the error
`swap_ts for <table> has passed and the CHECK now rejects new rows`. Drop `<table>_legacy_range` at once with the
statement above, which restores inserts. Then rerun the file in a new session, which uses the next Monday.

A rerun on converted tables does not change them, also after retention has dropped `<table>_p_legacy`, except that
it creates each missing week among the eight from the `swap_ts` it computes. It skips a week that an existing
partition overlaps or that ended before the current week, and it does not alter an existing partition.

The conversion does not have a scripted reversal. Until retention drops the legacy partition, every row from before
the swap is still in `<table>_p_legacy`.

## Enabling retention

Preconditions: the event tables are converted and the release is deployed. End state: the quorum node's retention
tick moves eligible units through their states, and the health fields show progress. The variables, states and
metrics are listed in the [reference](../reference/kudos_accounting.md#event-retention).

The commands below run inside an application container. The script lives under `tools/`, so the application root
must be the working directory and on the import path:

```bash
docker exec -w /app -e PYTHONPATH=/app <container> python tools/kudos_ledger_admin.py retention status
```

1. Configure the archive, if currency postings are to be removed. Set `KUDOS_ARCHIVE_ACCOUNT`,
   `KUDOS_ARCHIVE_BUCKET`, `KUDOS_ARCHIVE_AWS_ACCESS_KEY_ID` and `KUDOS_ARCHIVE_AWS_SECRET_ACCESS_KEY`, with
   credentials limited to that bucket. Each tier has its own byte budget. `KUDOS_ARCHIVE_BUDGET_BYTES` is the
   currency tier's budget, and it is then required, so startup fails without it.
   `KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES` is the statistics tier's budget, and unset means no limit. Retention does
   not evict anything. A dump or export that would pass its tier's budget blocks the unit instead.
   Without an archive, statistics are still rolled up and dropped (as `dump_waived`, with one
   `Kudos retention dump failed ... archive not configured` warning per unit), but currency partitions stay online.
   Verify:
   `retention status` reports `"archive_configured": true` and a byte count for each tier in `archive_tier_bytes`,
   and `horde.kudos.archive.budget_bytes` has one series for each tier with a budget.
2. Set `KUDOS_RETENTION_ENABLED=true`. Set `KUDOS_RETENTION_CURRENCY_DAYS` or `KUDOS_RETENTION_STATISTICS_DAYS` only
   to keep a dataset longer than 30 days. On a fresh production deployment, also set
   `KUDOS_RETENTION_HOLD_DROPS=true` for a soak, so every step runs but no partition is detached or dropped, and set
   `KUDOS_RETENTION_DROP_GRACE_DAYS` to the rollback window to keep after the soak, long enough for the checks run
   after a drop to notice a problem. It defaults to `7` when unset. The policy is read when the process starts, so
   restart every application process. Verify: `retention status` reports the intended window in `drop_grace_days`,
   and `uv run python tools/kudos_ledger_admin.py retention step` prints `"outcome": "idle"` with
   `"detail": "no actionable unit"`, or a step result. It prints `"outcome": "skipped_lock"` when the scheduled tick
   holds the lock, and then it can be run again. While retention is disabled, the detail is `retention disabled`. The
   command runs a real tick and can drop a partition.
3. Expect the first ticks to be idle. A partition becomes eligible when its upper bound is older than the window, so
   the legacy partition becomes eligible one window after `swap_ts`. Until then `oldest_eligible_age_seconds` is null
   for both datasets and the gauge reads 0.
4. Watch the legacy partition being processed. The first eligible tick catalogues it as one unit per partition width,
   starting at the Monday on or before its oldest row, and logs
   `Kudos retention catalogued <n> units of <dataset> partition <table>_p_legacy`. Each later tick makes one transition
   across both datasets, and the scheduler runs a tick every 60 seconds. Statistics units move through `rolled_up`
   and `dumped`, and currency units through `checkpointed`, `exported` and `verified`. The legacy partition is
   dropped by one tick after every one of its units is ready. Until then it stays attached, so
   `oldest_eligible_age_seconds` keeps growing and `pending_units` stays above zero. Verify: `retention status` shows
   the units advancing, `last_step_age_seconds` stays near the tick interval, and `horde.kudos.retention.steps` grows
   with `outcome` `stepped`. While drops are held, the units stop at `dumped`, `dump_waived` or `verified`, ticks
   turn `idle`, and each tick logs `Kudos retention holds <n> partitions`. Verify: `retention status` reports
   `"hold_drops": true` and `held_partitions` above 0, and `horde.kudos.retention.held_partitions` matches it.
5. End the soak. Compare the checkpoints, rollups and archive objects of the held partitions with their online rows,
   as in [the rehearsal](#rehearsing-partition-removal-on-a-restored-copy). Then set
   `KUDOS_RETENTION_HOLD_DROPS=false`, keep `KUDOS_RETENTION_DROP_GRACE_DAYS`, and restart. With a grace set, the next
   ticks detach each held partition and keep its table. Verify: `retention status` lists it in
   `detached_partitions` with its `drop_due_at`, its units are `detached`, and a tick after `drop_due_at` drops it.
   Until then it can be [rolled back](#rolling-back-a-removed-partition).
6. Confirm the steady state. After the legacy drop, each weekly partition is processed when it becomes eligible.
   Verify: `oldest_eligible_age_seconds` returns to a value of a few ticks or null, and `blocked_units` is 0.

To pause retention, set `KUDOS_RETENTION_ENABLED=false` and restart. Units keep their state, and enabling it again
continues from there. To pause only the removal, set `KUDOS_RETENTION_HOLD_DROPS=true` and restart: the other steps
continue, and partitions already `detached` are kept past their `drop_due_at` until the hold ends. A dropped partition
cannot be brought back online. Archived currency rows can be read with
[restoring an archived partition](#restoring-an-archived-partition-into-a-scratch-table). Per-job statistic rows
remain only in the statistics dumps that an operator has not evicted.

### Rolling back a removed partition

Preconditions: a partition listed in `detached_partitions` by `retention status`, whose units are `detached`, before
its `drop_due_at`. End state: the partition is attached again, its rows are read through the parent table, and its
units stay `blocked` until an operator releases them.

1. Reattach the partition, passing the `id` of any of its units from `retention status`:

   ```sh
   uv run python tools/kudos_ledger_admin.py retention reattach <unit-id>
   ```

   The command attaches the table over the partition's recorded range and commits the catalog change with it. Each
   unit becomes `blocked` with `reattached by operator` and keeps `state_before_detach`, so later ticks do not detach
   the partition again. A blocked currency unit stops currency retention, and statistics retention continues past a
   blocked statistics unit. A unit the drop recount blocked after the detach keeps its `late rows` reason, and a
   partition whose units were all blocked that way is reattached the same way. The command refuses when no unit of
   the partition has `detached_at` set or its table no longer exists,
   and exits with an error while a tick holds the retention lock, so run it again after the tick. Verify: the command
   prints the units with `"state": "blocked"`, `"error": "reattached by operator"` and a null `detached_at`, and the
   partition is listed by `SELECT inhrelid::regclass FROM pg_inherits WHERE inhparent = '<parent>'::regclass`.
2. Investigate with the rows online. To keep the partition, leave its units blocked. Retention of other partitions
   of that dataset continues for statistics and stops for currency until the units are released.
3. When the partition may be removed again, unblock each of its units:

   ```sh
   uv run python tools/kudos_ledger_admin.py retention unblock <unit-id>
   ```

   Each unit returns to its state before the detach (`verified`, `dumped` or `dump_waived`). Once every unit of the
   partition is released, the next tick detaches it again and starts a new grace period. When the investigation
   found rows that changed after the checkpoint, use
   [the recheckpoint cascade](#recovering-from-a-late-row-or-chain-block) on a currency unit instead. Verify:
   `retention status` lists the partition in `detached_partitions` again with a new `drop_due_at`.

A partition that was already dropped cannot be reattached. Its currency rows can still be read with
[restoring an archived partition](#restoring-an-archived-partition-into-a-scratch-table).

## Rehearsing partition removal on a restored copy

Nothing becomes eligible until a partition's upper bound is older than the window, so a freshly converted database
has nothing to remove for at least the window. To exercise the removal path before that, restore a copy of the
database, point it at a scratch bucket or at a prefix of its own (`KUDOS_ARCHIVE_PREFIX`), enable retention, and move
the clock forward for one tick at a time:

```bash
uv run python tools/kudos_ledger_admin.py retention step --as-of <swap time plus the window plus one partition width> --confirm-live-data-loss
```

Each call makes at most one transition and prints the effective `as_of`. Repeat until the outcome is `idle`, then
read `retention status` and compare the checkpoint, rollup and archive figures against the copy's pre-removal totals
as the end-to-end test does. The time must be between the current time and the current time plus the longer window
plus four partition widths, because partitions are created ahead of it in one transaction. A time later than now
makes the partitions that still receive rows eligible, so without `--confirm-live-data-loss` the tick is refused with
`"outcome": "refused"` and `reference time is in the future`. Pass the flag on a restored copy only. Against a live
database it drops the current week's rows. After the rehearsal, `retention status` reports
`"partition_coverage_ok": true` once partitions for the real current time exist again.

The drop grace is measured with the tick's reference time. With `KUDOS_RETENTION_DROP_GRACE_DAYS` set, a rehearsal
tick detaches a partition with `detached_at` set to its `--as-of`, and the partition is dropped only by a later tick
whose `--as-of` is at least the grace past it. Advance `--as-of` past `drop_due_at` from `retention status` to see the
drops. The latest accepted time grows by the grace for this reason. A tick at the real time does not drop a partition
that a rehearsal detached, because its `detached_at` is still in the future. To rehearse the rollback, run
`retention reattach` on a `detached` unit before advancing past its `drop_due_at`.

## Handling a blocked unit

Preconditions: a `Kudos retention blocked` warning, a non-zero `horde.kudos.retention.blocked_units` series (one
per `horde.kudos.block_reason`), or a unit with `"state": "blocked"` in `retention status`. End state: the cause is
fixed, the unit is back in `pending` or `checkpointed`, and it advances on later ticks.

1. Find the unit. `uv run python tools/kudos_ledger_admin.py retention status` lists it with `id`, `dataset`,
   `source_range` and `error`. A blocked currency unit stops all currency retention. Statistics retention continues
   past a blocked statistics unit.
2. Fix the cause for its `error`:

   - `unapplied rows`: the unit still has rows that are not folded by the projector. Check `status` for a stopped
     projector or a backlog, and `drain` if needed. A statistic event that cannot be folded is quarantined by the
     projector and then does not count. Do not set `applied` by hand. Verify that this query returns no rows:

     ```sql
     SELECT id, event_id, created, entry_type FROM ONLY <partition-name>
     WHERE applied = false AND created >= '<unit-lower>' AND created < '<unit-upper>';
     ```

     For a statistics unit, add `AND quarantined = false`. Omit the lower bound when `unit_lower` is null.
   - `checkpoint chain gap`: no currency unit in `checkpointed`, `exported`, `verified` or `dropped` ends at this
     unit's lower bound, or one already ends after its upper bound. The chain is read from the catalog, not from
     `kudos_ledger_checkpoints`, because an empty unit does not write any checkpoint rows. Compare the two:

     ```sql
     SELECT id, partition_name, unit_lower, unit_upper, state
     FROM kudos_retention_catalog WHERE dataset = 'currency' ORDER BY unit_upper;
     SELECT boundary, count(*) FROM kudos_ledger_checkpoints GROUP BY boundary ORDER BY boundary;
     ```

     Find out how the chain broke before changing either table. Unblocking while the gap remains blocks the unit
     again on the next tick. A later unit that was checkpointed out of order is recovered with
     [the recheckpoint cascade](#recovering-from-a-late-row-or-chain-block) from the earliest affected unit.
   - `boundary already checkpointed`: a unit of another partition already records a checkpoint at this unit's upper
     bound, as after a partition created by hand over a dropped range. Recomputing it would replace that checkpoint
     with this unit's rows alone, so the engine does not. Merge or remove the extra partition by hand, then unblock.
   - `currency tier would exceed budget`: the export would push the currency tier past `KUDOS_ARCHIVE_BUDGET_BYTES`.
     Currency objects are never evicted. Raise the budget and restart, or leave the unit blocked and let currency
     partitions stay online. Statistics retention continues meanwhile.
   - `statistics tier would exceed budget`: the dump would push the statistics tier past
     `KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES`, so nothing was uploaded. Later statistics units continue, and each of
     them blocks the same way until there is room. Raise the budget and restart, or free space by evicting the oldest
     dumps:

     ```sh
     uv run python tools/kudos_ledger_admin.py archive evict --tier statistics --budget-bytes <bytes>
     ```

     The command prints the deleted keys. When a delete fails it prints the keys deleted before the failure and exits
     1. On a bucket with versioning, a deleted object stays stored as a noncurrent version that the tier listing does
     not count, so free the space by the deployment's own procedure as well. The next tick clears the catalog keys of
     every deleted object. Then unblock the unit, which rolls it up again
     and retries the dump. Verify: the unit reaches `dumped` and its `object_key` is set. To drop the unit without a
     dump instead, run `retention unblock <unit-id> --waive-dump`.
   - `archive object missing`: a currency unit past its export has no archive object. Either the tick's archive
     listing does not hold the unit's key, or the drop found no key. Nothing was dropped, and the rows are still
     online or in the detached table. Find out why the object is gone before going on. For a unit in an attached
     partition, unblock it: it returns to `checkpointed`, and the next ticks export, verify and drop it again from
     the online rows. For a unit of a detached partition, `retention unblock` refuses, so run
     `retention reattach <unit-id>` first and then unblock. Verify: the unit reaches `verified` with a new
     `object_key`, and `archive audit --tier currency` reports no `missing_object` line for it.
   - `archive mismatch`: the object read back does not match its manifest. The object is kept. The warning includes
     the mismatched fields. Check the object store, and restore the object into a scratch table if the rows need
     inspection.
   - `online drift since export`: rows in the unit's range changed after the export, or the export holds a different
     number of rows than the checkpoint recorded. Restore the object into a scratch table and compare it with
     `SELECT * FROM ONLY <partition-name>` over the unit's range to find the changed rows and their writer. Rows
     committed after the checkpoint need [the recheckpoint cascade](#recovering-from-a-late-row-or-chain-block).
   - `late rows`: the drop recounted the unit and found a different row count or highest `id` than the replacement
     recorded. Nothing was dropped. Use [the recheckpoint cascade](#recovering-from-a-late-row-or-chain-block) for a
     currency unit. For a statistics unit, unblock it: the rollup runs again over the current rows.
   - `reattached by operator`: an operator reattached the unit's partition with `retention reattach`. Unblocking
     returns the unit to its state before the detach, and the partition is detached again with a new grace period.
     See [rolling back a removed partition](#rolling-back-a-removed-partition).
   - `dump retries exhausted`: statistics dumps failed for longer than `KUDOS_RETENTION_DUMP_RETRY_DAYS`. Fix the
     archive and unblock the unit to retry from the rollup, or accept the loss of the raw rows:

     ```sh
     uv run python tools/kudos_ledger_admin.py retention unblock <unit-id> --waive-dump
     ```

     The unit moves to `dump_waived` and is dropped on the next tick with only its rollups kept. The same command
     waives a unit blocked with `statistics tier would exceed budget`.

3. Return the unit to work:

   ```sh
   uv run python tools/kudos_ledger_admin.py retention unblock <unit-id>
   ```

   A currency unit whose checkpoint is committed returns to `checkpointed`, so export and verification run again
   against that checkpoint and the export replaces the object at the same key. Every other unit returns to `pending`
   and its steps run again from the start. The command exits with an error while a tick holds the retention lock, so
   run it again after the tick. Verify: the command prints the unit with `"state": "checkpointed"` or `"pending"`,
   the unit's `blocked_units` series drops, and later `retention status` output shows the unit advancing.

`snapshot` holds the retention lock and exits with an error while a tick or a recovery command holds it, so run it
again afterwards. It also exits with an error while a currency checkpoint is stale. After
`retention unblock --recheckpoint`, `reconcile` refuses every snapshot created since the earliest reset unit entered
the catalog, in read-only and `--apply` modes alike, because that baseline was computed from the deleted checkpoints.
The rows of those snapshots stay in `kudos_balance_snapshots` for inspection. Take a fresh snapshot after the recovery
and use it for later checks.

## Recovering from a late-row or chain block

Preconditions: a currency unit is `blocked` with `late rows`, or with `online drift since export` because rows were
committed into its range after its checkpoint. End state: the unit and every later currency unit have recomputed
checkpoints that include those rows, and they drop in order.

A late row makes the unit's checkpoint, daily statements and recorded row count wrong, and every later checkpoint
repeats the wrong totals. Unblocking the unit alone does not help: back in `checkpointed` it blocks again,
and recomputing only its checkpoint is refused while later boundaries exist.

1. Find out who wrote the rows and stop the writer. A row more than a window late comes from a clock fault or a
   reference time moved forward on a live database.
2. Reset the unit and every later currency unit:

   ```sh
   uv run python tools/kudos_ledger_admin.py retention unblock <unit-id> --recheckpoint
   ```

   The command refuses when a later currency unit is already `dropped`, because its rows cannot be recounted. A
   blocked currency unit stops currency retention, so this refusal means the catalog was changed by hand. Verify:
   the printed `reset` list starts with the unit and every entry has `"state": "pending"`.
3. Let the ticks recompute the tail. Each unit checkpoints, exports over its previous object, verifies and drops in
   upper-bound order. Verify: `retention status` shows the units advancing, `blocked_units` stays 0, and a
   reconciliation snapshot reports no drift for the affected users.

When a step fails without blocking, `Kudos retention <dataset> step failed for <source_range>` is logged as an error and
the tick outcome is `failed`. Examples are an unreachable archive and a rollup whose totals differ from the unit. The
step runs again on every tick. A failure that repeats on every tick needs the same investigation as a blocked unit.

## Reading a user's statement

Preconditions: retention has checkpointed the days in question. End state: the user's daily movements for a range
of UTC days.

1. Print the archived daily statement:

   ```sh
   uv run python tools/kudos_ledger_admin.py statement <user-id> --from <first-day> --to <last-day>
   ```

   Each line has `day`, `entry_type`, `escrow`, `amount_sum` and `row_count`, summed across the units that cover
   that day. Only applied postings are included. Verify: `lines` is non-empty for days on which the user had movements.
2. Read days that are still online from `kudos_ledger`, because they do not have statement lines yet:

   ```sql
   SELECT CAST(created AS date) AS day, entry_type, escrow, sum(amount), count(*)
   FROM kudos_ledger
   WHERE user_id = <user-id> AND applied AND created >= '<first-day>' AND created < CAST('<last-day>' AS date) + 1
   GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
   ```

3. For the individual postings of an archived day, restore the unit that covers it, as described in the next section.

## Restoring an archived partition into a scratch table

Preconditions: every `KUDOS_ARCHIVE_*` variable is set where the command runs. End state: the rows of one archive
object are in a scratch table in the application database. Qualify the scratch table with a schema (`public.kudos_restore_check`): an unqualified name is created in the first schema of the application role's search path, which is not always `public`.

1. Find the object key. Dropped units keep their `object_key` in the catalog:

   ```sql
   SELECT id, dataset, partition_name, unit_lower, unit_upper, state, object_key
   FROM kudos_retention_catalog WHERE object_key IS NOT NULL ORDER BY unit_upper;
   ```

   A statistics object deleted by `archive evict`, or lost in any other way, leaves its unit with a null
   `object_key` once a tick has run. A statistics unit in `dump_failed` or `dump_waived` does not have an object.
2. Restore it into a table name that does not exist yet:

   ```sh
   uv run python tools/kudos_ledger_admin.py archive restore <object-key> --into public.kudos_restore_check
   ```

   The command creates the table from the column names and PostgreSQL types the manifest recorded at export, and
   copies the rows. The live event tables are not consulted, so a column renamed, dropped or retyped since the export
   does not affect the restore. An object whose manifest is format version 1 does not record column types, and the command
   refuses it with `predates recorded column types`. A table with that name that exists already is refused,
   because a retried restore would hold every row twice. To collect several objects in one table, pass `--append`
   for each restore after the first. The output then has `"appended": true`, and the count check below covers the
   whole table. The copy commits only when the object's digest, its stored size and the copied row count equal the
   manifest and the compressed stream is complete. Otherwise the command fails with
   `does not match its manifest` and does not commit the rows.
   Verify: the printed `rows` equals the manifest's `row_count`, and
   `SELECT count(*) FROM public.kudos_restore_check` returns the same number.
3. Query the scratch table, then drop it with `DROP TABLE public.kudos_restore_check`.

## Auditing the archive

Preconditions: every `KUDOS_ARCHIVE_*` variable is set where the command runs. End state: every archive object has
been read back against its manifest, and the catalog has been compared with the listing. Nothing is changed.

1. Run the audit, monthly from cron or by hand:

   ```sh
   uv run python tools/kudos_ledger_admin.py archive audit > kudos-archive-audit.jsonl
   ```

   Pass `--tier currency` or `--tier statistics` to audit one tier. Each object is downloaded in full, so the audit
   takes as long as reading the whole tier.
2. Read the result. The command exits 1 when any line reports a problem. A line with `"kind": "object"` and
   `"matches_manifest": false` lists its `problems`: an unreadable manifest, an object that does not decompress, or a
   field that differs from the manifest. A line with `"kind": "missing_object"` is a catalog unit whose object is
   gone. For a currency unit that is lost history, because currency objects are never evicted. The last line is the
   summary. Verify: the exit status is 0 and the summary has `"problems": 0`. The summary's `noncurrent_bytes` and
   `delete_markers` map each tier to the storage still held by noncurrent object versions and to its delete markers.
   They grow after an eviction or a re-export in a versioned bucket until a lifecycle rule expires the old versions.
   A `null` value means the store did not list versions, so the figure is unknown. Neither figure affects the exit
   status.
3. Resume an interrupted audit from the summary's `last_key`, or from the last object line, with
   `--since-key <key>`. The catalog comparison always covers the whole listing.

## Automated drill coverage

`tests/unit/test_kudos_safety.py` covers concurrent-projector exclusion, reservation overspend prevention, transfer
idempotency, final-event trust promotion, an atomic ledger-to-shadow tail drain, snapshot drift detection,
idempotent compensating repair, and replay across floor adjustments. `tests/unit/test_wp_activate_deadlock.py`
covers bounded PostgreSQL deadlock retry behavior. Run both against PostgreSQL before every cutover or recovery
exercise. `tests/unit/test_kudos_retention.py`, `tests/unit/test_kudos_archive.py`,
`tests/integration/test_kudos_retention_migration.py` and `tests/integration/test_kudos_retention_end_to_end.py`
cover the retention steps, the archive and the partition conversion.
