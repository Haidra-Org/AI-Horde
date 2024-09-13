# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from flask_restx import fields

from horde.apis.models import v2
from horde.vars import horde_title


class TextParsers(v2.Parsers):
    def __init__(self):
        super().__init__()
        self.generate_parser.add_argument(
            "softprompt",
            type=str,
            required=False,
            help="If specified, only servers who can load this softprompt will generate this request.",
            location="json",
        )
        self.generate_parser.add_argument(
            "models",
            type=list,
            required=False,
            default=[],
            help="The acceptable models with which to generate.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "max_length",
            type=int,
            required=False,
            default=512,
            help="The maximum amount of tokens this worker can generate.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "max_context_length",
            type=int,
            required=False,
            default=2048,
            help="The max amount of context to submit to this AI for sampling.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "softprompts",
            type=list,
            required=False,
            help="The available softprompt files on this worker for the currently running model.",
            location="json",
        )
        # To remove the below once I updated the KAI server to use "models"
        self.job_submit_parser.add_argument(
            "seed",
            type=int,
            required=False,
            default=0,
            help="The seed of the text generation.",
            location="json",
        )


class TextModels(v2.Models):
    def __init__(self, api):
        super().__init__(api)
        self.model_job_metadata = api.model(
            "GenerationMetadataKobold",
            {
                "type": fields.String(
                    required=True,
                    enum=["censorship"],
                    description="The relevance of the metadata field",
                ),
                "value": fields.String(
                    required=True,
                    enum=["csam"],
                    description="The value of the metadata field",
                ),
                "ref": fields.String(
                    required=False,
                    description="Optionally a reference for the metadata (e.g. a lora ID)",
                    max_length=255,
                ),
            },
        )
        self.response_model_generation_result = api.inherit(
            "GenerationKobold",
            self.response_model_generation_result,
            {
                "text": fields.String(
                    title="Generated Text",
                    description="The generated text.",
                    min_length=0,
                ),
                "seed": fields.Integer(
                    title="Generation Seed",
                    description="The seed which generated this text.",
                    default=0,
                ),
                "gen_metadata": fields.List(fields.Nested(self.model_job_metadata, skip_none=True)),
            },
        )
        self.response_model_wp_status_full = api.inherit(
            "RequestStatusKobold",
            self.response_model_wp_status_lite,
            {
                "generations": fields.List(fields.Nested(self.response_model_generation_result)),
            },
        )
        self.root_model_generation_payload_kobold = api.model(
            "ModelPayloadRootKobold",
            {
                "n": fields.Integer(example=1, min=1, max=20),
                "frmtadsnsp": fields.Boolean(
                    example=False,
                    description=(
                        "Input formatting option. When enabled, adds a leading space to your input "
                        "if there is no trailing whitespace at the end of the previous action."
                    ),
                ),
                "frmtrmblln": fields.Boolean(
                    example=False,
                    description=(
                        "Output formatting option. When enabled, replaces all occurrences of two or more consecutive newlines "
                        "in the output with one newline."
                    ),
                ),
                "frmtrmspch": fields.Boolean(
                    example=False,
                    description=r"Output formatting option. When enabled, removes #/@%}{+=~|\^<> from the output.",
                ),
                "frmttriminc": fields.Boolean(
                    example=False,
                    description=(
                        "Output formatting option. When enabled, removes some characters from the end of the output such "
                        "that the output doesn't end in the middle of a sentence. "
                        "If the output is less than one sentence long, does nothing."
                    ),
                ),
                "max_context_length": fields.Integer(
                    min=80,
                    default=1024,
                    max=32000,
                    description="Maximum number of tokens to send to the model.",
                ),
                "max_length": fields.Integer(
                    min=16,
                    max=1024,
                    default=80,
                    description="Number of tokens to generate.",
                ),
                "rep_pen": fields.Float(description="Base repetition penalty value.", min=1, max=3),
                "rep_pen_range": fields.Integer(description="Repetition penalty range.", min=0, max=4096),
                "rep_pen_slope": fields.Float(description="Repetition penalty slope.", min=0, max=10),
                "singleline": fields.Boolean(
                    example=False,
                    description=(
                        "Output formatting option. When enabled, removes everything after the first line of the output, "
                        "including the newline."
                    ),
                ),
                # "soft_prompt": fields.String(
                #     description=(
                #         "Soft prompt to use when generating. If set to the empty string or any other string containing "
                #         "no non-whitespace characters, uses no soft prompt."
                #     )
                # ),
                "temperature": fields.Float(description="Temperature value.", min=0, max=5.0),
                "tfs": fields.Float(description="Tail free sampling value.", min=0.0, max=1.0),
                "top_a": fields.Float(description="Top-a sampling value.", min=0.0, max=1.0),
                "top_k": fields.Integer(description="Top-k sampling value.", min=0, max=100),
                "top_p": fields.Float(description="Top-p sampling value.", min=0.001, max=1.0),
                "typical": fields.Float(description="Typical sampling value.", min=0.0, max=1.0),
                "sampler_order": fields.List(
                    fields.Integer(description="Array of integers representing the sampler order to be used."),
                ),
                "use_default_badwordsids": fields.Boolean(
                    example=True,
                    description="When True, uses the default KoboldAI bad word IDs.",
                ),
                "stop_sequence": fields.List(
                    fields.String(
                        description=(
                            "An array of string sequences whereby the model will stop generating further tokens. "
                            "The returned text WILL contain the stop sequence."
                        ),
                    ),
                ),
                "min_p": fields.Float(description="Min-p sampling value.", min=0.0, default=0.0, max=1.0),
                "smoothing_factor": fields.Float(description="Quadratic sampling value.", min=0.0, default=0.0, max=10.0),
                "dynatemp_range": fields.Float(
                    description="Dynamic temperature range value.",
                    min=0.0,
                    default=0.0,
                    max=5.0,
                ),
                "dynatemp_exponent": fields.Float(
                    description="Dynamic temperature exponent value.",
                    min=0.0,
                    default=1.0,
                    max=5.0,
                ),
            },
        )
        self.response_model_generation_payload = api.inherit(
            "ModelPayloadKobold",
            self.root_model_generation_payload_kobold,
            {
                "prompt": fields.String(description="The prompt which will be sent to KoboldAI to generate the text."),
            },
        )
        self.input_model_generation_payload = api.inherit(
            "ModelGenerationInputKobold",
            self.root_model_generation_payload_kobold,
            {},
        )
        self.response_model_generations_skipped = api.inherit(
            "NoValidRequestFoundKobold",
            self.response_model_generations_skipped,
            {
                "max_context_length": fields.Integer(
                    example=0,
                    description=(
                        "How many waiting requests were skipped because they demanded a higher max_context_length "
                        "than what this worker provides."
                    ),
                ),
                "max_length": fields.Integer(
                    example=0,
                    description=(
                        "How many waiting requests were skipped because they demanded "
                        "more generated tokens that what this worker can provide."
                    ),
                ),
                "matching_softprompt": fields.Integer(
                    example=0,
                    description=(
                        "How many waiting requests were skipped because they demanded an available soft-prompt "
                        "which this worker does not have."
                    ),
                ),
            },
        )
        self.response_model_job_pop = api.model(
            "GenerationPayloadKobold",
            {
                "payload": fields.Nested(self.response_model_generation_payload, skip_none=True),
                "id": fields.String(description="The UUID for this text generation."),
                "ids": fields.List(
                    fields.String(
                        description="The UUID for this text generation.",
                        example="00000000-0000-0000-0000-000000000000",
                    ),
                ),
                "extra_source_images": fields.List(fields.Nested(self.model_extra_source_images)),
                "skipped": fields.Nested(self.response_model_generations_skipped, skip_none=True),
                "softprompt": fields.String(description="The soft prompt requested for this generation."),
                "model": fields.String(description="Which of the available models to use for this request."),
            },
        )
        self.input_model_job_pop = api.inherit(
            "PopInputKobold",
            self.input_model_job_pop,
            {
                "max_length": fields.Integer(
                    default=512,
                    description="The maximum amount of tokens this worker can generate.",
                ),
                "max_context_length": fields.Integer(
                    default=2048,
                    description="The max amount of context to submit to this AI for sampling.",
                ),
                "softprompts": fields.List(
                    fields.String(
                        description="The available softprompt files on this worker for the currently running model.",
                    ),
                ),
            },
        )
        self.input_model_request_generation = api.model(
            "GenerationInputKobold",
            {
                "prompt": fields.String(description="The prompt which will be sent to KoboldAI to generate text."),
                "params": fields.Nested(self.input_model_generation_payload, skip_none=True),
                "softprompt": fields.String(
                    description="Specify which softpompt needs to be used to service this request.",
                    required=False,
                    min_length=1,
                ),
                "trusted_workers": fields.Boolean(
                    default=False,
                    description=(
                        "When true, only trusted workers will serve this request. "
                        "When False, Evaluating workers will also be used which can increase speed but adds more risk!"
                    ),
                ),
                "validated_backends": fields.Boolean(
                    default=True,
                    description=(
                        f"When true, only inference backends that are validated by the {horde_title} devs will serve this request. "
                        "When False, non-validated backends will also be used which can increase speed but "
                        "you may end up with unexpected results."
                    ),
                ),
                "slow_workers": fields.Boolean(
                    default=True,
                    description="When True, allows slower workers to pick up this request. Disabling this incurs an extra kudos cost.",
                ),
                "workers": fields.List(
                    fields.String(description="Specify up to 5 workers which are allowed to service this request."),
                ),
                "worker_blacklist": fields.Boolean(
                    default=False,
                    required=False,
                    description="If true, the worker list will be treated as a blacklist instead of a whitelist.",
                ),
                "models": fields.List(
                    fields.String(description="Specify which models are allowed to be used for this request."),
                ),
                "dry_run": fields.Boolean(
                    default=False,
                    description="When true, the endpoint will simply return the cost of the request in kudos and exit.",
                ),
                "proxied_account": fields.String(
                    description=(
                        "If using a service account as a proxy, provide this value to identify the actual account "
                        "from which this request is coming from."
                    ),
                ),
                "extra_source_images": fields.List(fields.Nested(self.model_extra_source_images)),
                "disable_batching": fields.Boolean(
                    default=False,
                    description=(
                        "When true, This request will not use batching. This will allow you to retrieve accurate seeds. "
                        "Feature is restricted to Trusted users and Patreons."
                    ),
                ),
                "allow_downgrade": fields.Boolean(
                    default=False,
                    description=(
                        "When true and the request requires upfront kudos and the account does not have enough "
                        "The request will be downgraded in max context and max tokens so that it does not need upfront kudos."
                    ),
                ),
                "webhook": fields.String(
                    description=(
                        f"Provide a URL where the {horde_title} will send a POST call after each delivered generation. "
                        "The request will include the details of the job as well as the request ID."
                    ),
                ),
                "extra_slow_workers": fields.Boolean(
                    default=False,
                    description=(
                        "When True, allows very slower workers to pick up this request. " "Use this when you don't mind waiting a lot."
                    ),
                ),
            },
        )
        self.response_model_contrib_details = api.inherit(
            "ContributionsDetailsKobold",
            self.response_model_contrib_details,
            {
                "tokens": fields.Float(description="How many tokens this user has generated."),
            },
        )
        self.response_model_use_details = api.inherit(
            "UsageDetailsKobold",
            self.response_model_use_details,
            {
                "tokens": fields.Float(description="How many tokens this user has requested."),
            },
        )
        self.response_model_user_details = api.inherit(
            "UserDetailsKobold",
            self.response_model_user_details,
            {
                "kudos_details": fields.Nested(self.response_model_user_kudos_details),
                "usage": fields.Nested(self.response_model_use_details),
                "contributions": fields.Nested(self.response_model_contrib_details),
            },
        )
        self.response_model_team_details = api.inherit(
            "TeamDetailsKobold",
            self.response_model_team_details,
            {
                "contributions": fields.Float(
                    description="How many tokens the workers in this team have been rewarded while part of this team.",
                ),
                "performance": fields.Float(
                    description="The average performance of the workers in this team, in tokens per second.",
                ),
                "total_speed": fields.Float(
                    description="The total expected speed of this team when all workers are working parallel, in tokens per second.",
                ),
            },
        )

        self.response_model_single_period_total_txt_stat = api.model(
            "SinglePeriodTxtStat",
            {
                "requests": fields.Integer(description="The amount of text requests generated during this period."),
                "tokens": fields.Integer(description="The amount of tokens generated during this period."),
            },
        )

        self.response_model_stats_img_totals = api.model(
            "StatsTxtTotals",
            {
                "minute": fields.Nested(self.response_model_single_period_total_txt_stat),
                "hour": fields.Nested(self.response_model_single_period_total_txt_stat),
                "day": fields.Nested(self.response_model_single_period_total_txt_stat),
                "month": fields.Nested(self.response_model_single_period_total_txt_stat),
                "total": fields.Nested(self.response_model_single_period_total_txt_stat),
            },
        )

        self.response_model_model_stats = api.model(
            "SinglePeriodTxtModelStats",
            {
                "*": fields.Wildcard(
                    fields.Integer(
                        required=True,
                        description="The amount of requests fulfilled for this model.",
                    ),
                ),
            },
        )

        self.response_model_stats_models = api.model(
            "TxtModelStats",
            {
                "day": fields.Nested(self.response_model_model_stats),
                "month": fields.Nested(self.response_model_model_stats),
                "total": fields.Nested(self.response_model_model_stats),
            },
        )
        self.input_model_job_submit = api.inherit(
            "SubmitInputKobold",
            self.input_model_job_submit,
            {
                "gen_metadata": fields.List(fields.Nested(self.model_job_metadata)),
            },
        )
