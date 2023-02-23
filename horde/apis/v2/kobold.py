from .base import *
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.database import text_functions as text_database
from horde.classes.kobold.genstats import compile_textgen_stats_totals, compile_textgen_stats_models

from horde.apis.models.kobold_v2 import TextModels, TextParsers

models = TextModels(api)
parsers = TextParsers()

class TextAsyncGenerate(GenerateTemplate):
    @api.expect(parsers.generate_parser, models.input_model_request_generation, validate=True)
    @api.marshal_with(models.response_model_async, code=202, description='Generation Queued', skip_none=True)
    @api.response(400, 'Validation Error', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(503, 'Maintenance Mode', models.response_model_error)
    @api.response(429, 'Too Many Prompts', models.response_model_error)
    def post(self):
        '''Initiate an Asynchronous request to generate text.
        This endpoint will immediately return with the UUID of the request for generation.
        This endpoint will always be accepted, even if there are no workers available currently to fulfill this request. 
        Perhaps some will appear in the next 20 minutes.
        Asynchronous requests live for 20 minutes before being considered stale and being deleted.
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
        if not database.wp_has_valid_workers(self.wp, self.workers) and not raid.active:
            ret_dict['message'] = self.get_size_too_big_message()
        return(ret_dict, 202)

    def initiate_waiting_prompt(self):
        self.wp = TextWaitingPrompt(
            self.workers,
            self.models,
            prompt = self.args.prompt,
            user_id = self.user.id,
            params = self.params,
            softprompt = self.args.softprompt,
            trusted_workers = self.args.trusted_workers,
            ipaddr = self.user_ip,
            safe_ip=self.safe_ip,
            client_agent=self.args["Client-Agent"],
        )

    def get_size_too_big_message(self):
        return("Warning: No available workers can fulfill this request. It will expire in 10 minutes. Consider reducing the amount of tokens to generate.")

class TextAsyncStatus(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

     # If I marshal it here, it overrides the marshalling of the child class unfortunately
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', models.response_model_error)
    def get(self, id = ''):
        '''Retrieve the full status of an Asynchronous generation request.
        This request will include all already generated texts.
        '''
        wp = text_database.get_text_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("text"),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers("text")
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
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("text"),
            has_valid_workers=database.wp_has_valid_workers(wp),
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers("text")
        )
        logger.info(f"Request with ID {wp.id} has been cancelled.")
        # FIXME: I pevent it at the moment due to the race conditions
        # The WPCleaner is going to clean it up anyway
        wp.n = 0
        db.session.commit()
        return(wp_status, 200)

class TextJobPop(JobPopTemplate):
    worker_class = TextWorker
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
        return super().post()

    def check_in(self):
        self.softprompts = []
        if self.args.softprompts:
            self.softprompts = self.args.softprompts
        models = self.models
        self.worker.check_in(
            self.args['max_length'],
            self.args['max_context_length'],
            self.softprompts,
            models = models,
            nsfw = self.args.nsfw,
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            threads = self.args.threads,
            bridge_agent = self.args.bridge_agent,
        )


    def get_sorted_wp(self, priority_user_ids=None):
        '''We're sending the lists directly, to avoid having to join tables'''
        sorted_wps = text_database.get_sorted_text_wp_filtered_to_worker(
            self.worker,
            self.models,
            priority_user_ids = priority_user_ids,
        )        

        return sorted_wps

class TextJobSubmit(JobSubmitTemplate):
    decorators = [limiter.limit("60/second")]
    @api.expect(parsers.job_submit_parser, models.input_model_job_submit, validate=True)
    @api.marshal_with(models.response_model_job_submit, code=200, description='Generation Submitted')
    @api.response(400, 'Generation Already Submitted', models.response_model_error)
    @api.response(401, 'Invalid API Key', models.response_model_error)
    @api.response(403, 'Access Denied', models.response_model_error)
    @api.response(404, 'Request Not Found', models.response_model_error)
    def post(self):
        '''Submit generated text.
        This endpoint is used by registered workers only
        '''
        # We have to parse the args here, to ensure we use the correct parser class
        self.args = parsers.job_submit_parser.parse_args()
        return super().post()

    def get_progen(self):
        '''Set to its own function to it can be overwritten depending on the class'''
        return text_database.get_text_progen_by_id(self.args['id'])

class TextHordeStatsTotals(Resource):
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
        return compile_textgen_stats_totals(),200

class TextHordeStatsModels(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(models.response_model_stats_models, code=200, description='Horde generated images statistics per model')
    def get(self):
        '''Details how many images were generated per model for the past day, month and total
        '''
        return compile_textgen_stats_models(),200


class KoboldKudosTransfer(Resource):
    post_parser = reqparse.RequestParser()
    post_parser.add_argument("kai_id", type=int, required=True, location='json')
    post_parser.add_argument("kudos_amount", type=int, required=True, location='json')


    @api.expect(post_parser)
    def post(self, user_id = ''):
        '''Receives kudos from the KoboldAI Horde
        '''
        if request.remote_addr != "167.86.124.45":
            raise e.BadRequest("Access Denied")
        user = database.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        self.args = parsers.post_parser.parse_args()            
        logger.warning(f"{user.get_unique_alias()} Started {self.args.kudos_amount}Kudos Transfer from KAI ID {args.kai_id}")
        user.modify_kudos(self.args.kudos_amount, 'koboldai')
        return {"new_kudos": user.kudos},200

