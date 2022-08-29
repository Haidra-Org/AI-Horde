import requests
import json, os
import time
import argparse
import logging
import clientData as cd

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('-i', '--interval', action="store", required=False, type=int, default=1, help="The amount of seconds with which to check if there's new prompts to generate")

model = ''
max_content_length = 1024
max_length = 80

def validate_kai(kai):
    global model
    global max_content_length
    global max_length
    try:
        model_req = requests.get(kai + '/api/latest/model')
        if type(model_req.json()) is not dict:
            logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
            return(False)
        model = model_req.json()["result"]
    except requests.exceptions.JSONDecodeError:
        logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
        return(False)
    except requests.exceptions.ConnectionError:
        logging.error(f"Server {kai} is not reachable. Are you sure it's running?")
        return(False)
    try:
        model_req = requests.get(kai + '/api/latest/config/max_context_length')
        if type(model_req.json()) is not dict:
            logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
            return(False)
        max_content_length = model_req.json()["value"]
    except requests.exceptions.JSONDecodeError:
        logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
        return(False)
    try:
        model_req = requests.get(kai + '/api/latest/config/max_length')
        if type(model_req.json()) is not dict:
            logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
            return(False)
        max_length = model_req.json()["value"]
    except requests.exceptions.JSONDecodeError:
        logging.error(f"Server {kai} is up but does not appear to be a KoboldAI server. Are you sure it's running the UNITED branch?")
        return(False)
    return(True)


if __name__ == "__main__":
    #logging.basicConfig(filename='server.log', encoding='utf-8', level=logging.DEBUG)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.DEBUG)
    args = arg_parser.parse_args()
    global interval
    interval = args.interval
    current_id = None
    current_payload = None
    loop_retry = 0
    while True:
        if not validate_kai(cd.kai):
            logging.warning(f"Waiting 10 seconds...")
            time.sleep(10)
            continue
        gen_dict = {
            "username": cd.username,
            "name": cd.kai_name,
            "model": model,
            "max_length": max_length,
            "max_content_length": max_content_length,
        }
        if current_id:
            loop_retry += 1
        else:
            try:
                pop_req = requests.post(cd.server + '/generate/pop', json = gen_dict)
            except requests.exceptions.ConnectionError:
                logging.warning(f"Server {cd.server} unavailable during pop. Waiting 10 seconds...")
                time.sleep(10)
                continue
            if not pop_req.ok:
                logging.warning(f"During gen pop, server {cd.server} responded: {pop_req.text}. Waiting for 10 seconds...")
                time.sleep(10)
                continue
            pop = pop_req.json()
            if not pop["id"]:
                logging.info(f"Server {cd.server} has no valid generations to do for us. Skipped Info: {pop['skipped']}.")
                time.sleep(interval)
                continue
            current_id = pop['id']
            current_payload = pop['payload']
        gen_req = requests.post(cd.kai + '/api/latest/generate/', json = current_payload)
        if type(gen_req.json()) is not dict:
            logging.error(f'KAI instance {kai_instance} API unexpected response on generate: {gen_req}. Sleeping 10 seconds...')
            time.sleep(9)
            continue
        if gen_req.status_code == 503:
            logging.info(f'KAI instance {kai_instance} Busy (attempt {loop_retry}). Will try again...')
            continue
        current_generation = gen_req.json()["results"][0]["text"]
        submit_dict = {
            "id": current_id,
            "generation": current_generation,
        }
        while current_id and current_generation:
            try:
                submit_req = requests.post(cd.server + '/generate/submit', json = submit_dict)
                if not submit_req.ok:
                    logging.warning(f"During gen submit, server {cd.server} responded: {submit_req.text}. Waiting for 10 seconds...")
                    time.sleep(10)
                    continue
                logging.info(f'Submitted generation with id {current_id} and contributed for {submit_req.json()["reward"]}')
                current_id = None
                current_payload = None
                current_generation = None
            except requests.exceptions.ConnectionError:
                logging.warning(f"Server {cd.server} unavailable during submit. Waiting 10 seconds...")
                time.sleep(10)
                continue
        time.sleep(interval)
