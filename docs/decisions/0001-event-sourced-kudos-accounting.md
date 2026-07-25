---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Record kudos movements as events; keep balances as projections

## Context and Problem Statement

Kudos mutations edited the materialized balance and aggregate columns inline, inside whatever request
transaction needed them. That made frequently used user rows part of many otherwise unrelated transaction lock
graphs: concurrent activation, settlement, and transfer paths reached the same rows in different orders, which
produced a recurring deadlock class that could only be papered over with retries. Separately, the current
column value was the only accounting record. There was no audit trail, no replay, and no way to repair a bad
balance other than overwriting the one visible number.

## Decision Drivers

- Queueing, API responses, leaderboards, and existing integrations need inexpensive reads. Computing balances
  from history on demand was never viable at Horde read volume.
- Kudos are spendable. A design with asynchronous projection must still refuse an overspend at admission time.
- The Horde cannot take a maintenance window, so the migration must be observable and reversible online.

## Considered Options

- Record kudos movements as events; keep balances as projections
- Fix lock ordering in place
- Synchronous double-write, permanently
- Full event sourcing

## Decision Outcome

Chosen option: "Record kudos movements as events; keep balances as projections".

Every kudos-moving business event appends signed, typed rows to permanent event tables: `kudos_ledger` for
currency, `kudos_stat_events` for display totals and counters. The existing balance and aggregate columns are
retained as denormalized read models, folded from those events. Producers stop editing the materialized
columns inside request transactions. Spend admission is protected by payer reservations
([ADR 4](0004-payer-reservations-for-spend-admission.md)), projection is owned by a single serialized projector
([ADR 3](0003-single-serialized-projector.md)), and cutover happens through shadow mode
([ADR 5](0005-shadow-mode-cutover.md)).

### Consequences

- Good: Ordinary producers append rows instead of locking hot user, worker, team, and stats rows, which
  removes the motivating deadlock class (the
  [explanation](../explanation/kudos_accounting.md#deadlock-mitigation) states the precise, narrower
  claim).
- Good: Movements are durable, typed, and correlated by `event_id`. Drift is diagnosable by reconciliation
  against a snapshot, and repair is a deterministic compensating posting rather than a hand edit
  ([ADR 6](0006-permanent-archive-compensation-only-repair.md)).

- Bad: Balance and aggregate reads become eventually consistent in `ledger` mode. Every consumer had to be
  inventoried and its tolerated lag made explicit in the
  [consumer inventory](../reference/kudos_accounting.md#consumer-and-read-model-inventory).
- Bad: The permanent archives grow without pruning and need capacity planning.
- Bad: Projection throughput is bounded by the single serialized projector
  ([ADR 3](0003-single-serialized-projector.md)).

## Pros and Cons of the Options

### Fix lock ordering in place

Impose a global acquisition order on the inline design.

- Bad: Every future kudos-touching change would need to know and preserve the order, the audit/replay gap
  remains, and multi-account transfers still couple two hot rows in one transaction.

### Synchronous double-write, permanently

Emit events and keep mutating inline. Retained only as the temporary `shadow` migration mechanism
([ADR 5](0005-shadow-mode-cutover.md)).

- Good: Reads stay current.
- Bad: Keeps the whole lock graph and adds the event writes; the events become a write-amplified log with none
  of the concurrency benefit.

### Full event sourcing

Drop the columns and rebuild state from events.

- Bad: Read cost and blast radius: every consumer in the codebase changes at once, and the opening balances
  predate the ledger by design.

## More Information

The measurements behind this record come from lock-chain and `pg_stat_statements` captures taken on the
production primary on 2026-07-20. They are summarized in
[the problem is shared mutable rows](../explanation/kudos_accounting.md#the-problem-shared-mutable-rows),
including the queue depths that persisted on the statistics rows once the balance rows stopped serializing.
