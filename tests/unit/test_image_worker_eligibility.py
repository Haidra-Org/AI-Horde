# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify that image runtime eligibility enforces worker-pop constraints."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde.apis.v2.stable import ImageJobPop, parsers
from horde.classes.base.user import UserRoleTypes
from horde.classes.base.waiting_prompt import WPModels
from horde.classes.base.worker import WorkerModel
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.flask import db
from tests.unit.model_reference_seed import seed_image_reference

pytestmark = pytest.mark.unit

_MODEL = "stable_diffusion"


@pytest.fixture(autouse=True)
def _stub_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep worker checks independent of the network-backed model reference."""
    seed_image_reference(monkeypatch, {_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})


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


def _judge_with_model_names(worker: ImageWorker, request: ImageWaitingPrompt, model_names: list[str]) -> list[bool | str | None]:
    """Evaluate the worker as if it advertised ``model_names``, the way the availability query supplies them."""
    worker_inputs = replace(worker.get_eligibility_inputs(request), model_names=tuple(model_names))
    return worker.can_generate_with_eligibility_inputs(request, request.get_eligibility_inputs(), worker_inputs)


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

    assert _judge_with_model_names(worker, request, model_names) == [False, reason]


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

    assert _judge_with_model_names(worker, request, [_MODEL]) == [False, "bridge_version"]


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

    assert _judge_with_model_names(worker, request, [_MODEL]) == [True, None]


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

    assert _judge_with_model_names(worker, request, [_MODEL]) == [False, reason]


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

    assert _judge_with_model_names(worker, request, [_MODEL]) == [False, "bridge_version"]


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

    assert _judge_with_model_names(worker, request, [_MODEL]) == [True, None]


def test_runtime_gate_judges_baseline_features_on_the_requested_models_only(
    db_session: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker also holding a family no release renders hires-fix on still serves an SD1 hires-fix job."""
    flux_model = "flux-model"
    seed_image_reference(
        monkeypatch,
        {
            _MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1,
            flux_model: KNOWN_IMAGE_GENERATION_BASELINE.flux_1,
        },
    )
    request, worker = _make_pair(make_user, make_user_role)
    db.session.add(WorkerModel(worker_id=worker.id, model=flux_model))
    db.session.commit()
    request.params["hires_fix"] = True
    worker.bridge_agent = "AI Horde Worker reGen:18.4.1:https://github.com/Haidra-Org/horde-worker-reGen"
    monkeypatch.setattr("horde.classes.stable.worker.check_sampler_capability", lambda *_args: True)

    assert _judge_with_model_names(worker, request, [_MODEL, flux_model]) == [True, None]

    request.models = [WPModels(model=flux_model, wp_id=request.id)]
    db.session.commit()
    assert _judge_with_model_names(worker, request, [_MODEL, flux_model]) == [False, "bridge_version"]


def _reload_policy_worker(make_user: Any, make_user_role: Any, *, debounced: bool = True) -> ImageWorker:
    """Load a fresh ORM worker so request-local policy cannot leak from a previous pop."""
    owner = make_user()
    make_user_role(owner, UserRoleTypes.TRUSTED)
    worker = _make_worker(owner)
    worker.created = datetime.utcnow() - timedelta(minutes=5)
    worker.last_check_in = datetime.utcnow() - timedelta(seconds=0 if debounced else 40)
    worker.uptime = worker.last_reward_uptime = 1000
    db.session.commit()
    worker_id = worker.id
    db.session.expunge(worker)
    return db.session.get(ImageWorker, worker_id)


def _check_in_image_pop(
    app: Any,
    worker: ImageWorker,
    *,
    require_upfront_kudos: bool,
    priority_usernames: list[str] | None = None,
) -> None:
    """Exercise the image parser and its actual check-in keyword mapping."""
    with app.test_request_context(
        "/api/v2/generate/pop",
        method="POST",
        headers={"apikey": "test-worker-key"},
        json={
            "name": worker.name,
            "models": [_MODEL],
            "max_pixels": worker.max_pixels,
            "bridge_agent": "AI Horde Worker reGen:18.4.1:https://github.com/Haidra-Org/horde-worker-reGen",
            "require_upfront_kudos": require_upfront_kudos,
            "priority_usernames": priority_usernames or [],
        },
    ):
        pop = ImageJobPop()
        pop.args = parsers.job_pop_parser.parse_args()
        pop.worker = worker
        pop.models = pop.args.models
        pop.blacklist = pop.args.blacklist or []
        pop.priority_usernames = pop.args.priority_usernames or []
        pop.safe_ip = True
        pop.worker_ip = "127.0.0.1"
        pop.check_in()


@pytest.mark.parametrize("debounced", [False, True], ids=["bookkeeping-due", "debounced"])
@pytest.mark.parametrize(
    ("require_upfront_kudos", "trusted", "priority", "kudos", "expected"),
    [
        (True, False, False, 10, [False, "kudos"]),
        (True, True, False, 10, [True, None]),
        (True, False, True, 10, [True, None]),
        (False, False, False, 10, [True, None]),
        (True, False, False, 124, [False, "kudos"]),
        (True, False, False, 125, [True, None]),
    ],
    ids=[
        "insufficient-kudos",
        "trusted-exception",
        "priority-exception",
        "no-upfront-requirement",
        "below-spendable-threshold",
        "at-spendable-threshold",
    ],
)
def test_image_pop_applies_policy_to_a_fresh_worker(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    debounced: bool,
    require_upfront_kudos: bool,
    trusted: bool,
    priority: bool,
    kudos: int,
    expected: list[bool | str | None],
) -> None:
    worker = _reload_policy_worker(make_user, make_user_role, debounced=debounced)
    requester = make_user(kudos=kudos)
    if trusted:
        make_user_role(requester, UserRoleTypes.TRUSTED)
    request = _make_request(requester)
    request.kudos = 100

    _check_in_image_pop(
        app,
        worker,
        require_upfront_kudos=require_upfront_kudos,
        priority_usernames=[requester.get_unique_alias()] if priority else [],
    )

    assert _judge_with_model_names(worker, request, [_MODEL]) == expected


def test_debounced_image_policy_does_not_write_check_in_bookkeeping(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    assert_query_count: Any,
) -> None:
    worker = _reload_policy_worker(make_user, make_user_role)
    last_check_in, uptime, last_reward = worker.last_check_in, worker.uptime, worker.last_reward_uptime

    with assert_query_count() as queries:
        _check_in_image_pop(app, worker, require_upfront_kudos=True, priority_usernames=["priority#123"])
        db.session.flush()

    assert worker.require_upfront_kudos is True
    assert worker.prioritized_users == ["priority#123"]
    assert (worker.last_check_in, worker.uptime, worker.last_reward_uptime) == (last_check_in, uptime, last_reward)
    assert not queries.of_kind("INSERT")
    assert not queries.of_kind("UPDATE")
    assert not queries.of_kind("DELETE")


@pytest.mark.parametrize("require_upfront_kudos", [False, True])
def test_next_debounced_pop_replaces_previous_policy_inputs(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    require_upfront_kudos: bool,
) -> None:
    worker = _reload_policy_worker(make_user, make_user_role)
    requester = make_user(kudos=10)
    request = _make_request(requester)
    request.kudos = 100
    _check_in_image_pop(app, worker, require_upfront_kudos=True, priority_usernames=[requester.get_unique_alias()])
    assert _judge_with_model_names(worker, request, [_MODEL]) == [True, None]

    _check_in_image_pop(app, worker, require_upfront_kudos=require_upfront_kudos)

    assert worker.require_upfront_kudos is require_upfront_kudos
    assert worker.prioritized_users == []
    assert _judge_with_model_names(worker, request, [_MODEL]) == ([False, "kudos"] if require_upfront_kudos else [True, None])
