---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Estimate text queue wait from the request's eligible workers

## Context and Problem Statement

The text status route reports `queue_position` and `wait_time` for a waiting request, and the free output allowance
(ADR 0037) needs the same kind of figure before a request is created. Both were computed from horde-wide totals: the
whole text queue, the whole thread count and the fleet's average speed per job. A request for a model that one idle
worker serves was reported as waiting behind the backlog of every other model, and a request on a fast worker was
estimated at the fleet average.

Chat requests share the text queue (ADR 0019) and are served only by workers that opted into chat jobs, so a horde-wide
count also mixes requests that a given worker could never take.

What should the queue estimate for a text request count, and at what speed?

## Decision Drivers

- The estimate should describe the request's own path: the workers that could serve it and the work they have ahead.
- A fast worker should shorten the estimate for its requests.
- The status route and the allowance must read one figure, so a client is never told two different waits.
- The estimate must come from cached state on the status path, which is polled often.

## Considered Options

- Horde-wide queue, threads and average speed.
- The eligible workers' threads with the queue filtered by model name, at the fleet average speed.
- The eligible workers' threads and measured speed with the queue filtered by what each eligible worker could take.

## Decision Outcome

Chosen option: "The eligible workers' threads and measured speed with the queue filtered by what each eligible worker
could take".

The eligible workers for a request are those whose declarations and measured speed pass the eligibility check used
by the pop: model, chat opt-in, length and context limits, slow-worker and trust settings, and the speed gate of
ADR 0038. Their availability is measured when the request is created and cached for a short time for the status
polls that follow.

A queued request ahead of this one counts as competing when at least one of the eligible workers could take it,
judged per worker from the fields the stored queue carries: its models, whether it is a chat request, and its
`max_length` and `max_context_length` against that worker's text or chat limits. A chat request therefore does not
count against a plain text request whose eligible workers do not take chat jobs.

The throughput is the sum over the eligible workers of their measured tokens per second times their threads. A worker
that has not recorded a speed yet counts at the assumed 10 tokens per second that ADR 0038 sets for such a worker. When fewer jobs are queued than
there are threads, the throughput is scaled to the threads the jobs occupy, since a single job runs on one thread.

The estimated wait is the competing output ahead, divided by that throughput. The status route reports it as
`wait_time` for the work up to and including the request, and `queue_position` as the number of competing requests
ahead. The allowance reads the same estimate for the work ahead of the request alone. When no thread is eligible the
status route falls back to the horde-wide figures, and the allowance takes its floor.

### Consequences

- A request to an idle model reports a short wait however deep the queues of other models are.
- A request served by fast workers reports a shorter wait than the same queue on slow ones.
- The reported wait and the allowance decision describe the same situation.
- Estimates depend on measured speed and cached availability, so they change as workers complete jobs and as the
  availability cache refreshes.
- Image status estimates are unchanged.

### Limitations

The estimate counts queued work only. With every eligible thread busy and nothing queued it does not report a wait, so it
understates the wait by up to one job per thread. The measured speed it divides by is the lease-time speed (the
speed that admitted the running jobs), which biases the estimate the other way when a worker slows under load. The
shadow records of the speed gate (ADR 0041) and the `horde.text.queue_wait_seconds` histogram, which measures the
wait a request had from creation to assignment, give the data for revisiting both.

## Pros and Cons of the Options

### Horde-wide queue, threads and average speed

- Good, because it reads three cached totals.
- Bad, because the figure describes the fleet, not the request.
- Bad, because it counts requests the request's workers could never take.

### Eligible threads with the queue filtered by model name, at the fleet average speed

- Good, because it removes the backlog of unrelated models.
- Bad, because a chat request counts against a text request on the same model even when the overlapping workers
  do not take chat jobs.
- Bad, because the fleet average speed misstates the wait on fast or slow workers.

### Eligible threads and measured speed with the queue filtered per eligible worker

- Good, because it counts only work that competes for the same threads, at the speed of those threads.
- Good, because the allowance and the status route share it.
- Bad, because the per-worker check reads more fields from the stored queue, which the queue cache carries for it.

## Confirmation

`tests/unit/test_text_worker_eligibility.py` pins the per-worker competing predicate (model overlap, chat request
against a worker without chat, a request over a worker's `max_length`, chat limits applying in place of text limits,
a chat-only worker against a text request), the competing work read from the stored queue, the threshold speed for a
worker without samples, and that the status route uses the eligible speed over the horde average.
`tests/unit/test_kobold_text_wp.py` pins the shared wait function. `docs/explanation/request_feasibility_and_queue_pressure.md`
explains the estimate and `docs/reference/chat_requests.md` records the status fields.

## More Information

ADR 0037 reads this estimate for the free output allowance. ADR 0038 defines the speed gate that is part of worker
eligibility. ADR 0019 places chat requests in the text queue.
