import requests
import json, os
import time
import argparse
import logging
import random
try:
    import clientData as cd
except:
    class temp(object):
        def __init__(self):
            random.seed()
            # The cluster url
            self.cluster_url = "http://koboldai.net"
            # Where can your bridge reach your KAI instance
            self.kai_url = "http://localhost:5000"
            # Give a cool name to your instance
            self.kai_name = f"Automated Instance #{random.randint(-100000000, 100000000)}"
            # The api_key identifies a unique user in the horde
            # Visit https://koboldai.net/register to create one before you can join
            self.api_key = "0000000000"
            # Put other users whose prompts you want to prioritize.
            # The owner's username is always included so you don't need to add it here, unless you want it to have lower priority than another user
            self.priority_usernames = []
    cd = temp()
    pass

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('-i', '--interval', action="store", required=False, type=int, default=1, help="The amount of seconds with which to check if there's new prompts to generate")
arg_parser.add_argument('-a', '--api_key', action="store", required=False, type=str, help="The API key corresponding to the owner of the KAI instance")
arg_parser.add_argument('-n', '--kai_name', action="store", required=False, type=str, help="The server name. It will be shown to the world and there can be only one.")
arg_parser.add_argument('-k', '--kai_url', action="store", required=False, type=str, help="The KoboldAI server URL. Where the bridge will get its generations from.")
arg_parser.add_argument('-c', '--cluster_url', action="store", required=False, type=str, help="The KoboldAI Cluster URL. Where the bridge will pickup prompts and send the finished generations.")
arg_parser.add_argument('--debug', action="store_true", default=False, help="Show debugging messages.")
arg_parser.add_argument('--priority_usernames',type=str, action='append', required=False, help="Usernames which get priority use in this server. The owner's username is always in this list.")

model = ''
max_content_length = 1024
max_length = 80
current_softprompt = None
softprompts = {}

def validate_kai(kai):
    global model
    global max_content_length
    global max_length
    global softprompts
    global current_softprompt
    try:
        req = requests.get(kai + '/api/latest/model')
        model = req.json()["result"]
        req = requests.get(kai + '/api/latest/config/max_context_length')
        max_content_length = req.json()["value"]
        req = requests.get(kai + '/api/latest/config/max_length')
        max_length = req.json()["value"]
        if model not in softprompts:
                req = requests.get(kai + '/api/latest/config/soft_prompts_list')
                softprompts[model] = [sp['value'] for sp in req.json()["values"]]
        req = requests.get(kai + '/api/latest/config/soft_prompt')
        current_softprompt = req.json()["value"]
    except requests.exceptions.JSONDecodeError:
        logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
        return(False)
    except requests.exceptions.ConnectionError:
        logging.error(f"Server {kai} is not reachable. Are you sure it's running?")
        return(False)
    return(True)


if __name__ == "__main__":
    #logging.basicConfig(filename='server.log', encoding='utf-8', level=logging.DEBUG)
    args = arg_parser.parse_args()
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.DEBUG if args.debug else logging.INFO)
    global interval
    interval = args.interval
    current_id = None
    current_payload = None
    loop_retry = 0
    api_key = args.api_key if args.api_key else cd.api_key
    kai_name = args.kai_name if args.kai_name else cd.kai_name
    kai_url = args.kai_url if args.kai_url else cd.kai_url
    cluster = args.cluster_url if args.cluster_url else cd.cluster_url
    priority_usernames = args.priority_usernames if args.priority_usernames else cd.priority_usernames
    logging.info(f"Starting {kai_name} instance")
    while True:
        if not validate_kai(kai_url):
            logging.warning(f"Waiting 10 seconds...")
            time.sleep(10)
            continue
        gen_dict = {
            "api_key": api_key,
            "name": kai_name,
            "model": model,
            "max_length": max_length,
            "max_content_length": max_content_length,
            "priority_usernames": priority_usernames,
            "softprompts": softprompts[model],
        }
        if current_id:
            loop_retry += 1
        else:
            try:
                pop_req = requests.post(cluster + '/api/v1/generate/pop', json = gen_dict)
            except requests.exceptions.ConnectionError:
                logging.warning(f"Server {cluster} unavailable during pop. Waiting 10 seconds...")
                time.sleep(10)
                continue
            except requests.exceptions.JSONDecodeError():
                logging.warning(f"Server {cluster} unavailable during pop. Waiting 10 seconds...")
                time.sleep(10)
                continue
            if not pop_req.ok:
                logging.warning(f"During gen pop, server {cluster} responded: {pop_req.text}. Waiting for 10 seconds...")
                time.sleep(10)
                continue
            pop = pop_req.json()
            if not pop:
                logging.error(f"Something has gone wrong with {cluster}. Please inform its administrator!")
                time.sleep(interval)
                continue
            if not pop["id"]:
                logging.debug(f"Server {cluster} has no valid generations to do for us. Skipped Info: {pop['skipped']}.")
                time.sleep(interval)
                continue
            current_id = pop['id']
            current_payload = pop['payload']
            # By default, we don't want to be annoucing the prompt send from the Horde to the terminal
            current_payload['quiet'] = True
            requested_softprompt = pop['softprompt']
        if requested_softprompt != current_softprompt:
            req = requests.put(kai_url + '/api/latest/config/soft_prompt/', json = {"value": requested_softprompt})
            time.sleep(1) # Wait a second to unload the softprompt
        gen_req = requests.post(kai_url + '/api/latest/generate/', json = current_payload)
        if type(gen_req.json()) is not dict:
            logging.error(f'KAI instance {kai_url} API unexpected response on generate: {gen_req}. Sleeping 10 seconds...')
            time.sleep(9)
            continue
        if gen_req.status_code == 503:
            logging.debug(f'KAI instance {kai_url} Busy (attempt {loop_retry}). Will try again...')
            continue
        current_generation = gen_req.json()["results"][0]["text"]
        submit_dict = {
            "id": current_id,
            "generation": current_generation,
            "api_key": api_key,
        }
        while current_id and current_generation:
            try:
                submit_req = requests.post(cluster + '/api/v1/generate/submit', json = submit_dict)
                if submit_req.status_code == 404:
                    logging.warning(f"The generation we were working on got stale. Aborting!")
                elif not submit_req.ok:
                    if "already submitted" in submit_req.text:
                        logging.warning(f'Server think this gen already submitted. Continuing')
                    else:
                        logging.error(submit_req.status_code)
                        logging.warning(f"During gen submit, server {cluster} responded: {submit_req.text}. Waiting for 10 seconds...")
                        time.sleep(10)
                        continue
                else:
                    logging.info(f'Submitted generation with id {current_id} and contributed for {submit_req.json()["reward"]}')
                current_id = None
                current_payload = None
                current_generation = None
            except requests.exceptions.ConnectionError:
                logging.warning(f"Server {cluster} unavailable during submit. Waiting 10 seconds...")
                time.sleep(10)
                continue
        time.sleep(interval)
