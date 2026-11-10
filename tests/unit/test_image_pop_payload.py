# SPDX-FileCopyrightText: 2026 Abhinav Gorrepati <gorrepatiabhinav1@gmail.com>
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Worker-facing image generation pop payload coverage."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from flask_restx import marshal

from horde.apis.v2 import base
from horde.apis.v2.stable import models
from horde.classes.base.worker import EligibilityVerdict
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt

pytestmark = pytest.mark.unit


def _make_pop_payload(monkeypatch: pytest.MonkeyPatch, *, shared: bool) -> dict[str, object]:
    monkeypatch.setattr(
        "horde.classes.stable.waiting_prompt.generate_procgen_upload_url",
        lambda procgen_id, is_shared: f"https://uploads.example/{procgen_id}?shared={is_shared}",
    )
    worker = SimpleNamespace(bridge_agent="AI Horde Worker:24.0.0")
    procgen = SimpleNamespace(id="generation-id", model="stable_diffusion", job_ttl=150, worker=worker)
    wp = SimpleNamespace(shared=shared, source_image=None, extra_source_images=None)

    return ImageWaitingPrompt.get_pop_payload(wp, [procgen], {"prompt": "a unit-test prompt"})


@pytest.mark.parametrize("shared", [False, True])
def test_image_pop_payload_reports_sharing_intent(
    monkeypatch: pytest.MonkeyPatch,
    shared: bool,
) -> None:
    payload = _make_pop_payload(monkeypatch, shared=shared)

    assert payload["shared"] is shared


def test_image_pop_response_model_preserves_shared_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _make_pop_payload(monkeypatch, shared=True)

    assert marshal(payload, models.response_model_job_pop)["shared"] is True


@pytest.mark.parametrize("next_candidate_assignable", [False, True])
def test_pop_continues_after_an_assignment_returns_none(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    next_candidate_assignable: bool,
) -> None:
    """An assignment refusal permits later candidates or the normal empty response."""
    candidates = [
        SimpleNamespace(id=name, needs_gen=lambda: True, get_eligibility_inputs=lambda: SimpleNamespace())
        for name in ("first", "second")
    ]
    pop = base.JobPopTemplate()
    pop.args = SimpleNamespace(priority_usernames=[], models=[])
    pop.user = make_user()
    pop.worker = SimpleNamespace(
        id="worker",
        maintenance=False,
        read_eligibility_inputs=Mock(return_value=SimpleNamespace()),
        can_generate_any=Mock(return_value=EligibilityVerdict.accepted()),
        can_generate_with_eligibility_inputs=Mock(return_value=EligibilityVerdict.accepted()),
    )
    pop.skipped = {}
    monkeypatch.setattr(pop, "validate", lambda: None)
    monkeypatch.setattr(pop, "check_in", lambda: None)
    monkeypatch.setattr(pop, "get_sorted_wp", Mock(side_effect=[candidates, [], []]))
    monkeypatch.setattr(pop, "claim_wp", lambda wp: wp)
    assigned = {"id": "second-job", "ids": ["second-job"]}
    start_worker = Mock(side_effect=[None, assigned if next_candidate_assignable else None])
    monkeypatch.setattr(pop, "start_worker", start_worker)
    messages = [{"message": "worker notice"}]
    read_messages = Mock(return_value=messages)
    monkeypatch.setattr(base.database, "get_all_active_worker_messages", read_messages)

    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, status = pop._post_inner()

    assert status == 200
    assert response["ids"] == (["second-job"] if next_candidate_assignable else [])
    assert response["messages"] == messages
    assert [call.args[0].id for call in start_worker.call_args_list] == ["first", "second"]
    read_messages.assert_called_once_with("worker")
