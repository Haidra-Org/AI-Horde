---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Promote users to trusted inside the projector

## Context and Problem Statement

An untrusted worker owner's rewards go wholly or partly to `users.evaluating_kudos`. Once that escrow crosses
the trust threshold and the account is old enough, the user becomes trusted and the escrow is released as a
paired `EVALUATION_PROMOTION` escrow debit and balance credit.

With projection asynchronous ([ADR 3](0003-single-serialized-projector.md)), the transaction that emits the
final qualifying escrow credit cannot see its own effect: the credit is an unapplied `kudos_ledger` row, and
`users.evaluating_kudos` still holds the pre-credit value. The threshold has to be evaluated after the fold.

## Decision Drivers

- The final qualifying contribution should promote the user even if they never submit another request.
- Promotion is a threshold crossing, and a crossing evaluated twice can release the escrow twice.
- Shadow mode changes no balance the previous inline code would not ([ADR 5](0005-shadow-mode-cutover.md)).

## Considered Options

- Promote users to trusted inside the projector
- Promote on the user's next request
- Promote in the emitting transaction

## Decision Outcome

Chosen option: "Promote users to trusted inside the projector".

In `ledger` mode, the projector evaluates promotion after folding a cycle's escrow deltas, inside the same
transaction. `_promote_eligible_users` selects users who are not already trusted, whose projected
`evaluating_kudos` exceeds the trust threshold, and whose account is at least seven days old, skipping anon
and suspicious accounts. It grants the `TRUSTED` role and unpauses the user's workers.

`_drain_trusted_escrow` then scans for trusted users still carrying positive escrow and emits an
`EVALUATION_PROMOTION` pair for the full amount under one event ID. A later cycle folds that pair, the escrow
reaches zero, and the scan stops finding the user. It reads trust state and the drain amount from committed
columns rather than ORM instances, because the role write does not refresh a loaded role collection. A user
with an unapplied `EVALUATION_PROMOTION` posting is skipped: without that guard, a cycle that emitted a pair
but could not fold it would emit a second pair, over-crediting the balance once the backlog folds.

In `shadow` mode the compatibility projector owns promotion and the escrow drain (`project_trust_promotion`),
and the asynchronous projector's promotion duties do not run.

### Consequences

- Good: The contribution that crosses the threshold promotes the user, with no dependence on a later request.
- Good: The drain scan is self-healing. A trusted user carrying stranded escrow, including one promoted after their
  escrow folded in an earlier cycle, is drained by a later cycle with no operator touching a balance.
- Good: The release is a posting pair like any other, reconciling and replaying through the same path as the rest of
  the archive ([ADR 6](0006-permanent-archive-compensation-only-repair.md)).

- Bad: Promotion is eventually consistent: it happens a cycle after the qualifying contribution, and the spendable
  credit lands a cycle after that.
- Bad: The projector holds trust policy: threshold, account age, and the anon and suspicious exclusions. Changing
  that policy changes the projector and its shadow-mode counterpart, and a promotion test has to say which of
  the two it exercises.

## Pros and Cons of the Options

### Promote on the user's next request

- Bad: promotion then depends on the user coming back, and the request path would have to reason about its own
  unprojected escrow to get the threshold right.

### Promote in the emitting transaction

- Bad: it cannot observe the escrow its own credit will produce.
