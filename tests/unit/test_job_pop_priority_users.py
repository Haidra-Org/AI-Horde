# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify worker-pop setup: priority-user selection and the worker-side eligibility inputs read once per pop."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde import exceptions
from horde.apis.v2 import base
from horde.classes.base.user import User, UserRoleTypes
from horde.classes.base.worker import EligibilitySkipReason, EligibilityVerdict, WorkerModel
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker, TextWorkerSoftprompts
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.flask import db
from tests.unit.model_reference_seed import seed_image_reference

pytestmark = pytest.mark.unit

_IMAGE_MODEL = "stable_diffusion"
_TEXT_MODEL = "pop-snapshot-text-model"


def _stub_worker(worker_only_verdict: EligibilityVerdict | None = None) -> SimpleNamespace:
    """Return a worker stand-in that passes the worker-only rules unless ``worker_only_verdict`` says otherwise."""
    return SimpleNamespace(
        id="priority-test-worker",
        maintenance=False,
        read_eligibility_inputs=Mock(return_value=SimpleNamespace()),
        can_generate_any=Mock(return_value=worker_only_verdict or EligibilityVerdict.accepted()),
    )


def _prepare_pop(
    owner: User,
    priority_usernames: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker: Any = None,
    candidates: Sequence[Any] = (),
) -> tuple[Any, Mock, Mock]:
    """Build a pop request for the owner with its database and scheduling collaborators observed.

    Args:
        owner: The worker's owner, who sends the pop.
        priority_usernames: The priority usernames the pop sends.
        monkeypatch: The fixture that restores the patched collaborators.
        worker: The popping worker; a stand-in that passes the worker-only rules when None.
        candidates: The requests on the first page of the normal queue scan.

    Returns:
        The pop resource, a mock counting priority-user lookups, and the mock standing in for the sorted request scan.
    """
    original_query = db.session.query
    priority_lookup = Mock()

    def track_query(*entities: Any, **kwargs: Any) -> Any:
        if len(entities) == 1 and entities[0] is User.id:
            priority_lookup()
        return original_query(*entities, **kwargs)

    monkeypatch.setattr(db.session, "query", track_query)
    monkeypatch.setattr(base.database, "get_all_active_worker_messages", lambda worker_id: [])
    pop = base.JobPopTemplate()
    pop.args = SimpleNamespace(priority_usernames=priority_usernames, models=[])
    pop.user = owner
    pop.worker = worker if worker is not None else _stub_worker()
    pop.skipped = {}
    monkeypatch.setattr(pop, "validate", lambda: None)
    monkeypatch.setattr(pop, "check_in", lambda: None)

    def sorted_prompts(priority_user_ids: list[int] | None = None) -> list[Any]:
        is_first_normal_page = priority_user_ids is None and pop.wp_page == 0
        return list(candidates) if is_first_normal_page else []

    get_sorted_prompts = Mock(side_effect=sorted_prompts)
    monkeypatch.setattr(pop, "get_sorted_wp", get_sorted_prompts)
    return pop, priority_lookup, get_sorted_prompts


@pytest.mark.parametrize("has_priority_usernames", [False, True], ids=["empty", "nonempty"])
def test_pop_queries_priority_users_only_when_requested(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    has_priority_usernames: bool,
) -> None:
    """Retain owner priority and select only existing, explicitly requested users."""
    owner = make_user()
    requested_user = make_user()
    unrequested_user = make_user()
    owner_id, requested_user_id = owner.id, requested_user.id
    missing_user_id = max(owner_id, requested_user_id, unrequested_user.id) + 1000
    priority_usernames = [requested_user.get_unique_alias(), f"missing#{missing_user_id}"] if has_priority_usernames else []
    pop, priority_lookup, get_sorted_prompts = _prepare_pop(owner, priority_usernames, monkeypatch)

    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, status = pop._post_inner()

    expected_user_ids = [owner_id, requested_user_id] if has_priority_usernames else [owner_id]
    assert priority_lookup.call_count == int(has_priority_usernames)
    assert pop.priority_user_ids == expected_user_ids
    assert get_sorted_prompts.call_args_list[0].args == (expected_user_ids,)
    assert response["ids"] == []
    assert status == 200


@pytest.mark.parametrize("malformed_username", ["db0#abc", "db0#", "db0"], ids=["letters", "empty_suffix", "no_suffix"])
def test_pop_rejects_malformed_priority_usernames(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    malformed_username: str,
) -> None:
    """Refuse a priority username without a numeric user id before any lookup or scan."""
    owner = make_user()
    pop, priority_lookup, get_sorted_prompts = _prepare_pop(owner, [owner.get_unique_alias(), malformed_username], monkeypatch)

    with app.test_request_context("/api/v2/generate/pop", method="POST"), pytest.raises(exceptions.BadRequest) as refusal:
        pop._post_inner()

    assert refusal.value.rc == "InvalidPriorityUsername"
    priority_lookup.assert_not_called()
    get_sorted_prompts.assert_not_called()


def test_pop_gives_its_priority_users_to_the_maintenance_rule(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker-side inputs are read once per pop, with the pop's priority users as the users served in maintenance."""
    owner = make_user()
    requested_user = make_user()
    pop, _, _ = _prepare_pop(owner, [requested_user.get_unique_alias()], monkeypatch)

    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        pop._post_inner()

    pop.worker.read_eligibility_inputs.assert_called_once_with(maintenance_user_ids=[owner.id, requested_user.id])
    pop.worker.can_generate_any.assert_called_once_with(pop.worker.read_eligibility_inputs.return_value)


def test_a_worker_refused_by_a_worker_only_rule_reads_no_candidate_page(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that every request would refuse gets the no-job response, with the reason counted once."""
    owner = make_user()
    worker = _stub_worker(EligibilityVerdict.refused(EligibilitySkipReason.BRIDGE_VERSION))
    pop, _, get_sorted_prompts = _prepare_pop(owner, [], monkeypatch, worker=worker)

    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, status = pop._post_inner()

    get_sorted_prompts.assert_not_called()
    assert status == 200
    assert response["ids"] == []
    assert response["skipped"] == {"bridge_version": 1}


def _make_trusted_owner(make_user: Any, make_user_role: Any) -> User:
    owner = make_user()
    make_user_role(owner, UserRoleTypes.TRUSTED, value=True)
    return owner


def _make_image_worker(owner: User) -> ImageWorker:
    worker = ImageWorker(
        user_id=owner.id,
        name=f"pop-snapshot-{uuid.uuid4().hex[:12]}",
        max_pixels=512 * 512,
        last_check_in=datetime.utcnow(),
        speed=1_000_000,
        nsfw=True,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=_IMAGE_MODEL))
    db.session.commit()
    return worker


def _make_oversized_image_requests(owner: User, count: int) -> list[ImageWaitingPrompt]:
    """Create image requests larger than the worker's pixel limit, so the image rules refuse each of them."""
    requests = [
        ImageWaitingPrompt(
            worker_ids=[],
            models=[_IMAGE_MODEL],
            prompt="pop snapshot test",
            user_id=owner.id,
            params={"n": 1, "width": 1024, "height": 1024, "steps": 10, "sampler_name": "k_euler_a"},
        )
        for _ in range(count)
    ]
    db.session.commit()
    return requests


def _make_text_worker(owner: User) -> TextWorker:
    worker = TextWorker(
        user_id=owner.id,
        name=f"pop-snapshot-{uuid.uuid4().hex[:12]}",
        max_length=16,
        max_context_length=4096,
        last_check_in=datetime.utcnow(),
        speed=10,
        nsfw=True,
    )
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=_TEXT_MODEL))
    db.session.add(TextWorkerSoftprompts(worker_id=worker.id, softprompt="held_softprompt"))
    db.session.commit()
    return worker


def _make_overlong_text_requests(owner: User, count: int) -> list[TextWaitingPrompt]:
    """Create text requests longer than the worker's length limit, so the text rules refuse each of them."""
    requests = [
        TextWaitingPrompt(
            worker_ids=[],
            models=[_TEXT_MODEL],
            prompt="pop snapshot test",
            user_id=owner.id,
            params={"n": 1, "max_length": 80, "max_context_length": 2048},
        )
        for _ in range(count)
    ]
    db.session.commit()
    return requests


def _record_worker_cache_reads(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return a list that receives the key of every later read of a worker's model or softprompt cache."""
    cache_reads: list[str] = []
    original_get = fake_redis.horde_r_get

    def recording_get(key: str) -> Any:
        if key.endswith(("_model_cache", "_softprompts_cache")):
            cache_reads.append(key)
        return original_get(key)

    monkeypatch.setattr(fake_redis, "horde_r_get", recording_get)
    return cache_reads


def _pop_with_candidates(
    app: Any,
    owner: User,
    worker: Any,
    candidates: Sequence[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    pop, _, _ = _prepare_pop(owner, [], monkeypatch, worker=worker, candidates=candidates)
    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, _ = pop._post_inner()
    return response


@pytest.mark.parametrize("candidate_count", [1, 5])
def test_an_image_pop_reads_the_model_cache_once_whatever_the_candidate_count(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
    candidate_count: int,
) -> None:
    seed_image_reference(monkeypatch, {_IMAGE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})
    owner = _make_trusted_owner(make_user, make_user_role)
    worker = _make_image_worker(owner)
    candidates = _make_oversized_image_requests(owner, candidate_count)
    worker.get_model_names()
    cache_reads = _record_worker_cache_reads(fake_redis, monkeypatch)

    response = _pop_with_candidates(app, owner, worker, candidates, monkeypatch)

    assert response["skipped"] == {"max_pixels": candidate_count}
    assert cache_reads == [f"worker_{worker.id}_model_cache"]


@pytest.mark.parametrize("candidate_count", [1, 5])
def test_a_text_pop_reads_the_model_and_softprompt_caches_once_whatever_the_candidate_count(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
    candidate_count: int,
) -> None:
    owner = _make_trusted_owner(make_user, make_user_role)
    worker = _make_text_worker(owner)
    candidates = _make_overlong_text_requests(owner, candidate_count)
    worker.get_model_names()
    worker.get_softprompt_names()
    cache_reads = _record_worker_cache_reads(fake_redis, monkeypatch)

    response = _pop_with_candidates(app, owner, worker, candidates, monkeypatch)

    assert response["skipped"] == {"max_length": candidate_count}
    assert sorted(cache_reads) == sorted([f"worker_{worker.id}_model_cache", f"worker_{worker.id}_softprompts_cache"])


def test_an_image_worker_holding_a_model_its_bridge_cannot_run_reads_no_candidate_page(
    app: Any,
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
    make_user_role: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker holding a flux model on a bridge release without flux is refused every request, so it is not paged."""
    flux_model = "pop-snapshot-flux-model"
    seed_image_reference(
        monkeypatch,
        {
            _IMAGE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1,
            flux_model: KNOWN_IMAGE_GENERATION_BASELINE.flux_1,
        },
    )
    owner = _make_trusted_owner(make_user, make_user_role)
    worker = _make_image_worker(owner)
    db.session.add(WorkerModel(worker_id=worker.id, model=flux_model))
    db.session.commit()
    worker.refresh_model_cache()
    monkeypatch.setattr("horde.classes.stable.worker.get_bridge_capabilities", lambda _bridge_agent: frozenset({"img2img"}))
    candidates = _make_oversized_image_requests(owner, 3)
    pop, _, get_sorted_prompts = _prepare_pop(owner, [], monkeypatch, worker=worker, candidates=candidates)

    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, status = pop._post_inner()

    get_sorted_prompts.assert_not_called()
    assert status == 200
    assert response["skipped"] == {"bridge_version": 1}
