---
status: accepted
date: 2026-08-30
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Quarantine deterministically invalid statistic events without stopping the projector

## Context and Problem Statement

The serialized projector applies currency and statistic postings from one global queue. A statistic posting can be
durable and structurally readable while still being impossible to materialize: its target user may no longer exist,
its dimensions may exceed a projection table's limits, or its discriminator and dimensions may describe no supported
projection. Retrying such a row cannot change its data. Repeatedly rolling it back leaves every unrelated posting
behind it unapplied.

Workers require separate treatment. They are hard-deletable by design ([ADR 7](0007-accounting-foreign-key-policy.md)),
and a statistic event can retain team attribution after its worker target disappears. A missing worker therefore does
not make the event malformed.

## Decision Drivers

- One permanently invalid statistic event must not stop currency settlement or unrelated statistic projection.
- Infrastructure and database failures may recover on retry and must continue to roll the transaction back.
- Related statistic postings in one claimed business event must not be partially materialized when one of them is
  known to be invalid.
- Invalid rows remain audit evidence and require an explicit operator decision about repair or acceptance.
- Worker deletion remains a supported operation while surviving team attribution continues to fold.

## Considered Options

- Quarantine deterministically invalid statistic events by claimed business event
- Retry every projection failure indefinitely
- Mark invalid events applied or delete them
- Quarantine only the individual invalid row

## Decision Outcome

Chosen option: "Quarantine deterministically invalid statistic events by claimed business event".

After the projector claims a batch, it validates every statistic row before writing any materialized counter. The
validator returns a bounded `KudosStatEventQuarantineReason` only for data that cannot become projectable through
retry. If one claimed row is invalid, every claimed row with that `event_id` is marked `quarantined`, receives the same
reason and quarantine time, and remains `applied = false`. Unrelated valid events in the batch continue through the
ordinary projection transaction.

Quarantine state commits in the same transaction as the valid projections and their applied flags. A failed commit
records neither progress nor quarantine. Exceptions outside the deterministic validator, including database,
connectivity, lock, and infrastructure failures, escape normally and roll the entire cycle back for retry.

The drainable statistic queue is `applied = false AND quarantined = false`, amending the unapplied-queue rule in
[ADR 3](0003-single-serialized-projector.md). Its partial index uses the same predicate. Quarantined rows stay in
`kudos_stat_events` as retained evidence and do not re-enter the hot queue automatically. Repair appends or performs an
explicitly reviewed projection-specific correction; it does not erase the quarantined event.

A worker event whose worker has been hard-deleted remains valid. Worker deletion locks its pending statistic events,
and worker-stat projection takes a key-share lock while confirming that the worker exists. Whichever transaction
claims the event first makes the other wait. If deletion completes first, the projector skips only the missing
worker's materialization while continuing any surviving team attribution. This extends the lifetime policy in
[ADR 7](0007-accounting-foreign-key-policy.md).

### Consequences

- Good: A permanent data defect no longer blocks the global currency and statistic pipeline.
- Good: The reason, timestamp, original dimensions, and business-event correlation remain available for diagnosis.
- Good: Valid peers in the claimed portion of a malformed business event cannot produce a partial projection.
- Good: Transient failures retain the existing all-or-nothing retry behavior.
- Good: Deleting a worker cannot race a `worker_stats` insert into a foreign-key failure, and historical team
  attribution can still materialize.
- Bad: A quarantined event leaves one or more materialized counters incomplete until an operator accepts or repairs
  the discrepancy.
- Bad: A validator defect can classify valid data as permanent. The bounded reason catalogue and quarantine telemetry
  make that decision visible, while recovery still requires review.
- Bad: Business events can span claim batches. Grouping applies to the rows visible in one claim, so emitters should
  keep related statistic groups smaller than the configured batch size.

## Pros and Cons of the Options

### Retry every projection failure indefinitely

- Good: No event is set aside without eventually projecting or receiving an operator intervention.
- Bad: One row whose data cannot change blocks all later work and repeats the same transaction cost indefinitely.
- Bad: A repeated rollback looks like activity unless success telemetry is recorded only after commit.

### Mark invalid events applied or delete them

- Good: The queue continues without adding quarantine state or a new queue predicate.
- Bad: Marking a row applied claims a projection occurred when it did not.
- Bad: Deletion removes the evidence needed to explain incomplete materialized counters and to design a repair.

### Quarantine only the individual invalid row

- Good: Every independently valid peer can still project.
- Bad: One logical business event can become partially visible even when its rows were emitted and intended together.

## Confirmation

`tests/unit/test_kudos_counter_fold.py::TestPoisonEventIsolation` fixes malformed-event isolation, claimed-event
grouping, missing-user handling, retained evidence, and progress for unrelated valid events.
`tests/unit/test_kudos_concurrency.py::test_worker_delete_waits_for_claimed_projection_without_fk_failure` fixes both
orders of the worker-deletion race and the surviving team attribution. `tests/unit/test_kudos_safety.py` verifies that
the migration is repeatable and that its drainable partial index excludes quarantined rows.

## More Information

This decision amends the queue membership and failure policy in
[ADR 3](0003-single-serialized-projector.md) and the hard-deletion behavior in
[ADR 7](0007-accounting-foreign-key-policy.md).
