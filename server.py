from flask import Flask
from flask_restful import Resource, reqparse, Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging
import requests
import json, os
from enum import Enum
import threading, time
from uuid import uuid4
from datetime import datetime

class ServerErrors(Enum):
    INVALIDKAI = 0
    CONNECTIONERR = 1
    BADARGS = 2
    REJECTED = 3
    WRONG_CREDENTIALS = 4
    INVALID_PROCGEN = 5
    DUPLICATE_GEN = 6

### Globals

# This is used for asynchronous generations
# They key is the ID of the prompt, the value is the WaitingPrompt object
waiting_prompts = {}
# They key is the ID of the generation, the value is the ProcessingGeneration object
processing_generations = {}
# This is used for synchronous generations
servers_file = "servers.json"
servers = {}
# How many tokens each user has requested
usage_file = "usage.json"
usage = {}
# How many tokens each user's server has generated
contributions_file = "contributions.json"
contributions = {}

###Code goes here###


REST_API = Flask(__name__)
# Very basic DOS prevention
limiter = Limiter(
    REST_API,
    key_func=get_remote_address,
    default_limits=["90 per minute"]
)
api = Api(REST_API)


def get_error(error, **kwargs):
    if error == ServerErrors.INVALIDKAI:
        logging.warning(f'Invalid KAI instance: {kwargs["kai_instance"]}')
        return(f"Server {kwargs['kai_instance']} appears running but does not appear to be a KoboldAI instance. Please start KoboldAI first then try again!")
    if error == ServerErrors.CONNECTIONERR:
        logging.warning(f'Connection Error when attempting to reach server: {kwargs["kai_instance"]}')
        return(f"KoboldAI instance {kwargs['kai_instance']} does not seem to be responding. Please load KoboldAI first, ensure it's reachable through the internet, then try again")
    if error == ServerErrors.BADARGS:
        logging.warning(f'{kwargs["username"]} send bad value for {kwargs["bad_arg"]}: {kwargs["bad_value"]}')
        return(f'Bad value for {kwargs["bad_arg"]}: {kwargs["bad_value"]}')
    if error == ServerErrors.REJECTED:
        logging.warning(f'{kwargs["username"]} prompt rejected by all instances with reasons: {kwargs["rejection_details"]}')
        return(f'prompt rejected by all instances with reasons: {kwargs["rejection_details"]}')
    if error == ServerErrors.WRONG_CREDENTIALS:
        logging.warning(f'{kwargs["kai_instance"]} sent wrong credentials for modifying instance {kwargs["kai_instance"]}')
        return(f'wrong credentials for modifying instance {kwargs["kai_instance"]}')
    if error == ServerErrors.INVALID_PROCGEN:
        logging.warning(f'Server attempted to provide generation for {kwargs["id"]} but it did not exist')
        return(f'Processing Generation with ID {kwargs["id"]} does not exist')
    if error == ServerErrors.DUPLICATE_GEN:
        logging.warning(f'Server attempted to provide duplicate generation for {kwargs["id"]} ')
        return(f'Processing Generation with ID {kwargs["id"]} already submitted')

def write_servers_to_disk():
    serialized_list = []
    for s in servers:
        serialized_list.append(servers[s].serialize())
    with open(servers_file, 'w') as db:
        json.dump(serialized_list,db)

def write_usage_to_disk():
    with open(usage_file, 'w') as db:
        json.dump(usage,db)
    with open(contributions_file, 'w') as db:
        json.dump(contributions,db)


def update_instance_details(kai_instance, **kwargs):
    try:
        model_req = requests.get(kai_instance + '/api/latest/model')
        if type(model_req.json()) is not dict:
            return(f"{get_error(ServerErrors.INVALIDKAI,kai_instance = kai_instance)}",400)
        model = model_req.json()["result"]
    except requests.exceptions.JSONDecodeError:
        return(f"{get_error(ServerErrors.INVALIDKAI,kai_instance = kai_instance)}",400)
    except requests.exceptions.ConnectionError:
        return(f"{get_error(ServerErrors.CONNECTIONERR,kai_instance = kai_instance)}",400)
    # If the username arg is provided, we consider this server new
    if "username" in kwargs:
        existing_details = servers.get(kai_instance)
        username = kwargs["username"]
        if existing_details and existing_details['password'] != password:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = kai_instance, username = username)}",400)
        logging.info(f'{username} added server {kai_instance}')
        servers_dict = {
            "model": model,
            "username": username,
            "max_length": kwargs.get("max_length", 512),
            "max_content_length": kwargs.get("max_content_length", 2048)
        }
        servers[kai_instance] = servers_dict
        write_servers_to_disk()
    elif servers[kai_instance]["model"] != model:
        logging.info(f'Updated server {kai_instance} model from {servers_dict[model]} to {model}')
        servers_dict = servers[kai_instance]
        servers_dict[model] = model
        servers[kai_instance] = servers_dict
        write_servers_to_disk()
    return('OK',200)


def generate(prompt, username, models = [], params = {}):
    rejections = {}
    generations = []
    gen_n = params.get('n', 1)
    params['n'] = 1
    rejecting_servers = []
    for iter in range(gen_n):
        current_generation = None
        while not current_generation and len(servers):
            for kai_instance in servers:
                if kai_instance in rejecting_servers:
                    continue
                kai_details = servers[kai_instance]
                max_length = params.get("max_length", 80)
                if type(max_length) is not int:
                    return(f'{get_error(ServerErrors.BADARGS,username = username, bad_arg = "max_length", bad_value = max_length)}',400)
                if max_length > kai_details["max_length"]:
                    rejections["max_length"] = rejections.get("max_length",0) + 1
                    rejections["total"] = rejections.get("total",0) + 1
                    rejecting_servers.append(kai_instance)
                    continue
                max_content_length = params.get("max_content_length", 80)
                if type(max_content_length) is not int:
                    return(f'{get_error(ServerErrors.BADARGS,username = username, bad_arg = "max_content_length", bad_value = max_content_length)}',400)
                if max_content_length > kai_details["max_content_length"]:
                    rejections["total"] = rejections.get("total",0) + 1
                    rejections["max_content_length"] = rejections.get("max_content_length",0) + 1
                    rejecting_servers.append(kai_instance)
                    continue
                # We only refresh server status on first iteration
                if iter == 0:
                    srv_chk = update_instance_details(kai_instance)
                    if srv_chk[1] != 200:
                        rejections["total"] = rejections.get("total",0) + 1
                        rejections["server_unavailable"] = rejections.get("server_unavailable",0) + 1
                        continue
                models = params.get("models", [])
                if type(models) is not list:
                    return(f'{get_error(ServerErrors.BADARGS,username = username, bad_arg = "models", bad_value = models)}',400)
                if models != [] and kai_details["model"] not in models:
                    rejections["total"] = rejections.get("total",0) + 1
                    rejections["models"] = rejections.get("models",0) + 1
                    rejecting_servers.append(kai_instance)
                    continue
                try:
                    gen_dict = params
                    gen_dict["prompt"] = prompt
                    gen_req = requests.post(kai_instance + '/api/latest/generate/', json = gen_dict)
                    if type(gen_req.json()) is not dict:
                        logging.error(f'KAI instance {kai_instance} API unexpected response on generate: {gen_req}')
                        continue
                    if gen_req.status_code == 503:
                        logging.info(f'KAI instance {kai_instance} Busy. Will try again later')
                        continue
                    c_username = kai_details["username"]
                    contributions[c_username] = contributions.get(c_username,0) + max_length
                    usage[username] = usage.get(username,0) + max_length
                    current_generation = gen_req.json()["results"][0]["text"]
                    generations.append(current_generation)
                except requests.exceptions.ConnectionError:
                    logging.error(f'KAI instance {kai_instance} API unexpected error on generate: {prompt} with params {params}')
                    continue
    if len(generations) == 0:
        return(f'{get_error(ServerErrors.REJECTED,username = username, rejection_details = rejections)}',400)
    return(generations, 200)


@REST_API.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization"
    return response

class Register(Resource):
    #decorators = [limiter.limit("1/minute")]
    decorators = [limiter.limit("10/minute")]
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("url", type=str, required=True, help="Full URL The KoboldAI server. E.g. 'https://example.com:5000'")
        parser.add_argument("username", type=str, required=True, help="Username for contributions")
        parser.add_argument("password", type=str, required=True, help="Password for changing server settings")
        parser.add_argument("max_length", type=int, required=False, default=80, help="The max number of tokens this server can generate. This will set the max for each client.")
        parser.add_argument("max_content_length", type=int, required=False, default=1024, help="The max amount of context to submit to this AI for sampling. This will set the max for each client.")
        args = parser.parse_args()
        ret = update_instance_details(
            args["url"],
            username = args["username"],
            password = args["password"],
            max_length = args["max_length"],
            max_content_length = args["max_content_length"],
        )
        return(ret)

class Usage(Resource):
    def get(self):
        return(usage,200)


class Contributions(Resource):
    def get(self):
        return(contributions,200)


class Generate(Resource):
    decorators = [limiter.limit("10/minute")]
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
        parser.add_argument("username", type=str, required=True, help="Username to track usage")
        parser.add_argument("models", type=list, required=False, default=[], help="The acceptable models with which to generate")
        parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the KoboldAI server")
        args = parser.parse_args()
        ret = generate(
            args["prompt"],
            args["username"],
            args["models"],
            args["params"],
        )
        return(ret)


class AsyncGeneratePrompt(Resource):
    decorators = [limiter.limit("30/minute")]
    def get(self, id):
        wp = waiting_prompts.get(id)
        if not wp:
            return("ID not found", 404)
        return(wp.get_status(), 200)


class AsyncGenerate(Resource):
    decorators = [limiter.limit("10/minute")]
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
        parser.add_argument("username", type=str, required=True, help="Username to track usage")
        parser.add_argument("models", type=list, required=False, default=[], help="The acceptable models with which to generate")
        parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the KoboldAI server")
        args = parser.parse_args()
        wp = WaitingPrompt(
            args["prompt"],
            args["username"],
            args["models"],
            args["params"],

        )
        return({"id":wp.id}, 200)


class PromptPop(Resource):
    decorators = [limiter.limit("1/second")]
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("username", type=str, required=True, help="Username to track contributions")
        parser.add_argument("name", type=str, required=True, help="The server's unique name, to track contributions")
        parser.add_argument("model", type=str, required=True, default=[], help="The model currently running on this KoboldAI")
        parser.add_argument("max_length", type=int, required=False, default=512, help="The maximum amount of tokens this server can generate")
        parser.add_argument("max_content_length", type=int, required=False, default=2048, help="The max amount of context to submit to this AI for sampling.")
        args = parser.parse_args()
        skipped = {}
        server = servers.get(args['name'])
        if not server:
            server = KAIServer(args['username'], args['name'])
        server.check_in(args['model'], args['max_length'], args['max_content_length'])
        for wp_id in waiting_prompts:
            wp = waiting_prompts[wp_id]
            if not wp.needs_gen():
                continue
            if len(wp.models) and args['model'] not in wp.models:
                skipped["model"] = skipped.get("model",0) + 1
                continue
            if args['max_length'] < wp.max_length:
                skipped["max_length"] = skipped.get("max_length",0) + 1
                continue
            if args['max_content_length'] < wp.max_length:
                skipped["max_content_length"] = skipped.get("max_content_length",0) + 1
                continue
            ret = wp.start_generation(server)
            payload = ret[0]
            procgen = ret[1]
            return({"id": procgen.id, "payload": payload}, 200)
        return({"id": None, "skipped": skipped}, 200)


class SubmitGeneration(Resource):
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("id", type=str, required=True, help="The processing generation uuid")
        parser.add_argument("generation", type=str, required=False, default=[], help="The generated text")
        args = parser.parse_args()
        procgen = processing_generations.get(args['id'])
        if not procgen:
            return(f"{get_error(ServerErrors.INVALID_PROCGEN,id = args['id'])}",500)
        tokens = procgen.set_generation(args['generation'])
        if tokens == 0:
            return(f"{get_error(ServerErrors.DUPLICATE_GEN,id = args['id'])}",400)
        return({"reward": tokens}, 200)


class List(Resource):
    def get(self):
        servers_ret = []
        for s in servers:
            if servers[s].is_stale():
                continue
            sdict = {
                "name": servers[s].name,
                "id": servers[s].id,
                "model": servers[s].model,
                "max_length": servers[s].max_length,
                "max_content_length": servers[s].max_content_length,
                "tokens_generated": servers[s].contributions,
                "requests_fulfilled": servers[s].fulfilments,
                "latest_performance": servers[s].get_performance(),
            }
            servers_ret.append(sdict)
        return(servers_ret,200)

class ListSingle(Resource):
    def get(self, server_id):
        server = servers[server_id]
        sdict = {
            "name": server.name,
            "id": server.id,
            "model": server.model,
            "max_length": server.max_length,
            "max_content_length": server.max_content_length,
            "tokens_generated": server.contributions,
            "requests_fulfilled": server.fulfilments,
            "latest_performance": server.get_performance(),
        }
        return(sdict,200)


class WaitingPrompt:
    # Every 10 secs we store usage data to disk
    def __init__(self, prompt, username, models, params):
        self.prompt = prompt
        self.username = username
        self.models = models
        self.params = params
        self.n = params.get('n', 1)
        self.tokens = len(prompt.split())
        self.max_length = params.get("max_length", 80)
        self.max_content_length = params.get("max_content_length", 1024)
        self.total_usage = 0
        self.id = str(uuid4())
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = params
        self.gen_payload["prompt"] = prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["n"] = 1
        # The generations that have been created already
        self.processing_gens = []
        waiting_prompts[self.id] = self

    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, server):
        if self.n <= 0:
            return
        new_gen = ProcessingGeneration(self, server)
        self.processing_gens.append(new_gen)
        self.n -= 1
        return(self.gen_payload, new_gen)

    def is_completed(self):
        if self.needs_gen():
            return(False)
        for procgen in self.processing_gens:
            if not procgen.is_completed():
                return(False)
        return(True)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
        }
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["finished"] += 1
            else:
                ret_dict["processing"] += 1
        return(ret_dict)

    def get_status(self):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        ret_dict["done"] = self.is_completed()
        ret_dict["generations"] = []
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["generations"].append(procgen.generation)
        return(ret_dict)

    def record_usage(self):
        self.total_usage += self.tokens
        usage[self.username] += self.tokens


class ProcessingGeneration:
    def __init__(self, owner, server):
        self.id = str(uuid4())
        self.owner = owner
        self.server = server
        self.generation = None
        self.start_time = datetime.now()
        processing_generations[self.id] = self

    def set_generation(self, generation):
        if self.is_completed():
            return(0)
        self.generation = generation
        tokens = len(generation.split())
        self.server.record_contribution(tokens, (datetime.now() - self.start_time).seconds)
        self.owner.record_usage()
        return(tokens)

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)


class KAIServer:
    def __init__(self, username = None, name = None):
        self.username = username
        self.name = name
        self.contributions = 0
        self.fulfilments = 0
        self.performance = 0
        self.id = str(uuid4())
        if name:
            servers[self.id] = self

    def check_in(self, model, max_length, max_content_length):
        self.last_check_in = datetime.now()
        self.model = model
        self.max_content_length = max_content_length
        self.max_length = max_length

    def record_contribution(self, tokens, seconds_taken):
        contributions[self.username] = contributions.get(self.username,0) + tokens
        self.contributions += tokens
        self.fulfilments += 1
        self.performance = round(tokens / seconds_taken,2)

    def get_performance(self):
        if self.performance:
            ret_str = f'{self.performance} seconds per token'
        else:
            ret_str = f'No requests fulfiled yet'
        return(ret_str)

    def is_stale(self):
        if (datetime.now() - self.last_check_in).seconds > 300:
            return(True)
        return(False)

    def serialize(self):
        ret_dict = {
            "username": self.username,
            "name": self.name,
            "model": self.model,
            "max_length": self.max_length,
            "max_content_length": self.max_content_length,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "performance": self.performance,
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
        }
        return(ret_dict)

    def deserialize(self, saved_dict):
        self.username = saved_dict["username"]
        self.name = saved_dict["name"]
        self.model = saved_dict["model"]
        self.max_length = saved_dict["max_length"]
        self.max_content_length = saved_dict["max_content_length"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.performance = saved_dict["performance"]
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        servers[self.id] = self

class UsageStore(object):
    def __init__(self, interval = 3):
        self.interval = interval

        thread = threading.Thread(target=self.store_usage, args=())
        thread.daemon = True
        thread.start()

    def store_usage(self):
        while True:
            write_usage_to_disk()
            write_servers_to_disk()
            time.sleep(self.interval)


if __name__ == "__main__":
    #logging.basicConfig(filename='server.log', encoding='utf-8', level=logging.DEBUG)
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.DEBUG)
    if os.path.isfile(servers_file):
        with open(servers_file) as db:
            serialized_servers = json.load(db)
            for server_dict in serialized_servers:
                new_server = KAIServer()
                new_server.deserialize(server_dict)
                servers[new_server.name] = new_server
    if os.path.isfile(usage_file):
        with open(usage_file) as db:
            usage = json.load(db)
    if os.path.isfile(contributions_file):
        with open(contributions_file) as db:
            contributions = json.load(db)

    # api.add_resource(Register, "/register")
    api.add_resource(List, "/servers")
    api.add_resource(ListSingle, "/servers/<string:server_id>")
    # api.add_resource(Generate, "/generate")
    api.add_resource(Usage, "/usage")
    api.add_resource(Contributions, "/contributions")
    api.add_resource(AsyncGenerate, "/generate/prompt")
    api.add_resource(AsyncGeneratePrompt, "/generate/prompt/<string:id>")
    api.add_resource(PromptPop, "/generate/pop")
    api.add_resource(SubmitGeneration, "/generate/submit")
    UsageStore()
    # api.add_resource(Register, "/register")
    from waitress import serve
    serve(REST_API, host="0.0.0.0", port="5001")
    # REST_API.run(debug=True,host="0.0.0.0",port="5001")
