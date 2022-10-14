from flask_restx import fields, reqparse


class Parsers:

    generate_parser = reqparse.RequestParser()
    generate_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    generate_parser.add_argument("prompt", type=str, required=True, help="The prompt to generate from", location="json")
    generate_parser.add_argument("params", type=dict, required=False, help="Extra generate params to send to the worker", location="json")
    generate_parser.add_argument("trusted_workers", type=bool, required=False, default=True, help="When true, only Horde trusted workers will serve this request. When False, Evaluating workers will also be used.", location="json")
    generate_parser.add_argument("workers", type=list, required=False, help="If specified, only the worker with this ID will be able to generate this prompt", location="json")
    generate_parser.add_argument("nsfw", type=bool, default=True, required=False, help="Marks that this request expects or allows NSFW content. Only workers with the nsfw flag active will pick this request up.", location="json")
    generate_parser.add_argument("models", type=list, required=False, help="The acceptable models with which to generate", location="json")

    # The parser for RequestPop
    job_pop_parser = reqparse.RequestParser()
    job_pop_parser.add_argument("apikey", type=str, required=True, help="The API Key corresponding to a registered user", location='headers')
    job_pop_parser.add_argument("name", type=str, required=True, help="The worker's unique name, to track contributions", location="json")
    job_pop_parser.add_argument("priority_usernames", type=list, required=False, help="The usernames which get priority use on this worker", location="json")
    job_pop_parser.add_argument("nsfw", type=bool, default=True, required=False, help="Marks that this worker is capable of generating NSFW content", location="json")
    job_pop_parser.add_argument("blacklist", type=list, required=False, help="Specifies the words that this worker will not accept in a prompt.", location="json")
    job_pop_parser.add_argument("bridge_version", type=int, required=False, default=1, help="Specified the version of the worker bridge, as that can modify the way the arguments are being sent", location="json")
    job_pop_parser.add_argument("models", type=list, required=True, help="The models currently available on this worker", location="json")

    job_submit_parser = reqparse.RequestParser()
    job_submit_parser.add_argument("apikey", type=str, required=True, help="The worker's owner API key", location='headers')
    job_submit_parser.add_argument("id", type=str, required=True, help="The processing generation uuid", location="json")
    job_submit_parser.add_argument("generation", type=str, required=True, help="The generated output", location="json")


class Models:
    def __init__(self,api):

        self.response_model_generation_result = api.model('Generation', {
            'worker_id': fields.String(title="Worker ID", description="The UUID of the worker which generated this image"),
            'worker_name': fields.String(title="Worker Name", description="The name of the worker which generated this image"),
        })
        self.response_model_wp_status_lite = api.model('RequestStatusCheck', {
            'finished': fields.Integer(description="The amount of finished images in this request"),
            'processing': fields.Integer(description="The amount of still processing images in this request"),
            'waiting': fields.Integer(description="The amount of images waiting to be picked up by a worker"),
            'done': fields.Boolean(description="True when all images in this request are done. Else False."),
            'wait_time': fields.Integer(description="The expected amount to wait (in seconds) to generate all images in this request"),
            'queue_position': fields.Integer(description="The position in the requests queue. This position is determined by relative Kudos amounts."),
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
            'worker_id': fields.Integer(description="How many waiting requests were skipped because they demanded a specific worker"),
            'nsfw': fields.Integer(description="How many waiting requests were skipped because they demanded a nsfw generation which this worker does not provide."),
            'blacklist': fields.Integer(description="How many waiting requests were skipped because they demanded a generation with a word that this worker does not accept."),
            'untrusted': fields.Integer(description="How many waiting requests were skipped because they demanded a trusted worker which this worker is not."),
            'models': fields.Integer(example=0,description="How many waiting requests were skipped because they demanded a different model than what this worker provides."),
        })

        self.response_model_job_pop = api.model('GenerationPayload', {
            'payload': fields.Nested(self.response_model_generation_payload, skip_none=True),
            'id': fields.String(description="The UUID for this image generation"),
            'skipped': fields.Nested(self.response_model_generations_skipped, skip_none=True)
        })

        self.response_model_job_submit = api.model('GenerationSubmitted', {
            'reward': fields.Float(example=10.0,description="The amount of kudos gained for submitting this request"),
        })

        self.response_model_kudos_transfer = api.model('KudosTransferred', {
            'transferred': fields.Integer(example=100,description="The amount of Kudos tranferred"),
        })

        self.response_model_admin_maintenance = api.model('MaintenanceModeSet', {
            'maintenance_mode': fields.Boolean(example=True,description="The current state of maintenance_mode"),
        })

        self.response_model_worker_kudos_details = api.model('WorkerKudosDetails', {
            'generated': fields.Float(description="How much Kudos this worker has received for generating images"),
            'uptime': fields.Integer(description="How much Kudos this worker has received for staying online longer"),
        })

        self.response_model_worker_details = api.model('WorkerDetails', {
            "name": fields.String(description="The Name given to this worker."),
            "id": fields.String(description="The UUID of this worker."),
            "requests_fulfilled": fields.Integer(description="How many images this worker has generated."),
            "kudos_rewards": fields.Float(description="How many Kudos this worker has been rewarded in total."),
            "kudos_details": fields.Nested(self.response_model_worker_kudos_details),
            "performance": fields.String(description="The average performance of this worker in human readable form."),
            "uptime": fields.Integer(description="The amount of seconds this worker has been online for this Horde."),
            "maintenance_mode": fields.Boolean(example=False,description="When True, this worker will not pick up any new requests"),
            "paused": fields.Boolean(example=False,description="(Privileged) When True, this worker not be given any new requests."),
            "info": fields.String(description="Extra information or comments about this worker provided by its owner.", example="https://dbzer0.com", default=None),
            "nsfw": fields.Boolean(default=False, description="Whether this worker can generate NSFW requests or not."),
            "owner": fields.String(example="username#1", description="Privileged or public if the owner has allowed it. The alias of the owner of this worker."),
            "trusted": fields.Boolean(description="The worker is trusted to return valid generations."),
            "suspicious": fields.Integer(example=0,description="(Privileged) How much suspicion this worker has accumulated"),
            "suspicious": fields.Integer(example=0,description="(Privileged) How much suspicion this worker has accumulated"),
            'models': fields.List(fields.String(description="Which models this worker if offerring")),
        })

        self.response_model_worker_modify = api.model('ModifyWorker', {
            "maintenance": fields.Boolean(description="The new state of the 'maintenance' var for this worker. When True, this worker will not pick up any new requests."),
            "paused": fields.Boolean(description="The new state of the 'paused' var for this worker. When True, this worker will not be given any new requests."),
            "info": fields.String(description="The new state of the 'info' var for this worker."),
            "name": fields.String(description="The new name for this this worker."),
        })

        self.response_model_user_kudos_details = api.model('UserKudosDetails', {
            "accumulated": fields.Float(default=0,description="The ammount of Kudos accumulated or used for generating images."),
            "gifted": fields.Float(default=0,description="The amount of Kudos this user has given to other users."),
            "admin": fields.Float(default=0,description="The amount of Kudos this user has been given by the Horde admins."),
            "received": fields.Float(default=0,description="The amount of Kudos this user has been given by other users."),
            "recurring": fields.Float(default=0,description="The amount of Kudos this user has received from recurring rewards."),
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
            "evaluating_kudos": fields.Float(description="(Privileged) The amount of Evaluating Kudos this untrusted user has from generations and uptime. When this number reaches 50000, they automatically become trusted."),
            "concurrency": fields.Integer(description="How many concurrent generations this user may request."),    
            "worker_invited": fields.Integer(description="Whether this user has been invited to join a worker to the horde and how many of them. When 0, this user cannot add (new) workers to the horde."),
            "moderator": fields.Boolean(example=False,description="This user is a Horde moderator."),
            "kudos_details": fields.Nested(self.response_model_user_kudos_details),
            "worker_count": fields.Integer(description="How many workers this user has created (active or inactive)"),
            "worker_ids": fields.List(fields.String(description="Privileged or public when the user has explicitly allows it to be public.")),
            "monthly_kudos": fields.Nested(self.response_model_monthly_kudos, skip_none=True),
            "trusted": fields.Boolean(example=False,description="This user is a trusted member of the Horde."),
            "suspicious": fields.Integer(example=0,description="(Privileged) How much suspicion this user has accumulated"),
            # I need to pass these two via inheritabce, or they take over
            # "usage": fields.Nested(self.response_model_use_details),
            # "contributions": fields.Nested(self.response_model_contrib_details),
        })

        self.response_model_user_modify = api.model('ModifyUser', {
            "new_kudos": fields.Float(description="The new total Kudos this user has after this request"),
            "concurrency": fields.Integer(example=30,description="The request concurrency this user has after this request"),
            "usage_multiplier": fields.Float(example=1.0,description="Multiplies the amount of kudos lost when generating images."),
            "worker_invited": fields.Integer(example=1,description="This userWhether this user has been invited to join a worker to the horde and how many of them. When 0, this user cannot add (new) workers to the horde."),
            "moderator": fields.Boolean(example=False,description="The user's new moderator status."),
            "public_workers": fields.Boolean(example=False,description="The user's new public_workers status."),
            "username": fields.String(example='username#1',description="The user's new username."),
            "monthly_kudos": fields.Integer(example=0,description="The user's new monthly kudos total"),
            "trusted": fields.Boolean(description="The user's new trusted status"),
        })

        self.response_model_horde_performance = api.model('HordePerformance', {
            "queued_requests": fields.Integer(description="The amount of waiting and processing requests currently in this Horde"),
            "worker_count": fields.Integer(description="How many workers are actively processing image generations in this Horde in the past 5 minutes"),
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
        self.response_model_model = api.model('Model', {
            'name': fields.String(description="The Name of a model available by workers in this horde."),
            'count': fields.Integer(description="How many of workers in this horde are running this model."),
        })
