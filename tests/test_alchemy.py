import requests


def test_simple_alchemy(api_key: str, HORDE_URL: str, CIVERSION: str) -> None:
    headers = {"apikey": api_key, "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625"}  # ci/cd user
    async_dict = {
        "forms": [
            {"name": "caption"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = requests.post(f"http://{HORDE_URL}/api/v2/interrogate/async", json=async_dict, headers=headers)
    assert async_req.ok, async_req.text
    async_results = async_req.json()
    req_id = async_results["id"]
    # print(async_results)
    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["caption", "strip_background", "interrogation"],
        "bridge_agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625",
        "max_tiles": 96,
    }
    try:
        pop_req = requests.post(f"http://{HORDE_URL}/api/v2/interrogate/pop", json=pop_dict, headers=headers)
    except Exception:
        requests.delete(f"http://{HORDE_URL}/api/v2/interrogate/status/{req_id}", headers=headers)
        raise
    assert pop_req.ok, pop_req.text
    pop_results = pop_req.json()
    # print(json.dumps(pop_results, indent=4))

    job_id = pop_results["forms"][0]["id"]
    assert job_id is not None, pop_results
    submit_dict = {
        "id": job_id,
        "result": {"caption": "Test"},
        "state": "ok",
    }
    submit_req = requests.post(f"http://{HORDE_URL}/api/v2/interrogate/submit", json=submit_dict, headers=headers)
    assert submit_req.ok, submit_req.text
    submit_results = submit_req.json()
    assert submit_results["reward"] > 0
    retrieve_req = requests.get(f"http://{HORDE_URL}/api/v2/interrogate/status/{req_id}", headers=headers)
    assert retrieve_req.ok, retrieve_req.text
    retrieve_results = retrieve_req.json()
    # print(json.dumps(retrieve_results,indent=4))
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


if __name__ == "__main__":
    test_simple_alchemy()
