# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entrypoint for the attribution and possibility consistency scenarios.

This entrypoint spawns only the adversarial-timing text users defined in
``locustsuite.users.attribution`` and reuses the suite's existing key-bootstrap
and target-preflight events. The scenarios are driven with fixed per-class user
counts derived from a small set of CLI arguments so a run reproduces the same
adversarial shape every time.

Usage:
    locust -f tests/stress/locustfile_attribution.py --host http://localhost:7001 \
        --headless --users 40 --spawn-rate 20 --run-time 150s \
        --attribution-pairs 6 --maintenance-workers 2 \
        --attribution-evidence attribution_evidence.jsonl --csv attribution
"""
# ruff: noqa: I001

from __future__ import annotations

from locust import events

# Import the suite events module for its side effects: it registers the shared
# CLI arguments, the key-bootstrap and target-preflight test_start handler. It
# must be imported before the attribution test_start handler below so that key
# bootstrap runs first.
from locustsuite import events as _suite_events  # noqa: F401
from locustsuite.ground_truth import oracle_recorder, reset_run_state
from locustsuite.users.attribution import (
    AttributionRequester,
    MaintenanceFlipRequester,
    MaintenanceFlipWorker,
    PairedFlappingWorker,
)

__all__ = [
    "AttributionRequester",
    "MaintenanceFlipRequester",
    "MaintenanceFlipWorker",
    "PairedFlappingWorker",
]


@events.init_command_line_parser.add_listener
def _add_attribution_arguments(parser) -> None:
    group = parser.add_argument_group("AI Horde Attribution Scenario")
    group.add_argument(
        "--attribution-pairs",
        type=int,
        env_var="HORDE_ATTRIBUTION_PAIRS",
        default=6,
        help="Number of flapping worker pairs (each pair spawns two PairedFlappingWorker users).",
    )
    group.add_argument(
        "--attribution-requestors",
        type=int,
        env_var="HORDE_ATTRIBUTION_REQUESTORS",
        default=0,
        help="Number of AttributionRequester users (0 derives 2 per pair).",
    )
    group.add_argument(
        "--attribution-decoys",
        type=int,
        env_var="HORDE_ATTRIBUTION_DECOYS",
        default=2,
        help="Decoy models appended to each request's model constraint (models no worker declares).",
    )
    group.add_argument(
        "--attribution-churn-period",
        type=float,
        env_var="HORDE_ATTRIBUTION_CHURN_PERIOD",
        default=20.0,
        help="Seconds between churns onto a fresh worker identity (0 disables churn).",
    )
    group.add_argument(
        "--attribution-max-length",
        type=int,
        env_var="HORDE_ATTRIBUTION_MAX_LENGTH",
        default=24,
        help="max_length for attribution requests; kept small so jobs cycle quickly.",
    )
    group.add_argument(
        "--maintenance-workers",
        type=int,
        env_var="HORDE_MAINTENANCE_WORKERS",
        default=2,
        help="Number of MaintenanceFlipWorker users (each owns a unique model).",
    )
    group.add_argument(
        "--maintenance-requesters",
        type=int,
        env_var="HORDE_MAINTENANCE_REQUESTERS",
        default=0,
        help="Number of MaintenanceFlipRequester users (0 derives 2 per maintenance worker).",
    )
    group.add_argument(
        "--maintenance-cycles",
        type=int,
        env_var="HORDE_MAINTENANCE_CYCLES",
        default=0,
        help="Pop/maintenance/release cycles per maintenance worker (0 for unbounded within the run).",
    )
    group.add_argument(
        "--maintenance-hold-min",
        type=float,
        env_var="HORDE_MAINTENANCE_HOLD_MIN",
        default=6.0,
        help="Minimum seconds a maintenance worker holds a popped job in flight while in maintenance.",
    )
    group.add_argument(
        "--maintenance-hold-max",
        type=float,
        env_var="HORDE_MAINTENANCE_HOLD_MAX",
        default=12.0,
        help="Maximum seconds a maintenance worker holds a popped job in flight while in maintenance.",
    )
    group.add_argument(
        "--attribution-evidence",
        type=str,
        env_var="HORDE_ATTRIBUTION_EVIDENCE",
        default="attribution_evidence.jsonl",
        help="Path to the JSONL oracle-evidence file written when a consistency violation is observed.",
    )


@events.test_start.add_listener
def _configure_attribution_run(environment, **_kwargs) -> None:
    opts = environment.parsed_options

    # Start each run from a clean registry so timelines and slot counters never
    # carry over between runs sharing one process.
    reset_run_state()
    oracle_recorder.configure(opts.attribution_evidence)

    pairs = max(int(opts.attribution_pairs), 0)
    attribution_requestors = int(opts.attribution_requestors) or (pairs * 2)
    maintenance_workers = max(int(opts.maintenance_workers), 0)
    maintenance_requesters = int(opts.maintenance_requesters) or (maintenance_workers * 2)

    PairedFlappingWorker.fixed_count = pairs * 2
    AttributionRequester.fixed_count = attribution_requestors
    MaintenanceFlipWorker.fixed_count = maintenance_workers
    MaintenanceFlipRequester.fixed_count = maintenance_requesters
