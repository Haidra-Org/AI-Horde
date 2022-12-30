import base64
from io import BytesIO
from PIL import Image

from .v2 import *
from horde.classes import Interrogation
from horde.countermeasures import CounterMeasures
from horde.r2 import upload_image, generate_img_download_url
from horde.logger import logger
from ..exceptions import ImageValidationFailed


def convert_source_image_to_pil(source_image):
    base64_bytes = source_image_b64.encode('utf-8')
    img_bytes = base64.b64decode(base64_bytes)
    image = Image.open(BytesIO(img_bytes))
    width, height = image.size
    resolution = width * height
    resolution_threshold = 3072*3072
    if resolution > resolution_threshold:
        except_msg = "Image size cannot exceed 3072*3072 pixels"
        # Not sure e exists here?
        raise e.ImageValidationFailed()
    quality = 100
    # We adjust the amount of compression based on the starting image to avoid running out of bandwidth
    if resolution > resolution_threshold * 0.9:
        quality = 50
    elif resolution > resolution_threshold * 0.8:
        quality = 60
    elif resolution > resolution_threshold * 0.6:
        logger.debug([resolution,resolution_threshold * 0.6])
        quality = 70
    elif resolution > resolution_threshold * 0.4:
        logger.debug([resolution,resolution_threshold * 0.4])
        quality = 80
    elif resolution > resolution_threshold * 0.3:
        logger.debug([resolution,resolution_threshold * 0.4])
        quality = 90
    elif resolution > resolution_threshold * 0.15:
        quality = 95
    return image,quality

def convert_source_image_to_webp(source_image_b64):
    '''Convert img2img sources to 90% compressed webp, to avoid wasting bandwidth, while still supporting all types'''
    except_msg = None
    try:
        if source_image_b64 is None:
            return(source_image_b64)
        image, quality = convert_source_image_to_pil(source_image_b64)
        buffer = BytesIO()
        image.save(buffer, format="WebP", quality=quality)
        final_image_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
        logger.debug(f"Received img2img source of {width}*{height}. Started {round(len(source_image_b64) / 1000)} base64 kilochars. Ended with quality {quality} = {round(len(final_image_b64) / 1000)} base64 kilochars")
        return final_image_b64
    except ImageValidationFailed as e:
        raise e.ImageValidationFailed(except_msg)
    except Exception as e:
        logger.error(e)
        raise e.ImageValidationFailed

def upload_source_image_to_r2(source_image_b64, uuid_string):
    '''Convert source images to webp and uploads it to r2, to avoid wasting bandwidth, while still supporting all types'''
    except_msg = None
    try:
        if source_image_b64 is None:
            return(source_image_b64)
        image, quality = convert_source_image_to_pil(source_image_b64)
        filename = f"{uuid_string}.webp"
        image.save(filename, format="WebP", quality=quality)
        upload_image(filename)
        os.remove(filename)
        return generate_img_download_url(filename)
    except ImageValidationFailed as e:
        raise e.ImageValidationFailed(except_msg)
    except Exception as e:
        logger.error(e)
        raise e.ImageValidationFailed


def ensure_source_image_uploaded(source_image_string, uuid_string):
    if "http" in source_image_string:
        return source_image_string, False
    else:
        return upload_source_image_to_r2(source_image_string, uuid_string), True


class AsyncGenerate(AsyncGenerate):
    
    def validate(self):
        from datetime import datetime
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
        if not self.args.source_image and any(model_name == "Stable Diffusion 2 Depth" for model_name in self.args.models):
            raise e.UnsupportedModel
        if self.args.source_image:
            if self.args.source_processing == "img2img" and self.params.get("sampler_name") in ["k_dpm_fast", "k_dpm_adaptive", "k_dpmpp_2s_a", "k_dpmpp_2m"]:
                raise e.UnsupportedSampler
            if any(model_name.startswith("stable_diffusion_2") for model_name in self.args.models):
                raise e.UnsupportedModel
        if not any(model_name.startswith("stable_diffusion_2") for model_name in self.args.models) and self.params.get("sampler_name") in ["dpmsolver"]:
            raise e.UnsupportedSampler
        # if self.args.models == ["stable_diffusion_2.0"] and self.params.get("sampler_name") not in ["dpmsolver"]:
        #     raise e.UnsupportedSampler
        if len(self.args['prompt'].split()) > 500:
            raise e.InvalidPromptSize(self.username)

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Consider reducing the size to 512x512")

    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        from datetime import datetime
        #logger.warning(datetime.utcnow())
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
        needs_kudos,resolution = self.wp.requires_upfront_kudos(database.retrieve_totals())
        if needs_kudos:
            required_kudos = self.wp.kudos * self.wp.n
            if required_kudos > self.user.kudos:
                raise e.KudosUpfront(required_kudos, self.username, resolution)
            else:
                logger.warning(f"{self.username} requested generation {self.wp.id} requiring upfront kudos: {required_kudos}")
    
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
        needs_kudos,resolution = self.wp.requires_upfront_kudos(database.retrieve_totals())
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
            blacklist = self.blacklist, 
            models = self.models, 
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            threads = self.args.threads,
            bridge_version = self.args.bridge_version,
            allow_img2img = self.args.allow_img2img,
            allow_painting = self.args.allow_painting,
            allow_unsafe_ipaddr = self.args.allow_unsafe_ipaddr,
            allow_post_processing = self.args.allow_post_processing,
        )

    def get_sorted_wp(self):
        '''We're sending the lists directly, to avoid having to join tables'''
        return database.get_sorted_wp_filtered_to_worker(
            self.worker,
            self.models,
            self.blacklist,
        )



# I have to put it outside the class as I can't figure out how to extend the argparser and also pass it to the @api.expect decorator inside the class
class Interrogate(Resource):


    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=True, help="A User API key", location='headers')
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
            user_limit = self.user.get_concurrency()
            if wp_count + len(self.forms) > user_limit:
                raise e.TooManyPrompts(self.username, wp_count + n, user_limit)
            ip_timeout = CounterMeasures.retrieve_timeout(self.user_ip)
            if ip_timeout:
                raise e.TimeoutIP(self.user_ip, ip_timeout)
        if not self.user.trusted and not patrons.is_patron(self.user.id):
            self.safe_ip = CounterMeasures.is_ip_safe(self.user_ip)
            # We allow unsafe IPs when being rate limited as they're only temporary
            if self.safe_ip is None:
                self.safe_ip = True
            # We actually block unsafe IPs for now to combat CP
            if not self.safe_ip:
                raise e.NotTrusted


class InterrogationStatus(Resource):
    decorators = [limiter.limit("10/minute", key_func = get_request_path)]
     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.marshal_with(models.response_model_interrogation_status, code=200, description='Interrogation Request Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id = ''):
        '''Retrieve the full status of an interrogation request.
        This request will include all already generated images.
        As such, you are requested to not retrieve this endpoint often. Instead use the /check/ endpoint first
        This endpoint is limited to 10 requests per minute
        '''
        interrogation = database.get_interrogation_by_id(id)
        if not interrogation:
            raise e.RequestNotFound(id)
        i_status = interrogation.get_status()
        return(i_status, 200)

    @api.marshal_with(models.response_model_interrogation_status, code=200, description='Interrogation Request Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def delete(self, id = ''):
        '''Cancel an unfinished interrogation request.
        This request will return all already interrogated image results.
        '''
        interrogation = database.get_interrogation_by_id(id)
        if not interrogation:
            raise e.RequestNotFound(id)
        interrogation.cancel()
        i_status = interrogation.get_status()
        logger.info(f"Interrogation with ID {interrogation.id} has been cancelled.")
        return(i_status, 200)

class HordeLoad(HordeLoad):
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Maintenance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = super().get()[0]
        load_dict["past_minute_megapixelsteps"] = stats.get_things_per_min()
        return(load_dict,200)

class HordeNews(HordeNews):
    
    @cache.cached(timeout=300)
    def get_news(self):
        return(horde_news + stable_horde_news)


api.add_resource(SyncGenerate, "/generate/sync")
api.add_resource(AsyncGenerate, "/generate/async")
api.add_resource(AsyncStatus, "/generate/status/<string:id>")
api.add_resource(AsyncCheck, "/generate/check/<string:id>")
api.add_resource(JobPop, "/generate/pop")
api.add_resource(JobSubmit, "/generate/submit")
api.add_resource(Users, "/users")
api.add_resource(UserSingle, "/users/<string:user_id>")
api.add_resource(FindUser, "/find_user")
api.add_resource(Workers, "/workers")
api.add_resource(WorkerSingle, "/workers/<string:worker_id>")
api.add_resource(TransferKudos, "/kudos/transfer")
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
