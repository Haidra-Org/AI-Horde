# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify text and chat feasibility: the pop speed threshold, and chat eligibility by declaration."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from horde_sdk.generation_parameters.text.chat_policy import ChatConformance, ChatFeature, ChatGuarantee

from horde.apis.v2.base import GenerateTemplate
from horde.apis.v2.kobold import TextAsyncGenerate
from horde.classes.base.user import UserRoleTypes
from horde.classes.base.worker import WorkerModel
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TEXT_WORKER_DECLARATION_GENERATION_KEY, TextWorker, get_minimum_text_worker_speed
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


def test_queuing_a_text_request_seeds_the_availability_its_first_status_poll_reads(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_name = "seeded-model"
    monkeypatch.setattr(
        "horde.classes.kobold.worker.model_reference.get_text_model_multiplier",
        lambda _model_name: 20,
    )
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, model_name)
    _make_text_worker(user, model_name, speed=10)

    def create_request(resource: TextAsyncGenerate) -> None:
        resource.wp = request
        resource.kudos = 1

    resource = TextAsyncGenerate()
    monkeypatch.setattr(resource, "parse_request_args", lambda: SimpleNamespace(context_fit=None, dry_run=False))
    monkeypatch.setattr(GenerateTemplate, "post", create_request)

    response, status_code = resource.queue_generation()

    assert status_code == 202
    assert "message" not in response
    assert json.loads(fake_redis.horde_r_get(f"wp_availability_{request.id}")) == {
        "workers": 1,
        "threads": 1,
        "might_stall": False,
        "impossible_reason": None,
        "declaration_generation": 0,
    }

    def fail_worker_query(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the first status poll repeated the eligible-worker query")

    monkeypatch.setattr(functions, "_iter_eligible_workers_for_request", fail_worker_query)
    assert functions.get_worker_availability_for_request(request).worker_count == 1


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

STRICT_BASELINE_GUARANTEE = ChatGuarantee.PROMPT_OVERFLOW_REFUSED.value
"""The guarantee every strict chat request needs, which a conforming test worker declares."""

TOOL_EXTRAS: dict[str, Any] = {
    "tools": [{"type": "function", "function": {"name": "lookup_weather", "parameters": {"type": "object"}}}],
}
"""Stored extras offering one tool under the default ``tool_choice`` of ``auto``."""


def _make_chat_request(
    user: Any,
    params: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    *,
    chat_extras: dict[str, Any] | None = None,
    conformance: ChatConformance = ChatConformance.STRICT,
) -> TextWaitingPrompt:
    request = TextWaitingPrompt(
        worker_ids=[],
        models=[CHAT_MODEL],
        prompt="user: Hello?",
        user_id=user.id,
        params={"n": 1, "max_length": 512, "max_context_length": 4096, **(params or {})},
        is_chat=True,
        messages=CHAT_MESSAGES if messages is None else messages,
        chat_extras=chat_extras,
        chat_conformance=conformance.value,
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
        "chat_features": [ChatFeature.SEED.value, ChatFeature.PRESENCE_PENALTY.value, STRICT_BASELINE_GUARANTEE],
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
        ({"chat_features": [ChatFeature.VISION.value, STRICT_BASELINE_GUARANTEE]}, [False, "chat_features"]),
        ({"chat_features": [ChatFeature.VISION.value, STRICT_BASELINE_GUARANTEE], "image_transports": []}, [False, "chat_features"]),
        ({"chat_features": [ChatFeature.VISION.value, STRICT_BASELINE_GUARANTEE], "image_transports": ["base64"]}, [True, None]),
        ({"chat_features": [ChatFeature.VISION.value, STRICT_BASELINE_GUARANTEE], "image_transports": ["url"]}, [True, None]),
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
    image_request = _make_chat_request(user, messages=IMAGE_CHAT_MESSAGES)

    assert _chat_eligibility(worker, image_request) == expected


def test_a_strict_chat_request_is_refused_by_a_worker_without_the_guarantees_it_needs(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """Under strict conformance a missing guarantee refuses the worker; under best effort it does not."""
    user = make_user()
    worker = _make_chat_worker(user, chat_features=[ChatFeature.SEED.value])

    assert _chat_eligibility(worker, _make_chat_request(user)) == [False, "chat_features"]
    assert _chat_eligibility(worker, _make_chat_request(user, conformance=ChatConformance.BEST_EFFORT)) == [True, None]


@pytest.mark.parametrize(
    ("tools_max_temperature", "temperature", "expected"),
    [
        (None, 1.5, [True, None]),
        (1.0, 1.0, [True, None]),
        (1.0, 1.5, [False, "tools_max_temperature"]),
    ],
)
def test_a_tool_temperature_limit_refuses_a_hotter_request_that_forwards_tools(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    tools_max_temperature: float | None,
    temperature: float,
    expected: list[Any],
) -> None:
    """A worker without the limit applies any temperature with tools; one with it takes only requests within it."""
    user = make_user()
    worker = _make_chat_worker(
        user,
        chat_features=[ChatFeature.TOOLS.value, STRICT_BASELINE_GUARANTEE],
        chat_tools_max_temperature=tools_max_temperature,
    )
    request = _make_chat_request(user, {"temperature": temperature}, chat_extras=TOOL_EXTRAS)

    assert _chat_eligibility(worker, request) == expected


def test_best_effort_does_not_relax_the_tool_temperature_limit(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user, chat_features=[ChatFeature.TOOLS.value], chat_tools_max_temperature=1.0)
    request = _make_chat_request(user, {"temperature": 1.5}, chat_extras=TOOL_EXTRAS, conformance=ChatConformance.BEST_EFFORT)

    assert _chat_eligibility(worker, request) == [False, "tools_max_temperature"]


def test_a_tool_temperature_limit_does_not_apply_without_forwarded_tools(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """Under ``tool_choice: none`` the tools are not forwarded, so the backend does not clamp the temperature."""
    user = make_user()
    worker = _make_chat_worker(user, chat_tools_max_temperature=1.0)
    request = _make_chat_request(user, {"temperature": 1.5}, chat_extras={**TOOL_EXTRAS, "tool_choice": "none"})

    assert request.chat_tools_temperature is None
    assert _chat_eligibility(worker, request) == [True, None]


def test_the_pop_query_withholds_a_hotter_tool_request_from_a_worker_with_a_tool_temperature_limit(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    from horde.database.text_functions import get_sorted_text_wp_filtered_to_worker

    user = make_user()
    tool_features = [ChatFeature.TOOLS.value, STRICT_BASELINE_GUARANTEE]
    limited_worker = _make_chat_worker(user, chat_features=tool_features, chat_tools_max_temperature=1.0)
    unlimited_worker = _make_chat_worker(user, chat_features=tool_features)
    hot_request = _make_chat_request(user, {"temperature": 1.5}, chat_extras=TOOL_EXTRAS)
    cool_request = _make_chat_request(user, {"temperature": 0.7}, chat_extras=TOOL_EXTRAS)
    for request in (hot_request, cool_request):
        request.slow_workers = True
        request.active = True
    db.session.commit()

    limited_candidates = get_sorted_text_wp_filtered_to_worker(limited_worker, [CHAT_MODEL])
    db.session.commit()
    unlimited_candidates = get_sorted_text_wp_filtered_to_worker(unlimited_worker, [CHAT_MODEL])

    assert [candidate.id for candidate in limited_candidates] == [cool_request.id]
    assert {candidate.id for candidate in unlimited_candidates} == {hot_request.id, cool_request.id}


def test_a_check_in_stores_known_guarantees_and_the_tool_temperature_limit(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """Capability and guarantee names share one declared list; unknown names are dropped. The check-in debounce
    does not hold the declaration back."""
    user = make_user()
    worker = _make_chat_worker(user)
    worker.created = datetime.utcnow() - timedelta(minutes=5)
    worker.last_check_in = datetime.utcnow()
    db.session.commit()

    worker.check_in(
        80,
        1024,
        [],
        accepts_chat_jobs=True,
        chat_features=[ChatFeature.TOOLS.value, ChatGuarantee.TOOL_CHOICE_ENFORCED.value, "teleportation"],
        chat_max_context_length=8192,
        chat_max_length=1024,
        chat_tools_max_temperature=1.0,
        chat_model_id=CHAT_MODEL,
    )

    assert worker.chat_features == [ChatGuarantee.TOOL_CHOICE_ENFORCED.value, ChatFeature.TOOLS.value]
    assert worker.chat_tools_max_temperature == 1.0

    worker.check_in(80, 1024, [], accepts_chat_jobs=False, chat_tools_max_temperature=1.0)

    assert worker.chat_tools_max_temperature is None


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
    vision_worker = _make_chat_worker(user, chat_features=[ChatFeature.VISION.value, STRICT_BASELINE_GUARANTEE], image_transports=["url"])
    plain_request = _make_chat_request(user)
    plain_request.slow_workers = True
    image_request = _make_chat_request(user, messages=IMAGE_CHAT_MESSAGES)
    image_request.slow_workers = True
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


def test_a_chat_only_worker_refuses_text_requests_and_serves_chat_requests(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """A worker that turned text jobs off is refused every text request, whatever its text limits."""
    user = make_user()
    worker = _make_chat_worker(user, accepts_prompt_jobs=False)
    text_request = _make_text_request(user, CHAT_MODEL)
    text_request.slow_workers = True
    text_request.max_context_length = 512

    assert _chat_eligibility(worker, text_request) == [False, "prompt_jobs"]
    assert _chat_eligibility(worker, _make_chat_request(user)) == [True, None]


def test_availability_and_context_growth_leave_out_a_chat_only_worker_for_text_requests(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
) -> None:
    """A text request is not possible, and its context cannot grow, on the strength of a chat-only worker."""
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    _make_chat_worker(user, accepts_prompt_jobs=False)
    text_request = _make_text_request(user, CHAT_MODEL)
    text_request.max_context_length = 512

    assert functions.get_worker_availability_for_request(text_request).worker_count == 0
    assert functions.get_highest_text_worker_max_context_length([CHAT_MODEL]) is None
    assert functions.get_worker_availability_for_request(_make_chat_request(user)).worker_count == 1
    assert functions.get_highest_text_worker_max_context_length([CHAT_MODEL], accepts_chat_jobs=True) == 8192


def test_the_pop_query_gives_a_chat_only_worker_only_chat_requests(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """The text branch of the pop query is dropped for a chat-only worker; the chat branch is unchanged."""
    from horde.database.text_functions import get_sorted_text_wp_filtered_to_worker

    user = make_user()
    chat_only_worker = _make_chat_worker(user, accepts_prompt_jobs=False)
    both_worker = _make_chat_worker(user)
    text_request = _make_text_request(user, CHAT_MODEL)
    text_request.max_context_length = 512
    chat_request = _make_chat_request(user)
    for request in (text_request, chat_request):
        request.slow_workers = True
        request.active = True
    db.session.commit()

    chat_only_candidates = get_sorted_text_wp_filtered_to_worker(chat_only_worker, [CHAT_MODEL])
    db.session.commit()
    both_candidates = get_sorted_text_wp_filtered_to_worker(both_worker, [CHAT_MODEL])

    assert [candidate.id for candidate in chat_only_candidates] == [chat_request.id]
    assert {candidate.id for candidate in both_candidates} == {text_request.id, chat_request.id}


@pytest.mark.parametrize(
    ("accepts_chat_jobs", "accepts_prompt_jobs", "stored"),
    [(True, False, False), (True, True, True), (False, False, True)],
)
def test_a_debounced_check_in_stores_the_text_job_opt_out(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    accepts_chat_jobs: bool,
    accepts_prompt_jobs: bool,
    stored: bool,
) -> None:
    """The opt-out is stored on every check-in, and only a worker that accepts chat jobs can turn text jobs off."""
    user = make_user()
    worker = _make_chat_worker(user)
    worker.created = datetime.utcnow() - timedelta(minutes=5)
    worker.last_check_in = datetime.utcnow()
    db.session.commit()

    worker.check_in(
        80,
        1024,
        [],
        accepts_chat_jobs=accepts_chat_jobs,
        accepts_prompt_jobs=accepts_prompt_jobs,
        chat_max_context_length=8192,
        chat_max_length=1024,
        chat_model_id=CHAT_MODEL,
    )
    db.session.commit()

    assert worker.accepts_prompt_jobs is stored


def _declaration_generation(fake_redis: Any) -> int:
    return int(fake_redis.horde_r.get(TEXT_WORKER_DECLARATION_GENERATION_KEY) or 0)


def _make_debounced_chat_worker(user: Any) -> TextWorker:
    """Return a chat worker whose next check-in falls inside the debounce, which still replaces the declaration."""
    worker = _make_chat_worker(user)
    worker.created = datetime.utcnow() - timedelta(minutes=5)
    worker.last_check_in = datetime.utcnow()
    db.session.commit()
    return worker


def _check_in_chat_declaration(worker: TextWorker, **chat_declaration: Any) -> None:
    declaration = {
        "accepts_chat_jobs": True,
        "chat_features": [ChatFeature.SEED.value, ChatFeature.PRESENCE_PENALTY.value, STRICT_BASELINE_GUARANTEE],
        "chat_max_context_length": 8192,
        "chat_max_length": 1024,
        "chat_model_id": CHAT_MODEL,
        **chat_declaration,
    }
    worker.check_in(80, 1024, [], **declaration)


def test_cached_chat_availability_is_recomputed_after_a_worker_declaration_change(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """A worker popping with a new declaration is reflected at the next poll, while the cache entry is still live."""
    user = make_user()
    worker = _make_debounced_chat_worker(user)
    request = _make_chat_request(user)

    assert functions.get_worker_availability_for_request(request).worker_count == 1

    _check_in_chat_declaration(worker, accepts_chat_jobs=False)

    assert fake_redis.horde_r.get(f"wp_availability_{request.id}") is not None
    availability = functions.get_worker_availability_for_request(request)
    assert availability.worker_count == 0
    assert availability.is_possible is False


def test_a_steady_chat_check_in_does_not_bump_the_declaration_generation(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_debounced_chat_worker(user)
    _check_in_chat_declaration(worker)
    generation = _declaration_generation(fake_redis)

    _check_in_chat_declaration(worker)
    _check_in_chat_declaration(worker)

    assert _declaration_generation(fake_redis) == generation


def test_a_changed_chat_declaration_bumps_the_declaration_generation_once(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_debounced_chat_worker(user)
    _check_in_chat_declaration(worker)
    generation = _declaration_generation(fake_redis)

    _check_in_chat_declaration(worker, chat_tools_max_temperature=1.0)
    _check_in_chat_declaration(worker, chat_tools_max_temperature=1.0)

    assert _declaration_generation(fake_redis) == generation + 1
    db.session.expire(worker)
    assert worker.chat_tools_max_temperature == 1.0


def test_a_changed_model_list_bumps_the_declaration_generation_once(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    worker = _make_chat_worker(user)

    assert worker.set_models([CHAT_MODEL]) is False
    assert _declaration_generation(fake_redis) == 0

    assert worker.set_models([CHAT_MODEL, "koboldcpp/another-model"]) is True
    assert worker.set_models([CHAT_MODEL, "koboldcpp/another-model"]) is False
    assert _declaration_generation(fake_redis) == 1
