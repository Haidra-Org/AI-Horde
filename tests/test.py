import requests

CIVERSION = "0.1.1"

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
        "models": ["Fustercluck", "AlbedoBase XL (SDXL)"],
        "loras": [{"name": "247778", "is_version": True}],
    }    
    async_req = requests.post('https://dev.aihorde.net/api/v2/generate/async', json = async_dict, headers = headers)
    print(async_req)
    assert async_req.ok
    submit_results = async_req.json()
    req_id = submit_results['id']
    pop_dict = {
        "name": "CI/CD Fake Worker",
        "models": ["Fustercluck", "AlbedoBase XL (SDXL)"],
        "bridge_agent": f"aihorde_ci_worker:{CIVERSION}:(discord)db0#1625",
        "amount": 10,
        "max_pixels": 4194304,
        "allow_img2img": True,
        "allow_painting": True,
        "allow_unsafe_ipaddr": True,
        "allow_post_processing": True,
        "allow_controlnet": True,
        "allow_lora": True        
    }
    pop_req = requests.post('https://dev.aihorde.net/api/v2/generate/pop', json = pop_dict, headers = headers)
    print(pop_req)
    assert pop_req.ok
    pop_results = pop_req.json()
    job_id = pop_results['id']
    assert job_id is not None
    submit_dict = {
        "id": job_id,
        "generation": "R2",
        "state": "ok",
        "seed": 0,
    }
    submit_req = requests.post('https://dev.aihorde.net/api/v2/generate/pop', json = submit_dict, headers = headers)
    assert submit_req.ok
    submit_results = pop_req.json()
    assert submit_results["kudos"] > 0
    retrieve_req = requests.get(f'https://dev.aihorde.net/api/v2/generate/status/{req_id}', headers = headers)
    assert retrieve_req.ok
    retrieve_req = async_req.json()
    print(retrieve_req)

if __name__ == "__main__":
    test_simple_image_gen()