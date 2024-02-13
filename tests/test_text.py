import requests

CIVERSION = "0.1.1"
HORDE_URL = "dev.stablehorde.net"
TEST_MODELS = ["elinas/chronos-70b-v2"]


def test_simple_text_gen() -> None:
    headers = {"apikey": "2bc5XkMeLAWiN9O5s7bhfg", "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625"}  # ci/cd user
    async_dict = {
        "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
        "trusted_workers": True,
        "max_length": 512,
        "max_context_length": 2048,
        "temperature": 1,
        "models": TEST_MODELS,
    }
    async_req = requests.post(f"https://{HORDE_URL}/api/v2/generate/text/async", json=async_dict, headers=headers)
    assert async_req.ok
    async_results = async_req.json()
    req_id = async_results["id"]
    # print(async_results)
    pop_dict = {
        "name": "CICD Fake Scribe",
        "models": ["elinas/chronos-70b-v2"],
        "bridge_agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625",
        "amount": 10,
        "max_context_length": 4096,
        "max_length": 512,
    }
    pop_req = requests.post(f"https://{HORDE_URL}/api/v2/generate/text/pop", json=pop_dict, headers=headers)
    assert pop_req.ok
    pop_results = pop_req.json()
    # print(json.dumps(pop_results, indent=4))

    job_id = pop_results["id"]
    assert job_id is not None
    submit_dict = {
        "id": job_id,
        "generation": "Test",
        "state": "ok",
        "seed": 0,
    }
    submit_req = requests.post(f"https://{HORDE_URL}/api/v2/generate/text/submit", json=submit_dict, headers=headers)
    assert submit_req.ok
    submit_results = submit_req.json()
    assert submit_results["reward"] > 0
    retrieve_req = requests.get(f"https://{HORDE_URL}/api/v2/generate/text/status/{req_id}", headers=headers)
    assert retrieve_req.ok
    retrieve_results = retrieve_req.json()
    # print(json.dumps(retrieve_results,indent=4))
    assert len(retrieve_results["generations"]) == 1
    gen = retrieve_results["generations"][0]
    assert len(gen["gen_metadata"]) == 0
    assert gen["text"] == "Test"
    assert gen["worker_name"] == "CICD Fake Scribe"
    assert gen["model"] in TEST_MODELS
    assert gen["state"] == "ok"
    assert retrieve_results["kudos"] > 1
    assert retrieve_results["done"] is True


if __name__ == "__main__":
    test_simple_text_gen()
