# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entrypoint for the hot-user lock-convoy simulation.

Spawns the convoy populations (``AnonRequester``, ``HeavyProxyRequester``,
``ConvoyWorker``, ``StatusCheckPoller``, ``KudosTransfer``) with fixed per-class
counts derived from CLI arguments, and configures the shared phase schedule and
generation fan-out at test start. It reuses the suite's key-parsing and
target-preflight ``test_start`` handler by importing ``locustsuite.events`` for
its side effects; that import must precede the configuration handler below so
keys are parsed first.

Usage:
    locust -f tests/stress/locustfile_hot_user_convoy.py --host http://localhost:80 \
        --headless --users 120 --spawn-rate 60 --run-time 300s \
        --hc-anon-requestors 60 --hc-heavy-requestors 6 --hc-workers 40 \
        --hc-status-pollers 12 --hc-kudos-users 2 \
        --hc-baseline 60 --hc-pressure 180 --hc-relief 60 \
        --hc-n-pressure 6 --worker-api-keys W1,W2,... \
        --hc-heavy-api-keys H1,... --hc-kudos-api-keys K1,K2 \
        --csv hc --csv-full-history
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
from locustsuite.users.hot_user_convoy import (
    AnonRequester,
    ConvoyWorker,
    HeavyProxyRequester,
    KudosTransfer,
    StatusCheckPoller,
    configure_convoy,
    convoy_state,
)

__all__ = ["AnonRequester", "ConvoyWorker", "HeavyProxyRequester", "KudosTransfer", "StatusCheckPoller"]


@events.init_command_line_parser.add_listener
def _add_convoy_arguments(parser) -> None:
    group = parser.add_argument_group("AI Horde Hot User Convoy Scenario")
    group.add_argument(
        "--hc-anon-requestors",
        type=int,
        env_var="HC_ANON_REQUESTORS",
        default=60,
        help="Concurrent AnonRequester users (users.id=0 activations).",
    )
    group.add_argument(
        "--hc-heavy-requestors",
        type=int,
        env_var="HC_HEAVY_REQUESTORS",
        default=6,
        help="Concurrent HeavyProxyRequester users (registered high-concurrency accounts).",
    )
    group.add_argument(
        "--hc-workers",
        type=int,
        env_var="HC_WORKERS",
        default=40,
        help="Concurrent ConvoyWorker users (pop/submit settlement pressure).",
    )
    group.add_argument(
        "--hc-status-pollers",
        type=int,
        env_var="HC_STATUS_POLLERS",
        default=12,
        help="Concurrent StatusCheckPoller users (client-backend + micro-cache reads).",
    )
    group.add_argument(
        "--hc-kudos-users",
        type=int,
        env_var="HC_KUDOS_USERS",
        default=2,
        help="Concurrent KudosTransfer users (fixed pair of registered accounts).",
    )
    group.add_argument(
        "--hc-heavy-api-keys",
        type=str,
        env_var="HC_HEAVY_API_KEYS",
        default="",
        help="Comma-separated registered API keys for the heavy proxy requesters and pollers.",
    )
    group.add_argument(
        "--hc-kudos-api-keys",
        type=str,
        env_var="HC_KUDOS_API_KEYS",
        default="",
        help="Comma-separated registered API keys (ideally two) for the kudos-transfer pair.",
    )
    group.add_argument(
        "--hc-served-models",
        type=str,
        env_var="HC_SERVED_MODELS",
        default="koboldcpp/hc-served",
        help="Comma-separated model names both ConvoyWorker and the requesters use.",
    )
    group.add_argument("--hc-baseline", type=float, env_var="HC_BASELINE", default=60.0, help="Baseline phase duration (seconds).")
    group.add_argument("--hc-pressure", type=float, env_var="HC_PRESSURE", default=180.0, help="Pressure phase duration (seconds).")
    group.add_argument("--hc-relief", type=float, env_var="HC_RELIEF", default=60.0, help="Relief phase duration (seconds).")
    group.add_argument("--hc-gen-time-min", type=float, env_var="HC_GEN_TIME_MIN", default=0.2, help="Min simulated worker gen time (s).")
    group.add_argument("--hc-gen-time-max", type=float, env_var="HC_GEN_TIME_MAX", default=1.0, help="Max simulated worker gen time (s).")
    group.add_argument(
        "--hc-max-length",
        type=int,
        env_var="HC_MAX_LENGTH",
        default=24,
        help="max_length for requester prompts (kept small so anon kudos suffices).",
    )
    group.add_argument(
        "--hc-max-context-length",
        type=int,
        env_var="HC_MAX_CONTEXT_LENGTH",
        default=1024,
        help="max_context_length for requester prompts.",
    )
    group.add_argument(
        "--hc-n-baseline",
        type=int,
        env_var="HC_N_BASELINE",
        default=2,
        help="Generation fan-out (n) per request in the baseline phase.",
    )
    group.add_argument(
        "--hc-n-pressure",
        type=int,
        env_var="HC_N_PRESSURE",
        default=6,
        help="Generation fan-out (n) per request in the pressure phase.",
    )
    group.add_argument(
        "--hc-heavy-max-pending",
        type=int,
        env_var="HC_HEAVY_MAX_PENDING",
        default=20,
        help="Max in-flight requests a HeavyProxyRequester holds before it only polls.",
    )


@events.test_start.add_listener
def _configure_convoy_run(environment, **_kwargs) -> None:
    opts = environment.parsed_options

    AnonRequester.fixed_count = max(int(opts.hc_anon_requestors), 0)
    HeavyProxyRequester.fixed_count = max(int(opts.hc_heavy_requestors), 0)
    ConvoyWorker.fixed_count = max(int(opts.hc_workers), 0)
    StatusCheckPoller.fixed_count = max(int(opts.hc_status_pollers), 0)
    KudosTransfer.fixed_count = max(int(opts.hc_kudos_users), 0)

    # Worker-owner keys come from the suite-wide --worker-api-keys handled by
    # locustsuite.events; the convoy adds its own heavy/kudos key pools.
    worker_keys = _parse_csv(getattr(opts, "worker_api_keys", "") or "")

    configure_convoy(
        baseline_s=float(opts.hc_baseline),
        pressure_s=float(opts.hc_pressure),
        relief_s=float(opts.hc_relief),
        served_models=_parse_csv(opts.hc_served_models),
        heavy_keys=_parse_csv(opts.hc_heavy_api_keys),
        worker_keys=worker_keys,
        kudos_keys=_parse_csv(opts.hc_kudos_api_keys),
        gen_time_min=float(opts.hc_gen_time_min),
        gen_time_max=float(opts.hc_gen_time_max),
        max_length=int(opts.hc_max_length),
        max_context_length=int(opts.hc_max_context_length),
        n_baseline=int(opts.hc_n_baseline),
        n_pressure=int(opts.hc_n_pressure),
        heavy_max_pending=int(opts.hc_heavy_max_pending),
    )

    # Emit the true test-start instant and phase durations so the offline analyzer
    # buckets the prober/CSV series against the same boundaries current_phase()
    # applied during the run, rather than an orchestrator-side approximation.
    phases_out = os.environ.get("HC_PHASES_OUT")
    if phases_out:
        run_start = convoy_state.run_start or time.time()
        boundaries = {
            "run_start": run_start,
            "baseline_s": float(opts.hc_baseline),
            "pressure_s": float(opts.hc_pressure),
            "relief_s": float(opts.hc_relief),
            "baseline_start": run_start,
            "pressure_start": run_start + float(opts.hc_baseline),
            "relief_start": run_start + float(opts.hc_baseline) + float(opts.hc_pressure),
            "relief_end": run_start + float(opts.hc_baseline) + float(opts.hc_pressure) + float(opts.hc_relief),
        }
        Path(phases_out).write_text(json.dumps(boundaries, indent=2), encoding="utf-8")
