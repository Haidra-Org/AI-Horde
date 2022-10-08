from .v2 import *

class AsyncGenerate(AsyncGenerate):
    
    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.args["params"],
            workers = self.args["workers"],
            models = self.args["models"],
            softprompts = self.args["softprompts"],
            trusted_workers = self.args["trusted_workers"],
        )

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Consider reducing the amount of tokens to generate.")


class SyncGenerate(SyncGenerate):

    def initiate_waiting_prompt(self):
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.args["params"],
            workers = self.args["workers"],
            models = self.args["models"],
            softprompts = self.args["softprompts"],
            trusted_workers = self.args["trusted_workers"],
        )

class JobPop(JobPop):

    def check_in(self):
        self.worker.check_in(
            self.args['max_length'],
            self.args['max_content_length'],
            self.args['softprompts'],
            model = self.args['model'],
            nsfw = self.args['nsfw'],
            blacklist = self.args['blacklist'],
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
        )


    # Making it into its own function to allow extension
    def start_worker(self, wp):
        for wp in self.prioritized_wp:
            matching_softprompt = False
            for sp in wp.softprompts:
                # If a None softprompts has been provided, we always match, since we can always remove the softprompt
                if sp == '':
                    matching_softprompt = sp
                for sp_name in self.args['softprompts']:
                    # logger.info([sp_name,sp,sp in sp_name])
                    if sp in sp_name: # We do a very basic string matching. Don't think we need to do regex
                        matching_softprompt = sp_name
                        break
                if matching_softprompt:
                    break
        ret = wp.start_generation(self.worker, matching_softprompt)
        return(ret)


class HordeLoad(HordeLoad):
    decorators = [limiter.limit("2/second")]
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Maintenance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = super().get()[0]
        load_dict["past_minute_tokens"] = db.stats.get_things_per_min()
        return(load_dict,200)

class Models(Resource):
    decorators = [limiter.limit("30/minute")]
    @logger.catch
    @api.marshal_with(models.response_model_model, code=200, description='List All Active Models', as_list=True)
    def get(self):
        '''Returns a list of models active currently in this horde
        '''
        return(db.get_available_models(),200)


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
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(HordeModes, "/status/modes")
api.add_resource(Models, "/status/models")
