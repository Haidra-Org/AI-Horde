from .v2 import *
import base64
from io import BytesIO
from PIL import Image


def convert_source_image_to_webp(source_image_b64):
    '''Convert img2img sources to 90% compressed webp, to avoid wasting bandwidth, while still supporting all types'''
    try:
        if source_image_b64 == None:
            return(source_image_b64)
        base64_bytes = source_image_b64.encode('utf-8')
        img_bytes = base64.b64decode(base64_bytes)
        image = Image.open(BytesIO(img_bytes))
        buffer = BytesIO()
        image.save(buffer, format="WebP", quality=90)
        return(base64.b64encode(buffer.getvalue()).decode("utf8"))
    except:
        raise e.ImageValidationFailed

class AsyncGenerate(AsyncGenerate):
    
    def validate(self):
        super().validate()
        # Temporary exception. During trial period only trusted users can use img2img
        if self.args.source_image:
            if not self.user.trusted:
                raise e.NotTrusted
        if self.params.get("height",512)%64:
            raise e.InvalidSize(self.username)
        if self.params.get("height",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.params.get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.params.get("width",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.params.get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])

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
        )
    
class SyncGenerate(SyncGenerate):

    def validate(self):
        super().validate()
        # Temporary exception. During trial period only trusted users can use img2img
        if self.args.source_image and not self.user.trusted:
            raise e.NotTrusted
        if self.params.get("height",512)%64:
            raise e.InvalidSize(self.username)
        if self.params.get("height",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.params.get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.params.get("width",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.params.get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.params['steps'])

    
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
        )
    
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
        )
  
class HordeLoad(HordeLoad):
    decorators = [limiter.limit("2/second")]
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch
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
api.add_resource(HordeNews, "/status/news")
