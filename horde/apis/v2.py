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

response_model_generation_result = api.model('Generation', {
    'img': fields.String(title="Generated Image", description="The generated image as a Base64-encoded .webp file"),
    'seed': fields.String(title="Generation Seed", description="The seed which generated this image"),
    'worker_id': fields.String(title="Worker ID", description="The UUID of the worker which generated this image"),
    'worker_name': fields.String(title="Worker Name", description="The name of the worker which generated this image"),
})
response_model_wp_status_lite = api.model('RequestStatusCheck', {
    'finished': fields.Integer(description="The amount of finished images in this request"),
    'processing': fields.Integer(description="The amount of still processing images in this request"),
    'waiting': fields.Integer(description="The amount of images waiting to be picked up by a worker"),
    'done': fields.Boolean(description="True when all images in this request are done. Else False."),
    'wait_time': fields.Integer(description="The expected amount to wait (in seconds) to generate all images in this request"),
    'queue_position': fields.Integer(description="The position in the requests queue. This position is determined by relative Kudos amounts."),
})
response_model_wp_status_full = api.inherit('RequestStatus', response_model_wp_status_lite, {
    'generations': fields.List(fields.Nested(response_model_generation_result)),
})
response_model_async = api.model('RequestAsync', {
    'id': fields.String(description="The UUID of the request. Use this to retrieve the request status in the future"),
})
response_model_generation_payload = api.model('ModelPayload', {
    'prompt': fields.String(description="The prompt which will be sent to Stable Diffusion to generate an image"),
    'ddim_steps': fields.Integer(example=50), 
    'sampler_name': fields.String(enum=["k_lms", "k_heun", "k_euler", "k_euler_a", "k_dpm_2", "k_dpm_2_a", "DDIM", "PLMS"]), 
    'toggles': fields.List(fields.Integer,example=[1,4], description="Special Toggles used in the SD Webui. To be documented."), 
    'realesrgan_model_name': fields.String,
    'ddim_eta': fields.Float, 
    'n_iter': fields.Integer(example=1, description="The amount of images to generate"), 
    'batch_size': fields.Integer(example=1), 
    'cfg_scale': fields.Float(example=5.0), 
    'seed': fields.String(description="The seed to use to generete this request"),
    'height': fields.Integer(example=512,description="The height of the image to generate"), 
    'width': fields.Integer(example=512,description="The width of the image to generate"), 
    'fp': fields.Integer(example=512), 
    'variant_amount': fields.Float, 
    'variant_seed': fields.Integer
})
response_model_generations_skipped = api.model('NoValidRequestFound', {
    'worker_id': fields.Integer(description="How many waiting requests were skipped because they demanded a specific worker"),
    'max_pixels': fields.Integer(description="How many waiting requests were skipped because they demanded a higher size than this worker provides"),
})

response_model_generation_pop = api.model('GenerationPayload', {
    'payload': fields.Nested(response_model_generation_payload),
    'id': fields.String(description="The UUID for this image generation"),
    'skipped': fields.Nested(response_model_generations_skipped)
})

response_model_generation_submit = api.model('GenerationSubmitted', {
'reward': fields.Float(example=10.0,description="The amount of kudos gained for submitting this request"),
})

response_model_kudos_transfer = api.model('KudosTransferred', {
'transferred': fields.Integer(example=100,description="The amount of Kudos tranferred"),
})

response_model_admin_maintenance = api.model('MaintenanceModeSet', {
'maintenance_mode': fields.Boolean(example=True,description="The current state of maintenance_mode"),
})

response_model_worker_kudos_details = api.model('WorkerKudosDetails', {
    'generated': fields.Float(description="How much Kudos this worker has received for generating images"),
    'uptime': fields.Integer(description="How much Kudos this worker has received for staying online longer"),
})

response_model_worker_details = api.model('WorkerDetails', {
    "name": fields.String(description="The Name given to this worker"),
    "id": fields.String(description="The UUID of this worker"),
    "max_pixels": fields.Integer(example=262144,description="The maximum pixels in resolution this workr can generate"),
    "megapixelsteps_generated": fields.Float(description="How many megapixelsteps this worker has generated until now"),
    "requests_fulfilled": fields.Integer(description="How many images this worker has generated"),
    "kudos_rewards": fields.Float(description="How many Kudos this worker has been rewarded in total"),
    "kudos_details": fields.Nested(response_model_worker_kudos_details),
    "performance": fields.String(description="The average performance of this worker in human readable form"),
    "uptime": fields.Integer(description="The amount of seconds this worker has been online for this Horde"),
    "maintenance_mode": fields.Boolean(description="When True, this worker will not pick up any new requests"),
})

response_model_worker_modify = api.model('ModifyWorker', {
    "maintenance": fields.Boolean(description="The new state of the 'maintenance' var for this server. When True, this worker will not pick up any new requests"),
    "paused": fields.Boolean(description="The new state of the 'paused' var for this server. When True, this worker will not be given any new requests"),
})

response_model_user_kudos_details = api.model('UserKudosDetails', {
    "accumulated": fields.Float(description="The ammount of Kudos accumulated or used for generating images."),
    "gifted": fields.Float(description="The amount of Kudos this user has given to other users"),
    "admin": fields.Float(description="The amount of Kudos this user has been given by the Horde admins"),
    "received": fields.Float(description="The amount of Kudos this user has been given by other users"),
})

response_model_use_contrib_details = api.model('UsageAndContribDetails', {
    "megapixelsteps": fields.Float(description="How many megapixelsteps this user has generated or requested"),
    "fulfillments": fields.Integer(description="How many images this user has generated or requested")
})

response_model_user_details = api.model('UserDetails', {
    "id": fields.Integer(description="The user unique ID. It is always an integer."),
    "kudos": fields.Float(description="The amount of Kudos this user has. Can be negative. The amount of Kudos determines the priority when requesting image generations."),
    "kudos_details": fields.Nested(response_model_user_kudos_details),
    "usage": fields.Nested(response_model_use_contrib_details),
    "contributions": fields.Nested(response_model_use_contrib_details),
    "concurrency": fields.Integer(description="How many concurrent image generations this user may request."),    
})

response_model_user_modify = api.model('ModifyUser', {
    "new_kudos": fields.Float(description="The new total Kudos this user has after this request"),
    "concurrency": fields.Integer(example=30,description="The request concurrency this user has after this request"),
    "usage_multiplier": fields.Float(example=1.0,description="Multiplies the amount of kudos lost when generating images."),
})

response_model_horde_performance = api.model('HordePerformance', {
    "queued_requests": fields.Integer(description="The amount of waiting and processing requests currently in this Horde"),
    "queued_megapixelsteps": fields.Float(description="The amount of megapixelsteps in waiting and processing requests currently in this Horde"),
    "megapixelsteps_per_min": fields.Float(description="How many megapixelsteps this Horde generated in the last minute"),
    "worker_count": fields.Integer(description="How many workers are actively processing image generations in this Horde in the past 5 minutes"),
})

response_model_horde_maintenance_mode = api.model('HordeMaintenanceMode', {
    "maintenance_mode": fields.Boolean(description="When True, this Horde will not accept new requests for image generation, but will finish processing the ones currently in the queue."),
})

response_model_error = api.model('RequestError', {
    'message': fields.String(description="The error message for this status code."),
})


handle_missing_prompts = api.errorhandler(e.MissingPrompt)(e.handle_bad_requests)
handle_kudos_validation_error = api.errorhandler(e.KudosValidationError)(e.handle_bad_requests)
handle_invalid_size = api.errorhandler(e.InvalidSize)(e.handle_bad_requests)
handle_too_many_steps = api.errorhandler(e.TooManySteps)(e.handle_bad_requests)
handle_invalid_api = api.errorhandler(e.InvalidAPIKey)(e.handle_bad_requests)
handle_wrong_credentials = api.errorhandler(e.WrongCredentials)(e.handle_bad_requests)
handle_not_admin = api.errorhandler(e.NotAdmin)(e.handle_bad_requests)
handle_not_owner = api.errorhandler(e.NotOwner)(e.handle_bad_requests)
handle_worker_maintenance = api.errorhandler(e.WorkerMaintenance)(e.handle_bad_requests)
handle_invalid_procgen = api.errorhandler(e.InvalidProcGen)(e.handle_bad_requests)
handle_request_not_found = api.errorhandler(e.RequestNotFound)(e.handle_bad_requests)
handle_worker_not_found = api.errorhandler(e.WorkerNotFound)(e.handle_bad_requests)
handle_user_not_found = api.errorhandler(e.UserNotFound)(e.handle_bad_requests)
handle_duplicate_gen = api.errorhandler(e.DuplicateGen)(e.handle_bad_requests)
handle_too_many_prompts = api.errorhandler(e.TooManyPrompts)(e.handle_bad_requests)
handle_no_valid_servers = api.errorhandler(e.NotValidServers)(e.handle_bad_requests)
handle_maintenance_mode = api.errorhandler(e.MaintenanceMode)(e.handle_bad_requests)


class SyncGenerate(Resource):
    # model_generation = api.model('Successful Sync Generation', {
    # 'generations': fields.List(fields.String),
    # })
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from", location="json")
    parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the SD server", location="json")
    parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt", location="json")
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
            user = _db.find_user_by_api_key(args['apikey'])
        if not user:
            raise e.InvalidAPIKey('sync generation')
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
    @api.marshal_with(response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', response_model_error)
    def get(self, id = ''):
        wp = waiting_prompts.get_item(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status()
        # If the status is retrieved after the wp is done we clear it to free the ram
        if wp_status["done"]:
            wp.delete()
        return(wp_status, 200)


class AsyncCheck(Resource):
    # Increasing this until I can figure out how to pass original IP from reverse proxy
    decorators = [limiter.limit("10/second")]
    @logger.catch
    @api.marshal_with(response_model_wp_status_lite, code=200, description='Async Request Status Check')
    @api.response(404, 'Request Not found', response_model_error)
    def get(self, id = ''):
        wp = waiting_prompts.get_item(id)
        if not wp:
            raise e.RequestNotFound(id)
        return(wp.get_lite_status(), 200)


class AsyncGenerate(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from", location="json")
    parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the SD server", location="json")
    parser.add_argument("servers", type=str, action='append', required=False, default=[], help="If specified, only the server with this ID will be able to generate this prompt", location="json")

    @api.expect(parser)
    @api.marshal_with(response_model_async, code=202, description='Generation Queued')
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
        if args.apikey:
            user = _db.find_user_by_api_key(args['apikey'])
        if not user:
            raise e.InvalidAPIKey('async generation')
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
        return({"id":wp.id}, 202)


class PromptPop(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    parser.add_argument("name", type=str, required=True, help="The server's unique name, to track contributions", location="json")
    parser.add_argument("max_pixels", type=int, required=False, default=512, help="The maximum amount of pixels this server can generate", location="json")
    parser.add_argument("priority_usernames", type=str, action='append', required=False, default=[], help="The usernames which get priority use on this server", location="json")

    decorators = [limiter.limit("45/second")]
    @api.expect(parser)
    @api.marshal_with(response_model_generation_pop, code=200, description='Generation Popped')
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(403, 'Access Denied', response_model_error)
    def post(self):
        args = self.parser.parse_args()
        skipped = {}
        user = _db.find_user_by_api_key(args['apikey'])
        if not user:
            raise e.InvalidAPIKey('prompt pop')
        server = _db.find_server_by_name(args['name'])
        if not server:
            server = KAIServer(_db)
            server.create(user, args['name'])
        if user != server.user:
            raise e.WrongCredentials(user.get_unique_alias(), args['name'])
        server.check_in(args['max_pixels'])
        if server.maintenance:
            raise e.WorkerMaintenance()
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
    parser.add_argument("apikey", type=str, required=True, help="The server's owner API key", location='headers')
    parser.add_argument("id", type=str, required=True, help="The processing generation uuid", location="json")
    parser.add_argument("generation", type=str, required=False, default=[], help="The download location of the image", location="json")
    parser.add_argument("seed", type=str, required=True, default=[], help="The seed of the generated image", location="json")

    @api.expect(parser)
    @api.marshal_with(response_model_generation_submit, code=200, description='Generation Submitted')
    @api.response(400, 'Generation Already Submitted', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    @api.response(404, 'Request Not Found', response_model_error)
    def post(self):
        args = self.parser.parse_args()
        procgen = processing_generations.get_item(args['id'])
        if not procgen:
            raise e.InvalidProcGen(procgen.server.name, args['id'])
        user = _db.find_user_by_api_key(args['apikey'])
        if not user:
            raise e.InvalidAPIKey('server submit:' + args['name'])
        if user != procgen.server.user:
            raise e.WrongCredentials(user.get_unique_alias(), args['name'])
        kudos = procgen.set_generation(args['generation'], args['seed'])
        if kudos == 0:
            raise e.DuplicateGen(procgen.server.name, args['id'])
        return({"reward": kudos}, 200)

class TransferKudos(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The sending user's API key", location='headers')
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos", location="json")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to transfer", location="json")

    @api.expect(parser)
    @api.marshal_with(response_model_kudos_transfer, code=200, description='Generation Submitted')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    def post(self):
        args = self.parser.parse_args()
        user = _db.find_user_by_api_key(args['apikey'])
        if not user:
            raise e.InvalidAPIKey('kudos transfer to: ' + args['username'])
        ret = _db.transfer_kudos_from_apikey_to_username(args['apikey'],args['username'],args['amount'])
        kudos = ret[0]
        error = ret[1]
        if error != 'OK':
            raise e.KudosValidationError(user.get_unique_alias(), error)
        return({"transfered": kudos}, 200)

class Servers(Resource):
    @logger.catch
    @api.marshal_with(response_model_worker_details, code=200, description='Workers List', as_list=True)
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

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin or Owner API key", location='headers')
    parser.add_argument("maintenance", type=bool, required=False, help="Set to true to put this server into maintenance.", location="json")
    parser.add_argument("paused", type=bool, required=False, help="Set to true to pause this server.", location="json")

    @api.marshal_with(response_model_worker_details, code=200, description='Worker Details')
    @api.response(404, 'Worker Not Found', response_model_error)
    def get(self, worker_id = ''):
        server = _db.find_server_by_id(worker_id)
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
            ## Doesn't work at the moment. I'm getting a bad request when setting args. I'll come back to this later.
            # args = self.parser.parse_args()
            # logger.error(apikey)
            # if args.apikey:
            #     logger.error('hehehe')
            #     admin = _db.find_user_by_api_key(args['apikey'])
            #     if not admin:
            #         raise e.InvalidAPIKey('admin worker details')
            #     if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            #         raise e.NotAdmin(admin.get_unique_alias(), 'AdminServerDetails')
            #     logger.error('hehehehe')
            #     sdict['paused'] = server.paused
            return(sdict,200)
        else:
            raise e.WorkerNotFound(worker_id)

    decorators = [limiter.limit("30/minute")]
    # @api.expect(parser)
    @api.marshal_with(response_model_worker_modify, code=200, description='Modify Worker')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    @api.response(404, 'Worker Not Found', response_model_error)
    def put(self, worker_id = ''):
        server = _db.find_server_by_id(worker_id)
        if not server:
            raise e.WorkerNotFound(worker_id)
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PUT ServerSingle')
        ret_dict = {}
        # Both admins and owners can set the server to maintenance
        if args.maintenance != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
                if admin != server.user:
                    raise e.NotOwner(admin.get_unique_alias(), server.name)
            server.maintenance = args.maintenance
            ret_dict["maintenance"] = server.maintenance
        # Only admins can set a server as paused
        if args.paused != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
                raise e.NotAdmin(admin.get_unique_alias(), 'AdminModifyServer')
            server.paused = args.paused
            ret_dict["paused"] = server.paused
        if not len(ret_dict):
            raise e.NoValidActions("No worker modification selected!")
        return(ret_dict, 200)

class Users(Resource):
    @logger.catch
    @api.marshal_with(response_model_user_details, code=200, description='Users List', as_list=True)
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
    @api.marshal_with(response_model_user_details, code=200, description='User Details')
    @api.response(404, 'User Not Found', response_model_error)
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
            raise e.UserNotFound(user_id)

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API key", location='headers')
    parser.add_argument("kudos", type=int, required=False, help="The amount of kudos to modify (can be negative)", location="json")
    parser.add_argument("concurrency", type=int, required=False, help="The amount of concurrent request this user can have", location="json")
    parser.add_argument("usage_multiplier", type=float, required=False, help="The amount by which to multiply the users kudos consumption", location="json")

    decorators = [limiter.limit("30/minute")]
    @api.expect(parser)
    @api.marshal_with(response_model_user_modify, code=200, description='Modify User')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    @api.response(404, 'Worker Not Found', response_model_error)
    def put(self, user_id = ''):
        user = user = _db.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'PUT UserSingle')
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            raise e.NotAdmin(admin.get_unique_alias(), 'AdminModifyUser')
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
            raise e.NoValidActions("No usermod operations selected!")
        return(ret_dict, 200)


class HordeLoad(Resource):
    @logger.catch
    @api.marshal_with(response_model_horde_performance, code=200, description='Horde Performance')
    def get(self):
        load_dict = waiting_prompts.count_totals()
        load_dict["megapixelsteps_per_min"] = _db.stats.get_megapixelsteps_per_min()
        load_dict["server_count"] = _db.count_active_servers()
        return(load_dict,200)

class HordeMaintenance(Resource):
    @logger.catch
    @api.marshal_with(response_model_horde_maintenance_mode, code=200, description='Horde Maintenance')
    def get(self):
        ret_dict = {
            "maintenance_mode": maintenance.active
        }
        return(ret_dict,200)

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API key", location="json")
    parser.add_argument("active", type=bool, required=True, help="Star or stop maintenance mode", location="json")

    decorators = [limiter.limit("30/minute")]
    @api.expect(parser)
    @api.marshal_with(response_model_admin_maintenance, code=200, description='Maintenance Mode Set')
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    def put(self):
        args = self.parser.parse_args()
        admin = _db.find_user_by_api_key(args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'AdminMaintenanceMode')
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            raise e.NotAdmin(admin.get_unique_alias(), 'AdminMaintenanceMode')
        maintenance.toggle(args['active'])
        return({"maintenance_mode": maintenance.active}, 200)



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
api.add_resource(ServerSingle, "/servers/<string:worker_id>")
api.add_resource(TransferKudos, "/kudos/transfer")
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(HordeMaintenance, "/status/maintenance")
