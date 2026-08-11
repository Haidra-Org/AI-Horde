# SPDX-FileCopyrightText: 2026 Abhinav Gorrepati <gorrepatiabhinav1@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Worker-facing image generation pop payload coverage."""

from types import SimpleNamespace

import pytest
from flask_restx import marshal

from horde.apis.v2.stable import models
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
