from . import v2
from .v2 import *


response_model_generation_result = api.model('Generation', {
    'img': fields.String(title="Generated Image", description="The generated image as a Base64-encoded .webp file"),
    'seed': fields.String(title="Generation Seed", description="The seed which generated this image"),
    'worker_id': fields.String(title="Worker ID", description="The UUID of the worker which generated this image"),
    'worker_name': fields.String(title="Worker Name", description="The name of the worker which generated this image"),
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
    
    def validate(self):
        super().validate()
        if self.args["params"].get("length",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("width",512)%64:
            raise e.InvalidSize(self.username)
        if self.args["params"].get("steps",50) > 100:
            raise e.TooManySteps(self.username, self.args['params']['steps'])

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
        load_dict["past_minute_megapixelsteps"] = db.stats.get_megapixelsteps_per_min()
        load_dict["worker_count"] = db.count_active_servers()
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
