---
title: "Request scheduling telemetry and forecasts"
summary: "Assignment-pressure inputs, availability telemetry, shadow forecast retention and validation criteria for server maintainers."
topics: [generation, requests, workers, operations]
order: 105
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Request scheduling telemetry and forecasts

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [operations](../topics.md#operations), [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

This reference describes the server's assignment-pressure observations and shadow scheduling forecasts, including
Redis retention and validation criteria. Shadow forecasts are internal measurements; they add no fields to the
public status response. For interpreting public status fields, see
[Request feasibility and queue pressure](../explanation/request_feasibility_and_queue_pressure.md).

## Stall signal inputs

Incoming work uses the same normalized work units as completed batches. It adds two parts: the still-queued remainder
of recent compatible arrivals, and their batches already assigned to eligible workers. Cancelled work that was not
assigned is therefore not counted as continuing pressure. The observation window and event retention are finite.
Every candidate inside the observation window is evaluated. The scan is not cut off at a fixed request count.

## Availability and pressure telemetry

OpenTelemetry exports one assignment-pressure sample when the shared 60-second availability cache is populated.
It does not export a sample on every client poll. The only labels on `horde.request.assignment_pressure.samples` are
the bounded generation type, evidence and `might_stall` labels. Companion histograms expose dispatch opportunities,
lost opportunities, returned capacity, active preceding dispatches, and arriving versus returned normalized work.
Request, worker, model and prompt identifiers are left out on purpose. This keeps cardinality bounded and limits how
much clients can influence the metrics.

`horde.worker_availability.cache_lookups` counts each availability cache read. `horde.outcome` is `hit` for a usable
entry, `absent` when no entry exists, `generation_mismatch` when a text worker declaration changed after the entry was
written, and `malformed` when the entry cannot be parsed. `horde.gentype` and the boolean `horde.chat` are the only
other labels. Seeding the cache when a request is created is not a read and is not counted.

## Shadow scheduling forecasts

AI Horde calculates a shadow forecast at the first check or status call that runs while a request is waiting. This
supports a future response with separate p50 and p90 remaining-time values for first worker assignment and full
completion. The current API response stays unchanged while the forecast is measured against production outcomes.

The first version of the shadow estimator, `compatible-queue-v1`, uses normalized queue work, recent horde-wide
throughput, and the compatible thread count. Work belonging to the request is excluded from the first-assignment
calculation and included in the completion calculation. The provisional `p90` value adds the larger of 60 seconds or
50% of the `p50` value. It is a candidate upper estimate. It is not yet an empirical 90th percentile, so it cannot
be published under that name. The calculation uses the global queue for every request. This includes text requests,
whose `wait_time` counts only the compatible part of the queue. The reason is that compatible workers do not share a
single candidate queue. The validation loop measures whether later empirical calibration can support real percentile
fields.

### Event delivery and retention

Forecast and outcome events pass through a bounded local queue before a background thread stores them in Redis. This
keeps Redis latency and failures away from status, worker-submit, cancellation, and cleanup responses. If the queue is
full or an event cannot be processed, the event is dropped and a monitoring counter records the reason.

Redis keeps one short-lived hash per request. Atomic first-write claims preserve the earliest forecast and prevent two
Horde API replicas from recording the same validation twice. Events can arrive in either order, so a completion or
expiry observed before its forecast can still be paired when the forecast arrives. A request refresh extends the
record's lifetime far enough to include the current expiry time.

Cancelled requests are excluded from completion and stall validation because cancellation prevents those outcomes
from being observed. A start estimate can still be evaluated when assignment was observed before cancellation.
Monitoring receives durations and bounded labels. Request and worker identifiers remain in Redis until the temporary
record expires.

### Forecast validation

Accuracy is calculated separately for image and text requests, and separately for first assignment and completion.
Each of those four series uses a rolling 24-hour window with at least 10,000 paired forecasts. At least 80% of p50
values must fall within 60 seconds or 50% of the observed duration, whichever is larger. The 90th percentile of
absolute p50 error must remain within 120 seconds, and p90 coverage must remain between 85% and 95%. Monitoring reports
`HordeRequestEstimatorPromotionApproved` after all four series hold those levels for 24 hours. A changed estimator name
collects a separate series and is evaluated on its own results.

### Stall prediction validation

The shadow stall calculation uses the same request snapshot. It predicts an unstarted expiry when compatible capacity
is unavailable or the p90 first-assignment forecast reaches the request expiry. An observed stall is an expiry before
the first real worker assignment. Evaluation requires 10,000 predictions and 500 observed stalls for both image and
text, with at least 60% precision, 80% recall, and a false-positive rate no greater than 10%. Monitoring reports
`HordeRequestStallSignalPromotionApproved` after those levels hold for 24 hours.

## Code map

| Responsibility | File | Symbol |
| --- | --- | --- |
| Status field schema | `horde/apis/models/v2.py` | `Models.response_model_wp_status_lite` |
| Status assembly | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.get_status` |
| Matching and availability cache | `horde/database/functions.py` | `get_worker_availability_for_request` |
| Assignment pressure | `horde/request_scheduling.py` | `calculate_assignment_pressure`, `get_request_assignment_pressure` |
| Shadow forecast calculation | `horde/request_scheduling.py` | `calculate_scheduling_forecast` |
| Event queue and Redis storage | `horde/request_scheduling.py` | `_enqueue_scheduling_event`, `_process_scheduling_event` |
| Outcome pairing and retention | `horde/request_scheduling.py` | `_finalize_scheduling_validation`, `_extend_state_expiry` |

## Tests

`tests/unit/test_request_scheduling.py` covers these behaviors:

| Behavior | Test |
| --- | --- |
| Sustained pressure | `test_sustained_preceding_arrivals_outpacing_returns_might_stall` |
| Existing backlog and finite bursts | `test_finite_preexisting_backlog_never_counts_as_new_arrival_pressure`, `test_one_finite_arrival_burst_does_not_show_sustained_rate_pressure` |
| Changed worker state | `test_history_from_an_old_worker_state_is_ignored` |
| Observation storage | `test_dispatch_and_capacity_return_observations_round_trip_through_redis` |
| Separate assignment and completion forecasts | `test_calculate_scheduling_forecast_separates_start_and_completion` |
| Forecast and outcome pairing | `test_stored_forecast_is_paired_with_start_and_completion`, `test_terminal_observation_can_arrive_before_forecast` |
| Cancellation exclusion | `test_cancellation_excludes_terminal_validation` |
| Backend failure isolation | `test_backend_failure_isolated_from_caller` |

The promotion thresholds describe monitoring criteria. These unit tests cover forecast production and outcome
pairing; they do not validate the monitoring rules or demonstrate production accuracy.
