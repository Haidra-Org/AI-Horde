from flask import Flask
from flask_restful import Resource, reqparse, Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging, requests, random, time
from enum import Enum
from markdown import markdown
from server_classes import WaitingPrompt,ProcessingGeneration,KAIServer,Database,PromptsIndex,GenerationsIndex


class ServerErrors(Enum):
    WRONG_CREDENTIALS = 0
    INVALID_PROCGEN = 1
    DUPLICATE_GEN = 2
    TOO_MANY_PROMPTS = 3
    EMPTY_USERNAME = 4
    EMPTY_PROMPT = 5

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


@REST_API.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization"
    return response


class Usage(Resource):
    def get(self):
        return(_db.usage,200)


class Contributions(Resource):
    def get(self):
        return(_db.contributions,200)


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
        wp_count = _waiting_prompts.count_waiting_requests(args.username)
        if wp_count >= 3:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = args['username'], wp_count = wp_count)}",503)
        wp = WaitingPrompt(
            _db,
            _waiting_prompts,
            _processing_generations,
            args["prompt"],
            args["username"],
            args["models"],
            args["params"],
            servers=args["servers"],
            softprompts=args["softprompts"],

        )
        server_found = False
        for s in _db.servers:
            if len(args.servers) and servers[s].id not in args.servers:
                continue
            if _db.servers[s].can_generate(wp)[0]:
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
        wp = _waiting_prompts.get_item(id)
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
        wp_count = _waiting_prompts.count_waiting_requests(args.username)
        if args['username'] == '':
            return(f"{get_error(ServerErrors.EMPTY_USERNAME)}",400)
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = args['username'])}",400)
        if wp_count >= 3:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = args['username'], wp_count = wp_count)}",503)
        wp = WaitingPrompt(
            _db,
            _waiting_prompts,
            _processing_generations,
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
        server = _db.servers.get(args['name'])
        if not server:
            server = KAIServer(_db, args['username'], args['name'], args['password'], args["softprompts"])
        if args['password'] != server.password:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = args['username'])}",401)
        server.check_in(args['model'], args['max_length'], args['max_content_length'], args["softprompts"])
        # This ensures that the priority requested by the bridge is respected
        prioritized_wp = []
        for priority_username in args.priority_usernames:
            for wp in _waiting_prompts.get_all():
                if wp.username == priority_username:
                    prioritized_wp.append(wp)
        for wp in _waiting_prompts.get_all():
            if wp not in prioritized_wp:
                prioritized_wp.append(wp)
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
        procgen = _processing_generations.get_item(args['id'])
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
        return(_db.get_available_models(),200)


class List(Resource):
    def get(self):
        servers_ret = []
        for s in _db.servers:
            if _db.servers[s].is_stale():
                continue
            sdict = {
                "name": _db.servers[s].name,
                "id": _db.servers[s].id,
                "model": _db.servers[s].model,
                "max_length": _db.servers[s].max_length,
                "max_content_length": _db.servers[s].max_content_length,
                "tokens_generated": _db.servers[s].contributions,
                "requests_fulfilled": _db.servers[s].fulfilments,
                "performance": _db.servers[s].get_performance(),
                "uptime": _db.servers[s].uptime,
            }
            servers_ret.append(sdict)
        return(servers_ret,200)

class ListSingle(Resource):
    def get(self, server_id):
        server = None
        for s in _db.servers:
            if _db.servers[s].id == server_id:
                server = _db.servers[s]
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



@REST_API.route('/')
def index():
    with open('index.md') as index_file:
        index = index_file.read()
    top_contributor = _db.get_top_contributor()
    top_server = _db.get_top_server()
    align_image = random.randint(1, 6)
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 6)
    if not top_contributor or not top_server:
        top_contributors = f'\n<img src="https://github.com/db0/KoboldAI-Horde/blob/master/img/{big_image}.jpg?raw=true" width="800" />'
    else:
        top_contributors = f"""\n## Top Contributors
These are the people and servers who have contributed most to this horde.
### Users
This is the person whose server(s) have generated the most tokens for the horde.
#### {top_contributor['username']}
* {top_contributor['tokens']} tokens generated.
* {top_contributor['requests']} requests fulfiled.
### Servers
This is the server which has generated the most tokens for the horde.
#### {top_server.name}
* {top_server.contributions} tokens generated.
* {top_server.fulfilments} request fulfilments.
* {top_server.get_human_readable_uptime()} uptime.

<img src="https://github.com/db0/KoboldAI-Horde/blob/master/img/{big_image}.jpg?raw=true" width="800" />
"""
    totals = _db.get_total_usage()
    findex = index.format(
        kobold_image = align_image, 
        avg_performance= _db.get_request_avg(), 
        total_tokens = totals["tokens"], 
        total_fulfillments = totals["fulfilments"],
        total_queue = _waiting_prompts.count_total_waiting_generations(),
    )
    return(markdown(findex + top_contributors))


if __name__ == "__main__":
    global _db
    global _waiting_prompts
    global _processing_generations
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.DEBUG)
    _db = Database()
    _waiting_prompts = PromptsIndex()
    _processing_generations = GenerationsIndex()
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
    from waitress import serve
    serve(REST_API, host="0.0.0.0", port="5001")
    # REST_API.run(debug=True,host="0.0.0.0",port="5001")
