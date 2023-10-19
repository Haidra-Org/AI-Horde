import json
import os
import regex as re
import time
import random
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError,InvalidRequestError
from sqlalchemy import literal
from sqlalchemy import func, or_, and_

from horde.database import functions as database
from horde.classes.base import settings
from flask import request
from flask_restx import Namespace, Resource, reqparse
from horde.flask import cache, db, HORDE
from horde.limiter import limiter
from horde.logger import logger
from horde.argparser import args
from horde import exceptions as e
from horde.classes.base.user import User, UserSharedKey
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.classes.base.worker import Worker
import horde.classes.base.stats as stats
from horde.classes.base.team import Team
from horde.classes.base.news import News
from horde.classes.base.detection import Filter
from horde.suspicions import Suspicions
from horde.utils import is_profane, sanitize_string, hash_api_key, hash_dictionary
from horde.countermeasures import CounterMeasures
from horde import horde_redis as hr
from horde.patreon import patrons
from horde.detection import prompt_checker
from horde.r2 import upload_prompt
from horde.consts import HORDE_VERSION

# Not used yet
authorizations = {
    'apikey': {
        'type': 'apiKey',
        'in': 'header',
        'name': 'apikey'
    }
}

api = Namespace('v2', 'API Version 2' )

from horde.apis.models.v2 import Models, Parsers

models = Models(api)
parsers = Parsers()

handle_bad_request = api.errorhandler(e.BadRequest)(e.handle_bad_requests)
handle_forbidden = api.errorhandler(e.Forbidden)(e.handle_bad_requests)
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
handle_thing_not_found = api.errorhandler(e.ThingNotFound)(e.handle_bad_requests)
handle_user_not_found = api.errorhandler(e.UserNotFound)(e.handle_bad_requests)
handle_duplicate_gen = api.errorhandler(e.DuplicateGen)(e.handle_bad_requests)
handle_aborted_gen = api.errorhandler(e.AbortedGen)(e.handle_bad_requests)
handle_request_expired = api.errorhandler(e.RequestExpired)(e.handle_bad_requests)
handle_too_many_prompts = api.errorhandler(e.TooManyPrompts)(e.handle_bad_requests)
handle_no_valid_workers = api.errorhandler(e.NoValidWorkers)(e.handle_bad_requests)
handle_no_valid_actions = api.errorhandler(e.NoValidActions)(e.handle_bad_requests)
handle_maintenance_mode = api.errorhandler(e.MaintenanceMode)(e.handle_bad_requests)
locked = api.errorhandler(e.Locked)(e.handle_bad_requests)

# Used to for the flask limiter, to limit requests per url paths
def get_request_path():
    # logger.info(dir(request))
    return f"{request.remote_addr}@{request.method}@{request.path}"

def get_request_api_key():
    apikey = hash_api_key(request.headers.get("apikey", 0000000000))
    return f"{apikey}@{request.method}@{request.path}"


def check_for_mod(api_key, operation, whitelisted_users = None):
    mod = database.find_user_by_api_key(api_key)
    if not mod:
        raise e.InvalidAPIKey('User action: ' + operation)
    if not mod.moderator and not args.insecure:
        if whitelisted_users and mod.get_unique_alias() in whitelisted_users:
            return mod
        raise e.NotModerator(mod.get_unique_alias(), operation)
    return mod


# I have to put it outside the class as I can't figure out how to extend the argparser and also pass it to the @api.expect decorator inside the class
class GenerateTemplate(Resource):
    gentype = "template"
    def post(self):
        #logger.warning(datetime.utcnow())
        # I have to extract and store them this way, because if I use the defaults
        # It causes them to be a shared object from the parsers class
        self.params = {}
        if self.args.params:
            self.params = self.args.params
        self.models = []
        if self.args.models:
            self.models = self.args.models.copy()
        params_hash = self.get_hashed_params_dict()
        cached_payload_kudos_calc = hr.horde_r_get(f"payload_kudos_{params_hash}")
        if cached_payload_kudos_calc and self.args.dry_run:
            self.kudos = float(cached_payload_kudos_calc)
            return 
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
        if self.args.dry_run:
            self.kudos = self.extrapolate_dry_run_kudos()
            self.wp.delete()
            return
        self.activate_waiting_prompt()
        # We use the wp.kudos to avoid calling the model twice.
        self.kudos = self.wp.kudos
        #logger.warning(datetime.utcnow())

    # Extend if extra payload information needs to be sent
    def extrapolate_dry_run_kudos(self):
        kudos = self.wp.extrapolate_dry_run_kudos()
        params_hash = self.get_hashed_params_dict()
        hr.horde_r_setex(f"payload_kudos_{params_hash}", timedelta(days=2), kudos)
        return kudos

    # Override if extra payload information needs to be sent
    def get_hashed_params_dict(self):
        '''We create a simulacra dictionary of the WP payload to cache in redis with the expected kudos cache
        This avoids us having to create a WP object just to get the parameters dict.
        This is needed because some parameters are injected into the dict for the model, during runtime.
        So we need the logic of each GenerateTemplate class to be able to override this class to adjust the params dict accordingly.
        '''
        gen_payload = self.params.copy()
        gen_payload["models"] = self.models
        params_hash = hash_dictionary(gen_payload)
        return params_hash

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        if settings.mode_maintenance():
            raise e.MaintenanceMode('Generate')
        with HORDE.app_context():  # TODO DOUBLE CHECK THIS
            #logger.warning(datetime.utcnow())
            if self.args.apikey:
                self.sharedkey = database.find_sharedkey(self.args.apikey)
                if self.sharedkey:
                    is_valid, error_msg = self.sharedkey.is_valid()
                    if not is_valid:
                        raise e.Forbidden(error_msg)
                    self.user = self.sharedkey.user
                if not self.user:
                    self.user = database.find_user_by_api_key(self.args.apikey)
            #logger.warning(datetime.utcnow())
            if not self.user:
                raise e.InvalidAPIKey('generation')
            self.username = self.user.get_unique_alias()
            #logger.warning(datetime.utcnow())
            if self.args['prompt'] == '':
                raise e.MissingPrompt(self.username)
            if self.user.is_anon():
                wp_count = database.count_waiting_requests(
                    user = self.user,
                    models = self.args["models"],
                    request_type = self.gentype
                )
                #logger.warning(datetime.utcnow())
            else:
                wp_count = database.count_waiting_requests(
                    user = self.user,                    
                    request_type = self.gentype,
                )
                #logger.warning(datetime.utcnow())
            if len(self.workers):
                for worker_id in self.workers:
                    if not database.worker_exists(worker_id):
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
            prompt_suspicion, _ = prompt_checker(self.args.prompt)
            #logger.warning(datetime.utcnow())
            prompt_replaced = False
            if prompt_suspicion >= 2 and self.gentype != "text":
                # if replacement filter mode is enabled AND prompt is short enough, do that instead
                if self.args.replacement_filter:
                    if not prompt_checker.check_prompt_replacement_length(self.args.prompt):
                        raise e.BadRequest("Prompt has to be below 1000 chars when replacement filter is on")
                    self.args.prompt = prompt_checker.apply_replacement_filter(self.args.prompt)
                    # If it returns None, it means it replaced everything with an empty string
                    if self.args.prompt is not None:
                        prompt_replaced = True
                if not prompt_replaced:
                    # Moderators do not get ip blocked to allow for experiments
                    if not self.user.moderator:
                        prompt_dict = {
                            "prompt": self.args.prompt,
                            "user": self.username,
                            "type": "regex",
                        }
                        upload_prompt(prompt_dict)
                        self.user.report_suspicion(1,Suspicions.CORRUPT_PROMPT)
                        CounterMeasures.report_suspicion(self.user_ip)
                    raise e.CorruptPrompt(self.username, self.user_ip, self.args.prompt)
            if prompt_checker.check_nsfw_model_block(self.args.prompt, self.models):
                # For NSFW models, we always do replacements
                # This is to avoid someone using the NSFW models to figure out the regex since they don't have an IP timeout
                self.args.prompt = prompt_checker.nsfw_model_prompt_replace(self.args.prompt, self.models, already_replaced=prompt_replaced)
                if self.args.prompt is None:
                    prompt_replaced = False
                elif prompt_replaced is False:
                    prompt_replaced = True
                if not prompt_replaced:
                    raise e.CorruptPrompt(
                        self.username, 
                        self.user_ip, 
                        self.args.prompt, 
                        message = "To prevent generation of unethical images, we cannot allow this prompt with NSFW models. Please select another model and try again.")
            # Disabling as this is handled by the worker-csam-filter now
            # If I re-enable it, also make it use the prompt replacement
            # if not prompt_replaced:
            #     csam_trigger_check = prompt_checker.check_csam_triggers(self.args.prompt)
            #     if csam_trigger_check is not False and self.gentype != "text":
            #         raise e.CorruptPrompt(
            #             self.username, 
            #             self.user_ip, 
            #             self.args.prompt, 
            #             message = f"The trigger '{csam_trigger_check}' has been detected to generate unethical images on its own and as such has had to be prevented from use. Thank you for understanding.")

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 20 minutes. Please confider reducing its size of the request.")

    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            worker_ids = self.workers,
            models = self.models,
            prompt = self.args["prompt"],
            user_id = self.user.id,
            params = self.params,
            nsfw = self.args.nsfw,
            censor_nsfw = self.args.censor_nsfw,
            trusted_workers = self.args.trusted_workers,
            worker_blacklist = self.args.worker_blacklist,
            ipaddr = self.user_ip,
            sharedkey_id = self.args.apikey if self.sharedkey else None,
        )
    
    # We split this into its own function, so that it may be overriden and extended
    def activate_waiting_prompt(self):
        self.wp.activate()

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
            request_avg=database.get_request_avg(),
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

class JobPopTemplate(Resource):
    worker_class = Worker

    def post(self):
        # I have to extract and store them this way, because if I use the defaults
        # It causes them to be a shared object from the parsers class
        self.priority_usernames = []
        if self.args.priority_usernames:
            self.priority_usernames = self.args.priority_usernames
            if any("#" not in user_id for user_id in self.priority_usernames):
                raise e.BadRequest("Priority usernames need to be provided in the form of 'alias#number'. Example: 'db0#1'")
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
        self.wp_page = 0
        wp_list = self.get_sorted_wp(self.priority_user_ids)
        for wp in wp_list:
            self.prioritized_wp.append(wp)
        ## End prioritize by bridge request ##
        for wp in self.get_sorted_wp():
            if wp.id not in [wp.id for wp in self.prioritized_wp]:
                self.prioritized_wp.append(wp)
        # logger.warning(datetime.utcnow())
        while len(self.prioritized_wp) > 0:
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
                if not wp.needs_gen():  # this says if < 1
                    continue
                worker_ret = self.start_worker(wp)
                # logger.debug(worker_ret)
                if worker_ret is None:
                    continue
                # logger.debug(worker_ret)
                return worker_ret, 200
            self.wp_page += 1
            self.prioritized_wp = self.get_sorted_wp()
            logger.debug(f"Couldn't find WP. Checking next page: {self.wp_page}")
        # We report maintenance exception only if we couldn't find any jobs
        if self.worker.maintenance:
            raise e.WorkerMaintenance(self.worker.maintenance_msg)
        # logger.warning(datetime.utcnow())
        return({"id": None, "skipped": self.skipped}, 200)

    def get_sorted_wp(self,priority_user_ids=None):
        '''Extendable class to retrieve the sorted WP list for this worker'''
        return database.get_sorted_wp_filtered_to_worker(
            self.worker,
            priority_user_ids=priority_user_ids,
            page=self.wp_page
        )

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
            require_upfront_kudos = self.args['require_upfront_kudos'], 
            blacklist = self.args['blacklist'], 
            safe_ip = self.safe_ip, 
            ipaddr = self.worker_ip)

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        self.skipped = {}
        self.user = database.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('prompt pop')
        if self.user.flagged:
            raise e.WorkerMaintenance("Your user has been flagged by our community for suspicious activity. Please contact us on discord: https://discord.gg/3DxrhksKzn")
        if self.user.is_anon():
            raise e.AnonForbidden
        self.worker_name = sanitize_string(self.args['name'])
        self.worker = database.find_worker_by_name(self.worker_name, worker_class=self.worker_class)
        if not self.worker and database.worker_name_exists(self.worker_name):
            raise e.PolymorphicNameConflict(self.worker_name)
        self.check_ip()
        if not self.worker:
            if is_profane(self.worker_name):
                raise e.Profanity(self.user.get_unique_alias(), self.worker_name, 'worker name')
            if is_profane(self.args.bridge_agent):
                raise e.Profanity(self.user.get_unique_alias(), self.args.bridge_agent, 'bridge agent')
            colab_search = re.compile(r"colab|tpu|google", re.IGNORECASE)
            cs = colab_search.search(self.worker_name)
            if cs:
                raise e.BadRequest(f"To avoid unwanted attention, please do not use '{cs.group()}' in your worker names.")
            worker_count = self.user.count_workers()
            if settings.mode_invite_only() and worker_count >= self.user.worker_invited:
                raise e.WorkerInviteOnly(worker_count)
            # Untrusted users can only have 3 workers
            if not self.user.trusted and worker_count > 3:
                raise e.Forbidden("To avoid abuse, untrusted users can only have up to 3 distinct workers.")
            # Trusted users can have up to 20 workers by default unless overriden
            if worker_count > 20 and worker_count > self.user.worker_invited:
                raise e.Forbidden("To avoid abuse, tou cannot onboard more than 20 workers as a trusted user. Please contact us on Discord to adjust.")
            if self.user.exceeding_ipaddr_restrictions(self.worker_ip):
                raise e.TooManySameIPs(self.user.username)
            self.worker = self.worker_class(
                user_id=self.user.id,
                name=self.worker_name,
            )
            self.worker.create()
        if self.user != self.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.worker_name)

    def check_ip(self):
        ip_timeout = CounterMeasures.retrieve_timeout(self.worker_ip)
        if ip_timeout:
            raise e.TimeoutIP(self.worker_ip, ip_timeout, connect_type='Worker')
        self.safe_ip = True
        if not self.user.trusted and not self.user.vpn and not patrons.is_patron(self.user.id):
            self.safe_ip = CounterMeasures.is_ip_safe(self.worker_ip)
            if self.safe_ip is None:
                raise e.TooManyNewIPs(self.worker_ip)
            if self.safe_ip is False:
                # Outside of a raid, we allow 1 worker in unsafe IPs from untrusted users. They will have to explicitly request it via discord
                # EDIT # Below line commented for now, which means we do not allow any untrusted workers at all from untrusted users
                # if not raid.active and database.count_workers_in_ipaddr(self.worker_ip) == 0:
                #     self.safe_ip = True
                # if a raid is ongoing, we do not inform the suspicious IPs we detected them
                if not self.safe_ip and not settings.mode_raid():
                    raise e.UnsafeIP(self.worker_ip)


class JobSubmitTemplate(Resource):
    
    def post(self):
        self.validate()
        return({"reward": self.kudos}, 200)

    def get_progen(self):
        '''Set to its own function to it can be overwritten depending on the class'''
        return database.get_progen_by_id(self.args['id'])

    def set_generation(self):
        '''Set to its own function to it can be overwritten depending on the class'''
        things_per_sec = stats.record_fulfilment(self.procgen,self.procgen.get_things_count(self.args['generation']))
        self.kudos = self.procgen.set_generation(
            generation=self.args['generation'], 
            things_per_sec=things_per_sec, 
            seed=self.args.seed,
            state=self.args.state,
        )

    def validate(self):
        self.procgen = self.get_progen()
        if not self.procgen:
            raise e.InvalidJobID(self.args['id'])
        self.user = database.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('worker submit:' + self.args['name'])
        if self.user != self.procgen.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.procgen.worker.name)
        self.set_generation()
        if self.kudos == 0 and not self.procgen.worker.maintenance:
            raise e.DuplicateGen(self.procgen.worker.name, self.args['id'])
        if self.kudos == -1:
            # We don't want to report an error when they sent a faulted request themselves
            if self.args.state == "faulted":
                self.kudos = 0
            else:
                raise e.AbortedGen(self.procgen.worker.name, self.args['id'])


class TransferKudos(Resource):
    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The sending user's API key.", location='headers')
    parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos.", location="json")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to transfer.", location="json")

    decorators = [
        limiter.limit("1/second", key_func = get_request_api_key),
        limiter.limit("90/hour", key_func = get_request_api_key), 
    ]
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
    parser.add_argument("apikey", type=str, required=True, help="The sending user's API key.", location='headers')
    parser.add_argument("username", type=str, required=True, help="The user ID which will receive the kudos.", location="json")
    parser.add_argument("amount", type=int, required=False, default=100, help="The amount of kudos to award.", location="json")

    @api.expect(parser)
    @api.marshal_with(models.response_model_kudos_award, code=200, description='Kudos Awarded')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def post(self):
        '''Awards Kudos to registed user. 
        This API can only be used through privileged access.
        '''
        self.args = self.parser.parse_args()
        user = database.find_user_by_api_key(self.args['apikey'])
        if not user:
            raise e.InvalidAPIKey('kudos transfer to: ' + self.args['username'])
        if user.id not in {1}:
            raise e.NotPrivileged(user.get_unique_alias(), "Only special people can award kudos. Now you're very special as well, just not the right kind.", "AwardKudos")
        dest_user = database.find_user_by_username(self.args['username'])
        if not dest_user:
            raise e.KudosValidationError(user.get_unique_alias(), 'Invalid target username.', 'award')
        if dest_user.is_anon():
            raise e.KudosValidationError(user.get_unique_alias(), 'Cannot award anon. No go.', 'award')
        # if dest_user.is_suspicious():
        #     return([0,'Target user is rejected.'])
        if dest_user.flagged:
            return([0,'Target user is rejected.'])
        dest_user.modify_kudos(self.args.amount, "awarded")
        return({"awarded": self.args.amount}, 200)

class Workers(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="A Moderator API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    get_parser.add_argument("type", required=False, default=None, type=str, help="Filter the workers by type (image, text or interrogation).", location="args")

    @api.expect(get_parser)
    @logger.catch(reraise=True)
    #@cache.cached(timeout=10, query_string=True)
    @api.marshal_with(models.response_model_worker_details, code=200, description='Workers List', as_list=True, skip_none=True)
    def get(self):
        '''A List with the details of all registered and active workers
        '''
        self.args = self.get_parser.parse_args()
        return (self.retrieve_workers_details(),200)

    @logger.catch(reraise=True)
    def retrieve_workers_details(self):
        details_privilege = 0
        if self.args.apikey:
            admin = database.find_user_by_api_key(self.args['apikey'])
            if admin and admin.moderator:
                details_privilege = 2
        if not hr.horde_r:
            return self.parse_worker_by_query(self.get_worker_info_list(details_privilege))
        if details_privilege == 2:
            cached_workers = hr.horde_r_get('worker_cache_privileged')
        else:
            cached_workers = hr.horde_r_get('worker_cache')
        if cached_workers is None:
            logger.warning(f"No {details_privilege} worker cache found! Check caching thread!")
            workers = self.parse_worker_by_query(self.get_worker_info_list(details_privilege))
            if details_privilege > 0:
                hr.horde_local_setex_to_json("worker_cache_privileged", 300, workers)
            else:
                hr.horde_local_setex_to_json("worker_cache", 300, workers)
            return workers
        return self.parse_worker_by_query(json.loads(cached_workers))

    def get_worker_info_list(self, details_privilege):
        workers_ret = []
        for worker in database.get_active_workers():
            workers_ret.append(worker.get_details(details_privilege))
        return workers_ret

    def parse_worker_by_query(self, workers_list):
        if not self.args.type:
            return workers_list
        return [w for w in workers_list if w["type"] == self.args.type]

class WorkerSingle(Resource):

    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @api.expect(get_parser)
    # @cache.cached(timeout=10)
    @api.marshal_with(models.response_model_worker_details, code=200, description='Worker Details', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Worker Not Found', models.response_model_error)
    def get(self, worker_id = ''):
        '''Details of a registered worker
        Can retrieve the details of a worker even if inactive
        (A worker is considered inactive if it has not checked in for 5 minutes)
        '''
        cache_exists = True
        details_privilege = 0
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = database.find_user_by_api_key(self.args['apikey'])
            if admin and admin.moderator:
                details_privilege = 2
        if not hr.horde_r:
            cache_exists = False
        if details_privilege > 0:
            cache_name = f"cached_worker_{worker_id}_privileged"
            cached_worker = hr.horde_r_get(cache_name)
        else:
            cache_name = f"cached_worker_{worker_id}"
        cached_worker = hr.horde_r_get(cache_name)
        if cache_exists and cached_worker:
            worker_details = json.loads(cached_worker)
        else:
            worker = database.find_worker_by_id(worker_id)
            if not worker:
                raise e.WorkerNotFound(worker_id)
            worker_details = worker.get_details(details_privilege)
            hr.horde_r_setex_json(cache_name, timedelta(seconds=30), worker_details)
        return worker_details,200

    put_parser = reqparse.RequestParser()
    put_parser.add_argument("apikey", type=str, required=True, help="The Moderator or Owner API key.", location='headers')
    put_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    put_parser.add_argument("maintenance", type=bool, required=False, help="Set to true to put this worker into maintenance.", location="json")
    put_parser.add_argument("maintenance_msg", type=str, required=False, help="if maintenance is True, You can optionally provide a message to be used instead of the default maintenance message, so that the owner is informed.", location="json")
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
    delete_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key.", location='headers')
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")


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
        except (IntegrityError, InvalidRequestError):
            raise e.Locked("Could not delete the worker at this point as it's referenced by a job it completed. Please try again after 20 mins.")
        return(ret_dict, 200)

class Users(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    get_parser.add_argument("page", required=False, default=1, type=int, help="Which page of results to return. Each page has 25 users.", location="args")
    get_parser.add_argument("sort", required=False, default='kudos', type=str, help="How to sort the returned list.", location="args")

    decorators = [limiter.limit("90/minute")]
    # @cache.cached(timeout=10)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_user_details, code=200, description='Users List')
    def get(self): # TODO - Should this be exposed?
        '''A List with the details and statistic of all registered users
        '''
        self.args = self.get_parser.parse_args()
        return (self.retrieve_users_details(),200)

    @logger.catch(reraise=True)
    def retrieve_users_details(self):
        sort=self.args.sort
        page=self.args.page
        # I don't have 250K users, so might as well return immediately.
        # TODO: Adjust is I ever get more than 250K users >_<
        if page > 10000:
            return []
        if not hr.horde_r:
            return self.get_user_list(sort=sort, page=page)
        cache_name = f'users_cache_{sort}_{page}'
        cached_users = hr.horde_r_get(cache_name)
        if cached_users is None:
            logger.debug(f"No user cache found for sort: {sort} page:{page}")
            users = self.get_user_list(sort=sort, page=page)
            hr.horde_r_setex_json(cache_name, timedelta(seconds=300), users)
            return users
        return json.loads(cached_users)

    def get_user_list(self, sort="kudos", page=1):
        users_ret = []
        if sort not in ["kudos", "age"]:
            sort = "kudos"
        if page < 1:
            page = 1
        for user in database.get_all_users(sort=sort,offset=(page-1)*25):
            users_ret.append(user.get_details())
        return users_ret


class UserSingle(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=False, help="The Admin, Mod or Owner API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    decorators = [limiter.limit("60/minute", key_func = get_request_path)]
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_user_details, code=200, description='User Details', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(404, 'User Not Found', models.response_model_error)
    def get(self, user_id = ''):
        '''Details and statistics about a specific user
        '''
        if not user_id.isdigit():
            raise e.UserNotFound("Please use only the numerical part of the userID. E.g. the '1' in 'db0#1'")
        self.args = self.get_parser.parse_args()
        details_privilege = 0
        if self.args.apikey:
            resolved_user = database.find_user_by_api_key(self.args['apikey'])
            if not resolved_user:
                raise e.InvalidAPIKey('User action: ' + 'GET UserSingle')
            if resolved_user.moderator:
                details_privilege = 2
            elif str(resolved_user.id) == str(user_id):
                details_privilege = 1
        cached_user = None
        cache_name = f"cached_user_id_{user_id}_privilege_{details_privilege}"
        if hr.horde_r:
            cached_user = hr.horde_r_get(cache_name)
        if cached_user:
            user_details = json.loads(cached_user)
            if type(user_details.get("monthly_kudos",{}).get("last_received")) == str:
                user_details["monthly_kudos"]["last_received"] = datetime.fromisoformat(user_details["monthly_kudos"]["last_received"])
        else:
            user = database.find_user_by_id(user_id)
            if not user:
                raise e.UserNotFound(user_id)
            user_details = user.get_details(details_privilege)
            if hr.horde_r:
                cached_details = user_details.copy()
                if "monthly_kudos" in cached_details:
                    cached_details["monthly_kudos"] = cached_details["monthly_kudos"].copy()
                if user_details.get("monthly_kudos",{}).get("last_received"):
                    cached_details["monthly_kudos"]["last_received"] = cached_details["monthly_kudos"]["last_received"].isoformat()
                hr.horde_r_setex_json(cache_name, timedelta(seconds=30), cached_details)
        return user_details,200


    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API .", location='headers')
    parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    parser.add_argument("kudos", type=int, required=False, help="The amount of kudos to modify (can be negative).", location="json")
    parser.add_argument("concurrency", type=int, required=False, help="The amount of concurrent request this user can have.", location="json")
    parser.add_argument("usage_multiplier", type=float, required=False, help="The amount by which to multiply the users kudos consumption.", location="json")
    parser.add_argument("worker_invited", type=int, required=False, help="Set to the amount of workers this user is allowed to join to the horde when in worker invite-only mode.", location="json")
    parser.add_argument("moderator", type=bool, required=False, help="Set to true to Make this user a horde moderator.", location="json")
    parser.add_argument("public_workers", type=bool, required=False, help="Set to true to Make this user a display their worker IDs.", location="json")
    parser.add_argument("username", type=str, required=False, help="When specified, will change the username. No profanity allowed!", location="json")
    parser.add_argument("monthly_kudos", type=int, required=False, help="When specified, will start assigning the user monthly kudos, starting now!", location="json")
    parser.add_argument("trusted", type=bool, required=False, help="When set to true,the user and their servers will not be affected by suspicion.", location="json")
    parser.add_argument("flagged", type=bool, required=False, help="When set to true, the user cannot tranfer kudos and all their workers are put into permanent maintenance.", location="json")
    parser.add_argument("customizer", type=bool, required=False, help="When set to true, the user will be able to serve custom Stable Diffusion models which do not exist in the Official AI Horde Model Reference.", location="json")
    parser.add_argument("vpn", type=bool, required=False, help="When set to true, the user will be able to onboard workers behind a VPN. This should be used as a temporary solution until the user is trusted.", location="json")
    parser.add_argument("special", type=bool, required=False, help="When set to true, the user will be marked as special.", location="json")
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
            db.session.commit()
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
            db.session.commit()
            ret_dict["concurrency"] = user.concurrency
        if self.args.worker_invited is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.worker_invited = self.args.worker_invited
            db.session.commit()
            ret_dict["worker_invited"] = user.worker_invited
        if self.args.trusted is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_trusted(self.args.trusted)
            ret_dict["trusted"] = user.trusted
        if self.args.flagged is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_flagged(self.args.flagged)
            ret_dict["flagged"] = user.flagged
        if self.args.customizer is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_customizer(self.args.customizer)
            ret_dict["customizer"] = user.customizer
        if self.args.vpn is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_vpn(self.args.vpn)
            ret_dict["vpn"] = user.vpn
        if self.args.special is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT UserSingle')
            user.set_special(self.args.special)
            ret_dict["special"] = user.special
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
    get_parser.add_argument("apikey", type=str, required=False, help="User API key we're looking for.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

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
        cached_user = None
        cache_name = f"cached_apikey_user_{hash_api_key(self.args.apikey)}"
        if hr.horde_r:
            cached_user = hr.horde_r_get(cache_name)
        if cached_user:
            user_details = json.loads(cached_user)
        else:
            user = database.find_user_by_sharedkey(self.args.apikey)
            sharedkey = True
            privilege = 0
            if not user:
                user = database.find_user_by_api_key(self.args.apikey)
                sharedkey = False
                privilege = 1
            if not user:
                raise e.UserNotFound(self.args.apikey, 'api_key')
            user_details = user.get_details(privilege)
            if sharedkey:
                sk = database.find_sharedkey(self.args.apikey)
                skname = ''
                if sk.name is not None:
                    skname = f": {sk.name}"
                user_details["username"] = user_details["username"] + f" (Shared Key{skname})"
            if hr.horde_r:
                hr.horde_r_setex_json(cache_name, timedelta(seconds=300), user_details)
        return(user_details,200)

class Models(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    # TODO: Remove the default "image" once all UIs have updated
    get_parser.add_argument("type", required=False, default="image", type=str, help="Filter the models by type (image or text).", location="args")
    get_parser.add_argument("min_count", required=False, default=None, type=int, help="Filter only models that have at least this amount of threads serving.", location="args")
    get_parser.add_argument("max_count", required=False, default=None, type=int, help="Filter the models that have at most this amount of threads serving.", location="args")

    @logger.catch(reraise=True)
    @cache.cached(timeout=2, query_string=True)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_active_model, code=200, description='List All Active Models', as_list=True)
    def get(self):
        '''Returns a list of models active currently in this horde
        '''
        self.args = self.get_parser.parse_args()
        models_ret = database.retrieve_available_models(
            model_type=self.args.type,
            min_count=self.args.min_count,
            max_count=self.args.max_count,
        )
        return (models_ret,200)


class ModelSingle(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=1)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_active_model, code=200, description='Lists specific model stats')
    def get(self, model_name="stable_diffusion"):
        '''Returns all the statistics of a specific model in this horde
        '''
        self.args = self.get_parser.parse_args()
        models_ret = database.get_available_models(model_name)
        return (models_ret,200)


class HordeLoad(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @logger.catch(reraise=True)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Performance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = database.retrieve_totals()
        # TODO: Rename this to image_worker_count in apiv3
        load_dict["worker_count"], load_dict["thread_count"] = database.count_active_workers()
        load_dict["interrogator_count"], load_dict["interrogator_thread_count"] = database.count_active_workers("interrogation")
        load_dict["text_worker_count"], load_dict["text_thread_count"] = database.count_active_workers("text")
        load_dict["past_minute_megapixelsteps"] = stats.get_things_per_min("image")
        load_dict["past_minute_tokens"] = stats.get_things_per_min("text")
        return(load_dict,200)

class HordeNews(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

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
    get_parser.add_argument("apikey", type=str, required=False, help="The Admin or Owner API key.", location='headers')

    @api.expect(get_parser)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_horde_modes, code=200, description='Horde Maintenance', skip_none=True)
    def get(self):
        '''Horde Maintenance Mode Status
        Use this endpoint to quicky determine if this horde is in maintenance, invite_only or raid mode.
        '''
        cfg = settings.get_settings()
        ret_dict = {
            "maintenance_mode": cfg.maintenance,
            "invite_only_mode": cfg.invite_only,
            
        }
        is_privileged = False
        self.args = self.get_parser.parse_args()
        if self.args.apikey:
            admin = database.find_user_by_api_key(self.args['apikey'])
            if not admin:
                raise e.InvalidAPIKey('admin worker details')
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'ModeratorWorkerDetails')
            ret_dict["raid_mode"] = cfg.raid
        return(ret_dict,200)

    parser = reqparse.RequestParser()
    parser.add_argument("apikey", type=str, required=True, help="The Admin API key.", location="headers")
    parser.add_argument("maintenance", type=bool, required=False, help="Start or stop maintenance mode.", location="json")
    # parser.add_argument("shutdown", type=int, required=False, help="Initiate a graceful shutdown of the horde in this amount of seconds. Will put horde in maintenance if not already set.", location="json")
    parser.add_argument("invite_only", type=bool, required=False, help="Start or stop worker invite-only mode.", location="json")
    parser.add_argument("raid", type=bool, required=False, help="Start or stop raid mode.", location="json")

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
        cfg = settings.get_settings()
        if self.args.maintenance is not None:
            if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
                raise e.NotAdmin(admin.get_unique_alias(), 'PUT HordeModes')
            cfg.maintenance = self.args.maintenance
            if cfg.maintenance:
                logger.critical(f"Horde entered maintenance mode")
                for wp in database.get_all_active_wps():
                    wp.abort_for_maintenance()
            ret_dict["maintenance_mode"] = cfg.maintenance
        #TODO: Replace this with a node-offline call
        # if self.args.shutdown is not None:
        #     if not os.getenv("ADMINS") or admin.get_unique_alias() not in json.loads(os.getenv("ADMINS")):
        #         raise e.NotAdmin(admin.get_unique_alias(), 'PUT HordeModes')
        #     settings.maintenance = True
        #     for wp in database.get_all_wps():
        #         wp.abort_for_maintenance()
        #     database.shutdown(self.args.shutdown)
        #     ret_dict["maintenance_mode"] = settings.maintenance
        if self.args.invite_only is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT HordeModes')
            cfg.invite_only = self.args.invite_only
            ret_dict["invite_only_mode"] = cfg.invite_only
        if self.args.raid is not None:
            if not admin.moderator:
                raise e.NotModerator(admin.get_unique_alias(), 'PUT HordeModes')
            cfg.raid = self.args.raid
            ret_dict["raid_mode"] = cfg.raid
        if not len(ret_dict):
            raise e.NoValidActions("No mod change selected!")
        else:
            db.session.commit()
        return(ret_dict, 200)

class Teams(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

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
    post_parser.add_argument("apikey", type=str, required=True, help="A User API key.", location='headers')
    post_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
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
    get_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

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
    patch_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Creator API key.", location='headers')
    patch_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
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
            if not admin.moderator and admin != team.owner:
                raise e.NotOwner(admin.get_unique_alias(), team.name)
            ret = team.set_info(self.args.info)
            if ret == "Profanity":
                raise e.Profanity(admin.get_unique_alias(), self.args.info, 'team info')
            ret_dict["info"] = team.info
        if self.args.name is not None:
            if not admin.moderator and admin != team.owner:
                raise e.NotModerator(admin.get_unique_alias(), 'PATCH TeamSingle')
            ret = team.set_name(self.args.name)
            if ret == "Profanity":
                raise e.Profanity(self.user.get_unique_alias(), self.args.name, 'team name')
            if ret == "Already Exists":
                raise e.NameAlreadyExists(self.user.get_unique_alias(), team.name, self.args.name, 'team')
            ret_dict["name"] = team.name
        if not len(ret_dict):
            raise e.NoValidActions("No team modification selected!")
        return(ret_dict, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    delete_parser.add_argument("apikey", type=str, required=False, help="The Moderator or Owner API key.", location='headers')


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
        if not admin.moderator and admin != team.owner:
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
    delete_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    delete_parser.add_argument("ipaddr", type=str, required=True, location="json")

    @api.expect(delete_parser, models.input_model_delete_ip_timeout, validate=True)
    @api.marshal_with(models.response_model_simple_response, code=200, description='Operation Completed', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def delete(self):
        '''Remove an IP from timeout.
        Only usable by horde moderators
        '''
        self.args = self.delete_parser.parse_args()
        check_for_mod(self.args.apikey, 'DELETE OperationsIP')
        CounterMeasures.delete_timeout(self.args.ipaddr)
        return({"message":'OK'}, 200)

class OperationsBlockWorkerIP(Resource):
    put_parser = reqparse.RequestParser()
    put_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    put_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @api.expect(put_parser)
    @api.marshal_with(models.response_model_simple_response, code=200, description='Operation Completed', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def put(self, worker_id):
        '''Block worker's from a specific IP for 24 hours.
        Only usable by horde moderators
        '''
        self.args = self.delete_parser.parse_args()
        mod = check_for_mod(self.args.apikey, 'PUT OperationsBlockWorkerIP')
        self.worker = database.find_worker_by_id(worker_id)
        if self.worker is None:
            raise e.WorkerNotFound(worker_id)
        CounterMeasures.set_timeout(self.worker.ipaddr, minutes=60*24)
        logger.info(f"Worker {worker_id} set into 24h IP timeout by {mod.get_unique_alias()} ")
        return({"message":'OK'}, 200)
    
    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_simple_response, code=200, description='Operation Completed', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def delete(self, worker_id):
        '''Remove a worker's IP block.
        Only usable by horde moderators
        '''
        self.args = self.delete_parser.parse_args()
        mod = check_for_mod(self.args.apikey, 'DELETE OperationsBlockWorkerIP')
        self.worker = database.find_worker_by_id(worker_id)
        if self.worker is None:
            raise e.WorkerNotFound(worker_id)
        CounterMeasures.delete_timeout(self.worker.ipaddr)
        logger.info(f"Worker {worker_id} removed from IP timeout by {mod.get_unique_alias()} ")
        return({"message":'OK'}, 200)

class Filters(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    get_parser.add_argument("filter_type", type=int, required=False, help="The filter type.", location="args")
    get_parser.add_argument("contains", type=str, default=None, required=False, help="Only return filter containing this word.", location="args")

    # decorators = [limiter.limit("20/minute")]
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_filter_details, code=200, description='Filters List', as_list=True, skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def get(self):
        '''Moderator Only: A List all filters, or filtered by the query
        '''
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, 'GET Filter')
        filters = Filter.query
        if self.args.contains:
            filters = filters.filter(
                or_(
                    Filter.regex.contains(self.args.contains),
                    Filter.description.contains(self.args.contains),
                )
            )
        if self.args.filter_type:
            filters = filters.filter(Filter.filter_type == self.args.filter_type)
        return([f.get_details() for f in filters.all()],200)

    put_parser = reqparse.RequestParser()
    put_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    put_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    put_parser.add_argument("regex", type=str, required=True, help="The filter regex.", location="json")
    put_parser.add_argument("filter_type", type=int, required=True, help="The filter type.", location="json")
    put_parser.add_argument("description", type=str, required=False, help="Optional description about this filter.", location="json")
    put_parser.add_argument("replacement", type=str, default='', required=False, help="Replacement string to use for this regex.", location="json")

    # decorators = [limiter.limit("20/minute")]
    @api.expect(put_parser,models.input_model_filter_put, validate=True)
    @api.marshal_with(models.response_model_filter_details, code=201, description='New Filter details')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def put(self):
        '''Moderator Only: Add a new regex filter
        '''
        self.args = self.put_parser.parse_args()
        mod = check_for_mod(self.args.apikey, 'PUT Filter')
        new_filter = Filter.query.filter_by(regex=self.args.regex, filter_type=self.args.filter_type).first()
        if not new_filter:
            new_filter = Filter(
                regex = self.args.regex,
                filter_type = self.args.filter_type,
                description = self.args.description,
                replacement = self.args.replacement,
                user_id = mod.id,
            )
            db.session.add(new_filter)
            db.session.commit()
            logger.info(f"Mod {mod.get_unique_alias()} added new filter {new_filter.id}")
        return(new_filter.get_details(),200)

    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    post_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    post_parser.add_argument("prompt", type=str, required=True, help="The prompt to check.", location="json")
    post_parser.add_argument("filter_type", type=int, default=None, required=False, help="Only check if it matches a specific type.", location="json")

    # decorators = [limiter.limit("20/minute")]
    @api.expect(post_parser)
    @api.marshal_with(models.response_model_prompt_suspicion, code=200, description='Returns the suspicion of the provided prompt. A suspicion of 2 or more means it would be blocked.')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def post(self):
        '''Moderator Only: Check The suspicion of the provided prompt
        '''
        self.args = self.post_parser.parse_args()
        mod = check_for_mod(
            api_key = self.args.apikey, 
            operation = 'POST Filter',
            whitelisted_users = [
                "Webhead#1193",
            ],
        )
        suspicion, matches = prompt_checker(self.args.prompt, self.args.filter_type)
        logger.info(f"Mod {mod.get_unique_alias()} checked prompt {self.args.prompt}")
        return({"suspicion": suspicion, "matches": matches},200)

class FilterRegex(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    get_parser.add_argument("filter_type", type=int, required=False, help="The filter type.", location="args")

    # decorators = [limiter.limit("20/minute")]
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_filter_regex, code=200, description='Filters Regex', as_list=True, skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def get(self):
        '''Moderator Only: A List all filters, or filtered by the query
        '''
        self.args = self.get_parser.parse_args()
        mod = check_for_mod(
            api_key = self.args.apikey, 
            operation = 'GET FilterRegex',
            whitelisted_users = [
            ],
        )
        return_list = []
        for id in prompt_checker.known_ids:
            filter_id = f"filter_{id}"
            if self.args.filter_type and id != self.args.filter_type:
                continue
            return_list.append(
                {
                    "filter_type": id,
                    "regex": prompt_checker.regex[filter_id],
                }
            )
        return (return_list, 200)

class FilterSingle(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    # decorators = [limiter.limit("20/minute")]
    @cache.cached(timeout=10)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_filter_details, code=200, description='Filters List', as_list=True, skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def get(self, filter_id):
        '''Moderator Only: Display a single filter
        '''
        if not filter_id.isdigit():
            raise e.ThingNotFound("Filter", filter_id)
        self.args = self.get_parser.parse_args()
        mod = check_for_mod(
            api_key = self.args.apikey, 
            operation = 'GET FilterSingle',
            whitelisted_users = [
            ],
        )
        filter = Filter.query.filter_by(id=filter_id).first()
        if not filter:
            raise e.ThingNotFound('Filter', filter_id)
        return(filter.get_details(),200)

    patch_parser = reqparse.RequestParser()
    patch_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    patch_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    patch_parser.add_argument("regex", type=str, required=False, help="The filter regex.", location="json")
    patch_parser.add_argument("filter_type", type=int, required=False, help="The filter type.", location="json")
    patch_parser.add_argument("description", type=str, required=False, help="Optional description about this filter.", location="json")
    patch_parser.add_argument("replacement", type=str, default='', required=False, help="Replacement string to use for this regex.", location="json")

    # decorators = [limiter.limit("20/minute")]
    @api.expect(patch_parser,models.input_model_filter_patch, validate=True)
    @api.marshal_with(models.response_model_filter_details, code=200, description='Patched Filter details')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def patch(self, filter_id):
        '''Moderator Only: Modify an existing regex filter
        '''
        self.args = self.patch_parser.parse_args()
        mod = check_for_mod(self.args.apikey, 'PATCH FilterSingle')
        filter = Filter.query.filter_by(id=filter_id).first()
        if not filter:
            raise e.ThingNotFound('Filter', filter_id)
        if not self.args.filter_type and not self.args.regex and not self.args.description and not self.args.replacement:
            raise e.NoValidActions("No filter patching selected!")
        filter.user_id = mod.id,
        if self.args.filter_type:
            filter.filter_type = self.args.filter_type
        if self.args.regex:
            filter.regex = self.args.regex
        if self.args.description:
            filter.description = self.args.description
        if self.args.replacement:
            filter.replacement = self.args.replacement
        db.session.commit()
        logger.info(f"Mod {mod.get_unique_alias()} modified filter {filter.id}")
        return(filter.get_details(),200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location='headers')
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    # decorators = [limiter.limit("20/minute")]
    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_simple_response, code=200, description='Filter Deleted')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def delete(self, filter_id):
        '''Moderator Only: Delete a regex filter
        '''
        self.args = self.delete_parser.parse_args()
        mod = check_for_mod(self.args.apikey, 'DELETE FilterSingle')
        filter = Filter.query.filter_by(id=filter_id).first()
        if not filter:
            raise e.ThingNotFound('Filter', filter_id)
        logger.info(f"Mod {mod.get_unique_alias()} deleted filter {filter.id}")            
        db.session.delete(filter)
        db.session.commit()
        return({"message": "OK"},200)

class Heartbeat(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    decorators = [limiter.exempt]
    @api.expect(get_parser)
    def get(self):
        '''If this loads, this node is available
        '''
        return {
            'message': 'OK',
            'version': HORDE_VERSION,
        },200


class SharedKey(Resource):
    put_parser = reqparse.RequestParser()
    put_parser.add_argument("apikey", type=str, required=True, help="User API key.", location='headers')
    put_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")
    put_parser.add_argument("kudos", type=int, required=False, default=5000, help="The amount of kudos limit available to this key.", location="json")
    put_parser.add_argument("expiry", type=int, required=False, default=-1, help="The amount of days which this key will stay active.", location="json")
    put_parser.add_argument("name", type=str, required=False, help="A descriptive name for this key.", location="json")
    put_parser.add_argument("max_image_pixels", type=int, required=False, default=-1, help="The maximum number of pixels this key can generate per job.", location="json")
    put_parser.add_argument("max_image_steps", type=int, required=False, default=-1, help="The maximum number of steps this key can use per job.", location="json")
    put_parser.add_argument("max_text_tokens", type=int, required=False, default=-1, help="The maximum number of tokens this key can generate per job.", location="json")

    decorators = [limiter.limit("5/minute", key_func = get_request_path)]
    @api.expect(put_parser, models.input_model_sharedkey)
    @api.marshal_with(models.response_model_sharedkey_details, code=200, description='SharedKey Details', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Shared Key Not Found', models.response_model_error)
    def put(self):
        '''Create a new SharedKey for this user
        '''
        self.args = self.put_parser.parse_args()
        user: User = database.find_user_by_api_key(self.args.apikey)
        if not user:
            raise e.InvalidAPIKey("get sharedkey")
        if user.is_anon():
            raise e.AnonForbidden
        if user.count_sharedkeys() > user.max_sharedkeys():
            raise e.Forbidden(f"You cannot have more than {user.max_sharedkeys()} shared keys.")
        expiry = None
        if self.args.expiry and self.args.expiry != -1:
            expiry = datetime.utcnow() + timedelta(days=self.args.expiry)

        new_key = UserSharedKey(
            user_id = user.id,
            kudos = self.args.kudos,
            expiry = expiry,
            name = self.args.name,
            max_image_pixels = self.args.max_image_pixels,
            max_image_steps = self.args.max_image_steps,
            max_text_tokens = self.args.max_text_tokens,
        )
        db.session.add(new_key)
        db.session.commit()
        return new_key.get_details(),200


class SharedKeySingle(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @cache.cached(timeout=60)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_sharedkey_details, code=200, description='Shared Key Details', skip_none=True)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(404, 'Shared Key Not Found', models.response_model_error)
    def get(self, sharedkey_id=''):
        '''Get details about an existing Shared Key for this user
        '''
        self.args = self.get_parser.parse_args()
        sharedkey = database.find_sharedkey(sharedkey_id)
        if not sharedkey:
            raise e.InvalidAPIKey("get sharedkey", keytype="Shared")
        return sharedkey.get_details(),200

    patch_parser = reqparse.RequestParser()
    patch_parser.add_argument("apikey", type=str, required=True, help="User API key.", location='headers')
    patch_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
    patch_parser.add_argument("kudos", type=int, required=False, help="The amount of kudos limit available to this key.", location="json")
    patch_parser.add_argument("expiry", type=int, required=False, help="The amount of days from today which this key will stay active.", location="json")
    patch_parser.add_argument("name", type=str, required=False, help="A descriptive name for this key.", location="json")
    patch_parser.add_argument("max_image_pixels", type=int, required=False, help="The maximum number of pixels this key can generate per job.", location="json")
    patch_parser.add_argument("max_image_steps", type=int, required=False, help="The maximum number of steps this key can use per job.", location="json")
    patch_parser.add_argument("max_text_tokens", type=int, required=False, help="The maximum number of tokens this key can generate per job.", location="json")

    @api.expect(patch_parser, models.input_model_sharedkey)
    @api.marshal_with(models.response_model_sharedkey_details, code=200, description='Shared Key Details', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Shared Key Not Found', models.response_model_error)
    def patch(self, sharedkey_id=''):
        '''Modify an existing Shared Key
        '''
        self.args = self.patch_parser.parse_args()
        sharedkey = database.find_sharedkey(sharedkey_id)
        if not sharedkey:
            raise e.InvalidAPIKey("Shared Key Not Found.", keytype="Shared")
        user = database.find_user_by_api_key(self.args.apikey)
        if not user:
            raise e.InvalidAPIKey("patch sharedkey")
        if sharedkey.user_id != user.id:
            raise e.Forbidden(f"Shared Key {sharedkey.id} belongs to {sharedkey.user.get_unique_alias()} and not to {user.get_unique_alias()}.")
        no_valid_actions = self.args.expiry is None and self.args.kudos is None and self.args.name is None
        no_valid_limit_actions = self.args.max_image_pixels is None and self.args.max_image_steps is None and self.args.max_text_tokens is None

        if no_valid_actions and no_valid_limit_actions:
            raise e.NoValidActions("No shared key modification selected!")
        if self.args.expiry is not None:
            if self.args.expiry == -1:
                sharedkey.expiry = None
            else:
                sharedkey.expiry = datetime.utcnow() + timedelta(days=self.args.expiry)
        if self.args.kudos is not None:
            sharedkey.kudos = self.args.kudos
        if self.args.name is not None:
            sharedkey.name = self.args.name

        if self.args.max_image_pixels is not None:
            sharedkey.max_image_pixels = self.args.max_image_pixels
        if self.args.max_image_steps is not None:
            sharedkey.max_image_steps = self.args.max_image_steps
        if self.args.max_text_tokens is not None:
            sharedkey.max_text_tokens = self.args.max_text_tokens
        
        db.session.commit()
        return sharedkey.get_details(),200

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("apikey", type=str, required=True, help="User API key.", location='headers')
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version.", location="headers")

    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_simple_response, code=200, description='Shared Key Deleted')
    @api.response(404, 'Shared Key Not Found', models.response_model_error)
    def delete(self, sharedkey_id=''):
        '''Delete an existing SharedKey for this user
        '''
        self.args = self.delete_parser.parse_args()
        sharedkey = database.find_sharedkey(sharedkey_id)
        if not sharedkey:
            raise e.InvalidAPIKey("Shared Key Not Found.", keytype="Shared")
        user = database.find_user_by_api_key(self.args.apikey)
        if not user:
            raise e.InvalidAPIKey("delete sharedkey")
        if sharedkey.user_id != user.id:
            raise e.Forbidden(f"Shared Key {sharedkey.id} belongs to {sharedkey.user.get_unique_alias()} and not to {user.get_unique_alias()}.")
        db.session.delete(sharedkey)
        db.session.commit()
        return {"message": "OK"},200
