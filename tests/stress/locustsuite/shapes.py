# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in staged load shapes for the AI Horde Locust suite."""

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
