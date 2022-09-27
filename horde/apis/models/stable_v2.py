from flask_restx import fields, reqparse
from . import v2


class Parsers(v2.Parsers):
    def __init__(self):
        self.job_pop_parser.add_argument("max_pixels", type=int, required=False, default=512, help="The maximum amount of pixels this worker can generate", location="json")
        self.job_submit_parser.add_argument("seed", type=str, required=True, default=[], help="The seed of the generation", location="json")

class Models(v2.Models):
    def __init__(self,api):

        super().__init__(api)

        self.response_model_generation_result = api.inherit('GenerationStable', self.response_model_generation_result, {
            'img': fields.String(title="Generated Image", description="The generated image as a Base64-encoded .webp file"),
            'seed': fields.String(title="Generation Seed", description="The seed which generated this image"),
        })
        self.response_model_wp_status_full = api.inherit('RequestStatusStable', self.response_model_wp_status_lite, {
            'generations': fields.List(fields.Nested(self.response_model_generation_result)),
        })
        self.response_model_generation_payload = api.model('ModelPayload', {
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
        self.response_model_generations_skipped = api.inherit('NoValidRequestFoundStable', self.response_model_generations_skipped, {
            'max_pixels': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded a higher size than this worker provides"),
        })
        self.response_model_job_pop = api.model('GenerationPayload', {
            'payload': fields.Nested(self.response_model_generation_payload,skip_none=True),
            'id': fields.String(description="The UUID for this image generation"),
            'skipped': fields.Nested(self.response_model_generations_skipped,skip_none=True)
        })
        self.response_model_worker_details = api.inherit('WorkerDetailsStable', self.response_model_worker_details, {
            "max_pixels": fields.Integer(example=262144,description="The maximum pixels in resolution this workr can generate"),
            "megapixelsteps_generated": fields.Float(description="How many megapixelsteps this worker has generated until now"),
        })
        self.response_model_contrib_details = api.inherit('ContributionsDetailsStable', self.response_model_contrib_details, {
            "megapixelsteps": fields.Float(description="How many megapixelsteps this user has generated"),
        })
        self.response_model_use_details = api.inherit('UsageDetailsStable', self.response_model_use_details, {
            "megapixelsteps": fields.Float(description="How many megapixelsteps this user has requested"),
        })
        self.response_model_user_details = api.model('UserDetails', {
            "username": fields.String(description="The user's unique Username. It is a combination of their chosen alias plus their ID."),
            "id": fields.Integer(description="The user unique ID. It is always an integer."),
            "kudos": fields.Float(description="The amount of Kudos this user has. Can be negative. The amount of Kudos determines the priority when requesting image generations."),
            "kudos_details": fields.Nested(self.response_model_user_kudos_details),
            "usage": fields.Nested(self.response_model_use_details),
            "contributions": fields.Nested(self.response_model_contrib_details),
            "concurrency": fields.Integer(description="How many concurrent image generations this user may request."),    
        })
        self.response_model_horde_performance = api.inherit('HordePerformanceStable', self.response_model_horde_performance, {
            "queued_requests": fields.Integer(description="The amount of waiting and processing requests currently in this Horde"),
            "queued_megapixelsteps": fields.Float(description="The amount of megapixelsteps in waiting and processing requests currently in this Horde"),
            "past_minute_megapixelsteps": fields.Float(description="How many megapixelsteps this Horde generated in the last minute"),
            "worker_count": fields.Integer(description="How many workers are actively processing image generations in this Horde in the past 5 minutes"),
        })