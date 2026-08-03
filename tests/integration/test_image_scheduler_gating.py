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

from horde.bridge_reference import SCHEDULER_FIELD_REGEN_VERSION

TEST_MODELS = ["stable_diffusion"]

OLD_BRIDGE_AGENT = "AI Horde Worker reGen:13:https://github.com/Haidra-Org/horde-worker-reGen"
NEW_BRIDGE_AGENT = f"AI Horde Worker reGen:{SCHEDULER_FIELD_REGEN_VERSION}:https://github.com/Haidra-Org/horde-worker-reGen"

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


@pytest.mark.parametrize("schedule", ["sgm_uniform", "beta", "kl_optimal", "linear_quadratic"])
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
        assert new_results["id"] is not None, new_results
        assert new_results["payload"]["scheduler"] == schedule, new_results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


@pytest.mark.parametrize(("params", "expected"), [({"karras": True}, "karras"), ({"karras": False}, "normal")])
def test_karras_flag_still_reaches_old_bridges_with_its_original_meaning(
    client,
    request_headers: dict[str, str],
    params: dict,
    expected: str,
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
        assert results["payload"]["scheduler"] == expected, results
        # The flag travels alongside the resolved schedule so an old bridge renders from it unchanged.
        assert results["payload"]["karras"] is params["karras"], results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_explicit_legacy_schedule_is_not_gated(client, request_headers: dict[str, str]) -> None:
    # `karras` names this schedule, so asking for it by name must not narrow the worker population.
    async_req = client.post("/api/v2/generate/async", json=_async_dict({"scheduler": "karras"}), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert results["payload"]["scheduler"] == "karras", results
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


def test_unknown_schedule_is_rejected_at_request_time(client, request_headers: dict[str, str]) -> None:
    async_req = client.post(
        "/api/v2/generate/async",
        json=_async_dict({"scheduler": "not_a_schedule"}),
        headers=request_headers,
    )
    assert async_req.status_code >= 400, async_req.get_data(as_text=True)
