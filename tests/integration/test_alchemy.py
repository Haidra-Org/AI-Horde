# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later


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
