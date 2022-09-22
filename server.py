from flask import Flask, render_template, redirect, url_for, request, abort, Blueprint
from flask_restx import Resource, reqparse, Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_dance.contrib.google import make_google_blueprint, google
from flask_dance.contrib.discord import make_discord_blueprint, discord
from flask_dance.contrib.github import make_github_blueprint, github
import requests, random, time, os, oauthlib, secrets, argparse, logging
from enum import Enum
from markdown import markdown
from dotenv import load_dotenv
from uuid import uuid4
from werkzeug.middleware.proxy_fix import ProxyFix
from server_classes import WaitingPrompt,ProcessingGeneration,KAIServer,PromptsIndex,GenerationsIndex,User,Database
from logger import logger, set_logger_verbosity, quiesce_logger

class ServerErrors(Enum):
    WRONG_CREDENTIALS = 0
    INVALID_PROCGEN = 1
    DUPLICATE_GEN = 2
    TOO_MANY_PROMPTS = 3
    EMPTY_PROMPT = 4
    INVALID_API_KEY = 5
    INVALID_SIZE = 6
    NO_PROXY = 7
    TOO_MANY_STEPS = 8
    NOT_ADMIN = 9
    MAINTENANCE_MODE = 10
    NOT_OWNER = 11

REST_API = Flask(__name__)
REST_API.wsgi_app = ProxyFix(REST_API.wsgi_app, x_for=1)
blueprint = Blueprint('api', __name__, url_prefix='/api/v1')
api = Api(blueprint,
    version='1.0', 
    title='Stable Horde',
    description='The API documentation for the Stable Horde',
    contact_email="mail@dbzer0.com",
    default="v1",
    default_label="Latest Version",
    ordered=True,
)
REST_API.register_blueprint(blueprint)

# Very basic DOS prevention
try:
    limiter = Limiter(
        REST_API,
        key_func=get_remote_address,
        storage_uri="redis://localhost:6379/1",
        # storage_options={"connect_timeout": 30},
        strategy="fixed-window", # or "moving-window"
        default_limits=["90 per minute"]
    )
# Allow local workatation run
except:
    limiter = Limiter(
        REST_API,
        key_func=get_remote_address,
        default_limits=["90 per minute"]
    )

dance_return_to = '/'
maintenance_mode = False
allow_direct_connections = False
load_dotenv()

@logger.catch
def get_error(error, **kwargs):
    if error == ServerErrors.INVALID_API_KEY:
        logger.warning(f'Invalid API Key sent for {kwargs["subject"]}.')
        return(f'No user matching sent API Key. Have you remembered to register at https://stablehorde.net/register ?')
    if error == ServerErrors.WRONG_CREDENTIALS:
        logger.warning(f'User "{kwargs["username"]}" sent wrong credentials for utilizing instance {kwargs["kai_instance"]}')
        return(f'wrong credentials for utilizing instance {kwargs["kai_instance"]}')
    if error == ServerErrors.INVALID_PROCGEN:
        logger.warning(f'Server attempted to provide generation for {kwargs["id"]} but it did not exist')
        return(f'Processing Generation with ID {kwargs["id"]} does not exist')
    if error == ServerErrors.DUPLICATE_GEN:
        logger.warning(f'Server attempted to provide duplicate generation for {kwargs["id"]} ')
        return(f'Processing Generation with ID {kwargs["id"]} already submitted')
    if error == ServerErrors.TOO_MANY_PROMPTS:
        logger.warning(f'User "{kwargs["username"]}" has already requested too many parallel requests ({kwargs["wp_count"]}). Aborting!')
        return(f"Parallel requests exceeded user limit ({kwargs['wp_count']}). Please try again later or request to increase your concurrency.")
    if error == ServerErrors.EMPTY_PROMPT:
        logger.warning(f'User "{kwargs["username"]}" sent an empty prompt. Aborting!')
        return("You cannot specify an empty prompt.")
    if error == ServerErrors.INVALID_SIZE:
        logger.warning(f'User "{kwargs["username"]}" sent an invalid size. Aborting!')
        return("Invalid size. The image dimentions have to be multiples of 64.")
    if error == ServerErrors.TOO_MANY_STEPS:
        logger.warning(f'User "{kwargs["username"]}" sent too many steps ({kwargs["steps"]}). Aborting!')
        return("Too many sampling steps. To allow resources for everyone, we allow only up to 100 steps.")
    if error == ServerErrors.NO_PROXY:
        logger.warning(f'Attempt to access outside reverse proxy')
        return(f'Access allowed only through https')
    if error == ServerErrors.NOT_ADMIN:
        logger.warning(f'Non-admin user "{kwargs["username"]}" tried to use admin endpoint: "{kwargs["endpoint"]}". Aborting!')
        return("You're not an admin. Sod off!")
    if error == ServerErrors.MAINTENANCE_MODE:
        logger.info(f'Rejecting endpoint "{kwargs["endpoint"]}" because server in maintenance mode.')
        return("Server has enterred maintenance mode. Please try again later.")
    if error == ServerErrors.NOT_OWNER:
        logger.warning(f'User "{kwargs["username"]}" tried to modify server they do not own: "{kwargs["server_name"]}". Aborting!')
        return("You're not the owner of this server!")

@REST_API.before_request
def limit_remote_addr():
    logger.debug(request.remote_addr)
    # if not allow_direct_connections and request.remote_addr != '127.0.0.1':
    #     error_msg = get_error(ServerErrors.NO_PROXY)
    #     abort(403, error_msg)


@REST_API.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization"
    return response


class SyncGenerate(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
    parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
    parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the SD server")
    parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")
    @api.expect(parser)
    def post(self):
        args = self.parser.parse_args()
        username = 'Anonymous'
        user = None
        if maintenance_mode:
            return(f"{get_error(ServerErrors.MAINTENANCE_MODE, endpoint = 'SyncGenerate')}",503)
        if args.api_key:
            user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}",401)
        username = user.get_unique_alias()
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = username)}",400)
        wp_count = _waiting_prompts.count_waiting_requests(user)
        if wp_count >= user.concurrency:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = username, wp_count = wp_count)}",503)
        if args["params"].get("length",512)%64:
            return(f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400)
        if args["params"].get("width",512)%64:
            return(f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400)
        if args["params"].get("steps",50) > 100:
            return(f"{get_error(ServerErrors.TOO_MANY_STEPS, username = username, steps = args['params']['steps'])}",400)
        wp = WaitingPrompt(
            _db,
            _waiting_prompts,
            _processing_generations,
            args["prompt"],
            user,
            args["params"],
            servers=args["servers"],
        )
        server_found = False
        for server in _db.servers.values():
            if len(args.servers) and server.id not in args.servers:
                continue
            if server.can_generate(wp)[0]:
                server_found = True
                break
        if not server_found:
            del wp # Normally garbage collection will handle it, but doesn't hurt to be thorough
            return("No active server found to fulfill this request. Please Try again later...", 503)
        # if a server is available to fulfil this prompt, we activate it and add it to the queue to be generated
        wp.activate()
        while True:
            time.sleep(1)
            if wp.is_stale():
                return("Prompt Request Expired", 500)
            if wp.is_completed():
                break
        ret_dict = wp.get_status()['generations']
        # We delete it from memory immediately to ensure we don't run out
        wp.delete()
        return(ret_dict, 200)


class AsyncGeneratePrompt(Resource):
    @limiter.limit("3/minute")
    @logger.catch
    def get(self, id = ''):
        wp = _waiting_prompts.get_item(id)
        if not wp:
            return("ID not found", 404)
        wp_status = wp.get_status()
        # If the status is retrieved after the wp is done we clear it to free the ram
        if wp_status["done"]:
            wp.delete()
        return(wp_status, 200)


class AsyncCheck(Resource):
    # Increasing this until I can figure out how to pass original IP from reverse proxy
    @limiter.limit("10/second")
    @logger.catch
    def get(self, id = ''):
        wp = _waiting_prompts.get_item(id)
        if not wp:
            return("ID not found", 404)
        return(wp.get_lite_status(), 200)


class AsyncGenerate(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
    parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
    parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the SD server")
    parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")

    @api.expect(parser)
    def post(self):
        args = self.parser.parse_args()
        username = 'Anonymous'
        user = None
        if maintenance_mode:
            return(f"{get_error(ServerErrors.MAINTENANCE_MODE, endpoint = 'AsyncGenerate')}",503)
        if args.api_key:
            user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}",401)
        username = user.get_unique_alias()
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = username)}",400)
        wp_count = _waiting_prompts.count_waiting_requests(user)
        if wp_count >= user.concurrency:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = username, wp_count = wp_count)}",503)
        if args["params"].get("length",512)%64:
            return(f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400)
        if args["params"].get("width",512)%64:
            return(f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400)
        if args["params"].get("steps",50) > 100:
            return(f"{get_error(ServerErrors.TOO_MANY_STEPS, username = username, steps = args['params']['steps'])}",400)
        wp = WaitingPrompt(
            _db,
            _waiting_prompts,
            _processing_generations,
            args["prompt"],
            user,
            args["params"],
            servers=args["servers"],
        )
        server_found = False
        for server in _db.servers.values():
            if len(args.servers) and server.id not in args.servers:
                continue
            if server.can_generate(wp)[0]:
                server_found = True
                break
        if not server_found:
            del wp # Normally garbage collection will handle it, but doesn't hurt to be thorough
            return("No active server found to fulfill this request. Please Try again later...", 503)
        # if a server is available to fulfil this prompt, we activate it and add it to the queue to be generated
        wp.activate()
        return({"id":wp.id}, 200)


class PromptPop(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
    parser.add_argument("name", type=str, required=True, help="The server's unique name, to track contributions")
    parser.add_argument("max_pixels", type=int, required=False, default=512, help="The maximum amount of pixels this server can generate")
    parser.add_argument("priority_usernames", type=str, action='append', required=False, default=[], help="The usernames which get priority use on this server")

    @api.expect(parser)
    @limiter.limit("45/second")
    def post(self):
        args = self.parser.parse_args()
        skipped = {}
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'server promptpop: ' + args['name'])}",401)
        server = _db.find_server_by_name(args['name'])
        if not server:
            server = KAIServer(_db)
            server.create(user, args['name'])
        if user != server.user:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = user.get_unique_alias())}",401)
        server.check_in(args['max_pixels'])
        if server.maintenance:
            return(f"Server has been put into maintenance mode by the owner",403)
        if server.paused:
            return({"id": None, "skipped": {}},200)
        # This ensures that the priority requested by the bridge is respected
        prioritized_wp = []
        priority_users = [user]
        ## Start prioritize by bridge request ##
        for priority_username in args.priority_usernames:
            priority_user = _db.find_user_by_username(priority_username)
            if priority_user:
                priority_users.append(priority_user)
        for priority_user in priority_users:
            for wp in _waiting_prompts.get_all():
                if wp.user == priority_user and wp.needs_gen():
                    prioritized_wp.append(wp)
        ## End prioritize by bridge request ##
        for wp in _waiting_prompts.get_waiting_wp_by_kudos():
            if wp not in prioritized_wp:
                prioritized_wp.append(wp)
        for wp in prioritized_wp:
            check_gen = server.can_generate(wp)
            if not check_gen[0]:
                skipped_reason = check_gen[1]
                skipped[skipped_reason] = skipped.get(skipped_reason,0) + 1
                continue
            ret = wp.start_generation(server)
            return(ret, 200)
        return({"id": None, "skipped": skipped}, 200)


class SubmitGeneration(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("id", type=str, required=True, help="The processing generation uuid")
    parser.add_argument("api_key", type=str, required=True, help="The server's owner API key")
    parser.add_argument("generation", type=str, required=False, default=[], help="The download location of the image")
    parser.add_argument("seed", type=str, required=True, default=[], help="The seed of the generated image")

    @api.expect(parser)
    def post(self):
        args = self.parser.parse_args()
        procgen = _processing_generations.get_item(args['id'])
        if not procgen:
            return(f"{get_error(ServerErrors.INVALID_PROCGEN,id = args['id'])}",404)
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'server submit: ' + args['name'])}",401)
        if user != procgen.server.user:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = user.get_unique_alias())}",401)
        kudos = procgen.set_generation(args['generation'], args['seed'])
        if kudos == 0:
            return(f"{get_error(ServerErrors.DUPLICATE_GEN,id = args['id'])}",400)
        return({"reward": kudos}, 200)

class TransferKudos(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos")
    parser.add_argument("api_key", type=str, required=True, help="The sending user's API key")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to transfer")

    @api.expect(parser)
    def post(self):
        args = self.parser.parse_args()
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'kudos transfer to: ' + args['username'])}",401)
        ret = _db.transfer_kudos_from_apikey_to_username(args['api_key'],args['username'],args['amount'])
        kudos = ret[0]
        error = ret[1]
        if error != 'OK':
            return(f"{error}",400)
        return({"transfered": kudos}, 200)

class AdminMaintenanceMode(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("api_key", type=str, required=True, help="The Admin API key")
    parser.add_argument("active", type=bool, required=True, help="Star or stop maintenance mode")

    @limiter.limit("30/minute")
    @api.expect(parser)
    def put(self):
        global maintenance_mode
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'Admin action: ' + 'AdminMaintenanceMode')}",401)
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            return(f"{get_error(ServerErrors.NOT_ADMIN, username = admin.get_unique_alias(), endpoint = 'AdminMaintenanceMode')}",401)
        maintenance_mode = args['active']
        return({"maintenance_mode": maintenance_mode}, 200)

class Servers(Resource):
    @logger.catch
    def get(self):
        servers_ret = []
        for server in _db.servers.values():
            if server.is_stale():
                continue
            sdict = {
                "name": server.name,
                "id": server.id,
                "max_pixels": server.max_pixels,
                "megapixelsteps_generated": server.contributions,
                "requests_fulfilled": server.fulfilments,
                "kudos_rewards": server.kudos,
                "kudos_details": server.kudos_details,
                "performance": server.get_performance(),
                "uptime": server.uptime,
                "maintenance_mode": server.maintenance,
            }
            servers_ret.append(sdict)
        return(servers_ret,200)

class ServerSingle(Resource):
    @logger.catch
    def get(self, server_id = ''):
        server = _db.find_server_by_id(server_id)
        if server:
            sdict = {
                "name": server.name,
                "id": server.id,
                "max_pixels": server.max_pixels,
                "megapixelsteps_generated": server.contributions,
                "requests_fulfilled": server.fulfilments,
                "latest_performance": server.get_performance(),
                "maintenance_mode": server.maintenance,
            }
            return(sdict,200)
        else:
            return("Not found", 404)

    parser = reqparse.RequestParser()
    parser.add_argument("api_key", type=str, required=True, help="The Admin or server owner API key")
    parser.add_argument("maintenance", type=bool, required=False, help="Set to true to put this server into maintenance.")
    parser.add_argument("paused", type=bool, required=False, help="Set to true to pause this server.")

    @limiter.limit("30/minute")
    @api.expect(parser)
    def put(self, server_id = ''):
        server = _db.find_server_by_id(server_id)
        if not server:
            return("Invalid Server ID", 404)
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'User action: ' + 'PUT ServerSingle')}",401)
        ret_dict = {}
        # Both admins and owners can set the server to maintenance
        if args.maintenance != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
                if admin != server.user:
                    return(f"{get_error(ServerErrors.NOT_OWNER, username = admin.get_unique_alias(), server_name = server.name)}",401)
            server.maintenance = args.maintenance
            ret_dict["maintenance"] = server.maintenance
        # Only admins can set a server as paused
        if args.paused != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
                return(f"{get_error(ServerErrors.NOT_ADMIN, username = admin.get_unique_alias(), endpoint = 'AdminModifyServer')}",401)
            server.paused = args.paused
            ret_dict["paused"] = server.paused
        if not len(ret_dict):
            return("No server modification selected!", 400)
        return(ret_dict, 200)

    parser = reqparse.RequestParser()
    parser.add_argument("api_key", type=str, required=True, help="The Admin or server owner API key")

    # post shows also hidden server info
    @limiter.limit("30/minute")
    def post(self, server_id = ''):
        server = _db.find_server_by_id(server_id)
        if not server:
            return("Invalid Server ID", 404)
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'User action: ' + 'PUT ServerSingle')}",401)
        sdict = {
            "name": server.name,
            "id": server.id,
            "max_pixels": server.max_pixels,
            "megapixelsteps_generated": server.contributions,
            "requests_fulfilled": server.fulfilments,
            "latest_performance": server.get_performance(),
            "maintenance": server.maintenance,
            "paused": server.paused,
            "owner": server.user.get_unique_alias(),
        }
        return(sdict,200)


class Users(Resource):
    @logger.catch
    def get(self):
        user_dict = {}
        for user in _db.users.values():
            user_dict[user.get_unique_alias()] = {
                "id": user.id,
                "kudos": user.kudos,
                "kudos_details": user.kudos_details,
                "usage": user.usage,
                "contributions": user.contributions,
                "concurrency": user.concurrency,
            }
        return(user_dict,200)


class UserSingle(Resource):
    @logger.catch
    def get(self, user_id = ''):
        logger.debug(user_id)
        user = _db.find_user_by_id(user_id)
        if user:
            udict = {
                "username": user.get_unique_alias(),
                "kudos": user.kudos,
                "usage": user.usage,
                "contributions": user.contributions,
                "concurrency": user.concurrency,
            }
            return(udict,200)
        else:
            return("Not found", 404)

    parser = reqparse.RequestParser()
    parser.add_argument("api_key", type=str, required=True, help="The Admin API key")
    parser.add_argument("kudos", type=int, required=False, help="The amount of kudos to modify (can be negative)")
    parser.add_argument("concurrency", type=int, required=False, help="The amount of concurrent request this user can have")
    parser.add_argument("usage_multiplier", type=float, required=False, help="The amount by which to multiply the users kudos consumption")

    @limiter.limit("30/minute")
    @api.expect(parser)
    def put(self, user_id = ''):
        user = user = _db.find_user_by_id(user_id)
        if not user:
            return(f"Invalid user_id: {user_id}",400)        
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'Admin action: ' + 'PUT UserSingle')}",401)
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            return(f"{get_error(ServerErrors.NOT_ADMIN, username = admin.get_unique_alias(), endpoint = 'AdminModifyUser')}",401)
        ret_dict = {}
        if args.kudos:
            user.modify_kudos(args.kudos, 'admin')
            ret_dict["new_kudos"] = user.kudos
        if args.concurrency:
            user.concurrency = args.concurrency
            ret_dict["concurrency"] = user.concurrency
        if args.usage_multiplier:
            user.usage_multiplier = args.usage_multiplier
            ret_dict["usage_multiplier"] = user.usage_multiplier
        if not len(ret_dict):
            return("No usermod operations selected!", 400)
        return(ret_dict, 200)


class HordeLoad(Resource):
    @logger.catch
    def get(self):
        load_dict = _waiting_prompts.count_totals()
        load_dict["megapixelsteps_per_min"] = _db.stats.get_megapixelsteps_per_min()
        load_dict["server_count"] = _db.count_active_servers()
        load_dict["maintenance_mode"] = maintenance_mode
        return(load_dict,200)

# Had to put this before the API definition, as otherwise it takes over /
# https://stackoverflow.com/questions/43632686/how-to-indicate-base-url-in-flask-restplus-documentation
@logger.catch
@limiter.limit("30/minute")
@REST_API.route('/')
def index():
    with open('index.md') as index_file:
        index = index_file.read()
    top_contributor = _db.get_top_contributor()
    top_server = _db.get_top_server()
    align_image = 0
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 5)
    if not top_contributor or not top_server:
        top_contributors = f'\n<img src="https://github.com/db0/Stable-Horde/blob/master/img/{big_image}.png?raw=true" width="800" />'
    else:
        top_contributors = f"""\n## Top Contributors
These are the people and servers who have contributed most to this horde.
### Users
This is the person whose server(s) have generated the most pixels for the horde.
#### {top_contributor.get_unique_alias()}
* {round(top_contributor.contributions['megapixelsteps'] / 1000,2)} Gigapixelsteps generated.
* {top_contributor.contributions['fulfillments']} requests fulfilled.
### Servers
This is the server which has generated the most pixels for the horde.
#### {top_server.name}
* {round(top_server.contributions/1000,2)} Gigapixelsteps generated.
* {top_server.fulfilments} request fulfillments.
* {top_server.get_human_readable_uptime()} uptime.
"""
    policies = """
## Policies

[Privacy Policy](/privacy)

[Terms of Service](/terms)"""
    totals = _db.get_total_usage()
    findex = index.format(
        stable_image = align_image,
        avg_performance= round(_db.stats.get_request_avg() / 1000000,2),
        total_pixels = round(totals["megapixelsteps"] / 1000,2),
        total_fulfillments = totals["fulfilments"],
        active_servers = _db.count_active_servers(),
        total_queue = _waiting_prompts.count_total_waiting_generations(),
        maintenance_mode = maintenance_mode,
    )
    head = """<head>
    <title>Stable Horde</title>
    <meta name="google-site-verification" content="pmLKyCEPKM5csKT9mW1ZbGLu2TX_wD0S5FCxWlmg_iI" />
    </head>
    """
    return(head + markdown(findex + top_contributors + policies))


@logger.catch
def get_oauth_id():
    google_data = None
    discord_data = None
    github_data = None
    authorized = False
    if google.authorized:
        google_user_info_endpoint = '/oauth2/v2/userinfo'
        try:
            google_data = google.get(google_user_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and discord.authorized:
        discord_info_endpoint = '/api/users/@me'
        try:
            discord_data = discord.get(discord_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and github.authorized:
        github_info_endpoint = '/user'
        try:
            github_data = github.get(github_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    oauth_id = None
    if google_data:
        oauth_id = f'g_{google_data["id"]}'
    elif discord_data:
        oauth_id = f'd_{discord_data["id"]}'
    elif github_data:
        oauth_id = f'gh_{github_data["id"]}'
    return(oauth_id)


@logger.catch
@REST_API.route('/register', methods=['GET', 'POST'])
def register():
    api_key = None
    user = None
    welcome = 'Welcome'
    username = ''
    pseudonymous = False
    oauth_id = get_oauth_id()
    if oauth_id:
        user = _db.find_user_by_oauth_id(oauth_id)
        if user:
            username = user.username
    if request.method == 'POST':
        api_key = secrets.token_urlsafe(16)
        if user:
            username = request.form['username']
            user.username = request.form['username']
            user.api_key = api_key
        else:
            # Triggered when the user created a username without logging in
            if not oauth_id:
                oauth_id = str(uuid4())
                pseudonymous = True
            user = User(_db)
            user.create(request.form['username'], oauth_id, api_key, None)
            username = request.form['username']
    if user:
        welcome = f"Welcome back {user.get_unique_alias()}"
    return render_template('register.html',
                           page_title="Join the Stable Horde!",
                           welcome=welcome,
                           user=user,
                           api_key=api_key,
                           username=username,
                           pseudonymous=pseudonymous,
                           oauth_id=oauth_id)


@logger.catch
@REST_API.route('/transfer', methods=['GET', 'POST'])
def transfer():
    src_api_key = None
    src_user = None
    dest_username = None
    kudos = None
    error = None
    welcome = 'Welcome'
    oauth_id = get_oauth_id()
    if oauth_id:
        src_user = _db.find_user_by_oauth_id(oauth_id)
        if not src_user:
            # This probably means the user was deleted
            oauth_id = None
    if request.method == 'POST':
        dest_username = request.form['username']
        amount = request.form['amount']
        if not amount.isnumeric():
            kudos = 0
            error = "Please enter a number in the kudos field"
        # Triggered when the user submited without logging in
        elif src_user:
            ret = _db.transfer_kudos_to_username(src_user,dest_username,int(amount))
            kudos = ret[0]
            error = ret[1]
        else:
            ret = _db.transfer_kudos_from_apikey_to_username(request.form['src_api_key'],dest_username,int(amount))
            kudos = ret[0]
            error = ret[1]
    if src_user:
        welcome = f"Welcome back {src_user.get_unique_alias()}. You have {src_user.kudos} kudos remaining"
    return render_template('transfer_kudos.html',
                           page_title="Kudos Transfer",
                           welcome=welcome,
                           kudos=kudos,
                           error=error,
                           dest_username=dest_username,
                           oauth_id=oauth_id)


@REST_API.route('/google/<return_to>')
def google_login(return_to):
    global dance_return_to
    dance_return_to = '/' + return_to
    return redirect(url_for('google.login'))


@REST_API.route('/discord/<return_to>')
def discord_login(return_to):
    global dance_return_to
    dance_return_to = '/' + return_to
    return redirect(url_for('discord.login'))


@REST_API.route('/github/<return_to>')
def github_login(return_to):
    global dance_return_to
    dance_return_to = '/' + return_to
    return redirect(url_for('github.login'))


@REST_API.route('/finish_dance')
def finish_dance():
    global dance_return_to
    redirect_url = dance_return_to
    dance_return_to = '/'
    return redirect(redirect_url)


@REST_API.route('/privacy')
def privacy():
    return render_template('privacy_policy.html')

@REST_API.route('/terms')
def terms():
    return render_template('terms_of_service.html')


arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('-i', '--insecure', action="store_true", help="If set, will use http instead of https (useful for testing)")
arg_parser.add_argument('-v', '--verbosity', action='count', default=0, help="The default logging level is ERROR or higher. This value increases the amount of logging seen in your screen")
arg_parser.add_argument('-q', '--quiet', action='count', default=0, help="The default logging level is ERROR or higher. This value decreases the amount of logging seen in your screen")
arg_parser.add_argument('-c', '--convert_flag', action='store', default=None, required=False, type=str, help="A special flag to convert from previous DB entries to newer and exit")
arg_parser.add_argument('-p', '--port', action='store', default=7001, required=False, type=int, help="Provide a different port to start with")
arg_parser.add_argument('--allow_direct_connections', action="store_true", required=False, default=False, help="If set, will allow connections outside the reverse proxy (useful for testing)")

if __name__ == "__main__":
    global _db
    global _waiting_prompts
    global _processing_generations
    args = arg_parser.parse_args()
    allow_direct_connections = args.allow_direct_connections
    set_logger_verbosity(args.verbosity)
    quiesce_logger(args.quiet)    
    # Only setting this for the WSGI logs
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.WARNING)
    _db = Database(convert_flag=args.convert_flag)
    _waiting_prompts = PromptsIndex()
    _processing_generations = GenerationsIndex()
    google_client_id = os.getenv("GOOGLE_CLIENT_ID")
    google_client_secret = os.getenv("GLOOGLE_CLIENT_SECRET")
    discord_client_id = os.getenv("DISCORD_CLIENT_ID")
    discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    github_client_id = os.getenv("GITHUB_CLIENT_ID")
    github_client_secret = os.getenv("GITHUB_CLIENT_SECRET")
    REST_API.secret_key = os.getenv("secret_key")
    os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
    url_scheme = 'https'
    if args.insecure:
        os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1' # Disable this on prod
        url_scheme = 'http'
    google_blueprint = make_google_blueprint(
        client_id = google_client_id,
        client_secret = google_client_secret,
        reprompt_consent = True,
        redirect_url='/register',
        scope = ["email"],
    )
    REST_API.register_blueprint(google_blueprint,url_prefix="/google")
    discord_blueprint = make_discord_blueprint(
        client_id = discord_client_id,
        client_secret = discord_client_secret,
        scope = ["identify"],
        redirect_url='/finish_dance',
    )
    REST_API.register_blueprint(discord_blueprint,url_prefix="/discord")
    github_blueprint = make_github_blueprint(
        client_id = github_client_id,
        client_secret = github_client_secret,
        scope = ["identify"],
        redirect_url='/finish_dance',
    )
    REST_API.register_blueprint(github_blueprint,url_prefix="/github")
    api.add_resource(SyncGenerate, "/generate/sync")
    # Async is disabled due to the memory requirements of keeping images in running memory
    api.add_resource(AsyncGenerate, "/generate/async")
    api.add_resource(AsyncGeneratePrompt, "/generate/prompt/<string:id>")
    api.add_resource(AsyncCheck, "/generate/check/<string:id>")
    api.add_resource(PromptPop, "/generate/pop")
    api.add_resource(SubmitGeneration, "/generate/submit")
    api.add_resource(Users, "/users")
    api.add_resource(UserSingle, "/users/<string:user_id>")
    api.add_resource(Servers, "/servers")
    api.add_resource(ServerSingle, "servers/<string:server_id>")
    api.add_resource(TransferKudos, "/kudos/transfer")
    api.add_resource(HordeLoad, "/status/performance")
    api.add_resource(AdminMaintenanceMode, "/admin/maintenance")
    from waitress import serve
    logger.init("WSGI Server", status="Starting")
    serve(REST_API, host="127.0.0.1", port=args.port, url_scheme=url_scheme, threads=100, connection_limit=4096)
    # REST_API.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
