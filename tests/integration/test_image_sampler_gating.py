# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dispatch gating for the extended image-generation samplers.

An extended sampler may only be dispatched to bridge agents whose backend maps
it. This gate matters more than it looks: a bridge handed a sampler its backend
does not know does not fail the job, it renders the default sampler instead, so
an ungated dispatch silently returns the wrong image rather than erroring.

Unlike extended controlnet there is no per-worker opt-in. A sampler needs no
weights, no downloads and no annotator readiness, so bridge version alone
decides, and it decides fail-closed for every bridge below the threshold.
"""

import pytest

from horde.bridge_reference import EXTENDED_SAMPLERS_REGEN_VERSION

TEST_MODELS = ["stable_diffusion"]

# A pre-extended reGen agent: renders the classic samplers only.
OLD_BRIDGE_AGENT = "AI Horde Worker reGen:13:https://github.com/Haidra-Org/horde-worker-reGen"
# A reGen agent at the extended sampler threshold.
NEW_BRIDGE_AGENT = f"AI Horde Worker reGen:{EXTENDED_SAMPLERS_REGEN_VERSION}:https://github.com/Haidra-Org/horde-worker-reGen"

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def _async_dict(sampler_name: str, karras: bool = False) -> dict:
    return {
        "prompt": "a horde of robots sampling latent space",
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
            "sampler_name": sampler_name,
            "karras": karras,
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


@pytest.mark.parametrize("sampler_name", ["uni_pc", "deis", "heunpp2"])
def test_extended_sampler_skips_old_bridge_but_matches_new(
    client,
    request_headers: dict[str, str],
    sampler_name: str,
) -> None:
    async_req = client.post("/api/v2/generate/async", json=_async_dict(sampler_name), headers=request_headers)
    # The extended samplers are accepted by the async validator.
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
        assert new_results["payload"]["sampler_name"] == sampler_name, new_results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_extended_sampler_matches_new_bridge_under_karras(client, request_headers: dict[str, str]) -> None:
    # The backend chooses the sigma schedule independently of the solver, so a karras request must
    # not narrow the extended set away.
    async_req = client.post("/api/v2/generate/async", json=_async_dict("uni_pc", karras=True), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post("/api/v2/generate/pop", json=_pop_dict(NEW_BRIDGE_AGENT), headers=request_headers)
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert results["payload"]["sampler_name"] == "uni_pc", results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_classic_sampler_still_matches_old_bridge(client, request_headers: dict[str, str]) -> None:
    # The gate must not strand anything old bridges already rendered.
    async_req = client.post("/api/v2/generate/async", json=_async_dict("k_euler_a"), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert results["payload"]["sampler_name"] == "k_euler_a", results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_unknown_sampler_is_rejected_at_request_time(client, request_headers: dict[str, str]) -> None:
    # The accepted set is closed: a name outside it must not reach the queue at all.
    async_req = client.post("/api/v2/generate/async", json=_async_dict("not_a_real_sampler"), headers=request_headers)
    assert async_req.status_code >= 400, async_req.get_data(as_text=True)
