# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
from io import BytesIO

import pytest
from PIL import Image


def load_image_as_b64(image_path):
    final_src_img = Image.open(image_path)
    buffer = BytesIO()
    final_src_img.save(buffer, format="Webp", quality=50, exact=True)
    return base64.b64encode(buffer.getvalue()).decode("utf8")


TEST_MODELS = ["Stable Cascade 1.0"]

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def test_simple_image_gen(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "prompt": "A remix",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 1024,
            "height": 1024,
            "steps": 20,
            "cfg_scale": 4,
            "sampler_name": "k_euler_a",
        },
        "models": TEST_MODELS,
        "source_image": load_image_as_b64("img_stable/0.jpg"),
        "source_processing": "remix",
        "extra_source_images": [
            {
                "image": load_image_as_b64("img_stable/0.jpg"),
                "strength": 0.5,
            },
            {
                "image": load_image_as_b64("img_stable/0.jpg"),
            },
        ],
    }

    async_req = client.post("/api/v2/generate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Dreamer",
        "models": TEST_MODELS,
        "bridge_agent": "AI Horde Worker reGen:8.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
        "nsfw": True,
        "amount": 10,
        "max_pixels": 4194304,
        "allow_img2img": True,
        "allow_painting": True,
        "allow_unsafe_ipaddr": True,
        "allow_post_processing": True,
        "allow_controlnet": True,
        "allow_sdxl_controlnet": True,
        "allow_lora": True,
    }
    pop_req = client.post("/api/v2/generate/pop", json=pop_dict, headers=request_headers)
    try:
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    except AssertionError as err:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
        print("Request cancelled")
        raise err

    pop_results = pop_req.get_json()

    job_id = pop_results["id"]
    try:
        assert job_id is not None, pop_results
    except AssertionError as err:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
        print("Request cancelled")
        raise err

    submit_dict = {
        "id": job_id,
        "generation": "R2",
        "state": "ok",
        "seed": 0,
    }
    submit_req = client.post("/api/v2/generate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/generate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["generations"]) == 1
    gen = retrieve_results["generations"][0]
    assert len(gen["gen_metadata"]) == 0
    assert gen["seed"] == "0"
    assert gen["worker_name"] == "CICD Fake Dreamer"
    assert gen["model"] in TEST_MODELS
    assert gen["state"] == "ok"
    assert retrieve_results["kudos"] > 1
    assert retrieve_results["done"] is True
