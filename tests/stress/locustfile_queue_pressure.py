# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entrypoint for the queue-pressure simulation.

Spawns the three queue-pressure populations (``BacklogRequester``,
``ServingWorker``, ``ServedRequester``) with fixed per-class counts derived from
CLI arguments, and configures the shared phase schedule and backlog target at
test start. It reuses the suite's key-parsing and target-preflight ``test_start``
handler by importing ``locustsuite.events`` for its side effects; that import
must precede the configuration handler below so keys are parsed first.

Usage:
    locust -f tests/stress/locustfile_queue_pressure.py --host http://localhost:7001 \
        --headless --users 60 --spawn-rate 30 --run-time 300s \
        --qp-workers 20 --qp-served-requestors 8 --qp-backlog-requestors 24 \
        --qp-backlog-target 3000 --qp-baseline 60 --qp-pressure 180 --qp-relief 60 \
        --worker-api-keys K1,K2,... --requestor-api-keys R1,... \
        --qp-backlog-api-keys B1,B2,... --csv qp --csv-full-history
"""
# ruff: noqa: I001

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from locust import events

from locustsuite import events as _suite_events  # noqa: F401
from locustsuite.helpers import _parse_csv
from locustsuite.users.queue_pressure import (
    BacklogRequester,
    ServedRequester,
    ServingWorker,
    configure_queue_pressure,
    qp_state,
)

__all__ = ["BacklogRequester", "ServedRequester", "ServingWorker"]


@events.init_command_line_parser.add_listener
def _add_queue_pressure_arguments(parser) -> None:
    group = parser.add_argument_group("AI Horde Queue Pressure Scenario")
    group.add_argument("--qp-workers", type=int, env_var="QP_WORKERS", default=20, help="Concurrent ServingWorker users.")
    group.add_argument(
        "--qp-served-requestors",
        type=int,
        env_var="QP_SERVED_REQUESTORS",
        default=8,
        help="Concurrent ServedRequester users (servable workload + status polling).",
    )
    group.add_argument(
        "--qp-backlog-requestors",
        type=int,
        env_var="QP_BACKLOG_REQUESTORS",
        default=24,
        help="Concurrent BacklogRequester users inflating the unserved backlog.",
    )
    group.add_argument(
        "--qp-backlog-target",
        type=int,
        env_var="QP_BACKLOG_TARGET",
        default=3000,
        help="Target number of unserved backlog prompts to reach during the pressure phase.",
    )
    group.add_argument(
        "--qp-backlog-api-keys",
        type=str,
        env_var="QP_BACKLOG_API_KEYS",
        default="",
        help="Comma-separated API keys dedicated to backlog inflation (sized to cover the target at ~28 prompts/key).",
    )
    group.add_argument(
        "--qp-backlog-per-key-cap",
        type=int,
        env_var="QP_BACKLOG_PER_KEY_CAP",
        default=28,
        help="Max in-flight backlog prompts steered to a single key before it is treated as saturated.",
    )
    group.add_argument(
        "--qp-backlog-models",
        type=str,
        env_var="QP_BACKLOG_MODELS",
        default="qp-backlog-unserved-a,qp-backlog-unserved-b",
        help="Comma-separated decoy model names that no worker declares (keeps backlog prompts unservable).",
    )
    group.add_argument(
        "--qp-served-models",
        type=str,
        env_var="QP_SERVED_MODELS",
        default="koboldcpp/qp-served",
        help="Comma-separated model names both ServingWorker and ServedRequester use.",
    )
    group.add_argument("--qp-baseline", type=float, env_var="QP_BASELINE", default=60.0, help="Baseline phase duration (seconds).")
    group.add_argument("--qp-pressure", type=float, env_var="QP_PRESSURE", default=180.0, help="Pressure phase duration (seconds).")
    group.add_argument("--qp-relief", type=float, env_var="QP_RELIEF", default=60.0, help="Relief phase duration (seconds).")
    group.add_argument(
        "--qp-gen-time-min", type=float, env_var="QP_GEN_TIME_MIN", default=0.5, help="Min simulated worker generation time (s)."
    )
    group.add_argument(
        "--qp-gen-time-max", type=float, env_var="QP_GEN_TIME_MAX", default=2.5, help="Max simulated worker generation time (s)."
    )
    group.add_argument(
        "--qp-served-max-length", type=int, env_var="QP_SERVED_MAX_LENGTH", default=32, help="max_length for served/backlog prompts."
    )
    group.add_argument(
        "--qp-worker-max-length", type=int, env_var="QP_WORKER_MAX_LENGTH", default=512, help="max_length advertised by ServingWorker pops."
    )
    group.add_argument(
        "--qp-worker-max-context-length",
        type=int,
        env_var="QP_WORKER_MAX_CONTEXT_LENGTH",
        default=2048,
        help="max_context_length advertised by ServingWorker pops.",
    )


@events.test_start.add_listener
def _configure_queue_pressure_run(environment, **_kwargs) -> None:
    opts = environment.parsed_options

    BacklogRequester.fixed_count = max(int(opts.qp_backlog_requestors), 0)
    ServingWorker.fixed_count = max(int(opts.qp_workers), 0)
    ServedRequester.fixed_count = max(int(opts.qp_served_requestors), 0)

    configure_queue_pressure(
        baseline_s=float(opts.qp_baseline),
        pressure_s=float(opts.qp_pressure),
        relief_s=float(opts.qp_relief),
        backlog_target=int(opts.qp_backlog_target),
        backlog_models=_parse_csv(opts.qp_backlog_models),
        backlog_keys=_parse_csv(opts.qp_backlog_api_keys),
        backlog_per_key_cap=int(opts.qp_backlog_per_key_cap),
        served_models=_parse_csv(opts.qp_served_models),
        gen_time_min=float(opts.qp_gen_time_min),
        gen_time_max=float(opts.qp_gen_time_max),
        served_max_length=int(opts.qp_served_max_length),
        worker_max_length=int(opts.qp_worker_max_length),
        worker_max_context_length=int(opts.qp_worker_max_context_length),
    )

    # Emit the true test-start instant and phase durations so an offline analyzer
    # can bucket the prober/CSV series against the same phase boundaries the users
    # applied, rather than an orchestrator-side approximation of when spawning began.
    phases_out = os.environ.get("QP_PHASES_OUT")
    if phases_out:
        # Use the users' own run_start so the analyzer's phase boundaries match
        # exactly what current_phase() applied during the run.
        run_start = qp_state.run_start or time.time()
        boundaries = {
            "run_start": run_start,
            "baseline_s": float(opts.qp_baseline),
            "pressure_s": float(opts.qp_pressure),
            "relief_s": float(opts.qp_relief),
            "baseline_start": run_start,
            "pressure_start": run_start + float(opts.qp_baseline),
            "relief_start": run_start + float(opts.qp_baseline) + float(opts.qp_pressure),
            "relief_end": run_start + float(opts.qp_baseline) + float(opts.qp_pressure) + float(opts.qp_relief),
        }
        Path(phases_out).write_text(json.dumps(boundaries, indent=2), encoding="utf-8")
