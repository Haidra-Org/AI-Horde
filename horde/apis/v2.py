from flask_restx import Namespace, Resource, reqparse, fields, Api, abort
from .. import limiter
from ..logger import logger
from ..classes import db as _db
from ..classes import processing_generations,waiting_prompts,KAIServer,User,WaitingPrompt
from .. import maintenance
from enum import Enum
from . import exceptions as e
import os, time


api = Namespace('v2', 'API Version 2' )

response_model_generation = api.model('Generation', {
'img': fields.String,
'seed': fields.String,
'server_id': fields.String,
'server_name': fields.String,
})
response_model_wp_status_lite = api.model('RequestStatusCheck', {
'finished': fields.Integer,
'processing': fields.Integer,
'waiting': fields.Integer,
'done': fields.Boolean,
'wait_time': fields.Integer,
})
response_model_wp_status_full = api.inherit('RequestStatus', response_model_wp_status_lite, {
'generations': fields.List(fields.Nested(response_model_generation)),
})

response_model_error = api.model('RequestError', {
'message': fields.String,
})

handle_missing_prompts = api.errorhandler(e.MissingPrompt)(e.handle_bad_requests)
handle_invalid_size = api.errorhandler(e.InvalidSize)(e.handle_bad_requests)
handle_too_many_steps = api.errorhandler(e.TooManySteps)(e.handle_bad_requests)
handle_invalid_api = api.errorhandler(e.InvalidAPIKey)(e.handle_bad_requests)
handle_wrong_credentials = api.errorhandler(e.WrongCredentials)(e.handle_bad_requests)
handle_not_admin = api.errorhandler(e.NotAdmin)(e.handle_bad_requests)
handle_not_owner = api.errorhandler(e.NotOwner)(e.handle_bad_requests)
handle_invalid_procgen = api.errorhandler(e.InvalidProcGen)(e.handle_bad_requests)
handle_duplicate_gen = api.errorhandler(e.DuplicateGen)(e.handle_bad_requests)
handle_too_many_prompts = api.errorhandler(e.TooManyPrompts)(e.handle_bad_requests)
handle_no_valid_servers = api.errorhandler(e.NotValidServers)(e.handle_bad_requests)
handle_maintenance_mode = api.errorhandler(e.MaintenanceMode)(e.handle_bad_requests)


class SyncGenerate(Resource):
    # model_generation = api.model('Successful Sync Generation', {
    # 'generations': fields.List(fields.String),
    # })
    parser = reqparse.RequestParser()
    parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from")
    parser.add_argument("api_key", type=str, required=True, help="The API Key corresponding to a registered user")
    parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the SD server")
    parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt")
    @api.expect(parser)
    @api.marshal_with(response_model_wp_status_full, code=200, description='Images Generated')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(503, 'Maintenance Mode', response_model_error)
    @api.response(429, 'Too Many Prompts', response_model_error)
    def post(self):
        args = self.parser.parse_args()
        username = 'Anonymous'
        user = None
        if maintenance.active:
            raise e.MaintenanceMode('SyncGenerate')
        if args.api_key:
            user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            raise e.InvalidAPIKey('prompt generation')
        username = user.get_unique_alias()
        if args['prompt'] == '':
            raise e.MissingPrompt(username)
        wp_count = waiting_prompts.count_waiting_requests(user)
        if wp_count >= user.concurrency:
            raise e.TooManyPrompts(username, wp_count)
        if args["params"].get("length",512)%64:
            raise e.InvalidSize(username)
        if args["params"].get("width",512)%64:
            raise e.InvalidSize(username)
        if args["params"].get("steps",50) > 100:
            raise e.TooManySteps(username, args['params']['steps'])
        wp = WaitingPrompt(
            _db,
            waiting_prompts,
            processing_generations,
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
            raise e.NotValidServers(username)
        # if a server is available to fulfil this prompt, we activate it and add it to the queue to be generated
        wp.activate()
        while True:
            time.sleep(1)
            if wp.is_stale():
                raise e.RequestExpired(username)
            if wp.is_completed():
                break
        ret_dict = wp.get_status()
        # We delete it from memory immediately to ensure we don't run out
        wp.delete()
        return(ret_dict, 200)


class AsyncGeneratePrompt(Resource):
    decorators = [limiter.limit("3/minute")]
    @logger.catch
    def get(self, id = ''):
        wp = waiting_prompts.get_item(id)
        if not wp:
            return("ID not found", 404)
        wp_status = wp.get_status()
        # If the status is retrieved after the wp is done we clear it to free the ram
        if wp_status["done"]:
            wp.delete()
        return(wp_status, 200)


class AsyncCheck(Resource):
    # Increasing this until I can figure out how to pass original IP from reverse proxy
    decorators = [limiter.limit("10/second")]
    @logger.catch
    def get(self, id = ''):
        wp = waiting_prompts.get_item(id)
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
        if maintenance.active:
            return(f"{get_error(ServerErrors.MAINTENANCE_MODE, endpoint = 'AsyncGenerate')}",503)
        if args.api_key:
            user = _db.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}",401)
        username = user.get_unique_alias()
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = username)}",400)
        wp_count = waiting_prompts.count_waiting_requests(user)
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
            waiting_prompts,
            processing_generations,
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

    decorators = [limiter.limit("45/second")]
    @api.expect(parser)
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
            for wp in waiting_prompts.get_all():
                if wp.user == priority_user and wp.needs_gen():
                    prioritized_wp.append(wp)
        ## End prioritize by bridge request ##
        for wp in waiting_prompts.get_waiting_wp_by_kudos():
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
        procgen = processing_generations.get_item(args['id'])
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

    decorators = [limiter.limit("30/minute")]
    @api.expect(parser)
    def put(self):
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'Admin action: ' + 'AdminMaintenanceMode')}",401)
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            return(f"{get_error(ServerErrors.NOT_ADMIN, username = admin.get_unique_alias(), endpoint = 'AdminMaintenanceMode')}",401)
        maintenance.toggle(args['active'])
        return({"maintenance_mode": maintenance.active}, 200)

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

    decorators = [limiter.limit("30/minute")]
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
    decorators = [limiter.limit("30/minute")]
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

    decorators = [limiter.limit("30/minute")]
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
        load_dict = waiting_prompts.count_totals()
        load_dict["megapixelsteps_per_min"] = _db.stats.get_megapixelsteps_per_min()
        load_dict["server_count"] = _db.count_active_servers()
        load_dict["maintenance_mode"] = maintenance.active
        return(load_dict,200)

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
api.add_resource(ServerSingle, "/servers/<string:server_id>")
api.add_resource(TransferKudos, "/kudos/transfer")
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(AdminMaintenanceMode, "/admin/maintenance")