from .v2 import *

class AsyncGenerate(AsyncGenerate):
    
    def validate(self):
        super().validate()
        if self.args["params"].get("length",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("height",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
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
            self.args["params"],
            workers=self.args["workers"],
            nsfw=self.args["nsfw"],
            censor_nsfw=self.args["censor_nsfw"],
            trusted_workers=self.args["trusted_workers"],
        )
    
class SyncGenerate(SyncGenerate):

    def validate(self):
        super().validate()
        if self.args["params"].get("height",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("height",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512) <= 0:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])

    
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
            nsfw=self.args["nsfw"],
            censor_nsfw=self.args["censor_nsfw"],
            trusted_workers=self.args["trusted_workers"],
        )
    
class JobPop(JobPop):

    def check_in(self):
        self.worker.check_in(
            self.args['max_pixels'], 
            nsfw = self.args['nsfw'], 
            blacklist = self.args['blacklist'], 
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
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
