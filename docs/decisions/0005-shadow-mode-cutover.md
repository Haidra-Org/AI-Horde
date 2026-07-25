---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Cut over through a temporary shadow mode

## Context and Problem Statement

Moving balance ownership from inline mutation to the serialized projector changes who writes `users.kudos` for
every settlement, transfer, award, uptime reward, and adjustment in the service. The Horde cannot take a
maintenance window, so the change has to land on a running fleet and be reversible while it is running.

## Decision Drivers

- The event schema, emission primitives, reservations, and projector need production exposure before any
  balance depends on them.
- Rollback must not leave accepted postings unfolded behind a writer that has stopped reading them, and
  whatever carries the transition has to be removable afterwards without unpicking the accounting flow.

## Considered Options

- Cut over through a temporary shadow mode
- Deploy straight into ledger mode
- Feature-flag each producer

## Decision Outcome

Chosen option: "Cut over through a temporary shadow mode".

New and migrated installations start in `shadow` mode, recorded on the single `kudos_ledger_control` row.
Business methods emit the same typed events in both modes. What differs is ownership of the materialized
change and the applied flag chosen by the two emission primitives:

- In `shadow`, `horde/database/kudos_legacy_projection.py` performs the historical inline mutation and the
  rows are written `applied = true`, giving audit evidence without replaying a movement that already reached
  its target. That projector also owns trust promotion and the escrow drain
  ([ADR 8](0008-trust-promotion-in-the-projector.md)), so shadow changes no balance the old code would not.
- In `ledger`, rows are written `applied = false` and the serialized projector
  ([ADR 3](0003-single-serialized-projector.md)) owns every materialized target.

The mode branch is confined to `kudos_legacy_projection.py`, the control helpers, and the applied-flag choice
inside the two emission primitives. Endpoints and settlement methods contain no mode check, so removing the
cutover period is a deletion: the compatibility module, its direct calls, and the shadow transition.

Every mutation transaction pins the mode it observed with a key-share lock on the control row until commit.
`set_kudos_ledger_mode` takes the applier advisory lock, then an exclusive control-row lock that waits for
those writers; returning to `shadow` folds the remaining ledger tail in the same transaction.

### Consequences

- Good: Cutover is gated on evidence: a peak-load shadow window, a clean `status`, a snapshot, a clean reconcile.
- Good: Rollback is one command with no service freeze, and active upfront reservations survive it because
  shadow-mode debits consume the same holds inline.

- Bad: The migration mechanism is synchronous double-write, so shadow mode keeps the whole original lock graph and
  adds the event writes, buying none of the concurrency benefit.
- Bad: Shadow history is a forward audit beginning at deployment. The materialized balances at that moment are the
  opening position, recorded by a snapshot alongside the applied-ledger totals visible at the same time.
- Bad: Rolling back to code that predates reservations and shadow audit rows is unsafe at any point after deploy.

## Pros and Cons of the Options

### Deploy straight into ledger mode

- Bad: the first production evidence about the projector arrives at the moment balances already depend on it,
  and there is no rollback that does not strand postings.

### Feature-flag each producer

- Bad: mode checks inside settlements, transfers, awards, and endpoints spread across the codebase, and
  removing them becomes a diffuse edit rather than a deletion.
