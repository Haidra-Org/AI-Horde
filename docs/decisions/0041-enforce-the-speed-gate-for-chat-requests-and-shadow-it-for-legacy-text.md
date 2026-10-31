---
status: accepted
date: 2026-10-30
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Enforce the speed gate for chat requests and shadow it for legacy text

## Context and Problem Statement

ADR 0038 offers a text job to a worker only when the worker's measured speed produces the job's `max_length` inside
the base lease after the preparation allowance: `max_length <= floor(speed × 120 s)`. The rule keeps every assignment
finishable without progress posts, which only streaming chat jobs send.

Legacy text requests (the `/v2/generate/text/async` route without a chat payload) are served by a fleet whose slow
workers complete their jobs in production. Measured against a week of finished legacy jobs and the fleet online when
it was read, the gate would have this effect:

| Effect | Per week | Per day | Share |
| --- | --- | --- | --- |
| No eligible worker; the request queues as `is_possible: false` and expires after 20 minutes | 859 | about 123 | 0.1% |
| One worker refuses the job, another can take it | 30,478 | about 4,350 | 3.2% |

| Model | Refused / completed per week | Over 120 s per day | Over 150 s per day | p90 pop to submit | Workers (tokens/s, capacity, `max_length`) |
| --- | --- | --- | --- | --- | --- |
| Behemoth-128B | 673 / 8,375 | 25 | 1 | 105 s | 7.7, 924, 1024 and 7.5, 900, 1024 |
| Rocinante-XL-16B-v1 | 106 / 7,551 | 0 | 0 | 35 s | 5.1, 612, 8192 |
| Cydonia-24B-v4.3 | 60 / 183 | 8 | 5 | 141 s | 4.2, 504, 1024 |
| gemma-4-E4B-it | 18 / 27,439 | 0 | 0 | 42 s | 4.8, 576, 2048 |

Nearly all the shifted work is 512-token requests for Rocinante-X-12B-v1, a third of that model's traffic, which a
worker at 4.0 tokens/s (capacity 480) would be refused, leaving one thread for them.

The over-capacity jobs the gate refuses finish with p90 durations of 35 to 141 s, because stored speed understates
these workers: it is charged tokens per second of lease, depressed by preparation and short replies. The benefit is
bounded by the aborts the gate could prevent: a handful of over-capacity jobs a week run past 150 s, and stale aborts
on all gated workers combined are a few dozen a day. An abort restarts the job, so it delays rather than loses it.
The cost is about 123 requests a day with no eligible worker and up to about 1,400 a day moved off a slower worker.
Jobs are attributed to workers by model, which is exact for single-worker models and approximate otherwise, and the
capacities come from one reading of speeds that move a little between readings.

Should the gate apply to legacy text requests, and how is its effect on them measured?

## Decision Drivers

- Chat requests need every assignment to be finishable inside the lease a worker that does not stream receives.
- Legacy text requests should keep the service they receive in production.
- The effect the gate would have on legacy text must be measurable from production, so the scope can be revisited
  on evidence.
- Metric attributes stay bounded; request, worker and generation ids belong in logs.

## Considered Options

- Enforce the gate for chat and legacy text requests.
- Enforce the gate for chat requests and record legacy assignments it would refuse.
- Enforce the gate for neither.

## Decision Outcome

Chosen option: "Enforce the gate for chat requests and record legacy assignments it would refuse".

`TextWorker.can_generate_with_eligibility_inputs` refuses a chat request over the worker's capacity with
`lease_speed`, and `text_wp_speed_gate_clause` passes every legacy text request in the pop query. The status route,
worker availability and the wait estimate of ADR 0040 read the same eligibility check, so slow workers count as
capacity for legacy requests. `horde.pop.lease_capacity_limited` counts only pops by a worker whose capacity is below
the chat output it declares.

When a legacy text job is assigned to a worker whose capacity is below its `max_length`, the generation row stores
`over_capacity_at_assignment`, the counter `horde.text.gate_shadow.assignments` is incremented and an INFO log line
records the generation, request and worker ids with the `max_length`, capacity and speed. The marker fixes the record
at assignment, since the worker's measured speed moves between pop and submit. The job's end is counted by
`horde.text.gate_shadow.outcomes` as `completed` at a stored submit, `faulted` at the submit that faults it, or
`aborted` when its lease lapses; a completed job's pop to submit time is recorded in
`horde.text.gate_shadow.service_seconds`. Every record carries `horde.worker_trusted` and `horde.size_bucket`
(`<=512`, `513-1024`, `1025-2048`, `2049-4096`, `>4096`). A fake generation is not recorded.

### Consequences

- Chat assignments stay finishable inside the base lease.
- Legacy text requests reach the slow workers that serve them in production, and none becomes impossible for want of
  a fast enough worker.
- A legacy job over its worker's capacity can still run past the lease and be aborted and restarted; the outcome
  counter measures how often.
- The shadow records describe what enforcement would cost legacy text, by size and worker trust, without a second
  pass over historical data.

## Pros and Cons of the Options

### Enforce the gate for chat and legacy text requests

- Good, because one rule applies to every text job.
- Bad, because about 123 legacy requests a day would have no eligible worker and thousands would move off workers
  that complete them, to prevent a few dozen restarts a day.

### Enforce the gate for chat requests and record legacy assignments it would refuse

- Good, because chat requests get finishable assignments and legacy requests keep their service.
- Good, because the cost of enforcing it for legacy text is measured continuously.
- Bad, because the two request families follow different offer rules, which the pop clause and the eligibility check
  both carry.

### Enforce the gate for neither

- Good, because no offer rule depends on measured speed.
- Bad, because a chat request that does not stream could be assigned to a worker that cannot finish it inside its
  lease, and nothing measures how often.

## Confirmation

`tests/unit/test_text_job_ttl.py` pins the pop clause and the eligibility check agreeing for chat and legacy
requests, a slow worker refused a chat request and assigned a legacy one, the capacity-limited count reading only the
chat declaration, and the shadow records: the marker and assignment count, the completed outcome with its service
time, the faulted outcome counted once, the aborted outcome, and no record for an assignment within capacity.
`tests/unit/test_text_worker_eligibility.py` pins availability counting a slow worker for a legacy request and not
for a chat request. `docs/reference/chat_requests.md` records the rule and the counters under "Job lease".

## More Information

ADR 0038 defines the lease and the speed gate. ADR 0040 reads the eligibility check this record scopes.
