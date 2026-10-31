# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify text and chat feasibility: the pop speed threshold, and chat eligibility by declaration."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from horde_sdk.generation_parameters.text.chat_policy import ChatConformance, ChatFeature, ChatGuarantee

from horde.apis.v2.base import GenerateTemplate
from horde.apis.v2.kobold import TextAsyncGenerate, read_text_request_status
from horde.chat_contract import ChatImpossibleReason
from horde.classes.base.user import UserRoleTypes
from horde.classes.base.worker import WorkerBlackList, WorkerModel
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import (
    TEXT_WORKER_DECLARATION_GENERATION_KEY,
    UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND,
    TextWorker,
    TextWorkerSoftprompts,
    get_minimum_text_worker_speed,
)
from horde.database import functions, threads
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
        "worker_profiles": [
            {
                "models": ["seeded-model"],
                "accepts_prompt_jobs": True,
                "max_length": 512,
                "max_context_length": 4096,
                "chat_model_id": None,
                "chat_max_length": None,
                "chat_max_context_length": None,
            },
        ],
        "tokens_per_second": 10.0,
    }

    def fail_worker_query(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the first status poll repeated the eligible-worker query")

    monkeypatch.setattr(functions, "_iter_eligible_workers_for_request", fail_worker_query)
    assert functions.get_worker_availability_for_request(request).worker_count == 1


def _judge_with_names(worker: TextWorker, request: TextWaitingPrompt, *, model_names: list[str]) -> list[Any]:
    """Evaluate the worker with ``model_names`` and no softprompts in place of the inputs the availability query would supply."""
    worker_inputs = replace(worker.get_eligibility_inputs(request), model_names=tuple(model_names), softprompt_names=())
    return worker.can_generate_with_eligibility_inputs(request, request.get_eligibility_inputs(), worker_inputs)


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

    can_generate, reason = _judge_with_names(worker, request, model_names=["worker-model"])

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
    return _judge_with_names(worker, request, model_names=[CHAT_MODEL])


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


def test_availability_counts_a_slow_worker_for_a_legacy_request_over_its_capacity_and_not_for_a_chat_request(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """A worker at 3 tokens/s (capacity 360) counts toward a legacy request for 512 tokens, which the speed gate does
    not bound, and not toward a chat request for 512 tokens, which the gate refuses it with ``lease_speed``."""
    user = make_user()
    _make_text_worker(user, "slow-model", speed=3)
    slow_chat_worker = _make_chat_worker(user, speed=3)
    legacy_request = _queue_text_request(user, ["slow-model"], max_length=512)
    legacy_request.slow_workers = True
    db.session.commit()
    chat_request = _make_chat_request(user, {"max_length": 512})

    legacy_availability = functions.get_worker_availability_for_request(legacy_request)
    chat_availability = functions.get_worker_availability_for_request(chat_request)

    assert legacy_availability.worker_count == 1
    assert chat_availability.worker_count == 0
    assert _chat_eligibility(slow_chat_worker, chat_request) == [False, "lease_speed"]


@pytest.mark.parametrize(
    ("request_params", "exceeded_limits"),
    [
        ({"max_length": 2048}, ("max_length",)),
        ({"max_context_length": 16384}, ("max_context_length",)),
    ],
)
def test_availability_reports_a_request_over_every_chat_limit_as_a_declaration_gap(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    request_params: dict[str, Any],
    exceeded_limits: tuple[str, ...],
) -> None:
    user = make_user()
    _make_chat_worker(user)

    availability = functions.get_worker_availability_for_request(_make_chat_request(user, request_params))

    assert availability.worker_count == 0
    assert availability.impossible_reason == ChatImpossibleReason(exceeded_limits=exceeded_limits)


def test_a_request_over_one_worker_chat_limit_is_not_a_gap_while_another_worker_fits(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    user = make_user()
    _make_chat_worker(user)
    # Fast enough to finish the 2048 tokens inside the base lease, so only the chat limit decides.
    _make_chat_worker(user, chat_max_length=4096, speed=20)

    availability = functions.get_worker_availability_for_request(_make_chat_request(user, {"max_length": 2048}))

    assert availability.worker_count == 1
    assert availability.impossible_reason is None


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
        (
            {
                "chat_features": [ChatFeature.VISION.value, STRICT_BASELINE_GUARANTEE],
                "image_transports": ["messages_url"],
            },
            [False, "chat_features"],
        ),
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


def test_a_declaration_change_counts_the_live_chat_cache_entry_as_a_generation_mismatch(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_attributes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        functions,
        "worker_availability_cache_lookups",
        SimpleNamespace(add=lambda _amount, attributes: recorded_attributes.append(attributes)),
    )
    user = make_user()
    worker = _make_debounced_chat_worker(user)
    request = _make_chat_request(user)

    functions.get_worker_availability_for_request(request)
    _check_in_chat_declaration(worker, accepts_chat_jobs=False)
    functions.get_worker_availability_for_request(request)

    assert recorded_attributes == [
        {"horde.outcome": "absent", "horde.gentype": "text", "horde.chat": True},
        {"horde.outcome": "generation_mismatch", "horde.gentype": "text", "horde.chat": True},
    ]


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


def test_text_eligibility_refusals_match_can_generate(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
) -> None:
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, "softprompt-model")
    request.softprompt = "wanted_softprompt"
    matching = _make_text_worker(user, "softprompt-model", speed=10)
    db.session.add(TextWorkerSoftprompts(worker_id=matching.id, softprompt="other_softprompt"))
    db.session.add(TextWorkerSoftprompts(worker_id=matching.id, softprompt="wanted_softprompt"))
    mismatched = _make_text_worker(user, "softprompt-model", speed=10)
    db.session.add(TextWorkerSoftprompts(worker_id=mismatched.id, softprompt="other_softprompt"))
    without_softprompts = _make_text_worker(user, "softprompt-model", speed=10)
    blacklisted = _make_text_worker(user, "softprompt-model", speed=10)
    db.session.add(TextWorkerSoftprompts(worker_id=blacklisted.id, softprompt="wanted_softprompt"))
    db.session.add(WorkerBlackList(worker_id=blacklisted.id, word="Eligibility"))
    db.session.commit()
    workers = (matching, mismatched, without_softprompts, blacklisted)
    expected_reasons = {
        matching.id: None,
        mismatched.id: "matching_softprompt",
        without_softprompts.id: "matching_softprompt",
        blacklisted.id: "blacklist",
    }

    def reasons_from_eligibility() -> dict[Any, str | None]:
        refusals: list[tuple[Any, str]] = []
        eligible_workers = list(functions._iter_eligible_workers_for_request(request, refusals))
        reasons: dict[Any, str | None] = {eligible_worker.worker.id: None for eligible_worker in eligible_workers}
        reasons.update({worker.id: reason for worker, reason in refusals})
        return reasons

    def reasons_from_can_generate() -> dict[Any, str | None]:
        return {worker.id: worker.can_generate(request)[1] for worker in workers}

    assert reasons_from_eligibility() == expected_reasons
    assert reasons_from_can_generate() == expected_reasons
    db.session.expire_all()
    assert reasons_from_eligibility() == expected_reasons
    assert reasons_from_can_generate() == expected_reasons


def test_direct_and_query_eligibility_inputs_match_for_a_request_without_a_softprompt(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
) -> None:
    """A worker's eligibility inputs, softprompt names included, do not depend on whether the request names a softprompt."""
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, "softprompt-model")
    worker = _make_text_worker(user, "softprompt-model", speed=10)
    db.session.add(TextWorkerSoftprompts(worker_id=worker.id, softprompt="first_softprompt"))
    db.session.add(TextWorkerSoftprompts(worker_id=worker.id, softprompt="second_softprompt"))
    db.session.add(WorkerBlackList(worker_id=worker.id, word="unrelated"))
    db.session.commit()

    eligible_workers = list(functions._iter_eligible_workers_for_request(request))

    assert [eligible_worker.worker.id for eligible_worker in eligible_workers] == [worker.id]
    assert eligible_workers[0].eligibility_inputs.softprompt_names == ("first_softprompt", "second_softprompt")
    assert worker.get_eligibility_inputs(request) == eligible_workers[0].eligibility_inputs


TEXT_REQUEST_AVG = 10.0
"""The text throughput per worker thread, in tokens per second, the wait estimate tests store as the horde average."""


def _queue_text_request(user: Any, model_names: list[str], *, max_length: int) -> TextWaitingPrompt:
    """Create an active text request for ``model_names``, an empty list accepting any model."""
    request = TextWaitingPrompt(
        worker_ids=[],
        models=model_names,
        prompt="text wait estimate test",
        user_id=user.id,
        params={"n": 1, "max_length": max_length, "max_context_length": 2048},
    )
    request.slow_workers = False
    request.validated_backends = False
    request.active = True
    db.session.commit()
    return request


def _read_status_after_queue_refresh(request: TextWaitingPrompt, fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Store the prioritized queues as the primary does and return the request's text status.

    The in-process copies of the stored queue are cleared first, so the status reads what was just stored.
    """
    fake_redis.horde_r_setex("text_worker_performances_avg_cache", timedelta(seconds=60), TEXT_REQUEST_AVG)
    threads.store_prioritized_wp_queue()
    monkeypatch.setitem(functions._wp_queue_cache, "text", None)
    monkeypatch.setitem(functions._wp_queue_positions_time, "text", 0.0)
    monkeypatch.setitem(functions._wp_queue_positions_cache, "text", {})
    return read_text_request_status(request)


def test_a_request_for_a_model_without_a_queue_of_its_own_reads_a_short_wait_behind_a_long_horde_queue(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requests ahead for models no eligible worker serves do not count toward the position or the wait."""
    monkeypatch.setattr("horde.classes.kobold.worker.model_reference.get_text_model_multiplier", lambda _model_name: 20)
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    for _ in range(20):
        _queue_text_request(user, ["busy-model"], max_length=512)
    request = _queue_text_request(user, ["quiet-model"], max_length=80)
    _make_text_worker(user, "busy-model", speed=10)
    _make_text_worker(user, "quiet-model", speed=10)

    status = _read_status_after_queue_refresh(request, fake_redis, monkeypatch)

    assert status["is_possible"] is True
    assert status["eligible_worker_threads"] == 1
    assert status["queue_position"] == 1
    assert status["wait_time"] == round(80 / TEXT_REQUEST_AVG)


def test_a_request_behind_its_own_models_queue_reads_that_work_over_the_eligible_threads(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is the competing work, including a request that accepts any model, over the eligible thread throughput."""
    monkeypatch.setattr("horde.classes.kobold.worker.model_reference.get_text_model_multiplier", lambda _model_name: 20)
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    for _ in range(6):
        _queue_text_request(user, ["shared-model"], max_length=200)
    _queue_text_request(user, [], max_length=200)
    for _ in range(5):
        _queue_text_request(user, ["busy-model"], max_length=512)
    request = _queue_text_request(user, ["shared-model"], max_length=80)
    eligible_worker = _make_text_worker(user, "shared-model", speed=10)
    eligible_worker.threads = 3
    _make_text_worker(user, "busy-model", speed=10)
    db.session.commit()

    status = _read_status_after_queue_refresh(request, fake_redis, monkeypatch)

    competing_work = 7 * 200 + 80
    assert status["eligible_worker_threads"] == 3
    assert status["queue_position"] == 8
    assert status["wait_time"] == round(competing_work / (TEXT_REQUEST_AVG * 3))


def test_the_wait_is_measured_at_the_eligible_workers_speed_rather_than_the_horde_average(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker faster than the horde average clears the same queue in proportionally less time."""
    monkeypatch.setattr("horde.classes.kobold.worker.model_reference.get_text_model_multiplier", lambda _model_name: 20)
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    for _ in range(6):
        _queue_text_request(user, ["shared-model"], max_length=200)
    request = _queue_text_request(user, ["shared-model"], max_length=80)
    fast_worker = _make_text_worker(user, "shared-model", speed=4 * TEXT_REQUEST_AVG)
    fast_worker.threads = 2
    db.session.commit()

    status = _read_status_after_queue_refresh(request, fake_redis, monkeypatch)

    assert status["wait_time"] == round((6 * 200 + 80) / (4 * TEXT_REQUEST_AVG * 2))


def test_a_queued_request_over_every_eligible_workers_length_limit_does_not_count_ahead(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request ahead on the same model that asks for more tokens than the eligible worker generates is not ahead."""
    monkeypatch.setattr("horde.classes.kobold.worker.model_reference.get_text_model_multiplier", lambda _model_name: 20)
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    _queue_text_request(user, ["shared-model"], max_length=1024)
    _queue_text_request(user, ["shared-model"], max_length=200)
    request = _queue_text_request(user, ["shared-model"], max_length=80)
    _make_text_worker(user, "shared-model", speed=10)

    status = _read_status_after_queue_refresh(request, fake_redis, monkeypatch)

    assert status["queue_position"] == 2
    assert status["wait_time"] == round((200 + 80) / TEXT_REQUEST_AVG)


def test_a_request_without_eligible_threads_reads_impossible_with_the_horde_wide_estimate(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no eligible thread the status reports the request impossible and estimates the wait from the whole queue."""
    monkeypatch.setattr("horde.classes.kobold.worker.model_reference.get_text_model_multiplier", lambda _model_name: 20)
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    _queue_text_request(user, ["busy-model"], max_length=512)
    request = _queue_text_request(user, ["unserved-model"], max_length=80)
    _make_text_worker(user, "busy-model", speed=10)

    status = _read_status_after_queue_refresh(request, fake_redis, monkeypatch)

    assert status["is_possible"] is False
    assert status["eligible_workers"] == 0
    assert status["eligible_worker_threads"] == 0
    assert status["queue_position"] == 2
    assert status["wait_time"] == round((512 + 80) / TEXT_REQUEST_AVG)


def _refresh_stored_queue(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Store the prioritized queues as the primary does and clear the in-process copies, so the next read loads them."""
    threads.store_prioritized_wp_queue()
    monkeypatch.setitem(functions._wp_queue_cache, "text", None)
    monkeypatch.setitem(functions._wp_queue_positions_time, "text", 0.0)
    monkeypatch.setitem(functions._wp_queue_positions_cache, "text", {})


def _text_worker_profile(model_names: list[str], **declaration: Any) -> functions.EligibleTextWorkerProfile:
    """Return the profile of a text worker serving ``model_names``, with the test worker's text limits by default."""
    profile_fields: dict[str, Any] = {
        "model_names": frozenset(model_names),
        "accepts_prompt_jobs": True,
        "max_length": 512,
        "max_context_length": 4096,
        "chat_model_id": None,
        "chat_max_length": None,
        "chat_max_context_length": None,
        **declaration,
    }
    return functions.EligibleTextWorkerProfile(**profile_fields)


def _queued_request(model_names: list[str], *, is_chat: bool = False, max_length: int = 200) -> functions.QueuedRequest:
    return functions.QueuedRequest(
        queued_things=max_length,
        n=1,
        model_names=frozenset(model_names),
        is_chat=is_chat,
        max_length=max_length,
        max_context_length=2048,
    )


CHAT_WORKER_DECLARATION: dict[str, Any] = {
    "chat_model_id": "shared-model",
    "chat_max_length": 1024,
    "chat_max_context_length": 8192,
}
"""The chat declaration of a worker that serves ``shared-model`` with chat."""


def test_a_queued_request_on_an_overlapping_model_competes() -> None:
    assert _queued_request(["shared-model", "other-model"]).can_be_taken_by(_text_worker_profile(["shared-model"]))


def test_a_queued_request_accepting_any_model_competes() -> None:
    assert _queued_request([]).can_be_taken_by(_text_worker_profile(["shared-model"]))


def test_a_queued_request_on_other_models_does_not_compete() -> None:
    assert not _queued_request(["busy-model"]).can_be_taken_by(_text_worker_profile(["shared-model"]))


def test_a_queued_chat_request_does_not_compete_for_a_worker_without_chat() -> None:
    """Sharing the model is not enough: a worker that does not take chat jobs cannot take a queued chat request."""
    assert not _queued_request(["shared-model"], is_chat=True).can_be_taken_by(_text_worker_profile(["shared-model"]))


def test_a_queued_chat_request_competes_for_a_worker_serving_its_model_with_chat() -> None:
    chat_worker = _text_worker_profile(["shared-model"], **CHAT_WORKER_DECLARATION)
    assert _queued_request(["shared-model"], is_chat=True).can_be_taken_by(chat_worker)


def test_a_queued_request_over_the_workers_max_length_does_not_compete() -> None:
    assert not _queued_request(["shared-model"], max_length=513).can_be_taken_by(_text_worker_profile(["shared-model"]))
    assert _queued_request(["shared-model"], max_length=512).can_be_taken_by(_text_worker_profile(["shared-model"]))


def test_a_queued_chat_request_is_bounded_by_the_chat_limits_rather_than_the_text_limits() -> None:
    chat_worker = _text_worker_profile(["shared-model"], max_length=80, **CHAT_WORKER_DECLARATION)
    assert _queued_request(["shared-model"], is_chat=True, max_length=1024).can_be_taken_by(chat_worker)
    assert not _queued_request(["shared-model"], is_chat=True, max_length=1025).can_be_taken_by(chat_worker)
    assert not _queued_request(["shared-model"], max_length=81).can_be_taken_by(chat_worker)


def test_a_queued_text_request_does_not_compete_for_a_chat_only_worker() -> None:
    chat_only_worker = _text_worker_profile(["shared-model"], accepts_prompt_jobs=False, **CHAT_WORKER_DECLARATION)
    assert not _queued_request(["shared-model"]).can_be_taken_by(chat_only_worker)


def test_a_queued_request_competes_when_any_eligible_worker_could_take_it() -> None:
    worker_profiles = [_text_worker_profile(["shared-model"]), _text_worker_profile(["shared-model"], **CHAT_WORKER_DECLARATION)]
    assert functions._queued_request_competes(_queued_request(["shared-model"], is_chat=True), worker_profiles)
    assert not functions._queued_request_competes(_queued_request(["shared-model"], is_chat=True), worker_profiles[:1])


def test_a_new_request_counts_the_work_of_every_queued_request_its_eligible_workers_could_take(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every competing queued request counts, with its waiting generations and work, since the new request is not queued yet."""
    user = make_user()
    for _ in range(2):
        _queue_text_request(user, ["shared-model"], max_length=200)
    multi_generation_request = _queue_text_request(user, ["shared-model", "other-model"], max_length=200)
    multi_generation_request.n = 3
    _queue_text_request(user, [], max_length=200)
    for _ in range(4):
        _queue_text_request(user, ["busy-model"], max_length=200)
    db.session.commit()
    _refresh_stored_queue(fake_redis, monkeypatch)

    competing_work = functions.measure_competing_queued_work("text", [_text_worker_profile(["shared-model"])])

    assert competing_work == functions.CompetingQueuedWork(queued_things=(2 + 3 + 1) * 200, queued_jobs=2 + 3 + 1)


def test_a_queued_chat_request_does_not_count_against_a_text_request_whose_eligible_workers_lack_chat(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The competing work is judged per eligible worker, so a chat request on the shared model is not counted."""
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    queued_chat_request = _make_chat_request(user)
    queued_chat_request.active = True
    _queue_text_request(user, [CHAT_MODEL], max_length=200)
    db.session.commit()
    _make_text_worker(user, CHAT_MODEL, speed=10)
    new_request = _make_text_request(user, CHAT_MODEL)
    new_request.slow_workers = True
    db.session.commit()
    _refresh_stored_queue(fake_redis, monkeypatch)

    availability = functions.get_worker_availability_for_request(new_request)
    competing_work = functions.measure_competing_queued_work("text", availability.worker_profiles)

    assert competing_work == functions.CompetingQueuedWork(queued_things=200, queued_jobs=1)


def test_a_new_request_has_no_competing_work_without_a_stored_queue(
    db_session: Any,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(functions._wp_queue_cache, "text", None)
    monkeypatch.setitem(functions._wp_queue_positions_time, "text", 0.0)

    assert functions.measure_competing_queued_work("text", [_text_worker_profile(["shared-model"])]) is None


def test_a_worker_without_performance_samples_counts_at_the_assumed_unmeasured_speed(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The baseline speed a new worker stores does not describe it, so its throughput is taken at the assumed speed."""
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    new_worker = _make_text_worker(user, "new-model", speed=1)
    new_worker.threads = 2
    _make_text_worker(user, "new-model", speed=7)
    request = _make_text_request(user, "new-model")
    request.slow_workers = True
    db.session.commit()

    availability = functions.get_worker_availability_for_request(request)

    assert availability.worker_count == 2
    assert availability.tokens_per_second == UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND * 2 + 7


def test_the_check_after_activation_reuses_the_availability_measured_at_creation(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feasible creation-time measurement answers the check and stays cached for the first status poll."""
    monkeypatch.setattr("horde.classes.kobold.worker.model_reference.get_text_model_multiplier", lambda _model_name: 20)
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, "measured-model")
    _make_text_worker(user, "measured-model", speed=10)
    creation_availability = functions.get_worker_availability_for_request(request)

    def fail_worker_query(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the check after activation repeated the eligible-worker query")

    monkeypatch.setattr(functions, "_iter_eligible_workers_for_request", fail_worker_query)

    assert functions.check_new_request_has_valid_workers(request, creation_availability=creation_availability) is True
    assert fake_redis.horde_r_get(f"wp_availability_{request.id}") is not None
    assert functions.get_worker_availability_for_request(request).worker_count == 1


def test_an_infeasible_creation_measurement_leaves_only_the_validity_flag_cached(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first status poll of a request no worker could take at creation measures its availability afresh."""
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    request = _make_text_request(user, "unserved-model")
    creation_availability = functions.get_worker_availability_for_request(request)

    def fail_worker_query(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the check after activation repeated the eligible-worker query")

    with monkeypatch.context() as query_guard:
        query_guard.setattr(functions, "_iter_eligible_workers_for_request", fail_worker_query)
        has_valid_workers = functions.check_new_request_has_valid_workers(request, creation_availability=creation_availability)

    assert has_valid_workers is False
    assert fake_redis.horde_r_get(f"wp_availability_{request.id}") is None
    assert int(fake_redis.horde_r_get(f"wp_validity_{request.id}")) == 0
