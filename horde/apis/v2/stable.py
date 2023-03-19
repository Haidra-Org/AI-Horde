import requests
import sys
from datetime import datetime

from .base import *
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.countermeasures import CounterMeasures
from horde.logger import logger
from horde.classes.stable.genstats import compile_imagegen_stats_totals, compile_imagegen_stats_models
from horde.image import ensure_source_image_uploaded
from horde.model_reference import model_reference
from horde.consts import KNOWN_POST_PROCESSORS, KNOWN_UPSCALERS

from horde.apis.models.stable_v2 import ImageModels, ImageParsers

models = ImageModels(api)
parsers = ImageParsers()

class ImageAsyncGenerate(GenerateTemplate):
    gentype = "image"

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
        self.args = parsers.generate_parser.parse_args()
        try:
            super().post()
        except KeyError:
            logger.error(f"caught missing Key.")
            logger.error(self.args)
            logger.error(self.args.params)
            return {"message": "Internal Server Error"},500
        ret_dict = {"id":self.wp.id}
        if not database.wp_has_valid_workers(self.wp, self.workers) and not settings.mode_raid():
            ret_dict['message'] = self.get_size_too_big_message()
        return(ret_dict, 202)

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Please confider reducing its size of the request.")

    def validate(self):
        #logger.warning(datetime.utcnow())
        super().validate()
        #logger.warning(datetime.utcnow())
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
        if (
            self.params.get("control_type") in ["normal", "mlsd", "hough"]
            and any(
                model_reference.get_model_baseline(model_name).startswith("stable diffusion 2") 
                for model_name in self.args.models
            )
        ):
            raise e.UnsupportedModel(f"No current model available for this particular ControlNet for SD2.x")
        if "control_type" in self.params and any(model_name in ["pix2pix"] for model_name in self.args.models):
            raise e.UnsupportedModel("You cannot use ControlNet with these models.")
        #if self.params.get("image_is_control"):
        #    raise e.UnsupportedModel("This feature is disabled for the moment.")
        if "control_type" in self.params and not self.args.source_image:
            raise e.UnsupportedModel("Controlnet Requires a source image.")
        if self.params.get("init_as_image") and self.params.get("return_control_map"):
            raise e.UnsupportedModel("Invalid ControlNet parameters - cannot send inital map and return the same map")
        if not self.args.source_image and any(model_name in ["Stable Diffusion 2 Depth", "pix2pix"] for model_name in self.args.models):
            raise e.UnsupportedModel
        if not self.args.source_image and any(model_name in model_reference.controlnet_models for model_name in self.args.models):
            raise e.UnsupportedModel
        if self.args.source_mask and self.params.get("sampler_name") == "DDIM":
            raise e.UnsupportedModel("You cannot use a mask with the DDIM sampler")
        if self.args.source_image:
            if self.args.source_processing == "img2img" and self.params.get("sampler_name") in ["k_dpm_fast", "k_dpm_adaptive", "k_dpmpp_2s_a", "k_dpmpp_2m"]:
                raise e.UnsupportedSampler
        #     if any(model_name.startswith("stable_diffusion_2") for model_name in self.args.models):
        #         raise e.UnsupportedModel
        # if not any(model_name.startswith("stable_diffusion_2") for model_name in self.args.models) and self.params.get("sampler_name") in ["dpmsolver"]:
        #     raise e.UnsupportedSampler
        if self.args.models == ["pix2pix"] and self.params.get("sampler_name") == "DDIM":
             raise e.UnsupportedSampler("You cannot use pix2pix with the DDIM sampler")
        if len(self.args['prompt'].split()) > 7500:
            raise e.InvalidPromptSize(self.username)
        if any(model_name in KNOWN_POST_PROCESSORS for model_name in self.args.models):
            raise e.UnsupportedModel
        if self.args.params:
            upscaler_count = len(
                [
                    pp for pp in self.args.params.get("post_processing", [])
                    if pp in KNOWN_UPSCALERS
                ]
            )
            if upscaler_count > 1:
                raise e.UnsupportedModel("Cannot use more than 1 upscaler at a time.")
        if self.args["Client-Agent"] in ["My-Project:v0.0.1:My-Contact"]:
            raise e.BadRequest(
                "This Client-Agent appears badly designed and is causing too many warnings. "
                "First ensure it provides a proper name and contact details. "
                "Then contact us on Discord to discuss the issue it's creating."
            )

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
        # hlky metldown
        shared=False
        self.wp = ImageWaitingPrompt(
            self.workers,
            self.models,
            prompt = self.args.prompt,
            user_id = self.user.id,
            params = self.params,
            nsfw = self.args.nsfw,
            censor_nsfw = self.args.censor_nsfw,
            trusted_workers = self.args.trusted_workers,
            slow_workers = self.args.slow_workers,
            source_processing = self.args.source_processing,
            ipaddr = self.user_ip,
            safe_ip=self.safe_ip,
            r2=self.args.r2,
            shared=shared,
            client_agent=self.args["Client-Agent"],
        )
        _, total_threads = database.count_active_workers("image")
        needs_kudos,resolution = self.wp.require_upfront_kudos(database.retrieve_totals(),total_threads)
        if needs_kudos:
            required_kudos = self.wp.kudos * self.wp.n
            if required_kudos > self.user.kudos:
                raise e.KudosUpfront(
                    required_kudos, 
                    self.username, 
                    message=f"Due to heavy demand, for requests over {resolution}x{resolution} or over 50 steps (25 for k_heun and k_dpm_2*), and for 12 or more weights, the client needs to already have the required kudos. This request requires {required_kudos} kudos to fulfil."
                )
            # else:
            #     logger.warning(f"{self.username} requested generation {self.wp.id} requiring upfront kudos: {required_kudos}")


    # We split this into its own function, so that it may be overriden and extended
    def activate_waiting_prompt(self):
        # Not using yet, but might need later
        self.source_image = None
        self.source_mask = None
        if self.args.source_image:
            self.source_image, img, self.source_image_r2stored = ensure_source_image_uploaded(self.args.source_image, f"{self.wp.id}_src", force_r2 = True)
            if self.args.source_mask:
                self.source_mask, img, self.source_mask_r2stored = ensure_source_image_uploaded(self.args.source_mask, f"{self.wp.id}_msk", force_r2 = True)
            elif self.args.source_processing == "inpainting":
                # FIXME
                raise e.UnsupportedModel("Inpainting is disabled momentarily while we investigate a bug crashing workers performing it.")
                try:
                    _red, _green, _blue, _alpha = img.split()
                except ValueError:
                    raise e.ImageValidationFailed("Inpainting requests must either include a mask, or an alpha channel.")
        self.wp.activate(self.source_image, self.source_mask)


class ImageAsyncStatus(Resource):
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
        self.args = self.get_parser.parse_args()
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id,client_agent=self.args["Client-Agent"])
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("image"),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers()
        )
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
        self.args = self.delete_parser.parse_args()
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id,client_agent=self.args["Client-Agent"])
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("image"),
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


class ImageAsyncCheck(Resource):
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
        # Sending lite mode to try and reduce the amount of bandwidth
        # This will not retrieve procgens, so ETA will not be completely accurate
        self.args = self.get_parser.parse_args()
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id,client_agent=self.args["Client-Agent"])
        lite_status = wp.get_lite_status(
            request_avg=database.get_request_avg("image"),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers()
        )
        return(lite_status, 200)


    
class ImageJobPop(JobPopTemplate):
    worker_class = ImageWorker

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
        # Splitting the post to its own function so that I can have the decorators of post on each extended class
        # Without copying the whole post() code
        self.args = parsers.job_pop_parser.parse_args()
        self.blacklist = []
        if self.args.blacklist:
            self.blacklist = self.args.blacklist
        return super().post()

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
            allow_controlnet = self.args.allow_controlnet,
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

class ImageJobSubmit(JobSubmitTemplate):
    decorators = [limiter.limit("60/second")]
    @api.expect(parsers.job_submit_parser, models.input_model_job_submit, validate=True)
    @api.marshal_with(models.response_model_job_submit, code=200, description='Generation Submitted')
    @api.response(400, 'Generation Already Submitted', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Request Not Found', models.response_model_error)
    def post(self):
        '''Submit a generated image.
        This endpoint is used by registered workers only
        '''
        # We have to parse the args here, to ensure we use the correct parser class
        self.args = parsers.job_submit_parser.parse_args()
        return super().post()

    def get_progen(self):
        '''Set to its own function to it can be overwritten depending on the class'''
        return database.get_progen_by_id(self.args['id'])

    def set_generation(self):
        '''Set to its own function to it can be overwritten depending on the class'''
        things_per_sec = stats.record_fulfilment(self.procgen)
        self.kudos = self.procgen.set_generation(
            generation=self.args['generation'], 
            things_per_sec=things_per_sec, 
            seed=self.args.seed,
            censored=self.args.censored,
            state=self.args.state,
        )



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
        self.args = self.post_parser.parse_args()
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id,client_agent=self.args["Client-Agent"])
        if not wp.is_completed():
            raise e.InvalidAestheticAttempt("You can only aesthetically rate completed requests!")
        if not wp.shared:
            raise e.InvalidAestheticAttempt("You can only aesthetically rate requests you have opted to share publicly")
        procgen_ids = [str(procgen.id) for procgen in wp.processing_gens if not procgen.faulted and not procgen.cancelled]
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
            self.source_image, img, self.r2stored = ensure_source_image_uploaded(self.args.source_image, str(self.interrogation.id))
        except Exception as err:
            db.session.delete(self.interrogation)
            db.session.commit()
            raise err
        self.interrogation.set_source_image(self.source_image, self.r2stored)
        ret_dict = {"id":self.interrogation.id}
        return(ret_dict, 202)

    # We split this into its own function, so that it may be overriden and extended
    def validate(self):
        if settings.mode_maintenance():
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
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    decorators = [limiter.limit("10/second", key_func = get_request_path)]
    @api.expect(get_parser)
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.marshal_with(models.response_model_interrogation_status, code=200, description='Interrogation Request Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id):
        '''Retrieve the full status of an interrogation request.
        This request will include all already generated images.
        As such, you are requested to not retrieve this endpoint often. Instead use the /check/ endpoint first
        '''
        self.args = self.get_parser.parse_args()
        interrogation = database.get_interrogation_by_id(id)
        if not interrogation:
            raise e.RequestNotFound(id, 'Interrogation',client_agent=self.args["Client-Agent"])
        i_status = interrogation.get_status()
        return(i_status, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @api.expect(delete_parser)
    @api.marshal_with(models.response_model_interrogation_status, code=200, description='Interrogation Request Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def delete(self, id):
        '''Cancel an unfinished interrogation request.
        This request will return all already interrogated image results.
        '''
        self.args = self.delete_parser.parse_args()
        interrogation = database.get_interrogation_by_id(id)
        if not interrogation:
            raise e.RequestNotFound(id, 'Interrogation',client_agent=self.args["Client-Agent"])
        interrogation.cancel()
        i_status = interrogation.get_status()
        logger.info(f"Interrogation with ID {interrogation.id} has been cancelled.")
        return(i_status, 200)


class InterrogatePop(JobPopTemplate):
    worker_class = InterrogationWorker
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
            if any("#" not in user_id for user_id in self.priority_usernames):
                raise e.BadRequest("Priority usernames need to be provided in the form of 'alias#number'. Example: 'db0#1'")
        self.forms = []
        if self.args.forms:
            self.forms = self.args.forms
        self.worker_ip = request.remote_addr
        self.validate()
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
            except Exception as err:
                logger.error(f"Error when checking interrogation for worker. Skipping: {err}.")
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
            except Exception as err:
                logger.error(f"Error when popping interrogation. Skipping: {err}.")
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


class ImageHordeStatsTotals(Resource):
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

class ImageHordeStatsModels(Resource):
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

