---
status: proposed
date: 2026-11-03
decision-makers: [Tazlin]
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Keep 30 days of exact kudos history online, roll statistics up by day, and archive currency rows

Exact currency and statistic events stay in PostgreSQL for at least 30 days. Older statistic events are reduced to
UTC-day rollups and deleted. Older currency events are summarized into per-user checkpoints and daily statements,
written to a verified compressed archive, and then deleted. A small idempotency table replaces the lookups that
currently read history of any age. This amends the storage meaning of permanence in
[ADR 6](0006-permanent-archive-compensation-only-repair.md) and keeps compensation-only repair.

## Context and Problem Statement

`kudos_ledger` and `kudos_stat_events` are ordinary heaps that grow with service lifetime. One business event
appends one or two currency rows and several statistic rows, so statistic rows are the large majority of both tables
and of their growth. Neither table is bounded, and backups, replicas and the all-time reconciliation aggregate all
scale with them.

Three code paths read event rows older than the unapplied queue. Every other reader uses materialized projections.

- Reconciliation computes each user's all-time applied movements in `horde/database/kudos_reconciliation.py`,
  `_applied_totals_subquery`, on every snapshot and every reconcile.
- Transfer idempotency in `horde/database/functions.py`, `transfer_kudos`, looks up prior postings by `event_id`
  without an age bound and compares their amounts and parties.
- Repair idempotency in `kudos_reconciliation.py` looks up the `RECONCILIATION` posting by `event_id` before emitting
  one.

No API route, leaderboard, record or moderation view reads either table. Worker deletion and spend admission read only
unapplied rows. After the first few minutes of a row's life, the only use of the row is incident investigation.
Request expiry and job leases settle an event within an hour
([ADR 38](0038-lease-text-jobs-for-a-fixed-term-renewed-by-progress.md),
[ADR 39](0039-settle-requests-after-cancellation-and-repeated-faults.md)), so 30 days is a generous investigation
window.

An earlier form of this proposal archived both datasets row for row into object storage forever, sealed ID ranges
behind a writer barrier under the exclusive mode gate, and migrated to ID-range partitions through a mirrored
backfill. That design placed the checkpoint boundary at the present moment, which is what required the barrier. It
also moved the growth problem into the object store instead of removing it. It is recorded under Considered Options.

## Decision Drivers

- Bound primary storage, backup size and routine reconciliation work by the retention window instead of by service
  lifetime.
- Keep 30 days of exact events directly queryable for incident response.
- Preserve the currency evidence that compensation-only repair and reconciliation depend on.
- Keep transfer retries, the parameter-conflict check, and repair idempotency correct after rows leave the tables.
- Keep worker, team and user attribution in whatever replaces the statistic rows.
- Object-store availability must not stall settlement, projection or writes.
- Every removal waits for a verified replacement to be committed first.
- State the retention of accounting history in the published privacy disclosure.

## Considered Options

- Keep all exact history in primary PostgreSQL indefinitely
- Replace old postings with net balance and counter totals
- Archive both datasets row for row forever behind sealed ID ranges
- Roll statistics up by day, archive currency rows, and checkpoint reconciliation

## Decision Outcome

Chosen option: "Roll statistics up by day, archive currency rows, and checkpoint reconciliation".

### Online window and partitions

Both tables become range-partitioned by `created`, in UTC. The online window is at least 30 days for each dataset. It
is configurable upward per dataset and is not shortened by capacity pressure. A partition is eligible for removal when
its upper bound is older than the window, so partition width adds to the effective retention. `created` is assigned
when the row is written, so a row that commits late lands in the partition of its write time. A partition whose upper
bound is 30 days in the past cannot receive a row from any transaction that is still open. Eligibility by age
therefore does not need a writer barrier, sequence sealing or a drain under the mode gate.

The primary key becomes (`id`, `created`). No foreign key references either table. The existing heap becomes the
first partition. A validated `CHECK` on `created` lets it attach without a scan, the swap is a rename under a brief
exclusive lock with no copy, and its indexes attach to partitioned indexes created on the parent, including the partial
unapplied and quarantine indexes. New partitions default to one week. The first partition becomes eligible 30 days
after the swap. Because it holds the whole pre-swap history, its replacements are built in bounded `created`
sub-ranges recorded in the catalog, and the single drop waits for all of them. The migration is rehearsed on a restored
copy before production.

The retention job runs under its own advisory lock and processes one eligible partition at a time in ascending order.
It records each transition in a catalog table so that a crash resumes the work instead of repeating it. A partition
with an unapplied row is not removed. A row that is unapplied after 30 days is an incident, and the job reports it.

### Statistics: roll up by UTC day and delete

Before a statistic partition is dropped, its rows are folded into `kudos_stat_rollups`, keyed by UTC day,
`entry_type`, `unit`, `stat_action`, `record`, `wp_type`, `user_id`, `worker_id`, `worker_user_id` and `team_id`, with
`amount_sum`, `row_count`, `amount_min`, `amount_max` and `created_max`. Those aggregates reproduce every statistic
projection. Counters and contributions fold by sum, user records fold by maximum, and last activity folds by the
newest `created`. Attribution survives at full dimensionality. The per-job row is lost, together with its `job_id`,
`event_id` and `detail`. The rollup for one source partition is written in one transaction that first deletes any
rollup rows already attributed to that partition, so a retried or rebuilt rollup cannot double-count. Its totals are
compared with the partition's totals before the drop is allowed.

Quarantined rows ([ADR 14](0014-quarantine-deterministically-invalid-statistic-events.md)) are not folded. They move,
unchanged, into `kudos_stat_quarantine` in the same transaction as the rollup. An unresolved quarantine stays
discoverable and does not block retention. Repair of a quarantined event continues to append.

A compressed dump of each statistic partition is written to the archive bucket before the drop on a best-effort basis.
A failed dump is reported and does not block the drop. The rollup is the durable replacement for statistics.

Rollups also grow, with the number of distinct attribution keys per day rather than with the number of events. They
have no retention in this decision.

### Currency: checkpoint, statement, verified archive, delete

Before a currency partition is dropped, three replacements are committed and the archive object is verified.

`kudos_ledger_checkpoints` holds, per user, the cumulative applied balance and escrow totals through the partition's
upper bound, excluding `RECONCILIATION` postings exactly as reconciliation does today. A checkpoint is the previous
checkpoint plus the partition's applied sums, computed in one repeatable-read transaction that also verifies that the
partition holds no unapplied row. Reconciliation becomes the latest checkpoint plus the applied online tail at or above
its boundary. Existing `kudos_balance_snapshots` rows store the same cumulative all-time totals, so the present
subtraction of a snapshot's `applied_balance_total` keeps working unchanged against the new sum. Checkpoints are sums
of events, not copies of materialized balances, so drift present at checkpoint time stays visible as drift.

`kudos_ledger_daily` holds, per user, `escrow`, `entry_type` and UTC day, the summed amount and the row count. It is an
operator statement for disputes older than the window, read through the ledger administration command. It has no API
surface in this decision.

The partition's rows are exported, compressed, to the archive bucket with a manifest that includes the schema version,
the `created` range, the row count, per-user control totals and a content digest. The object is read back and checked
against the manifest and the online partition before the drop. A failed export, verification or upload leaves the
partition online and raises an alert. When the currency tier would exceed its budget, currency retention stops and
alerts, and statistics retention continues.

### Idempotency keys

A new `kudos_idempotency_keys` table records, for each transfer and each repair posting set, the scoped key's
`event_id`, the kind, the parties, the amount and the outcome. The row is written in the same transaction as the
postings. `transfer_kudos` and the repair path read this table instead of `kudos_ledger`, and both the
parameter-conflict check and the second check under the payer lock are kept. Keys live for the online window and are
deleted by the retention job. A retry after expiry is a new transfer, and the API documentation states the lifetime.
Repair refuses a snapshot older than the key lifetime, so an expired repair key cannot produce a second
`RECONCILIATION` posting. At migration, keys for the transfers and repairs inside the online window are backfilled from
the tables by `event_id`. Older keys would already have expired.

### Archive bucket and budget

The archive is one S3-compatible bucket with dedicated least-privilege credentials and a configured byte budget per
tier. Objects are written once and are not modified afterwards. Nothing is evicted automatically: a tier whose next
upload would exceed its budget stops that tier's retention and alerts, and an operator either raises the budget or
evicts statistic dumps by hand, oldest first. The deployment's backup policy decides whether the bucket is copied
elsewhere. This decision requires only that the policy be written down together with the bucket's
restore procedure. An unavailable bucket pauses currency retention and statistic dumps. It does not pause writes,
projection or statistic rollups.

### Erasure and disclosure

Accounting history is retained through an account wipe, online and in the archive, as
[ADR 7](0007-accounting-foreign-key-policy.md) already requires for `kudos_ledger`. Rows hold numeric account, worker
and team identifiers. The only free-text field is the `reason` key in an administrative adjustment's `detail`, and
administrators must not place personal data there. The published privacy disclosure states the online window, that
currency events are archived without a fixed end, and that statistic events are reduced to daily aggregates. The
disclosure is rendered from the same configuration that the retention job reads, as
[ADR 15](0015-tiered-moderation-evidence-retention.md) does for moderation records.

### Consequences

- Good: Online size is bounded by the window and by traffic instead of by service lifetime, and reconciliation scans
  a 30-day tail.
- Good: The largest dataset leaves the database without an archive dependency. The rollup is the replacement.
- Good: Currency evidence survives row for row, and repair stays compensation-only.
- Good: There is no writer barrier, sequence contract or mirrored backfill. The swap is a rename and the removal is a
  partition drop.
- Bad: Per-job statistic detail older than the window is gone except in best-effort dumps.
- Bad: Four new tables (rollups, quarantine, checkpoints, daily statements) plus the key table and the catalog join
  the accounting schema and its recovery procedure.
- Bad: A transfer retried after 30 days with the same key is a new transfer, and repair cannot use a snapshot older
  than 30 days.
- Bad: Reading archived currency rows requires restoring an object instead of an indexed query.
- Bad: The archive budget is a policy that the operator must size against measured compressed growth.

## Pros and Cons of the Options

### Keep all exact history in primary PostgreSQL indefinitely

- Good: No new tables, procedures or stores.
- Bad: Storage, backups, replicas and reconciliation cost grow without bound.

### Replace old postings with net balance and counter totals

- Good: Smallest possible state.
- Bad: Net totals cannot explain movements, retries or disputed settlements, and reconciliation loses the event sums
  that define it. This violates ADR 6.

### Archive both datasets row for row forever behind sealed ID ranges

The previous form of this proposal.

- Good: Every row of both datasets remains recoverable.
- Bad: Statistic rows are replayable display history and are not reconciled. Archiving them forever moves most of the
  growth into the object store instead of removing it.
- Bad: A checkpoint boundary at the present moment needs a writer barrier under the exclusive mode gate, uncached
  sequences and a mirrored backfill with its own conflict protocol. The age-based boundary makes all of these
  unnecessary.
- Bad: An unresolved quarantined row would block a whole range indefinitely.

### Roll statistics up by day, archive currency rows, and checkpoint reconciliation

- Good: Each dataset gets the cheapest replacement that preserves what its consumers need.
- Bad: Coordination across PostgreSQL, the catalog and the bucket, and operator tooling to read the archive.

## Confirmation

Implementation acceptance requires PostgreSQL tests for:

- the partition swap preserving unapplied and quarantine state and their partial indexes;
- rollup totals equal to partition totals, including record maxima and last activity;
- rollup retry without double counting;
- quarantine relocation;
- checkpoint plus tail equal to the all-time sum, with and without legacy snapshots;
- drift present at checkpoint time still reported afterwards;
- identical and conflicting transfer retries before and after key expiry;
- repair refusal on an expired snapshot;
- archive export, verification failure and restore;
- the drop refused while the archive object or its manifest cannot be checked, and an upload refused when its
  measured size exceeds the tier's budget;
- reconciliation refusing a snapshot baseline captured before its checkpoints were rebuilt;
- bucket unavailability leaving writes, projection and rollups unaffected.

A rehearsal on a restored production copy measures the swap lock duration, rollup and export throughput, checkpoint
cost, partition query plans and restore time before the first production partition is removed.

## More Information

This record amends the storage meaning of permanence in [ADR 6](0006-permanent-archive-compensation-only-repair.md)
while keeping compensation-only repair, the dataset split in
[ADR 2](0002-separate-currency-and-statistic-event-tables.md), the serialized projector in
[ADR 3](0003-single-serialized-projector.md), the lifetime policy in
[ADR 7](0007-accounting-foreign-key-policy.md), the mode gate in [ADR 10](0010-advisory-lock-mode-gate.md) and
quarantine evidence in [ADR 14](0014-quarantine-deterministically-invalid-statistic-events.md). Its disclosure
follows [ADR 15](0015-tiered-moderation-evidence-retention.md).
