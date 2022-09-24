from .v2 import *


response_model_generation_result = api.model('Generation', {
    'img': fields.String(title="Generated Image", description="The generated image as a Base64-encoded .webp file"),
    'seed': fields.String(title="Generation Seed", description="The seed which generated this image"),
    'worker_id': fields.String(title="Worker ID", description="The UUID of the worker which generated this image"),
    'worker_name': fields.String(title="Worker Name", description="The name of the worker which generated this image"),
})
response_model_wp_status_full = api.inherit('RequestStatus', response_model_wp_status_lite, {
    'generations': fields.List(fields.Nested(response_model_generation_result)),
})
response_model_generation_payload = api.model('ModelPayload', {
    'prompt': fields.String(description="The prompt which will be sent to Stable Diffusion to generate an image"),
    'ddim_steps': fields.Integer(example=50), 
    'sampler_name': fields.String(enum=["k_lms", "k_heun", "k_euler", "k_euler_a", "k_dpm_2", "k_dpm_2_a", "DDIM", "PLMS"]), 
    'toggles': fields.List(fields.Integer,example=[1,4], description="Special Toggles used in the SD Webui. To be documented."), 
    'realesrgan_model_name': fields.String,
    'ddim_eta': fields.Float, 
    'n_iter': fields.Integer(example=1, description="The amount of images to generate"), 
    'batch_size': fields.Integer(example=1), 
    'cfg_scale': fields.Float(example=5.0), 
    'seed': fields.String(description="The seed to use to generete this request"),
    'height': fields.Integer(example=512,description="The height of the image to generate"), 
    'width': fields.Integer(example=512,description="The width of the image to generate"), 
    'fp': fields.Integer(example=512), 
    'variant_amount': fields.Float, 
    'variant_seed': fields.Integer
})
response_model_generations_skipped = api.model('NoValidRequestFound', {
    'worker_id': fields.Integer(description="How many waiting requests were skipped because they demanded a specific worker"),
    'max_pixels': fields.Integer(description="How many waiting requests were skipped because they demanded a higher size than this worker provides"),
})
response_model_worker_details = api.model('WorkerDetails', {
    "name": fields.String(description="The Name given to this worker"),
    "id": fields.String(description="The UUID of this worker"),
    "max_pixels": fields.Integer(example=262144,description="The maximum pixels in resolution this workr can generate"),
    "megapixelsteps_generated": fields.Float(description="How many megapixelsteps this worker has generated until now"),
    "requests_fulfilled": fields.Integer(description="How many images this worker has generated"),
    "kudos_rewards": fields.Float(description="How many Kudos this worker has been rewarded in total"),
    "kudos_details": fields.Nested(response_model_worker_kudos_details),
    "performance": fields.String(description="The average performance of this worker in human readable form"),
    "uptime": fields.Integer(description="The amount of seconds this worker has been online for this Horde"),
    "maintenance_mode": fields.Boolean(description="When True, this worker will not pick up any new requests"),
})
response_model_use_contrib_details = api.model('UsageAndContribDetails', {
    "megapixelsteps": fields.Float(description="How many megapixelsteps this user has generated or requested"),
    "fulfillments": fields.Integer(description="How many images this user has generated or requested")
})
response_model_horde_performance = api.model('HordePerformance', {
    "queued_requests": fields.Integer(description="The amount of waiting and processing requests currently in this Horde"),
    "queued_megapixelsteps": fields.Float(description="The amount of megapixelsteps in waiting and processing requests currently in this Horde"),
    "past_minute_megapixelsteps": fields.Float(description="How many megapixelsteps this Horde generated in the last minute"),
    "worker_count": fields.Integer(description="How many workers are actively processing image generations in this Horde in the past 5 minutes"),
})

# generate_parser.add_argument("models", type=str, action='append', required=False, default=[], help="Models", location="json")
# generate_parser.add_argument("test", type=str, required=True, help="Models", location="json")

class AsyncGenerate(AsyncGenerateTemplate):
    
    def validate(self):
        super().validate()
        if self.args["params"].get("length",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])


class SyncGenerate(SyncGenerateTemplate):

    @api.expect(generate_parser)
    @api.marshal_with(response_model_wp_status_full, code=200, description='Images Generated')
    @api.response(400, 'Validation Error', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(503, 'Maintenance Mode', response_model_error)
    @api.response(429, 'Too Many Prompts', response_model_error)
    def post(self):
        '''Initiate a Synchronous request to generate images.
        This connection will only terminate when the images have been generated, or an error occured.
        If you connection is interrupted, you will not have the request UUID, so you cannot retrieve the images asynchronously.
        '''
        return(super().post())

    def validate(self):
        super().validate()
        if self.args["params"].get("length",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])

# I need to override it just for the decorators :-/
class AsyncStatus(AsyncStatusTemplate):
    decorators = [limiter.limit("2/minute", key_func = get_request_path)]
    @api.marshal_with(response_model_wp_status_full, code=200, description='Async Request Full Status')
    @api.response(404, 'Request Not found', response_model_error)
    def get(self, id = ''):
        '''Retrieve the full status of an Asynchronous generation request.
        This request will include all already generated images in base64 encoded .webp files.
        As such, you are requested to not retrieve this endpoint often. Instead use the /check/ endpoint first
        This endpoint is limited to 1 request per minute
        '''
        return(super().get(id))

job_pop_parser.add_argument("max_pixels", type=int, required=False, default=512, help="The maximum amount of pixels this worker can generate", location="json")

class JobPop(JobPopTemplate):

    decorators = [limiter.limit("2/second")]
    @api.expect(job_pop_parser)
    @api.marshal_with(response_model_job_pop, code=200, description='Generation Popped')
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(403, 'Access Denied', response_model_error)
    def post(self):
        '''Check if there are generation requests queued for fulfillment.
        This endpoint is used by registered workers only
        '''
        super().post()


    def check_in(self):
        self.worker.check_in(self.args['max_pixels'])
  
job_submit_parser.add_argument("seed", type=str, required=True, default=[], help="The seed of the generation", location="json")

class JobSubmit(JobSubmitTemplate):

    @api.expect(job_submit_parser)
    @api.marshal_with(response_model_job_submit, code=200, description='Generation Submitted')
    @api.response(400, 'Generation Already Submitted', response_model_error)
    @api.response(401, 'Invalid API Key', response_model_error)
    @api.response(402, 'Access Denied', response_model_error)
    @api.response(404, 'Request Not Found', response_model_error)
    def post(self):
        '''Submit a generated image.
        This endpoint is used by registered workers only
        '''
        super().post()


class HordeLoad(HordeLoadTemplate):
    decorators = [limiter.limit("20/minute")]
    @logger.catch
    @api.marshal_with(response_model_horde_performance, code=200, description='Horde Performance')
    def get(self):
        '''Details about the current performance of this Horde
        '''
        load_dict = waiting_prompts.count_totals()
        load_dict["past_minute_megapixelsteps"] = db.stats.get_things_per_min()
        load_dict["worker_count"] = db.count_active_workers()
        return(load_dict,200)


api.add_resource(SyncGenerate, "/generate/sync")
api.add_resource(AsyncGenerate, "/generate/async")
api.add_resource(AsyncStatus, "/generate/status/<string:id>")
api.add_resource(AsyncCheck, "/generate/check/<string:id>")
api.add_resource(JobPop, "/generate/pop")
api.add_resource(JobSubmit, "/generate/submit")
api.add_resource(Users, "/users")
api.add_resource(UserSingle, "/users/<string:user_id>")
api.add_resource(Workers, "/workers")
api.add_resource(WorkerSingle, "/workers/<string:worker_id>")
api.add_resource(TransferKudos, "/kudos/transfer")
api.add_resource(HordeLoad, "/status/performance")
api.add_resource(HordeMaintenance, "/status/maintenance")
