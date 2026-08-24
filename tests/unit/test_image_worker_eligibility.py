# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify that image runtime eligibility enforces worker-pop constraints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from horde.classes.base.user import UserRoleTypes
from horde.classes.base.worker import WorkerModel
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.flask import db

pytestmark = pytest.mark.unit

_MODEL = "stable_diffusion"


@pytest.fixture(autouse=True)
def _stub_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep worker checks independent of the network-backed model reference."""
    from horde import model_reference as model_reference_module

    monkeypatch.setattr(
        model_reference_module.model_reference,
        "reference",
        {_MODEL: {"baseline": "stable diffusion 1"}},
    )


def _make_request(user: Any) -> ImageWaitingPrompt:
    request = ImageWaitingPrompt(
        worker_ids=[],
        models=[_MODEL],
        prompt="image eligibility test",
        user_id=user.id,
        params={
            "n": 1,
            "width": 512,
            "height": 512,
            "steps": 10,
            "sampler_name": "k_euler_a",
            "karras": True,
        },
    )
    request.slow_workers = True
    request.extra_slow_workers = True
    db.session.commit()
    return request


def _make_worker(user: Any) -> ImageWorker:
    worker = ImageWorker(
        user_id=user.id,
        name=f"image-eligibility-{uuid.uuid4().hex[:12]}",
        max_pixels=1024 * 1024,
        last_check_in=datetime.utcnow(),
        speed=1_000_000,
        allow_lora=True,
        allow_sdxl_controlnet=True,
        allow_unsafe_ipaddr=True,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=_MODEL))
    db.session.commit()
    return worker


def _make_pair(make_user: Any, make_user_role: Any) -> tuple[ImageWaitingPrompt, ImageWorker]:
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    return _make_request(user), _make_worker(user)


@pytest.mark.parametrize(
    ("request_change", "worker_change", "model_names", "reason"),
    [
        ({}, {}, ["another-model"], "models"),
        ({"width": 2048, "height": 1024}, {}, [_MODEL], "max_pixels"),
        ({"slow_workers": False}, {"speed": 499_999}, [_MODEL], "performance"),
        ({"extra_slow_workers": False}, {"extra_slow_worker": True}, [_MODEL], "performance"),
    ],
)
def test_runtime_gate_rejects_worker_pop_constraints(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    request_change: dict[str, Any],
    worker_change: dict[str, Any],
    model_names: list[str],
    reason: str,
) -> None:
    request, worker = _make_pair(make_user, make_user_role)
    for name, value in request_change.items():
        setattr(request, name, value)
    for name, value in worker_change.items():
        setattr(worker, name, value)

    assert worker.can_generate_with_model_names(request, model_names) == [False, reason]


@pytest.mark.parametrize(
    ("capability", "params", "request_change"),
    [
        ("extra_source_images", {}, {"extra_source_images": {"esi": []}}),
        ("lora", {"loras": []}, {}),
        ("textual_inversion", {"tis": []}, {}),
        ("layer_diffuse", {"transparent": True}, {}),
    ],
)
def test_runtime_gate_rejects_missing_bridge_capability(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    params: dict[str, Any],
    request_change: dict[str, Any],
) -> None:
    request, worker = _make_pair(make_user, make_user_role)
    request.params.update(params)
    for name, value in request_change.items():
        setattr(request, name, value)
    monkeypatch.setattr("horde.classes.stable.worker.check_sampler_capability", lambda *_args: True)
    monkeypatch.setattr(
        "horde.classes.stable.worker.check_bridge_capability",
        lambda checked_capability, _bridge_agent: checked_capability != capability,
    )

    assert worker.can_generate_with_model_names(request, [_MODEL]) == [False, "bridge_version"]


def test_runtime_gate_allows_r2_request_without_bridge_capability(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, worker = _make_pair(make_user, make_user_role)
    request.r2 = True
    monkeypatch.setattr("horde.classes.stable.worker.check_sampler_capability", lambda *_args: True)
    monkeypatch.setattr(
        "horde.classes.stable.worker.check_bridge_capability",
        lambda checked_capability, _bridge_agent: checked_capability != "r2",
    )

    assert worker.can_generate_with_model_names(request, [_MODEL]) == [True, None]


@pytest.mark.parametrize(
    ("params", "worker_change", "reason"),
    [
        ({"loras": []}, {"allow_lora": False}, "lora"),
        ({"transparent": True}, {"allow_sdxl_controlnet": False}, "controlnet"),
    ],
)
def test_runtime_gate_honors_worker_capability_opt_outs(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    worker_change: dict[str, Any],
    reason: str,
) -> None:
    request, worker = _make_pair(make_user, make_user_role)
    request.params.update(params)
    for name, value in worker_change.items():
        setattr(worker, name, value)
    monkeypatch.setattr("horde.classes.stable.worker.check_sampler_capability", lambda *_args: True)
    monkeypatch.setattr("horde.classes.stable.worker.check_bridge_capability", lambda *_args: True)

    assert worker.can_generate_with_model_names(request, [_MODEL]) == [False, reason]


def test_runtime_gate_rejects_control_strength_on_a_bridge_that_cannot_read_it(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Read from the job payload rather than the request params, which is what the worker is handed.
    request, worker = _make_pair(make_user, make_user_role)
    request.gen_payload = {**request.params, "control_type": "canny", "control_strength": 0.8}
    monkeypatch.setattr("horde.classes.stable.worker.check_sampler_capability", lambda *_args: True)
    monkeypatch.setattr(
        "horde.classes.stable.worker.check_bridge_capability",
        lambda checked_capability, _bridge_agent: checked_capability != "control_strength",
    )

    assert worker.can_generate_with_model_names(request, [_MODEL]) == [False, "bridge_version"]


def test_runtime_gate_allows_control_strength_on_a_bridge_that_reads_it(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, worker = _make_pair(make_user, make_user_role)
    request.gen_payload = {**request.params, "control_type": "canny", "control_strength": 0.8}
    monkeypatch.setattr("horde.classes.stable.worker.check_sampler_capability", lambda *_args: True)
    monkeypatch.setattr("horde.classes.stable.worker.check_bridge_capability", lambda *_args: True)

    assert worker.can_generate_with_model_names(request, [_MODEL]) == [True, None]
