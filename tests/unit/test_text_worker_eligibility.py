# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify text and chat feasibility: the pop speed threshold, and chat eligibility by declaration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from horde.classes.base.user import UserRoleTypes
from horde.classes.base.worker import WorkerModel
from horde.classes.kobold.chat_request import ChatFeature
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


CHAT_MODEL = "koboldcpp/chat-eligibility-model"
"""The model the chat eligibility tests request and serve."""

CHAT_MESSAGES = [{"role": "user", "content": "Hello?"}]
"""A minimal conversation for a chat request."""


def _make_chat_request(user: Any, params: dict[str, Any] | None = None) -> TextWaitingPrompt:
    request = TextWaitingPrompt(
        worker_ids=[],
        models=[CHAT_MODEL],
        prompt="user: Hello?",
        user_id=user.id,
        params={"n": 1, "max_length": 512, "max_context_length": 4096, **(params or {})},
        is_chat=True,
        messages=CHAT_MESSAGES,
    )
    request.validated_backends = False
    db.session.commit()
    return request


def _make_chat_worker(user: Any, **chat_declaration: Any) -> TextWorker:
    worker = _make_text_worker(user, CHAT_MODEL, speed=10)
    worker.max_length = 80
    worker.max_context_length = 1024
    declaration = {
        "accepts_chat_jobs": True,
        "chat_features": [ChatFeature.SEED.value, ChatFeature.PRESENCE_PENALTY.value],
        "chat_max_context_length": 8192,
        "chat_max_length": 1024,
        "chat_model_id": CHAT_MODEL,
        **chat_declaration,
    }
    for column_name, value in declaration.items():
        setattr(worker, column_name, value)
    db.session.commit()
    return worker


def _chat_eligibility(worker: TextWorker, request: TextWaitingPrompt) -> list[Any]:
    return worker.can_generate_with_softprompt_names(request, [], model_names=[CHAT_MODEL])


def test_a_chat_request_is_refused_by_a_worker_that_did_not_declare_chat_jobs(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user, accepts_chat_jobs=False)

    assert _chat_eligibility(worker, _make_chat_request(user)) == [False, "chat_jobs"]


def test_a_chat_request_is_refused_by_a_worker_serving_another_chat_model(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user, chat_model_id="koboldcpp/another-model")

    assert _chat_eligibility(worker, _make_chat_request(user)) == [False, "chat_jobs"]


def test_chat_limits_and_not_text_limits_gate_a_chat_request(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user)

    assert _chat_eligibility(worker, _make_chat_request(user)) == [True, None]
    assert _chat_eligibility(worker, _make_chat_request(user, {"max_context_length": 16384})) == [False, "max_context_length"]
    assert _chat_eligibility(worker, _make_chat_request(user, {"max_length": 2048})) == [False, "max_length"]


def test_a_chat_worker_is_still_bounded_by_its_text_limits_for_text_requests(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user)
    text_request = _make_text_request(user, CHAT_MODEL)
    text_request.slow_workers = True
    text_request.max_context_length = 1024

    assert _chat_eligibility(worker, text_request) == [True, None]
    text_request.max_context_length = 4096
    assert _chat_eligibility(worker, text_request) == [False, "max_context_length"]


@pytest.mark.parametrize(
    ("request_params", "expected"),
    [
        ({}, [True, None]),
        ({"frequency_penalty": 0, "presence_penalty": 0}, [True, None]),
        ({"seed": 42, "presence_penalty": 0.5}, [True, None]),
        ({"frequency_penalty": 0.5}, [False, "chat_features"]),
    ],
)
def test_a_chat_request_needing_an_undeclared_feature_is_refused(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    request_params: dict[str, Any],
    expected: list[Any],
) -> None:
    user = make_user()
    worker = _make_chat_worker(user)

    assert _chat_eligibility(worker, _make_chat_request(user, request_params)) == expected


def test_availability_counts_a_chat_worker_by_its_chat_limits(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    _make_chat_worker(user)
    _make_chat_worker(user, chat_max_context_length=2048)

    availability = functions.get_worker_availability_for_request(_make_chat_request(user))

    assert availability.worker_count == 1


IMAGE_CHAT_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "horde-r2://request_chat_0.jpg"}},
        ],
    },
]
"""A conversation whose user message carries a stored image reference."""


@pytest.mark.parametrize(
    ("image_declaration", "expected"),
    [
        ({}, [False, "chat_features"]),
        ({"image_transports": ["url"]}, [False, "chat_features"]),
        ({"chat_features": [ChatFeature.VISION.value]}, [False, "chat_features"]),
        ({"chat_features": [ChatFeature.VISION.value], "image_transports": []}, [False, "chat_features"]),
        ({"chat_features": [ChatFeature.VISION.value], "image_transports": ["base64"]}, [True, None]),
        ({"chat_features": [ChatFeature.VISION.value], "image_transports": ["url"]}, [True, None]),
    ],
)
def test_a_chat_request_with_images_needs_vision_and_an_image_transport(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    image_declaration: dict[str, Any],
    expected: list[Any],
) -> None:
    user = make_user()
    worker = _make_chat_worker(user, **image_declaration)
    image_request = _make_chat_request(user)
    image_request.messages = IMAGE_CHAT_MESSAGES
    db.session.commit()

    assert _chat_eligibility(worker, image_request) == expected


def test_a_chat_request_without_images_needs_no_image_declaration(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user, image_transports=None)

    assert _chat_eligibility(worker, _make_chat_request(user)) == [True, None]


def test_the_pop_query_withholds_image_requests_from_a_worker_that_cannot_take_images(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    from horde.database.text_functions import get_sorted_text_wp_filtered_to_worker

    user = make_user()
    text_only_worker = _make_chat_worker(user)
    vision_worker = _make_chat_worker(user, chat_features=[ChatFeature.VISION.value], image_transports=["url"])
    plain_request = _make_chat_request(user)
    plain_request.slow_workers = True
    image_request = _make_chat_request(user)
    image_request.slow_workers = True
    image_request.messages = IMAGE_CHAT_MESSAGES
    for request in (plain_request, image_request):
        request.active = True
    db.session.commit()

    text_only_candidates = get_sorted_text_wp_filtered_to_worker(text_only_worker, [CHAT_MODEL])
    db.session.commit()
    vision_candidates = get_sorted_text_wp_filtered_to_worker(vision_worker, [CHAT_MODEL])

    assert [candidate.id for candidate in text_only_candidates] == [plain_request.id]
    assert {candidate.id for candidate in vision_candidates} == {plain_request.id, image_request.id}
    assert {candidate.id: candidate.carries_chat_images for candidate in vision_candidates} == {
        plain_request.id: False,
        image_request.id: True,
    }
