# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify which requests count toward a user's concurrency limit at admission."""

from __future__ import annotations

from typing import Any

import pytest

from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.database import functions as database
from horde.flask import db

pytestmark = pytest.mark.unit

_MODEL = "stable_diffusion"
"""The model of every request in these tests."""


def _make_request(requester: Any, *, active: bool, faulted: bool = False, jobs_left: int = 1) -> ImageWaitingPrompt:
    request = ImageWaitingPrompt(
        worker_ids=[],
        models=[_MODEL],
        prompt="waiting request count test",
        user_id=requester.id,
        params={"n": 1, "width": 512, "height": 512, "steps": 10, "sampler_name": "k_euler_a"},
    )
    request.active = active
    request.faulted = faulted
    request.n = jobs_left
    db.session.commit()
    return request


@pytest.mark.parametrize(
    "models",
    [
        pytest.param(None, id="without_models"),
        pytest.param([_MODEL], id="with_models"),
    ],
)
def test_only_activated_requests_count_toward_concurrency(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    models: list[str] | None,
) -> None:
    """A request counts toward the user's waiting requests once it is activated, with or without a model list. A
    request that is still being admitted does not count."""
    requester = make_user()
    _make_request(requester, active=False)
    assert database.count_waiting_requests(user=requester, models=models, request_type="image") == 0

    _make_request(requester, active=True)
    assert database.count_waiting_requests(user=requester, models=models, request_type="image") == 1


@pytest.mark.parametrize(
    "models",
    [
        pytest.param(None, id="without_models"),
        pytest.param([_MODEL], id="with_models"),
    ],
)
def test_faulted_and_fully_assigned_requests_do_not_count(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    models: list[str] | None,
) -> None:
    """A faulted request and a request with no jobs left to assign do not count, active or not."""
    requester = make_user()
    for active in (False, True):
        _make_request(requester, active=active, faulted=True)
        _make_request(requester, active=active, jobs_left=0)

    assert database.count_waiting_requests(user=requester, models=models, request_type="image") == 0
