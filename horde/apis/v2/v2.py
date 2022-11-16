from flask_restx import Namespace, Resource, reqparse, fields, Api, abort
from flask import request
from ... import limiter, logger, maintenance, invite_only, raid, cm, cache
from ...classes import db,processing_generations,waiting_prompts,Worker,User,Team,WaitingPrompt,News,Suspicions
from enum import Enum
from .. import exceptions as e
import os, time, json, re, bleach
from .. import ModelsV2, ParsersV2
from ...utils import is_profane

# Not used yet
authorizations = {
    'apikey': {
        'type': 'apiKey',
        'in': 'header',
        'name': 'apikey'
    }
}

api = Namespace('v2', 'API Version 2' )
models = ModelsV2(api)
parsers = ParsersV2()

handle_missing_prompts = api.errorhandler(e.MissingPrompt)(e.handle_bad_requests)
handle_corrupt_prompt = api.errorhandler(e.CorruptPrompt)(e.handle_bad_requests)
handle_kudos_validation_error = api.errorhandler(e.KudosValidationError)(e.handle_bad_requests)
handle_invalid_size = api.errorhandler(e.InvalidSize)(e.handle_bad_requests)
handle_invalid_prompt_size = api.errorhandler(e.InvalidPromptSize)(e.handle_bad_requests)
handle_too_many_steps = api.errorhandler(e.TooManySteps)(e.handle_bad_requests)
handle_profanity = api.errorhandler(e.Profanity)(e.handle_bad_requests)
handle_too_long = api.errorhandler(e.TooLong)(e.handle_bad_requests)
handle_name_conflict = api.errorhandler(e.NameAlreadyExists)(e.handle_bad_requests)
handle_invalid_api = api.errorhandler(e.InvalidAPIKey)(e.handle_bad_requests)
handle_image_validation_failed = api.errorhandler(e.ImageValidationFailed)(e.handle_bad_requests)
handle_source_mask_unnecessary = api.errorhandler(e.SourceMaskUnnecessary)(e.handle_bad_requests)
handle_unsupported_sampler = api.errorhandler(e.UnsupportedSampler)(e.handle_bad_requests)
handle_wrong_credentials = api.errorhandler(e.WrongCredentials)(e.handle_bad_requests)
handle_not_admin = api.errorhandler(e.NotAdmin)(e.handle_bad_requests)
handle_not_mod = api.errorhandler(e.NotModerator)(e.handle_bad_requests)
handle_not_owner = api.errorhandler(e.NotOwner)(e.handle_bad_requests)
handle_anon_forbidden = api.errorhandler(e.AnonForbidden)(e.handle_bad_requests)
handle_not_trusted = api.errorhandler(e.NotTrusted)(e.handle_bad_requests)
handle_worker_maintenance = api.errorhandler(e.WorkerMaintenance)(e.handle_bad_requests)
handle_too_many_same_ips = api.errorhandler(e.TooManySameIPs)(e.handle_bad_requests)
handle_worker_invite_only = api.errorhandler(e.WorkerInviteOnly)(e.handle_bad_requests)
handle_unsafe_ip = api.errorhandler(e.UnsafeIP)(e.handle_bad_requests)
handle_timeout_ip = api.errorhandler(e.TimeoutIP)(e.handle_bad_requests)
handle_too_many_new_ips = api.errorhandler(e.TooManyNewIPs)(e.handle_bad_requests)
handle_kudos_upfront = api.errorhandler(e.KudosUpfront)(e.handle_bad_requests)
handle_invalid_procgen = api.errorhandler(e.InvalidProcGen)(e.handle_bad_requests)
handle_request_not_found = api.errorhandler(e.RequestNotFound)(e.handle_bad_requests)
handle_worker_not_found = api.errorhandler(e.WorkerNotFound)(e.handle_bad_requests)
handle_team_not_found = api.errorhandler(e.TeamNotFound)(e.handle_bad_requests)
handle_user_not_found = api.errorhandler(e.UserNotFound)(e.handle_bad_requests)
handle_duplicate_gen = api.errorhandler(e.DuplicateGen)(e.handle_bad_requests)
handle_request_expired = api.errorhandler(e.RequestExpired)(e.handle_bad_requests)
handle_too_many_prompts = api.errorhandler(e.TooManyPrompts)(e.handle_bad_requests)
handle_no_valid_workers = api.errorhandler(e.NoValidWorkers)(e.handle_bad_requests)
handle_no_valid_actions = api.errorhandler(e.NoValidActions)(e.handle_bad_requests)
handle_maintenance_mode = api.errorhandler(e.MaintenanceMode)(e.handle_bad_requests)

regex_blacklists1 = []
regex_blacklists2 = []
if os.getenv("BLACKLIST1A"):
    for blacklist in ["BLACKLIST1A","BLACKLIST1B"]:
        regex_blacklists1.append(re.compile(os.getenv(blacklist), re.IGNORECASE))
if os.getenv("BLACKLIST2A"):
    for blacklist in ["BLACKLIST2A"]:
        regex_blacklists2.append(re.compile(os.getenv(blacklist), re.IGNORECASE))

# Used to for the flask limiter, to limit requests per url paths
def get_request_path():
    # logger.info(dir(request))
    return(f"{request.remote_addr}@{request.method}@{request.path}")

# I have to put it outside the class as I can't figure out how to extend the argparser and also pass it to the @api.expect decorator inside the class
class GenerateTemplate(Resource):

    def post(self):
        self.args = parsers.generate_parser.parse_args()
        # I have to extract and store them this way, because if I use the defaults
        # It causes them to be a shared object from the parsers class
        self.params = {}
        if self.args.params:
            self.params = self.args.params
        self.models = []
        if self.args.models:
            self.models = self.args.models
        self.workers = []
        if self.args.workers:
            self.workers = self.args.workers
        self.username = 'Anonymous'
        self.user = None
        self.user_ip = request.remote_addr
        # For now this is checked on validate()
        self.safe_ip = True
        self.validate()
        self.initiate_waiting_prompt()
        worker_found = False
        for worker in list(db.workers.values()):
            if len(self.workers) and worker.id not in self.workers:
                continue
            if worker.can_generate(self.wp)[0]:
                worker_found = True
                break
        self.activate_waiting_prompt()

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        if maintenance.active:
            raise e.MaintenanceMode('Generate')
        if self.args.apikey:
            self.user = db.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('generation')
        self.username = self.user.get_unique_alias()
        if self.args['prompt'] == '':
            raise e.MissingPrompt(self.username)
        wp_count = waiting_prompts.count_waiting_requests(self.user)
        if len(self.workers):
            for worker_id in self.workers:
                if not db.find_worker_by_id(worker_id):
                    raise e.WorkerNotFound(worker_id)
        n = 1
        if self.args.params:
            n = self.args.params.get('n',1)
        user_limit = self.user.get_concurrency(self.args["models"],db.get_available_models(waiting_prompts,lite_dict=True))
        if wp_count + n > user_limit:
            raise e.TooManyPrompts(self.username, wp_count + n, user_limit)
        ip_timeout = cm.retrieve_timeout(self.user_ip)
        if ip_timeout:
            raise e.TimeoutIP(self.user_ip, ip_timeout)
        prompt_suspicion = 0
        if "###" in self.args.prompt:
            prompt, negprompt = self.args.prompt.split("###", 1)
        else:
            prompt = self.args.prompt
        for blacklist_regex in [regex_blacklists1, regex_blacklists2]:
            for blacklist in blacklist_regex:
                if blacklist.search(prompt):
                    prompt_suspicion += 1
                    break
        if prompt_suspicion >= 2:
            self.user.report_suspicion(1,Suspicions.CORRUPT_PROMPT)
            cm.report_suspicion(self.user_ip)
            raise e.CorruptPrompt(self.username, self.user_ip, prompt)

    
    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.params,
            workers=self.args["workers"],
            nsfw=self.args["nsfw"],
            trusted_workers=self.args["trusted_workers"],
        )
    
    # We split this into its own function, so that it may be overriden and extended
    def activate_waiting_prompt(self):
        self.wp.activate()

class AsyncGenerate(GenerateTemplate):

    @api.expect(parsers.generate_parser, models.input_model_request_generation, validate=True)
    @api.marshal_with(models.response_model_async, code=202, description='Generation Queued', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(503, 'Maintenance Mode', models.response_model_error)
    @api.response(429, 'Too Many Prompts', models.response_model_error)
    def post(self):
        '''Initiate an Asynchronous request to generate images.
        This endpoint will immediately return with the UUID of the request for generation.
        This endpoint will always be accepted, even if there are no workers available currently to fulfill this request. 
        Perhaps some will appear in the next 10 minutes.
        Asynchronous requests live for 10 minutes before being considered stale and being deleted.
        '''
        super().post()
        ret_dict = {"id":self.wp.id}
        if not self.wp.has_valid_workers() and not raid.active:
            ret_dict['message'] = self.get_size_too_big_message()
        return(ret_dict, 202)

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Please confider reducing its size of the request.")


class SyncGenerate(GenerateTemplate):

    @api.expect(parsers.generate_parser, models.input_model_request_generation, validate=True)
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.marshal_with(models.response_model_wp_status_full, code=200, description='Images Generated')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(503, 'Maintenance Mode', models.response_model_error)
    @api.response(429, 'Too Many Prompts', models.response_model_error)
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
        if not self.wp.has_valid_workers():
            # We don't need to call .delete() on the wp because it's not activated yet
            # And therefore not added to the waiting_prompt dict.
            raise e.NoValidWorkers(self.username)
        # if a worker is available to fulfil this prompt, we activate it and add it to the queue to be generated
        super().activate_waiting_prompt()

class AsyncStatus(Resource):
    decorators = [limiter.limit("2/minute", key_func = get_request_path)]
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.marshal_with(models.response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', models.response_model_error)
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

    @api.marshal_with(models.response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def delete(self, id = ''):
        '''Cancel an unfinished request.
        This request will include all already generated images in base64 encoded .webp files.
        '''
        wp = waiting_prompts.get_item(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status()
        logger.info(f"Request with ID {wp.id} has been cancelled.")
        wp.delete()
        return(wp_status, 200)


class AsyncCheck(Resource):
    # Increasing this until I can figure out how to pass original IP from reverse proxy
    decorators = [limiter.limit("10/second", key_func = get_request_path)]
    @cache.cached(timeout=1)
    @api.marshal_with(models.response_model_wp_status_lite, code=200, description='Async Request Status Check')
    # @cache.cached(timeout=0.5)
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id = ''):
        '''Retrieve the status of an Asynchronous generation request without images.
        Use this request to check the status of a currently running asynchronous request without consuming bandwidth.
        '''
        wp = waiting_prompts.get_item(id)
        if not wp:
            raise e.RequestNotFound(id)
        return(wp.get_lite_status(), 200)


class JobPop(Resource):

    decorators = [limiter.limit("60/second")]
    @api.expect(parsers.job_pop_parser, models.input_model_job_pop, validate=True)
    @api.marshal_with(models.response_model_job_pop, code=200, description='Generation Popped')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def post(self):
        '''Check if there are generation requests queued for fulfillment.
        This endpoint is used by registered workers only
        '''
        self.args = parsers.job_pop_parser.parse_args()
        # I have to extract and store them this way, because if I use the defaults
        # It causes them to be a shared object from the parsers class
        self.blacklist = []
        if self.args.blacklist:
            self.blacklist = self.args.blacklist
        self.priority_usernames = []
        if self.args.priority_usernames:
            self.priority_usernames = self.args.priority_usernames
        self.models = []
        if self.args.models:
            self.models = self.args.models
        self.worker_ip = request.remote_addr
        self.validate()
        self.check_in()
        # This ensures that the priority requested by the bridge is respected
        self.prioritized_wp = []
        self.priority_users = [self.user]
        ## Start prioritize by bridge request ##
        for priority_username in self.priority_usernames:
            priority_user = db.find_user_by_username(priority_username)
            if priority_user:
               self.priority_users.append(priority_user)
        for priority_user in self.priority_users:
            wp_list = waiting_prompts.get_all()
            for wp in wp_list:
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
                # We don't report on secret skipped reasons
                # as they're typically countermeasures to raids
                if skipped_reason != "secret":
                    self.skipped[skipped_reason] = self.skipped.get(skipped_reason,0) + 1
                continue
            # There is a chance that by the time we finished all the checks, another worker picked up the WP. 
            # So we do another final check here before picking it up to avoid sending the same WP to two workers by mistake.
            if not wp.needs_gen():
                continue
            return(self.start_worker(wp), 200)
        # We report maintenance exception only if we couldn't find any jobs
        if self.worker.maintenance:
            raise e.WorkerMaintenance(self.worker.id)
        return({"id": None, "skipped": self.skipped}, 200)

    # Making it into its own function to allow extension
    def start_worker(self, wp):
        # Paused worker gives a fake prompt
        # Unless the owner of the worker is the owner of the prompt
        # Then we allow them to fulfil their own request
        if self.worker.paused and wp.user != self.worker.user:
            ret = wp.fake_generation(self.worker)
        else:
            ret = wp.start_generation(self.worker)
        return(ret)

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        self.skipped = {}
        self.user = db.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('prompt pop')
        self.worker_name = bleach.clean(self.args['name'])
        self.worker = db.find_worker_by_name(self.worker_name)
        self.safe_ip = True
        if not self.worker or not self.worker.user.trusted:
            self.safe_ip = cm.is_ip_safe(self.worker_ip)
            if self.safe_ip == None:
                raise e.TooManyNewIPs(self.worker_ip)
            if self.safe_ip == False:
                # Outside of a raid, we allow 1 worker in unsafe IPs from untrusted users. They will have to explicitly request it via discord
                # EDIT # Below line commented for now, which means we do not allow any untrusted workers at all from untrusted users
                # if not raid.active and db.count_workers_in_ipaddr(self.worker_ip) == 0:
                #     self.safe_ip = True
                # if a raid is ongoing, we do not inform the suspicious IPs we detected them
                if not self.safe_ip and not raid.active:
                    raise e.UnsafeIP(self.worker_ip)
        if not self.worker:
            if is_profane(self.worker_name):
                raise e.Profanity(self.user.get_unique_alias(), self.worker_name, 'worker name')
            worker_count = self.user.count_workers()
            if invite_only.active and worker_count >= self.user.worker_invited:
                raise e.WorkerInviteOnly(worker_count)
            if self.user.exceeding_ipaddr_restrictions(self.worker_ip):
                raise e.TooManySameIPs(self.user.username)
            self.worker = Worker(db)
            self.worker.create(self.user, self.worker_name)
        if self.user != self.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.worker_name)
    
    # We split this to its own function so that it can be extended with the specific vars needed to check in
    # You typically never want to use this template's function without extending it
    def check_in(self):
        self.worker.check_in(
            nsfw = self.args['nsfw'], 
            blacklist = self.args['blacklist'], 
            safe_ip = self.safe_ip, 
            ipaddr = self.worker_ip)


class JobSubmit(Resource):
    decorators = [limiter.limit("60/second")]
    @api.expect(parsers.job_submit_parser)
    @api.marshal_with(models.response_model_job_submit, code=200, description='Generation Submitted')
    @api.response(400, 'Generation Already Submitted', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Request Not Found', models.response_model_error)
    def post(self):
        '''Submit a generated image.
        This endpoint is used by registered workers only
        '''
        self.args = parsers.job_submit_parser.parse_args()
        self.validate()
        return({"reward": self.kudos}, 200)

    def validate(self):
        self.procgen = processing_generations.get_item(self.args['id'])
        if not self.procgen:
            raise e.InvalidProcGen(self.args['id'])
        self.user = db.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('worker submit:' + self.args['name'])
        if self.user != self.procgen.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.procgen.worker.name)
        self.kudos = self.procgen.set_generation(self.args['generation'], seed=self.args['seed'])
        if self.kudos == 0 and not self.procgen.worker.maintenance:
            raise e.DuplicateGen(self.procgen.worker.name, self.args['id'])


class TransferKudos(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The sending user's API key", location='headers')
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos", location="json")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to transfer", location="json")

    @api.expect(parser)
    @api.marshal_with(models.response_model_kudos_transfer, code=200, description='Generation Submitted')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
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
        return({"transferred": kudos}, 200)

class Workers(Resource):
    @logger.catch(reraise=True)
    @cache.cached(timeout=10)
    @api.marshal_with(models.response_model_worker_details, code=200, description='Workers List', as_list=True, skip_none=True)
    def get(self):
        '''A List with the details of all registered and active workers
        '''
        workers_ret = []
        # I could do this with a comprehension, but this is clearer to understand
        for worker in list(db.workers.values()):
            if worker.is_stale():
                continue
            workers_ret.append(worker.get_details())
        return(workers_ret,200)

class WorkerSingle(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')

    @api.expect(get_parser)
    @cache.cached(timeout=10)
    @api.marshal_with(models.response_model_worker_details, code=200, description='Worker Details', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Worker Not Found', models.response_model_error)
    def get(self, worker_id = ''):
        '''Details of a registered worker
        Can retrieve the details of a worker even if inactive
        (A worker is considered inactive if it has not checked in for 5 minutes)
        '''
        worker = db.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = db.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('admin worker details')
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorWorkerDetails')
            details_privilege = 2
        return(worker.get_details(details_privilege),200)

    put_parser = reqparse.RequestParser()
    put_parser.add_argument("apikey", type=str, required=True, help="The Moderator or Owner API key", location='headers')
    put_parser.add_argument("maintenance", type=bool, required=False, help="Set to true to put this worker into maintenance.", location="json")
    put_parser.add_argument("paused", type=bool, required=False, help="Set to true to pause this worker.", location="json")
    put_parser.add_argument("info", type=str, required=False, help="You can optionally provide a server note which will be seen in the server details. No profanity allowed!", location="json")
    put_parser.add_argument("name", type=str, required=False, help="When this is set, it will change the worker's name. No profanity allowed!", location="json")
    put_parser.add_argument("team", type=str, required=False, help="The team ID towards which this worker contributes kudos.", location="json")


    decorators = [limiter.limit("30/minute", key_func = get_request_path)]
    @api.expect(put_parser, models.input_model_worker_modify, validate=True)
    @api.marshal_with(models.response_model_worker_modify, code=200, description='Modify Worker', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Worker Not Found', models.response_model_error)
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
        self.args = self.put_parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PUT WorkerSingle')
        ret_dict = {}
        # Both admins and owners can set the worker to maintenance
        if self.args.maintenance != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                if admin != worker.user:
                    raise e.NotOwner(admin.get_unique_alias(), worker.name)
            worker.maintenance = self.args.maintenance
            ret_dict["maintenance"] = worker.maintenance
        # Only owners can set info notes
        if self.args.info != None:
            if not admin.moderator and admin != worker.user:
                raise e.NotOwner(admin.get_unique_alias(), worker.name)
            if admin.is_anon():
                raise e.AnonForbidden()
            ret = worker.set_info(self.args.info)
            if ret == "Profanity":
                raise e.Profanity(admin.get_unique_alias(), self.args.info, 'worker info')
            if ret == "Too Long":
                raise e.TooLong(admin.get_unique_alias(), len(self.args.info), 1000, 'worker info')
            ret_dict["info"] = worker.info
        # Only mods can set a worker as paused
        if self.args.paused != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT WorkerSingle')
            worker.paused = self.args.paused
            ret_dict["paused"] = worker.paused
        if self.args.name != None:
            if not admin.moderator and admin != worker.user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT WorkerSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            ret = worker.set_name(self.args.name)
            if ret == "Profanity":
                raise e.Profanity(self.user.get_unique_alias(), self.args.name, 'worker name')
            if ret == "Too Long":
                raise e.TooLong(admin.get_unique_alias(), len(self.args.name), 100, 'worker name')
            if ret == "Already Exists":
                raise e.NameAlreadyExists(admin.get_unique_alias(), worker.name, self.args.name)
            ret_dict["name"] = worker.name
        if self.args.team != None:
            if not admin.moderator and admin != worker.user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT WorkerSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            if self.args.team == '':
                worker.set_team(None)
                ret_dict["team"] = 'None'
            else:
                team = db.find_team_by_id(self.args.team)
                if not team:
                    raise e.TeamNotFound(self.args.team)
                ret = worker.set_team(team)
                ret_dict["team"] = team.name
        if not len(ret_dict):
            raise e.NoValidActions("No worker modification selected!")
        return(ret_dict, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')


    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_deleted_worker, code=200, description='Delete Worker')
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Worker Not Found', models.response_model_error)
    def delete(self, worker_id = ''):
        '''Delete the worker entry
        This will delete the worker and their statistics. Will not affect the kudos generated by that worker for their owner.
        Only the worker's owner and an admin can use this endpoint.
        This action is unrecoverable!
        '''
        worker = db.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        self.args = self.delete_parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PUT WorkerSingle')
        if not admin.moderator and admin != worker.user:
            raise e.NotModerator(admin.get_unique_alias(), 'DELETE WorkerSingle')
        if admin.is_anon():
            raise e.AnonForbidden()
        logger.warning(f'{admin.get_unique_alias()} deleted worker: {worker.name}')
        ret_dict = {
            'deleted_id': worker.id,
            'deleted_name': worker.name,
        }
        worker.delete()
        return(ret_dict, 200)

class Users(Resource):
    decorators = [limiter.limit("30/minute")]
    @cache.cached(timeout=10)
    @api.marshal_with(models.response_model_user_details, code=200, description='Users List')
    def get(self):
        '''A List with the details and statistic of all registered users
        '''
        # To avoid the the dict changing size while we're iterating it
        all_users = list(db.users.values())
        users_list = [user.get_details() for user in all_users]
        return(users_list,200)


class UserSingle(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Admin, Mod or Owner API key", location='headers')

    decorators = [limiter.limit("60/minute", key_func = get_request_path)]
    @api.expect(get_parser)
    @cache.cached(timeout=3)
    @api.marshal_with(models.response_model_user_details, code=200, description='User Details', skip_none=True)
    @api.response(404, 'User Not Found', models.response_model_error)
    def get(self, user_id = ''):
        '''Details and statistics about a specific user
        '''
        user = db.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = db.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('privileged user details')
            if admin.moderator:
                details_privilege = 2
            elif admin == user:
                details_privilege = 1
            else:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorWorkerDetails')
        ret_dict = {}
        return(user.get_details(details_privilege),200)

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API key", location='headers')
    parser.add_argument("kudos", type=int, required=False, help="The amount of kudos to modify (can be negative)", location="json")
    parser.add_argument("concurrency", type=int, required=False, help="The amount of concurrent request this user can have", location="json")
    parser.add_argument("usage_multiplier", type=float, required=False, help="The amount by which to multiply the users kudos consumption", location="json")
    parser.add_argument("worker_invite", type=int, required=False, help="Set to the amount of workers this user is allowed to join to the horde when in worker invite-only mode.", location="json")
    parser.add_argument("moderator", type=bool, required=False, help="Set to true to Make this user a horde moderator", location="json")
    parser.add_argument("public_workers", type=bool, required=False, help="Set to true to Make this user a display their worker IDs", location="json")
    parser.add_argument("username", type=str, required=False, help="When specified, will change the username. No profanity allowed!", location="json")
    parser.add_argument("monthly_kudos", type=int, required=False, help="When specified, will start assigning the user monthly kudos, starting now!", location="json")
    parser.add_argument("trusted", type=bool, required=False, help="When set to true,the user and their servers will not be affected by suspicion", location="json")
    parser.add_argument("contact", type=str, required=False, location="json")
    parser.add_argument("reset_suspicion", type=bool, required=False, location="json")

    decorators = [limiter.limit("60/minute", key_func = get_request_path)]
    @api.expect(parser, models.input_model_user_details, validate=True)
    @api.marshal_with(models.response_model_user_modify, code=200, description='Modify User', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Worker Not Found', models.response_model_error)
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
        ret_dict = {}
        # Admin Access
        if self.args.kudos != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.modify_kudos(self.args.kudos, 'admin')
            ret_dict["new_kudos"] = user.kudos
        if self.args.monthly_kudos != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.modify_monthly_kudos(self.args.monthly_kudos)
            ret_dict["monthly_kudos"] = user.monthly_kudos['amount']
        if self.args.usage_multiplier != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.usage_multiplier = self.args.usage_multiplier
            ret_dict["usage_multiplier"] = user.usage_multiplier
        if self.args.moderator != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_moderator(self.args.moderator)
            ret_dict["moderator"] = user.moderator
        # Moderator Access
        if self.args.concurrency != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.concurrency = self.args.concurrency
            ret_dict["concurrency"] = user.concurrency
        if self.args.worker_invite != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.worker_invited = self.args.worker_invite
            ret_dict["worker_invited"] = user.worker_invited
        if self.args.trusted != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_trusted(self.args.trusted)
            ret_dict["trusted"] = user.trusted
        if self.args.reset_suspicion != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.reset_suspicion()
            ret_dict["new_suspicion"] = user.suspicious
        # User Access
        if self.args.public_workers != None:
            if not admin.moderator and admin != user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            user.public_workers = self.args.public_workers
            ret_dict["public_workers"] = user.public_workers
        if self.args.username != None:
            if not admin.moderator and admin != user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            ret = user.set_username(self.args.username)
            if ret == "Profanity":
                raise e.Profanity(admin.get_unique_alias(), self.args.username, 'username')
            if ret == "Too Long":
                raise e.TooLong(admin.get_unique_alias(), len(self.args.username), 30, 'username')
            ret_dict["username"] = user.username
        if self.args.contact != None:
            if not admin.moderator and admin != user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            ret = user.set_contact(self.args.contact)
            if ret == "Profanity":
                raise e.Profanity(admin.get_unique_alias(), self.args.contact, 'worker contact')
            ret_dict["contact"] = user.contact
        if not len(ret_dict):
            raise e.NoValidActions("No usermod operations selected!")
        return(ret_dict, 200)


class FindUser(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="User API key we're looking for", location='headers')

    @api.expect(get_parser)
    @api.marshal_with(models.response_model_user_details, code=200, description='Worker Details', skip_none=True)
    @api.response(404, 'User Not Found', models.response_model_error)
    def get(self):
        '''Lookup user details based on their API key
        This can be used to verify a user exists
        '''
        self.args = self.get_parser.parse_args()
        user = db.find_user_by_api_key(self.args.apikey)
        if not user:
            raise e.UserNotFound(self.args.apikey, 'api_key')
        return(user.get_details(1),200)


class Models(Resource):
    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.marshal_with(models.response_model_active_model, code=200, description='List All Active Models', as_list=True)
    def get(self):
        '''Returns a list of models active currently in this horde
        '''
        return(db.get_available_models(waiting_prompts),200)


class HordeLoad(Resource):
    # decorators = [limiter.limit("20/minute")]
    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Performance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = waiting_prompts.count_totals()
        load_dict["worker_count"] = db.count_active_workers()
        return(load_dict,200)

class HordeNews(Resource):
    @logger.catch(reraise=True)
    @cache.cached(timeout=300)
    @api.marshal_with(models.response_model_newspiece, code=200, description='Horde News', as_list = True)
    def get(self):
        '''Read the latest happenings on the horde
        '''
        news = News()
        logger.debug(news.sorted_news())
        return(news.sorted_news(),200)
    

class HordeModes(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Admin or Owner API key", location='headers')

    @api.expect(get_parser)
    @cache.cached(timeout=50)
    @api.marshal_with(models.response_model_horde_modes, code=200, description='Horde Maintenance', skip_none=True)
    def get(self):
        '''Horde Maintenance Mode Status
        Use this endpoint to quicky determine if this horde is in maintenance, invite_only or raid mode.
        '''
        ret_dict = {
            "maintenance_mode": maintenance.active,
            "invite_only_mode": invite_only.active,
            
        }
        is_privileged = False
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = db.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('admin worker details')
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorWorkerDetails')
            ret_dict["raid_mode"] = raid.active
        return(ret_dict,200)

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API key", location="headers")
    parser.add_argument("maintenance", type=bool, required=False, help="Start or stop maintenance mode", location="json")
    parser.add_argument("shutdown", type=int, required=False, help="Initiate a graceful shutdown of the horde in this amount of seconds. Will put horde in maintenance if not already set.", location="json")
    parser.add_argument("invite_only", type=bool, required=False, help="Start or stop worker invite-only mode", location="json")
    parser.add_argument("raid", type=bool, required=False, help="Start or stop raid mode", location="json")

    decorators = [limiter.limit("30/minute")]
    @api.expect(parser)
    @api.marshal_with(models.response_model_horde_modes, code=200, description='Maintenance Mode Set', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def put(self):
        '''Change Horde Modes
        Endpoint for admins to (un)set the horde into maintenance, invite_only or raid modes.
        '''
        self.args = self.parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'PUT HordeModes')
        ret_dict = {}
        if self.args.maintenance != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT HordeModes')
            maintenance.toggle(self.args.maintenance)
            logger.critical(f"Horde entered maintenance mode")
            db.initiate_save(10)
            for wp in waiting_prompts.get_all():
                wp.abort_for_maintenance()
            ret_dict["maintenance_mode"] = maintenance.active
        if self.args.shutdown != None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT HordeModes')
            maintenance.toggle(self.args.maintenance)
            for wp in waiting_prompts.get_all():
                wp.abort_for_maintenance()
            db.shutdown(self.args.shutdown)
            ret_dict["maintenance_mode"] = maintenance.active
        if self.args.invite_only != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT HordeModes')
            invite_only.toggle(self.args.invite_only)
            ret_dict["invite_only_mode"] = invite_only.active
        if self.args.raid != None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT HordeModes')
            raid.toggle(self.args.raid)
            ret_dict["raid_mode"] = raid.active
        if not len(ret_dict):
            raise e.NoValidActions("No mod change selected!")
        return(ret_dict, 200)

class Teams(Resource):
    @logger.catch(reraise=True)
    @cache.cached(timeout=10)
    @api.marshal_with(models.response_model_team_details, code=200, description='Teams List', as_list=True, skip_none=True)
    def get(self):
        '''A List with the details of all teams
        '''
        teams_ret = []
        # I could do this with a comprehension, but this is clearer to understand
        for team in list(db.teams.values()):
            teams_ret.append(team.get_details())
        return(teams_ret,200)

    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="A User API key", location='headers')
    post_parser.add_argument("name", type=str, required=True, location="json")
    post_parser.add_argument("info", type=str, required=False, location="json")


    decorators = [limiter.limit("30/minute", key_func = get_request_path)]
    @api.expect(post_parser, models.input_model_team_create, validate=True)
    @api.marshal_with(models.response_model_team_modify, code=200, description='Create Team', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def post(self, team_id = ''):
        '''Create a new team.
        Only trusted users can create new teams.
        '''
        self.args = self.post_parser.parse_args()
        user = db.find_user_by_api_key(self.args['apikey'])
        if not user:
            raise e.InvalidAPIKey('User action: ' + 'PUT Teams')
        if user.is_anon():
            raise e.AnonForbidden()
        if not user.trusted:
            raise e.NotTrusted
        ret_dict = {}
        team = Team(db)
        ret = team.set_name(self.args.name)
        if ret == "Profanity":
            raise e.Profanity(self.user.get_unique_alias(), self.args.name, 'team name')
        if ret == "Already Exists":
            raise e.NameAlreadyExists(user.get_unique_alias(), team.name, self.args.name, 'team')
        ret_dict["name"] = team.name
        if self.args.info != None:
            ret = team.set_info(self.args.info)
            if ret == "Profanity":
                raise e.Profanity(user.get_unique_alias(), self.args.info, 'team info')
            ret_dict["info"] = team.info
        team.create(user)
        ret_dict["id"] = team.id
        return(ret_dict, 200)


class TeamSingle(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')

    @api.expect(get_parser)
    @cache.cached(timeout=3)
    @api.marshal_with(models.response_model_team_details, code=200, description='Team Details', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Team Not Found', models.response_model_error)
    def get(self, team_id = ''):
        '''Details of a worker Team'''
        team = db.find_team_by_id(team_id)
        if not team:
            raise e.TeamNotFound(team_id)
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = db.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('admin team details')
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorTeamDetails')
            details_privilege = 2
        return(team.get_details(details_privilege),200)

    patch_parser = reqparse.RequestParser()
    patch_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Creator API key", location='headers')
    patch_parser.add_argument("name", type=str, required=False, location="json")
    patch_parser.add_argument("info", type=str, required=False, location="json")


    decorators = [limiter.limit("30/minute", key_func = get_request_path)]
    @api.expect(patch_parser, models.input_model_team_modify, validate=True)
    @api.marshal_with(models.response_model_team_modify, code=200, description='Modify Team', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Team Not Found', models.response_model_error)
    def patch(self, team_id = ''):
        '''Update a Team's information
        '''
        team = db.find_team_by_id(team_id)
        if not team:
            raise e.TeamNotFound(team_id)
        self.args = self.patch_parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PATCH TeamSingle')
        ret_dict = {}
        # Only creators can set info notes
        if self.args.info != None:
            if not admin.moderator and admin != team.user:
                raise e.NotOwner(admin.get_unique_alias(), team.name)
            ret = team.set_info(self.args.info)
            if ret == "Profanity":
                raise e.Profanity(admin.get_unique_alias(), self.args.info, 'team info')
            ret_dict["info"] = team.info
        if self.args.name != None:
            if not admin.moderator and admin != team.user:
                raise e.NotModerator(admin.get_unique_alias(), 'PATCH TeamSingle')
            ret = team.set_name(self.args.name)
            if ret == "Profanity":
                raise e.Profanity(self.user.get_unique_alias(), self.args.name, 'team name')
            if ret == "Already Exists":
                raise e.NameAlreadyExists(user.get_unique_alias(), team.name, self.args.name, 'team')
            ret_dict["name"] = team.name
        if not len(ret_dict):
            raise e.NoValidActions("No team modification selected!")
        return(ret_dict, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')


    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_deleted_team, code=200, description='Delete Team')
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Team Not Found', models.response_model_error)
    def delete(self, team_id = ''):
        '''Delete the team entry
        Only the team's creator or a horde moderator can use this endpoint.
        This action is unrecoverable!
        '''
        team = db.find_team_by_id(team_id)
        if not team:
            raise e.TeamNotFound(team_id)
        self.args = self.delete_parser.parse_args()
        admin = db.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'DELETE TeamSingle')
        if not admin.moderator and admin != team.user:
            raise e.NotModerator(admin.get_unique_alias(), 'DELETE TeamSingle')
        logger.warning(f'{admin.get_unique_alias()} deleted team: {team.name}')
        ret_dict = {
            'deleted_id': team.id,
            'deleted_name': team.name,
        }
        team.delete()
        return(ret_dict, 200)


class OperationsIP(Resource):
    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=True, help="A mod API key", location='headers')
    delete_parser.add_argument("ipaddr", type=str, required=True, location="json")

    @api.expect(delete_parser, models.input_model_delete_ip_timeout, validate=True)
    @api.marshal_with(models.response_model_simple_response, code=200, description='Operation Completed', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def delete(self, team_id = ''):
        '''Remove an IP from timeout.
        Only usable by horde moderators
        '''
        self.args = self.delete_parser.parse_args()
        mod = db.find_user_by_api_key(self.args['apikey'])
        if not mod:
            raise e.InvalidAPIKey('User action: ' + 'DELETE OperationsIP')
        if not mod.moderator:
            raise e.NotModerator(mod.get_unique_alias(), 'DELETE OperationsIP')
        cm.delete_timeout(self.args.ipaddr)
        return({"message":'OK'}, 200)

        