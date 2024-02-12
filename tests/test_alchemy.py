import requests
import json

CIVERSION = "0.1.1"
HORDE_URL = "dev.stablehorde.net"
TEST_MODELS = ["elinas/chronos-70b-v2"]

def test_simple_alchemy() -> None:
    headers = {
        "apikey": "2bc5XkMeLAWiN9O5s7bhfg", # ci/cd user
        "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625"
    }
    async_dict = {
        "forms":[
            {"name": "caption"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true"
    }    
    async_req = requests.post(f'https://{HORDE_URL}/api/v2/interrogate/async', json = async_dict, headers = headers)
    assert async_req.ok
    async_results = async_req.json()
    req_id = async_results['id']
    # print(async_results)
    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["caption", "strip_background", "interrogation"],
        "bridge_agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625",
        "max_tiles": 96,
    }
    try:
        pop_req = requests.post(f'https://{HORDE_URL}/api/v2/interrogate/pop', json = pop_dict, headers = headers)
    except Exception:
        requests.delete(f'https://{HORDE_URL}/api/v2/interrogate/status/{req_id}', headers = headers)
        raise
    assert pop_req.ok
    pop_results = pop_req.json()
    # print(json.dumps(pop_results, indent=4))

    job_id = pop_results['forms'][0]['id']
    assert job_id is not None
    submit_dict = {
        "id": job_id,
        "result": {"caption":"Test"},
        "state": "ok",
    }
    submit_req = requests.post(f'https://{HORDE_URL}/api/v2/interrogate/submit', json = submit_dict, headers = headers)
    assert submit_req.ok
    submit_results = submit_req.json()
    assert submit_results["reward"] > 0
    retrieve_req = requests.get(f'https://{HORDE_URL}/api/v2/interrogate/status/{req_id}', headers = headers)
    assert retrieve_req.ok
    retrieve_results = retrieve_req.json()
    # print(json.dumps(retrieve_results,indent=4))
    assert len(retrieve_results['forms']) == 1
    gen = retrieve_results['forms'][0]
    assert 'result' in gen
    assert 'result' in gen
    assert isinstance(gen['result'], dict)
    assert 'caption' in gen['result']
    assert gen['form'] == "caption"
    assert gen['result']['caption'] == "Test"
    assert gen['state'] == 'done'
    assert retrieve_results['state'] == 'done'

if __name__ == "__main__":
    test_simple_alchemy()