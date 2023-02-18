import json
import os
import time
from enum import Enum

from flask import request
from flask_restx import Namespace, Resource, reqparse, fields

from horde.argparser import maintenance, invite_only
from horde.classes import Worker, WaitingPrompt
from horde.database import functions as database
from horde.classes.base import stats
from horde.countermeasures import CounterMeasures
from horde.flask import db
from horde.limiter import limiter
from horde.logger import logger

api = Namespace('v1', 'API Version 1' )

response_model_generation = api.model('GenerationStableV1', {
    'img': fields.String,
    'seed': fields.String,
    'server_id': fields.String(attribute='worker_id'),
    'server_name': fields.String(attribute='worker_name'),
    'queue_position': fields.Integer(description="The position in the requests queue. This position is determined by relative Kudos amounts."),
})
response_model_wp_status_lite = api.model('RequestStatusCheckStableV1', {
    'finished': fields.Integer,
    'processing': fields.Integer,
    'waiting': fields.Integer,
    'done': fields.Boolean,
    'wait_time': fields.Integer,
    'queue_position': fields.Integer(description="The position in the requests queue. This position is determined by relative Kudos amounts."),
})
response_model_wp_status_full = api.inherit('RequestStatusStableV1', response_model_wp_status_lite, {
    'generations': fields.List(fields.Nested(response_model_generation)),
})
# Used to for the flask limiter, to limit requests per url paths
def get_request_path():
    return request.path

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

@logger.catch(reraise=True)
def get_error(error, **kwargs):
    if error == ServerErrors.INVALID_API_KEY:
        logger.warning(f'Invalid API Key sent for {kwargs["subject"]}.')
        return 'No user matching sent API Key. Have you remembered to register at https://stablehorde.net/register ?'
    if error == ServerErrors.WRONG_CREDENTIALS:
        logger.warning(f'User "{kwargs["username"]}" sent wrong credentials for utilizing instance {kwargs["kai_instance"]}')
        return f'wrong credentials for utilizing instance {kwargs["kai_instance"]}'
    if error == ServerErrors.INVALID_PROCGEN:
        logger.warning(f'Server attempted to provide generation for {kwargs["id"]} but it did not exist')
        return f'Processing Generation with ID {kwargs["id"]} does not exist'
    if error == ServerErrors.DUPLICATE_GEN:
        logger.warning(f'Server attempted to provide duplicate generation for {kwargs["id"]} ')
        return f'Processing Generation with ID {kwargs["id"]} already submitted'
    if error == ServerErrors.TOO_MANY_PROMPTS:
        logger.warning(f'User "{kwargs["username"]}" has already requested too many parallel requests ({kwargs["wp_count"]}). Aborting!')
        return f"Parallel requests exceeded user limit ({kwargs['wp_count']}). Please try again later or request to increase your concurrency."
    if error == ServerErrors.EMPTY_PROMPT:
        logger.warning(f'User "{kwargs["username"]}" sent an empty prompt. Aborting!')
        return "You cannot specify an empty prompt."
    if error == ServerErrors.INVALID_SIZE:
        logger.warning(f'User "{kwargs["username"]}" sent an invalid size. Aborting!')
        return "Invalid size. The image dimentions have to be multiples of 64."
    if error == ServerErrors.TOO_MANY_STEPS:
        logger.warning(f'User "{kwargs["username"]}" sent too many steps ({kwargs["steps"]}). Aborting!')
        return "Too many sampling steps. To allow resources for everyone, we allow only up to 100 steps."
    if error == ServerErrors.NO_PROXY:
        logger.warning(f'Attempt to access outside reverse proxy')
        return 'Access allowed only through https'
    if error == ServerErrors.NOT_ADMIN:
        logger.warning(f'Non-admin user "{kwargs["username"]}" tried to use admin endpoint: "{kwargs["endpoint"]}". Aborting!')
        return "You're not an admin. Sod off!"
    if error == ServerErrors.MAINTENANCE_MODE:
        logger.info(f'Rejecting endpoint "{kwargs["endpoint"]}" because server in maintenance mode.')
        return "Server has entered maintenance mode. Please try again later."
    if error == ServerErrors.NOT_OWNER:
        logger.warning(f'User "{kwargs["username"]}" tried to modify server they do not own: "{kwargs["server_name"]}". Aborting!')
        return "You're not the owner of this server!"



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
    @api.response(200, 'Success', response_model_generation)
    @api.response(400, 'Validation Error')
    def post(self):
        args = self.parser.parse_args()
        username = 'Anonymous'
        user = None
        if maintenance.active:
            return f"{get_error(ServerErrors.MAINTENANCE_MODE, endpoint = 'SyncGenerate')}", 503
        if args.api_key:
            user = database.find_user_by_api_key(args['api_key'])
        if not user:
            return f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}", 401
        username = user.get_unique_alias()
        if args['prompt'] == '':
            return f"{get_error(ServerErrors.EMPTY_PROMPT, username = username)}", 400
        wp_count = database.count_waiting_requests(user)
        if wp_count >= user.get_concurrency(args["models"],database.retrieve_available_models()):
            return f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = username, wp_count = wp_count)}", 503
        if args["params"].get("height",512)%64 or args["params"].get("height",512) <= 0:
            return f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400
        if args["params"].get("width",512)%64 or args["params"].get("width",512) <= 0:
            return f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400
        if args["params"].get("steps",50) > 100:
            return f"{get_error(ServerErrors.TOO_MANY_STEPS, username = username, steps = args['params']['steps'])}", 400
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
        for server in _db.workers.values():
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
        ret_dict = wp.get_status(
            request_avg=database.get_request_avg(),
            has_valid_workers=database.wp_has_valid_workers(self.wp, self.workers),
            active_worker_count=database.count_active_workers()
        )['generations']
        # We delete it from memory immediately to ensure we don't run out
        wp.delete()
        return(ret_dict, 200)


class AsyncStatus(Resource):
    decorators = [limiter.limit("2/minute", key_func = get_request_path)]
    @logger.catch(reraise=True)
    @api.marshal_with(response_model_wp_status_full, code=200, description='Images Generated')
    def get(self, id = ''):
        wp = database.get_wp_by_id(id)
        if not wp:
            return("ID not found", 404)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg(),
            has_valid_workers=database.wp_has_valid_workers(self.wp, self.workers),
            active_worker_count=database.count_active_workers()
        )
        # If the status is retrieved after the wp is done we clear it to free the ram
        if wp_status["done"]:
            wp.delete()
        return(wp_status, 200)


class AsyncCheck(Resource):
    # Increasing this until I can figure out how to pass original IP from reverse proxy
    decorators = [limiter.limit("10/second")]
    @logger.catch(reraise=True)
    def get(self, id = ''):
        wp = database.get_wp_by_id(id)
        if not wp:
            return("ID not found", 404)
        lite_status = wp.get_lite_status(
            request_avg=database.get_request_avg(),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers()
        )
        return(lite_status, 200)


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
            user = database.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'prompt generation')}",401)
        username = user.get_unique_alias()
        if args['prompt'] == '':
            return(f"{get_error(ServerErrors.EMPTY_PROMPT, username = username)}",400)
        wp_count = database.count_waiting_requests(user)
        if wp_count >= user.get_concurrency(args["models"],database.retrieve_available_models()):
            return(f"{get_error(ServerErrors.TOO_MANY_PROMPTS, username = username, wp_count = wp_count)}",503)
        if args["params"].get("height",512)%64 or args["params"].get("height",512) <= 0:
            return(f"{get_error(ServerErrors.INVALID_SIZE, username = username)}",400)
        if args["params"].get("width",512)%64 or args["params"].get("width",512) <= 0:
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
        for server in _db.workers.values():
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
        if not CounterMeasures.is_ip_safe(request.remote_addr):
            return f"Due to abuse prevention, we cannot accept workers from your IP address. Please contact us on Discord if you feel this is a mistake.", 403
        args = self.parser.parse_args()
        skipped = {}
        user = database.find_user_by_api_key(args['api_key'])
        if not user:
            return f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'server promptpop: ' + args['name'])}", 401
        server = db.session.query(Worker).filter(name=args["name"]).first()
        if not server:
            if invite_only.active:
                return f"Horde in worker invite mode only. Please use APIv2 if you have an invite.", 401
            server = Worker()
            server.create(user, args['name'])
        if user != server.user:
            return f"{get_error(ServerErrors.WRONG_CREDENTIALS,kai_instance = args['name'], username = user.get_unique_alias())}", 401
        server.check_in(args['max_pixels'])
        if server.maintenance:
            return f"Server has been put into maintenance mode by the owner", 403
        if server.paused:
            return {"id": None, "skipped": {}}, 200
        # This ensures that the priority requested by the bridge is respected
        prioritized_wp = []
        priority_users = [user]

        ## Start prioritize by bridge request ##
        for priority_username in args.priority_usernames:
            priority_user = database.find_user_by_username(priority_username)
            if priority_user:
                priority_users.append(priority_user)
        for priority_user in priority_users:
            for wp in database.get_all_wps():
                if wp.user == priority_user and wp.needs_gen():
                    prioritized_wp.append(wp)
        ## End prioritize by bridge request ##
        for wp in database.get_sorted_wp_filtered_to_worker(server):
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
        procgen = database.get_progen_by_id(args['id'])
        if not procgen:
            return(f"{get_error(ServerErrors.INVALID_PROCGEN,id = args['id'])}",404)
        user = database.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'server submit: ' + args['name'])}",401)
        if user != procgen.worker.user:
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
        user = database.find_user_by_api_key(args['api_key'])
        if not user:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'kudos transfer to: ' + args['username'])}",401)
        ret = database.transfer_kudos_from_apikey_to_username(args['api_key'],args['username'],args['amount'])
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
        admin = database.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'Admin action: ' + 'AdminMaintenanceMode')}",401)
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
            return(f"{get_error(ServerErrors.NOT_ADMIN, username = admin.get_unique_alias(), endpoint = 'AdminMaintenanceMode')}",401)
        logger.debug(maintenance)
        maintenance.toggle(args['active'])
        return({"maintenance_mode": maintenance.active}, 200)

class Servers(Resource):
    @logger.catch(reraise=True)
    def get(self):
        servers_ret = []
        for server in _db.workers.values():
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
    @logger.catch(reraise=True)
    def get(self, server_id = ''):
        server = database.find_worker_by_id(server_id)
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
        server = database.find_worker_by_id(server_id)
        if not server:
            return("Invalid Server ID", 404)
        args = self.parser.parse_args()
        admin = database.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'User action: ' + 'PUT ServerSingle')}",401)
        ret_dict = {}
        # Both admins and owners can set the server to maintenance
        if args.maintenance is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                if admin != server.user:
                    return(f"{get_error(ServerErrors.NOT_OWNER, username = admin.get_unique_alias(), server_name = server.name)}",401)
            server.maintenance = args.maintenance
            ret_dict["maintenance"] = server.maintenance
        # Only admins can set a server as paused
        if args.paused is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                return(f"{get_error(ServerErrors.NOT_ADMIN, username = admin.get_unique_alias(), endpoint = 'AdminModifyServer')}",401)
            server.paused = args.paused
            ret_dict["paused"] = server.paused
        if not len(ret_dict):
            return("No server modification selected!", 400)
        return(ret_dict, 200)

    # parser = reqparse.RequestParser()
    # parser.add_argument("api_key", type=str, required=True, help="The Admin or server owner API key")

    # # post shows also hidden server info
    # decorators = [limiter.limit("30/minute")]
    # def post(self, server_id = ''):
    #     server = _db.find_worker_by_id(server_id)
    #     if not server:
    #         return("Invalid Server ID", 404)
    #     args = self.parser.parse_args()
    #     admin = database.find_user_by_api_key(args['api_key'])
    #     if not admin:
    #         return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'User action: ' + 'PUT ServerSingle')}",401)
    #     sdict = {
    #         "name": server.name,
    #         "id": server.id,
    #         "max_pixels": server.max_pixels,
    #         "megapixelsteps_generated": server.contributions,
    #         "requests_fulfilled": server.fulfilments,
    #         "latest_performance": server.get_performance(),
    #         "maintenance": server.maintenance,
    #         "paused": server.paused,
    #         "owner": server.user.get_unique_alias(),
    #     }
    #     return(sdict,200)


class Users(Resource):
    @logger.catch(reraise=True)
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
    @logger.catch(reraise=True)
    def get(self, user_id = ''):
        logger.debug(user_id)
        user = database.find_user_by_id(user_id)
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
        user = user = database.find_user_by_id(user_id)
        if not user:
            return(f"Invalid user_id: {user_id}",400)        
        args = self.parser.parse_args()
        admin = database.find_user_by_api_key(args['api_key'])
        if not admin:
            return(f"{get_error(ServerErrors.INVALID_API_KEY, subject = 'Admin action: ' + 'PUT UserSingle')}",401)
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
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
    @logger.catch(reraise=True)
    def get(self):
        load_dict = database.retrieve_totals()
        load_dict["megapixelsteps_per_min"] = stats.get_things_per_min()
        load_dict["server_count"] = database.count_active_workers()[0]
        load_dict["maintenance_mode"] = maintenance.active
        return(load_dict,200)

api.add_resource(SyncGenerate, "/generate/sync")
# Async is disabled due to the memory requirements of keeping images in running memory
api.add_resource(AsyncGenerate, "/generate/async")
api.add_resource(AsyncStatus, "/generate/prompt/<string:id>")
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