# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the fallback poll interval, which stretches with the Horde's wait estimate."""

from __future__ import annotations

import pytest

from horde.oai.waiter import MAX_STRETCHED_POLL_SECONDS, compute_fallback_poll_seconds, jitter_poll_seconds

BASE_POLL_SECONDS: float = 15.0
"""The default configured fallback poll interval."""


@pytest.mark.parametrize(
    ("wait_time", "expected_seconds"),
    [
        (None, BASE_POLL_SECONDS),
        (0, BASE_POLL_SECONDS),
        (10, BASE_POLL_SECONDS),
        (30, BASE_POLL_SECONDS),
        (50, 25.0),
        (120, MAX_STRETCHED_POLL_SECONDS),
        (3600, MAX_STRETCHED_POLL_SECONDS),
    ],
)
def test_poll_interval_stretches_with_the_wait_estimate_between_the_base_and_the_cap(
    wait_time: int | None,
    expected_seconds: float,
) -> None:
    assert compute_fallback_poll_seconds(base_seconds=BASE_POLL_SECONDS, wait_time=wait_time) == expected_seconds


def test_poll_interval_never_drops_below_a_base_above_the_cap() -> None:
    assert compute_fallback_poll_seconds(base_seconds=90.0, wait_time=3600) == 90.0


@pytest.mark.parametrize(("random_fraction", "expected_seconds"), [(0.0, 60.0), (0.5, 54.0), (0.999, 48.012)])
def test_jitter_only_shortens_the_interval_so_the_cap_still_holds(random_fraction: float, expected_seconds: float) -> None:
    jittered = jitter_poll_seconds(MAX_STRETCHED_POLL_SECONDS, lambda: random_fraction)

    assert jittered == pytest.approx(expected_seconds)
    assert jittered <= MAX_STRETCHED_POLL_SECONDS
