from .base import *
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt

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
            prompt = self.args.prompt,
            user_id = self.user.id,
            params = self.params,
            workers = self.workers,
            models = self.models,
            softprompt = self.args.softprompt,
            trusted_workers = self.args["trusted_workers"],
            ipaddr=self.ipaddr,
            safe_ip=self.safe_ip,
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
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg(),
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
        wp = database.get_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(id)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg(),
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

class TextJobPop(JobPop):

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
        self.process_post()

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
            safe_ip = self.safe_ip,
            ipaddr = self.worker_ip,
            threads = self.args.threads,
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

    def get_sorted_wp(self, priority_user_ids=None):
        '''We're sending the lists directly, to avoid having to join tables'''
        sorted_wps = database.get_sorted_wp_filtered_to_worker(
            self.worker,
            self.models,
            priority_user_ids = priority_user_ids,
        )        
        return sorted_wps

class TextJobSubmit(Resource):
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
        self.process_post()


class HordeLoad(HordeLoad):
    # When we extend the actual method, we need to re-apply the decorators
    @logger.catch(reraise=True)
    @cache.cached(timeout=2)
    @api.marshal_with(models.response_model_horde_performance, code=200, description='Horde Maintenance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = super().get()[0]
        load_dict["past_minute_tokens"] = stats.get_things_per_min("text")
        return(load_dict,200)
