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
from markdown import markdown
import random


class ServerErrors(Enum):
    WRONG_CREDENTIALS = 0
    INVALID_PROCGEN = 1
    DUPLICATE_GEN = 2
    TOO_MANY_PROMPTS = 3
    EMPTY_USERNAME = 4
    EMPTY_PROMPT = 5

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
    if error == ServerErrors.WRONG_CREDENTIALS:
        logging.warning(f'User "{kwargs["username"]}" sent wrong credentials for utilizing instance {kwargs["kai_instance"]}')
        return(f'wrong credentials for utilizing instance {kwargs["kai_instance"]}')
    if error == ServerErrors.INVALID_PROCGEN:
        logging.warning(f'Server attempted to provide generation for {kwargs["id"]} but it did not exist')
        return(f'Processing Generation with ID {kwargs["id"]} does not exist')
    if error == ServerErrors.DUPLICATE_GEN:
        logging.warning(f'Server attempted to provide duplicate generation for {kwargs["id"]} ')
        return(f'Processing Generation with ID {kwargs["id"]} already submitted')
    if error == ServerErrors.TOO_MANY_PROMPTS:
        logging.warning(f'User "{kwargs["username"]}" has already requested too many parallel prompts ({kwargs["wp_count"]}). Aborting!')
        return("Too many parallel requests from same user. Please try again later.")
    if error == ServerErrors.EMPTY_USERNAME:
        logging.warning(f'Request sent with an invalid username. Aborting!')
        return("Please provide a valid username.")
    if error == ServerErrors.EMPTY_PROMPT:
        logging.warning(f'User "{kwargs["username"]}" sent an empty prompt. Aborting!')
        return("You cannot specify an empty prompt.")

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


def get_available_models():
    models_ret = {}
    for s in servers:
        if servers[s].is_stale():
            continue
        models_ret[servers[s].model] = models_ret.get(servers[s].model,0) + 1
    return(models_ret)

def count_waiting_requests(username):
    count = 0
    for wp in waiting_prompts:
        if waiting_prompts[wp].username == username and not waiting_prompts[wp].is_completed():
            count += 1
    return(count)

@REST_API.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization"
    return response



class Usage(Resource):
    def get(self):
        return(usage,200)


class Contributions(Resource):
    def get(self):
        return(contributions,200)


class SyncGenerate(Resource):
    decorators = [limiter.limit("10/minute")]
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
        parser.add_argument("username", type=str, required=True, help="Username to track usage")
        parser.add_argument("models", type=str, action='append', required=False, default=[], help="The acceptable models with which to generate")
        parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the KoboldAI server")
        parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")
        parser.add_argument("softprompts", type=str, action='append', required=False, default=[''], help="If specified, only servers who can load this softprompt will generate this request")
        # Not implemented yet
        parser.add_argument("world_info", type=str, required=False, help="If specified, only servers who can load this this world info will generate this request")
        args = parser.parse_args()
        if args['username'] == '':
            return(f"{get_error(ServerErrors.EMPTY_USERNAME)}",400)
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = args['username'])}",400)
        wp_count = count_waiting_requests(args.username)
        if wp_count >= 3:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = args['username'], wp_count = wp_count)}",503)
        wp = WaitingPrompt(
            args["prompt"],
            args["username"],
            args["models"],
            args["params"],
            servers=args["servers"],
            softprompts=args["softprompts"],

        )
        server_found = False
        for s in servers:
            if len(args.servers) and servers[s].id not in args.servers:
                continue
            if servers[s].can_generate(wp)[0]:
                server_found = True
                break
        if not server_found:
            del wp # Normally garbage collection will handle it, but doesn't hurt to be thorough
            return("No active server found to fulfil this request. Please Try again later...", 503)
        # if a server is available to fulfil this prompt, we activate it and add it to the queue to be generated
        wp.activate()
        while True:
            time.sleep(1)
            if wp.is_stale():
                return("Prompt Request Expired", 500)
            if wp.is_completed():
                break
        return(wp.get_status()['generations'], 200)


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
        parser.add_argument("models", type=str, action='append', required=False, default=[], help="The acceptable models with which to generate")
        parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the KoboldAI server")
        parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")
        parser.add_argument("softprompts", action='append', required=False, default=[''], help="If specified, only servers who can load this softprompt will generate this request")
        args = parser.parse_args()
        wp_count = count_waiting_requests(args.username)
        if args['username'] == '':
            return(f"{get_error(ServerErrors.EMPTY_USERNAME)}",400)
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = args['username'])}",400)
        if wp_count >= 3:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = args['username'], wp_count = wp_count)}",503)
        wp = WaitingPrompt(
            args["prompt"],
            args["username"],
            args["models"],
            args["params"],
            servers=args["servers"],
            softprompts=args["softprompts"],

        )
        wp.activate()
        return({"id":wp.id}, 200)


class PromptPop(Resource):
    decorators = [limiter.limit("2/second")]
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("username", type=str, required=True, help="Username to track contributions")
        parser.add_argument("password", type=str, required=True, help="Password to authenticate with")
        parser.add_argument("name", type=str, required=True, help="The server's unique name, to track contributions")
        parser.add_argument("model", type=str, required=True, help="The model currently running on this KoboldAI")
        parser.add_argument("max_length", type=int, required=False, default=512, help="The maximum amount of tokens this server can generate")
        parser.add_argument("max_content_length", type=int, required=False, default=2048, help="The max amount of context to submit to this AI for sampling.")
        parser.add_argument("priority_usernames", type=str, action='append', required=False, default=[], help="The usernames which get priority use on this server")
        parser.add_argument("softprompts", type=str, action='append', required=False, default=[], help="The available softprompt files on this cluster for the currently running model")
        args = parser.parse_args()
        skipped = {}
        server = servers.get(args['name'])
        if not server:
            server = KAIServer(args['username'], args['name'], args['password'], args["softprompts"])
        if args['password'] != server.password:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = args['username'])}",401)
        server.check_in(args['model'], args['max_length'], args['max_content_length'], args["softprompts"])
        # This ensures that the priority requested by the bridge is respected
        prioritized_wp = []
        for priority_username in args.priority_usernames:
            for wp_id in waiting_prompts:
                if waiting_prompts[wp_id].username == priority_username:
                    prioritized_wp.append(waiting_prompts[wp_id])
        for wp_id in waiting_prompts:
            if waiting_prompts[wp_id] not in prioritized_wp:
                prioritized_wp.append(waiting_prompts[wp_id])
        for wp in prioritized_wp:
            if not wp.needs_gen():
                continue
            check_gen = server.can_generate(wp)
            if not check_gen[0]:
                skipped_reason = check_gen[1]
                skipped[skipped_reason] = skipped.get(skipped_reason,0) + 1
                continue
            matching_softprompt = False
            for sp in wp.softprompts:
                # If a None softprompts has been provided, we always match, since we can always remove the softprompt
                if sp == '':
                    matching_softprompt = sp
                for sp_name in args['softprompts']:
                    # logging.info([sp_name,sp,sp in sp_name])
                    if sp in sp_name: # We do a very basic string matching. Don't think we need to do regex
                        matching_softprompt = sp_name
                        break
                if matching_softprompt:
                    break
            ret = wp.start_generation(server, matching_softprompt)
            return(ret, 200)
        return({"id": None, "skipped": skipped}, 200)


class SubmitGeneration(Resource):
    def post(self):
        parser = reqparse.RequestParser()
        parser.add_argument("id", type=str, required=True, help="The processing generation uuid")
        parser.add_argument("password", type=str, required=True, help="The server password")
        parser.add_argument("generation", type=str, required=False, default=[], help="The generated text")
        args = parser.parse_args()
        procgen = processing_generations.get(args['id'])
        if not procgen:
            return(f"{get_error(ServerErrors.INVALID_PROCGEN,id = args['id'])}",404)
        if args['password'] != procgen.server.password:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = procgen.server.name, username = procgen.server.username)}",401)
        tokens = procgen.set_generation(args['generation'])
        if tokens == 0:
            return(f"{get_error(ServerErrors.DUPLICATE_GEN,id = args['id'])}",400)
        return({"reward": tokens}, 200)

class Models(Resource):
    def get(self):
        return(get_available_models(),200)


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
                "performance": servers[s].get_performance(),
                "uptime": servers[s].uptime,
            }
            servers_ret.append(sdict)
        return(servers_ret,200)

class ListSingle(Resource):
    def get(self, server_id):
        server = None
        for s in servers:
            if servers[s].id == server_id:
                server = servers[s]
        if server:
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
        else:
            return("Not found", 404)


class WaitingPrompt:
    # Every 10 secs we store usage data to disk
    def __init__(self, prompt, username, models, params, **kwargs):
        self.prompt = prompt
        self.username = username
        self.models = models
        self.params = params
        self.n = params.get('n', 1)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logging.warning(f"User {self.username} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
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
        self.last_process_time = datetime.now()
        self.servers = kwargs.get("servers", [])
        self.softprompts = kwargs.get("softprompts", [''])
        # Prompt requests are removed after 10 mins of inactivity, to prevent memory usage
        self.stale_time = 600


    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid server for it
        # Before we add it to the queue
        waiting_prompts[self.id] = self
        logging.info(f"New prompt request by user: {self.username}")
        thread = threading.Thread(target=self.check_for_stale, args=())
        thread.daemon = True
        thread.start()

    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, server, matching_softprompt):
        if self.n <= 0:
            return
        new_gen = ProcessingGeneration(self, server)
        self.processing_gens.append(new_gen)
        self.n -= 1
        self.refresh()
        prompt_payload = {
            "payload": self.gen_payload,
            "softprompt": matching_softprompt,
            "id": new_gen.id,
        }
        return(prompt_payload)

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
        if self.username not in usage:
            usage[self.username] = 0
            logging.info(f'New user requested generation: {self.username}')
        if self.username not in usage:
            usage[self.username] = {"tokens":0, "requests": 0}
        # Conver all format. Will remove this eventually
        elif type(usage[self.username]) is not dict:
            old_tokens = usage[self.username]
            usage[self.username] = {"tokens":old_tokens, "requests": 0}
        usage[self.username]["tokens"] += self.tokens
        usage[self.username]["requests"] += 1
        self.refresh()

    def check_for_stale(self):
        while True:
            if self.is_stale():
                self.delete()
                break
            time.sleep(10)

    def delete(self):
        for gen in self.processing_gens:
            gen.delete()
        del waiting_prompts[self.id]
        del self

    def refresh(self):
        self.last_process_time = datetime.now()

    def is_stale(self):
        if (datetime.now() - self.last_process_time).seconds > self.stale_time:
            return(True)
        return(False)

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
        logging.info(f"New Generation delivered by server: {self.server.name}")
        return(tokens)

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def delete(self):
        del processing_generations[self.id]
        del self


class KAIServer:
    def __init__(self, username = None, name = None, password = None, softprompts = []):
        self.username = username
        self.password = password
        self.name = name
        self.softprompts = softprompts
        self.contributions = 0
        self.fulfilments = 0
        self.performances = []
        self.uptime = 0
        self.id = str(uuid4())
        if name:
            servers[self.name] = self
            logging.info(f'New server checked-in: {name} by {username}')

    def check_in(self, model, max_length, max_content_length, softprompts):
        if not self.is_stale():
            self.uptime += (datetime.now() - self.last_check_in).seconds
        self.last_check_in = datetime.now()
        self.model = model
        self.max_content_length = max_content_length
        self.max_length = max_length
        self.softprompts = softprompts

    def get_human_readable_uptime(self):
        if self.uptime < 60:
            return(f"{self.uptime} seconds")
        elif self.uptime < 60*60:
            return(f"{round(self.uptime/60,2)} minutes")
        elif self.uptime < 60*60*24:
            return(f"{round(self.uptime/60/60,2)} hours")
        else:
            return(f"{round(self.uptime/60/60/24,2)} days")

    def can_generate(self, waiting_prompt):
        # takes as an argument a WaitingPrompt class and checks if this server is valid for generating it
        is_matching = True
        skipped_reason = None
        if len(waiting_prompt.servers) >= 1 and self.id not in waiting_prompt.servers:
            is_matching = False
            skipped_reason = 'server_id'
        if len(waiting_prompt.models) >= 1 and self.model not in waiting_prompt.models:
            is_matching = False
            skipped_reason = 'models'
        if self.max_content_length < waiting_prompt.max_content_length:
            is_matching = False
            skipped_reason = 'max_content_length'
        if self.max_length < waiting_prompt.max_length:
            is_matching = False
            skipped_reason = 'max_length'
        matching_softprompt = False
        for sp in waiting_prompt.softprompts:
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if sp == '':
                matching_softprompt = True
                break
            for sp_name in self.softprompts:
                if sp in sp_name: 
                    matching_softprompt = True
                    break
        if not matching_softprompt:
            is_matching = False
            skipped_reason = 'matching_softprompt'
        return([is_matching,skipped_reason])

    def record_contribution(self, tokens, seconds_taken):
        contributions[self.username] = contributions.get(self.username,0) + tokens
        self.contributions += tokens
        self.fulfilments += 1
        self.performances.append(round(tokens / seconds_taken))
        if len(self.performances) > 20:
            del self.performances[0]

    def get_performance(self):
        if len(self.performances):
            ret_str = f'{round(sum(self.performances) / len(self.performances),2)} tokens per second'
        else:
            ret_str = f'No requests fulfiled yet'
        return(ret_str)

    def is_stale(self):
        try:
            if (datetime.now() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new server, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    def serialize(self):
        ret_dict = {
            "username": self.username,
            "password": self.password,
            "name": self.name,
            "model": self.model,
            "max_length": self.max_length,
            "max_content_length": self.max_content_length,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "performances": self.performances,
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "softprompts": self.softprompts,
            "uptime": self.uptime,
        }
        return(ret_dict)

    def deserialize(self, saved_dict):
        self.username = saved_dict["username"]
        self.password = saved_dict["password"]
        self.name = saved_dict["name"]
        self.model = saved_dict["model"]
        self.max_length = saved_dict["max_length"]
        self.max_content_length = saved_dict["max_content_length"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.softprompts = saved_dict.get("softprompts",[])
        self.uptime = saved_dict.get("uptime",0)
        servers[self.name] = self

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

@REST_API.route('/')
def index():
    with open('index.md') as index_file:
        index = index_file.read()
    top_contributor = 'N/A'
    top_contribution = 0
    for user in contributions:
        if contributions[user] > top_contribution:
            top_contributor = user
            top_contribution = contributions[user]
        elif contributions[user] == top_contribution:
            top_contributor += ', ' + user
    top_server = None
    top_server_contribution = 0
    for server in servers:
        if servers[server].contributions > top_server_contribution:
            top_server = servers[server]
            top_server_contribution = servers[server].contributions
    align_image = random.randint(1, 6)
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 6)
    top_contributors = f"""\n## Top Contributors
These are the people and servers who have contributed most to this horde.
### Users
This is the person whose server(s) have generated the most tokens for the horde.

#### {top_contributor}
* {top_contribution} tokens generated.
### Servers
This is the server which has generated the most tokens for the horde.
#### {top_server.name}
* {top_server_contribution} tokens generated.
* {top_server.fulfilments} request fulfilments.
* {top_server.get_human_readable_uptime()} uptime.

<img src="https://github.com/db0/KoboldAI-Horde/blob/master/img/{big_image}.jpg?raw=true" width="800" />
"""
    return(markdown(index.format(kobold_image = align_image) + top_contributors))

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

    api.add_resource(SyncGenerate, "/generate/sync")
    api.add_resource(AsyncGenerate, "/generate/async")
    api.add_resource(AsyncGeneratePrompt, "/generate/prompt/<string:id>")
    api.add_resource(PromptPop, "/generate/pop")
    api.add_resource(SubmitGeneration, "/generate/submit")
    api.add_resource(Usage, "/usage")
    api.add_resource(Contributions, "/contributions")
    api.add_resource(List, "/servers")
    api.add_resource(Models, "/models")
    api.add_resource(ListSingle, "/servers/<string:server_id>")
    UsageStore()
    # api.add_resource(Register, "/register")
    from waitress import serve
    serve(REST_API, host="0.0.0.0", port="5001")
    # REST_API.run(debug=True,host="0.0.0.0",port="5001")
