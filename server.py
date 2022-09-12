from flask import Flask, render_template, redirect, url_for, request
from flask_restful import Resource, reqparse, Api
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
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
from logger import logger

class ServerErrors(Enum):
    WRONG_CREDENTIALS = 0
    INVALID_PROCGEN = 1
    DUPLICATE_GEN = 2
    TOO_MANY_PROMPTS = 3
    EMPTY_PROMPT = 4
    INVALID_API_KEY = 5
    INVALID_MODEL = 6

REST_API = Flask(__name__)
# Very basic DOS prevention
limiter = Limiter(
    REST_API,
    key_func=get_remote_address,
    default_limits=["90 per minute"]
)
api = Api(REST_API)
dance_return_to = '/'
load_dotenv()


def get_error(error, **kwargs):
    if error == ServerErrors.INVALID_API_KEY:
        logger.warning(f'Invalid API Key sent for {kwargs["subject"]}.')
        return(f'No user matching sent API Key. Have you remembered to register at https://koboldai.net/register ?')
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
        logger.warning(f'User "{kwargs["username"]}" has already requested too many parallel prompts ({kwargs["wp_count"]}). Aborting!')
        return("Too many parallel requests from same user. Please try again later.")
    if error == ServerErrors.EMPTY_PROMPT:
        logger.warning(f'User "{kwargs["username"]}" sent an empty prompt. Aborting!')
        return("You cannot specify an empty prompt.")
    if error == ServerErrors.INVALID_MODEL:
        logger.warning(f'Server "{kwargs["name"]}" tried to prompt pop with invalid model: {kwargs["model"]}. Aborting!')
        return(f'Invalid model for generating: {kwargs["model"]}.')


@REST_API.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, Authorization"
    return response

class SyncGenerate(Resource):
    decorators = [limiter.limit("10/minute")]
    def post(self, api_version = None):
        parser = reqparse.RequestParser()
        parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
        parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
        parser.add_argument("models", type=str, action='append', required=False, default=[], help="The acceptable models with which to generate")
        parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the KoboldAI server")
        parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")
        parser.add_argument("softprompts", type=str, action='append', required=False, default=[''], help="If specified, only servers who can load this softprompt will generate this request")
        # Not implemented yet
        parser.add_argument("world_info", type=str, required=False, help="If specified, only servers who can load this this world info will generate this request")
        args = parser.parse_args()
        username = 'Anonymous'
        if args.api_key:
            user = _db.find_user_by_api_key(args['api_key'])
            if not user:
                return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}",401)
            username = user.get_unique_alias()
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = username)}",400)
        wp_count = _waiting_prompts.count_waiting_requests(user)
        if wp_count >= 3:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = username, wp_count = wp_count)}",503)
        wp = WaitingPrompt(
            _db,
            _waiting_prompts,
            _processing_generations,
            args["prompt"],
            user,
            args["models"],
            args["params"],
            servers=args["servers"],
            softprompts=args["softprompts"],

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
        if not api_version:
            return([gen['text'] for gen in wp.get_status()['generations']], 200)
        else:
            return(wp.get_status()['generations'], 200)


class AsyncGeneratePrompt(Resource):
    decorators = [limiter.limit("30/minute")]
    def get(self, api_version = None, id = ''):
        wp = _waiting_prompts.get_item(id)
        if not wp:
            return("ID not found", 404)
        return(wp.get_status(), 200)


class AsyncGenerate(Resource):
    decorators = [limiter.limit("10/minute")]
    def post(self, api_version = None):
        parser = reqparse.RequestParser()
        parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
        parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
        parser.add_argument("models", type=str, action='append', required=False, default=[], help="The acceptable models with which to generate")
        parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the KoboldAI server")
        parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")
        parser.add_argument("softprompts", action='append', required=False, default=[''], help="If specified, only servers who can load this softprompt will generate this request")
        args = parser.parse_args()
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}",401)
        wp_count = _waiting_prompts.count_waiting_requests(user)
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = user.get_unique_alias())}",400)
        if wp_count >= 3:
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = user.get_unique_alias(), wp_count = wp_count)}",503)
        wp = WaitingPrompt(
            _db,
            _waiting_prompts,
            _processing_generations,
            args["prompt"],
            user,
            args["models"],
            args["params"],
            servers=args["servers"],
            softprompts=args["softprompts"],

        )
        wp.activate()
        return({"id":wp.id}, 200)


class PromptPop(Resource):
    decorators = [limiter.limit("45/second")]
    def post(self, api_version = None):
        parser = reqparse.RequestParser()
        parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
        parser.add_argument("name", type=str, required=True, help="The server's unique name, to track contributions")
        parser.add_argument("model", type=str, required=True, help="The model currently running on this KoboldAI")
        parser.add_argument("max_length", type=int, required=False, default=512, help="The maximum amount of tokens this server can generate")
        parser.add_argument("max_content_length", type=int, required=False, default=2048, help="The max amount of context to submit to this AI for sampling.")
        parser.add_argument("priority_usernames", type=str, action='append', required=False, default=[], help="The usernames which get priority use on this server")
        parser.add_argument("softprompts", type=str, action='append', required=False, default=[], help="The available softprompt files on this cluster for the currently running model")
        args = parser.parse_args()
        skipped = {}
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'server promptpop: ' + args['name'])}",401)
        if args['model'] in ['CLUSTER', 'ReadOnly']:
            return(f"{get_error(ServerErrors.INVALID_MODEL, name = args['name'], model = args['model'])}",400)
        server = _db.find_server_by_name(args['name'])
        if not server:
            server = KAIServer(_db)
            server.create(user, args['name'], args["softprompts"])
        if user != server.user:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = user.get_unique_alias())}",401)
        server.check_in(args['model'], args['max_length'], args['max_content_length'], args["softprompts"])
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
            matching_softprompt = False
            for sp in wp.softprompts:
                # If a None softprompts has been provided, we always match, since we can always remove the softprompt
                if sp == '':
                    matching_softprompt = sp
                for sp_name in args['softprompts']:
                    # logger.info([sp_name,sp,sp in sp_name])
                    if sp in sp_name: # We do a very basic string matching. Don't think we need to do regex
                        matching_softprompt = sp_name
                        break
                if matching_softprompt:
                    break
            ret = wp.start_generation(server, matching_softprompt)
            return(ret, 200)
        return({"id": None, "skipped": skipped}, 200)


class SubmitGeneration(Resource):
    def post(self, api_version = None):
        parser = reqparse.RequestParser()
        parser.add_argument("id", type=str, required=True, help="The processing generation uuid")
        parser.add_argument("api_key", type=str, required=True, help="The server's owner API key")
        parser.add_argument("generation", type=str, required=False, default=[], help="The generated text")
        args = parser.parse_args()
        procgen = _processing_generations.get_item(args['id'])
        if not procgen:
            return(f"{get_error(ServerErrors.INVALID_PROCGEN,id = args['id'])}",404)
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'server submit: ' + args['name'])}",401)
        if user != procgen.server.user:
            return(f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = user.get_unique_alias())}",401)
        chars = procgen.set_generation(args['generation'])
        if chars == 0:
            return(f"{get_error(ServerErrors.DUPLICATE_GEN,id = args['id'])}",400)
        return({"reward": chars}, 200)

class TransferKudos(Resource):
    def post(self, api_version = None):
        parser = reqparse.RequestParser()
        parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos")
        parser.add_argument("api_key", type=str, required=True, help="The sending user's API key")
        parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to transfer")
        args = parser.parse_args()
        user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'kudos transfer to: ' + args['username'])}",401)
        ret = _db.transfer_kudos_from_apikey_to_username(args['api_key'],args['username'],args['amount'])
        kudos = ret[0]
        error = ret[1]
        if error != 'OK':
            return(f"{error}",400)
        return({"transfered": kudos}, 200)

class Models(Resource):
    def get(self, api_version = None):
        return(_db.get_available_models(),200)


class Servers(Resource):
    def get(self, api_version = None):
        servers_ret = []
        for server in _db.servers.values():
            if server.is_stale():
                continue
            sdict = {
                "name": server.name,
                "id": server.id,
                "model": server.model,
                "max_length": server.max_length,
                "max_content_length": server.max_content_length,
                "chars_generated": server.contributions,
                "requests_fulfilled": server.fulfilments,
                "kudos_rewards": server.kudos,
                "kudos_details": server.kudos_details,
                "performance": server.get_performance(),
                "uptime": server.uptime,
            }
            servers_ret.append(sdict)
        return(servers_ret,200)

class ServerSingle(Resource):
    def get(self, api_version = None, server_id = ''):
        server = None
        for s in _db.servers.values():
            if s.id == server_id:
                server = s
                break
        if server:
            sdict = {
                "name": server.name,
                "id": server.id,
                "model": server.model,
                "max_length": server.max_length,
                "max_content_length": server.max_content_length,
                "chars_generated": server.contributions,
                "requests_fulfilled": server.fulfilments,
                "latest_performance": server.get_performance(),
            }
            return(sdict,200)
        else:
            return("Not found", 404)



class Users(Resource):
    def get(self, api_version = None):
        user_dict = {}
        for user in _db.users.values():
            user_dict[user.get_unique_alias()] = {
                "id": user.id,
                "kudos": user.kudos,
                "kudos_details": user.kudos_details,
                "usage": user.usage,
                "contributions": user.contributions,
            }
        return(user_dict,200)


class UserSingle(Resource):
    def get(self, api_version = None, user_id = ''):
        logger.debug(user_id)
        user = None
        for u in _db.users.values():
            if str(u.id) == user_id:
                user = u
                break
        if user:
            udict = {
                "username": user.get_unique_alias(),
                "kudos": user.kudos,
                "usage": user.usage,
                "contributions": user.contributions,
            }
            return(udict,200)
        else:
            return("Not found", 404)


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
        top_contributors = f'\n<img src="https://github.com/db0/KoboldAI-Horde/blob/master/img/{big_image}.jpg?raw=true" width="800" />'
    else:
        top_contributors = f"""\n## Top Contributors
These are the people and servers who have contributed most to this horde.
### Users
This is the person whose server(s) have generated the most chars for the horde.
#### {top_contributor.get_unique_alias()}
* {top_contributor.contributions['chars']} chars generated.
* {top_contributor.contributions['fulfillments']} requests fulfilled.
### Servers
This is the server which has generated the most chars for the horde.
#### {top_server.name}
* {top_server.contributions} chars generated.
* {top_server.fulfilments} request fulfillments.
* {top_server.get_human_readable_uptime()} uptime.

<img src="https://github.com/db0/KoboldAI-Horde/blob/master/img/{big_image}.jpg?raw=true" width="800" />
"""
    policies = """
## Policies

[Privacy Policy](/privacy)

[Terms of Service](/terms)"""
    totals = _db.get_total_usage()
    findex = index.format(
        kobold_image = align_image,
        avg_performance= _db.get_request_avg(),
        total_chars = totals["chars"],
        total_fulfillments = totals["fulfilments"],
        active_servers = _db.count_active_servers(),
        total_queue = _waiting_prompts.count_total_waiting_generations(),
    )
    head = """<head>
    <title>KoboldAI Horde</title>
    <meta name="google-site-verification" content="5imNnbyz39-i9j6dbAeS0o0ZRIfzpznY9FBa_kMZns0" />
    </head>
    """
    return(head + markdown(findex + top_contributors + policies))

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
                           page_title="Join the KoboldAI Horde!",
                           welcome=welcome,
                           user=user,
                           api_key=api_key,
                           username=username,
                           pseudonymous=pseudonymous,
                           oauth_id=oauth_id)


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


if __name__ == "__main__":
    global _db
    global _waiting_prompts
    global _processing_generations

    args = arg_parser.parse_args()
    # Only setting this for the WSGI logs
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',level=logging.ERROR)
    _db = Database()
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
    api.add_resource(SyncGenerate, "/generate/sync","/api/<string:api_version>/generate/sync")
    api.add_resource(AsyncGenerate, "/generate/async","/api/<string:api_version>/generate/async")
    api.add_resource(AsyncGeneratePrompt, "/generate/prompt/<string:id>","/api/<string:api_version>/generate/prompt/<string:id>")
    api.add_resource(PromptPop, "/generate/pop","/api/<string:api_version>/generate/pop")
    api.add_resource(SubmitGeneration, "/generate/submit","/api/<string:api_version>/generate/submit")
    api.add_resource(Users, "/users","/api/<string:api_version>/users")
    api.add_resource(UserSingle, "/users/<string:user_id>","/api/<string:api_version>/users/<string:user_id>")
    api.add_resource(Servers, "/servers","/api/<string:api_version>/servers")
    api.add_resource(ServerSingle, "/servers/<string:server_id>","/api/<string:api_version>/servers/<string:server_id>")
    api.add_resource(Models, "/models","/api/<string:api_version>/models")
    api.add_resource(TransferKudos, "/api/<string:api_version>/kudos/transfer")
    from waitress import serve
    logger.init("WSGI Server", status="Starting")
    serve(REST_API, host="0.0.0.0", port="5001",url_scheme=url_scheme)
    # REST_API.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
