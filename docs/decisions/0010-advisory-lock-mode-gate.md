---
status: accepted
date: 2026-07-25
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Gate mode pins and transitions on an advisory lock

## Context and Problem Statement

The shadow cutover ([ADR 5](0005-shadow-mode-cutover.md)) requires every mutation transaction to pin the
mode it observed until commit, and a transition to wait out all old-mode writers before the new ownership
rule becomes visible. The original mechanism was row locking on the `kudos_ledger_control` row: writers took
`FOR KEY SHARE`, transitions took `FOR UPDATE`.

PostgreSQL row locks do not queue fairly across those modes. A new `FOR KEY SHARE` request checks for
conflict only with current lock holders; key-share does not conflict with key-share, so it acquires on the
fast path without joining the wait queue behind a blocked `FOR UPDATE`. Whenever writer pins overlap without
a gap, the transition's exclusive lock waits for the holders present at each retry, new pins arrive in the
meantime, and acquisition never completes. Under sustained production write load, gapless overlap is the
normal case, not a corner case. A deterministic reproduction
(`test_mode_transition_is_not_starved_by_gapless_writer_pins`) holds two lock-step writer pins with
guaranteed overlap and starves the transition until its statement timeout on every run.

## Decision Drivers

- A mode flip is an operator action on a live fleet; its latency must be bounded by the longest in-flight
  mutation transaction, not by writer arrival rate.
- The pin is taken by every balance mutation, so the primitive sits on the hottest accounting path and must
  not add measurable per-transaction cost or new deadlock edges.

## Considered Options

- Advisory shared/exclusive lock as the mode gate
- Keep row locks and retry the transition in a loop
- Quiesce writers out-of-band before flipping

## Decision Outcome

Chosen option: "Advisory shared/exclusive lock as the mode gate".

`get_kudos_ledger_mode` takes `pg_advisory_xact_lock_shared` on a dedicated key and then reads the control
row plainly; `set_kudos_ledger_mode` takes the same key with `pg_advisory_xact_lock`, after the applier lock
as before. Advisory locks are heavyweight locks with fair queueing: a shared request that arrives while an
exclusive request waits queues behind it. The transition therefore admits as soon as the pins held at
queue time drain, and every ADR 5 guarantee (pin until commit, transition waits out old-mode writers, final
fold inside the transition transaction) is preserved with the same acquisition points and the same
applier-before-gate lock order.

### Consequences

- Good: Transition latency is bounded by the longest in-flight mutation transaction.
- Good: The hot path sheds the control-row `FOR KEY SHARE`, removing per-transaction multixact membership
  churn on one global tuple and its vacuum burden; a shared advisory lock acquisition is a cheaper
  in-memory operation.
- Good: New pin requests block only while a transition is queued or active, which is the intended brief
  write pause of ADR 5, now with a bounded start.
- Bad: The gate no longer locks the row it protects; the coupling between the lock key and
  `kudos_ledger_control` is by convention in `horde/database/kudos_db.py` rather than enforced by the
  database.
- Bad: Like the other accounting advisory locks, the gate is invisible to `USE_SQLITE` runtime mode and is
  validated only by the PostgreSQL-backed suites.

## Pros and Cons of the Options

### Keep row locks and retry the transition in a loop

- Bad: retries do not change the admission rule; under gapless pins every retry starves the same way.

### Quiesce writers out-of-band before flipping

- Bad: reintroduces the maintenance-window class of operation ADR 5 exists to avoid, and needs fleet-wide
  coordination machinery for what one fair lock provides.
