import requests
import json

CIVERSION = "0.1.1"
HORDE_URL = "dev.stablehorde.net"
TEST_MODELS = ["Fustercluck", "AlbedoBase XL (SDXL)"]

def test_simple_image_gen() -> None:
    headers = {
        "apikey": "2bc5XkMeLAWiN9O5s7bhfg", # ci/cd user
        "Client-Agent": f"aihorde_ci_client:{CIVERSION}:(discord)db0#1625"
    }
    async_dict = {
        "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "width": 1024,
        "height": 1024,
        "steps": 8,
        "cfg_scale": 1.5,
        "sampler_name": "k_euler_a",
        "models": TEST_MODELS,
        "loras": [{"name": "247778", "is_version": True}],
    }    
    async_req = requests.post(f'https://{HORDE_URL}/api/v2/generate/async', json = async_dict, headers = headers)
    assert async_req.ok
    async_results = async_req.json()
    req_id = async_results['id']
    # print(async_results)
    pop_dict = {
        "name": "CICD Fake Dreamer",
        "models": ["Fustercluck", "AlbedoBase XL (SDXL)"],
        "bridge_agent": f"AI Horde Worker reGen:4.1.0-citests:https://github.com/Haidra-Org/horde-worker-reGen",
        "amount": 10,
        "max_pixels": 4194304,
        "allow_img2img": True,
        "allow_painting": True,
        "allow_unsafe_ipaddr": True,
        "allow_post_processing": True,
        "allow_controlnet": True,
        "allow_lora": True        
    }
    pop_req = requests.post(f'https://{HORDE_URL}/api/v2/generate/pop', json = pop_dict, headers = headers)
    assert pop_req.ok
    pop_results = pop_req.json()
    # print(json.dumps(pop_results, indent=4))

    job_id = pop_results['id']
    assert job_id is not None
    submit_dict = {
        "id": job_id,
        "generation": "R2",
        "state": "ok",
        "seed": 0,
    }
    submit_req = requests.post(f'https://{HORDE_URL}/api/v2/generate/submit', json = submit_dict, headers = headers)
    assert submit_req.ok
    submit_results = submit_req.json()
    assert submit_results["reward"] > 0
    retrieve_req = requests.get(f'https://{HORDE_URL}/api/v2/generate/status/{req_id}', headers = headers)
    assert retrieve_req.ok
    retrieve_results = retrieve_req.json()
    # print(json.dumps(retrieve_results,indent=4))
    assert len(retrieve_results['generations']) == 1
    gen = retrieve_results['generations'][0]
    assert len(gen['gen_metadata']) == 0
    assert gen['seed'] == "0"
    assert gen['worker_name'] == "CICD Fake Dreamer"
    assert gen['model'] in TEST_MODELS
    assert gen['state'] == 'ok'
    assert retrieve_results['kudos'] > 1
    assert retrieve_results['done'] is True

if __name__ == "__main__":
    test_simple_image_gen()