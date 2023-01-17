import json
import os
import regex as re
import time
import random
from datetime import datetime
from sqlalchemy.exc import IntegrityError

from horde.database import functions as database
from flask import request
from flask_restx import Namespace, Resource, reqparse
from horde.flask import cache, db, HORDE
from horde.limiter import limiter
from horde.logger import logger
from horde.argparser import maintenance, invite_only, raid
from horde.apis import ModelsV2, ParsersV2
from horde.apis import exceptions as e
from horde.classes import stats, Worker, Team, WaitingPrompt, News, User
from horde.suspicions import Suspicions
from horde.utils import is_profane, sanitize_string
from horde.countermeasures import CounterMeasures
from horde.horde_redis import horde_r
from horde.patreon import patrons

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
handle_polymorphic_name_conflict = api.errorhandler(e.PolymorphicNameConflict)(e.handle_bad_requests)
handle_invalid_api = api.errorhandler(e.InvalidAPIKey)(e.handle_bad_requests)
handle_image_validation_failed = api.errorhandler(e.ImageValidationFailed)(e.handle_bad_requests)
handle_source_mask_unnecessary = api.errorhandler(e.SourceMaskUnnecessary)(e.handle_bad_requests)
handle_unsupported_sampler = api.errorhandler(e.UnsupportedSampler)(e.handle_bad_requests)
handle_unsupported_model = api.errorhandler(e.UnsupportedModel)(e.handle_bad_requests)
handle_invalid_aesthetic_attempt = api.errorhandler(e.InvalidAestheticAttempt)(e.handle_bad_requests)
handle_procgen_not_found = api.errorhandler(e.ProcGenNotFound)(e.handle_bad_requests)
handle_wrong_credentials = api.errorhandler(e.WrongCredentials)(e.handle_bad_requests)
handle_not_admin = api.errorhandler(e.NotAdmin)(e.handle_bad_requests)
handle_not_mod = api.errorhandler(e.NotModerator)(e.handle_bad_requests)
handle_not_owner = api.errorhandler(e.NotOwner)(e.handle_bad_requests)
handle_not_privileged = api.errorhandler(e.NotPrivileged)(e.handle_bad_requests)
handle_anon_forbidden = api.errorhandler(e.AnonForbidden)(e.handle_bad_requests)
handle_not_trusted = api.errorhandler(e.NotTrusted)(e.handle_bad_requests)
handle_worker_maintenance = api.errorhandler(e.WorkerMaintenance)(e.handle_bad_requests)
handle_too_many_same_ips = api.errorhandler(e.TooManySameIPs)(e.handle_bad_requests)
handle_worker_invite_only = api.errorhandler(e.WorkerInviteOnly)(e.handle_bad_requests)
handle_unsafe_ip = api.errorhandler(e.UnsafeIP)(e.handle_bad_requests)
handle_timeout_ip = api.errorhandler(e.TimeoutIP)(e.handle_bad_requests)
handle_too_many_new_ips = api.errorhandler(e.TooManyNewIPs)(e.handle_bad_requests)
handle_kudos_upfront = api.errorhandler(e.KudosUpfront)(e.handle_bad_requests)
handle_invalid_procgen = api.errorhandler(e.InvalidJobID)(e.handle_bad_requests)
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
locked = api.errorhandler(e.Locked)(e.handle_bad_requests)

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
        #logger.warning(datetime.utcnow())
        self.args = parsers.generate_parser.parse_args()
        # I have to extract and store them this way, because if I use the defaults
        # It causes them to be a shared object from the parsers class
        self.params = {}
        if self.args.params:
            self.params = self.args.params
        self.models = []
        if self.args.models:
            self.models = self.args.models.copy()
        self.workers = []
        if self.args.workers:
            self.workers = self.args.workers
        self.user = None
        self.user_ip = request.remote_addr
        # For now this is checked on validate()
        self.safe_ip = True
        self.validate()
        #logger.warning(datetime.utcnow())
        self.initiate_waiting_prompt()
        #logger.warning(datetime.utcnow())
        self.activate_waiting_prompt()
        #logger.warning(datetime.utcnow())

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        if maintenance.active:
            raise e.MaintenanceMode('Generate')
        with HORDE.app_context():  # TODO DOUBLE CHECK THIS
            #logger.warning(datetime.utcnow())
            if self.args.apikey:
                self.user = database.find_user_by_api_key(self.args['apikey'])
            #logger.warning(datetime.utcnow())
            if not self.user:
                raise e.InvalidAPIKey('generation')
            self.username = self.user.get_unique_alias()
            #logger.warning(datetime.utcnow())
            if self.args['prompt'] == '':
                raise e.MissingPrompt(self.username)
            if self.user.is_anon():
                wp_count = database.count_waiting_requests(self.user,self.args["models"])
                #logger.warning(datetime.utcnow())
            else:
                wp_count = database.count_waiting_requests(self.user)
                #logger.warning(datetime.utcnow())
            if len(self.workers):
                for worker_id in self.workers:
                    if not database.find_worker_by_id(worker_id):
                        raise e.WorkerNotFound(worker_id)
            #logger.warning(datetime.utcnow())
            n = 1
            if self.args.params:
                n = self.args.params.get('n',1)
            user_limit = self.user.get_concurrency(self.args["models"],database.retrieve_available_models)
            #logger.warning(datetime.utcnow())
            if wp_count + n > user_limit:
                raise e.TooManyPrompts(self.username, wp_count + n, user_limit)
            ip_timeout = CounterMeasures.retrieve_timeout(self.user_ip)
            #logger.warning(datetime.utcnow())
            if ip_timeout:
                raise e.TimeoutIP(self.user_ip, ip_timeout)
            #logger.warning(datetime.utcnow())
            prompt_suspicion = 0
            prompt_suspicion_words = []
            if "###" in self.args.prompt:
                prompt, negprompt = self.args.prompt.split("###", 1)
            else:
                prompt = self.args.prompt
            for blacklist_regex in [regex_blacklists1, regex_blacklists2]:
                for blacklist in blacklist_regex:
                    blacklist_match = blacklist.search(prompt)
                    if blacklist_match:
                        prompt_suspicion += 1
                        prompt_suspicion_words += blacklist_match[0]
                        break
            #logger.warning(datetime.utcnow())
            if prompt_suspicion >= 2:
                # Moderators do not get ip blocked to allow for experiments
                if not self.user.moderator:
                    self.user.report_suspicion(1,Suspicions.CORRUPT_PROMPT)
                    CounterMeasures.report_suspicion(self.user_ip)
                raise e.CorruptPrompt(self.username, self.user_ip, prompt, prompt_suspicion_words)

    
    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            self.workers,
            self.models,
            prompt = self.args["prompt"],
            user_id = self.user.id,
            params = self.params,
            nsfw = self.args.nsfw,
            censor_nsfw = self.args.censor_nsfw,
            trusted_workers = self.args.trusted_workers,
            ipaddr = self.user_ip,
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
        try:
            super().post()
        except KeyError:
            logger.error(f"caught missing Key.")
            logger.error(self.args)
            logger.error(self.args.params)
            return {"message": "Internal Server Error"},500
        ret_dict = {"id":self.wp.id}
        if not database.wp_has_valid_workers(self.wp, self.workers) and not raid.active:
            ret_dict['message'] = self.get_size_too_big_message()
        return(ret_dict, 202)

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Please confider reducing its size of the request.")


class SyncGenerate(GenerateTemplate):

    # @api.expect(parsers.generate_parser, models.input_model_request_generation, validate=True)
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    # @api.marshal_with(models.response_model_wp_status_full, code=200, description='Images Generated')
    # @api.response(400, 'Validation Error', models.response_model_error)
    # @api.response(401, 'Invalid API Key', models.response_model_error)
    # @api.response(503, 'Maintenance Mode', models.response_model_error)
    @api.response(501, 'Decommissioned', models.response_model_error)
    def post(self):
        '''THIS ENDPOINT HAS BEEN DECOMMISSIONED.
        Initiate a Synchronous request to generate images
        This connection will only terminate when the images have been generated, or an error occured.
        If you connection is interrupted, you will not have the request UUID, so you cannot retrieve the images asynchronously.
        '''
        return({"message": "This functionality has been decommissioned. Please use /api/v2/generate/async instead"}, 501)
        super().post()
        while True:
            time.sleep(1)
            if self.wp.is_stale():
                raise e.RequestExpired(self.username)
            if self.wp.is_completed():
                break
            # logger.debug(self.wp.is_completed())
        ret_dict = self.wp.get_status(
            request_avg=stats.get_request_avg(database.get_worker_performances()),
            has_valid_workers=database.wp_has_valid_workers(self.wp, self.workers),
            wp_queue_stats=database.get_wp_queue_stats(self.wp),
            active_worker_count=database.count_active_workers()
        )
        return(ret_dict, 200)

    # We extend this function so we can check if any workers can fulfil the request, before adding it to the queue
    def activate_waiting_prompt(self):
        # We don't want to keep synchronous requests up unless there's someone who can fulfill them
        if not database.wp_has_valid_workers(self.wp, self.workers):
            # We don't need to call .delete() on the wp because it's not activated yet
            # And therefore not added to the waiting_prompt dict.
            self.wp.delete()
            raise e.NoValidWorkers(self.username)
        # if a worker is available to fulfil this prompt, we activate it and add it to the queue to be generated
        super().activate_waiting_prompt()

class AsyncStatus(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    decorators = [limiter.limit("10/minute", key_func = get_request_path)]
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id = ''):
        '''Retrieve the full status of an Asynchronous generation request.
        This request will include all already generated images in download URL or base64 encoded .webp files.
        As such, you are requested to not retrieve this endpoint often. Instead use the /check/ endpoint first
        This endpoint is limited to 10 request per minute
        '''
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status(
            request_avg=stats.get_request_avg(database.get_worker_performances()),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers()
        )
        # If the status is retrieved after the wp is done we clear it to free the ram
        # FIXME: I pevent it at the moment due to the race conditions
        # The WPCleaner is going to clean it up anyway
        # if wp_status["done"]:
            # wp.delete()
        return(wp_status, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def delete(self, id = ''):
        '''Cancel an unfinished request.
        This request will include all already generated images in base64 encoded .webp files.
        '''
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status(
            request_avg=stats.get_request_avg(database.get_worker_performances()),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers()
        )
        logger.info(f"Request with ID {wp.id} has been cancelled.")
        # FIXME: I pevent it at the moment due to the race conditions
        # The WPCleaner is going to clean it up anyway
        wp.n = 0
        db.session.commit()
        return(wp_status, 200)


class AsyncCheck(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    # Increasing this until I can figure out how to pass original IP from reverse proxy
    decorators = [limiter.limit("10/second", key_func = get_request_path)]
    @cache.cached(timeout=1)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_wp_status_lite, code=200, description='Async Request Status Check')
    # @cache.cached(timeout=0.5)
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id):
        '''Retrieve the status of an Asynchronous generation request without images.
        Use this request to check the status of a currently running asynchronous request without consuming bandwidth.
        '''
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        lite_status = wp.get_lite_status(
            request_avg=stats.get_request_avg(database.get_worker_performances()),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers()
        )
        return(lite_status, 200)


class JobPopTemplate(Resource):

    # We split this into its own function, so that it may be overriden and extended
    def validate(self, worker_class = Worker):
        self.skipped = {}
        self.user = database.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('prompt pop')
        self.worker_name = sanitize_string(self.args['name'])
        self.worker = database.find_worker_by_name(self.worker_name, worker_class=worker_class)
        if not self.worker and database.worker_name_exists(self.worker_name):
            raise e.PolymorphicNameConflict(self.worker_name)
        self.safe_ip = True
        if not self.worker or not (self.worker.user.trusted or patrons.is_patron(self.worker.user.id)):
            self.safe_ip = CounterMeasures.is_ip_safe(self.worker_ip)
            if self.safe_ip is None:
                raise e.TooManyNewIPs(self.worker_ip)
            if self.safe_ip is False:
                # Outside of a raid, we allow 1 worker in unsafe IPs from untrusted users. They will have to explicitly request it via discord
                # EDIT # Below line commented for now, which means we do not allow any untrusted workers at all from untrusted users
                # if not raid.active and database.count_workers_in_ipaddr(self.worker_ip) == 0:
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
                # raise e.TooManySameIPs(self.user.username) # TODO: Renable when IP works
                pass
            self.worker = worker_class(
                user_id=self.user.id,
                name=self.worker_name,
            )
            self.worker.create()
        if self.user != self.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.worker_name)


class JobPop(JobPopTemplate):

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
        # logger.warning(datetime.utcnow())
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
        # self.priority_users = [self.user]
        ## Start prioritize by bridge request ##

        pre_priority_user_ids = [x.split("#")[-1] for x in self.priority_usernames]
        self.priority_user_ids = [self.user.id]
        # TODO move to database class
        p_users_id_from_db = db.session.query(User.id).filter(User.id.in_(pre_priority_user_ids)).all()
        if p_users_id_from_db:
            self.priority_user_ids.extend([x.id for x in p_users_id_from_db])

        # for priority_username in self.priority_usernames:
        #     priority_user = database.find_user_by_username(priority_username)
        #     if priority_user:
        #        self.priority_users.append(priority_user)

        wp_list = db.session.query(WaitingPrompt).filter(WaitingPrompt.user_id.in_(self.priority_user_ids), WaitingPrompt.n > 0).all()
        for wp in wp_list:
            self.prioritized_wp.append(wp)
        # for priority_user in self.priority_users:
        #     wp_list = database.get_all_wps()
        #     for wp in wp_list:
        #         if wp.user == priority_user and wp.needs_gen():
        #             self.prioritized_wp.append(wp)
        # logger.warning(datetime.utcnow())
        ## End prioritize by bridge request ##
        for wp in self.get_sorted_wp():
            if wp not in self.prioritized_wp:
                self.prioritized_wp.append(wp)
        # logger.warning(datetime.utcnow())
        for wp in self.prioritized_wp:
            check_gen = self.worker.can_generate(wp)
            if not check_gen[0]:
                skipped_reason = check_gen[1]
                # We don't report on secret skipped reasons
                # as they're typically countermeasures to raids
                if skipped_reason != "secret":
                    self.skipped[skipped_reason] = self.skipped.get(skipped_reason,0) + 1
                #logger.warning(datetime.utcnow())
                continue
            # There is a chance that by the time we finished all the checks, another worker picked up the WP.
            # So we do another final check here before picking it up to avoid sending the same WP to two workers by mistake.
            # time.sleep(random.uniform(0, 1))
            wp.refresh()
            if not wp.needs_gen():  # this says if < 1
                continue
            worker_ret = self.start_worker(wp)
            # logger.debug(worker_ret)
            if worker_ret is None:
                continue
            # logger.debug(worker_ret)
            return(worker_ret, 200)
        # We report maintenance exception only if we couldn't find any jobs
        if self.worker.maintenance:
            raise e.WorkerMaintenance(self.worker.maintenance_msg)
        # logger.warning(datetime.utcnow())
        return({"id": None, "skipped": self.skipped}, 200)

    def get_sorted_wp(self):
        '''Extendable class to retrieve the sorted WP list for this worker'''
        return database.get_sorted_wp_filtered_to_worker(self.worker)

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

    # We split this to its own function so that it can be extended with the specific vars needed to check in
    # You typically never want to use this template's function without extending it
    def check_in(self):
        self.worker.check_in(
            nsfw = self.args['nsfw'],
            blacklist = self.args['blacklist'],
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip)

    # We split this into its own function, so that it may be overriden and extended
    def validate(self, worker_class = Worker):
        super().validate(worker_class = worker_class)
        for model in self.models:
            if is_profane(model) and not "Hentai" in model:
                raise e.Profanity(self.user.get_unique_alias(), model, 'model name')


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
        self.procgen = database.get_progen_by_id(self.args['id'])
        if not self.procgen:
            raise e.InvalidJobID(self.args['id'])
        self.user = database.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('worker submit:' + self.args['name'])
        if self.user != self.procgen.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.procgen.worker.name)
        things_per_sec = stats.record_fulfilment(self.procgen)
        self.kudos = self.procgen.set_generation(
            generation=self.args['generation'],
            things_per_sec=things_per_sec,
            seed=self.args.seed,
            censored=self.args.censored,
        )
        if self.kudos == 0 and not self.procgen.worker.maintenance:
            raise e.DuplicateGen(self.procgen.worker.name, self.args['id'])


class TransferKudos(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The sending user's API key", location='headers')
    parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos", location="json")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to transfer", location="json")

    @api.expect(parser)
    @api.marshal_with(models.response_model_kudos_transfer, code=200, description='Kudos Transferred')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    def post(self):
        '''Transfer Kudos to another registed user
        '''
        self.args = self.parser.parse_args()
        user = database.find_user_by_api_key(self.args['apikey'])
        if not user:
            raise e.InvalidAPIKey('kudos transfer to: ' + self.args['username'])
        ret = database.transfer_kudos_from_apikey_to_username(self.args['apikey'],self.args['username'],self.args['amount'])
        kudos = ret[0]
        error = ret[1]
        if error != 'OK':
            raise e.KudosValidationError(user.get_unique_alias(), error)
        return({"transferred": kudos}, 200)


class AwardKudos(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The sending user's API key", location='headers')
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos", location="json")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to award", location="json")

    @api.expect(parser)
    @api.marshal_with(models.response_model_kudos_award, code=200, description='Kudos Awarded')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def post(self):
        '''Award Kudos to registed user
        '''
        self.args = self.parser.parse_args()
        user = database.find_user_by_api_key(self.args['apikey'])
        if not user:
            raise e.InvalidAPIKey('kudos transfer to: ' + self.args['username'])
        if user.id not in [1, 2047]:
            raise e.NotPrivileged(user.get_unique_alias(), "Only special people can award kudos. Now you're very special as well, just not the right kind.", "AwardKudos")
        dest_user = database.find_user_by_username(self.args['username'])
        if not dest_user:
            raise e.KudosValidationError(user.get_unique_alias(), 'Invalid target username.', 'award')
        if dest_user.is_anon():
            raise e.KudosValidationError(user.get_unique_alias(), 'Cannot award anon. No go.', 'award')
        if dest_user.is_suspicious():
            return([0,'Target user is suspicious.'])
        dest_user.modify_kudos(self.args.amount, "awarded")
        return({"awarded": self.args.amount}, 200)

class Workers(Resource):
    @logger.catch(reraise=True)
    @cache.cached(timeout=10)
    @api.marshal_with(models.response_model_worker_details, code=200, description='Workers List', as_list=True, skip_none=True)
    def get(self):
        '''A List with the details of all registered and active workers
        '''
        return (self.retrieve_workers_details(),200)

    @logger.catch(reraise=True)
    def retrieve_workers_details(self):
        cached_workers = horde_r.get('worker_cache')
        if cached_workers is None:
            workers_ret = []
            for worker in database.get_active_workers():
                workers_ret.append(worker.get_details())
            return workers_ret
        return json.loads(cached_workers)

class WorkerSingle(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

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
        worker = database.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = database.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('admin worker details')
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorWorkerDetails')
            details_privilege = 2
        worker_details = worker.get_details(details_privilege)
        # logger.debug(worker_details)
        return worker_details,200

    put_parser = reqparse.RequestParser()
    put_parser.add_argument("apikey", type=str, required=True, help="The Moderator or Owner API key", location='headers')
    put_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
    put_parser.add_argument("maintenance", type=bool, required=False, help="Set to true to put this worker into maintenance.", location="json")
    put_parser.add_argument("maintenance_msg", type=str, required=False, help="if maintenance is True, You can optionally provide a message to be used instead of the default maintenance message, so that the owner is informed", location="json")
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
        worker = database.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        self.args = self.put_parser.parse_args()
        admin = database.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PUT WorkerSingle')
        ret_dict = {}
        # Both mods and owners can set the worker to maintenance
        if self.args.maintenance is not None:
            if not admin.moderator and admin != worker.user:
                raise e.NotOwner(admin.get_unique_alias(), worker.name)
            worker.toggle_maintenance(self.args.maintenance, self.args.maintenance_msg)
            ret_dict["maintenance"] = worker.maintenance
        # Only owners can set info notes
        if self.args.info is not None:
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
        if self.args.paused is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT WorkerSingle')
            worker.toggle_paused(self.args.paused)
            ret_dict["paused"] = worker.paused
        if self.args.name is not None:
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
        if self.args.team is not None:
            if not admin.moderator and admin != worker.user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT WorkerSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            if self.args.team == '':
                worker.set_team(None)
                ret_dict["team"] = 'None'
            else:
                team = database.find_team_by_id(self.args.team)
                if not team:
                    raise e.TeamNotFound(self.args.team)
                ret = worker.set_team(team)
                ret_dict["team"] = team.name
        if not len(ret_dict):
            raise e.NoValidActions("No worker modification selected!")
        return(ret_dict, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")


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
        worker = database.find_worker_by_id(worker_id)
        if not worker:
            raise e.WorkerNotFound(worker_id)
        self.args = self.delete_parser.parse_args()
        admin = database.find_user_by_api_key(self.args['apikey'])
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
        try:
            worker.delete()
        except IntegrityError:
            raise e.Locked("Could not delete the worker at this point as it's referenced by a job it completed. Please try again after 20 mins.")
        return(ret_dict, 200)

class Users(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    decorators = [limiter.limit("30/minute")]
    @cache.cached(timeout=10)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_user_details, code=200, description='Users List')
    def get(self): # TODO - Should this be exposed?
        '''A List with the details and statistic of all registered users
        '''
        return ([],200) #FIXME: Debat
        all_users = db.session.query(User)
        users_list = [user.get_details() for user in all_users]
        return(users_list,200)


class UserSingle(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Admin, Mod or Owner API key", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    decorators = [limiter.limit("60/minute", key_func = get_request_path)]
    @api.expect(get_parser)
    @cache.cached(timeout=3)
    @api.marshal_with(models.response_model_user_details, code=200, description='User Details', skip_none=True)
    @api.response(404, 'User Not Found', models.response_model_error)
    def get(self, user_id = ''):
        '''Details and statistics about a specific user
        '''
        user = database.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = database.find_user_by_api_key(self.args['apikey'])
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
    parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
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
        user = user = database.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        self.args = self.parser.parse_args()
        admin = database.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'PUT UserSingle')
        ret_dict = {}
        # Admin Access
        if self.args.kudos is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.modify_kudos(self.args.kudos, 'admin')
            ret_dict["new_kudos"] = user.kudos
        if self.args.monthly_kudos is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.modify_monthly_kudos(self.args.monthly_kudos)
            ret_dict["monthly_kudos"] = user.monthly_kudos
        if self.args.usage_multiplier is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.usage_multiplier = self.args.usage_multiplier
            ret_dict["usage_multiplier"] = user.usage_multiplier
        if self.args.moderator is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_moderator(self.args.moderator)
            ret_dict["moderator"] = user.moderator
        # Moderator Access
        if self.args.concurrency is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.concurrency = self.args.concurrency
            ret_dict["concurrency"] = user.concurrency
        if self.args.worker_invite is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.worker_invited = self.args.worker_invite
            ret_dict["worker_invited"] = user.worker_invited
        if self.args.trusted is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_trusted(self.args.trusted)
            ret_dict["trusted"] = user.trusted
        if self.args.reset_suspicion is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.reset_suspicion()
            ret_dict["new_suspicion"] = user.get_suspicion()
        # User Access
        if self.args.public_workers is not None:
            if not admin.moderator and admin != user:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            if admin.is_anon():
                raise e.AnonForbidden()
            user.public_workers = self.args.public_workers
            ret_dict["public_workers"] = user.public_workers
        if self.args.username is not None:
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
        if self.args.contact is not None:
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
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @api.expect(get_parser)
    @api.marshal_with(models.response_model_user_details, code=200, description='Worker Details', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(404, 'User Not Found', models.response_model_error)
    def get(self):
        '''Lookup user details based on their API key
        This can be used to verify a user exists
        '''
        self.args = self.get_parser.parse_args()
        if not self.args.apikey:
            raise e.InvalidAPIKey('GET FindUser')
        user = database.find_user_by_api_key(self.args.apikey)
        if not user:
            raise e.UserNotFound(self.args.apikey, 'api_key')
        return(user.get_details(1),200)


class Models(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_active_model, code=200, description='List All Active Models', as_list=True)
    def get(self):
        '''Returns a list of models active currently in this horde
        '''
        return(database.retrieve_available_models(),200)


class HordeLoad(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Performance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = database.retrieve_totals()
        load_dict["worker_count"], load_dict["thread_count"] = database.count_active_workers()
        return(load_dict,200)

class HordeNews(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=300)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_newspiece, code=200, description='Horde News', as_list = True)
    def get(self):
        '''Read the latest happenings on the horde
        '''
        news = News()
        # logger.debug(news.sorted_news())
        return(news.sorted_news(),200)


class HordeModes(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Admin or Owner API key", location='headers')

    @api.expect(get_parser)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
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
            admin = database.find_user_by_api_key(self.args['apikey'])
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
        admin = database.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('Admin action: ' + 'PUT HordeModes')
        ret_dict = {}
        if self.args.maintenance is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT HordeModes')
            maintenance.toggle(self.args.maintenance)
            logger.critical(f"Horde entered maintenance mode")
            for wp in database.get_all_wps():
                wp.abort_for_maintenance()
            ret_dict["maintenance_mode"] = maintenance.active
        #TODO: Replace this with a node-offline call
        if self.args.shutdown is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT HordeModes')
            maintenance.activate()
            for wp in database.get_all_wps():
                wp.abort_for_maintenance()
            database.shutdown(self.args.shutdown)
            ret_dict["maintenance_mode"] = maintenance.active
        if self.args.invite_only is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT HordeModes')
            invite_only.toggle(self.args.invite_only)
            ret_dict["invite_only_mode"] = invite_only.active
        if self.args.raid is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT HordeModes')
            raid.toggle(self.args.raid)
            ret_dict["raid_mode"] = raid.active
        if not len(ret_dict):
            raise e.NoValidActions("No mod change selected!")
        return(ret_dict, 200)

class Teams(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    # decorators = [limiter.limit("20/minute")]
    @logger.catch(reraise=True)
    @cache.cached(timeout=10)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_team_details, code=200, description='Teams List', as_list=True, skip_none=True)
    def get(self):
        '''A List with the details of all teams
        '''
        teams_ret = []
        # I could do this with a comprehension, but this is clearer to understand
        for team in database.get_all_teams():
            teams_ret.append(team.get_details())
        return(teams_ret,200)

    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="A User API key", location='headers')
    post_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
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
        self.user = database.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('User action: ' + 'PUT Teams')
        if self.user.is_anon():
            raise e.AnonForbidden()
        if not self.user.trusted:
            raise e.NotTrusted
        ret_dict = {}

        self.team_name = sanitize_string(self.args.name)
        self.team = database.find_team_by_name(self.team_name)
        self.team_info = self.args.info
        if self.team_info is not None:
            self.team_info = sanitize_string(self.team_info)

        if self.team:
            raise e.NameAlreadyExists(self.user.get_unique_alias(), self.team_name, self.args.name, 'team')
        if is_profane(self.team_name):
            raise e.Profanity(self.user.get_unique_alias(), self.team_name, 'team name')
        if self.team_info and is_profane(self.team_info):
            raise e.Profanity(self.user.get_unique_alias(), self.team_info, 'team info')
        team = Team(
            owner_id=self.user.id,
            name=self.team_name,
            info=self.team_info,
        )
        team.create()
        ret_dict["name"] = self.team_name
        ret_dict["info"] = self.team_info
        ret_dict["id"] = team.id
        return(ret_dict, 200)


class TeamSingle(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @api.expect(get_parser)
    @cache.cached(timeout=3)
    @api.marshal_with(models.response_model_team_details, code=200, description='Team Details', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Team Not Found', models.response_model_error)
    def get(self, team_id = ''):
        '''Details of a worker Team'''
        team = database.find_team_by_id(team_id)
        if not team:
            raise e.TeamNotFound(team_id)
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = database.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('admin team details')
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorTeamDetails')
            details_privilege = 2
        return(team.get_details(details_privilege),200)

    patch_parser = reqparse.RequestParser()
    patch_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Creator API key", location='headers')
    patch_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
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
        team = database.find_team_by_id(team_id)
        if not team:
            raise e.TeamNotFound(team_id)
        self.args = self.patch_parser.parse_args()
        admin = database.find_user_by_api_key(self.args['apikey'])
        if not admin:
            raise e.InvalidAPIKey('User action: ' + 'PATCH TeamSingle')
        ret_dict = {}
        # Only creators can set info notes
        if self.args.info is not None:
            if not admin.moderator and admin != team.user:
                raise e.NotOwner(admin.get_unique_alias(), team.name)
            ret = team.set_info(self.args.info)
            if ret == "Profanity":
                raise e.Profanity(admin.get_unique_alias(), self.args.info, 'team info')
            ret_dict["info"] = team.info
        if self.args.name is not None:
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
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
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
        team = database.find_team_by_id(team_id)
        if not team:
            raise e.TeamNotFound(team_id)
        self.args = self.delete_parser.parse_args()
        admin = database.find_user_by_api_key(self.args['apikey'])
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
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
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
        mod = database.find_user_by_api_key(self.args['apikey'])
        if not mod:
            raise e.InvalidAPIKey('User action: ' + 'DELETE OperationsIP')
        if not mod.moderator:
            raise e.NotModerator(mod.get_unique_alias(), 'DELETE OperationsIP')
        CounterMeasures.delete_timeout(self.args.ipaddr)
        return({"message":'OK'}, 200)


class Heartbeat(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    decorators = [limiter.exempt]
    @api.expect(get_parser)
    def get(self):
        '''If this loads, this node is available
        '''
        return({'message': 'OK'},200)
