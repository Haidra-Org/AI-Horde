from flask_restx import fields
from . import v2
from horde.logger import logger


class Parsers(v2.Parsers):
    def __init__(self):
        self.generate_parser.add_argument("censor_nsfw", type=bool, default=True, required=False, help="If the request is SFW, and the worker accidentaly generates NSFW, it will send back a censored image.", location="json")
        self.generate_parser.add_argument("source_image", type=str, required=False, help="The Base64-encoded webp to use for img2img", location="json")
        self.generate_parser.add_argument("source_processing", type=str, default="img2img", required=False, help="If source_image is provided, specifies how to process it.", location="json")
        self.generate_parser.add_argument("source_mask", type=str, required=False, help="If img_processing is set to 'inpainting' or 'outpainting', this parameter can be optionally provided as the mask of the areas to inpaint. If this arg is not passed, the inpainting/outpainting mask has to be embedded as alpha channel", location="json")
        self.generate_parser.add_argument("models", type=list, required=False, default=['stable_diffusion'], help="The acceptable models with which to generate", location="json")
        self.generate_parser.add_argument("r2", type=bool, default=False, required=False, help="If True, the image will be sent via cloudflare r2 download link", location="json")
        self.generate_parser.add_argument("shared", type=bool, default=False, required=False, help="If True, The image will be shared with LAION for improving their dataset. This will also reduce your kudos consumption by 2. For anonymous users, this is always True.", location="json")
        self.job_pop_parser.add_argument("max_pixels", type=int, required=False, default=512*512, help="The maximum amount of pixels this worker can generate", location="json")
        self.job_pop_parser.add_argument("allow_img2img", type=bool, required=False, default=True, help="If True, this worker will pick up img2img requests", location="json")
        self.job_pop_parser.add_argument("allow_painting", type=bool, required=False, default=True, help="If True, this worker will pick up inpainting/outpaining requests", location="json")
        self.job_pop_parser.add_argument("allow_unsafe_ipaddr", type=bool, required=False, default=True, help="If True, this worker will pick up img2img requests coming from clients with an unsafe IP.", location="json")
        self.job_pop_parser.add_argument("allow_post_processing", type=bool, required=False, default=True, help="If True, this worker will pick up requests requesting post-processing.", location="json")
        self.job_submit_parser.add_argument("seed", type=str, required=True, default='', help="The seed of the generation", location="json")

class Models(v2.Models):
    def __init__(self,api):

        super().__init__(api)

        self.response_model_generation_result = api.inherit('GenerationStable', self.response_model_generation_result, {
            'img': fields.String(title="Generated Image", description="The generated image as a Base64-encoded .webp file"),
            'seed': fields.String(title="Generation Seed", description="The seed which generated this image"),
            'id': fields.String(title="Generation ID", description="The ID for this image"),
        })
        self.response_model_wp_status_full = api.inherit('RequestStatusStable', self.response_model_wp_status_lite, {
            'generations': fields.List(fields.Nested(self.response_model_generation_result)),
        })
        self.root_model_generation_payload_stable = api.model('ModelPayloadRootStable', {
            'sampler_name': fields.String(required=False, default='k_euler_a',enum=["k_lms", "k_heun", "k_euler", "k_euler_a", "k_dpm_2", "k_dpm_2_a", "k_dpm_fast", "k_dpm_adaptive", "k_dpmpp_2s_a", "k_dpmpp_2m", "dpmsolver"]), 
            'toggles': fields.List(fields.Integer,required=False, example=[1,4], description="Obsolete Toggles used in the SD Webui. To be removed. Do not modify unless you know what you're doing."), 
            'cfg_scale': fields.Float(required=False,default=5.0, min=-40, max=30, multiple=0.5), 
            'denoising_strength': fields.Float(required=False,example=0.75, min=0, max=1.0), 
            'seed': fields.String(required=False,description="The seed to use to generete this request"),
            'height': fields.Integer(required=False, default=512, description="The height of the image to generate", min=64, max=3072, multiple=64), 
            'width': fields.Integer(required=False, default=512, description="The width of the image to generate", min=64, max=3072, multiple=64), 
            'seed_variation': fields.Integer(required=False, example=1, min = 1, max=1000, description="If passed with multiple n, the provided seed will be incremented every time by this value"),
            'post_processing': fields.List(fields.String(description="The list of post-processors to apply to the image, in the order to be applied",enum=["GFPGAN", "RealESRGAN_x4plus", "CodeFormers"]),unique=True),
            'karras': fields.Boolean(default=False,description="Set to True to enable karras noise scheduling tweaks"),
        })
        self.response_model_generation_payload = api.inherit('ModelPayloadStable', self.root_model_generation_payload_stable, {
            'prompt': fields.String(description="The prompt which will be sent to Stable Diffusion to generate an image"),
            'ddim_steps': fields.Integer(default=30), 
            'n_iter': fields.Integer(default=1, description="The amount of images to generate"), 
            'use_nsfw_censor': fields.Boolean(description="When true will apply NSFW censoring model on the generation"),
            'use_embeds': fields.Boolean(default=False, description="When true will use embeddings from the concepts library when doing the generation"),
        })
        self.input_model_generation_payload = api.inherit('ModelGenerationInputStable', self.root_model_generation_payload_stable, {
            'steps': fields.Integer(default=30, required=False, min = 1, max=500), 
            'n': fields.Integer(default=1, required=False, description="The amount of images to generate", min = 1, max=20), 
        })
        self.response_model_generations_skipped = api.inherit('NoValidRequestFoundStable', self.response_model_generations_skipped, {
            'max_pixels': fields.Integer(description="How many waiting requests were skipped because they demanded a higher size than this worker provides"),
            'unsafe_ip': fields.Integer(description="How many waiting requests were skipped because they came from an unsafe IP"),
            'img2img': fields.Integer(description="How many waiting requests were skipped because they requested img2img"),
            'painting': fields.Integer(description="How many waiting requests were skipped because they requested inpainting/outpainting"),
            'post-processing': fields.Integer(description="How many waiting requests were skipped because they requested post-processing"),
            'kudos': fields.Integer(description="How many waiting requests were skipped because the user didn't have enough kudos when this worker requires upfront kudos"),
        })
        self.response_model_job_pop = api.model('GenerationPayload', {
            'payload': fields.Nested(self.response_model_generation_payload,skip_none=True),
            'id': fields.String(description="The UUID for this image generation"),
            'skipped': fields.Nested(self.response_model_generations_skipped, skip_none=True),
            'model': fields.String(description="Which of the available models to use for this request"),
            'source_image': fields.String(description="The Base64-encoded webp to use for img2img"),
            'source_processing': fields.String(required=False, default='img2img',enum=["img2img", "inpainting", "outpainting"], description="If source_image is provided, specifies how to process it."), 
            'source_mask': fields.String(description="If img_processing is set to 'inpainting' or 'outpainting', this parameter can be optionally provided as the mask of the areas to inpaint. If this arg is not passed, the inpainting/outpainting mask has to be embedded as alpha channel"),
            'r2_upload': fields.String(description="The r2 upload link to use to upload this image"),
        })
        self.input_model_job_pop = api.inherit('PopInputStable', self.input_model_job_pop, {
            'max_pixels': fields.Integer(default=512*512,description="The maximum amount of pixels this worker can generate"), 
            'allow_img2img': fields.Boolean(default=True,description="If True, this worker will pick up img2img requests"),
            'allow_painting': fields.Boolean(default=True,description="If True, this worker will pick up inpainting/outpainting requests"),
            'allow_unsafe_ipaddr': fields.Boolean(default=True,description="If True, this worker will pick up img2img requests coming from clients with an unsafe IP."),
            'allow_post_processing': fields.Boolean(default=True,description="If True, this worker will pick up requests requesting post-processing."),
            'require_upfront_kudos': fields.Boolean(description="If True, then will only pick up requests where the users has the required kudos for them already."),
        })

        self.input_model_request_generation = api.model('GenerationInput', {
            'prompt': fields.String(required=True,description="The prompt which will be sent to Stable Diffusion to generate an image", min_length = 1),
            'params': fields.Nested(self.input_model_generation_payload, skip_none=True),
            'nsfw': fields.Boolean(default=False,description="Set to true if this request is NSFW. This will skip workers which censor images."),
            'trusted_workers': fields.Boolean(default=True,description="When true, only trusted workers will serve this request. When False, Evaluating workers will also be used which can increase speed but adds more risk!"),
            'censor_nsfw': fields.Boolean(default=False,description="If the request is SFW, and the worker accidentaly generates NSFW, it will send back a censored image."),
            'workers': fields.List(fields.String(description="Specify which workers are allowed to service this request")),
            'models': fields.List(fields.String(description="Specify which models are allowed to be used for this request")),
            'source_image': fields.String(required=False, description="The Base64-encoded webp to use for img2img"),
            'source_processing': fields.String(required=False, default='img2img',enum=["img2img", "inpainting", "outpainting"], description="If source_image is provided, specifies how to process it."), 
            'source_mask': fields.String(description="If source_processing is set to 'inpainting' or 'outpainting', this parameter can be optionally provided as the  Base64-encoded webp mask of the areas to inpaint. If this arg is not passed, the inpainting/outpainting mask has to be embedded as alpha channel"),
            'r2': fields.Boolean(default=False, description="If True, the image will be sent via cloudflare r2 download link"),
            'shared': fields.Boolean(default=False, description="If True, The image will be shared with LAION for improving their dataset. This will also reduce your kudos consumption by 2. For anonymous users, this is always True."),
        })
        self.response_model_worker_details = api.inherit('WorkerDetailsStable', self.response_model_worker_details, {
            "max_pixels": fields.Integer(example=262144,description="The maximum pixels in resolution this worker can generate"),
            "megapixelsteps_generated": fields.Float(description="How many megapixelsteps this worker has generated until now"),
            'img2img': fields.Boolean(default=None,description="If True, this worker supports and allows img2img requests."),
            'painting': fields.Boolean(default=None,description="If True, this worker supports and allows inpainting requests."),
            'post-processing': fields.Boolean(default=None,description="If True, this worker supports and allows post-processing requests."),
        })
        self.response_model_contrib_details = api.inherit('ContributionsDetailsStable', self.response_model_contrib_details, {
            "megapixelsteps": fields.Float(description="How many megapixelsteps this user has generated"),
        })
        self.response_model_use_details = api.inherit('UsageDetailsStable', self.response_model_use_details, {
            "megapixelsteps": fields.Float(description="How many megapixelsteps this user has requested"),
        })
        self.response_model_user_details = api.inherit('UserDetailsStable', self.response_model_user_details, {
            "kudos_details": fields.Nested(self.response_model_user_kudos_details),
            "usage": fields.Nested(self.response_model_use_details),
            "contributions": fields.Nested(self.response_model_contrib_details),
        })
        self.response_model_horde_performance = api.inherit('HordePerformanceStable', self.response_model_horde_performance, {
            "queued_megapixelsteps": fields.Float(description="The amount of megapixelsteps in waiting and processing requests currently in this Horde"),
            "past_minute_megapixelsteps": fields.Float(description="How many megapixelsteps this Horde generated in the last minute"),
            "queued_forms": fields.Float(description="The amount of image interrogations waiting and processing currently in this Horde"),
            "interrogator_count": fields.Integer(description="How many workers are actively processing image interrogations in this Horde in the past 5 minutes"),
            "interrogator_thread_count": fields.Integer(description="How many worker threads are actively processing image interrogation in this Horde in the past 5 minutes"),
        })
        self.response_model_team_details = api.inherit('TeamDetailsStable', self.response_model_team_details, {
            "contributions": fields.Float(description="How many megapixelsteps the workers in this team have been rewarded while part of this team."),
            "performance": fields.Float(description="The average performance of the workers in this team, in megapixelsteps per second."),
            "speed": fields.Float(description="The total expected speed of this team when all workers are working in parallel, in megapixelsteps per second."),
        })
        # Intentionally left blank to allow to add payloads later
        self.input_model_interrogation_form_payload = api.model('ModelInterrogationFormPayloadStable', {
            "*": fields.Wildcard(fields.String)
        })
        self.input_model_interrogation_form = api.model('ModelInterrogationFormStable', {
            'name': fields.String(required=True, enum=["caption", "interrogation", "nsfw"], description="The type of interrogation this is", unique=True), 
            'payload': fields.Nested(self.input_model_interrogation_form_payload, skip_none=True), 
        })
        self.input_interrogate_request_generation = api.model('ModelInterrogationInputStable', {
            'forms': fields.List(fields.Nested(self.input_model_interrogation_form)),
            'source_image': fields.String(required=False, description="The public URL of the image to interrogate"),
        })
        self.response_model_interrogation = api.model('RequestInterrogationResponse', {
            'id': fields.String(description="The UUID of the request. Use this to retrieve the request status in the future"),
            'message': fields.String(default=None,description="Any extra information from the horde about this request"),
        })
        self.response_model_interrogation_result = api.model('InterrogationResult', {
            'worker_id': fields.String(title="Worker ID", description="The UUID of the worker which interrogated this image"),
            'worker_name': fields.String(title="Worker Name", description="The name of the worker which interrogated this image"),
            'form': fields.String(title="Interrogation Form", description="The form which interrogated this image"),
            'state': fields.String(title="Interrogation Form State", description="The status of this interrogation form"),
            'nsfw': fields.Boolean(title="NSFW", description="If true, this image has been detected to have NSFW context"),
            'caption': fields.String(title="Caption", description="The caption generated for this image")
        })
        # Intentionally left blank to allow to add payloads later
        self.response_model_interrogation_form_result = api.model('InterrogationFormResult', {
            "*": fields.Wildcard(fields.Raw)
        })
        self.response_model_interrogation_form_status = api.model('InterrogationFormStatus', {
            'form': fields.String(description="The name of this interrogation form"),
            'state': fields.String(title="Interrogation State", description="The overall status of this interrogation"),
            'result': fields.Nested(self.response_model_interrogation_form_result, skip_none=True)
        })
        self.response_model_interrogation_status = api.model('InterrogationStatus', {
            'state': fields.String(title="Interrogation State", description="The overall status of this interrogation"),
            'forms': fields.List(fields.Nested(self.response_model_interrogation_form_status, skip_none=True)),
        })
        self.input_model_interrogation_pop = api.model('InterrogationPopInput', {
            'name': fields.String(description="The Name of the Worker"),
            'priority_usernames': fields.List(fields.String(description="Users with priority to use this worker")),
            'forms': fields.List(fields.String(description="The type of interrogation this worker can fulfil", enum=["caption", "interrogation", "nsfw"], unique=True)),
            'amount': fields.Integer(default=1, description="The amount of forms to pop at the same time"),
            'bridge_version': fields.Integer(default=1, description="The version of the bridge used by this worker"),
            'threads': fields.Integer(default=1, description="How many threads this worker is running. This is used to accurately the current power available in the horde",min=1, max=10),
        })
        self.response_model_interrogation_pop_payload = api.model('InterrogationPopFormPayload', {
            'id': fields.String(description="The UUID of the interrogation form. Use this to post the results in the future"),
            'form': fields.String(description="The name of this interrogation form", enum=["caption", "interrogation", "nsfw"]),
            'payload': fields.Nested(self.input_model_interrogation_form_payload, skip_none=True), 
            'source_image': fields.String(description="The URL From which the source image can be downloaded"),
        })
        self.response_model_interrogation_forms_skipped = api.model('NoValidInterrogationsFound', {
            'worker_id': fields.Integer(description="How many waiting requests were skipped because they demanded a specific worker", min=0),
            'untrusted': fields.Integer(description="How many waiting requests were skipped because they demanded a trusted worker which this worker is not.", min=0),
            'bridge_version': fields.Integer(example=0,description="How many waiting requests were skipped because they require a higher version of the bridge than this worker is running (upgrade if you see this in your skipped list).", min=0),
        })
        self.response_model_interrogation_pop = api.model('InterrogationPopPayload', {
            'forms': fields.List(fields.Nested(self.response_model_interrogation_pop_payload, skip_none=True)),
            'skipped': fields.Nested(self.response_model_interrogation_forms_skipped, skip_none=True)
        })
        self.response_model_aesthetic_rating = api.model('AestheticRating', {
            "id": fields.String(required=True,description="The UUID of image being rated",min_length=36,max_length=36),
            "rating": fields.Integer(required=True,description="The aesthetic rating 1-10 for this image", min=1, max=10),
        })
        self.input_model_aesthetics_payload = api.model('AestheticsPayload', {
            "best": fields.String(required=False,description="The UUID of the best image in this generation batch (only used when 2+ images generated). If 2+ aesthetic ratings are also provided, then they take precedence if they're not tied.",min_length=36,max_length=36),
            "ratings": fields.List(fields.Nested(self.response_model_aesthetic_ratings, skip_none=True),required=False),
        })
