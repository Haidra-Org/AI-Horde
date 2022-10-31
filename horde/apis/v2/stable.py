from .v2 import *
import base64
from io import BytesIO
from PIL import Image


def convert_source_image_to_webp(source_image_b64):
    '''Convert img2img sources to 90% compressed webp, to avoid wasting bandwidth, while still supporting all types'''
    except_msg = None
    try:
        if source_image_b64 == None:
            return(source_image_b64)
        base64_bytes = source_image_b64.encode('utf-8')
        img_bytes = base64.b64decode(base64_bytes)
        image = Image.open(BytesIO(img_bytes))
        width, height = image.size
        resolution = width * height
        resolution_threshold = 3072*3072
        if resolution > resolution_threshold:
            except_msg = "Image size cannot exceed 3072*3072 pixels"
            raise e.ImageValidationFailed()
        buffer = BytesIO()
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
        image.save(buffer, format="WebP", quality=quality)
        final_image_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
        logger.debug(f"Received img2img source of {width}*{height}. Started {round(len(source_image_b64) / 1000)} base64 kilochars. Ended with quality {quality} = {round(len(final_image_b64) / 1000)} base64 kilochars")
        return(final_image_b64)
    except e.ImageValidationFailed:
        raise e.ImageValidationFailed(except_msg)
    except:
        raise e.ImageValidationFailed

class AsyncGenerate(AsyncGenerate):
    
    def validate(self):
        super().validate()
        # Temporary exception. During trial period only trusted users can use img2img
        if not self.user.trusted:
            self.safe_ip = cm.is_ip_safe(self.user_ip)
            # We allow unsafe IPs when being rate limited as they're only temporary
            if self.safe_ip == None:
                self.safe_ip = True
                # We actually block img2img from unsafe IPs for now.
                if self.args.source_image:
                    raise e.NotTrusted
        if self.args.source_mask and self.args.source_processing == 'img2img':
            raise e.SourceMaskUnnecessary
        if len(self.args['prompt'].split()) > 500:
            raise e.InvalidPromptSize(self.username)

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Consider reducing the size to 512x512")

    
    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.params,
            workers = self.workers,
            nsfw = self.args["nsfw"],
            censor_nsfw = self.args["censor_nsfw"],
            trusted_workers = self.args["trusted_workers"],
            models = self.models,
            source_image = convert_source_image_to_webp(self.args.source_image),
            source_processing = self.args.source_processing,
            source_mask = convert_source_image_to_webp(self.args.source_mask),
            ipaddr = self.user_ip,
        )
        if self.wp.requires_upfront_kudos():
            required_kudos = self.wp.kudos * self.wp.n
            if required_kudos > self.user.kudos:
                raise e.KudosUpfront(required_kudos, self.username)
            else:
                logger.warning(f"{self.username} requested generation {self.wp.id} requiring upfront kudos: {required_kudos}")
    
class SyncGenerate(SyncGenerate):

    def validate(self):
        super().validate()
        # Temporary exception. During trial period only trusted users can use img2img
        if self.args.source_image:
            if not self.user.trusted:
                self.safe_ip = cm.is_ip_safe(self.user_ip)
                # We allow unsafe IPs when being rate limited as they're only temporary
                if self.safe_ip == False:
                    self.safe_ip = False
                    raise e.NotTrusted
        if self.args.source_mask and self.args.source_processing == 'img2img':
            raise e.SourceMaskUnnecessary
            raise e.InvalidSize(self.username)
        if len(self.args['prompt'].split()) > 80:
            raise e.InvalidPromptSize(self.username)

    
    # We split this into its own function, so that it may be overriden
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.params,
            workers = self.workers,
            nsfw = self.args["nsfw"],
            censor_nsfw = self.args["censor_nsfw"],
            trusted_workers = self.args["trusted_workers"],
            models = self.models,
            source_image = convert_source_image_to_webp(self.args.source_image),
            source_processing = self.args.source_processing,
            source_mask = convert_source_image_to_webp(self.args.source_mask),
            ipaddr = self.user_ip,
        )
        if self.wp.requires_upfront_kudos():
            required_kudos = self.wp.kudos * self.wp.n
            if required_kudos > self.user.kudos:
                raise e.KudosUpfront(required_kudos, self.username)
            else:
                logger.warning(f"{self.username} requested generation {self.wp.id} requiring upfront kudos: {required_kudos}")

    
class JobPop(JobPop):

    def check_in(self):
        self.worker.check_in(
            self.args['max_pixels'], 
            nsfw = self.args['nsfw'], 
            blacklist = self.blacklist, 
            models = self.models, 
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            bridge_version = self.args["bridge_version"],
            allow_img2img = self.args["allow_img2img"],
            allow_painting = self.args["allow_painting"],
            allow_unsafe_ipaddr = self.args["allow_unsafe_ipaddr"],
        )
  
class HordeLoad(HordeLoad):
    decorators = [limiter.limit("2/second")]
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch(reraise=True)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Maintenance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = super().get()[0]
        load_dict["past_minute_megapixelsteps"] = db.stats.get_things_per_min()
        return(load_dict,200)

class HordeNews(HordeNews):
    
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
