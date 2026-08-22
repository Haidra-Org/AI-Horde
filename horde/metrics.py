# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Application metrics.

Two unrelated kinds of metric live here:

* :class:`WaitressMetrics` exposes the live Waitress task-dispatcher gauges
  via the legacy ``/metrics`` endpoint.

* The module-level histogram and counter constants below are the OpenTelemetry
  metric instruments used throughout the codebase. They are created via the
  documented Logfire API (``logfire.metric_histogram`` / ``logfire.metric_counter``),
  which returns a *proxy* instrument: the real SDK instrument is materialised
  on first ``record()`` / ``add()`` call, so module-level construction is safe
  even when this module is imported before ``logfire.configure()`` runs.

  Custom histogram bucket boundaries are configured by
  :func:`horde.telemetry.init_telemetry_early` via
  ``MetricsOptions(views=histogram_views())``. See :func:`histogram_views`.

Adding a new metric is one line: pick the right section, call the matching
``_*_histogram`` / ``logfire.metric_counter`` helper, and assign to a
module-level constant. The helper auto-registers the bucket profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import logfire

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram
    from opentelemetry.sdk.metrics.view import View


class WaitressMetrics:
    task_dispatcher: Any = None

    def setup(self, td: Any) -> None:
        self.task_dispatcher = td

    @property
    def queue(self) -> int:
        return len(self.task_dispatcher.queue)

    @property
    def threads(self) -> int:
        return len(self.task_dispatcher.threads)

    @property
    def active_count(self) -> int:
        # -1 to ignore the /metrics task
        return int(self.task_dispatcher.active_count) - 1


waitress_metrics = WaitressMetrics()


# ---------------------------------------------------------------------------
# OTel histogram bucket profiles
#
# Logfire's ``metric_histogram`` wrapper does not expose
# ``explicit_bucket_boundaries_advisory``, so we attach explicit boundaries
# via SDK ``View`` objects passed into ``logfire.configure(metrics=...)``
# (see ``histogram_views`` below).
# ---------------------------------------------------------------------------

BUCKETS_SECONDS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    5.0,
    7.5,
    10.0,
    30.0,
    60.0,
    300.0,
    1800.0,
)
BUCKETS_COUNT = (0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 5000)
BUCKETS_KUDOS = (0, 1, 10, 100, 1000, 10000, 100000)
BUCKETS_REQUEST_LIFECYCLE_SECONDS = (
    0,
    1,
    2,
    5,
    10,
    15,
    20,
    30,
    45,
    60,
    90,
    120,
    180,
    240,
    300,
    450,
    600,
    900,
    1200,
    1800,
    3600,
)

REQUEST_ESTIMATE_MINIMUM_ABSOLUTE_TOLERANCE_SECONDS = 60
REQUEST_ESTIMATE_RELATIVE_TOLERANCE = 0.5


_BUCKET_REGISTRY: dict[str, tuple[float, ...]] = {}


def _seconds_histogram(name: str, description: str) -> Histogram:
    _BUCKET_REGISTRY[name] = BUCKETS_SECONDS
    return logfire.metric_histogram(name, unit="s", description=description)


def _count_histogram(name: str, description: str) -> Histogram:
    _BUCKET_REGISTRY[name] = BUCKETS_COUNT
    return logfire.metric_histogram(name, unit="1", description=description)


def _kudos_histogram(name: str, description: str) -> Histogram:
    _BUCKET_REGISTRY[name] = BUCKETS_KUDOS
    return logfire.metric_histogram(name, unit="kudos", description=description)


def _request_lifecycle_histogram(name: str, description: str) -> Histogram:
    _BUCKET_REGISTRY[name] = BUCKETS_REQUEST_LIFECYCLE_SECONDS
    return logfire.metric_histogram(name, unit="s", description=description)


def histogram_views() -> list[View]:
    """Return SDK ``View`` objects mapping each registered histogram to its
    explicit bucket boundaries. Pass into
    ``logfire.configure(metrics=logfire.MetricsOptions(views=...))``.
    """
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    return [
        View(
            instrument_name=name,
            aggregation=ExplicitBucketHistogramAggregation(boundaries=list(boundaries)),
        )
        for name, boundaries in _BUCKET_REGISTRY.items()
    ]


# --- /generate request lifecycle ---------------------------------------------
generate_duration = _seconds_histogram(
    "horde.generate.duration",
    "End-to-end duration of a generate request",
)
generate_validate_duration = _seconds_histogram(
    "horde.generate.validate.duration",
    "Duration of GenerateTemplate.validate",
)
generate_initiate_wp_duration = _seconds_histogram(
    "horde.generate.initiate_wp.duration",
    "Duration of GenerateTemplate.initiate_waiting_prompt",
)
generate_activate_wp_duration = _seconds_histogram(
    "horde.generate.activate_wp.duration",
    "Duration of GenerateTemplate.activate_waiting_prompt",
)
generate_init_wp_build_duration = _seconds_histogram(
    "horde.generate.init_wp.build.duration",
    "Duration of WaitingPrompt constructor within initiate_waiting_prompt",
)
generate_init_wp_kudos_check_duration = _seconds_histogram(
    "horde.generate.init_wp.kudos_check.duration",
    "Duration of upfront-kudos/active-workers check within initiate_waiting_prompt",
)
generate_validate_find_user_duration = _seconds_histogram(
    "horde.generate.validate.find_user.duration",
    "Duration of the shared-key/api-key user resolution within generate validate",
)
generate_validate_wp_count_duration = _seconds_histogram(
    "horde.generate.validate.wp_count.duration",
    "Duration of count_waiting_requests within generate validate",
)
generate_validate_prompt_filter_duration = _seconds_histogram(
    "horde.generate.validate.prompt_filter.duration",
    "Duration of the prompt regex suspicion check within generate validate",
)
generate_source_upload_duration = _seconds_histogram(
    "horde.generate.source_upload.duration",
    "Duration of the source image/mask object-storage uploads within image activate_waiting_prompt",
)

# --- waiting-prompt activation / kudos ---------------------------------------
wp_calculate_kudos_duration = _seconds_histogram(
    "horde.wp.calculate_kudos.duration",
    "Duration of ImageWaitingPrompt.calculate_kudos",
)
wp_kudos_model_duration = _seconds_histogram(
    "horde.wp.kudos.torch.duration",
    "Duration of KudosModel.calculate_kudos model forward pass",
)
wp_kudos_commit_duration = _seconds_histogram(
    "horde.wp.kudos.commit.duration",
    "Duration of db.session.commit() at end of calculate_kudos",
)
wp_activate_post_super_duration = _seconds_histogram(
    "horde.wp.activate.post_super.duration",
    "Duration of stable WP.activate body after super().activate()",
)
wp_activate_post_kudos_duration = _seconds_histogram(
    "horde.wp.activate.post_kudos.duration",
    "Duration of post_super body after calculate_kudos returns",
)
wp_activate_base_record_usage_duration = _seconds_histogram(
    "horde.wp.activate.base.record_usage.duration",
    "Duration of base WP.activate->record_usage (horde tax)",
)
wp_activate_base_commit_duration = _seconds_histogram(
    "horde.wp.activate.base.commit.duration",
    "Duration of trailing db.session.commit() in base WP.activate",
)
wp_activate_duration = _seconds_histogram(
    "horde.wp.activate.duration",
    "Duration of WaitingPrompt.activate inner body",
)
wp_activation_age = _seconds_histogram(
    "horde.wp.activation_age",
    "Elapsed time between WP create and activation",
)
request_time_to_first_start = _request_lifecycle_histogram(
    "horde.request.time_to_first_start",
    "Elapsed time between request creation and its first real worker assignment",
)
request_time_to_completion = _request_lifecycle_histogram(
    "horde.request.time_to_completion",
    "Elapsed time between request creation and successful completion",
)
request_time_to_expiry = _request_lifecycle_histogram(
    "horde.request.time_to_expiry",
    "Elapsed time between request creation and expiry",
)
request_outcomes = logfire.metric_counter(
    "horde.request.outcomes",
    unit="1",
    description="Terminal request outcomes used to interpret lifecycle latency",
)

# These instruments form the shadow-validation contract for scheduling
# estimators. Candidate code records paired forecasts and observations through
# the helpers below. Monitoring opens promotion only after every request type
# and milestone satisfies the sample, accuracy, calibration, and error thresholds.
request_estimate_absolute_error = _request_lifecycle_histogram(
    "horde.request.estimate.absolute_error",
    "Absolute error of a shadow request scheduling estimate",
)
request_estimate_validations = logfire.metric_counter(
    "horde.request.estimate.validations",
    unit="1",
    description="Shadow scheduling-estimate validations split by tolerance result",
)
request_estimate_quantile_coverage = logfire.metric_counter(
    "horde.request.estimate.quantile_coverage",
    unit="1",
    description="Observed coverage of a shadow scheduling estimate's p90 bound",
)
request_stall_validations = logfire.metric_counter(
    "horde.request.stall.validations",
    unit="1",
    description="Shadow stall-signal validations split by confusion-matrix outcome",
)
request_scheduling_events_dropped = logfire.metric_counter(
    "horde.request.scheduling_events_dropped",
    unit="1",
    description="Shadow scheduling events dropped before validation",
)


def record_request_estimate_validation(
    *,
    estimator: str,
    gentype: str,
    phase: str,
    predicted_p50_seconds: float,
    predicted_p90_seconds: float,
    observed_seconds: float,
) -> None:
    """Record one paired shadow estimate and observed scheduling outcome.

    The point-accuracy tolerance is the larger of 60 seconds and 50 percent of
    the observed duration. The monitoring promotion gate additionally checks
    p90 absolute error and aggregate p90 coverage.

    Args:
        estimator: Bounded version label for the candidate estimator.
        gentype: Generation type, currently ``image`` or ``text``.
        phase: Predicted milestone, currently ``start`` or ``completion``.
        predicted_p50_seconds: Candidate median duration in seconds.
        predicted_p90_seconds: Candidate 90th-percentile duration in seconds.
        observed_seconds: Actual duration in seconds.

    Raises:
        ValueError: If a duration is negative or p90 is below p50.
    """

    durations = (predicted_p50_seconds, predicted_p90_seconds, observed_seconds)
    if any(duration < 0 for duration in durations):
        raise ValueError("Request estimate durations must be non-negative")
    if predicted_p90_seconds < predicted_p50_seconds:
        raise ValueError("The p90 request estimate must be greater than or equal to p50")

    absolute_error = abs(predicted_p50_seconds - observed_seconds)
    tolerance = max(
        REQUEST_ESTIMATE_MINIMUM_ABSOLUTE_TOLERANCE_SECONDS,
        observed_seconds * REQUEST_ESTIMATE_RELATIVE_TOLERANCE,
    )
    attributes = {
        "horde.estimator": estimator,
        "horde.gentype": gentype,
        "horde.phase": phase,
    }
    request_estimate_absolute_error.record(absolute_error, attributes)
    request_estimate_validations.add(
        1,
        {
            **attributes,
            "horde.result": "within_tolerance" if absolute_error <= tolerance else "outside_tolerance",
        },
    )
    request_estimate_quantile_coverage.add(
        1,
        {
            **attributes,
            "horde.quantile": "0.9",
            "horde.result": "covered" if observed_seconds <= predicted_p90_seconds else "missed",
        },
    )


def record_request_stall_validation(
    *,
    estimator: str,
    gentype: str,
    predicted_stall: bool,
    expired_without_start: bool,
) -> None:
    """Compare one shadow stall prediction with an unstarted expiry."""

    outcome = {
        (True, True): "true_positive",
        (True, False): "false_positive",
        (False, True): "false_negative",
        (False, False): "true_negative",
    }[(predicted_stall, expired_without_start)]
    request_stall_validations.add(
        1,
        {
            "horde.estimator": estimator,
            "horde.gentype": gentype,
            "horde.result": outcome,
        },
    )

# --- pop ---------------------------------------------------------------------
pop_duration = _seconds_histogram(
    "horde.pop.duration",
    "End-to-end duration of a job_pop request",
)
pop_query_duration = _seconds_histogram(
    "horde.pop.wp_query.duration",
    "Duration of get_sorted_wp_filtered_to_worker query",
)
pop_pre_eval_duration = _seconds_histogram(
    "horde.pop.pre_eval.duration",
    "Duration of pop validate + check_in + wp_query",
)
pop_validate_duration = _seconds_histogram(
    "horde.pop.validate.duration",
    "Duration of pop validate()",
)
pop_check_in_duration = _seconds_histogram(
    "horde.pop.check_in.duration",
    "Duration of pop worker.check_in()",
)
pop_eval_duration = _seconds_histogram(
    "horde.pop.eval_loop.duration",
    "Duration of pop candidate evaluation loop",
)
pop_start_gen_duration = _seconds_histogram(
    "horde.pop.start_generation.duration",
    "Duration of WP.start_generation dispatch on a successful pop",
)
pop_candidates = _count_histogram(
    "horde.pop.candidates_evaluated",
    "Number of WaitingPrompts evaluated per pop",
)
pop_returned_jobs = _count_histogram(
    "horde.pop.returned_jobs",
    "Jobs returned to the worker per pop (0=no-match)",
)
pop_skipped = logfire.metric_counter(
    "horde.pop.skipped",
    unit="1",
    description="WPs skipped during pop, by reason",
)

# --- submit ------------------------------------------------------------------
submit_duration = _seconds_histogram(
    "horde.submit.duration",
    "End-to-end duration of a job_submit request",
)
submit_get_progen_duration = _seconds_histogram(
    "horde.submit.get_progen.duration",
    "Duration of get_progen_by_id during submit validate",
)
submit_find_user_duration = _seconds_histogram(
    "horde.submit.find_user.duration",
    "Duration of find_user_by_api_key during submit validate",
)
submit_set_gen_duration = _seconds_histogram(
    "horde.submit.set_generation.duration",
    "Duration of procgen.set_generation",
)
submit_state_handling_duration = _seconds_histogram(
    "horde.submit.state_handling.duration",
    "Duration of the gentype-specific censorship/faulted handling preceding base set_generation",
)
submit_claim_duration = _seconds_histogram(
    "horde.submit.claim.duration",
    "Duration of the compare-and-set UPDATE claiming a procgen for this submission",
)
submit_gen_kudos_duration = _seconds_histogram(
    "horde.submit.get_gen_kudos.duration",
    "Duration of procgen.get_gen_kudos within set_generation",
)
submit_record_duration = _seconds_histogram(
    "horde.submit.record.duration",
    "Duration of procgen.record",
)
submit_record_performance_duration = _seconds_histogram(
    "horde.submit.record_performance.duration",
    "Duration of worker.record_performance (the worker_performances sample insert) after the submit commit",
)
submit_wp_completion_duration = _seconds_histogram(
    "horde.submit.wp_completion.duration",
    "Duration of the wp completion check and upfront reservation release after the submit commit",
)
submit_worker_contrib_duration = _seconds_histogram(
    "horde.submit.worker_contribution.duration",
    "Duration of worker.record_contribution within procgen.record",
)
submit_wp_record_usage_duration = _seconds_histogram(
    "horde.submit.wp_record_usage.duration",
    "Duration of wp.record_usage within procgen.record",
)
submit_webhook_call_duration = _seconds_histogram(
    "horde.submit.webhook_call.duration",
    "Duration of procgen.send_webhook in submit",
)
submit_record_fulfilment_stat_duration = _seconds_histogram(
    "horde.submit.record_fulfilment_stat.duration",
    "Duration of stats.record_fulfilment (gen-stats archive insert) during submit",
)
submit_server_upload_duration = _seconds_histogram(
    "horde.submit.server_upload.duration",
    "Duration of server-side object-storage uploads during submit (b64 fallback image, shared metadata)",
)
submit_genstats_record_duration = _seconds_histogram(
    "horde.submit.genstats_record.duration",
    "Duration of the inline gen-stats archive insert during procgen.set_generation",
)
submit_commit_duration = _seconds_histogram(
    "horde.submit.commit.duration",
    "Duration of db.session.commit() at end of procgen.set_generation",
)
submit_kudos = _kudos_histogram(
    "horde.submit.kudos",
    "Kudos awarded per job submission",
)
submit_outcomes = logfire.metric_counter(
    "horde.submit.outcomes",
    unit="1",
    description="/generate/submit outcomes",
)

# --- /generate/check & /generate/status --------------------------------------
check_duration = _seconds_histogram(
    "horde.generate.check.duration",
    "End-to-end duration of a /generate/check poll",
)
status_duration = _seconds_histogram(
    "horde.generate.status.duration",
    "End-to-end duration of a /generate/status fetch",
)
check_outcomes = logfire.metric_counter(
    "horde.generate.check.outcomes",
    unit="1",
    description="/generate/check outcomes",
)

# --- webhooks ----------------------------------------------------------------
webhook_duration = _seconds_histogram(
    "horde.webhook.attempt.duration",
    "Duration of a single webhook POST attempt",
)
webhook_outcomes = logfire.metric_counter(
    "horde.webhook.outcomes",
    unit="1",
    description="Terminal webhook outcomes",
)

# --- background jobs / countermeasures / db ----------------------------------
job_duration = _seconds_histogram(
    "horde.job.duration",
    "Duration of a PrimaryTimedFunction invocation",
)
job_failures = logfire.metric_counter(
    "horde.job.failures",
    unit="1",
    description="PrimaryTimedFunction invocations that raised",
)
ip_check_duration = _seconds_histogram(
    "horde.countermeasures.ip_check.duration",
    "Duration of is_ip_safe external check",
)
db_pool_timeout = logfire.metric_counter(
    "horde.db.pool.timeout",
    unit="1",
    description="SQLAlchemy QueuePool TimeoutError occurrences",
)
kudos_applier_lag_seconds = _seconds_histogram(
    "horde.kudos.applier_lag",
    "Seconds between now and the kudos ledger applier's last fold, per applier cycle",
)
kudos_oldest_pending_seconds = _seconds_histogram(
    "horde.kudos.oldest_pending",
    "Age of the oldest unapplied kudos posting",
)
kudos_pending_rows = logfire.metric_histogram(
    "horde.kudos.pending_rows",
    unit="{posting}",
    description="Number of unapplied kudos postings",
)
kudos_active_reservations = logfire.metric_histogram(
    "horde.kudos.active_reservations",
    unit="{reservation}",
    description="Number of active kudos holds",
)
kudos_oldest_reservation_seconds = _seconds_histogram(
    "horde.kudos.oldest_reservation",
    "Age of the oldest active kudos hold",
)
kudos_applier_folded = logfire.metric_counter(
    "horde.kudos.applier.folded",
    unit="1",
    description="Ledger rows folded by the applier, by row_type (currency/stat)",
)
kudos_applier_cycles = logfire.metric_counter(
    "horde.kudos.applier.cycles",
    unit="1",
    description="Applier fold cycles run",
)
kudos_applier_phase_duration = _seconds_histogram(
    "horde.kudos.applier.phase.duration",
    "Duration of one applier cycle phase, by horde.kudos.phase",
)
kudos_applier_saturation = logfire.metric_counter(
    "horde.kudos.applier.saturation",
    unit="1",
    description="Applier ticks that exhausted the catch-up cycle bound with a full final batch",
)
kudos_floor_adjustments = logfire.metric_counter(
    "horde.kudos.floor_adjustments",
    unit="1",
    description="Floor-adjustment postings the applier emitted (currency minted by the balance floor)",
)
kudos_floor_adjustments_created = logfire.metric_counter(
    "horde.kudos.floor_adjustments.kudos",
    unit="kudos",
    description="Total kudos created by floor adjustments",
)
kudos_reservations_rejected = logfire.metric_counter(
    "horde.kudos.reservations.rejected",
    unit="1",
    description="reserve_kudos admission denials (insufficient available balance)",
)
kudos_transfers_idempotent_replays = logfire.metric_counter(
    "horde.kudos.transfers.idempotent_replays",
    unit="1",
    description="transfer_kudos calls short-circuited as an idempotent replay",
)
