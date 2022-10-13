from flask_restx import fields, reqparse
from . import v2


class Parsers(v2.Parsers):
    def __init__(self):
        self.generate_parser.add_argument("models", type=list, required=False, default=[], help="The acceptable models with which to generate", location="json")
        self.generate_parser.add_argument("softprompts", type=list, required=False, default=[''], help="If specified, only servers who can load this softprompt will generate this request", location="json")
        self.job_pop_parser.add_argument("model", type=str, required=True, help="The model currently running on this KoboldAI", location="json")
        self.job_pop_parser.add_argument("max_length", type=int, required=False, default=512, help="The maximum amount of tokens this worker can generate", location="json")
        self.job_pop_parser.add_argument("max_content_length", type=int, required=False, default=2048, help="The max amount of context to submit to this AI for sampling.", location="json")
        self.job_pop_parser.add_argument("softprompts", type=list, required=False, default=[], help="The available softprompt files on this worker for the currently running model", location="json")
        self.job_submit_parser.add_argument("seed", type=str, required=False, default='', help="The seed of the generation", location="json")

class Models(v2.Models):
    def __init__(self,api):

        super().__init__(api)

        self.response_model_generation_result = api.inherit('GenerationKobold', self.response_model_generation_result, {
            'text': fields.String(title="Generated Text", description="The generated text."),
            # 'seed': fields.String(title="Generation Seed", description="The seed which generated this image"),
        })
        self.response_model_wp_status_full = api.inherit('RequestStatusKobold', self.response_model_wp_status_lite, {
            'generations': fields.List(fields.Nested(self.response_model_generation_result)),
        })
        self.root_model_generation_payload_kobold = api.model('ModelPayloadRootKobold', {
            'n': fields.Integer(example=1), 
            'frmtadsnsp': fields.Boolean(example=False,description="Input formatting option. When enabled, adds a leading space to your input if there is no trailing whitespace at the end of the previous action."),
            'frmtrmblln': fields.Boolean(example=False,description="Output formatting option. When enabled, replaces all occurrences of two or more consecutive newlines in the output with one newline."),
            'frmtrmspch': fields.Boolean(example=False,description="Output formatting option. When enabled, removes #/@%}{+=~|\^<> from the output."),
            'frmttriminc': fields.Boolean(example=False,description="Output formatting option. When enabled, removes some characters from the end of the output such that the output doesn't end in the middle of a sentence. If the output is less than one sentence long, does nothing."),
            'max_context_length': fields.Integer(min=80, max=2048, example=1024, description="Maximum number of tokens to send to the model."), 
            'max_length': fields.Integer(min=16, max=512, description="Number of tokens to generate."), 
            'rep_pen': fields.Float(description="Base repetition penalty value."), 
            'rep_pen_range': fields.Integer(description="Repetition penalty range."), 
            'rep_pen_slope': fields.Float(description="Repetition penalty slope."), 
            'singleline': fields.Boolean(example=False,description="Output formatting option. When enabled, removes everything after the first line of the output, including the newline."),
            'soft_prompt': fields.String(description="Soft prompt to use when generating. If set to the empty string or any other string containing no non-whitespace characters, uses no soft prompt."),
            'temperature': fields.Float(description="Temperature value."), 
            'tfs': fields.Float(description="Tail free sampling value."), 
            'top_a': fields.Float(description="Top-a sampling value."), 
            'top_k': fields.Integer(description="Top-k sampling value."), 
            'top_p': fields.Float(description="Top-p sampling value."), 
            'typical': fields.Float(description="Typical sampling value."), 
        })
        self.response_model_generation_payload = api.inherit('ModelPayloadKobold', self.root_model_generation_payload_kobold, {
            'prompt': fields.String(description="The prompt which will be sent to Kobold Diffusion to generate an image"),
        })
        self.input_model_generation_payload = api.inherit('ModelGenerationInputKobold', self.root_model_generation_payload_kobold, {
        })
        self.response_model_generations_skipped = api.inherit('NoValidRequestFoundKobold', self.response_model_generations_skipped, {
            'models': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded a different model than what this worker provides."),
            'max_content_length': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded a higher max_content_length than what this worker provides."),
            'max_length': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded more generated tokens that what this worker can provide."),
            'matching_softprompt': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded an available soft-prompt which this worker does not have."),
        })
        self.response_model_job_pop = api.model('GenerationPayload', {
            'payload': fields.Nested(self.response_model_generation_payload,skip_none=True),
            'id': fields.String(description="The UUID for this image generation"),
            'skipped': fields.Nested(self.response_model_generations_skipped,skip_none=True),
            'softprompt': fields.String(description="The soft prompt requested for this generation"),
        })
        self.input_model_request_generation = api.model('GenerationInput', {
            'prompt': fields.String(description="The prompt which will be sent to KoboldAI to generate an image"),
            'params': fields.Nested(self.input_model_generation_payload,skip_none=True),
            'workers': fields.List(fields.String(description="Specify which workers are allowed to service this request")),
            'models': fields.List(fields.String(description="Specify which models are allowed to service this request")),
            'softprompts': fields.List(fields.String(description="Specify which softpompts need to be used to service this request")),
            'trusted_workers': fields.Boolean(default=True,description="When true, only trusted workers will serve this request. When False, Evaluating workers will also be used which can increase speed but adds more risk!"),
            'nsfw': fields.Boolean(default=False,description="Set to true if this request is NSFW. This will skip workers censor text."),
        })
        self.response_model_worker_details = api.inherit('WorkerDetailsKobold', self.response_model_worker_details, {
            "max_length": fields.Integer(example=80,description="The maximum tokens this worker can generate"),
            "max_content_length": fields.Integer(example=80,description="The maximum tokens this worker can read"),
            "tokens_generated": fields.Float(description="How many tokens this worker has generated until now"),
        })
        self.response_model_contrib_details = api.inherit('ContributionsDetailsKobold', self.response_model_contrib_details, {
            "tokens": fields.Float(description="How many tokens this user has generated"),
        })
        self.response_model_use_details = api.inherit('UsageDetailsKobold', self.response_model_use_details, {
            "tokens": fields.Float(description="How many tokens this user has requested"),
        })
        self.response_model_user_details = api.inherit('UserDetailsKobold', self.response_model_user_details, {
            "kudos_details": fields.Nested(self.response_model_user_kudos_details),
            "usage": fields.Nested(self.response_model_use_details),
            "contributions": fields.Nested(self.response_model_contrib_details),
        })
        self.response_model_horde_performance = api.inherit('HordePerformanceKobold', self.response_model_horde_performance, {
            "queued_requests": fields.Integer(description="The amount of waiting and processing requests currently in this Horde"),
            "queued_tokens": fields.Float(description="The amount of tokens in waiting and processing requests currently in this Horde"),
            "past_minute_tokens": fields.Float(description="How many tokens this Horde generated in the last minute"),
            "worker_count": fields.Integer(description="How many workers are actively processing text generations in this Horde in the past 5 minutes"),
        })
        self.response_model_model = api.model('Model', {
            'name': fields.String(description="The Name of a model available by workers in this horde."),
            'count': fields.Integer(description="How many of workers in this horde are running this model."),
        })
        