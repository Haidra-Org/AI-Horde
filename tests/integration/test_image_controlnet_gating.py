# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dispatch gating for the unified image-generation control types.

The classic controlnet set is renderable by any controlnet-capable worker. The
extended set (everything outside ``LEGACY_IMAGE_CONTROL_TYPES``) may only be
dispatched to reGen bridge agents new enough to annotate it, otherwise the job
would be sent to a worker that cannot fulfil it. The legacy ``hough`` alias must
keep matching old workers unchanged.
"""

import base64
from io import BytesIO

import pytest
from PIL import Image

from horde.bridge_reference import CAPABILITY_EXPANDED_REGEN_VERSION

TEST_MODELS = ["stable_diffusion"]
SDXL_MODELS = ["AlbedoBase XL (SDXL)"]

# A pre-extended reGen agent: has classic controlnet, lacks extended controlnet.
OLD_BRIDGE_AGENT = "AI Horde Worker reGen:13:https://github.com/Haidra-Org/horde-worker-reGen"
# A reGen agent at the extended controlnet threshold.
NEW_BRIDGE_AGENT = f"AI Horde Worker reGen:{CAPABILITY_EXPANDED_REGEN_VERSION}:https://github.com/Haidra-Org/horde-worker-reGen"

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def _load_image_as_b64(image_path: str) -> str:
    final_src_img = Image.open(image_path)
    buffer = BytesIO()
    final_src_img.save(buffer, format="Webp", quality=50, exact=True)
    return base64.b64encode(buffer.getvalue()).decode("utf8")


def _controlnet_async_dict(control_type: str, models: list[str] | None = None) -> dict:
    return {
        "prompt": "a controlnet-guided horde of robots",
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
            "sampler_name": "k_euler_a",
            "control_type": control_type,
        },
        "models": models if models is not None else TEST_MODELS,
        "source_image": _load_image_as_b64("img_stable/0.jpg"),
        "source_processing": "img2img",
    }


def _pop_dict(bridge_agent: str, allow_extended_controlnet: bool = True) -> dict:
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
        "allow_controlnet": True,
        "allow_extended_controlnet": allow_extended_controlnet,
        "allow_sdxl_controlnet": True,
        "allow_lora": True,
    }


def test_new_control_type_skips_old_bridge_but_matches_new(client, request_headers: dict[str, str]) -> None:
    async_req = client.post("/api/v2/generate/async", json=_controlnet_async_dict("lineart"), headers=request_headers)
    # The unified control set is accepted by the async validator.
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        old_pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert old_pop.status_code < 400, old_pop.get_data(as_text=True)
        old_results = old_pop.get_json()
        # A pre-extended controlnet worker cannot annotate `lineart`, so it is skipped as too old.
        assert old_results["id"] is None, old_results
        assert old_results["skipped"].get("bridge_version", 0) >= 1, old_results

        new_pop = client.post("/api/v2/generate/pop", json=_pop_dict(NEW_BRIDGE_AGENT), headers=request_headers)
        assert new_pop.status_code < 400, new_pop.get_data(as_text=True)
        new_results = new_pop.get_json()
        # The extended-capable worker with the opt-in flag matches the same job.
        assert new_results["id"] is not None, new_results
        assert new_results["payload"]["control_type"] == "lineart", new_results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_new_control_type_skips_new_bridge_when_flag_off(client, request_headers: dict[str, str]) -> None:
    async_req = client.post("/api/v2/generate/async", json=_controlnet_async_dict("lineart"), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop = client.post(
            "/api/v2/generate/pop",
            json=_pop_dict(NEW_BRIDGE_AGENT, allow_extended_controlnet=False),
            headers=request_headers,
        )
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        # An extended-capable bridge that opts out of extended types must not receive `lineart`.
        assert results["id"] is None, results
        # The opt-out is a worker-choice skip, attributed to the controlnet bucket rather than bridge_version.
        assert results["skipped"].get("controlnet", 0) >= 1, results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_classic_control_type_matches_new_bridge_regardless_of_flag(client, request_headers: dict[str, str]) -> None:
    async_req = client.post("/api/v2/generate/async", json=_controlnet_async_dict("canny"), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        # The extended opt-out must not affect classic control types.
        pop = client.post(
            "/api/v2/generate/pop",
            json=_pop_dict(NEW_BRIDGE_AGENT, allow_extended_controlnet=False),
            headers=request_headers,
        )
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert results["payload"]["control_type"] == "canny", results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


@pytest.mark.parametrize("control_type", ["canny", "teed"])
def test_sdxl_baseline_rejects_controlnet(client, request_headers: dict[str, str], control_type: str) -> None:
    async_req = client.post(
        "/api/v2/generate/async",
        json=_controlnet_async_dict(control_type, models=SDXL_MODELS),
        headers=request_headers,
    )
    # An SDXL-baseline model rejects any control_type at validation, classic or extended alike.
    assert async_req.status_code == 400, async_req.get_data(as_text=True)
    assert async_req.get_json().get("rc") == "ControlNetMismatch", async_req.get_data(as_text=True)


def test_legacy_hough_alias_still_matches_old_bridge(client, request_headers: dict[str, str]) -> None:
    async_req = client.post("/api/v2/generate/async", json=_controlnet_async_dict("hough"), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        old_pop = client.post("/api/v2/generate/pop", json=_pop_dict(OLD_BRIDGE_AGENT), headers=request_headers)
        assert old_pop.status_code < 400, old_pop.get_data(as_text=True)
        old_results = old_pop.get_json()
        # `hough` is a classic control type, so an old controlnet worker still fulfils it.
        assert old_results["id"] is not None, old_results
        assert old_results["payload"]["control_type"] == "hough", old_results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
