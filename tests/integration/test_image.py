# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later


import time

import pytest

from tests.integration._object_storage import (
    assert_presigned_image_download,
    make_test_webp,
    upload_to_presigned_url,
)

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
        # 8 first-order work units at 1024x1024 calculate below the 150-second floor. The
        # assignment contract is returned to the worker as an integer number of seconds.
        assert pop_results["ttl"] == 150, pop_results
    except AssertionError as err:
        _cancel_image_request(client, request_headers, req_id)
        print("Request cancelled")
        raise err

    generated_image = make_test_webp(size=(16, 10), color=(126, 44, 211))
    # Use the worker-facing presigned URL for a real transfer before submitting the R2 sentinel.
    upload_to_presigned_url(pop_results["r2_upload"], generated_image)

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
    # The requester-facing URL must retrieve exactly the bytes uploaded by the worker simulation.
    assert_presigned_image_download(gen["img"], generated_image)
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
        "sampler_execution_contract_version": "1.0",
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
        # 30 + (5 work * 2 seconds * 4x pixels), then the assigned Flux model and the
        # accepting extra-slow worker each contribute their 3x allowance.
        assert pop_results["ttl"] == 630, pop_results
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


@pytest.mark.parametrize("stale_last_fault", [False, True])
def test_an_image_request_whose_unrestarted_fault_leaves_nothing_to_do_reads_faulted(
    app,
    client,
    request_headers: dict[str, str],
    stale_last_fault: bool,
) -> None:
    """The second fault of a one-image request is not restarted, since its earlier fault counts toward the jobs the
    request asked for. With nothing waiting or processing and no image stored, the request faults in that commit
    rather than reading done with no image or staying open for good."""
    async_dict = {
        "prompt": "a lighthouse keeper counting faulted jobs by candlelight",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "trusted_workers": True,
        "params": {"width": 512, "height": 512, "steps": 8, "sampler_name": "k_euler_a"},
        "models": TEST_MODELS,
    }
    async_req = client.post("/api/v2/generate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]
    pop_dict = {
        "name": "CICD Fake Dreamer",
        "models": TEST_MODELS,
        "bridge_agent": "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
        "nsfw": True,
        "amount": 1,
        "max_pixels": 4194304,
        "allow_unsafe_ipaddr": True,
    }
    try:
        for attempt in range(2):
            pop_req = client.post("/api/v2/generate/pop", json=pop_dict, headers=request_headers)
            assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
            job_id = pop_req.get_json()["id"]
            assert job_id is not None, pop_req.get_json()
            if stale_last_fault and attempt == 1:
                import uuid

                from horde.classes.stable.processing_generation import ImageProcessingGeneration
                from horde.flask import db

                with app.app_context():
                    generation = db.session.get(ImageProcessingGeneration, uuid.UUID(job_id))
                    assert generation.restarts_after_fault() is False
                    generation.fault_and_restart(stale=True)
                    assert generation.faulted is True
                continue
            submit_req = client.post(
                "/api/v2/generate/submit",
                json={"id": job_id, "generation": "", "state": "faulted", "seed": 0},
                headers=request_headers,
            )
            assert submit_req.status_code < 400, submit_req.get_data(as_text=True)

        status = client.get(f"/api/v2/generate/status/{req_id}", headers=request_headers).get_json()
        if stale_last_fault:
            assert (status["done"], status["faulted"], status["waiting"]) == (False, False, 1)
        else:
            assert (status["done"], status["faulted"], status["generations"]) == (True, True, [])
    finally:
        _cancel_image_request(client, request_headers, req_id)
