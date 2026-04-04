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
  ``MetricsOptions(views=histogram_views())`` — see :func:`histogram_views`.

Adding a new metric is one line: pick the right section, call the matching
``_*_histogram`` / ``logfire.metric_counter`` helper, and assign to a
module-level constant. The helper auto-registers the bucket profile.
"""

import logfire


class WaitressMetrics:
    task_dispatcher = None

    def setup(self, td):
        self.task_dispatcher = td

    @property
    def queue(self):
        return len(self.task_dispatcher.queue)

    @property
    def threads(self):
        return len(self.task_dispatcher.threads)

    @property
    def active_count(self):
        # -1 to ignore the /metrics task
        return self.task_dispatcher.active_count - 1


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
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
    1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.5, 10.0, 30.0, 60.0, 300.0, 1800.0,
)
BUCKETS_COUNT = (0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 5000)
BUCKETS_KUDOS = (0, 1, 10, 100, 1000, 10000, 100000)


_BUCKET_REGISTRY: dict[str, tuple] = {}


def _seconds_histogram(name: str, description: str):
    _BUCKET_REGISTRY[name] = BUCKETS_SECONDS
    return logfire.metric_histogram(name, unit="s", description=description)


def _count_histogram(name: str, description: str):
    _BUCKET_REGISTRY[name] = BUCKETS_COUNT
    return logfire.metric_histogram(name, unit="1", description=description)


def _kudos_histogram(name: str, description: str):
    _BUCKET_REGISTRY[name] = BUCKETS_KUDOS
    return logfire.metric_histogram(name, unit="kudos", description=description)


def histogram_views():
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
    "horde.generate.duration", "End-to-end duration of a generate request",
)
generate_validate_duration = _seconds_histogram(
    "horde.generate.validate.duration", "Duration of GenerateTemplate.validate",
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

# --- waiting-prompt activation / kudos ---------------------------------------
wp_calculate_kudos_duration = _seconds_histogram(
    "horde.wp.calculate_kudos.duration",
    "Duration of ImageWaitingPrompt.calculate_kudos",
)
wp_kudos_torch_duration = _seconds_histogram(
    "horde.wp.kudos.torch.duration",
    "Duration of KudosModel.calculate_kudos torch forward pass",
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
    "horde.wp.activate.duration", "Duration of WaitingPrompt.activate inner body",
)
wp_activation_age = _seconds_histogram(
    "horde.wp.activation_age", "Elapsed time between WP create and activation",
)

# --- pop ---------------------------------------------------------------------
pop_duration = _seconds_histogram(
    "horde.pop.duration", "End-to-end duration of a job_pop request",
)
pop_query_duration = _seconds_histogram(
    "horde.pop.wp_query.duration",
    "Duration of get_sorted_wp_filtered_to_worker query",
)
pop_pre_eval_duration = _seconds_histogram(
    "horde.pop.pre_eval.duration", "Duration of pop validate + check_in + wp_query",
)
pop_validate_duration = _seconds_histogram(
    "horde.pop.validate.duration", "Duration of pop validate()",
)
pop_check_in_duration = _seconds_histogram(
    "horde.pop.check_in.duration", "Duration of pop worker.check_in()",
)
pop_eval_duration = _seconds_histogram(
    "horde.pop.eval_loop.duration", "Duration of pop candidate evaluation loop",
)
pop_start_gen_duration = _seconds_histogram(
    "horde.pop.start_generation.duration",
    "Duration of WP.start_generation dispatch on a successful pop",
)
pop_candidates = _count_histogram(
    "horde.pop.candidates_evaluated", "Number of WaitingPrompts evaluated per pop",
)
pop_returned_jobs = _count_histogram(
    "horde.pop.returned_jobs", "Jobs returned to the worker per pop (0=no-match)",
)
pop_skipped = logfire.metric_counter(
    "horde.pop.skipped", unit="1", description="WPs skipped during pop, by reason",
)

# --- submit ------------------------------------------------------------------
submit_duration = _seconds_histogram(
    "horde.submit.duration", "End-to-end duration of a job_submit request",
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
    "horde.submit.set_generation.duration", "Duration of procgen.set_generation",
)
submit_record_duration = _seconds_histogram(
    "horde.submit.record.duration", "Duration of procgen.record",
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
    "horde.submit.webhook_call.duration", "Duration of procgen.send_webhook in submit",
)
submit_commit_duration = _seconds_histogram(
    "horde.submit.commit.duration",
    "Duration of db.session.commit() at end of procgen.set_generation",
)
submit_kudos = _kudos_histogram(
    "horde.submit.kudos", "Kudos awarded per job submission",
)
submit_outcomes = logfire.metric_counter(
    "horde.submit.outcomes", unit="1", description="/generate/submit outcomes",
)

# --- /generate/check & /generate/status --------------------------------------
check_duration = _seconds_histogram(
    "horde.generate.check.duration", "End-to-end duration of a /generate/check poll",
)
status_duration = _seconds_histogram(
    "horde.generate.status.duration", "End-to-end duration of a /generate/status fetch",
)
check_outcomes = logfire.metric_counter(
    "horde.generate.check.outcomes", unit="1", description="/generate/check outcomes",
)

# --- webhooks ----------------------------------------------------------------
webhook_duration = _seconds_histogram(
    "horde.webhook.attempt.duration", "Duration of a single webhook POST attempt",
)
webhook_outcomes = logfire.metric_counter(
    "horde.webhook.outcomes", unit="1", description="Terminal webhook outcomes",
)

# --- background jobs / countermeasures / db ----------------------------------
job_duration = _seconds_histogram(
    "horde.job.duration", "Duration of a PrimaryTimedFunction invocation",
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
