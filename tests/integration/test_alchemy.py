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


def test_alchemist_pallette_and_describe(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "pallette"},
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
        "forms": ["pallette", "describe"],
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
        "result": {"pallette": "Test Pallette", "describe": "Test Describe"},
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
    pallette_gen = retrieve_results["forms"][0]
    describe_gen = retrieve_results["forms"][1]
    assert "result" in pallette_gen
    assert isinstance(pallette_gen["result"], dict)
    assert "pallette" in pallette_gen["result"]
    assert pallette_gen["form"] == "pallette"
    assert pallette_gen["result"]["pallette"] == "Test Pallette"
    assert pallette_gen["state"] == "done"

    assert "result" in describe_gen
    assert isinstance(describe_gen["result"], dict)
    assert "describe" in describe_gen["result"]
    assert describe_gen["form"] == "describe"
    assert describe_gen["result"]["describe"] == "Test Describe"
    assert describe_gen["state"] == "done"
