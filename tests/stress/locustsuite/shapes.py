# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in staged load shapes for the AI Horde Locust suite."""

import time

from locust import LoadTestShape

_STAGE_PROFILES = {
    "smoke": (
        {"duration": 30, "users": 2, "spawn_rate": 1},
        {"duration": 90, "users": 8, "spawn_rate": 2},
        {"duration": 120, "users": 2, "spawn_rate": 2},
    ),
    "baseline": (
        {"duration": 120, "users": 20, "spawn_rate": 5},
        {"duration": 600, "users": 80, "spawn_rate": 10},
        {"duration": 900, "users": 40, "spawn_rate": 10},
    ),
    "spike": (
        {"duration": 60, "users": 10, "spawn_rate": 5},
        {"duration": 180, "users": 120, "spawn_rate": 60},
        {"duration": 300, "users": 120, "spawn_rate": 20},
        {"duration": 420, "users": 20, "spawn_rate": 20},
    ),
}


class HordeStagesShape(LoadTestShape):
    """Staged ramp/sustain/cooldown profile selected with suite CLI flags."""

    use_common_options = True

    def tick(self):
        opts = self.runner.environment.parsed_options
        profile_name = getattr(opts, "stress_shape_profile", "baseline")
        scale = max(float(getattr(opts, "stress_shape_scale", 1.0)), 0.01)
        run_time = self.get_run_time()

        for stage in _STAGE_PROFILES[profile_name]:
            if run_time < stage["duration"]:
                users = max(1, round(stage["users"] * scale))
                spawn_rate = max(1.0, stage["spawn_rate"] * scale)
                return users, spawn_rate
        return None


class ChatStageClock:
    """Hold the time the chat scenario finished its test-start work, which the chat stages are measured from.

    Locust starts a shape's clock before the test-start hooks run, and the hooks register the run's API keys, which
    takes seconds per hundred keys. Measuring the stages from the end of that work keeps every stage its full length.
    """

    def __init__(self) -> None:
        """Create the clock, not yet started."""
        self.started_at: float | None = None
        """The Unix time the chat scenario was ready, or None before then."""

    def mark_started(self) -> None:
        """Mutate the clock to start now."""
        self.started_at = time.time()

    def elapsed_seconds(self) -> float | None:
        """Return the seconds since the clock started, or None when it has not."""
        if self.started_at is None:
            return None
        return time.time() - self.started_at


chat_stage_clock = ChatStageClock()
"""The process-wide clock the chat locustfile starts and ``ChatStagesShape`` reads."""


class ChatStagesShape(LoadTestShape):
    """Hold the chat workers alone for a warm-up, then step the chat clients through ``--chat-stages``.

    Each stage holds ``--chat-workers`` plus that stage's client count for ``--chat-stage-seconds``; the run stops
    after the last stage. The workers are pinned with ``fixed_count``, so Locust spreads only the clients across the
    client classes by weight.
    """

    def tick(self) -> tuple[int, float] | None:
        """Return the user count and spawn rate for the current moment, or None once the last stage is over."""
        options = self.runner.environment.parsed_options
        workers = int(options.chat_workers)
        spawn_rate = float(options.chat_spawn_rate)
        elapsed_seconds = chat_stage_clock.elapsed_seconds()
        if elapsed_seconds is None or elapsed_seconds < float(options.chat_warmup_seconds):
            return max(workers, 1), spawn_rate
        stage_client_counts = [int(stage) for stage in str(options.chat_stages).split(",") if stage.strip()]
        stage_index = int((elapsed_seconds - float(options.chat_warmup_seconds)) // float(options.chat_stage_seconds))
        if stage_index >= len(stage_client_counts):
            return None
        return workers + stage_client_counts[stage_index], spawn_rate
