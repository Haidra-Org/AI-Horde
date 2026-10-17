# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entrypoint for the chat front scale demo.

Spawns ``ChatWorkerSimulator`` (pinned to ``--chat-workers``) against the Horde instances, and ``ChatStreamClient``,
``ChatClient`` and ``ChatImpatientClient`` against the front, split by the ``--chat-*-clients`` shares. The
``ChatStagesShape`` holds the workers alone for ``--chat-warmup-seconds``, then steps the client population through
``--chat-stages``, each for ``--chat-stage-seconds``. It reuses the suite's key-parsing, key-bootstrap and
target-preflight ``test_start`` handler by importing ``locustsuite.events`` for its side effects; that import must
precede the configuration handler below so keys are registered before the stage clock starts.

``--host`` is a Horde instance: keys are registered there and the preflight checks its heartbeat. Workers pop from
every instance in ``--chat-horde-urls`` round-robin, and clients talk only to ``--chat-front-host``.

Usage:
    locust -f tests/stress/locustfile_chat.py --host http://127.0.0.1:7101 --headless \\
        --chat-front-host http://127.0.0.1:7002 \\
        --chat-horde-urls http://127.0.0.1:7101,http://127.0.0.1:7102,http://127.0.0.1:7103 \\
        --chat-workers 50 --chat-stages 500,2000,5000 --chat-stage-seconds 120 \\
        --bootstrap-requestors 210 --bootstrap-workers 20 \\
        --chat-evidence-path run/chat_evidence.jsonl --csv run/locust --csv-full-history

``tests/stress/run_chat_scale_demo.sh`` starts the Horde instances, the front and a gauge sampler around this
entrypoint, and ``tests/stress/chat_scale_demo.py summarize`` turns the run's files into the summary.
"""

# ruff: noqa: I001
from __future__ import annotations

import logging
import time
from typing import Any

import requests
from locust import events
from locust.env import Environment

from locustsuite import events as _suite_events  # noqa: F401
from locustsuite.shapes import ChatStagesShape, chat_stage_clock
from locustsuite.users.chat import (
    DEFAULT_CHAT_MODEL,
    ChatClient,
    ChatImpatientClient,
    ChatStreamClient,
    ChatWorkerSimulator,
    chat_evidence_recorder,
    chat_settings,
)

__all__ = ["ChatClient", "ChatImpatientClient", "ChatStagesShape", "ChatStreamClient", "ChatWorkerSimulator"]

logger = logging.getLogger(__name__)

DEFAULT_SIM_GEN_TIME_MIN_SECONDS: float = 3.0
"""The shortest simulated generation time for this scenario; a short chat reply, unlike the suite's text default."""

DEFAULT_SIM_GEN_TIME_MAX_SECONDS: float = 8.0
"""The longest simulated generation time for this scenario."""

FRONT_PREFLIGHT_TIMEOUT_SECONDS: float = 10.0
"""How long the start hook waits for the front's ``/healthz``."""


@events.init_command_line_parser.add_listener
def _add_chat_arguments(parser: Any) -> None:
    group = parser.add_argument_group("AI Horde Chat Front Scale Demo")
    group.add_argument(
        "--chat-front-host",
        type=str,
        env_var="HORDE_CHAT_FRONT_HOST",
        default="http://127.0.0.1:7002",
        help="Base URL of the OpenAI-compatible front the clients post to.",
    )
    group.add_argument(
        "--chat-horde-urls",
        type=str,
        env_var="HORDE_CHAT_HORDE_URLS",
        default="",
        help="Comma-separated Horde base URLs (without /api) the workers pop from round-robin; empty uses --host.",
    )
    group.add_argument(
        "--chat-model",
        type=str,
        env_var="HORDE_CHAT_MODEL",
        default=DEFAULT_CHAT_MODEL,
        help="Model the clients request and the workers serve.",
    )
    group.add_argument(
        "--chat-workers",
        type=int,
        env_var="HORDE_CHAT_WORKERS",
        default=50,
        help="Concurrent ChatWorkerSimulator users, held through every stage.",
    )
    group.add_argument(
        "--chat-stream-clients",
        type=int,
        env_var="HORDE_CHAT_STREAM_CLIENTS",
        default=70,
        help="Share (Locust weight) of each stage's clients that are ChatStreamClient users; 0 leaves them out.",
    )
    group.add_argument(
        "--chat-clients",
        type=int,
        env_var="HORDE_CHAT_CLIENTS",
        default=25,
        help="Share (Locust weight) of each stage's clients that are non-streamed ChatClient users; 0 leaves them out.",
    )
    group.add_argument(
        "--chat-impatient-clients",
        type=int,
        env_var="HORDE_CHAT_IMPATIENT_CLIENTS",
        default=5,
        help="Share (Locust weight) of each stage's clients that are ChatImpatientClient users; 0 leaves them out.",
    )
    group.add_argument(
        "--chat-stages",
        type=str,
        env_var="HORDE_CHAT_STAGES",
        default="500,2000,5000",
        help="Comma-separated concurrent client counts, one per stage, on top of the workers.",
    )
    group.add_argument(
        "--chat-stage-seconds",
        type=float,
        env_var="HORDE_CHAT_STAGE_SECONDS",
        default=120.0,
        help="How long each stage is held, including its ramp.",
    )
    group.add_argument(
        "--chat-warmup-seconds",
        type=float,
        env_var="HORDE_CHAT_WARMUP_SECONDS",
        default=20.0,
        help="How long the workers run alone before the first stage, so every Horde knows them before clients arrive.",
    )
    group.add_argument(
        "--chat-spawn-rate",
        type=float,
        env_var="HORDE_CHAT_SPAWN_RATE",
        default=100.0,
        help="Users started (or stopped) per second when moving between stages.",
    )
    group.add_argument(
        "--chat-max-tokens",
        type=int,
        env_var="HORDE_CHAT_MAX_TOKENS",
        default=64,
        help="max_tokens of every client request.",
    )
    group.add_argument(
        "--chat-no-include-usage",
        action="store_true",
        env_var="HORDE_CHAT_NO_INCLUDE_USAGE",
        default=False,
        help="Leave stream_options.include_usage out of streamed requests.",
    )
    group.add_argument(
        "--chat-impatient-timeout",
        type=float,
        env_var="HORDE_CHAT_IMPATIENT_TIMEOUT",
        default=2.0,
        help="Seconds an impatient client waits before disconnecting.",
    )
    group.add_argument(
        "--chat-impatient-stream-chance",
        type=float,
        env_var="HORDE_CHAT_IMPATIENT_STREAM_CHANCE",
        default=0.5,
        help="Probability (0.0-1.0) that an impatient request is streamed rather than non-streamed.",
    )
    group.add_argument(
        "--chat-nonstream-timeout",
        type=float,
        env_var="HORDE_CHAT_NONSTREAM_TIMEOUT",
        default=300.0,
        help="The front's HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS; non-streamed clients wait 30 seconds longer than it.",
    )
    group.add_argument(
        "--chat-unreported-usage-chance",
        type=float,
        env_var="HORDE_CHAT_UNREPORTED_USAGE_CHANCE",
        default=0.1,
        help="Probability (0.0-1.0) that a worker submits without token counts, so the front's estimate path runs.",
    )
    group.add_argument(
        "--chat-worker-progress",
        action="store_true",
        env_var="HORDE_CHAT_WORKER_PROGRESS",
        default=False,
        help="Workers post the reply as content deltas to /api/v2/generate/text/progress for jobs whose client streams.",
    )
    group.add_argument(
        "--chat-worker-progress-interval",
        type=float,
        env_var="HORDE_CHAT_WORKER_PROGRESS_INTERVAL",
        default=1.0,
        help="Seconds between two progress posts of one job; a job posts one delta per interval of its generation time.",
    )
    group.add_argument(
        "--chat-evidence-path",
        type=str,
        env_var="HORDE_CHAT_EVIDENCE_PATH",
        default="chat_evidence.jsonl",
        help="JSONL file of every client request, truncated at test start; chat_scale_demo.py summarize reads it.",
    )
    # The suite's text default (4 to 120 seconds) models long completions; a chat reply of max_tokens 64 is short.
    parser.set_defaults(sim_gen_time_min=DEFAULT_SIM_GEN_TIME_MIN_SECONDS, sim_gen_time_max=DEFAULT_SIM_GEN_TIME_MAX_SECONDS)


def _check_front(front_host: str) -> None:
    """Log whether the front's ``/healthz`` answers, so a wrong ``--chat-front-host`` shows before the first stage."""
    healthz_url = f"{front_host}/healthz"
    try:
        response = requests.get(healthz_url, timeout=FRONT_PREFLIGHT_TIMEOUT_SECONDS)
    except requests.RequestException as error:
        logger.warning("Front preflight failed for %s: %s", healthz_url, error)
        return
    if response.status_code >= 400:
        logger.warning("Front preflight returned HTTP %d for %s", response.status_code, healthz_url)
        return
    logger.info("Front preflight OK: %s %s", healthz_url, response.text[:200])


@events.test_start.add_listener
def _configure_chat_run(environment: Environment, **_kwargs: Any) -> None:
    options = environment.parsed_options
    chat_settings.configure(options, fallback_horde_url=environment.host or "")

    ChatWorkerSimulator.fixed_count = max(int(options.chat_workers), 0)
    client_shares = (
        (ChatStreamClient, int(options.chat_stream_clients)),
        (ChatClient, int(options.chat_clients)),
        (ChatImpatientClient, int(options.chat_impatient_clients)),
    )
    excluded_classes: set[type] = {ChatWorkerSimulator} if ChatWorkerSimulator.fixed_count == 0 else set()
    for client_class, share in client_shares:
        if share <= 0:
            excluded_classes.add(client_class)
            continue
        client_class.weight = share
    environment.user_classes[:] = [user_class for user_class in environment.user_classes if user_class not in excluded_classes]

    _check_front(chat_settings.front_host)
    chat_evidence_recorder.configure(options.chat_evidence_path)
    chat_stage_clock.mark_started()
    chat_evidence_recorder.write(
        {
            "kind": "run_start",
            "ts": chat_stage_clock.started_at,
            "warmup_seconds": float(options.chat_warmup_seconds),
            "stage_seconds": float(options.chat_stage_seconds),
            "stages": [int(stage) for stage in str(options.chat_stages).split(",") if stage.strip()],
            "workers": ChatWorkerSimulator.fixed_count,
            "client_shares": {client_class.__name__: share for client_class, share in client_shares},
            "front_host": chat_settings.front_host,
            "horde_urls": chat_settings.horde_urls,
            "model": chat_settings.model,
            "sim_gen_time_seconds": [chat_settings.gen_time_min_seconds, chat_settings.gen_time_max_seconds],
            "worker_progress": chat_settings.worker_progress,
            "worker_progress_interval_seconds": chat_settings.worker_progress_interval_seconds,
        },
    )


@events.test_stop.add_listener
def _close_chat_evidence(environment: Environment, **_kwargs: Any) -> None:
    chat_evidence_recorder.write({"kind": "run_stop", "ts": time.time()})
    chat_evidence_recorder.close()
