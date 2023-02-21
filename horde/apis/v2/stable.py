import base64
import requests
import sys
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from datetime import datetime

from .v2 import *
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.countermeasures import CounterMeasures
from horde.r2 import upload_source_image, generate_img_download_url
from horde.logger import logger
from horde.classes.stable.genstats import compile_imagegen_stats_totals, compile_imagegen_stats_models
from horde.image import convert_source_image_to_pil, convert_source_image_to_webp, upload_source_image_to_r2, ensure_source_image_uploaded
from horde.threads import model_reference

class AsyncGenerate(AsyncGenerate):
    
    def validate(self):
        #logger.warning(datetime.utcnow())
        super().validate()
        #logger.warning(datetime.utcnow())
        # Temporary exception. During trial period only trusted users can use img2img
        if not self.user.trusted and not patrons.is_patron(self.user.id):
            self.safe_ip = CounterMeasures.is_ip_safe(self.user_ip)
            # We allow unsafe IPs when being rate limited as they're only temporary
            if self.safe_ip is None:
                self.safe_ip = True
            # We actually block unsafe IPs for now to combat CP
            if not self.safe_ip:
                raise e.NotTrusted
        if not self.args.source_image and self.args.source_mask:
            raise e.SourceMaskUnnecessary
        if not self.args.source_image and any(model_name in ["Stable Diffusion 2 Depth", "pix2pix"] for model_name in self.args.models):
            raise e.UnsupportedModel
        if not self.args.source_image and any(model_name in model_reference.controlnet_models for model_name in self.args.models):
            raise e.UnsupportedModel
        if self.args.source_image:
            if self.args.source_processing == "img2img" and self.params.get("sampler_name") in ["k_dpm_fast", "k_dpm_adaptive", "k_dpmpp_2s_a", "k_dpmpp_2m"]:
                raise e.UnsupportedSampler
        #     if any(model_name.startswith("stable_diffusion_2") for model_name in self.args.models):
        #         raise e.UnsupportedModel
        # if not any(model_name.startswith("stable_diffusion_2") for model_name in self.args.models) and self.params.get("sampler_name") in ["dpmsolver"]:
        #     raise e.UnsupportedSampler
        # if self.args.models == ["stable_diffusion_2.0"] and self.params.get("sampler_name") not in ["dpmsolver"]:
        #     raise e.UnsupportedSampler
        if len(self.args['prompt'].split()) > 7500:
            raise e.InvalidPromptSize(self.username)
        if any(model_name in ["GFPGAN", "RealESRGAN_x4plus", "CodeFormers"] for model_name in self.args.models):
            raise e.UnsupportedModel

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Consider reducing the size to 512x512")

    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        # logger.debug(self.params)
        shared=self.args.shared
        # Anon users are always shared
        if self.user.is_anon():
            shared=True
        if self.args.source_image:
            shared=False
        self.wp = WaitingPrompt(
            self.workers,
            self.models,
            prompt = self.args["prompt"],
            user_id = self.user.id,
            params = self.params,
            nsfw = self.args.nsfw,
            censor_nsfw = self.args.censor_nsfw,
            trusted_workers = self.args.trusted_workers,
            source_processing = self.args.source_processing,
            ipaddr = self.user_ip,
            safe_ip=self.safe_ip,
            r2=self.args.r2,
            shared=shared,
        )
        needs_kudos,resolution = self.wp.require_upfront_kudos(database.retrieve_totals())
        if needs_kudos:
            required_kudos = self.wp.kudos * self.wp.n
            if required_kudos > self.user.kudos:
                raise e.KudosUpfront(required_kudos, self.username, resolution)
            else:
                logger.warning(f"{self.username} requested generation {self.wp.id} requiring upfront kudos: {required_kudos}")


    # We split this into its own function, so that it may be overriden and extended
    def activate_waiting_prompt(self):
        # Not using yet, but might need later
        self.source_image = None
        self.source_mask = None
        if self.args.source_image:
            self.source_image, self.source_image_r2stored = ensure_source_image_uploaded(self.args.source_image, f"{self.wp.id}_src", force_r2 = True)
            if self.args.source_mask:
                self.source_mask, self.source_mask_r2stored = ensure_source_image_uploaded(self.args.source_mask, f"{self.wp.id}_msk", force_r2 = True)
        self.wp.activate(self.source_image, self.source_mask)

class SyncGenerate(SyncGenerate):

    def validate(self):
        super().validate()
        # Temporary exception. During trial period only trusted users can use img2img
        if self.args.source_image:
            if not self.user.trusted and not patrons.is_patron(self.user.id):
                self.safe_ip = CounterMeasures.is_ip_safe(self.user_ip)
                # We allow unsafe IPs when being rate limited as they're only temporary
                if self.safe_ip is False:
                    self.safe_ip = False
                    raise e.NotTrusted
        if not self.args.source_image and self.args.source_mask:
            raise e.SourceMaskUnnecessary
        if len(self.args['prompt'].split()) > 80:
            raise e.InvalidPromptSize(self.username)

    
    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        logger.debug(self.params)
        self.wp = WaitingPrompt(
            self.workers,
            self.models,
            prompt = self.args["prompt"],
            user_id = self.user.id,
            params = self.params,
            nsfw = self.args.nsfw,
            censor_nsfw = self.args.censor_nsfw,
            trusted_workers = self.args.trusted_workers,
            source_image = convert_source_image_to_webp(self.args.source_image),
            source_processing = self.args.source_processing,
            source_mask = convert_source_image_to_webp(self.args.source_mask),
            ipaddr = self.user_ip,
            safe_ip=self.safe_ip,
            r2=self.args.r2,
        )
        needs_kudos,resolution = self.wp.require_upfront_kudos(database.retrieve_totals())
        if needs_kudos:
            required_kudos = self.wp.kudos * self.wp.n
            if required_kudos > self.user.kudos:
                raise e.KudosUpfront(required_kudos, self.username, resolution)
            else:
                logger.warning(f"{self.username} requested generation {self.wp.id} requiring upfront kudos: {required_kudos}")

    
class JobPop(JobPop):
    def check_in(self):
        self.worker.check_in(
            self.args.max_pixels, 
            nsfw = self.args.nsfw, 
            require_upfront_kudos = self.args.require_upfront_kudos, 
            blacklist = self.blacklist, 
            models = self.models, 
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            threads = self.args.threads,
            bridge_version = self.args.bridge_version,
            bridge_agent = self.args.bridge_agent,
            allow_img2img = self.args.allow_img2img,
            allow_painting = self.args.allow_painting,
            allow_unsafe_ipaddr = self.args.allow_unsafe_ipaddr,
            allow_post_processing = self.args.allow_post_processing,
            priority_usernames = self.priority_usernames,
        )

    def get_sorted_wp(self, priority_user_ids=None):
        '''We're sending the lists directly, to avoid having to join tables'''
        sorted_wps = database.get_sorted_wp_filtered_to_worker(
            self.worker,
            self.models,
            self.blacklist,
            priority_user_ids = priority_user_ids,
        )        
        return sorted_wps


class Aesthetics(Resource):

    post_parser = reqparse.RequestParser()
    post_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
    post_parser.add_argument("best", type=str, required=False, location="json")
    post_parser.add_argument("ratings", type=list, required=False, default=False, location="json")

    decorators = [limiter.limit("5/minute", key_func = get_request_path)]
    @api.expect(post_parser, models.input_model_aesthetics_payload, validate=True)
    @api.marshal_with(models.response_model_job_submit, code=200, description='Aesthetics Submitted')
    @api.response(400, 'Aesthetics Already Submitted', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(404, 'Generation Request Not Found', models.response_model_error)
    def post(self, id):
        '''Submit aesthetic ratings for generated images to be used by LAION
        The request has to have been sent as shared: true.
        You can select the best image in the set, and/or provide a rating for each or some images in the set.
        If you select best-of image, you will gain 4 kudos. Each rating is 5 kudos. Best-of will be ignored when ratings conflict with it.
        You can never gain more kudos than you spent for this generation. Your reward at max will be your kudos consumption - 1.
        '''
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        if not wp.is_completed():
            raise e.InvalidAestheticAttempt("You can only aesthetically rate completed requests!")
        if not wp.shared:
            raise e.InvalidAestheticAttempt("You can only aesthetically rate requests you have opted to share publicly")
        self.args = self.post_parser.parse_args()
        procgen_ids = [str(procgen.id) for procgen in wp.processing_gens if not procgen.faulted and not procgen.cancelled]
        logger.debug(procgen_ids)
        if self.args.ratings:
            seen_ids = []
            for rating in self.args.ratings:
                if rating["id"] not in procgen_ids:
                    raise e.ProcGenNotFound(rating["id"])
                if rating["id"] in seen_ids:
                    raise e.InvalidAestheticAttempt("Duplicate image ID found in your ratings. You should be ashamed!")
                seen_ids.append(rating["id"])
        if self.args.best:
            if self.args.best not in procgen_ids:
                raise e.ProcGenNotFound(self.args.best)
        if not self.args.ratings and not self.args.best:
            raise e.InvalidAestheticAttempt("You need to either point to the best image, or aesthetic ratings.")
        if not self.args.ratings and self.args.best and len(procgen_ids) <= 1:
            raise e.InvalidAestheticAttempt("Well done! You have pointed to a single image generation as being the best one of the set. Unfortunately that doesn't help anyone. no kudos for you!")
        aesthetic_payload = {
            "set": id,
            "all_set_ids": procgen_ids,
            "client_agent": self.args["Client-Agent"],
            "user": {
                "username": wp.user.get_unique_alias(),
                "trusted": wp.user.trusted,
                "account_age": (datetime.utcnow() - wp.user.created).seconds,
                "usage_requests": wp.user.usage_requests,
                "kudos": wp.user.kudos,
                "kudos_accumulated": wp.user.compile_kudos_details().get("accumulated",0),
                "ipaddr": request.remote_addr,
            },
        }
        self.kudos = 0
        if self.args.ratings:
            self.kudos = 5 * len(self.args.ratings)
            for r in self.args.ratings:
                if r.get("artifacts") is not None:
                    self.kudos += 3
            aesthetic_payload["ratings"] = self.args.ratings
            # If they only rated one, and rated it > 7, we assume it's the best of the set by default
            # Unless another bestof was selected (for some reason)
            if len(self.args.ratings) == 1 and len(procgen_ids) > 1:
                if self.args.ratings[0]["rating"] >= 7:
                    if not self.args.best or self.args.best == self.args.ratings[0]["id"]:
                        aesthetic_payload["best"] = self.args.ratings[0]["id"]
                elif self.args.best:
                    self.kudos += 4
                    aesthetic_payload["best"] = self.args.best
            if len(self.args.ratings) > 1:
                bestofs = None
                bestof_rating = -1
                for rating in self.args.ratings:
                    if rating["rating"] > bestof_rating:
                        bestofs = [rating["id"]]
                        bestof_rating = rating["rating"]
                        continue
                    if rating["rating"] > bestof_rating:
                        bestofs.append(rating["id"])
                        continue
                if len(bestofs) > 1:
                    if self.args.best:
                        if self.args.best not in bestofs:
                            raise e.InvalidAestheticAttempt("What are you even doing? How could the best image you selected not be one of those with the highest aesthetic rating?")
                        aesthetic_payload["best"] = self.args.best
                if len(bestofs) == 1:
                    aesthetic_payload["best"] = bestofs[0]
        else:
            self.kudos = 4
            aesthetic_payload["best"] = self.args.best
        # You can never get more kudos from rating that what you consumed
        if self.kudos >= wp.consumed_kudos:
            self.kudos = wp.consumed_kudos - 1
        logger.debug(aesthetic_payload)
        try:
            submit_req = requests.post("https://ratings.droom.cloud/api/v1/rating/set", json = aesthetic_payload, timeout=3)
            if not submit_req.ok:
                if submit_req.status_code == 403:
                    raise e.InvalidAestheticAttempt("This generation appears already rated")
                try:
                    error_msg = submit_req.json()
                except Exception:
                    raise e.InvalidAestheticAttempt(f"Received unexpected response from rating server: {submit_req.text}")
                raise e.InvalidAestheticAttempt(f"Rating Server returned error: {error_msg['message']}")
        except requests.exceptions.ConnectionError:
            raise e.InvalidAestheticAttempt("The rating server appears to be down")
        except requests.exceptions.ReadTimeout:
            raise e.InvalidAestheticAttempt("The rating server took to long to respond")
        except Exception as err:
            if type(err) == e.InvalidAestheticAttempt:
                raise err
            logger.error(f"Error when submitting Aesthetic: {err}")
            raise e.InvalidAestheticAttempt("Oops, Something went wrong when submitting the request. Please contact us.")
        wp.user.modify_kudos(self.kudos, "awarded")
        return({"reward": self.kudos}, 200)



# I have to put it outside the class as I can't figure out how to extend the argparser and also pass it to the @api.expect decorator inside the class
class Interrogate(Resource):


    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="A User API key", location='headers')
    post_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
    post_parser.add_argument("forms", type=list, required=False, default=None, help="The acceptable forms with which to interrogate", location="json")
    post_parser.add_argument("source_image", type=str, required=True, location="json")
    post_parser.add_argument("trusted_workers", type=bool, required=False, default=False, help="When true, only Horde trusted workers will serve this request. When False, Evaluating workers will also be used.", location="json")

    @api.expect(post_parser, models.input_interrogate_request_generation, validate=True)
    @api.marshal_with(models.response_model_interrogation, code=202, description='Interrogation Queued', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(503, 'Maintenance Mode', models.response_model_error)
    @api.response(429, 'Too Many Prompts', models.response_model_error)
    def post(self):
        '''Initiate an Asynchronous request to interrogate an image.
        This endpoint will immediately return with the UUID of the request for interrogation.
        This endpoint will always be accepted, even if there are no workers available currently to fulfill this request. 
        Perhaps some will appear in the next 20 minutes.
        Asynchronous requests live for 20 minutes before being considered stale and being deleted.
        '''
        #logger.warning(datetime.utcnow())
        self.args = self.post_parser.parse_args()
        self.forms = []
        if self.args.forms:
            self.forms = self.args.forms
        self.user = None
        self.user_ip = request.remote_addr
        # For now this is checked on validate()
        self.safe_ip = True
        self.validate()
        #logger.warning(datetime.utcnow())
        self.interrogation = Interrogation(
            self.forms,
            user_id = self.user.id,
            trusted_workers = self.args.trusted_workers,
            ipaddr = self.user_ip,
            safe_ip = self.safe_ip,
        )
        # If anything goes wrong when uploading an image, we don't want to leave garbage around
        try:
            self.source_image, self.r2stored = ensure_source_image_uploaded(self.args.source_image, str(self.interrogation.id))
        except Exception as e:
            db.session.delete(self.interrogation)
            db.session.commit()
            raise e
        self.interrogation.set_source_image(self.source_image, self.r2stored)
        ret_dict = {"id":self.interrogation.id}
        return(ret_dict, 202)

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        if maintenance.active:
            raise e.MaintenanceMode('Interrogate')
        with HORDE.app_context():
            if self.args.apikey:
                self.user = database.find_user_by_api_key(self.args['apikey'])
            if not self.user:
                raise e.InvalidAPIKey('generation')
            self.username = self.user.get_unique_alias()
            i_count = database.count_waiting_interrogations(self.user)
            # More concurrency for interrogations
            user_limit = self.user.get_concurrency() * 10
            if i_count + len(self.forms) > user_limit:
                raise e.TooManyPrompts(self.username, i_count + len(self.forms), user_limit)
        if not self.user.trusted and not patrons.is_patron(self.user.id):
            self.safe_ip = CounterMeasures.is_ip_safe(self.user_ip)
            # We allow unsafe IPs when being rate limited as they're only temporary
            if self.safe_ip is None:
                self.safe_ip = True
            # We actually block unsafe IPs for now to combat CP
            if not self.safe_ip:
                raise e.NotTrusted


class InterrogationStatus(Resource):
    decorators = [limiter.limit("10/second", key_func = get_request_path)]
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.marshal_with(models.response_model_interrogation_status, code=200, description='Interrogation Request Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id):
        '''Retrieve the full status of an interrogation request.
        This request will include all already generated images.
        As such, you are requested to not retrieve this endpoint often. Instead use the /check/ endpoint first
        '''
        interrogation = database.get_interrogation_by_id(id)
        if not interrogation:
            raise e.RequestNotFound(id, 'Interrogation')
        i_status = interrogation.get_status()
        return(i_status, 200)

    @api.marshal_with(models.response_model_interrogation_status, code=200, description='Interrogation Request Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def delete(self, id):
        '''Cancel an unfinished interrogation request.
        This request will return all already interrogated image results.
        '''
        interrogation = database.get_interrogation_by_id(id)
        if not interrogation:
            raise e.RequestNotFound(id, 'Interrogation')
        interrogation.cancel()
        i_status = interrogation.get_status()
        logger.info(f"Interrogation with ID {interrogation.id} has been cancelled.")
        return(i_status, 200)


class InterrogatePop(JobPopTemplate):

    # The parser for RequestPop
    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    post_parser.add_argument("name", type=str, required=True, help="The worker's unique name, to track contributions", location="json")
    post_parser.add_argument("priority_usernames", type=list, required=False, help="The usernames which get priority use on this worker", location="json")
    post_parser.add_argument("forms", type=list, required=False, help="The forms currently supported on this worker", location="json")
    post_parser.add_argument("amount", type=int, required=False, default=1, help="How many forms to pop at the same time", location="json")
    post_parser.add_argument("bridge_version", type=int, required=False, default=1, help="Specify the version of the worker bridge, as that can modify the way the arguments are being sent", location="json")
    post_parser.add_argument("bridge_agent", type=str, required=False, default="unknown:0:unknown", location="json")
    post_parser.add_argument("threads", type=int, required=False, default=1, help="How many threads this worker is running. This is used to accurately the current power available in the horde", location="json")


    decorators = [limiter.limit("60/second")]
    @api.expect(post_parser, models.input_model_interrogation_pop, validate=True)
    @api.marshal_with(models.response_model_interrogation_pop, code=200, description='Interrogation Popped')
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    def post(self):
        '''Check if there are interrogation requests queued for fulfillment.
        This endpoint is used by registered workers only
        '''
        # logger.warning(datetime.utcnow())
        self.args = self.post_parser.parse_args()
        self.priority_usernames = []
        if self.args.priority_usernames:
            self.priority_usernames = self.args.priority_usernames
        self.forms = []
        if self.args.forms:
            self.forms = self.args.forms
        self.worker_ip = request.remote_addr
        self.validate(worker_class = InterrogationWorker)
        self.check_in()
        # This ensures that the priority requested by the bridge is respected
        self.prioritized_forms = []
        # self.priority_users = [self.user]
        ## Start prioritize by bridge request ##

        pre_priority_user_ids = [x.split("#")[-1] for x in self.priority_usernames if x != '']
        self.priority_user_ids = [self.user.id]
        # TODO move to database class
        p_users_id_from_db = db.session.query(User.id).filter(User.id.in_(pre_priority_user_ids)).all()
        if p_users_id_from_db:
            self.priority_user_ids.extend([x.id for x in p_users_id_from_db])

        priority_list = database.get_sorted_forms_filtered_to_worker(
            worker = self.worker, 
            forms_list = self.forms, 
            priority_user_ids = self.priority_user_ids,
        )
        for form in priority_list:
            # We append to the list so that we have the prioritized forms first
            self.prioritized_forms.append(form)
        
        # If we already have 100 requests from prioritized users, we don't want to do another DB call
        if len(self.prioritized_forms) < 100:
            for form in database.get_sorted_forms_filtered_to_worker(
                worker = self.worker, 
                forms_list = self.forms,
                excluded_forms = self.prioritized_forms,
            ):
                self.prioritized_forms.append(form)
        # logger.warning(datetime.utcnow())
        worker_ret = {"forms": []}
        for form in self.prioritized_forms:
            try:
                can_interrogate, skipped_reason = self.worker.can_interrogate(form)
            except Exception as e:
                logger.error(f"Error when checking interrogation for worker. Skipping: {e}.")
                continue
            if not can_interrogate:
                # We don't report on secret skipped reasons
                # as they're typically countermeasures to raids
                if skipped_reason != "secret":
                    self.skipped[skipped_reason] = self.skipped.get(skipped_reason,0) + 1
                #logger.warning(datetime.utcnow())
                continue
            # There is a chance that by the time we finished all the checks, another worker picked up the WP. 
            # So we do another final check here before picking it up to avoid sending the same WP to two workers by mistake.
            # time.sleep(random.uniform(0, 1))
            if not form.is_waiting(): 
                continue
            try:
                form_ret = form.pop(self.worker)
            except Exception as e:
                logger.error(f"Error when popping interrogation. Skipping: {e}.")
                continue
            # logger.debug(worker_ret)
            if form_ret is None:
                continue
            worker_ret["forms"].append(form_ret)
            if len(worker_ret["forms"]) >= self.args.amount:
                # logger.debug(worker_ret)
                return(worker_ret, 200)
        if len(worker_ret["forms"]) >= 1:
            # logger.debug(worker_ret)
            return(worker_ret, 200)
        # We report maintenance exception only if we couldn't find any jobs
        if self.worker.maintenance:
            raise e.WorkerMaintenance(self.worker.maintenance_msg)
        # logger.warning(datetime.utcnow())
        return({"skipped": self.skipped}, 200)


    def check_in(self):
        self.worker.check_in(
            forms = self.forms, 
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            threads = self.args.threads,
            bridge_version = self.args.bridge_version,
            bridge_agent = self.args.bridge_agent,
            priority_usernames = self.priority_usernames,
        )


class InterrogateSubmit(Resource):
    decorators = [limiter.limit("60/second")]


    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="The worker's owner API key", location='headers')
    post_parser.add_argument("id", type=str, required=True, help="The processing generation uuid", location="json")
    post_parser.add_argument("result", type=dict, required=True, help="The completed interrogation form results", location="json")
    post_parser.add_argument("state", type=str, required=False, default='ok', help="The state of this returned generation.", location="json")

    @api.expect(post_parser)
    @api.marshal_with(models.response_model_job_submit, code=200, description='Interrogation Submitted')
    @api.response(400, 'Generation Already Submitted', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Request Not Found', models.response_model_error)
    def post(self):
        '''Submit the results of an interrogated image.
        This endpoint is used by registered workers only
        '''
        self.args = self.post_parser.parse_args()
        self.validate()
        self.kudos = self.form.deliver(
            result=self.args.result, 
            state=self.args.state, 
        )
        # -1 means faulted
        if self.kudos == -1:
            return({"reward": 0}, 200)
        if self.kudos == 0 and not self.form.worker.maintenance:
            raise e.DuplicateGen(self.form.worker.name, self.args['id'])
        return({"reward": self.kudos}, 200)

    def validate(self):
        self.form = database.get_form_by_id(self.args['id'])
        if not self.form:
            raise e.InvalidJobID(self.args['id'])
        self.user = database.find_user_by_api_key(self.args['apikey'])
        if not self.user:
            raise e.InvalidAPIKey('worker submit:' + self.args['name'])
        if self.user != self.form.worker.user:
            raise e.WrongCredentials(self.user.get_unique_alias(), self.form.worker.name)



class HordeLoad(HordeLoad):
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Maintenance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = super().get()[0]
        load_dict["interrogator_count"], load_dict["interrogator_thread_count"] = database.count_active_workers("InterrogationWorker")
        load_dict["past_minute_megapixelsteps"] = stats.get_things_per_min()
        return(load_dict,200)

class HordeNews(HordeNews):
    
    @cache.cached(timeout=300)
    def get_news(self):
        return(horde_news + stable_horde_news)


class HordeStatsTotals(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_stats_img_totals, code=200, description='Horde generated images statistics')
    def get(self):
        '''Details how many images have been generated in the past minux,hour,day,month and total
        Also shows the amount of pixelsteps for the same timeframe.
        '''
        return compile_imagegen_stats_totals(),200

class HordeStatsModels(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_stats_models, code=200, description='Horde generated images statistics per model')
    def get(self):
        '''Details how many images were generated per model for the past day, month and total
        '''
        return compile_imagegen_stats_models(),200

api.add_resource(SyncGenerate, "/generate/sync")
api.add_resource(AsyncGenerate, "/generate/async")
api.add_resource(AsyncStatus, "/generate/status/<string:id>")
api.add_resource(AsyncCheck, "/generate/check/<string:id>")
api.add_resource(Aesthetics, "/generate/rate/<string:id>")
api.add_resource(JobPop, "/generate/pop")
api.add_resource(JobSubmit, "/generate/submit")
api.add_resource(Users, "/users")
api.add_resource(UserSingle, "/users/<string:user_id>")
api.add_resource(FindUser, "/find_user")
api.add_resource(Workers, "/workers")
api.add_resource(WorkerSingle, "/workers/<string:worker_id>")
api.add_resource(TransferKudos, "/kudos/transfer")
api.add_resource(AwardKudos, "/kudos/award")
api.add_resource(HordeModes, "/status/modes")
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(Models, "/status/models")
api.add_resource(HordeNews, "/status/news")
api.add_resource(Heartbeat, "/status/heartbeat")
api.add_resource(Teams, "/teams")
api.add_resource(TeamSingle, "/teams/<string:team_id>")
api.add_resource(OperationsIP, "/operations/ipaddr")
api.add_resource(Interrogate, "/interrogate/async")
api.add_resource(InterrogationStatus, "/interrogate/status/<string:id>")
api.add_resource(InterrogatePop, "/interrogate/pop")
#TODO APIv2 Merge with status as a POST this part of /interrogate/<string:id>
api.add_resource(InterrogateSubmit, "/interrogate/submit")
api.add_resource(Filters, "/filters")
api.add_resource(FilterRegex, "/filters/regex")
api.add_resource(FilterSingle, "/filters/<string:filter_id>")
api.add_resource(HordeStatsTotals, "/stats/img/totals")
api.add_resource(HordeStatsModels, "/stats/img/models")
