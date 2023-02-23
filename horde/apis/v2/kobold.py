from .v2 import *
import requests

class AsyncGenerate(AsyncGenerate):
    
    def initiate_waiting_prompt(self):
        self.softprompts = ['']
        if self.args.softprompts:
            self.softprompts = self.args.softprompts
        logger.debug([self.models, self.args.models])
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.params,
            workers = self.workers,
            models = self.models,
            softprompts = self.softprompts,
            trusted_workers = self.args["trusted_workers"],
        )

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Consider reducing the amount of tokens to generate.")


class SyncGenerate(SyncGenerate):

    def initiate_waiting_prompt(self):
        self.softprompts = ['']
        if self.args.softprompts:
            self.softprompts = self.args.softprompts
        self.wp = WaitingPrompt(
            db,
            waiting_prompts,
            processing_generations,
            self.args["prompt"],
            self.user,
            self.params,
            workers = self.workers,
            models = self.models,
            softprompts = self.softpompts,
            trusted_workers = self.args["trusted_workers"],
        )

class JobPop(JobPop):

    def check_in(self):
        self.softprompts = []
        if self.args.softprompts:
            self.softprompts = self.args.softprompts
        models = self.models
        # To adjust the below once I updated the KAI server to use "models" arg
        if self.args.model:
            models = [self.args.model]
        self.worker.check_in(
            self.args['max_length'],
            self.args['max_content_length'],
            self.softprompts,
            models = models,
            nsfw = self.args.nsfw,
            blacklist = self.blacklist,
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            threads = self.args.threads,
        )


    # Making it into its own function to allow extension
    def start_worker(self, wp_to_start):
        for wp in self.prioritized_wp:
            matching_softprompt = False
            for sp in wp.softprompts:
                # If a None softprompts has been provided, we always match, since we can always remove the softprompt
                if sp == '':
                    matching_softprompt = sp
                arg_softprompts = self.args['softprompts']
                if not arg_softprompts:
                    arg_softprompts = []
                for sp_name in arg_softprompts:
                    # logger.info([sp_name,sp,sp in sp_name])
                    if sp in sp_name: # We do a very basic string matching. Don't think we need to do regex
                        matching_softprompt = sp_name
                        break
                if matching_softprompt:
                    break
        ret = wp_to_start.start_generation(self.worker, matching_softprompt)
        return(ret)


class HordeLoad(HordeLoad):
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Maintenance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = super().get()[0]
        load_dict["past_minute_tokens"] = db.stats.get_things_per_min()
        return(load_dict,200)


class KoboldKudosTransfer(Resource):
    post_parser = reqparse.RequestParser()
    post_parser.add_argument("apikey", type=str, required=False, help="The User API key", location='headers')
    post_parser.add_argument("username", type=str, required=True, help="The AI Horde user ID which will receive the kudos", location="json")


    @api.expect(post_parser)
    def post(self):
        '''Transfers all user kudos to the AI Horde
        '''
        user = None
        self.args = self.post_parser.parse_args()
        if self.args.apikey:
            user = db.find_user_by_api_key(self.args.apikey)
        if not user:
            raise e.InvalidAPIKey('AI Horde Kudos Transfer')
        if user.is_anon():
            raise e.KudosValidationError(user.get_unique_alias(),"Cannot transfer from anon")
        ulist = self.args.username.split('#')
        if len(ulist) != 2:
            raise e.KudosValidationError(user.get_unique_alias(),"Invalid username format given")
        ai_user_id = ulist[-1]
        if int(ai_user_id) == 0:
            raise e.KudosValidationError(user.get_unique_alias(),"Cannot transfer to anon")
        kudos_amount = user.kudos - user.min_kudos
        if kudos_amount <= 0:
            raise e.KudosValidationError(user.get_unique_alias(),"Not any kudos to give!")
        logger.warning(f"{user.get_unique_alias()} Started {kudos_amount} Kudos Transfer to AI Horde ID {ai_user_id}")
        submit_dict = {
            "kai_id": user.id,
            "kudos_amount": kudos_amount,
            "trusted": user.trusted,
        }
        logger.debug(submit_dict)
        try:
            submit_req = requests.post(f'https://stablehorde.net/api/v2/kudos/kai/{ai_user_id}', json = submit_dict)
        except Exception as err:
            raise e.KudosValidationError(user.get_unique_alias(),f"Something went wrong when trying to transfter Kudos to AI Horde: {err}")
        if not submit_req.ok:
            err = submit_req.json()
            if "message" in err:
                err = err["message"]
            raise e.KudosValidationError(user.get_unique_alias(),f"Something went wrong when trying to transfter Kudos to AI Horde: {err}")
        user.modify_kudos(-kudos_amount, 'koboldai')
        return submit_req.json(),200



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
api.add_resource(KoboldKudosTransfer, "/kudos/transfer/kai")
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(HordeModes, "/status/modes")
api.add_resource(Models, "/status/models")
api.add_resource(HordeNews, "/status/news")
api.add_resource(Teams, "/teams")
api.add_resource(TeamSingle, "/teams/<string:team_id>")
api.add_resource(OperationsIP, "/operations/ipaddr")
