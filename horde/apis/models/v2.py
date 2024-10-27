# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from flask_restx import fields, reqparse

from horde.enums import WarningMessage
from horde.exceptions import KNOWN_RC
from horde.vars import horde_noun, horde_title


class Parsers:
    def __init__(self):
        self.generate_parser = reqparse.RequestParser()
        self.generate_parser.add_argument(
            "apikey",
            type=str,
            required=True,
            help="The API Key corresponding to a registered user.",
            location="headers",
        )
        self.generate_parser.add_argument(
            "Client-Agent",
            default="unknown:0:unknown",
            type=str,
            required=False,
            help="The client name and version",
            location="headers",
        )
        self.generate_parser.add_argument(
            "prompt",
            type=str,
            required=True,
            help="The prompt to generate from.",
            location="json",
        )
        self.generate_parser.add_argument(
            "params",
            type=dict,
            required=False,
            help="Extra generate params to send to the worker.",
            location="json",
        )
        self.generate_parser.add_argument(
            "extra_source_images",
            type=list,
            required=False,
            help="Extra images to send to the worker to processing",
            location="json",
        )
        self.generate_parser.add_argument(
            "trusted_workers",
            type=bool,
            required=False,
            default=False,
            help=f"When true, only {horde_title} trusted workers will serve this request. "
            "When False, Evaluating workers will also be used.",
            location="json",
        )
        self.generate_parser.add_argument(
            "validated_backends",
            type=bool,
            required=False,
            default=False,
            help=f"When true, only inference backends that are validated by the {horde_title} devs will serve this request. "
            "When False, non-validated backends will also be used which can increase speed but "
            "you may end up with unexpected results.",
            location="json",
        )
        self.generate_parser.add_argument(
            "workers",
            type=list,
            required=False,
            help="If specified, only the worker with this ID will be able to generate this prompt.",
            location="json",
        )
        self.generate_parser.add_argument(
            "worker_blacklist",
            type=bool,
            required=False,
            default=False,
            help="If true, the worker list will be treated as a blacklist instead of a whitelist.",
            location="json",
        )
        self.generate_parser.add_argument(
            "nsfw",
            type=bool,
            default=True,
            required=False,
            help=(
                "Marks that this request expects or allows NSFW content. "
                "Only workers with the nsfw flag active will pick this request up."
            ),
            location="json",
        )
        self.generate_parser.add_argument(
            "slow_workers",
            type=bool,
            default=True,
            required=False,
            help="When True, allows slower workers to pick up this request. Disabling this incurs an extra kudos cost.",
            location="json",
        )
        self.generate_parser.add_argument(
            "extra_slow_workers",
            type=bool,
            default=False,
            required=False,
            help="When True, allows very slower workers to pick up this request. Use this when you don't mind waiting a lot.",
            location="json",
        )
        self.generate_parser.add_argument(
            "dry_run",
            type=bool,
            default=False,
            required=False,
            help="When true, the endpoint will simply return the cost of the request in kudos and exit.",
            location="json",
        )
        self.generate_parser.add_argument(
            "proxied_account",
            type=str,
            required=False,
            help=(
                "If using a service account as a proxy, provide this value to identify "
                "the actual account from which this request is coming from."
            ),
            location="json",
        )
        self.generate_parser.add_argument(
            "disable_batching",
            type=bool,
            default=False,
            required=False,
            location="json",
        )
        self.generate_parser.add_argument(
            "allow_downgrade",
            type=bool,
            default=False,
            required=False,
            location="json",
        )
        self.generate_parser.add_argument("webhook", type=str, required=False, location="json")
        self.generate_parser.add_argument("style", type=str, required=False, location="json")

        # The parser for RequestPop
        self.job_pop_parser = reqparse.RequestParser()
        self.job_pop_parser.add_argument(
            "apikey",
            type=str,
            required=True,
            help="The API Key corresponding to a registered user.",
            location="headers",
        )
        self.job_pop_parser.add_argument(
            "name",
            type=str,
            required=True,
            help="The worker's unique name, to track contributions.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "priority_usernames",
            type=list,
            required=False,
            help="The usernames which get priority use on this worker.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "nsfw",
            type=bool,
            default=True,
            required=False,
            help="Marks that this worker is capable of generating NSFW content.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "models",
            type=list,
            required=False,
            help="The models currently available on this worker.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "bridge_agent",
            type=str,
            required=False,
            default="unknown:0:unknown",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "threads",
            type=int,
            required=False,
            default=1,
            help="How many threads this worker is running. This is used to accurately the current power available in the horde.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "require_upfront_kudos",
            type=bool,
            required=False,
            default=False,
            help="If True, this worker will only pick up requests where the owner has the required kudos to consume already available.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "amount",
            type=int,
            required=False,
            default=1,
            help="How many jobvs to pop at the same time",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "extra_slow_worker",
            type=bool,
            default=False,
            required=False,
            location="json",
        )

        self.job_submit_parser = reqparse.RequestParser()
        self.job_submit_parser.add_argument(
            "apikey",
            type=str,
            required=True,
            help="The worker's owner API key.",
            location="headers",
        )
        self.job_submit_parser.add_argument(
            "id",
            type=str,
            required=True,
            help="The processing generation uuid.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "generation",
            type=str,
            required=True,
            help="The generated output.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "state",
            type=str,
            required=False,
            default="ok",
            help="The state of this returned generation.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "gen_metadata",
            type=list,
            required=False,
            help="Metadata about this job such as defaulted components due to failures.",
            location="json",
        )

        # Style Parsers
        self.style_parser = reqparse.RequestParser()
        self.style_parser.add_argument(
            "apikey",
            type=str,
            required=True,
            help="The API Key corresponding to a registered user.",
            location="headers",
        )
        self.style_parser.add_argument(
            "Client-Agent",
            default="unknown:0:unknown",
            type=str,
            required=False,
            help="The client name and version",
            location="headers",
        )
        self.style_parser.add_argument(
            "name",
            type=str,
            required=True,
            help="The name of the style.",
            location="json",
        )
        self.style_parser.add_argument(
            "info",
            type=str,
            required=False,
            help="Extra information about this style.",
            location="json",
        )
        self.style_parser.add_argument(
            "prompt",
            type=str,
            required=False,
            default="{p}{np}",
            help="The prompt to generate from.",
            location="json",
        )
        self.style_parser.add_argument(
            "params",
            type=dict,
            required=False,
            help="Extra generate params to send to the worker.",
            location="json",
        )
        self.style_parser.add_argument(
            "public",
            type=bool,
            default=True,
            required=False,
            location="json",
        )
        self.style_parser.add_argument(
            "nsfw",
            type=bool,
            default=False,
            required=False,
            location="json",
        )
        self.style_parser.add_argument(
            "tags",
            type=list,
            required=False,
            help="Tags describing this style. Can be used for style discovery.",
            location="json",
        )
        self.style_parser.add_argument(
            "models",
            type=list,
            required=False,
            help="Tags describing this style. Can be used for style discovery.",
            location="json",
        )
        self.style_parser_patch = reqparse.RequestParser()
        self.style_parser_patch.add_argument(
            "apikey",
            type=str,
            required=True,
            help="The API Key corresponding to a registered user.",
            location="headers",
        )
        self.style_parser_patch.add_argument(
            "Client-Agent",
            default="unknown:0:unknown",
            type=str,
            required=False,
            help="The client name and version",
            location="headers",
        )
        self.style_parser_patch.add_argument(
            "name",
            type=str,
            required=False,
            help="The name of the style.",
            location="json",
        )
        self.style_parser_patch.add_argument(
            "info",
            type=str,
            required=False,
            help="Extra information about this style.",
            location="json",
        )
        self.style_parser_patch.add_argument(
            "prompt",
            type=str,
            required=False,
            help="The prompt to generate from.",
            location="json",
        )
        self.style_parser_patch.add_argument(
            "params",
            type=dict,
            required=False,
            help="Extra generate params to send to the worker.",
            location="json",
        )
        self.style_parser_patch.add_argument(
            "public",
            type=bool,
            default=True,
            required=False,
            location="json",
        )
        self.style_parser_patch.add_argument(
            "nsfw",
            type=bool,
            default=False,
            required=False,
            location="json",
        )
        self.style_parser_patch.add_argument(
            "tags",
            type=list,
            required=False,
            help="Tags describing this style. Can be used for style discovery.",
            location="json",
        )
        self.style_parser_patch.add_argument(
            "models",
            type=list,
            required=False,
            help="Tags describing this style. Can be used for style discovery.",
            location="json",
        )


class Models:
    def __init__(self, api):
        self.response_model_wp_status_lite = api.model(
            "RequestStatusCheck",
            {
                "finished": fields.Integer(description="The amount of finished jobs in this request."),
                "processing": fields.Integer(description="The amount of still processing jobs in this request."),
                "restarted": fields.Integer(
                    description="The amount of jobs that timed out and had to be restarted or were reported as failed by a worker.",
                ),
                "waiting": fields.Integer(description="The amount of jobs waiting to be picked up by a worker."),
                "done": fields.Boolean(description="True when all jobs in this request are done. Else False."),
                "faulted": fields.Boolean(
                    default=False,
                    description="True when this request caused an internal server error and could not be completed.",
                ),
                "wait_time": fields.Integer(
                    description="The expected amount to wait (in seconds) to generate all jobs in this request.",
                ),
                "queue_position": fields.Integer(
                    description="The position in the requests queue. This position is determined by relative Kudos amounts.",
                ),
                "kudos": fields.Float(description="The amount of total Kudos this request has consumed until now."),
                "is_possible": fields.Boolean(
                    default=True,
                    description="If False, this request will not be able to be completed with the pool of workers currently available.",
                ),
            },
        )
        self.response_model_worker_details_lite = api.model(
            "WorkerDetailsLite",
            {
                "type": fields.String(
                    example="image",
                    description="The Type of worker this is.",
                    enum=["image", "text", "interrogation"],
                ),
                "name": fields.String(description="The Name given to this worker."),
                "id": fields.String(description="The UUID of this worker."),
                "online": fields.Boolean(description="True if the worker has checked-in the past 5 minutes."),
            },
        )
        self.response_model_team_details_lite = api.model(
            "TeamDetailsLite",
            {
                "name": fields.String(description="The Name given to this team."),
                "id": fields.String(description="The UUID of this team."),
            },
        )
        self.response_model_active_model_lite = api.model(
            "ActiveModelLite",
            {
                "name": fields.String(description="The Name of a model available by workers in this horde."),
                "count": fields.Integer(description="How many of workers in this horde are running this model."),
            },
        )
        self.response_model_generation_result = api.model(
            "Generation",
            {
                "worker_id": fields.String(
                    title="Worker ID",
                    description="The UUID of the worker which generated this image.",
                ),
                "worker_name": fields.String(
                    title="Worker Name",
                    description="The name of the worker which generated this image.",
                ),
                "model": fields.String(
                    title="Generation Model",
                    description="The model which generated this image.",
                ),
                "state": fields.String(
                    title="Generation State",
                    required=True,
                    default="ok",
                    enum=["ok", "censored"],
                    description="OBSOLETE (Use the gen_metadata field). The state of this generation.",
                ),
            },
        )
        self.response_model_wp_status_full = api.inherit(
            "RequestStatus",
            self.response_model_wp_status_lite,
            {
                "generations": fields.List(fields.Nested(self.response_model_generation_result)),
            },
        )
        self.response_model_warning = api.model(
            "RequestSingleWarning",
            {
                "code": fields.String(description="A unique identifier for this warning.", enum=[i.name for i in WarningMessage]),
                "message": fields.String(
                    description="Something that you should be aware about this request, in plain text.",
                    min_length=1,
                ),
            },
        )
        self.response_model_async = api.model(
            "RequestAsync",
            {
                "id": fields.String(
                    description="The UUID of the request. Use this to retrieve the request status in the future.",
                ),
                "kudos": fields.Float(description="The expected kudos consumption for this request."),
                "message": fields.String(
                    default=None,
                    description="Any extra information from the horde about this request.",
                ),
                "warnings": fields.List(fields.Nested(self.response_model_warning)),
            },
        )
        self.response_model_generation_payload = api.model(
            "ModelPayload",
            {
                "prompt": fields.String(
                    description="The prompt which will be sent to the horde against which to run inference.",
                ),
                "n": fields.Integer(example=1, description="The amount of images to generate."),
                "seed": fields.String(description="The seed to use to generete this request."),
            },
        )
        self.response_model_generations_skipped = api.model(
            "NoValidRequestFound",
            {
                "worker_id": fields.Integer(
                    description="How many waiting requests were skipped because they demanded a specific worker.",
                    min=0,
                ),
                "performance": fields.Integer(
                    description="How many waiting requests were skipped because they required higher performance.",
                    min=0,
                ),
                "nsfw": fields.Integer(
                    description=(
                        "How many waiting requests were skipped because "
                        "they demanded a nsfw generation which this worker does not provide."
                    ),
                    min=0,
                ),
                "blacklist": fields.Integer(
                    description=(
                        "How many waiting requests were skipped because "
                        "they demanded a generation with a word that this worker does not accept."
                    ),
                    min=0,
                ),
                "untrusted": fields.Integer(
                    description=("How many waiting requests were skipped because they demanded a trusted worker which this worker is not."),
                    min=0,
                ),
                "models": fields.Integer(
                    example=0,
                    description=(
                        "How many waiting requests were skipped because they demanded a different model than what this worker provides."
                    ),
                    min=0,
                ),
                "bridge_version": fields.Integer(
                    example=0,
                    description=(
                        "How many waiting requests were skipped because they require a higher version of the bridge "
                        "than this worker is running (upgrade if you see this in your skipped list)."
                    ),
                    min=0,
                ),
                "kudos": fields.Integer(
                    description=(
                        "How many waiting requests were skipped because the user "
                        "didn't have enough kudos when this worker requires upfront kudos."
                    ),
                ),
            },
        )

        self.response_model_job_pop = api.model(
            "GenerationPayload",
            {
                "payload": fields.Nested(self.response_model_generation_payload, skip_none=True),
                "id": fields.String(description="The UUID for this generation."),
                "ttl": fields.Integer(description="The amount of seconds before this job is considered stale and aborted."),
                "skipped": fields.Nested(self.response_model_generations_skipped, skip_none=True),
            },
        )
        self.input_model_job_submit = api.model(
            "SubmitInput",
            {
                "id": fields.String(
                    required=True,
                    description="The UUID of this generation.",
                    example="00000000-0000-0000-0000-000000000000",
                ),
                "generation": fields.String(
                    example="R2",
                    required=False,
                    description="R2 result was uploaded to R2, else the string of the result.",
                ),
                "state": fields.String(
                    title="Generation State",
                    required=False,
                    default="ok",
                    enum=["ok", "censored", "faulted", "csam"],
                    description="The state of this generation.",
                ),
            },
        )
        self.response_model_job_submit = api.model(
            "GenerationSubmitted",
            {
                "reward": fields.Float(
                    example=10.0,
                    description="The amount of kudos gained for submitting this request.",
                ),
            },
        )

        self.response_model_kudos_transfer = api.model(
            "KudosTransferred",
            {
                "transferred": fields.Float(example=100, description="The amount of Kudos tranferred."),
            },
        )
        self.response_model_kudos_award = api.model(
            "KudosAwarded",
            {
                "awarded": fields.Float(example=100, description="The amount of Kudos awarded."),
            },
        )

        self.response_model_admin_maintenance = api.model(
            "MaintenanceModeSet",
            {
                "maintenance_mode": fields.Boolean(example=True, description="The current state of maintenance_mode."),
            },
        )

        self.response_model_worker_kudos_details = api.model(
            "WorkerKudosDetails",
            {
                "generated": fields.Float(
                    description="How much Kudos this worker has received for generating images.",
                ),
                "uptime": fields.Integer(
                    description="How much Kudos this worker has received for staying online longer.",
                ),
            },
        )
        self.input_model_job_pop = api.model(
            "PopInput",
            {
                "name": fields.String(description="The Name of the Worker."),
                "priority_usernames": fields.List(
                    fields.String(description="Users with priority to use this worker."),
                ),
                "nsfw": fields.Boolean(
                    default=False,
                    description="Whether this worker can generate NSFW requests or not.",
                ),
                "models": fields.List(
                    fields.String(
                        description="Which models this worker is serving.",
                        min_length=3,
                        max_length=255,
                    ),
                ),
                "bridge_agent": fields.String(
                    required=False,
                    default="unknown:0:unknown",
                    example=f"{horde_title} Worker reGen:4.1.0:https://github.com/Haidra-Org/horde-worker-reGen",
                    description="The worker name, version and website.",
                    max_length=1000,
                ),
                "threads": fields.Integer(
                    default=1,
                    description=(
                        "How many threads this worker is running. This is used to accurately the current power available in the horde."
                    ),
                    min=1,
                    max=50,
                ),
                "require_upfront_kudos": fields.Boolean(
                    example=False,
                    default=False,
                    description=(
                        "If True, this worker will only pick up requests where the owner "
                        "has the required kudos to consume already available."
                    ),
                ),
                "amount": fields.Integer(
                    default=1,
                    required=False,
                    description="How many jobvs to pop at the same time",
                    min=1,
                    max=20,
                ),
                "extra_slow_worker": fields.Boolean(
                    default=True,
                    description=(
                        "If True, marks the worker as very slow. You should only use this if your mps/s is lower than 0.1."
                        "Extra slow workers are excluded from normal requests but users can opt in to use them."
                    ),
                ),
            },
        )
        self.response_model_worker_details = api.inherit(
            "WorkerDetails",
            self.response_model_worker_details_lite,
            {
                "requests_fulfilled": fields.Integer(description="How many images this worker has generated."),
                "kudos_rewards": fields.Float(description="How many Kudos this worker has been rewarded in total."),
                "kudos_details": fields.Nested(self.response_model_worker_kudos_details),
                "performance": fields.String(
                    description="The average performance of this worker in human readable form.",
                ),
                "threads": fields.Integer(description="How many threads this worker is running."),
                "uptime": fields.Integer(
                    description=f"The amount of seconds this worker has been online for this {horde_title}.",
                ),
                "maintenance_mode": fields.Boolean(
                    example=False,
                    description="When True, this worker will not pick up any new requests.",
                ),
                "paused": fields.Boolean(
                    example=False,
                    description="(Privileged) When True, this worker not be given any new requests.",
                ),
                "info": fields.String(
                    description="Extra information or comments about this worker provided by its owner.",
                    example="https://dbzer0.com",
                    default=None,
                ),
                "nsfw": fields.Boolean(
                    default=False,
                    description="Whether this worker can generate NSFW requests or not.",
                ),
                "owner": fields.String(
                    example="username#1",
                    description="Privileged or public if the owner has allowed it. The alias of the owner of this worker.",
                ),
                "ipaddr": fields.String(
                    example="username#1",
                    description="Privileged. The last known IP this worker has connected from.",
                ),
                "trusted": fields.Boolean(description="The worker is trusted to return valid generations."),
                "flagged": fields.Boolean(
                    description=(
                        "The worker's owner has been flagged for suspicious activity. This worker will not be given any jobs to process."
                    ),
                ),
                "suspicious": fields.Integer(
                    example=0,
                    description="(Privileged) How much suspicion this worker has accumulated.",
                ),
                "uncompleted_jobs": fields.Integer(
                    example=0,
                    description="How many jobs this worker has left uncompleted after it started them.",
                ),
                "models": fields.List(fields.String(description="Which models this worker if offering.")),
                "forms": fields.List(fields.String(description="Which forms this worker if offering.")),
                "team": fields.Nested(
                    self.response_model_team_details_lite,
                    "The Team to which this worker is dedicated.",
                ),
                "contact": fields.String(
                    example="email@example.com",
                    description=("(Privileged) Contact details for the horde admins to reach the owner of this worker in emergencies."),
                    min_length=5,
                    max_length=500,
                ),
                "bridge_agent": fields.String(
                    required=True,
                    default="unknown:0:unknown",
                    example="AI Horde Worker reGen:4.1.0:https://github.com/Haidra-Org/horde-worker-reGen",
                    description="The bridge agent name, version and website.",
                    max_length=1000,
                ),
                "max_pixels": fields.Integer(
                    example=262144,
                    description="The maximum pixels in resolution this worker can generate.",
                ),
                "megapixelsteps_generated": fields.Float(
                    description="How many megapixelsteps this worker has generated until now.",
                ),
                "img2img": fields.Boolean(
                    default=None,
                    description="If True, this worker supports and allows img2img requests.",
                ),
                "painting": fields.Boolean(
                    default=None,
                    description="If True, this worker supports and allows inpainting requests.",
                ),
                "post-processing": fields.Boolean(
                    default=None,
                    description="If True, this worker supports and allows post-processing requests.",
                ),
                "lora": fields.Boolean(
                    default=None,
                    description="If True, this worker supports and allows lora requests.",
                ),
                "controlnet": fields.Boolean(
                    default=None,
                    description="If True, this worker supports and allows controlnet requests.",
                ),
                "sdxl_controlnet": fields.Boolean(
                    default=None,
                    description="If True, this worker supports and allows SDXL controlnet requests.",
                ),
                "max_length": fields.Integer(
                    example=80,
                    description="The maximum tokens this worker can generate.",
                ),
                "max_context_length": fields.Integer(
                    example=80,
                    description="The maximum tokens this worker can read.",
                ),
                "tokens_generated": fields.Float(description="How many tokens this worker has generated until now."),
            },
        )

        self.input_model_worker_modify = api.model(
            "ModifyWorkerInput",
            {
                "maintenance": fields.Boolean(description="Set to true to put this worker into maintenance."),
                "maintenance_msg": fields.String(
                    description=(
                        "if maintenance is True, you can optionally provide a message to be used "
                        "instead of the default maintenance message, so that the owner is informed."
                    ),
                ),
                "paused": fields.Boolean(description="(Mods only) Set to true to pause this worker."),
                "info": fields.String(
                    description=(
                        "You can optionally provide a server note which will be seen in the server details. No profanity allowed!"
                    ),
                    max_length=1000,
                ),
                "name": fields.String(
                    description="When this is set, it will change the worker's name. No profanity allowed!",
                    min_length=5,
                    max_length=100,
                ),
                "team": fields.String(
                    example="0bed257b-e57c-4327-ac64-40cdfb1ac5e6",
                    description=(
                        "The team towards which this worker contributes kudos.  "
                        "It an empty string ('') is passed, it will leave the worker without a team. No profanity allowed!"
                    ),
                    max_length=36,
                ),
            },
        )

        self.response_model_worker_modify = api.model(
            "ModifyWorker",
            {
                "maintenance": fields.Boolean(
                    description=(
                        "The new state of the 'maintenance' var for this worker. "
                        "When True, this worker will not pick up any new requests."
                    ),
                ),
                "paused": fields.Boolean(
                    description=(
                        "The new state of the 'paused' var for this worker. When True, this worker will not be given any new requests."
                    ),
                ),
                "info": fields.String(description="The new state of the 'info' var for this worker."),
                "name": fields.String(description="The new name for this this worker."),
                "team": fields.String(example="Direct Action", description="The new team of this worker."),
            },
        )

        self.response_model_user_kudos_details = api.model(
            "UserKudosDetails",
            {
                "accumulated": fields.Float(
                    default=0,
                    description="The ammount of Kudos accumulated or used for generating images.",
                ),
                "gifted": fields.Float(
                    default=0,
                    description="The amount of Kudos this user has given to other users.",
                ),
                "donated": fields.Float(
                    default=0,
                    description="The amount of Kudos this user has donated to public goods accounts like education.",
                ),
                "admin": fields.Float(
                    default=0,
                    description=f"The amount of Kudos this user has been given by the {horde_title} admins.",
                ),
                "received": fields.Float(
                    default=0,
                    description="The amount of Kudos this user has been given by other users.",
                ),
                "recurring": fields.Float(
                    default=0,
                    description="The amount of Kudos this user has received from recurring rewards.",
                ),
                "awarded": fields.Float(
                    default=0,
                    description="The amount of Kudos this user has been awarded from things like rating images.",
                ),
            },
        )

        self.input_model_sharedkey = api.model(
            "SharedKeyInput",
            {
                "kudos": fields.Integer(
                    min=-1,
                    max=50000000,
                    default=5000,
                    required=False,
                    description=(
                        "The Kudos limit assigned to this key. "
                        "If -1, then anyone with this key can use an unlimited amount of kudos from this account."
                    ),
                ),
                "expiry": fields.Integer(
                    min=-1,
                    default=-1,
                    example=30,
                    required=False,
                    description="The amount of days after which this key will expire. If -1, this key will not expire.",
                ),
                "name": fields.String(
                    min_length=3,
                    max_length=255,
                    required=False,
                    example="Mutual Aid",
                    description="A descriptive name for this key.",
                ),
                "max_image_pixels": fields.Integer(
                    min=-1,
                    max=4194304,
                    default=-1,
                    required=False,
                    description="The maximum amount of image pixels this key can generate per job. -1 means unlimited.",
                ),
                "max_image_steps": fields.Integer(
                    min=-1,
                    max=500,
                    default=-1,
                    required=False,
                    description="The maximum amount of image steps this key can use per job. -1 means unlimited.",
                ),
                "max_text_tokens": fields.Integer(
                    min=-1,
                    max=500,
                    default=-1,
                    required=False,
                    description="The maximum amount of text tokens this key can generate per job. -1 means unlimited.",
                ),
            },
        )

        self.response_model_sharedkey_details = api.model(
            "SharedKeyDetails",
            {
                "id": fields.String(description="The SharedKey ID."),
                "username": fields.String(
                    description="The owning user's unique Username. It is a combination of their chosen alias plus their ID.",
                ),
                "name": fields.String(description="The Shared Key Name."),
                "kudos": fields.Integer(description="The Kudos limit assigned to this key."),
                "expiry": fields.DateTime(
                    dt_format="rfc822",
                    description="The date at which this API key will expire.",
                ),
                "utilized": fields.Integer(
                    description="How much kudos has been utilized via this shared key until now.",
                ),
                "max_image_pixels": fields.Integer(
                    description="The maximum amount of image pixels this key can generate per job. -1 means unlimited.",
                ),
                "max_image_steps": fields.Integer(
                    description="The maximum amount of image steps this key can use per job. -1 means unlimited.",
                ),
                "max_text_tokens": fields.Integer(
                    description="The maximum amount of text tokens this key can generate per job. -1 means unlimited.",
                ),
            },
        )

        # TODO: Obsolete
        self.response_model_contrib_details = api.model(
            "ContributionsDetails",
            {
                "megapixelsteps": fields.Float(description="How many megapixelsteps this user has generated."),
                "fulfillments": fields.Integer(description="How many images this user has generated."),
            },
        )
        # TODO: Obsolete
        self.response_model_use_details = api.model(
            "UsageDetails",
            {
                "megapixelsteps": fields.Float(description="How many megapixelsteps this user has requested."),
                "requests": fields.Integer(description="How many images this user has requested."),
            },
        )

        self.response_model_monthly_kudos = api.model(
            "MonthlyKudos",
            {
                "amount": fields.Integer(description="How much recurring Kudos this user receives monthly."),
                "last_received": fields.DateTime(
                    dt_format="rfc822",
                    description="Last date this user received monthly Kudos.",
                ),
            },
        )

        self.response_model_user_thing_records = api.model(
            "UserThingRecords",
            {
                "megapixelsteps": fields.Float(
                    description="How many megapixelsteps this user has generated or requested.",
                    default=0,
                ),
                "tokens": fields.Integer(
                    description="How many token this user has generated or requested.",
                    default=0,
                ),
            },
        )

        self.response_model_user_amount_records = api.model(
            "UserAmountRecords",
            {
                "image": fields.Integer(
                    description="How many images this user has generated or requested.",
                    default=0,
                ),
                "text": fields.Integer(
                    description="How many texts this user has generated or requested.",
                    default=0,
                ),
                "interrogation": fields.Integer(
                    description="How many texts this user has generated or requested.",
                    default=0,
                ),
            },
        )

        self.response_model_user_records = api.model(
            "UserRecords",
            {
                "usage": fields.Nested(self.response_model_user_thing_records),
                "contribution": fields.Nested(self.response_model_user_thing_records),
                "fulfillment": fields.Nested(self.response_model_user_amount_records),
                "request": fields.Nested(self.response_model_user_amount_records),
            },
        )

        self.response_model_user_active_generations = api.model(
            "UserActiveGenerations",
            {
                "text": fields.List(
                    fields.String(
                        description="(Privileged) The list of active text generation IDs requested by this user.",
                        example="00000000-0000-0000-0000-000000000000",
                    ),
                ),
                "image": fields.List(
                    fields.String(
                        description="(Privileged) The list of active image generation IDs requested by this user.",
                        example="00000000-0000-0000-0000-000000000000",
                    ),
                ),
                "alchemy": fields.List(
                    fields.String(
                        description="(Privileged) The list of active alchemy generation IDs requested by this user.",
                        example="00000000-0000-0000-0000-000000000000",
                    ),
                ),
            },
        )

        self.response_model_user_details = api.model(
            "UserDetails",
            {
                "username": fields.String(
                    description="The user's unique Username. It is a combination of their chosen alias plus their ID.",
                ),
                "id": fields.Integer(description="The user unique ID. It is always an integer."),
                "kudos": fields.Float(
                    description=(
                        "The amount of Kudos this user has. "
                        "The amount of Kudos determines the priority when requesting image generations."
                    ),
                ),
                "evaluating_kudos": fields.Float(
                    description=(
                        "(Privileged) The amount of Evaluating Kudos this untrusted user has from generations and uptime. "
                        "When this number reaches a prespecified threshold, they automatically become trusted."
                    ),
                ),
                "concurrency": fields.Integer(description="How many concurrent generations this user may request."),
                "worker_invited": fields.Integer(
                    description=(
                        f"Whether this user has been invited to join a worker to the {horde_title} and how many of them. "
                        "When 0, this user cannot add (new) workers to the horde."
                    ),
                ),
                "moderator": fields.Boolean(example=False, description=f"This user is a {horde_title} moderator."),
                "kudos_details": fields.Nested(self.response_model_user_kudos_details),
                "worker_count": fields.Integer(
                    description="How many workers this user has created (active or inactive).",
                ),
                "worker_ids": fields.List(
                    fields.String(
                        description="Privileged or public when the user has explicitly allows it to be public.",
                        example="00000000-0000-0000-0000-000000000000",
                    ),
                ),
                "sharedkey_ids": fields.List(
                    fields.String(
                        description="(Privileged) The list of shared key IDs created by this user.",
                        example="00000000-0000-0000-0000-000000000000",
                    ),
                ),
                "active_generations": fields.Nested(self.response_model_user_active_generations, skip_none=True),
                "monthly_kudos": fields.Nested(self.response_model_monthly_kudos, skip_none=True),
                "trusted": fields.Boolean(
                    example=False,
                    description=f"This user is a trusted member of the {horde_title}.",
                ),
                "flagged": fields.Boolean(
                    example=False,
                    description="(Privileged) This user has been flagged for suspicious activity.",
                ),
                "vpn": fields.Boolean(
                    example=False,
                    description="(Privileged) This user has been given the VPN role.",
                ),
                "service": fields.Boolean(
                    example=False,
                    description="This is a service account used by a horde proxy.",
                ),
                "education": fields.Boolean(
                    example=False,
                    description="This is an education account used schools and universities.",
                ),
                "customizer": fields.Boolean(
                    example=False,
                    description=(
                        "When set to true, the user will be able to serve custom Stable Diffusion models "
                        f"which do not exist in the Official {horde_title} Model Reference."
                    ),
                ),
                "special": fields.Boolean(
                    example=False,
                    description="(Privileged) This user has been given the Special role.",
                ),
                "suspicious": fields.Integer(
                    example=0,
                    description="(Privileged) How much suspicion this user has accumulated.",
                ),
                "pseudonymous": fields.Boolean(
                    example=False,
                    description="If true, this user has not registered using an oauth service.",
                ),
                "contact": fields.String(
                    example="email@example.com",
                    description="(Privileged) Contact details for the horde admins to reach the user in case of emergency.",
                ),
                "admin_comment": fields.String(
                    example="User is sus",
                    description="(Privileged) Information about this users by the admins",
                ),
                "account_age": fields.Integer(
                    example=60,
                    description="How many seconds since this account was created.",
                ),
                "usage": fields.Nested(self.response_model_use_details),  # TODO: OBSOLETE
                "contributions": fields.Nested(self.response_model_contrib_details),  # TODO: OBSOLETE
                "records": fields.Nested(self.response_model_user_records),  # TODO: OBSOLETE
            },
        )

        self.input_model_user_details = api.model(
            "ModifyUserInput",
            {
                "kudos": fields.Float(description="The amount of kudos to modify (can be negative)."),
                "concurrency": fields.Integer(
                    description="The amount of concurrent request this user can have.",
                    min=0,
                    max=500,
                ),
                "usage_multiplier": fields.Float(
                    description="The amount by which to multiply the users kudos consumption.",
                    min=0.1,
                    max=10,
                ),
                "worker_invited": fields.Integer(
                    description=("Set to the amount of workers this user is allowed to join to the horde when in worker invite-only mode."),
                ),
                "moderator": fields.Boolean(
                    example=False,
                    description="Set to true to make this user a horde moderator.",
                ),
                "public_workers": fields.Boolean(
                    example=False,
                    description="Set to true to make this user display their worker IDs.",
                ),
                "monthly_kudos": fields.Integer(
                    description="When specified, will start assigning the user monthly kudos, starting now!",
                ),
                "username": fields.String(
                    description="When specified, will change the username. No profanity allowed!",
                    min_length=3,
                    max_length=100,
                ),
                "trusted": fields.Boolean(
                    example=False,
                    description="When set to true,the user and their servers will not be affected by suspicion.",
                ),
                "flagged": fields.Boolean(
                    example=False,
                    description=(
                        "When set to true, the user cannot tranfer kudos and all their workers are put into permanent maintenance."
                    ),
                ),
                "customizer": fields.Boolean(
                    example=False,
                    description=(
                        "When set to true, the user will be able to serve custom Stable Diffusion models "
                        f"which do not exist in the Official {horde_title} Model Reference."
                    ),
                ),
                "vpn": fields.Boolean(
                    example=False,
                    description=(
                        "When set to true, the user will be able to onboard workers behind a VPN. "
                        "This should be used as a temporary solution until the user is trusted."
                    ),
                ),
                "service": fields.Boolean(
                    example=False,
                    description="When set to true, the user is considered a service account proxying the requests for other users.",
                ),
                "education": fields.Boolean(
                    example=False,
                    description="When set to true, the user is considered an education account and some options become more restrictive.",
                ),
                "special": fields.Boolean(
                    example=False,
                    description="When set to true, The user can send special payloads.",
                ),
                "filtered": fields.Boolean(
                    example=False,
                    description="When set to true, the replacement filter will always be applied against this user",
                ),
                "reset_suspicion": fields.Boolean(description="Set the user's suspicion back to 0."),
                "contact": fields.String(
                    example="email@example.com",
                    description=(
                        "Contact details for the horde admins to reach the user in case of emergency. "
                        "This is only visible to horde moderators."
                    ),
                    min_length=5,
                    max_length=500,
                ),
                "admin_comment": fields.String(
                    example="User is sus",
                    description="Add further information about this user for the other admins.",
                    min_length=5,
                    max_length=500,
                ),
            },
        )

        self.response_model_user_modify = api.model(
            "ModifyUser",
            {
                "new_kudos": fields.Float(description="The new total Kudos this user has after this request."),
                "concurrency": fields.Integer(
                    example=30,
                    description="The request concurrency this user has after this request.",
                ),
                "usage_multiplier": fields.Float(
                    example=1.0,
                    description="Multiplies the amount of kudos lost when generating images.",
                ),
                "worker_invited": fields.Integer(
                    example=1,
                    description=(
                        "Whether this user has been invited to join a worker to the horde and how many of them. "
                        "When 0, this user cannot add (new) workers to the horde."
                    ),
                ),
                "moderator": fields.Boolean(example=False, description="The user's new moderator status."),
                "public_workers": fields.Boolean(example=False, description="The user's new public_workers status."),
                "username": fields.String(example="username#1", description="The user's new username."),
                "monthly_kudos": fields.Integer(example=0, description="The user's new monthly kudos total."),
                "trusted": fields.Boolean(description="The user's new trusted status."),
                "flagged": fields.Boolean(description="The user's new flagged status."),
                "customizer": fields.Boolean(description="The user's new customizer status."),
                "vpn": fields.Boolean(description="The user's new vpn status."),
                "service": fields.Boolean(description="The user's new service status."),
                "education": fields.Boolean(description="The user's new education status."),
                "special": fields.Boolean(description="The user's new special status."),
                "new_suspicion": fields.Integer(description="The user's new suspiciousness rating."),
                "contact": fields.String(example="email@example.com", description="The new contact details."),
                "admin_comment": fields.String(
                    example="User is sus",
                    description="The new admin comment.",
                    min_length=5,
                    max_length=500,
                ),
            },
        )

        self.response_model_horde_performance = api.model(
            "HordePerformance",
            {
                "queued_requests": fields.Integer(
                    description=f"The amount of waiting and processing image requests currently in this {horde_noun}.",
                ),
                "queued_text_requests": fields.Integer(
                    description=f"The amount of waiting and processing text requests currently in this {horde_noun}.",
                ),
                "worker_count": fields.Integer(
                    description=f"How many workers are actively processing prompt generations in this {horde_noun} in the past 5 minutes.",
                ),
                "text_worker_count": fields.Integer(
                    description=f"How many workers are actively processing prompt generations in this {horde_noun} in the past 5 minutes.",
                ),
                "thread_count": fields.Integer(
                    description="How many worker threads are actively processing prompt generations "
                    "in this {horde_noun} in the past 5 minutes.",
                ),
                "text_thread_count": fields.Integer(
                    description="How many worker threads are actively processing prompt generations "
                    "in this {horde_noun} in the past 5 minutes.",
                ),
                "queued_megapixelsteps": fields.Float(
                    description=f"The amount of megapixelsteps in waiting and processing requests currently in this {horde_noun}.",
                ),
                "past_minute_megapixelsteps": fields.Float(
                    description=f"How many megapixelsteps this {horde_noun} generated in the last minute.",
                ),
                "queued_forms": fields.Float(
                    description=f"The amount of image interrogations waiting and processing currently in this {horde_noun}.",
                ),
                "interrogator_count": fields.Integer(
                    description="How many workers are actively processing image interrogations "
                    "in this {horde_noun} in the past 5 minutes.",
                ),
                "interrogator_thread_count": fields.Integer(
                    description="How many worker threads are actively processing image interrogation "
                    "in this {horde_noun} in the past 5 minutes.",
                ),
                "queued_tokens": fields.Float(
                    description=f"The amount of tokens in waiting and processing requests currently in this {horde_noun}.",
                ),
                "past_minute_tokens": fields.Float(
                    description=f"How many tokens this {horde_noun} generated in the last minute.",
                ),
            },
        )

        self.response_model_newspiece = api.model(
            "Newspiece",
            {
                "date_published": fields.String(description="The date this newspiece was published."),
                "newspiece": fields.String(description="The actual piece of news."),
                "importance": fields.String(
                    example="Information",
                    description="How critical this piece of news is.",
                ),
                "tags": fields.List(
                    fields.String(description="Tags for this newspiece."),
                ),
                "title": fields.String(description="The title of this newspiece."),
                "more_info_urls": fields.List(
                    fields.String(description="URLs for more information about this newspiece."),
                ),
            },
        )

        self.response_model_horde_modes = api.model(
            "HordeModes",
            {
                "maintenance_mode": fields.Boolean(
                    description=(
                        f"When True, this {horde_noun} will not accept new requests for image generation,"
                        " but will finish processing the ones currently in the queue."
                    ),
                ),
                "invite_only_mode": fields.Boolean(
                    description=f"When True, this {horde_noun} will not only accept worker explicitly invited to join.",
                ),
                "raid_mode": fields.Boolean(
                    description=f"When True, this {horde_noun} will not always provide full information in order to throw off attackers.",
                ),
            },
        )

        self.response_model_error = api.model(
            "RequestError",
            {
                "message": fields.String(description="The error message for this status code."),
                "rc": fields.String(
                    required=True,
                    description="The return code for this error. See: https://github.com/Haidra-Org/AI-Horde/blob/main/README_return_codes.md",
                    enum=KNOWN_RC,
                    example="ExampleHordeError",
                ),
            },
        )
        self.response_model_validation_errors = api.inherit(
            "RequestValidationError",
            self.response_model_error,
            {
                "errors": fields.Wildcard(
                    fields.String(required=True, description="The details of the validation error"),
                ),
            },
        )
        self.response_model_active_model = api.inherit(
            "ActiveModel",
            self.response_model_active_model_lite,
            {
                "performance": fields.Float(description="The average speed of generation for this model."),
                "queued": fields.Float(description="The amount waiting to be generated by this model."),
                "jobs": fields.Float(description="The job count waiting to be generated by this model."),
                "eta": fields.Integer(description="Estimated time in seconds for this model's queue to be cleared."),
                "type": fields.String(
                    example="image",
                    description="The model type (text or image).",
                    enum=["image", "text"],
                ),
            },
        )
        self.response_model_deleted_worker = api.model(
            "DeletedWorker",
            {
                "deleted_id": fields.String(description="The ID of the deleted worker."),
                "deleted_name": fields.String(description="The Name of the deleted worker."),
            },
        )
        self.response_model_team_details = api.inherit(
            "TeamDetails",
            self.response_model_team_details_lite,
            {
                "info": fields.String(
                    description="Extra information or comments about this team provided by its owner.",
                    example="Anarchy is emergent order.",
                    default=None,
                ),
                "requests_fulfilled": fields.Integer(
                    description="How many images this team's workers have generated.",
                ),
                "kudos": fields.Float(
                    description="How many Kudos the workers in this team have been rewarded while part of this team.",
                ),
                "uptime": fields.Integer(
                    description="The total amount of time workers have stayed online while on this team.",
                ),
                "creator": fields.String(
                    example="db0#1",
                    description="The alias of the user which created this team.",
                ),
                "worker_count": fields.Integer(
                    example=10,
                    description="How many workers have been dedicated to this team.",
                ),
                "workers": fields.List(fields.Nested(self.response_model_worker_details_lite)),
                "models": fields.List(fields.Nested(self.response_model_active_model_lite)),
            },
        )
        self.input_model_team_modify = api.model(
            "ModifyTeamInput",
            {
                "name": fields.String(
                    description="The name of the team. No profanity allowed!",
                    min_length=3,
                    max_length=100,
                ),
                "info": fields.String(
                    description="Extra information or comments about this team.",
                    example="Anarchy is emergent order.",
                    default=None,
                    min_length=3,
                    max_length=1000,
                ),
            },
        )
        self.input_model_team_create = api.model(
            "CreateTeamInput",
            {
                "name": fields.String(
                    required=True,
                    description="The name of the team. No profanity allowed!",
                    min_length=3,
                    max_length=100,
                ),
                "info": fields.String(
                    description="Extra information or comments about this team.",
                    example="Anarchy is emergent order.",
                    default=None,
                    min_length=3,
                    max_length=1000,
                ),
            },
        )
        self.response_model_deleted_team = api.model(
            "DeletedTeam",
            {
                "deleted_id": fields.String(description="The ID of the deleted team."),
                "deleted_name": fields.String(description="The Name of the deleted team."),
            },
        )
        self.response_model_team_modify = api.model(
            "ModifyTeam",
            {
                "id": fields.String(description="The ID of the team."),
                "name": fields.String(description="The Name of the team."),
                "info": fields.String(description="The Info of the team."),
            },
        )
        self.input_model_delete_ip_timeout = api.model(
            "DeleteTimeoutIPInput",
            {
                "ipaddr": fields.String(
                    example="127.0.0.1",
                    required=True,
                    description="The IP address or CIDR to remove from timeout.",
                    min_length=7,
                    max_length=40,
                ),
            },
        )
        self.input_model_add_ip_timeout = api.model(
            "AddTimeoutIPInput",
            {
                "ipaddr": fields.String(
                    example="127.0.0.1",
                    required=True,
                    description="The IP address or CIDR to add from timeout.",
                    min_length=7,
                    max_length=40,
                ),
                "hours": fields.Integer(
                    example=24,
                    required=True,
                    description="For how many hours to put this IP in timeout.",
                    min=1,
                    max=24 * 30,
                ),
            },
        )
        self.response_model_ip_timeout = api.model(
            "IPTimeout",
            {
                "ipaddr": fields.String(
                    example="127.0.0.1",
                    required=True,
                    description="The CIDR which is in timeout.",
                    min_length=7,
                    max_length=40,
                ),
                "seconds": fields.Integer(
                    example=24 * 60,
                    required=True,
                    description="How many more seconds this IP block is in timeout ",
                ),
            },
        )
        self.input_model_add_worker_timeout = api.model(
            "AddWorkerTimeout",
            {
                "days": fields.Integer(
                    example=7,
                    required=True,
                    description="For how many days to put this worker's IP in timeout.",
                    min=1,
                    max=30,
                ),
            },
        )
        self.response_model_simple_response = api.model(
            "SimpleResponse",
            {
                "message": fields.String(
                    default="OK",
                    required=True,
                    description="The result of this operation.",
                ),
            },
        )
        self.input_model_filter_put = api.model(
            "PutNewFilter",
            {
                "regex": fields.String(
                    required=True,
                    description="The regex for this filter.",
                    example="ac.*",
                ),
                "filter_type": fields.Integer(
                    required=True,
                    description="The integer defining this filter type.",
                    min=10,
                    max=29,
                    example=10,
                ),
                "description": fields.String(required=False, description="Description about this regex."),
                "replacement": fields.String(
                    required=False,
                    default="",
                    description="The replacement string for this regex.",
                ),
            },
        )
        self.input_model_filter_patch = api.model(
            "PatchExistingFilter",
            {
                "regex": fields.String(
                    required=False,
                    description="The regex for this filter.",
                    example="ac.*",
                ),
                "filter_type": fields.Integer(
                    required=False,
                    description="The integer defining this filter type.",
                    min=10,
                    max=29,
                    example=10,
                ),
                "description": fields.String(required=False, description="Description about this regex."),
                "replacement": fields.String(
                    required=False,
                    default="",
                    description="The replacement string for this regex.",
                ),
            },
        )

        self.response_model_filter_details = api.model(
            "FilterDetails",
            {
                "id": fields.String(required=True, description="The UUID of this filter."),
                "regex": fields.String(
                    required=True,
                    description="The regex for this filter.",
                    example="ac.*",
                ),
                "filter_type": fields.Integer(
                    required=True,
                    description="The integer defining this filter type.",
                    min=10,
                    max=29,
                    example=10,
                ),
                "description": fields.String(required=False, description="Description about this regex."),
                "replacement": fields.String(
                    required=False,
                    default="",
                    description="The replacement string for this regex.",
                ),
                "user": fields.String(
                    required=True,
                    description="The moderator which added or last updated this regex.",
                ),
            },
        )
        self.response_model_prompt_suspicion = api.model(
            "FilterPromptSuspicion",
            {
                "suspicion": fields.String(
                    default=0,
                    required=True,
                    description="Rates how suspicious the provided prompt is. A suspicion over 2 means it would be blocked.",
                ),
                "matches": fields.List(
                    fields.String(
                        required=True,
                        description="Which words in the prompt matched the filters.",
                    ),
                ),
            },
        )
        self.response_model_filter_regex = api.model(
            "FilterRegex",
            {
                "filter_type": fields.Integer(
                    required=True,
                    description="The integer defining this filter type.",
                    min=10,
                    max=29,
                    example=10,
                ),
                "regex": fields.String(required=True, description="The full regex for this filter type."),
            },
        )
        self.model_extra_source_images = api.model(
            "ExtraSourceImage",
            {
                "image": fields.String(description="The Base64-encoded webp to use for further processing.", min_length=1),
                "strength": fields.Float(description="Optional field, determining the strength to use for the processing", default=1.0),
            },
        )
        self.model_extra_texts = api.model(
            "ExtraText",
            {
                "text": fields.String(description="The extra text to send along with this generation.", min_length=1),
                "reference": fields.String(description="The reference which points how and where this text should be used.", min_length=3),
            },
        )
        self.response_model_doc_terms = api.model(
            "HordeDocument",
            {
                "html": fields.String(
                    required=False,
                    description="The document in html format.",
                ),
                "markdown": fields.String(
                    required=False,
                    description="The document in markdown format.",
                ),
            },
        )

        # Styles
        self.response_model_styles_post = api.model(
            "StyleModify",
            {
                "id": fields.String(
                    description="The UUID of the style. Use this to use this style of retrieve its information in the future.",
                ),
                "message": fields.String(
                    default=None,
                    description="Any extra information from the horde about this request.",
                ),
                "warnings": fields.List(fields.Nested(self.response_model_warning)),
            },
        )
