---
status: proposed
date: 2026-07-25
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Balance every currency event against system accounts

## Context and Problem Statement

Currency events are not all two-sided. A settlement or a transfer posts a debit and a matching credit, so its
postings already sum to zero. Mint events (uptime rewards, awards, monthly grants) post only a credit, and burn
events (the activation tax, the cancellation burn) post only a debit; the counterparty exists in the economy but
not in `kudos_ledger`. Without it there is no global arithmetic identity to check, so an emission bug that
creates or destroys kudos on one side of an event has no cheap detector. Reconciliation against a snapshot finds
drift between the ledger and a projection, and it does not find a ledger that is internally unbalanced.

## Decision Drivers

- An emission bug that creates or destroys kudos on one side of an event needs a cheap detector.
- The detector should be internal to the ledger, so it cannot mistake a bad projection for a bad event.
- Statistics events share the table but conserve nothing, so the invariant must scope to the currency unit.

## Considered Options

- Balance every currency event against system accounts
- Per-entry-type conservation assertions
- Leave events unbalanced

## Decision Outcome

Chosen option: "Balance every currency event against system accounts", because it makes the ledger's internal
consistency checkable with a single aggregate query.

Virtual system accounts (a treasury the mints draw from and a burn sink the debits flow into) are introduced,
and every mint and burn event posts its counterparty explicitly. Every `event_id` then sums to zero, which makes
the double-entry global invariant (all `unit = 'kudos'` postings sum to zero) true by construction. Scoping the
invariant to the currency unit keeps it principled once statistics events share the table, since counters
denominated in things, counts, or seconds conserve nothing.

### Consequences

- Good: One query decides whether the ledger is internally consistent, independent of any projection.
- Good: A per-event sum check localizes an emission bug to the event that broke, rather than to whichever
  balance later looked wrong.
- Bad: Every mint and burn producer gains a second posting, adding one currency row per mint or burn event and
  enlarging the archive.
- Bad: System accounts are a new account kind that is not a user, so the account columns and every query that
  assumes a user or worker target need a defined representation for them.

## Pros and Cons of the Options

### Per-entry-type conservation assertions

Keep events one-sided and encode the expected shape of each entry type in tests and reconciliation.

- Good: No schema change and no additional rows.
- Bad: The check is a list that must be extended for every new entry type, and it verifies only the types
  somebody remembered to enumerate.

### Leave events unbalanced

- Good: Nothing to build.
- Bad: The only detector for created or destroyed kudos stays snapshot reconciliation, which reports drift
  between the ledger and a projection and cannot distinguish a bad projection from a bad event.
