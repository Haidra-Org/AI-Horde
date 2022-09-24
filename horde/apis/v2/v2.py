from flask_restx import Namespace, Resource, reqparse, fields, Api, abort
from flask import request
from ... import limiter
from ...logger import logger
from ...classes import db
from ...classes import processing_generations,waiting_prompts,Worker,User,WaitingPrompt
from ... import maintenance
from enum import Enum
from .. import exceptions as e
import os, time


api = Namespace('v2', 'API Version 2' )

response_model_generation_result = api.model('Generation', {
    'generation': fields.String(title="Generated Image", description="The generated image as a Base64-encoded .webp file"),
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
    'n': fields.Integer(example=1, description="The amount of images to generate"), 
    'seed': fields.String(description="The seed to use to generete this request"),
})
response_model_generations_skipped = api.model('NoValidRequestFound', {
    'worker_id': fields.Integer(description="How many waiting requests were skipped because they demanded a specific worker"),
})

response_model_job_pop = api.model('GenerationPayload', {
    'payload': fields.Nested(response_model_generation_payload),
    'id': fields.String(description="The UUID for this image generation"),
    'skipped': fields.Nested(response_model_generations_skipped)
})

response_model_job_submit = api.model('GenerationSubmitted', {
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
    "requests_fulfilled": fields.Integer(description="How many images this worker has generated"),
    "kudos_rewards": fields.Float(description="How many Kudos this worker has been rewarded in total"),
    "kudos_details": fields.Nested(response_model_worker_kudos_details),
    "performance": fields.String(description="The average performance of this worker in human readable form"),
    "uptime": fields.Integer(description="The amount of seconds this worker has been online for this Horde"),
    "maintenance_mode": fields.Boolean(description="When True, this worker will not pick up any new requests"),
})

response_model_worker_modify = api.model('ModifyWorker', {
    "maintenance": fields.Boolean(description="The new state of the 'maintenance' var for this worker. When True, this worker will not pick up any new requests"),
    "paused": fields.Boolean(description="The new state of the 'paused' var for this worker. When True, this worker will not be given any new requests"),
})

response_model_user_kudos_details = api.model('UserKudosDetails', {
    "accumulated": fields.Float(description="The ammount of Kudos accumulated or used for generating images."),
    "gifted": fields.Float(description="The amount of Kudos this user has given to other users"),
    "admin": fields.Float(description="The amount of Kudos this user has been given by the Horde admins"),
    "received": fields.Float(description="The amount of Kudos this user has been given by other users"),
})

response_model_use_contrib_details = api.model('UsageAndContribDetails', {
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
handle_no_valid_workers = api.errorhandler(e.NoValidWorkers)(e.handle_bad_requests)
handle_maintenance_mode = api.errorhandler(e.MaintenanceMode)(e.handle_bad_requests)

# Used to for the flas limiter, to limit requests per url paths
def get_request_path():
    return(request.path)

generate_parser = reqparse.RequestParser()
generate_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
generate_parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from", location="json")
generate_parser.add_argument("params", type=dict, required=False, default={}, help="Extra generate params to send to the worker", location="json")
generate_parser.add_argument("workers", type=str, action='append', required=False, default=[], help="If specified, only the worker with this ID will be able to generate this prompt", location="json")

# I have to put it outside the class as I can't figure out how to extend the argparser and also pass it to the @api.expect decorator inside the class
class GenerateTemplate(Resource):

    def post(self):
        self.args = generate_parser.parse_args()
        self.username = 'Anonymous'
        self.user = None
        self.validate()
        self.initiate_waiting_prompt()
        worker_found = False
        for worker in db.workers.values():
            if len(self.args.workers) and worker.id not in self.args.workers:
                continue
            if worker.can_generate(self.wp)[0]:
                worker_found = True
                break
        self.activate_waiting_prompt()

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        if maintenance.active:
            raise e.MaintenanceMode('SyncGenerate')
        if self.args.apikey:
            self.user = db.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('async generation')
        self.username = self.user.get_unique_alias()
        if self.args['prompt'] == '':
            raise e.MissingPrompt(self.username)
        wp_count = waiting_prompts.count_waiting_requests(self.user)
        if wp_count >= self.user.concurrency:
            raise e.TooManyPrompts(self.username, wp_count)
    
    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.args["params"],
            workers=self.args["workers"],
        )
    
    # We split this into its own function, so that it may be overriden and extended
    def activate_waiting_prompt(self):
        self.wp.activate()

class AsyncGenerateTemplate(GenerateTemplate):

    @api.expect(generate_parser)
    @api.marshal_with(response_model_async, code=202, description='Generation Queued')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(503, 'Maintenance Mode', response_model_error)
    @api.response(429, 'Too Many Prompts', response_model_error)
    def post(self):
        '''Initiate an Asynchronous request to generate images.
        This endpoint will immediately return with the UUID of the request for generation.
        This endpoint will always be accepted, even if there are no workers available currently to fulfill this request. 
        Perhaps some will appear in the next 10 minutes.
        Asynchronous requests live for 10 minutes before being considered stale and being deleted.
        '''
        super().post()
        return({"id":self.wp.id}, 202)

class SyncGenerateTemplate(GenerateTemplate):

    @api.expect(generate_parser)
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    # @api.marshal_with(response_model_wp_status_full, code=200, description='Images Generated')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(503, 'Maintenance Mode', response_model_error)
    @api.response(429, 'Too Many Prompts', response_model_error)
    def post(self):
        '''Initiate a Synchronous request to generate images.
        This connection will only terminate when the images have been generated, or an error occured.
        If you connection is interrupted, you will not have the request UUID, so you cannot retrieve the images asynchronously.
        '''
        super().post()
        while True:
            time.sleep(1)
            if self.wp.is_stale():
                raise e.RequestExpired(self.username)
            if self.wp.is_completed():
                break
        ret_dict = self.wp.get_status()
        # We delete it from memory immediately to ensure we don't run out
        self.wp.delete()
        return(ret_dict, 200)

    # We extend this function so we can check if any workers can fulfil the request, before adding it to the queue
    def activate_waiting_prompt(self):
        # We don't want to keep synchronous requests up unless there's someone who can fulfill them
        for worker in db.workers.values():
            if len(self.args.workers) and worker.id not in self.args.workers:
                continue
            if worker.can_generate(self.wp)[0]:
                worker_found = True
                break
        if not worker_found:
            # We don't need to call .delete() on the wp because it's not activated yet
            # And therefore not added to the waiting_prompt dict.
            raise e.NoValidWorkers(username)
        # if a worker is available to fulfil this prompt, we activate it and add it to the queue to be generated
        super().activate_waiting_prompt()

class AsyncStatusTemplate(Resource):
    decorators = [limiter.limit("2/minute", key_func = get_request_path)]
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    # @api.marshal_with(response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', response_model_error)
    def get(self, id = ''):
        '''Retrieve the full status of an Asynchronous generation request.
        This request will include all already generated images in base64 encoded .webp files.
        As such, you are requested to not retrieve this endpoint often. Instead use the /check/ endpoint first
        This endpoint is limited to 1 request per minute
        '''
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
    @api.marshal_with(response_model_wp_status_lite, code=200, description='Async Request Status Check')
    @api.response(404, 'Request Not found', response_model_error)
    def get(self, id = ''):
        '''Retrieve the status of an Asynchronous generation request without images.
        Use this request to check the status of a currently running asynchronous request without consuming bandwidth.
        '''
        wp = waiting_prompts.get_item(id)
        if not wp:
            raise e.RequestNotFound(id)
        return(wp.get_lite_status(), 200)


# The parser for RequestPop
job_pop_parser = reqparse.RequestParser()
job_pop_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
job_pop_parser.add_argument("name", type=str, required=True, help="The worker's unique name, to track contributions", location="json")
job_pop_parser.add_argument("priority_usernames", type=str, action='append', required=False, default=[], help="The usernames which get priority use on this worker", location="json")

class JobPopTemplate(Resource):

    decorators = [limiter.limit("2/second")]
    @api.expect(job_pop_parser)
    @api.marshal_with(response_model_job_pop, code=200, description='Generation Popped')
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(403, 'Access Denied', response_model_error)
    def post(self):
        '''Check if there are generation requests queued for fulfillment.
        This endpoint is used by registered workers only
        '''
        self.args = job_pop_parser.parse_args()
        self.validate()
        self.check_in()
        # Paused worker return silently
        if self.worker.paused:
            return({"id": None, "skipped": {}},200)
        # This ensures that the priority requested by the bridge is respected
        self.prioritized_wp = []
        self.priority_users = [self.user]
        ## Start prioritize by bridge request ##
        for priority_username in self.args.priority_usernames:
            priority_user = db.find_user_by_username(priority_username)
            if priority_user:
               self.priority_users.append(priority_user)
        for priority_user in self.priority_users:
            for wp in waiting_prompts.get_all():
                if wp.user == priority_user and wp.needs_gen():
                    self.prioritized_wp.append(wp)
        ## End prioritize by bridge request ##
        for wp in waiting_prompts.get_waiting_wp_by_kudos():
            if wp not in self.prioritized_wp:
                self.prioritized_wp.append(wp)
        for wp in self.prioritized_wp:
            check_gen = self.worker.can_generate(wp)
            if not check_gen[0]:
                skipped_reason = check_gen[1]
                self.skipped[skipped_reason] = self.skipped.get(skipped_reason,0) + 1
                continue
            ret = wp.start_generation(worker)
            return(ret, 200)
        return({"id": None, "skipped": skipped}, 200)

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        self.skipped = {}
        self.user = db.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('prompt pop')
        self.worker = db.find_worker_by_name(self.args['name'])
        if not self.worker:
            self.worker = Worker(db)
            self.worker.create(self.user, self.args['name'])
        if self.user != self.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.args['name'])
        if self.worker.maintenance:
            raise e.WorkerMaintenance()
    
    # We split this to its own function so that it can be extended with the specific vars needed to check in
    # You typically never want to use this template's function without extending it
    def check_in(self):
        self.worker.check_in()


job_submit_parser = reqparse.RequestParser()
job_submit_parser.add_argument("apikey", type=str, required=True, help="The worker's owner API key", location='headers')
job_submit_parser.add_argument("id", type=str, required=True, help="The processing generation uuid", location="json")
job_submit_parser.add_argument("generation", type=str, required=False, default=[], help="The generated output", location="json")

class JobSubmitTemplate(Resource):
    @api.expect(job_submit_parser)
    @api.marshal_with(response_model_job_submit, code=200, description='Generation Submitted')
    @api.response(400, 'Generation Already Submitted', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    @api.response(404, 'Request Not Found', response_model_error)
    def post(self):
        '''Submit a generated image.
        This endpoint is used by registered workers only
        '''
        self.args = job_submit_parser.parse_args()
        self.validate()
        return({"reward": kudos}, 200)

    def validate(self):
        self.procgen = processing_generations.get_item(self.args['id'])
        if not self.procgen:
            raise e.InvalidProcGen(procgen.worker.name, self.args['id'])
        self.user = db.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('worker submit:' + self.args['name'])
        if self.user != self.procgen.worker.user:
            raise e.WrongCredentials(user.get_unique_alias(), self.args['name'])
        self.kudos = self.procgen.set_generation(self.args['generation'], self.args['seed'])
        if kudos == 0:
            raise e.DuplicateGen(procgen.worker.name, self.args['id'])


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
        '''Transfer Kudos to another registed user
        '''
        self.args = self.parser.parse_args()
        user = db.find_user_by_api_key(self.args['apikey'])
        if not user:
            raise e.InvalidAPIKey('kudos transfer to: ' + self.args['username'])
        ret = db.transfer_kudos_from_apikey_to_username(self.args['apikey'],self.args['username'],self.args['amount'])
        kudos = ret[0]
        error = ret[1]
        if error != 'OK':
            raise e.KudosValidationError(user.get_unique_alias(), error)
        return({"transfered": kudos}, 200)

class Workers(Resource):
    @logger.catch
    @api.marshal_with(response_model_worker_details, code=200, description='Workers List', as_list=True)
    def get(self):
        '''A List with the details of all registered and active workers
        '''
        workers_ret = []
        # I could do this with a comprehension, but this is clearer to understand
        for worker in db.workers.values():
            if worker.is_stale():
                continue
            workers_ret.append(worker.get_details())
        return(workers_ret,200)

class WorkerSingle(Resource):

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin or Owner API key", location='headers')
    parser.add_argument("maintenance", type=bool, required=False, help="Set to true to put this worker into maintenance.", location="json")
    parser.add_argument("paused", type=bool, required=False, help="Set to true to pause this worker.", location="json")

    @api.marshal_with(response_model_worker_details, code=200, description='Worker Details')
    @api.response(404, 'Worker Not Found', response_model_error)
    def get(self, worker_id = ''):
        '''Details of a registered worker
        Can retrieve the details of a worker even if inactive
        (A worker is considered inactive if it has not checked in for 5 minutes)
        '''
        worker = db.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        is_privileged = False
        # logger.debug('test start')
        # # Doesn't work at the moment. I'm getting a bad request when setting args. I'll come back to this later.
        # self.args = self.parser.parse_args()
        # if self.args.apikey:
        #     logger.debug('hehehe')
        #     admin = db.find_user_by_api_key(self.args['apikey'])
        #     if not admin:
        #         raise e.InvalidAPIKey('admin worker details')
        #     if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
        #         raise e.NotAdmin(admin.get_unique_alias(), 'AdminWorkerDetails')
        #     logger.debug('hehehehe')
        #     is_privileged = True
        return(worker.get_details(is_privileged),200)

    decorators = [limiter.limit("30/minute")]
    # @api.expect(parser)
    @api.marshal_with(response_model_worker_modify, code=200, description='Modify Worker', skip_none=True)
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    @api.response(404, 'Worker Not Found', response_model_error)
    def put(self, worker_id = ''):
        '''Put the worker into maintenance or pause mode
        Maintenance can be set by the owner of the serve or an admin. 
        When in maintenance, the worker will receive a 503 request when trying to retrieve new requests. Use this to avoid disconnecting your worker in the middle of a generation
        Paused can be set only by the admins of this Horde.
        When in paused mode, the worker will not be given any requests to generate.
        '''
        worker = db.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        self.args = self.parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PUT WorkerSingle')
        ret_dict = {}
        # Both admins and owners can set the worker to maintenance
        if self.args.maintenance != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
                if admin != worker.user:
                    raise e.NotOwner(admin.get_unique_alias(), worker.name)
            worker.maintenance = args.maintenance
            ret_dict["maintenance"] = worker.maintenance
        # Only admins can set a worker as paused
        if self.args.paused != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
                raise e.NotAdmin(admin.get_unique_alias(), 'AdminModifyWorker')
            worker.paused = args.paused
            ret_dict["paused"] = worker.paused
        if not len(ret_dict):
            raise e.NoValidActions("No worker modification selected!")
        return(ret_dict, 200)

class Users(Resource):
    decorators = [limiter.limit("2/minute")]
    @logger.catch
    @api.marshal_with(response_model_user_details, code=200, description='Users List')
    def get(self):
        '''A List with the details and statistic of all registered users
        '''
        users_list = [user.get_details() for user in db.users.values()]
        return(users_list,200)


class UserSingle(Resource):
    decorators = [limiter.limit("30/minute")]
    @api.marshal_with(response_model_user_details, code=200, description='User Details')
    @api.response(404, 'User Not Found', response_model_error)
    def get(self, user_id = ''):
        '''Details and statistics about a specific user
        '''
        logger.debug(user_id)
        user = db.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        return(user.get_details(),200)

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
        '''Endpoint for horde admins to perform operations on users
        '''
        user = user = db.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        self.args = self.parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'PUT UserSingle')
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            raise e.NotAdmin(admin.get_unique_alias(), 'AdminModifyUser')
        ret_dict = {}
        if self.args.kudos:
            user.modify_kudos(self.args.kudos, 'admin')
            ret_dict["new_kudos"] = user.kudos
        if self.args.concurrency:
            user.concurrency = self.args.concurrency
            ret_dict["concurrency"] = user.concurrency
        if self.args.usage_multiplier:
            user.usage_multiplier = self.args.usage_multiplier
            ret_dict["usage_multiplier"] = user.usage_multiplier
        if not len(ret_dict):
            raise e.NoValidActions("No usermod operations selected!")
        return(ret_dict, 200)


class HordeLoadTemplate(Resource):
    decorators = [limiter.limit("20/minute")]
    @logger.catch
    # @api.marshal_with(response_model_horde_performance, code=200, description='Horde Performance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = waiting_prompts.count_totals()
        load_dict["worker_count"] = db.count_active_workers()
        return(load_dict,200)

class HordeMaintenance(Resource):
    decorators = [limiter.limit("2/second")]
    @logger.catch
    @api.marshal_with(response_model_horde_maintenance_mode, code=200, description='Horde Maintenance')
    def get(self):
        '''Horde Maintenance Mode Status
        Use this endpoint to quicky determine if this horde is in maintenance.
        '''
        ret_dict = {
            "maintenance_mode": maintenance.active
        }
        return(ret_dict,200)

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API key", location="headers")
    parser.add_argument("active", type=bool, required=True, help="Star or stop maintenance mode", location="json")

    decorators = [limiter.limit("30/minute")]
    @api.expect(parser)
    @api.marshal_with(response_model_admin_maintenance, code=200, description='Maintenance Mode Set')
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    def put(self):
        '''Change Horde Maintenance Mode 
        Endpoint for admins to (un)set the horde into maintenance.
        When in maintenance no new requests for generation will be accepted
        but requests currently in the queue will be completed.
        '''
        self.args = self.parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'AdminMaintenanceMode')
        if not os.getenv("ADMINS") or admin.get_unique_alias() not in os.getenv("ADMINS"):
            raise e.NotAdmin(admin.get_unique_alias(), 'AdminMaintenanceMode')
        maintenance.toggle(self.args['active'])
        return({"maintenance_mode": maintenance.active}, 200)


