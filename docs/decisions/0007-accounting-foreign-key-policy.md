---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Match each accounting table's foreign keys to its lifetime

## Context and Problem Statement

The accounting tables reference users, workers, and teams, and those three are deleted in different ways. User
accounts are soft-deleted and wiped. Workers and teams can be hard-deleted by their owners, and that deletion
is a supported operation rather than an exceptional one.

The four tables also differ in what their rows are for:

- `kudos_ledger` rows are authoritative currency history and the input to reconciliation.
- `kudos_stat_events` rows are display and counter history behind worker, team, and user aggregates.
- `kudos_reservations` rows are temporary holds against one payer.
- `kudos_balance_snapshots` rows are a per-user reconciliation baseline.

## Decision Drivers

- Users, workers, and teams are deleted in different ways, and worker and team deletion is a supported
  operation that must stay unblocked.
- The four tables hold rows with different lifetimes: authoritative currency history, display and counter
  history, temporary holds, and a per-user reconciliation baseline.

## Considered Options

- Match each accounting table's foreign keys to its lifetime
- Ownership foreign keys everywhere, cascading
- Ownership foreign keys everywhere, restricting
- No foreign keys on any accounting table

## Decision Outcome

Chosen option: "Match each accounting table's foreign keys to its lifetime".

Each table gets the policy its contents' lifetime requires.

- `kudos_ledger.user_id` is a required foreign key with `ON DELETE RESTRICT`. Authoritative currency history
  must not become orphaned, and users are removed by soft delete and wipe rather than row deletion, so the
  restriction does not block a supported operation.
- `kudos_reservations.user_id` and `kudos_balance_snapshots.user_id` are ownership foreign keys with
  `ON DELETE CASCADE`. A hold and a per-user reconciliation baseline only have meaning while the user exists.
- `kudos_stat_events` carries `user_id`, `worker_id`, `worker_user_id`, and `team_id` as immutable audit
  references with no foreign key. Workers and teams are hard-deletable while their counter history must
  survive them, and neither cascade nor restrict can deliver both. The exception is documented on the mapped
  model so it reads as a decision rather than an omission.

### Consequences

- Good: A `kudos_ledger` row always names a real user, so reconciliation never has to reason about a posting
  whose account is gone.
- Good: Deleting a worker or a team stays a supported, unblocked operation, and the totals its work
  contributed to keep their explanation in `kudos_stat_events`.
- Good: Holds and snapshots clean themselves up with the account they belong to.

- Bad: `kudos_stat_events` rows can reference a worker or team that no longer exists, so a consumer joining
  against live rows must tolerate a missing target rather than assume one.
- Bad: Every future accounting table must be assigned a policy deliberately, answering the same question: is
  this row authoritative history, an audit reference, or operational state?
- Bad: The split reinforces why currency and statistic postings live in separate tables
  ([ADR 2](0002-separate-currency-and-statistic-event-tables.md)). Merging them would reopen a conflict with
  no schema-level resolution.

## Pros and Cons of the Options

### Ownership foreign keys everywhere, cascading

- Bad: Deleting a worker would erase the postings that explain its team's totals, and reconciliation would
  lose the currency history for a wiped user.

### Ownership foreign keys everywhere, restricting

- Bad: Worker and team deletion would fail for any worker that had ever earned anything, which is every worker
  that has run.

### No foreign keys on any accounting table

- Bad: It gives up the guarantee that a currency posting always names a real user, which is the one
  referential guarantee reconciliation depends on.
