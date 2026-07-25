---
status: accepted
date: 2026-07-24
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Keep the archive permanent and repair only by compensation

## Context and Problem Statement

Under inline mutation the current column was the only accounting record, so correcting a wrong balance meant
overwriting the one visible number and leaving no trace of the error or the correction. Event tables make a
better answer possible, and a worse one tempting: an operator facing drift can reach for
`UPDATE kudos_ledger SET applied = true`, a `DELETE` of a suspect posting, or a direct balance assignment.

## Decision Drivers

- Reconciliation computes expected current values from a snapshot baseline plus the events applied since,
  which is only sound if applied history never changes after the fact.
- Repair runs during incidents, under time pressure, and may be run twice without doubling its correction.
- Some projector rules are not linear. Minimum-balance forgiveness makes the balance change smaller than the
  debit's own amount, so replaying the postings alone would not reproduce the projection.

## Considered Options

- Keep the archive permanent and repair only by compensation
- Prune applied events after a retention window
- Repair by assigning the correct balance
- Repair by clearing `applied` so the projector refolds

## Decision Outcome

Chosen option: "Keep the archive permanent and repair only by compensation".

Applied currency and statistic history is permanent. Repair happens only by appending.

`reconcile <snapshot-id>` is read-only: it compares the materialized balances against the snapshot plus every
currency posting applied since, and reports drift. `reconcile <snapshot-id> --apply` serializes repair runs on
`KUDOS_RECONCILIATION_LOCK` and emits one deterministic `RECONCILIATION` posting per affected user. That
computation excludes prior `RECONCILIATION` entries from its movement baseline, so re-running the same repair
before or after projection cannot duplicate it.

Non-linear projector behavior is represented as its own posting rather than left implicit. When the account
floor forgives part of a debit, the projector emits a `FLOOR_ADJUSTMENT` credit for the amount created,
written already applied, so replaying postings from a snapshot reproduces the projection exactly across
separate batches. No repair edits an `applied` flag, an amount, a target, or a balance column directly.

### Consequences

- Good: Drift is a diagnosable difference against a stated baseline, and its correction is a posting with an entry
  type, an amount, and a timestamp that later reconciliations account for. Replay stays linear, so a snapshot
  plus subsequent postings is a checkable statement about current balances.
- Good: Disaster recovery keeps its evidence: restore to the selected PITR point, retain the archives and snapshots,
  start in `shadow` ([ADR 5](0005-shadow-mode-cutover.md)), reconcile, then repeat the cutover proof.

- Bad: `kudos_ledger` and `kudos_stat_events` grow without bound and need capacity planning.
- Bad: Repairing an incident is a procedure rather than an edit: preserve evidence, snapshot, reconcile against the
  last known-good baseline, review the complete drift list, apply compensation, drain, reconcile again.
- Bad: The reconcile and repair command covers user currency and evaluation escrow. Worker, team, and counter rows
  are replayable from `kudos_stat_events` but are not compared by it.

## Pros and Cons of the Options

### Prune applied events after a retention window

- Bad: the baseline for reconciliation and the evidence for a post-incident review both live in applied
  history. `prune_applied_kudos_ledger` is a no-op.

### Repair by assigning the correct balance

- Bad: the assignment is invisible to reconciliation, which then reports the repair itself as new drift on the
  next run.

### Repair by clearing `applied` so the projector refolds

- Bad: it silently changes what the snapshot baseline means and can refold a movement that already reached its
  target.
