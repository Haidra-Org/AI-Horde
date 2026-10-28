---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Settle requests after cancellation and repeated faults

## Context and Problem Statement

A request ends in one of three ways: its results are stored, its client cancels it, or its jobs fault often enough
that the horde gives up. Cancellation also happens without the client when the stale-request cleanup expires a
request. Each ending has to leave the status route, the queue and the worker accounting in a consistent state, and the
clients that poll `done` need a reading they can act on.

Three gaps had to be closed. A cancelled request kept reading `done: false` until it expired, although nothing more
would ever arrive. A job that faulted after its request had been cancelled was put back in the queue, creating work
for a request nobody was waiting on. And a request whose jobs kept faulting was only faulted by the periodic
maintenance pass, so a client polled a doomed request until that pass ran, and two submits arriving together could
each count the fault.

How does a request settle after a cancellation or after repeated faults, and what does its status read while that
happens?

## Decision Drivers

- `done: true` must mean that nothing more will arrive for the request.
- Work already in flight at a cancellation should be kept, not thrown away.
- No work should be created for a request that has been cancelled.
- A request with repeated faults should fault as soon as the fact is known, and exactly once.
- The rules must be the same for text, chat and image requests, with the differences stated.

## Considered Options

- Keep `done` false after a cancellation until the request expires, and fault only in the maintenance pass.
- Settle on cancellation and fault at the second faulted job, with compare-and-set state changes.

## Decision Outcome

Chosen option: "Settle on cancellation and fault at the second faulted job, with compare-and-set state changes".

Cancellation, by the client's DELETE or by the stale-request cleanup, records a cancelled outcome and sets the
request's remaining jobs to zero. Jobs already in flight finish and store their results. The request reads
`done: false` while any job is still processing and `done: true` once the last one settles. Partial results are
retained and returned. A job that faults after the cancellation is not put back in the queue.

A request faults when its second job faults (`FAULTED_GENERATIONS_LIMIT` is 2). The fault is applied when the submit
that reports the second faulted job arrives, in the same transaction, instead of waiting for the maintenance pass. The
state change is a compare-and-set on the request row, so a submit and the maintenance pass that observe the same fault
apply it once. A faulted request reads `faulted: true` and `done: true`.

An image request whose last job faulted and which has nothing left to do reads `faulted: true`. Text and chat faults
always restart their job, so this case does not arise for them. Cancellation of a streamed chat request also settles
each in-flight generation on the output its worker posted; ADR 0027 records that pricing rule.

### Consequences

- Pollers can stop at `done: true` in every case; a cancelled request reaches it as soon as its in-flight jobs end.
- A cancelled request never gets a new job assigned, so workers do not spend time on abandoned requests.
- A client sees the fault of a doomed request on the next status poll rather than after the next maintenance pass.
- Worker abort accounting is unchanged: each faulted job still counts against the worker that held it.

## Pros and Cons of the Options

### Keep `done` false until expiry, fault only in the maintenance pass

- Good, because it needs no change to the status route or the submit path.
- Bad, because `done: false` on a cancelled request promises results that will not arrive.
- Bad, because a fault after cancellation requeues the job and a worker generates for nobody.
- Bad, because two submits can both count the second fault before the pass runs.

### Settle on cancellation and fault at the second faulted job

- Good, because every terminal state is reached as soon as it is known and reported truthfully.
- Good, because compare-and-set makes the terminal transition happen once under concurrent submits and passes.
- Bad, because `done: true` after a cancellation can come with fewer results than requested; the status shows the
  cancelled outcome and the results that exist.

## Confirmation

`tests/integration/test_generation_lifecycle_races.py` pins that a worker fault updates the loaded request and the
abort statistic once, that stale cleanup cannot requeue a job claimed after it loaded, and that a maintenance
cancellation settles after in-flight work, retains partial results and never requeues. `tests/integration/test_maintenance_threads.py`
pins the maintenance pass against these rules. `docs/reference/chat_requests.md` records the public reading of
`done` and `faulted` for chat requests, which text and image requests share.

## More Information

ADR 0027 records how a cancelled streamed chat generation is priced. ADR 0038 records when a job is aborted as stale,
which is one source of the faults counted here.
