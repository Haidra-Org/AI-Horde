---
title: "Request feasibility and queue pressure"
summary: "Practical guidance for using request progress, worker compatibility, capacity counts, queue estimates, and stall warnings in API clients."
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

A request status draws on several parts of the scheduler. Check `done`, `faulted`, and `processing` first. While work
is waiting, use `is_possible` to check whether the request can currently be served, the eligible-worker counts to
understand how much compatible capacity exists, and `might_stall` to decide whether the delay needs additional
explanation. `queue_position` and `wait_time` provide useful context for the horde as a whole.

For the surrounding lifecycle, see [Job lifecycle](../haidra-assets/docs/job_lifecycle.md). The
[Workers](../haidra-assets/docs/workers.md) overview explains why independently operated workers expose different
models and capabilities.

## Why these fields can appear inconsistent

AI Horde workers choose the models, capabilities, limits, concurrency, and types of work they support. A request's
model, dimensions, context length, worker restrictions, safety requirements, and advanced controls determine which of
those workers are eligible. Each eligible worker may also see a different set of competing requests.

Status values are collected from that changing environment. Worker availability is cached briefly, and a generation
can start between the availability check and the progress check. A response may contain `processing > 0` and
`eligible_workers == 0`, and worker counts may change between adjacent polls. When fields seem to conflict, lifecycle
progress is the most useful current signal.

## Field guidance

### Progress fields

`waiting`, `processing`, and `finished` report how many generations are in each stage. `done` and `faulted` indicate
terminal outcomes. Once `processing` is greater than zero, a worker has accepted part of the request. Clients should
show active progress and continue polling for completion.

### `is_possible`

`is_possible` is true when a generation is already in progress or at least one recently active worker passes the known
dispatch checks for the request. Those checks include the requested model and relevant capability, safety, size, and
worker-selection constraints.

The field reports feasibility at the time of the check. Worker time remains unreserved, and compatible threads may be
busy. A request can remain possible while its eligible workers are selecting higher-priority work. When the value is
false, the service has found no current route to execution. The request may become possible later when worker
availability changes.

### Eligible workers and threads

`eligible_workers` and `eligible_worker_threads` apply the same known capability gates used by the feasibility check.
The first value counts matching workers, while the second sums their advertised generation concurrency. They are most
useful as an indication of how widely the request is supported.

A small value means that worker churn or competing work can have a large effect on the request. A larger value means
that more workers have the technical ability to accept it. The thread count is advertised capacity and can include
threads that are already occupied. Current occupancy and reservations are outside the scope of both fields.

### Queue position and wait time

`queue_position` reflects the request's priority in the horde-wide queue. `wait_time` estimates delay from aggregate
horde throughput. Clients can use these values to describe general queue conditions and should label `wait_time` as an
estimate.

A request supported by only a few workers may wait longer than the horde-wide estimate. It may also start sooner when
its compatible workers have little competing work. Each eligible worker sees its own candidate queue, while
`queue_position` represents the global ordering.

Producing a compatibility-specific position would require a separate scheduling calculation for every eligible
worker. Their candidate queues overlap without being identical, and they change as workers and requests arrive. The
API keeps the queue values horde-wide and supplies compatible-capacity fields alongside them.

### `might_stall`

`might_stall` is available when work is waiting and none of the request is processing. It compares newly arrived work
that the scheduler places ahead of this request with capacity returned by workers that can currently serve it. The value
becomes true only when arrivals strictly outpace returned eligible capacity in each half of the observation window,
both halves contain a complete replacement wave, every observed opportunity goes to preceding work, and that work
still occupies the compatible thread pool. Pre-existing backlog and a burst confined to one half cannot trigger it.

Incoming work is measured in the same normalized work units as completed batches. It combines the still-queued
remainder of recent compatible arrivals with their batches already assigned to eligible workers. Consequently,
cancelled unassigned work is not treated as continuing pressure. The observation window and event retention are
finite. Missing events, capability changes, and transient bridge priorities that have not yet produced an observable
pop all make the signal fail clear. Every candidate inside the observation window is evaluated; the scan is not
truncated by a fixed request count.

A true value is a reason to set expectations with the user or offer alternatives. The request may still start on the
next worker cycle. A false value can mean either that arrival demand is not persistently exceeding clearance or that
the service has not yet observed enough comparable work; it is not a promise of prompt assignment. History from a
worker's old model, bridge, or softprompt state is ignored. The signal remains false while a generation is processing
because active progress is more relevant at that point.

## Common response combinations

| Observed state | Suggested interpretation |
| --- | --- |
| `done` or `faulted` | Follow the normal result or error flow for a terminal request. |
| `processing > 0` | Show active progress and continue polling, even when the eligible-worker count is low or zero. |
| `waiting > 0` and `is_possible == false` | No recently observed worker can currently serve the request. Keep polling while the request is live, or offer cancellation and a replacement with different constraints. |
| `waiting > 0`, `is_possible == true`, and `might_stall == true` | The request is technically supported, with enough current pressure to make further delay plausible. Present a cautious status and treat the ETA as general guidance. |
| `waiting > 0`, `is_possible == true`, and `might_stall == false` | The request is supported, but the signal may lack enough evidence. Continue polling and present the ETA as approximate. |

## Recommendations for clients

- Keep polling until the request reaches a terminal state, expires, is cancelled, or reaches a timeout chosen by the
  client.
- Use several consecutive polls before changing a user-facing warning. This prevents a worker check-in or cache
  refresh from causing the interface to flicker.
- Use the eligible-worker fields to add context to the horde-wide estimate. Free-slot calculations require current
  occupancy data, which the response omits.
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

## AI Horde maintainer guidance

### Shadow scheduling forecasts

AI Horde calculates a shadow forecast when a check or status call first finds a request waiting. This supports a
future response with separate p50 and p90 remaining-time values for first worker assignment and full completion. The
current API response remains unchanged while the forecast is measured against production outcomes.

The first version of the shadow estimator, `compatible-queue-v1`, uses normalized queue work, recent horde-wide
throughput, and the compatible thread count. Work belonging to the request is excluded from the first-assignment
calculation and included in the completion calculation. The provisional `p90` value adds the larger of 60 seconds or
50% of the `p50` value. It is a candidate upper estimate, not yet an empirical 90th percentile and therefore is not
eligible for public promotion under that name. The global queue remains part of the calculation because compatible
workers do not share a single candidate queue. The validation loop measures whether later empirical calibration can
support real percentile fields.

Forecast and outcome events pass through a bounded local queue before a background thread stores them in Redis. This
keeps Redis latency and failures away from status, worker-submit, cancellation, and cleanup responses. If the queue is
full or an event cannot be processed, the event is dropped and a monitoring counter records the reason.

Redis keeps one short-lived hash per request. Atomic first-write claims preserve the earliest forecast and prevent two
application instances from recording the same validation twice. Events can arrive in either order, so a completion or
expiry observed before its forecast can still be paired when the forecast arrives. A request refresh extends the
record's lifetime far enough to include the current expiry time.

Cancelled requests are excluded from completion and stall validation because cancellation prevents those outcomes
from being observed. A start estimate can still be evaluated when assignment was observed before cancellation.
Monitoring receives durations and bounded labels; request and worker identifiers remain in Redis until the temporary
record expires.

Accuracy is calculated separately for image and text requests, and separately for first assignment and completion.
Each of those four series uses a rolling 24-hour window with at least 10,000 paired forecasts. At least 80% of p50
values must fall within 60 seconds or 50% of the observed duration, whichever is larger. The 90th percentile of
absolute p50 error must remain within 120 seconds, and p90 coverage must remain between 85% and 95%. Monitoring reports
`HordeRequestEstimatorPromotionApproved` after all four series hold those levels for 24 hours. A changed estimator name
collects a separate series and is evaluated on its own results.

The shadow stall calculation uses the same request snapshot. It predicts an unstarted expiry when compatible capacity
is unavailable or the p90 first-assignment forecast reaches the request expiry. An observed stall is an expiry before
the first real worker assignment. Evaluation requires 10,000 predictions and 500 observed stalls for both image and
text, with at least 60% precision, 80% recall, and a false-positive rate no greater than 10%. Monitoring reports
`HordeRequestStallSignalPromotionApproved` after those levels hold for 24 hours.

### Implementation references

The serialized field descriptions live in `horde/apis/models/v2.py`, `Models.response_model_wp_status_lite`. Status
assembly lives in `horde/classes/base/waiting_prompt.py`, `WaitingPrompt.get_status`. Worker matching and its
short-lived cache live in `horde/database/functions.py`,
`get_worker_availability_for_request`. Shadow forecast calculation, temporary storage, and outcome pairing live in
`horde/request_scheduling.py`, alongside the centralized assignment-pressure calculation. These implementations
remain the authority for exact behavior.
