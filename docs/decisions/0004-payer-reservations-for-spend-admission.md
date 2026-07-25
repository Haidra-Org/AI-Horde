---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Admit spends under a payer lock against reserved availability

## Context and Problem Statement

Once projection is asynchronous ([ADR 3](0003-single-serialized-projector.md)), `users.kudos` stops being a
sufficient answer to "may this debit be accepted?". A user with 100 visible kudos can submit two simultaneous
80-kudos transfers; if each debit exists only as an unapplied `kudos_ledger` row, both transactions read 100
and both accept. Overspend has to be refused at admission, before the projector runs.

## Decision Drivers

- A transfer involves two accounts. Locking both rebuilds the two-hot-row coupling the event design removed.
- Upfront admission is retryable. A worker re-claiming an interrogation form, or a client retrying a request,
  must not stack a second hold for the same business operation.

## Considered Options

- Admit spends under a payer lock against reserved availability
- Read the materialized balance and accept
- Row-lock the payer's `users` row for admission
- Authorize against `effective_kudos`

## Decision Outcome

Chosen option: "Admit spends under a payer lock against reserved availability".

Spend admission serializes on the payer alone, through a transaction-scoped advisory lock derived from the
payer's user ID (`acquire_payer_lock`, namespace `KUDOS_PAYER_LOCK_NAMESPACE`). The recipient of a transfer is
never locked for admission.

Under that lock, `available_kudos` computes capacity as the materialized balance less the account floor, less
active reservations, less ordinary queued debits, reading all three terms in one statement so they come from a
single database snapshot. Queued credits are deliberately excluded: unprojected income cannot fund a spend.

An accepted operation calls `reserve_kudos` with a stable `business_id` in the same transaction as its
posting, and attaches the reservation ID to the debit's `detail`. The business IDs are `upfront:<wp-id>`,
`interrogation:<form-id>`, and `transfer:<source-user-id>:<event-id>`. Uniqueness on `business_id` makes a
retry reactivate the existing hold rather than add one, and reactivation may not change the payer. The
projector consumes a request hold as its debit folds, and releases a transfer hold once no unapplied posting
remains for the event.

`effective_kudos` remains available: materialized balance plus all committed unapplied deltas, clamped to the
floor. It answers "what is my new balance?" for an admin adjustment response or a diagnosis, never a spend.

### Consequences

- Good: An overspend cannot be authorized on the strength of unmaterialized income, whatever the projector lag is.
- Good: Transfers serialize on one account, so two users gifting the same recipient do not contend.
- Good: Retries are stable, and reservation release is idempotent, returning zero when no active hold remains.

- Bad: Admission is conservative. A user whose incoming credit is queued is told they cannot afford a spend they
  will shortly be able to afford, and projector lag lengthens that window. Relaxing the queued-credit rule
  requires a proof covering credit rollback, event ordering, and projector failure.
- Bad: Some legacy reads still consult the materialized balance, including the stable worker's secondary upfront
  eligibility check. Those can transiently alter scheduling, though the initial admission hold still stands.

## Pros and Cons of the Options

### Read the materialized balance and accept

- Bad: The two-transfer case in the problem statement accepts both debits.

### Row-lock the payer's `users` row for admission

- Bad: it puts the hot user row back into every admission transaction's lock graph, the coupling
  [ADR 1](0001-event-sourced-kudos-accounting.md) removed.

### Authorize against `effective_kudos`

- Bad: it includes queued credits and represents no holds, so two concurrent admissions can both be funded by
  the same unprojected income.
