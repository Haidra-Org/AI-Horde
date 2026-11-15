---
title: "Request feasibility and queue pressure"
summary: "Interpret request progress, worker compatibility, capacity counts, queue estimates and stall warnings."
topics: [generation, requests, workers]
order: 20
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Request feasibility and queue pressure

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

A status response combines progress with estimates of worker availability and queue delay. Check `done`, `faulted`,
and `processing` first. While work is waiting, use `is_possible` to check whether the request can currently be served, the eligible-worker counts to
understand how much compatible capacity exists, and `might_stall` to decide whether the delay needs additional
explanation. `queue_position` and `wait_time` describe queued work. For an image request they cover the whole Horde's
queue. For a text request they cover the part of the queue that its eligible workers could take.

For the surrounding lifecycle, see [Job lifecycle](../haidra-assets/docs/job_lifecycle.md). The
[Workers](../haidra-assets/docs/workers.md) overview explains why independently operated workers expose different
models and capabilities.

## Why these fields can appear inconsistent

AI Horde workers choose the models, capabilities, limits, concurrency, and types of work they support. A request's
model, dimensions, context length, worker restrictions, safety requirements, and advanced controls determine which of
those workers are eligible. Each eligible worker may also have a different set of competing requests.

Status values are collected from that changing environment. Worker availability is cached for up to 60 seconds, and
a generation can start between the availability check and the progress check. A response may contain
`processing > 0` and `eligible_workers == 0`, and worker counts may change between adjacent polls. When fields seem to
conflict, lifecycle progress is the most useful current signal.

## Field guidance

### Progress fields

`waiting`, `processing`, and `finished` report how many generations are in each stage. `done` and `faulted` indicate
terminal outcomes. Once `processing` is greater than zero, a worker has accepted part of the request. Clients should
show active progress and continue polling for completion.

### `is_possible`

`is_possible` is true when a generation is already in progress or at least one recently active worker passes the known
dispatch checks for the request. Those checks include the requested model and relevant capability, safety, size, and
worker-selection constraints.

The field reports feasibility at the time of the check. Worker time is not reserved, and compatible threads may be
busy. A request can remain possible while its eligible workers take higher-priority work. When the value is false,
the request does not currently match any available worker. It may become possible later when worker
availability changes.

### Eligible workers and threads

The [worker eligibility constants](../reference/service_limits.md#worker-leases-and-eligibility) explain online
windows and speed thresholds. [Cached availability and speed measurements](../reference/operational_constants.md#queue-scans-availability-and-worker-measurements)
explain how quickly those observations can change.

`eligible_workers` and `eligible_worker_threads` apply the same known capability gates used by the feasibility check.
The first value counts matching workers, while the second sums their advertised generation concurrency. They are most
useful as an indication of how widely the request is supported.

A small value means that worker churn or competing work can have a large effect on the request. A larger value means
that more workers have the technical ability to accept it. The thread count is advertised capacity and can include
threads that are already occupied. The fields do not report current occupancy or reservations.

### Queue position and wait time

For an image request, `queue_position` reflects the request's priority in the horde-wide queue, and `wait_time`
estimates delay from the work queued ahead and the throughput of the horde's active worker threads. A request
supported by only a few workers may wait longer than this estimate. It may also start sooner when its compatible
workers have little competing work.

For a text or chat request, both values count only the work that at least one eligible worker could take, based on
models, chat opt-in and length limits. `queue_position` counts the competing requests ahead. To calculate `wait_time`,
the Horde:

- Adds the queued work up to and including the current request.
- Divides that work by the eligible workers' measured speed across `eligible_worker_threads`.
- Counts only as many threads as queued generations when there are fewer generations than threads.

A request for a lightly queued model therefore gets a short estimate, however long the horde's queue is. The free
output allowance uses the same calculation for a request being created. It leaves out the request's own output,
because that output is not queued yet.

With zero eligible threads there is not a throughput value to divide by, so both values use the Horde-wide
calculation. `is_possible` and the thread count report that case.

Both are estimates, and clients should label `wait_time` as one. Each eligible worker has its own candidate queue.
An exact position that accounts for compatibility would require a separate scheduling calculation for every
eligible worker. The candidate queues overlap without being identical, and they change as workers and requests
arrive. The per-worker check is an approximation of those queues. A worker that serves several models can still pick
up work for its other models before this request.

### `might_stall`

`might_stall` indicates sustained competition for the workers that can serve a waiting request. It applies only while
the request does not have any generations in progress. The calculation compares newly arrived work ahead of the
request with work completed by eligible workers. The observation window is split into two halves, and the value is
true only when all of these conditions hold:

- In each half, the arriving work is strictly greater than the capacity returned by eligible workers.
- Each half contains a complete replacement wave. That is, eligible workers returned at least as many batches as
  they have threads.
- Every observed dispatch opportunity went to work ahead of this request. A dispatch opportunity is a pop by an
  eligible worker.
- The batches of that preceding work still occupy all of the compatible threads.

A backlog that existed before the window cannot make the value true. Neither can a burst confined to one half.

The value stays false when events are missing or capabilities change. It also stays false when a bridge's transient
priority has not yet led to an observable pop. How the inputs are measured is under
[Stall signal inputs](../reference/request_scheduling.md#stall-signal-inputs).

A true value is a reason to set expectations with the user or offer alternatives. The request may still start on the
next worker cycle. A false value can mean that new work is not persistently arriving faster than workers clear it. It
can also mean that the service has not yet observed enough comparable work. It does not promise prompt assignment.
History from a worker's old model, bridge, or softprompt state is ignored. The signal remains false while a generation
is processing because active progress is more relevant at that point.

## Common response combinations

| Observed state | Suggested interpretation |
| --- | --- |
| `done` or `faulted` | Follow the normal result or error flow for a terminal request. |
| `processing > 0` | Show active progress and continue polling, even when the eligible-worker count is low or zero. |
| `waiting > 0` and `is_possible == false` | The request cannot currently be served by any recently observed worker. Keep polling while the request is live, or offer cancellation and a replacement with different constraints. |
| `waiting > 0`, `is_possible == true`, and `might_stall == true` | The request is technically supported, with enough current pressure to make further delay plausible. Present a cautious status and treat the ETA as general guidance. |
| `waiting > 0`, `is_possible == true`, and `might_stall == false` | The request is supported, but the signal may lack enough evidence. Continue polling and present the ETA as approximate. |

## Recommendations for clients

- Keep polling until the request reaches a terminal state, expires, is cancelled, or reaches a timeout chosen by the
  client.
- Use several consecutive polls before changing a user-facing warning. This prevents a worker check-in or cache
  refresh from causing the interface to flicker.
- Use the eligible-worker fields to add context to the wait estimate. The response does not include current
  occupancy, so free slots cannot be calculated from it.
- When `might_stall` is true, explain that compatible capacity is limited or under pressure. Allow the request to keep
  running unless the user or the client's own policy decides otherwise.
- Offer a replacement with different constraints only when the application can explain the effect. A more widely
  served model or a relaxed optional worker restriction may increase the eligible pool.
- Accept responses that omit the newer capacity and pressure fields. This supports older deployments. Continue to
  ignore additional fields that a later API version may introduce.
- Keep product timeouts separate from `wait_time`. The service estimate can inform the interface, while the client
  owns its retry, timeout, and cancellation policy.

Suitable user-facing messages might include:

- `is_possible == true`, `might_stall == false`: "Waiting for an available worker."
- `might_stall == true`: "Compatible worker capacity is limited. This request may take longer than the current
  estimate."
- `is_possible == false`: "No active worker currently supports this request. It can start if a compatible worker
  becomes available."

## Server measurements

For assignment-pressure telemetry, Redis retention and shadow forecast validation, see
[Request scheduling telemetry and forecasts](../reference/request_scheduling.md).
