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
| Without an eligible worker, the request queues as `is_possible: false` and expires after 20 minutes | 859 | about 123 | 0.1% |
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

Chat requests meet the same fleet. When the gate refuses a chat request to every worker below its `max_length`, a
request over the capacity of every online chat worker for its model has no eligible worker. It waits out the chat
front's grace and gets a 503, while the model's listing shows workers serving it.

Should the gate apply to legacy text requests, how strictly should it apply to chat requests, and how is its effect
measured?

## Decision Drivers

- Chat requests should go to workers that finish them inside the lease a worker that does not stream receives.
- A chat request for a model served only by slower workers should still be served.
- Legacy text requests should keep the service they receive in production.
- The effect the gate would have on legacy text must be measurable from production, so the scope can be revisited
  on evidence.
- Metric attributes stay bounded. Request, worker and generation ids belong in logs.

## Considered Options

- Enforce the gate for chat and legacy text requests.
- Enforce the gate for chat requests and record legacy assignments it would refuse.
- Prefer capable workers for chat requests and record every over-capacity assignment.
- Enforce the gate for neither.

## Decision Outcome

Chosen option: "Prefer capable workers for chat requests and record every over-capacity assignment".

A capable worker for a chat request is an online chat worker for one of the request's models, not in maintenance or
paused, whose declared chat limits admit the request, which declares every chat feature the request requires and the
`url` or `base64` image transport when the request requires `vision`, and whose capacity is at least its `max_length`
(`capable_chat_worker_clauses`).

`TextWorker.can_generate_with_eligibility_inputs` refuses a chat request over the
worker's capacity with `lease_speed` while a capable worker is online, and admits it when none is. In the pop query,
`text_wp_speed_gate_clause` applies the same rule through a correlated `NOT EXISTS` over the capable workers, and
passes every legacy text request. The feature test is `TextArrayContainsAll`, which compiles to `@>` on PostgreSQL
and to a `json_each` anti-join on SQLite.

The status
route, worker availability and the wait estimate of ADR 0040 read the same eligibility check. Slow workers count as
capacity for legacy requests, and for chat requests that no capable worker can take. A chat request over every online
chat worker's capacity is therefore possible and is not refused as `no_eligible_worker`.

`horde.pop.lease_capacity_limited` counts only pops by a worker whose capacity is below the chat output it declares.

When a legacy text or chat job is assigned to a worker whose capacity is below its `max_length`, the generation row
stores `over_capacity_at_assignment`, the counter `horde.text.gate_shadow.assignments` is incremented and an INFO log
line records the generation, request and worker ids with the `max_length`, capacity and speed. The marker fixes the
record at assignment, since the worker's measured speed moves between pop and submit. The job's end is counted by
`horde.text.gate_shadow.outcomes` as `completed` at a stored submit, `faulted` at the submit that faults it, or
`aborted` when its lease lapses. A completed job's pop to submit time is recorded in
`horde.text.gate_shadow.service_seconds`. Every record has `horde.worker_trusted`, `horde.size_bucket` (`<=512`,
`513-1024`, `1025-2048`, `2049-4096`, `>4096`) and `horde.chat`. A fake generation is not recorded.

### Consequences

- A chat request goes to a worker that finishes it inside the base lease whenever such a worker is online.
- A chat request over the capacity of every online chat worker for its model is served by a slower worker. A worker
  that does not stream progress can run past the lease, and the job is then aborted and restarted. The outcome
  counter with `horde.chat` true measures how often.
- Legacy text requests reach the slow workers that serve them in production, and none becomes impossible for want of
  a fast enough worker.
- A legacy job over its worker's capacity can still run past the lease and be aborted and restarted. The outcome
  counter measures how often.
- The shadow records describe what enforcement would cost legacy text, by size and worker trust, without a second
  pass over historical data.
- A faster worker without a feature the request requires does not keep the request from a slower worker that has
  it, so a tool request reaches a slower worker that declares `tools`.

## Pros and Cons of the Options

### Enforce the gate for chat and legacy text requests

- Good, because one rule applies to every text job.
- Bad, because about 123 legacy requests a day would have no eligible worker and thousands would move off workers
  that complete them, to prevent a few dozen restarts a day.

### Enforce the gate for chat requests and record legacy assignments it would refuse

- Good, because every chat assignment is finishable inside the base lease.
- Good, because the cost of enforcing it for legacy text is measured continuously.
- Bad, because a chat request over every online chat worker's capacity has no eligible worker and gets a 503 while
  its model is listed as served.

### Prefer capable workers for chat requests and record every over-capacity assignment

- Good, because chat requests reach capable workers first and a model served only by slower workers stays servable.
- Good, because the over-capacity chat assignments and the legacy ones are measured with the same records.
- Bad, because the pop query evaluates a correlated subquery over the online chat workers for each chat request
  above the popping worker's capacity.
- Bad, because the two request families follow different offer rules, which the pop clause and the eligibility check
  both apply.

### Enforce the gate for neither

- Good, because no offer rule depends on measured speed.
- Bad, because a chat request that does not stream could be assigned to a worker that cannot finish it inside its
  lease, and nothing measures how often.

## Confirmation

`tests/unit/test_text_job_ttl.py` checks:

- Agreement between the pop clause and the eligibility check for chat and legacy requests, with and without a
  capable worker online.
- An offline, unavailable or under-declared faster worker does not block a chat request. A faster worker counts as
  capable only with every required feature. Vision also requires the `url` or `base64` image transport.
- An unmeasured worker uses the assumed speed. A slow worker is refused a chat request when a capable worker can
  take it, and is assigned a legacy request. The capacity-limited count uses only the chat declaration.
- Shadow records include assignment markers and counts for legacy and chat jobs, completed outcomes with service
  time, faulted outcomes counted once, and aborted outcomes. Assignments within capacity are not recorded.
- Feature containment produces the same results on PostgreSQL and SQLite.

`tests/unit/test_text_worker_eligibility.py` checks that slow workers count toward availability for legacy
requests and for chat requests beyond every online worker's capacity. They do not count for chat requests that a
capable worker can take.

`tests/integration/test_chat.py` checks assignment of a chat request beyond the only online worker's capacity,
and refusal to assign a chat request to a slower worker when a capable worker can take it.
`docs/reference/chat_requests.md` records the rule and the counters under "Job lease".

## More Information

ADR 0038 defines the lease and the speed gate. ADR 0040 reads the eligibility check this record scopes.
