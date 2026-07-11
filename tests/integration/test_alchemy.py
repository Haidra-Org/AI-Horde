# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

# The annotation form is image-output: its pop mints an R2 upload URL, so the object store must be
# provisioned for this module rather than depending on a co-running marked module to bring it up.
pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def test_simple_alchemy(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "caption"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["caption", "strip_background", "interrogation"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
    }
    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    job_id = pop_results["forms"][0]["id"]
    assert job_id is not None, pop_results

    submit_dict = {
        "id": job_id,
        "result": {"caption": "Test"},
        "state": "ok",
    }
    submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 1
    gen = retrieve_results["forms"][0]
    assert "result" in gen
    assert isinstance(gen["result"], dict)
    assert "caption" in gen["result"]
    assert gen["form"] == "caption"
    assert gen["result"]["caption"] == "Test"
    assert gen["state"] == "done"
    assert retrieve_results["state"] == "done"


def test_alchemy_vectorize(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "vectorize"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["vectorize"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
    }
    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    job_id = pop_results["forms"][0]["id"]
    assert job_id is not None, pop_results

    submit_dict = {
        "id": job_id,
        "result": {"vectorize": "Test"},
        "state": "ok",
    }
    submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 1
    gen = retrieve_results["forms"][0]
    assert "result" in gen
    assert isinstance(gen["result"], dict)
    assert "vectorize" in gen["result"]
    assert gen["form"] == "vectorize"
    assert gen["result"]["vectorize"] == "Test"
    assert gen["state"] == "done"
    assert retrieve_results["state"] == "done"


def test_alchemy_annotation(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "canny"}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["annotation"],
        "annotation_types": ["canny"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
    }
    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    popped_form = pop_results["forms"][0]
    job_id = popped_form["id"]
    assert job_id is not None, pop_results
    assert popped_form["form"] == "annotation"
    # annotation is an image-output form, so the pop must carry an R2 upload destination.
    assert popped_form["r2_upload"], pop_results
    # The parameterized control type must survive round-trip to the worker via the form payload.
    assert popped_form["payload"]["control_type"] == "canny", pop_results

    submit_dict = {
        "id": job_id,
        "result": {"annotation": "R2"},
        "state": "ok",
    }
    submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 1
    gen = retrieve_results["forms"][0]
    assert gen["form"] == "annotation"
    assert gen["state"] == "done"
    assert retrieve_results["state"] == "done"


def _annotation_reward_for_control_type(client, request_headers: dict[str, str], control_type: str) -> float:
    """Run a full annotation pop->submit cycle for ``control_type`` and return the awarded kudos."""
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": control_type}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop_dict = {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "annotation_types": [control_type],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        }
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
        job_id = pop_req.get_json()["forms"][0]["id"]

        submit_dict = {"id": job_id, "result": {"annotation": "R2"}, "state": "ok"}
        submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
        assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
        return submit_req.get_json()["reward"]
    finally:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)


def test_alchemy_annotation_bucket_prices_by_detector_class(client, request_headers: dict[str, str]) -> None:
    """Pricing uses the detector cost bucket: a heavy hub detector (oneformer) pays more per tile
    than a weightless one (canny) for the same source image."""
    canny_reward = _annotation_reward_for_control_type(client, request_headers, "canny")
    oneformer_reward = _annotation_reward_for_control_type(client, request_headers, "oneformer_ade20k")
    assert canny_reward > 0
    assert oneformer_reward > canny_reward, (canny_reward, oneformer_reward)


def _pop_annotation(client, request_headers: dict[str, str], pop_dict: dict) -> dict:
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "canny"}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
        return pop_req.get_json()
    finally:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)


def test_alchemy_annotation_non_matching_type_does_not_pop(client, request_headers: dict[str, str]) -> None:
    """A worker advertising annotation types that exclude the job's control_type receives no job."""
    pop_results = _pop_annotation(
        client,
        request_headers,
        {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "annotation_types": ["depth"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
    )
    assert not pop_results.get("forms"), pop_results


def test_alchemy_annotation_absent_types_does_not_pop(client, request_headers: dict[str, str]) -> None:
    """The annotation form is fail-closed: a worker that advertises no annotation types matches no jobs."""
    pop_results = _pop_annotation(
        client,
        request_headers,
        {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
    )
    assert not pop_results.get("forms"), pop_results


def test_alchemy_annotation_rejects_unknown_control_type(client, request_headers: dict[str, str]) -> None:
    """The server validates control_type against its closed set and rejects anything outside it."""
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "not_a_real_detector"}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code == 400, async_req.get_data(as_text=True)


def test_alchemy_annotation_requires_control_type(client, request_headers: dict[str, str]) -> None:
    """An annotation form with no control_type payload is rejected rather than silently defaulted."""
    async_dict = {
        "forms": [
            {"name": "annotation"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code == 400, async_req.get_data(as_text=True)


def test_alchemist_palette_and_describe(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "palette"},
            {"name": "describe"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["palette", "describe"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
        "amount": 2,
    }

    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    assert len(pop_results["forms"]) == 2, pop_results

    expected_results = {"palette": "Test palette", "describe": "Test Describe"}

    # Submit each form individually with its own job ID
    for form in pop_results["forms"]:
        job_id = form["id"]
        form_name = form["form"]
        assert job_id is not None, pop_results
        submit_dict = {
            "id": job_id,
            "result": {form_name: expected_results[form_name]},
            "state": "ok",
        }
        submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
        assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
        submit_results = submit_req.get_json()
        assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 2
    assert retrieve_results["state"] == "done"

    # Build a dict keyed by form name for order-independent assertions
    forms_by_name = {f["form"]: f for f in retrieve_results["forms"]}
    for form_name, expected_result in expected_results.items():
        form = forms_by_name[form_name]
        assert "result" in form
        assert isinstance(form["result"], dict)
        assert form_name in form["result"]
        assert form["result"][form_name] == expected_result
        assert form["state"] == "done"
