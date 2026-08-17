# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dispatch gating for the `scheduler` field and its back-compatibility with the karras flag.

A bridge older than the field ignores it and samples on whatever the karras flag says, so dispatching
an extended schedule to one returns a different image than was asked for rather than an error. The
legacy schedules stay ungated because the flag can express them, which is what keeps every existing
request reaching the same population of workers it always did.
"""

import pytest
from horde_sdk.ai_horde_api.apimodels import ImageGenerateJobPopResponse

from horde.bridge_reference import CAPABILITY_EXPANDED_REGEN_VERSION

TEST_MODELS = ["stable_diffusion"]
FLOW_MODELS = ["Flux.1-Schnell fp8 (Compact)"]

OLD_BRIDGE_AGENT = "AI Horde Worker reGen:13:https://github.com/Haidra-Org/horde-worker-reGen"
NEW_BRIDGE_AGENT = f"AI Horde Worker reGen:{CAPABILITY_EXPANDED_REGEN_VERSION}:https://github.com/Haidra-Org/horde-worker-reGen"

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def _async_dict(params: dict) -> dict:
    return {
        "prompt": "a horde of robots sampling on a schedule",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 512,
            "height": 512,
            "steps": 20,
            "cfg_scale": 7.5,
            "sampler_name": "k_dpmpp_2m",
            **params,
        },
        "models": TEST_MODELS,
    }


def _pop_dict(bridge_agent: str) -> dict:
    return {
        "name": "CICD Fake Dreamer",
        "models": TEST_MODELS,
        "bridge_agent": bridge_agent,
        "nsfw": True,
        "amount": 10,
        "max_pixels": 4194304,
        "allow_img2img": True,
        "allow_painting": True,
        "allow_unsafe_ipaddr": True,
        "allow_post_processing": True,
        "allow_lora": True,
    }


@pytest.mark.parametrize(
    "schedule",
    ["sgm_uniform", "beta", "kl_optimal", "linear_quadratic", "align_your_steps", "gits"],
)
def test_extended_schedule_skips_old_bridge_but_matches_new(
    client,
    request_headers: dict[str, str],
    schedule: str,
) -> None:
    async_req = client.post("/api/v2/generate/async", json=_async_dict({"scheduler": schedule}), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        old_pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert old_pop.status_code < 400, old_pop.get_data(as_text=True)
        old_results = old_pop.get_json()
        assert old_results["id"] is None, old_results
        assert old_results["skipped"].get("bridge_version", 0) >= 1, old_results

        new_pop = client.post("/api/v2/generate/pop", json=_pop_dict(NEW_BRIDGE_AGENT), headers=request_headers)
        assert new_pop.status_code < 400, new_pop.get_data(as_text=True)
        new_results = new_pop.get_json()
        sdk_response = ImageGenerateJobPopResponse.model_validate(new_results)
        assert new_results["id"] is not None, new_results
        assert new_results["payload"]["scheduler"] == schedule, new_results
        assert sdk_response.payload.scheduler == schedule
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


@pytest.mark.parametrize("params", [{"karras": True}, {"karras": False}])
def test_karras_flag_still_reaches_old_bridges_with_its_original_meaning(
    client,
    request_headers: dict[str, str],
    params: dict,
) -> None:
    # The compatibility guarantee: a request that names no schedule keeps the schedule it always got and
    # keeps reaching bridges that predate the field.
    async_req = client.post("/api/v2/generate/async", json=_async_dict(params), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert "scheduler" not in results["payload"], results
        # The flag travels alongside the resolved schedule so an old bridge renders from it unchanged.
        assert results["payload"]["karras"] is params["karras"], results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


@pytest.mark.parametrize(
    ("schedule", "contradictory_karras"),
    [("karras", False), ("normal", True)],
)
def test_explicit_legacy_schedule_synchronizes_flag_for_old_bridges(
    client,
    request_headers: dict[str, str],
    schedule: str,
    contradictory_karras: bool,
) -> None:
    # A legacy schedule remains available to old bridges even when the caller also supplied the
    # opposite flag: scheduler takes precedence, and the persisted flag is made safe for old clients.
    async_req = client.post(
        "/api/v2/generate/async",
        json=_async_dict({"scheduler": schedule, "karras": contradictory_karras}),
        headers=request_headers,
    )
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert "scheduler" not in results["payload"], results
        assert results["payload"]["karras"] is (schedule == "karras"), results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_field_overrides_the_flag_on_dispatch(client, request_headers: dict[str, str]) -> None:
    async_req = client.post(
        "/api/v2/generate/async",
        json=_async_dict({"scheduler": "beta", "karras": True}),
        headers=request_headers,
    )
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post("/api/v2/generate/pop", json=_pop_dict(NEW_BRIDGE_AGENT), headers=request_headers)
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert results["payload"]["scheduler"] == "beta", results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


@pytest.mark.parametrize(
    ("sampler_name", "field", "value"),
    [
        ("k_euler_a", "sampler_eta", 0.5),
        ("k_euler", "sampler_s_noise", 1.0),
        ("k_euler", "sampler_s_churn", 0.1),
        ("k_euler", "sampler_s_tmin", 0.0),
        ("k_euler", "sampler_s_tmax", 1.0),
        ("k_lms", "sampler_order", 3),
        ("dpmpp_2m_sde", "sampler_solver_type", "heun"),
    ],
)
def test_solver_option_skips_old_bridge_but_matches_new(
    client,
    request_headers: dict[str, str],
    sampler_name: str,
    field: str,
    value: float | int | str,
) -> None:
    async_req = client.post(
        "/api/v2/generate/async",
        json=_async_dict({"sampler_name": sampler_name, field: value}),
        headers=request_headers,
    )
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        old_pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert old_pop.status_code < 400, old_pop.get_data(as_text=True)
        old_results = old_pop.get_json()
        assert old_results["id"] is None, old_results
        assert old_results["skipped"].get("bridge_version", 0) >= 1, old_results

        new_pop = client.post("/api/v2/generate/pop", json=_pop_dict(NEW_BRIDGE_AGENT), headers=request_headers)
        assert new_pop.status_code < 400, new_pop.get_data(as_text=True)
        new_results = new_pop.get_json()
        sdk_response = ImageGenerateJobPopResponse.model_validate(new_results)
        assert new_results["id"] is not None, new_results
        assert new_results["payload"][field] == value, new_results
        assert getattr(sdk_response.payload, field) == value
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_flow_shift_skips_old_bridge_but_matches_new(client, request_headers: dict[str, str], settle_kudos) -> None:
    settle_kudos()
    request_body = _async_dict({"flow_shift": 1.1})
    request_body["models"] = FLOW_MODELS
    async_req = client.post("/api/v2/generate/async", json=request_body, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        old_pop_body = _pop_dict(OLD_BRIDGE_AGENT)
        old_pop_body["models"] = FLOW_MODELS
        old_pop = client.post("/api/v2/generate/pop", json=old_pop_body, headers=request_headers)
        assert old_pop.status_code < 400, old_pop.get_data(as_text=True)
        old_results = old_pop.get_json()
        assert old_results["id"] is None, old_results
        assert old_results["skipped"].get("bridge_version", 0) >= 1, old_results

        new_pop_body = _pop_dict(NEW_BRIDGE_AGENT)
        new_pop_body["models"] = FLOW_MODELS
        new_pop = client.post("/api/v2/generate/pop", json=new_pop_body, headers=request_headers)
        assert new_pop.status_code < 400, new_pop.get_data(as_text=True)
        new_results = new_pop.get_json()
        sdk_response = ImageGenerateJobPopResponse.model_validate(new_results)
        assert new_results["id"] is not None, new_results
        assert new_results["payload"]["flow_shift"] == 1.1, new_results
        assert sdk_response.payload.flow_shift == 1.1
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_unknown_schedule_is_rejected_at_request_time(client, request_headers: dict[str, str]) -> None:
    async_req = client.post(
        "/api/v2/generate/async",
        json=_async_dict({"scheduler": "not_a_schedule"}),
        headers=request_headers,
    )
    assert async_req.status_code >= 400, async_req.get_data(as_text=True)
