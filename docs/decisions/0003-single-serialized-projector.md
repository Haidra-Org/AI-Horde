---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Fold events with one database-serialized projector

## Context and Problem Statement

Appending postings ([ADR 1](0001-event-sourced-kudos-accounting.md)) leaves the question of who updates
`users.kudos`, `workers.kudos`, the team totals, `user_stats`, `user_records`, and the reservation lifecycle.
The projection targets are exactly the hot rows whose uncoordinated acquisition order produced the motivating
deadlock class, so a concurrent set of appliers would rebuild that lock graph one level down.

## Decision Drivers

- A fold must be all-or-nothing with the applied flags it sets. A crash between the two would double-apply or
  silently drop a movement.
- The Horde runs several replicas. The mechanism that picks the single writer cannot depend on the Redis
  process quorum, which does not participate in the database transaction that commits the fold.
- Some rows commit out of ID order. A transaction that acquired a low `kudos_ledger.id` can commit after a
  higher ID is already visible and folded.

## Considered Options

- Fold events with one database-serialized projector
- Parallel appliers sharded by target
- A high-water mark over `id`

## Decision Outcome

Chosen option: "Fold events with one database-serialized projector".

One projector owns projection in `ledger` mode. Each cycle acquires the PostgreSQL transaction advisory lock
`KUDOS_APPLIER_LOCK` in `horde/database/kudos_db.py`, which is held for the transaction and is independent of
the process quorum. Under that lock it claims bounded, ID-ordered batches of currency and statistic rows with
`FOR UPDATE SKIP LOCKED`, groups deltas by target, visits materialized targets in stable primary-key order,
consumes or releases the matching reservations, marks the exact claimed row IDs applied, and commits all of it
in one transaction.

`applied = false` is the work queue. A row is selected by its flag, never by `id > watermark`, so a
late-committing lower ID is claimed by a later cycle instead of being skipped. `kudos_ledger_applier_state`
holds a heartbeat for observability and is not consulted when choosing work. Batches are
`KUDOS_APPLIER_BATCH_SIZE` rows; the background task runs every three seconds and keeps folding within a tick
while batches come back full, up to `KUDOS_APPLIER_MAX_CATCHUP_CYCLES`, so a backlog clears at many batches
per tick while each transaction stays small.

### Consequences

- Good: Exactly one committed fold per accepted row. If the process dies before commit, neither the target updates
  nor the applied flags commit, and a later cycle sees the same rows.
- Good: The accounting lock graph is one writer visiting targets in a documented order, which is what lets the mode
  transition ([ADR 5](0005-shadow-mode-cutover.md)) share that order without an applier/control deadlock.
- Good: A stopped projector only delays folding: rows stay durable and unapplied until it returns.

- Bad: Projection throughput is bounded by one writer. Batch size, the three-second interval, and the catch-up
  cycle limit are the only scaling controls; going wider means a new ordering and reservation design.
- Bad: Queue age, heartbeat age, and reservation age need alerting; `/api/v2/status/heartbeat` reports `DEGRADED`
  once the oldest pending event exceeds 30 seconds.

## Pros and Cons of the Options

### Parallel appliers sharded by target

- Bad: sharding needs a new ordering scheme and a reservation design that survives an event whose postings land
  in different shards, which is more machinery than the throughput requires.

### A high-water mark over `id`

Track the highest folded ID and claim above it.

- Bad: a lower ID that commits later is permanently skipped, and the skip is silent.
