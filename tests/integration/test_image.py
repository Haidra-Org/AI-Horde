# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later


import time

import pytest

TEST_MODELS = ["Fustercluck", "AlbedoBase XL (SDXL)"]

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def _cancel_image_request(client, request_headers: dict[str, str], req_id: str) -> None:
    client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_simple_image_gen(client, request_headers: dict[str, str]) -> None:
    print("test_simple_image_gen")
    async_dict = {
        "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 1024,
            "height": 1024,
            "steps": 8,
            "cfg_scale": 1.5,
            "sampler_name": "k_euler_a",
        },
        "sampler_name": "k_euler_a",
        "models": TEST_MODELS,
        "loras": [{"name": "247778", "is_version": True}],
    }
    async_req = client.post("/api/v2/generate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Dreamer",
        "models": TEST_MODELS,
        "bridge_agent": "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
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
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    pop_results = pop_req.get_json()

    job_id = pop_results["id"]
    try:
        assert job_id is not None, pop_results
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
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
    _cancel_image_request(client, request_headers, req_id)


TEST_MODELS_FLUX = ["Flux.1-Schnell fp8 (Compact)"]


def test_flux_image_gen(client, request_headers: dict[str, str]) -> None:
    print("test_flux_image_gen")
    async_dict = {
        "prompt": "a horde of cute flux robots in a sprawling server room repairing a massive mainframe",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 1024,
            "height": 1024,
            "steps": 8,
            "cfg_scale": 1,
            "sampler_name": "k_euler",
        },
        "models": TEST_MODELS_FLUX,
        # "extra_slow_workers": True,
    }

    time.sleep(1)
    async_req = client.post("/api/v2/generate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Dreamer",
        "models": TEST_MODELS_FLUX,
        "bridge_agent": "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
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
        "extra_slow_worker": False,
        "limit_max_steps": True,
    }

    # Test limit_max_steps
    pop_req = client.post("/api/v2/generate/pop", json=pop_dict, headers=request_headers)
    try:
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    pop_results = pop_req.get_json()
    try:
        assert pop_results["id"] is None, pop_results
        assert pop_results["skipped"].get("step_count") == 1, pop_results
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    # Test extra_slow_worker
    async_dict["params"]["steps"] = 5
    pop_dict["extra_slow_worker"] = True
    time.sleep(0.5)
    pop_req = client.post("/api/v2/generate/pop", json=pop_dict, headers=request_headers)
    try:
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    pop_results = pop_req.get_json()
    try:
        assert pop_results["id"] is None, pop_results
        assert pop_results["skipped"]["performance"] == 1, pop_results
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    _cancel_image_request(client, request_headers, req_id)

    # Try popping as an extra slow worker
    async_dict["extra_slow_workers"] = True
    time.sleep(0.5)
    async_req = client.post("/api/v2/generate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]
    time.sleep(0.5)
    pop_req = client.post("/api/v2/generate/pop", json=pop_dict, headers=request_headers)
    try:
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    pop_results = pop_req.get_json()
    job_id = pop_results["id"]
    try:
        assert job_id is not None, pop_results
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
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
    assert gen["model"] in TEST_MODELS_FLUX
    assert gen["state"] == "ok"
    assert retrieve_results["kudos"] > 1
    assert retrieve_results["done"] is True
    _cancel_image_request(client, request_headers, req_id)
