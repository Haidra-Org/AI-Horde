# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify that text feasibility uses the worker-pop speed threshold."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from horde.classes.base.user import UserRoleTypes
from horde.classes.base.worker import WorkerModel
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker, get_minimum_text_worker_speed
from horde.database import functions
from horde.flask import db

pytestmark = pytest.mark.unit


def _make_text_request(user: Any, model_name: str) -> TextWaitingPrompt:
    request = TextWaitingPrompt(
        worker_ids=[],
        models=[model_name],
        prompt="text eligibility test",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 2048},
    )
    request.slow_workers = False
    request.validated_backends = False
    db.session.commit()
    return request


def _make_text_worker(user: Any, model_name: str, speed: float) -> TextWorker:
    worker = TextWorker(
        user_id=user.id,
        name=f"text-eligibility-{uuid.uuid4().hex[:12]}",
        max_length=512,
        max_context_length=4096,
        last_check_in=datetime.utcnow(),
        speed=speed,
        nsfw=True,
        allow_unsafe_ipaddr=True,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=model_name))
    db.session.commit()
    return worker


@pytest.mark.parametrize(
    ("parameter_count", "worker_speed", "expected_workers"),
    [(7, 4, 0), (20, 3, 1)],
)
def test_availability_matches_model_dependent_pop_speed(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
    parameter_count: int,
    worker_speed: float,
    expected_workers: int,
) -> None:
    model_name = f"model-{parameter_count}b"
    monkeypatch.setattr(
        "horde.classes.kobold.worker.model_reference.get_text_model_multiplier",
        lambda _model_name: parameter_count,
    )
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, model_name)
    _make_text_worker(user, model_name, worker_speed)

    availability = functions.get_worker_availability_for_request(request)

    assert get_minimum_text_worker_speed([model_name]) == (5 if parameter_count == 7 else 3)
    assert availability.worker_count == expected_workers


def test_runtime_gate_rejects_a_model_the_worker_does_not_serve(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
) -> None:
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, "requested-model")
    worker = _make_text_worker(user, "worker-model", speed=10)

    can_generate, reason = worker.can_generate_with_softprompt_names(
        request,
        [],
        model_names=["worker-model"],
    )

    assert can_generate is False
    assert reason == "models"
