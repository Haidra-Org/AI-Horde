---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Separate currency postings from statistic postings

## Context and Problem Statement

One settlement moves several kinds of value at once: the requester's spendable balance, the worker owner's
spendable or evaluation balance, the worker's display kudos, the worker's contribution and fulfilment
aggregates, the team totals stamped with the membership at settlement time, and the requester's usage records.
These values are denominated differently. Currency is a two-decimal amount against one user account. A
contribution is measured in things. A fulfilment is a count. Worker and team kudos describe attributed work
that nobody can spend.

## Decision Drivers

- Reconciliation has to answer whether user currency is conserved. That question only has an answer if every
  row in the table it reads is a signed delta against one user balance in one unit.
- Worker and team rows are hard-deletable, while user rows are soft-deleted and wiped. A single table cannot
  carry both the `ON DELETE RESTRICT` currency history requires and the deletion tolerance counter history
  requires ([ADR 7](0007-accounting-foreign-key-policy.md)).
- Consumers of the two classes differ. Balance readers, spend admission, and snapshots want currency.
  Leaderboards, worker detail, team rankings, and user records want counters.

## Considered Options

- Separate currency postings from statistic postings
- One posting table for everything
- Treat worker kudos as currency

## Decision Outcome

Chosen option: "Separate currency postings from statistic postings".

Currency postings go to `kudos_ledger`. Every row is a signed `NUMERIC(20,2)` delta against exactly one
`users.id`, with `escrow` selecting the spendable balance or the evaluation balance. Display and counter
postings go to `kudos_stat_events`. Every row carries a `unit` of `kudos`, `things`, or `count`, targets
exactly one of `user_id` or `worker_id`, and records `team_id` and `worker_user_id` as captured attribution.
Rows produced by one business event share an `event_id` across both tables, so the pairing survives the split.

Worker and team kudos are display attribution. `WorkerTemplate.modify_kudos` emits stat events; the matching
currency credit reaches the owner through `User.modify_kudos`.

### Consequences

- Good: Reconciliation reads one table whose every row is comparable, which is what makes the snapshot baseline in
  [ADR 6](0006-permanent-archive-compensation-only-repair.md) a checkable statement about user currency.
- Good: The two tables can hold the foreign-key policies their contents need.
- Good: A reviewer classifies a new value before writing code: currency, display attribution, counter, price, or
  quota. The reference records the excluded kudos-like state that is none of these.

- Bad: The projector claims two batches and folds both, and an event can straddle a batch boundary in either table.
  Event-wide side effects such as transfer-hold release must query for remaining unapplied rows.
- Bad: Reconciliation covers user currency and evaluation escrow. Counter drift is replayable from
  `kudos_stat_events` but is not compared by the snapshot and repair command.

## Pros and Cons of the Options

### One posting table for everything

A nullable target set (`user_id`, `worker_id`, `team_id`) plus a unit column.

- Bad: conservation, reconciliation, and reporting all become ambiguous once unlike units and
  unlike targets share a table, and the foreign-key policy conflict above has no resolution within one schema.

### Treat worker kudos as currency

Credit the worker as an account holder.

- Bad: a worker is not an account. The spendable credit for a worker's job belongs to its owning user in
  `users.kudos`, and `workers.kudos` is the attribution total the worker's own pages display.
