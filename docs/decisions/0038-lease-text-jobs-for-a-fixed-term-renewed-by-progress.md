---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Lease text jobs for a fixed term renewed by progress

## Context and Problem Statement

When a worker pops a text job it receives a `ttl`: the seconds it has to submit before the horde treats the job as
stale, aborts it, offers it to another worker and counts the abort against the worker. ADR 0013 sets this lease for
image jobs from the estimated work. Text jobs had a flat 150 s, which a reply of several thousand tokens cannot meet
on most hardware: a 4096-token reply needs about 30 tokens per second to finish in time, and text requests accept
`max_length` up to 16384.

A longer lease is not free. A worker that pops a job and never submits holds it for the whole lease, and the request
waits that long before another worker can take it. A lease sized to the longest permitted reply at a slow worker's
speed reaches the request's own lifetime, so a handful of workers that pop and stall could absorb requests with no
retry at all.

How long should a text job's lease be, and how should long replies become serviceable without that exposure?

## Decision Drivers

- A stalled or disconnected worker must lose the job within a short, fixed time.
- A worker that is visibly producing output should keep its job for as long as the reply takes, within a bound.
- The bound must be tighter for workers the horde has no history with.
- Long replies should go to workers fast enough to finish them rather than to anyone with a longer lease.
- The rule must be observable in metrics so its effect on the fleet can be watched.

## Considered Options

- A flat lease with no renewal.
- A lease proportional to the requested output at a slow worker's speed, capped at the request lifetime.
- A flat lease renewed by progress posts under a cap, with offers gated by worker speed.

## Decision Outcome

Chosen option: "A flat lease renewed by progress posts under a cap, with offers gated by worker speed".

Every text and chat job starts with a lease of `TEXT_JOB_LEASE_SECONDS` (150 s) from the pop. Of that,
`TEXT_JOB_PREPARATION_SECONDS` (30 s) is set aside for reaching the job and processing its prompt.

A chat job whose worker streams progress renews its lease. Each progress post that adds output moves the end of the
lease to 150 s after the post. The lease never extends beyond a cap measured from the pop:
`TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS` (600 s) for a worker owned by a trusted user and
`UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS` (200 s) otherwise. A post that repeats the last one does not renew, and a post
after the lease has lapsed does not revive it. Posts past the cap are accepted and relayed to the client but do not
extend the lease. The lease state lives with the stream lease in Redis; a granted renewal writes the new `job_ttl` to
the generation row.

The pop offers a job to a worker only when the worker's measured speed can produce the job's `max_length` inside the
base lease after the preparation allowance, that is when `speed × 120 s` is at least `max_length`. A worker that has
not recorded a speed yet is taken to run at `UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND` (10 tokens per second) until
its completed jobs record a measured speed. The same rule
is applied in the pop query and in the eligibility check that the status route and ADR 0040 use, so a worker that
cannot be offered a job is not counted as capacity for it either.

A job past its lease is aborted as stale and restarted. The worker's submit is then refused with `AbortedGen`, whose
message states the lease the job had, and the abort counts toward the worker's maintenance threshold.

Three counters in `horde/metrics.py` expose the rule: `horde.text.lease_renewals` (granted or at cap, by worker
trust), `horde.text.stale_aborts` (by worker trust) and `horde.pop.lease_capacity_limited` (pops by a worker whose
speed caps the output it is offered below a length it declares).

### Consequences

- A worker that stalls loses the job within 150 s whatever it was asked for.
- A streaming chat worker can finish a reply of any permitted length within its cap: about 11 tokens per second for
  16384 tokens at the trusted cap.
- A plain text job and a non-streamed chat job keep the flat 150 s; they are finishable because they are offered only
  to workers fast enough for them.
- A slow worker is offered shorter jobs than it declares. The gap is visible in `horde.pop.lease_capacity_limited`.
- A new worker is treated as running at 10 tokens per second and so is offered up to 1200 tokens until it records
  its speed; the assumption is replaced after its first completed jobs.
- A request asking for more than a worker's capacity waits for a faster worker; a worker measured at 10 tokens per
  second is offered up to 1200 tokens, one at 40 tokens per second up to 4800.

## Pros and Cons of the Options

### A flat lease with no renewal

- Good, because exposure to a stalled worker is fixed and small.
- Bad, because a reply longer than 150 s of generation can never be served, whatever the worker's speed.

### A lease proportional to the requested output at a slow worker's speed

- Good, because every eligible worker can finish every job it is offered.
- Bad, because the lease for a long reply reaches the request's lifetime, so a worker that pops and stalls holds the
  request until it expires and there is no retry.
- Bad, because the exposure grows with the request size an attacker chooses.

### A flat lease renewed by progress posts under a cap, with offers gated by worker speed

- Good, because renewal is earned by delivered output, which a stalled worker cannot show.
- Good, because the cap bounds the exposure, and the bound is smaller for workers without a trust record.
- Good, because long replies are routed to the workers that can finish them.
- Bad, because renewal needs progress posts, so only streaming chat jobs can exceed the base lease.
- Bad, because a job renewed to the cap is held longer than 150 s if the worker stalls late; the cap limits that.

## Confirmation

`tests/unit/test_text_job_ttl.py` pins the base lease, the renewal rule at both caps, the at-cap and lapsed outcomes,
the database and Redis writes with their counters, the progress route (a retry does not renew, a new delta does), the
stale-abort counter and the speed gate for measured and unmeasured workers, including the agreement between the pop
query and the eligibility check. `tests/unit/test_chat_progress.py` pins the stream lease fields and their extension.
`docs/reference/chat_requests.md` records the public rule under "Job lease" and `docs/how-to/serve_chat_jobs.md`
tells worker authors how progress posts renew it.

## More Information

ADR 0013 sets the image job lease. ADR 0024 defines the progress posts that renew this lease. ADR 0040 uses the same
speed gate when it selects the workers eligible for a request.
