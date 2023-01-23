from flask_restx import fields, reqparse


class Parsers:

    generate_parser = reqparse.RequestParser()
    generate_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    generate_parser.add_argument("Client-Agent", default="unknown:0:unknown", type=str, required=False, help="The client name and version", location="headers")
    generate_parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from", location="json")
    generate_parser.add_argument("params", type=dict, required=False, help="Extra generate params to send to the worker", location="json")
    generate_parser.add_argument("trusted_workers", type=bool, required=False, default=False, help="When true, only Horde trusted workers will serve this request. When False, Evaluating workers will also be used.", location="json")
    generate_parser.add_argument("workers", type=list, required=False, help="If specified, only the worker with this ID will be able to generate this prompt", location="json")
    generate_parser.add_argument("nsfw", type=bool, default=True, required=False, help="Marks that this request expects or allows NSFW content. Only workers with the nsfw flag active will pick this request up.", location="json")

    # The parser for RequestPop
    job_pop_parser = reqparse.RequestParser()
    job_pop_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    job_pop_parser.add_argument("name", type=str, required=True, help="The worker's unique name, to track contributions", location="json")
    job_pop_parser.add_argument("priority_usernames", type=list, required=False, help="The usernames which get priority use on this worker", location="json")
    job_pop_parser.add_argument("nsfw", type=bool, default=True, required=False, help="Marks that this worker is capable of generating NSFW content", location="json")
    job_pop_parser.add_argument("blacklist", type=list, required=False, help="Specifies the words that this worker will not accept in a prompt.", location="json")
    job_pop_parser.add_argument("models", type=list, required=False, help="The models currently available on this worker", location="json")
    job_pop_parser.add_argument("bridge_version", type=int, required=False, default=1, help="Specify the version of the worker bridge, as that can modify the way the arguments are being sent", location="json")
    job_pop_parser.add_argument("bridge_agent", type=str, required=False, default="unknown", location="json")
    job_pop_parser.add_argument("threads", type=int, required=False, default=1, help="How many threads this worker is running. This is used to accurately the current power available in the horde", location="json")
    job_pop_parser.add_argument("require_upfront_kudos", type=bool, required=False, default=False, help="If True, this worker will only pick up requests where the owner has the required kudos to consume already available.", location="json")

    job_submit_parser = reqparse.RequestParser()
    job_submit_parser.add_argument("apikey", type=str, required=True, help="The worker's owner API key", location='headers')
    job_submit_parser.add_argument("id", type=str, required=True, help="The processing generation uuid", location="json")
    job_submit_parser.add_argument("generation", type=str, required=True, help="The generated output", location="json")


class Models:
    def __init__(self,api):
        self.response_model_wp_status_lite = api.model('RequestStatusCheck', {
            'finished': fields.Integer(description="The amount of finished jobs in this request"),
            'processing': fields.Integer(description="The amount of still processing jobs in this request"),
            'restarted': fields.Integer(description="The amount of jobs that timed out and had to be restarted or were reported as failed by a worker"),
            'waiting': fields.Integer(description="The amount of jobs waiting to be picked up by a worker"),
            'done': fields.Boolean(description="True when all jobs in this request are done. Else False."),
            'faulted': fields.Boolean(default=False,description="True when this request caused an internal server error and could not be completed."),
            'wait_time': fields.Integer(description="The expected amount to wait (in seconds) to generate all jobs in this request"),
            'queue_position': fields.Integer(description="The position in the requests queue. This position is determined by relative Kudos amounts."),
            "kudos": fields.Float(description="The amount of total Kudos this request has consumed until now."),
            "is_possible": fields.Boolean(default=True,description="If False, this request will not be able to be completed with the pool of workers currently available"),
        })
        self.response_model_worker_details_lite = api.model('WorkerDetailsLite', {
            "name": fields.String(description="The Name given to this worker."),
            "id": fields.String(description="The UUID of this worker."),
            "online": fields.Boolean(description="True if the worker has checked-in the past 5 minutes."),
        })
        self.response_model_team_details_lite = api.model('TeamDetailsLite', {
            "name": fields.String(description="The Name given to this team."),
            "id": fields.String(description="The UUID of this team."),
        })
        self.response_model_active_model_lite = api.model('ActiveModelLite', {
            'name': fields.String(description="The Name of a model available by workers in this horde."),
            'count': fields.Integer(description="How many of workers in this horde are running this model."),
        })
        self.response_model_generation_result = api.model('Generation', {
            'worker_id': fields.String(title="Worker ID", description="The UUID of the worker which generated this image"),
            'worker_name': fields.String(title="Worker Name", description="The name of the worker which generated this image"),
            'model': fields.String(title="Generation Model", description="The model which generated this image"),
        })
        self.response_model_wp_status_full = api.inherit('RequestStatus', self.response_model_wp_status_lite, {
            'generations': fields.List(fields.Nested(self.response_model_generation_result)),
        })
        self.response_model_async = api.model('RequestAsync', {
            'id': fields.String(description="The UUID of the request. Use this to retrieve the request status in the future"),
            'message': fields.String(default=None,description="Any extra information from the horde about this request"),
        })
        self.response_model_generation_payload = api.model('ModelPayload', {
            'prompt': fields.String(description="The prompt which will be sent to Stable Diffusion to generate an image"),
            'n': fields.Integer(example=1, description="The amount of images to generate"), 
            'seed': fields.String(description="The seed to use to generete this request"),
        })
        self.response_model_generations_skipped = api.model('NoValidRequestFound', {
            'worker_id': fields.Integer(description="How many waiting requests were skipped because they demanded a specific worker", min=0),
            'performance': fields.Integer(description="How many waiting requests were skipped because they required higher performance", min=0),
            'nsfw': fields.Integer(description="How many waiting requests were skipped because they demanded a nsfw generation which this worker does not provide.", min=0),
            'blacklist': fields.Integer(description="How many waiting requests were skipped because they demanded a generation with a word that this worker does not accept.", min=0),
            'untrusted': fields.Integer(description="How many waiting requests were skipped because they demanded a trusted worker which this worker is not.", min=0),
            'models': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded a different model than what this worker provides.", min=0),
            'bridge_version': fields.Integer(example=0,description="How many waiting requests were skipped because they require a higher version of the bridge than this worker is running (upgrade if you see this in your skipped list).", min=0),
        })

        self.response_model_job_pop = api.model('GenerationPayload', {
            'payload': fields.Nested(self.response_model_generation_payload, skip_none=True),
            'id': fields.String(description="The UUID for this image generation"),
            'skipped': fields.Nested(self.response_model_generations_skipped, skip_none=True)
        })
        self.input_model_job_submit = api.model('SubmitInput', {
            'id': fields.String(required=True, description="The UUID of this generation", example="00000000-0000-0000-0000-000000000000"), 
            'generation': fields.String(example="R2", required=False, description="R2 if the image has been uploaded to R2, or the b64 string of the encoded image."),
        })
        self.response_model_job_submit = api.model('GenerationSubmitted', {
            'reward': fields.Float(example=10.0,description="The amount of kudos gained for submitting this request"),
        })

        self.response_model_kudos_transfer = api.model('KudosTransferred', {
            'transferred': fields.Integer(example=100,description="The amount of Kudos tranferred"),
        })
        self.response_model_kudos_award = api.model('KudosAwarded', {
            'awarded': fields.Integer(example=100,description="The amount of Kudos awarded"),
        })

        self.response_model_admin_maintenance = api.model('MaintenanceModeSet', {
            'maintenance_mode': fields.Boolean(example=True,description="The current state of maintenance_mode"),
        })

        self.response_model_worker_kudos_details = api.model('WorkerKudosDetails', {
            'generated': fields.Float(description="How much Kudos this worker has received for generating images"),
            'uptime': fields.Integer(description="How much Kudos this worker has received for staying online longer"),
        })
        self.input_model_job_pop = api.model('PopInput', {
            'name': fields.String(description="The Name of the Worker"),
            'priority_usernames': fields.List(fields.String(description="Users with priority to use this worker")),
            'nsfw': fields.Boolean(default=False, description="Whether this worker can generate NSFW requests or not."),
            'blacklist': fields.List(fields.String(description="Words which, when detected will refuste to pick up any jobs")),
            'models': fields.List(fields.String(description="Which models this worker is serving",min_length=3,max_length=50)),
            'bridge_version': fields.Integer(default=1,description="The version of the bridge used by this worker"),
            'bridge_agent': fields.String(required=False, default="unknown", example="AI Horde Worker:11:https://github.com/db0/AI-Horde-Worker", description="The worker name, version and website", max_length=1000),
            'threads': fields.Integer(default=1,description="How many threads this worker is running. This is used to accurately the current power available in the horde",min=1, max=10),
            'require_upfront_kudos': fields.Boolean(example=False, default=False, description="If True, this worker will only pick up requests where the owner has the required kudos to consume already available."),
        })
        self.response_model_worker_details = api.inherit('WorkerDetails', self.response_model_worker_details_lite, {
            "requests_fulfilled": fields.Integer(description="How many images this worker has generated."),
            "kudos_rewards": fields.Float(description="How many Kudos this worker has been rewarded in total."),
            "kudos_details": fields.Nested(self.response_model_worker_kudos_details),
            "performance": fields.String(description="The average performance of this worker in human readable form."),
            "threads": fields.Integer(description="How many threads this worker is running."),
            "uptime": fields.Integer(description="The amount of seconds this worker has been online for this Horde."),
            "maintenance_mode": fields.Boolean(example=False,description="When True, this worker will not pick up any new requests"),
            "paused": fields.Boolean(example=False,description="(Privileged) When True, this worker not be given any new requests."),
            "info": fields.String(description="Extra information or comments about this worker provided by its owner.", example="https://dbzer0.com", default=None),
            "nsfw": fields.Boolean(default=False, description="Whether this worker can generate NSFW requests or not."),
            "owner": fields.String(example="username#1", description="Privileged or public if the owner has allowed it. The alias of the owner of this worker."),
            "trusted": fields.Boolean(description="The worker is trusted to return valid generations."),
            "suspicious": fields.Integer(example=0,description="(Privileged) How much suspicion this worker has accumulated"),
            "uncompleted_jobs": fields.Integer(example=0,description="How many jobs this worker has left uncompleted after it started them."),
            'models': fields.List(fields.String(description="Which models this worker if offerring")),
            'team': fields.Nested(self.response_model_team_details_lite, "The Team to which this worker is dedicated."),
            "contact": fields.String(example="email@example.com", description="(Privileged) Contact details for the horde admins to reach the owner of this worker in emergencies.",min_length=5,max_length=500),
        })

        self.input_model_worker_modify = api.model('ModifyWorkerInput', {
            "maintenance": fields.Boolean(description="Set to true to put this worker into maintenance."),
            "maintenance_msg": fields.String(description="if maintenance is True, you can optionally provide a message to be used instead of the default maintenance message, so that the owner is informed."),
            "paused": fields.Boolean(description="(Mods only) Set to true to pause this worker."),
            "info": fields.String(description="You can optionally provide a server note which will be seen in the server details. No profanity allowed!",min_length=2,max_length=1000),
            "name": fields.String(description="When this is set, it will change the worker's name. No profanity allowed!",min_length=5,max_length=100),
            "team": fields.String(example="0bed257b-e57c-4327-ac64-40cdfb1ac5e6", description="The team towards which this worker contributes kudos.  It an empty string ('') is passed, it will leave the worker without a team. No profanity allowed!", max_length=36),
        })

        self.response_model_worker_modify = api.model('ModifyWorker', {
            "maintenance": fields.Boolean(description="The new state of the 'maintenance' var for this worker. When True, this worker will not pick up any new requests."),
            "paused": fields.Boolean(description="The new state of the 'paused' var for this worker. When True, this worker will not be given any new requests."),
            "info": fields.String(description="The new state of the 'info' var for this worker."),
            "name": fields.String(description="The new name for this this worker."),
            "team": fields.String(example="Direct Action", description="The new team of this worker."),
        })

        self.response_model_user_kudos_details = api.model('UserKudosDetails', {
            "accumulated": fields.Float(default=0,description="The ammount of Kudos accumulated or used for generating images."),
            "gifted": fields.Float(default=0,description="The amount of Kudos this user has given to other users."),
            "admin": fields.Float(default=0,description="The amount of Kudos this user has been given by the Horde admins."),
            "received": fields.Float(default=0,description="The amount of Kudos this user has been given by other users."),
            "recurring": fields.Float(default=0,description="The amount of Kudos this user has received from recurring rewards."),
            "awarded": fields.Float(default=0,description="The amount of Kudos this user has been awarded from things like rating images."),
        })

        self.response_model_contrib_details = api.model('ContributionsDetails', {
            "fulfillments": fields.Integer(description="How many images this user has generated")
        })
        self.response_model_use_details = api.model('UsageDetails', {
            "requests": fields.Integer(description="How many images this user has requested")
        })

        self.response_model_monthly_kudos = api.model('MonthlyKudos', {
            "amount": fields.Integer(description="How much recurring Kudos this user receives monthly."),
            "last_received": fields.DateTime(dt_format='rfc822',description="Last date this user received monthly Kudos."),
        })

        self.response_model_user_details = api.model('UserDetails', {
            "username": fields.String(description="The user's unique Username. It is a combination of their chosen alias plus their ID."),
            "id": fields.Integer(description="The user unique ID. It is always an integer."),
            "kudos": fields.Float(description="The amount of Kudos this user has. The amount of Kudos determines the priority when requesting image generations."),
            "evaluating_kudos": fields.Float(description="(Privileged) The amount of Evaluating Kudos this untrusted user has from generations and uptime. When this number reaches a prespecified threshold, they automatically become trusted."),
            "concurrency": fields.Integer(description="How many concurrent generations this user may request."),    
            "worker_invited": fields.Integer(description="Whether this user has been invited to join a worker to the horde and how many of them. When 0, this user cannot add (new) workers to the horde."),
            "moderator": fields.Boolean(example=False,description="This user is a Horde moderator."),
            "kudos_details": fields.Nested(self.response_model_user_kudos_details),
            "worker_count": fields.Integer(description="How many workers this user has created (active or inactive)"),
            "worker_ids": fields.List(fields.String(description="Privileged or public when the user has explicitly allows it to be public.")),
            "monthly_kudos": fields.Nested(self.response_model_monthly_kudos, skip_none=True),
            "trusted": fields.Boolean(example=False,description="This user is a trusted member of the Horde."),
            "suspicious": fields.Integer(example=0,description="(Privileged) How much suspicion this user has accumulated"),
            "pseudonymous": fields.Boolean(example=False,description="If true, this user has not registered using an oauth service."),
            "contact": fields.String(example="email@example.com", description="(Privileged) Contact details for the horde admins to reach the user in case of emergency."),
            "account_age": fields.Integer(example=60, description="How many seconds since this account was created"),
            # I need to pass these two via inheritabce, or they take over
            # "usage": fields.Nested(self.response_model_use_details),
            # "contributions": fields.Nested(self.response_model_contrib_details),
        })

        self.input_model_user_details = api.model('ModifyUserInput', {
            "kudos": fields.Float(description="The amount of kudos to modify (can be negative)"),
            "concurrency": fields.Integer(description="The amount of concurrent request this user can have",min=0, max=100),
            "usage_multiplier": fields.Float(description="The amount by which to multiply the users kudos consumption",min=0.1, max=10),    
            "worker_invited": fields.Integer(description="Set to the amount of workers this user is allowed to join to the horde when in worker invite-only mode."),
            "moderator": fields.Boolean(example=False,description="Set to true to Make this user a horde moderator"),
            "public_workers": fields.Boolean(example=False,description="Set to true to Make this user a display their worker IDs"),
            "monthly_kudos": fields.Integer(description="When specified, will start assigning the user monthly kudos, starting now!"),
            "username": fields.String(description="When specified, will change the username. No profanity allowed!",min_length=3,max_length=100),
            "trusted": fields.Boolean(example=False,description="When set to true,the user and their servers will not be affected by suspicion"),
            "reset_suspicion": fields.Boolean(description="Set the user's suspicion back to 0"),
            "contact": fields.String(example="email@example.com", description="Contact details for the horde admins to reach the user in case of emergency. This is only visible to horde moderators.",min_length=5,max_length=500),
        })

        self.response_model_user_modify = api.model('ModifyUser', {
            "new_kudos": fields.Float(description="The new total Kudos this user has after this request"),
            "concurrency": fields.Integer(example=30,description="The request concurrency this user has after this request"),
            "usage_multiplier": fields.Float(example=1.0,description="Multiplies the amount of kudos lost when generating images."),
            "worker_invited": fields.Integer(example=1,description="Whether this user has been invited to join a worker to the horde and how many of them. When 0, this user cannot add (new) workers to the horde."),
            "moderator": fields.Boolean(example=False,description="The user's new moderator status."),
            "public_workers": fields.Boolean(example=False,description="The user's new public_workers status."),
            "username": fields.String(example='username#1',description="The user's new username."),
            "monthly_kudos": fields.Integer(example=0,description="The user's new monthly kudos total"),
            "trusted": fields.Boolean(description="The user's new trusted status"),
            "new_suspicion": fields.Integer(description="The user's new suspiciousness rating"),
            "contact": fields.String(example="email@example.com", description="The new contact details"),
        })

        self.response_model_horde_performance = api.model('HordePerformance', {
            "queued_requests": fields.Integer(description="The amount of waiting and processing requests currently in this Horde"),
            "worker_count": fields.Integer(description="How many workers are actively processing prompt generations in this Horde in the past 5 minutes"),
            "thread_count": fields.Integer(description="How many worker threads are actively processing prompt generations in this Horde in the past 5 minutes"),
        })

        self.response_model_newspiece = api.model('Newspiece', {
            'date_published': fields.String(description="The date this newspiece was published"),
            'newspiece': fields.String(description="The actual piece of news"),
            'importance': fields.String(example='Information',description="How critical this piece of news is."),
        })

        self.response_model_horde_modes = api.model('HordeModes', {
            "maintenance_mode": fields.Boolean(description="When True, this Horde will not accept new requests for image generation, but will finish processing the ones currently in the queue."),
            "invite_only_mode": fields.Boolean(description="When True, this Horde will not only accept worker explicitly invited to join."),
            "raid_mode": fields.Boolean(description="When True, this Horde will not always provide full information in order to throw off attackers."),
        })

        self.response_model_error = api.model('RequestError', {
            'message': fields.String(description="The error message for this status code."),
        })
        self.response_model_active_model = api.inherit('ActiveModel', self.response_model_active_model_lite, {
            'performance': fields.Float(description="The average speed of generation for this model"),
            'queued': fields.Float(description="The amount waiting to be generated by this model"),
            'eta': fields.Integer(description="Estimated time in seconds for this model's queue to be cleared"),
        })
        self.response_model_deleted_worker = api.model('DeletedWorker', {
            'deleted_id': fields.String(description="The ID of the deleted worker"),
            'deleted_name': fields.String(description="The Name of the deleted worker"),
        })
        self.response_model_team_details = api.inherit('TeamDetails', self.response_model_team_details_lite, {
            "info": fields.String(description="Extra information or comments about this team provided by its owner.", example="Anarchy is emergent order.", default=None),
            "requests_fulfilled": fields.Integer(description="How many images this team's workers have generated."),
            "kudos": fields.Float(description="How many Kudos the workers in this team have been rewarded while part of this team."),
            "uptime": fields.Integer(description="The total amount of time workers have stayed online while on this team"),
            "creator": fields.String(example="db0#1", description="The alias of the user which created this team."),
            "worker_count": fields.Integer(example=10,description="How many workers have been dedicated to this team"),
            'workers': fields.List(fields.Nested(self.response_model_worker_details_lite)),
            'models': fields.List(fields.Nested(self.response_model_active_model_lite)),
        })
        self.input_model_team_modify = api.model('ModifyTeamInput', {
            "name": fields.String(description="The name of the team. No profanity allowed!",min_length=3, max_length=100),
            "info": fields.String(description="Extra information or comments about this team.", example="Anarchy is emergent order.", default=None, min_length=3,max_length=1000),
        })
        self.input_model_team_create = api.model('CreateTeamInput', {
            "name": fields.String(required=True, description="The name of the team. No profanity allowed!",min_length=3, max_length=100),
            "info": fields.String(description="Extra information or comments about this team.", example="Anarchy is emergent order.", default=None, min_length=3,max_length=1000),
        })
        self.response_model_deleted_team = api.model('DeletedTeam', {
            'deleted_id': fields.String(description="The ID of the deleted team"),
            'deleted_name': fields.String(description="The Name of the deleted team"),
        })
        self.response_model_team_modify = api.model('ModifyTeam', {
            'id': fields.String(description="The ID of the team"),
            'name': fields.String(description="The Name of the team"),
            'info': fields.String(description="The Info of the team"),
        })
        self.input_model_delete_ip_timeout = api.model('DeleteTimeoutIPInput', {
            "ipaddr": fields.String(example="127.0.0.1",required=True, description="The IP address to remove from timeout",min_length=7, max_length=15),
        })
        self.response_model_simple_response = api.model('SimpleResponse', {
            "message": fields.String(default='OK',required=True, description="The result of this operation"),
        })

        self.input_model_filter_put = api.model('PutNewFilter', {
            "regex": fields.String(required=True, description="The regex for this filter.", example="ac.*"),
            "filter_type": fields.Integer(required=True, description="The integer defining this filter type", min=10, max=29, example=10),
            "description": fields.String(required=False, description="Description about this regex"),
        })
        self.input_model_filter_patch = api.model('PatchExistingFilter', {
            "regex": fields.String(required=False, description="The regex for this filter.", example="ac.*"),
            "filter_type": fields.Integer(required=False, description="The integer defining this filter type", min=10, max=29, example=10),
            "description": fields.String(required=False, description="Description about this regex"),
        })

        self.response_model_filter_details = api.model('FilterDetails', {
            "id": fields.String(required=True,description="The UUID of this filter."),
            "regex": fields.String(required=True,description="The regex for this filter.", example="ac.*"),
            "filter_type": fields.Integer(required=True,description="The integer defining this filter type", min=10, max=29, example=10),
            "description": fields.String(required=False, description="Description about this regex"),
            "user": fields.String(required=True, description="The moderator which added or last updated this regex"),
        })
        self.response_model_prompt_suspicion = api.model('FilterPromptSuspicion', {
            "suspicion": fields.String(default=0, required=True, description="Rates how suspicious the provided prompt is. A suspicion over 2 means it would be blocked."),
            "matches": fields.List(fields.String(required=True, description="Which words in the prompt matched the filters")),
        })
        self.response_model_filter_regex = api.model('FilterRegex', {
            "filter_type": fields.Integer(required=True,description="The integer defining this filter type", min=10, max=29, example=10),
            "regex": fields.String(required=True,description="The full regex for this filter type."),
        })
