# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's synchronous building blocks: the no-op table's coverage and the inflight limiter."""

from __future__ import annotations

from horde.oai.app import InflightLimiter
from horde.oai.schema import DEFAULT_NO_OP_PARAMETERS
from tests.unit.oai.test_request_screening import NO_OP_ROWS


def test_no_op_rows_cover_every_key_of_the_table() -> None:
    assert {parameter for parameter, _value in NO_OP_ROWS} == set(DEFAULT_NO_OP_PARAMETERS)


def test_per_source_cap_refuses_before_the_global_cap() -> None:
    limiter = InflightLimiter(max_inflight=2, max_inflight_per_source=1)
    first_slot = limiter.try_acquire("198.51.100.1")

    assert first_slot is not None
    assert limiter.has_capacity
    assert limiter.try_acquire("198.51.100.1") is None
    second_slot = limiter.try_acquire("198.51.100.2")
    assert second_slot is not None
    assert limiter.try_acquire("198.51.100.3") is None

    first_slot.release()
    first_slot.release()
    assert limiter.inflight == 1
    assert limiter.inflight_for("198.51.100.1") == 0
    assert limiter.try_acquire("198.51.100.1") is not None
