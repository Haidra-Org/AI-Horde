# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import Any

from flask_restx import fields, reqparse
from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_TOOL_COUNT, MAX_CHAT_TOOL_SPEC_BYTES
from horde_sdk.generation_parameters.text.chat_policy import (
    DEFAULT_CHAT_CONFORMANCE,
    ChatConformance,
    ChatFeature,
    ChatGuarantee,
    ChatServiceGapKind,
)
from horde_sdk.worker.chat_declaration import ChatFaultReason, ChatFinishReason, ChatImageTransport

from horde.apis.models import v2
from horde.chat_contract import (
    MAX_CHAT_TOOL_CALL_COUNT,
    MAX_CHAT_TOOL_CALL_ID_LENGTH,
    ChatContextFit,
)
from horde.classes.base.style_application import MAX_TEMPLATE_FIELD_WORDS
from horde.classes.base.style_contract import MAX_TEMPLATE_FIELDS
from horde.classes.kobold.chat_request import (
    DEFAULT_CHAT_MAX_TOKENS,
    MAX_NATIVE_FINISH_REASON_LENGTH,
    MAX_REPORTED_TOKEN_COUNT,
    ChargeBasis,
    ChatRole,
)
from horde.classes.kobold.request_fit import (
    DEFAULT_MAX_CONTEXT_LENGTH,
    DEFAULT_MAX_LENGTH,
    MAX_CONTEXT_LENGTH_LIMIT,
    MAX_LENGTH_LIMIT,
    ContextFit,
)
from horde.vars import horde_title


class NullableStringField(fields.Raw):
    """A string field that may also be null.

    Swagger 2 has no nullable type, and the schema a validated route checks a body against is the documented one, so
    the field declares no type and carries the ``x-nullable`` vendor extension instead. The endpoint's own model
    checks the value.
    """

    __schema_type__ = None

    def schema(self) -> dict[str, Any]:
        """Return the field's schema, marked ``x-nullable``."""
        return {**super().schema(), "x-nullable": True}


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
        self.generate_parser.add_argument(
            "context_fit",
            type=str,
            required=False,
            help="What to do when the prompt does not fit the requested max_context_length.",
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
        self.job_pop_parser.add_argument(
            "chat_jobs",
            type=bool,
            required=False,
            default=False,
            help="The worker accepts chat jobs, which carry messages instead of a prompt.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "prompt_jobs",
            type=bool,
            required=False,
            default=True,
            help="The worker accepts text jobs, which carry a prompt. False requires chat_jobs.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "chat_features",
            type=list,
            required=False,
            help="The optional chat features the worker implements and the chat guarantees its backend keeps.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "chat_limits",
            type=dict,
            required=False,
            help="The context, output and tool temperature limits for chat jobs. Required with chat_jobs.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "model_id",
            type=str,
            required=False,
            help="The model the worker serves chat jobs with. Required with chat_jobs.",
            location="json",
        )
        self.job_pop_parser.add_argument(
            "image_transports",
            type=list,
            required=False,
            help="How the worker accepts a chat job's images: url, base64 or both. Absent means it takes none.",
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
        self.job_submit_parser.add_argument(
            "message",
            type=dict,
            required=False,
            help="A chat job's assistant message, sent in place of generation.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "finish_reason",
            type=str,
            required=False,
            choices=[finish_reason.value for finish_reason in ChatFinishReason],
            help="Why the worker stopped generating a chat job.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "native_finish_reason",
            type=str,
            required=False,
            help="The backend's own name for why it stopped.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "prompt_tokens",
            type=int,
            required=False,
            help="How many tokens the prompt took, as the worker's tokenizer counted them.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "completion_tokens",
            type=int,
            required=False,
            help="How many tokens the worker generated.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "model_id",
            type=str,
            required=False,
            help="The model a chat job ran on, which has to be the model it was assigned.",
            location="json",
        )
        self.job_submit_parser.add_argument(
            "fault_reason",
            type=str,
            required=False,
            help="Why the worker faulted a chat job, sent with state faulted. An unknown value is ignored.",
            location="json",
        )
        # A chat job's submit carries message rather than generation; a text job's submit keeps requiring it.
        self.chat_job_submit_parser = self.job_submit_parser.copy()
        self.chat_job_submit_parser.replace_argument(
            "generation",
            type=str,
            required=False,
            help="Not sent for chat jobs; the message content is stored as the generation.",
            location="json",
        )
        # A chat request's body is validated against its own model in the endpoint; only the headers are parsed.
        self.chat_generate_parser = reqparse.RequestParser()
        self.chat_generate_parser.add_argument(
            "apikey",
            type=str,
            required=False,
            help="The API Key corresponding to a registered user.",
            location="headers",
        )
        self.chat_generate_parser.add_argument(
            "Client-Agent",
            default="unknown:0:unknown",
            type=str,
            required=False,
            help="The client name and version.",
            location="headers",
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
        self.root_model_generation_payload_style_kobold = api.model(
            "ModelPayloadStyleKobold",
            {
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
                "temperature": fields.Float(description="Temperature value.", min=0, max=5.0),
                "tfs": fields.Float(description="Tail free sampling value.", min=0.0, max=1.0),
                "top_a": fields.Float(description="Top-a sampling value.", min=0.0, max=1.0),
                "top_k": fields.Integer(description="Top-k sampling value.", min=0, max=200),
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
        # Defined once and reused: a style's ceiling on either param has to sit inside the range the
        # param itself accepts, and a style's own params model advertises that same range.
        kobold_max_context_length = fields.Integer(
            min=80,
            default=DEFAULT_MAX_CONTEXT_LENGTH,
            max=MAX_CONTEXT_LENGTH_LIMIT,
            description="Maximum number of tokens to send to the model.",
        )
        kobold_max_length = fields.Integer(
            min=16,
            max=MAX_LENGTH_LIMIT,
            default=DEFAULT_MAX_LENGTH,
            description="Number of tokens to generate.",
        )
        self.root_model_generation_payload_kobold = api.inherit(
            "ModelPayloadRootKobold",
            self.root_model_generation_payload_style_kobold,
            {
                "n": fields.Integer(example=1, min=1, max=20),
                "max_context_length": kobold_max_context_length,
                "max_length": kobold_max_length,
                # "soft_prompt": fields.String(
                #     description=(
                #         "Soft prompt to use when generating. If set to the empty string or any other string containing "
                #         "no non-whitespace characters, uses no soft prompt."
                #     )
                # ),
            },
        )
        # The pop response playload
        self.response_model_generation_payload = api.inherit(
            "ModelPayloadKobold",
            self.root_model_generation_payload_kobold,
            {
                "prompt": fields.String(description="The prompt which will be sent to KoboldAI to generate the text."),
            },
        )
        self.response_model_chat_generation_payload = api.model(
            "ModelPayloadChat",
            {
                "messages": fields.List(
                    fields.Raw,
                    description=(
                        "The OpenAI chat messages array, in place of a prompt. Each entry has a role (system, user, "
                        "assistant or tool) and a content that is a string or an array of parts. A user message's parts "
                        "may be image parts, {type: image_url, image_url: {url, detail?}}, whose url is a signed JPEG "
                        "download URL for a worker that declared the url transport, or a data:image/jpeg;base64 URL for "
                        "one that declared only base64. An assistant message may carry tool_calls, with content null "
                        "when it carries nothing else; a tool message carries tool_call_id, the id of the call it "
                        "answers. A developer message is sent as system. The worker applies its own chat template. "
                        "Absent when messages_url is sent."
                    ),
                ),
                "messages_url": fields.String(
                    description=(
                        "In place of messages, for a worker that declared the messages_url transport and a request "
                        "whose messages the horde stored as one object: a signed URL, valid for 120 seconds, to the "
                        "messages array as JSON with each image part's url a data:image/jpeg;base64 URL. The body is "
                        "served either plain or with Content-Encoding: gzip; honour the header. A failed fetch faults "
                        "the job."
                    ),
                ),
                "tools": fields.List(
                    fields.Raw,
                    description="The request's OpenAI tools, as the client sent them; only when the request offers tools.",
                ),
                "tool_choice": fields.Raw(
                    description=(
                        "The request's tool_choice as the client sent it: 'auto', 'required' or {type: function, "
                        "function: {name}}; only when the client sent it with tools."
                    ),
                ),
                "parallel_tool_calls": fields.Boolean(
                    description="The request's parallel_tool_calls; only when the client sent it with tools.",
                ),
                "response_format": fields.Raw(
                    description=(
                        "The request's response_format as the client sent it, {type: json_object} or {type: "
                        "json_schema, json_schema: {name, schema, description?, strict?}}; absent for text."
                    ),
                ),
                "temperature": fields.Float(description="Sampling temperature; 1.0 when the client did not set it."),
                "top_p": fields.Float(description="Nucleus sampling value; 1.0 when the client did not set it."),
                "frequency_penalty": fields.Float(description="OpenAI's frequency penalty; 0 when the client did not set it."),
                "presence_penalty": fields.Float(description="OpenAI's presence penalty; 0 when the client did not set it."),
                "rep_pen": fields.Float(description="Repetition penalty; always 1.0 (none)."),
                "top_k": fields.Integer(description="Top-k sampling value; always 0 (disabled)."),
                "min_p": fields.Float(description="Min-p sampling value; always 0 (disabled)."),
                "stop_sequence": fields.List(
                    fields.String,
                    description="Stop sequences, only when the client sent them.",
                ),
                "seed": fields.Integer(description="The sampling seed, only when the client sent one."),
                "max_length": fields.Integer(description="Number of tokens to generate."),
                "max_context_length": fields.Integer(description="The context length the rendered messages have to fit."),
                "n": fields.Integer(description="Always 1."),
            },
        )
        # The generation input
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
                "messages": fields.List(fields.Nested(self.response_model_message, skip_none=True)),
                "ttl": fields.Integer(description="The amount of seconds before this job is considered stale and aborted."),
                "extra_source_images": fields.List(fields.Nested(self.model_extra_source_images)),
                "skipped": fields.Nested(self.response_model_generations_skipped, skip_none=True),
                "softprompt": fields.String(description="The soft prompt requested for this generation."),
                "model": fields.String(description="Which of the available models to use for this request."),
            },
        )
        # A separate model so the chat key and payload never appear in a text job's pop response. It copies the text
        # fields rather than inheriting them, since an inherited model's parent fields override its own.
        self.response_model_chat_job_pop = api.model(
            "GenerationPayloadChat",
            {
                **self.response_model_job_pop,
                "payload": fields.Nested(self.response_model_chat_generation_payload, skip_none=True),
                "chat": fields.Boolean(description="Always true: this is a chat job, whose payload carries messages."),
            },
        )
        self.input_model_chat_limits = api.model(
            "ChatLimits",
            {
                "max_context_length": fields.Integer(
                    required=True,
                    min=1,
                    description="The largest context, rendered messages and reply together, a chat job may need.",
                ),
                "max_length": fields.Integer(
                    required=True,
                    min=1,
                    description="The most tokens the worker generates for a chat job.",
                ),
                # No schema minimum: a negative value is refused by the pop with ChatDeclarationInvalid, as the other
                # declaration errors are, rather than by the generic payload validation.
                "tools_max_temperature": fields.Float(
                    description=(
                        "The highest temperature, at least 0, the backend applies as sent when tools are present. A "
                        "chat request that forwards tools at a higher temperature is not given to the worker. Absent "
                        "means every temperature is applied as sent."
                    ),
                ),
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
                "chat_jobs": fields.Boolean(
                    default=False,
                    description=(
                        "The worker accepts chat jobs, which carry messages instead of a prompt. A worker that sets "
                        "it still receives text jobs unless prompt_jobs is false. Requires chat_limits and model_id."
                    ),
                ),
                "prompt_jobs": fields.Boolean(
                    default=True,
                    description=(
                        "The worker accepts text jobs, which carry a prompt. False, together with chat_jobs, makes "
                        "the worker chat-only: it receives only chat jobs, and max_length and max_context_length "
                        "do not apply. False without chat_jobs is refused."
                    ),
                ),
                "chat_features": fields.List(
                    fields.String,
                    description=(
                        "The optional chat features the worker implements: "
                        f"{', '.join(feature.value for feature in ChatFeature)}; and the parts of the OpenAI chat "
                        f"contract its backend follows exactly: {', '.join(guarantee.value for guarantee in ChatGuarantee)}. "
                        "A chat request that needs a feature (a non-default seed or penalty, image parts, tools or a "
                        "history with tool calls, a json_object or json_schema response format) only goes to workers "
                        "that list it; under strict conformance so does a request that needs a guarantee. Unknown "
                        "names are ignored."
                    ),
                ),
                "chat_limits": fields.Nested(
                    self.input_model_chat_limits,
                    description="The limits chat jobs run under, in place of max_context_length and max_length.",
                ),
                "model_id": fields.String(
                    description="The model the worker serves chat jobs with; exactly one of its models.",
                ),
                "image_transports": fields.List(
                    fields.String,
                    description=(
                        f"How the worker accepts a chat job's images: {', '.join(transport.value for transport in ChatImageTransport)}. "
                        "'url' fetches a signed object storage URL, 'base64' "
                        "reads a data URL in the pop response. A chat request with images only goes to workers that "
                        "declare the vision feature and url or base64; url is used whenever it is declared. "
                        "'messages_url' fetches the whole messages array from the payload's messages_url, sent in "
                        "place of messages for a request whose messages the horde stored as one object; it is "
                        "declared beside url or base64. "
                        "Absent or empty means the worker takes no images. Unknown names are ignored."
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
                "style": fields.String(
                    required=False,
                    max_length=1024,
                    min_length=3,
                    example="00000000-0000-0000-0000-000000000000",
                    description=("A horde style ID or name to use for this generation"),
                ),
                "template_fields": fields.Raw(
                    required=False,
                    example={"caption": "a lighthouse in a storm"},
                    description=(
                        "Values for the placeholders the requested style's prompt accepts, as a flat object of "
                        "strings keyed by field name. Only accepted together with 'style', and only for fields "
                        "that style declares. Each value, and all of them together, are limited to the same "
                        f"length as a prompt ({MAX_TEMPLATE_FIELD_WORDS} words)."
                    ),
                ),
                "context_fit": fields.String(
                    required=False,
                    default=ContextFit.IGNORE.value,
                    enum=[setting.value for setting in ContextFit],
                    description=(
                        "What to do when the prompt does not fit the requested max_context_length. "
                        f"'{ContextFit.IGNORE.value}' sends it anyway and the worker cuts the front of it away, "
                        f"'{ContextFit.REJECT.value}' refuses the request, and "
                        f"'{ContextFit.GROW.value}' raises max_context_length until the prompt fits and charges "
                        "for the larger context."
                    ),
                ),
                "extra_slow_workers": fields.Boolean(
                    default=False,
                    description=(
                        "When True, allows very slower workers to pick up this request. Use this when you don't mind waiting a lot."
                    ),
                ),
            },
        )
        self.response_model_submitted_request = v2.derive_submitted_request_model(
            api,
            "GenerationSubmittedKobold",
            self.input_model_request_generation,
        )
        self.response_model_resolved_request = api.inherit(
            "ResolvedRequestKobold",
            self.response_model_resolved_request,
            {
                "estimated_prompt_tokens": fields.Integer(
                    description=(
                        "The estimated token count of the prompt. The horde runs no tokenizer, so this is a "
                        "conservative estimate from the character count."
                    ),
                ),
                "context_fit": fields.String(
                    enum=[setting.value for setting in ContextFit],
                    description="The context_fit setting that was applied.",
                ),
            },
        )
        # skip_none is off here: a null inside the resolved request, where no style was applied, is
        # part of the answer and has to be returned rather than dropped.
        resolved_request_field = fields.Nested(
            self.response_model_resolved_request,
            allow_null=True,
            skip_none=False,
            description="The request as the horde resolved it. Only returned for a dry run.",
        )
        # The queued response marshals through this model too and leaves 'resolved' unset: it then
        # marshals to null and the endpoint's skip_none drops it, so the 202 body is unchanged.
        self.response_model_async = api.inherit(
            "RequestAsyncKobold",
            self.response_model_async,
            {"resolved": resolved_request_field},
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

        # NOTE: these per-period maps are keyed by model name (an unbounded set for
        # text models). Marshalling them with fields.Wildcard triggers
        # flask_restx's O(N) Wildcard._flatten per request, which pegged a CPU core
        # (~13s/request in prod). The source is already a plain dict[str, int], so
        # fields.Raw emits identical JSON without the flatten cost.
        self.response_model_stats_models = api.model(
            "TxtModelStats",
            {
                "day": fields.Raw(description="The amount of requests fulfilled for each model in the past day."),
                "month": fields.Raw(description="The amount of requests fulfilled for each model in the past month."),
                "total": fields.Raw(description="The total amount of requests fulfilled for each model."),
            },
        )
        self.model_chat_function_call = api.model(
            "ChatFunctionCall",
            {
                "name": fields.String(description="The function called."),
                "arguments": fields.String(description="The arguments as the model wrote them, a JSON document in a string."),
            },
        )
        self.model_chat_tool_call = api.model(
            "ChatToolCall",
            {
                "id": fields.String(
                    description=f"The id a tool message answers, 1 to {MAX_CHAT_TOOL_CALL_ID_LENGTH} characters.",
                ),
                "type": fields.String(enum=["function"], description="Always 'function'."),
                "function": fields.Nested(self.model_chat_function_call),
            },
        )
        self.input_model_chat_submitted_message = api.model(
            "ChatSubmittedMessage",
            {
                "role": fields.String(required=True, enum=["assistant"], description="Always 'assistant'."),
                "content": NullableStringField(
                    description=(
                        "The reply text, a string. An empty string is a completed reply with no visible text; null only with tool_calls."
                    ),
                ),
                "tool_calls": fields.List(
                    fields.Nested(self.model_chat_tool_call),
                    description=(
                        f"The tool calls the model made, 1 to {MAX_CHAT_TOOL_CALL_COUNT}, with the ids the backend "
                        "minted. A tool call is a complete reply and is charged as output with the content."
                    ),
                ),
            },
        )
        self.input_model_job_submit = api.inherit(
            "SubmitInputKobold",
            self.input_model_job_submit,
            {
                "gen_metadata": fields.List(fields.Nested(self.model_job_metadata)),
                "message": fields.Nested(
                    self.input_model_chat_submitted_message,
                    description="Chat jobs only, in place of generation: the assistant message.",
                ),
                "finish_reason": fields.String(
                    enum=[finish_reason.value for finish_reason in ChatFinishReason],
                    description=(
                        "Chat jobs only, required: why the worker stopped generating. 'stop' for an end of reply or "
                        "stop sequence, 'length' at max_length, 'content_filter' when the backend withheld output, "
                        "'tool_calls' when the model called tools, which needs message.tool_calls."
                    ),
                ),
                "native_finish_reason": fields.String(
                    max_length=MAX_NATIVE_FINISH_REASON_LENGTH,
                    description="Chat jobs only, optional: the backend's own name for why it stopped.",
                ),
                "prompt_tokens": fields.Integer(
                    min=0,
                    max=MAX_REPORTED_TOKEN_COUNT,
                    description=(
                        "Chat jobs only, optional: how many tokens the rendered messages took. Stored and shown, never "
                        "charged; omit it when the backend did not count them."
                    ),
                ),
                "completion_tokens": fields.Integer(
                    min=0,
                    max=MAX_REPORTED_TOKEN_COUNT,
                    description=(
                        "Chat jobs only, optional: how many tokens the worker generated. Stored and shown, never "
                        "charged; omit it when the backend did not count them."
                    ),
                ),
                "model_id": fields.String(
                    description="Chat jobs only, required: the model the job ran on, which has to be the model it was assigned.",
                ),
                # No enum, so the model validation does not refuse a reason this Horde does not know.
                "fault_reason": fields.String(
                    description=(
                        "Chat jobs only, optional, with state 'faulted': why the worker faulted the job, one of "
                        f"{', '.join(repr(reason.value) for reason in ChatFaultReason)}. The Horde stores its "
                        "own message for the reason as the request's fault_reason with fault_rc 'ChatWorkerFault'. An "
                        "unknown value is ignored, and the value is ignored on every other submit."
                    ),
                ),
            },
        )
        self._define_chat_models(api)

        # Styles
        # The two params a policy can cap are declared on ModelPayloadRootKobold, which a style's params
        # do not inherit, so a style setting them marshalled them away and a client could not size a
        # request against the style. They are redeclared here rather than moved to the shared parent, so
        # ModelPayloadRootKobold keeps the declaration that carries the request defaults. No defaults
        # here: a style that does not set a param leaves it unset, and skip_none on the nested params
        # field then drops the key, as it does for every other param without a default.
        self.input_model_style_params = api.inherit(
            "ModelStyleInputParamsKobold",
            self.root_model_generation_payload_style_kobold,
            {
                "max_context_length": fields.Integer(
                    min=kobold_max_context_length.minimum,
                    max=kobold_max_context_length.maximum,
                    description=kobold_max_context_length.description,
                ),
                "max_length": fields.Integer(
                    min=kobold_max_length.minimum,
                    max=kobold_max_length.maximum,
                    description=kobold_max_length.description,
                ),
            },
        )
        self.input_model_style = api.model(
            "ModelStyleInputKobold",
            {
                "name": fields.String(
                    required=True,
                    example="My Awesome Text Style",
                    description="The name for the style. Case-sensitive and unique per user.",
                    min_length=1,
                    max_length=100,
                ),
                "info": fields.String(
                    required=False,
                    description="Some information about this style.",
                    example="Dark, brooding vibes",
                    min_length=10,
                    max_length=1000,
                ),
                "prompt": fields.String(
                    required=False,
                    description=(
                        "The prompt template which wraps the request's prompt before it is sent to the text model."
                        " This argument MUST include a '{p}' which specifies the part where the request's prompt"
                        " will be injected, and every template field this style declares is filled in by name."
                        " A '{{[NAME]}}' token, with uppercase letters and underscores between the brackets, is left in"
                        " the prompt exactly as written, as described in docs/reference/style_contract.md."
                    ),
                    default="{p}",
                    min_length=3,
                ),
                "params": fields.Nested(self.input_model_style_params, skip_none=True),
                # allow_null so a style with no policy returns the key as null rather than as an empty
                # object, giving clients one shape either way.
                "parameter_policy": fields.Nested(
                    self.model_style_parameter_policy,
                    skip_none=True,
                    allow_null=True,
                    required=False,
                    description="Which params a request using this style may set, and the limits on them.",
                ),
                "template_fields": fields.List(
                    fields.Nested(self.model_style_template_field, skip_none=True),
                    required=False,
                    allow_null=True,
                    description=f"The placeholders this style's prompt accepts from the request, at most {MAX_TEMPLATE_FIELDS} of them.",
                ),
                "public": fields.Boolean(
                    default=True,
                    description=(
                        "When true this style will be listed among all styles publicly."
                        "When false, information about this style can only be seen by people who know its ID or name."
                    ),
                ),
                "nsfw": fields.Boolean(
                    default=False,
                    description=("When true, it signified this style is expected to generare NSFW images primarily."),
                ),
                "tags": fields.List(
                    fields.String(
                        description="Tags describing this style. Used for filtering and discovery.",
                        min_length=1,
                        max_length=25,
                        example="dark",
                    ),
                ),
                "models": fields.List(fields.String(description="The models to use with this style.", min_length=1, example="llama3")),
            },
        )
        self.patch_model_style = api.model(
            "ModelStylePatchKobold",
            {
                "name": fields.String(
                    required=False,
                    example="My Awesome Text Style",
                    description="The name for the style. Case-sensitive and unique per user.",
                    min_length=1,
                    max_length=100,
                ),
                "info": fields.String(
                    required=False,
                    example="Dark, brooding vibes",
                    description="Extra information about this style.",
                    min_length=1,
                    max_length=1000,
                ),
                "prompt": fields.String(
                    required=False,
                    description=(
                        "The prompt template which wraps the request's prompt before it is sent to the text model."
                        " This argument MUST include a '{p}' which specifies the part where the request's prompt"
                        " will be injected, and every template field this style declares is filled in by name."
                        " A '{{[NAME]}}' token, with uppercase letters and underscores between the brackets, is left in"
                        " the prompt exactly as written, as described in docs/reference/style_contract.md."
                    ),
                    min_length=7,
                ),
                "params": fields.Nested(self.input_model_style_params, skip_none=True),
                # allow_null so a style with no policy returns the key as null rather than as an empty
                # object, giving clients one shape either way.
                "parameter_policy": fields.Nested(
                    self.model_style_parameter_policy,
                    skip_none=True,
                    allow_null=True,
                    required=False,
                    description="Which params a request using this style may set, and the limits on them.",
                ),
                "template_fields": fields.List(
                    fields.Nested(self.model_style_template_field, skip_none=True),
                    required=False,
                    allow_null=True,
                    description=f"The placeholders this style's prompt accepts from the request, at most {MAX_TEMPLATE_FIELDS} of them.",
                ),
                "public": fields.Boolean(
                    default=True,
                    description=(
                        "When true this style will be listed among all styles publicly."
                        "When false, information about this style can only be seen by people who know its ID or name."
                    ),
                ),
                "nsfw": fields.Boolean(
                    default=False,
                    description=("When true, it signified this style is expected to generare NSFW images primarily."),
                ),
                "tags": fields.List(
                    fields.String(
                        description="Tags describing this style. Used for filtering and discovery.",
                        min_length=1,
                        max_length=25,
                        example="dark",
                    ),
                ),
                "models": fields.List(fields.String(description="The models to use with this style.", min_length=1, example="llama3")),
            },
        )
        self.response_model_style = api.inherit(
            "StyleKobold",
            self.input_model_style,
            {
                "id": fields.String(
                    description="The UUID of the style. Use this to use the style or retrieve its information in the future.",
                    example="00000000-0000-0000-0000-000000000000",
                ),
                "use_count": fields.Integer(description="The amount of times this style has been used in generations."),
                "creator": fields.String(description="The alias of the user to whom this style belongs to.", example="db0#1"),
                "updated": fields.DateTime(
                    description="When this style was last modified, so a client can tell whether a cached copy is stale.",
                ),
                "shared_key": fields.Nested(
                    self.response_model_sharedkey_details,
                    skip_none=True,
                    allow_null=True,
                    description=(
                        "The shared key requests using this style generate under. Served only on the single-style routes"
                        " to the style's owner, authenticated by the apikey header; null for every other caller and on"
                        " the style list."
                    ),
                ),
            },
        )

    def _define_chat_models(self, api):
        """Define the models of the chat routes, which take an OpenAI chat-completions body.

        The input models document the body; the endpoint validates it itself, since unknown keys are
        refused by name, which a flask-restx model does not do.
        """
        self.model_chat_text_part = api.model(
            "ChatTextPart",
            {
                "type": fields.String(required=True, enum=["text"], description="Always 'text'."),
                "text": fields.String(required=True, description="The part's text."),
            },
        )
        self.model_chat_message = api.model(
            "ChatMessage",
            {
                "role": fields.String(
                    required=True,
                    enum=[chat_role.value for chat_role in ChatRole],
                    description="Who wrote the message. A developer message is stored and sent to workers as system.",
                ),
                "content": fields.Raw(
                    description=(
                        "The message text, as a string or as an array of parts: ChatTextPart, or on a user message "
                        "{type: image_url, image_url: {url, detail?}} with a base64 image data URL or an http(s) URL. "
                        "At most 5 images per request, 1 for users who are neither trusted nor patrons, each at most "
                        "5 MiB and 3072*3072 pixels. A tool message takes text only. Null or absent only on an "
                        "assistant message with tool_calls."
                    ),
                ),
                "name": fields.String(
                    description="An optional participant name, passed to the worker unchanged. Not on a tool message.",
                ),
                "tool_calls": fields.List(
                    fields.Nested(self.model_chat_tool_call),
                    description=f"Assistant messages only: the tool calls the model made, 1 to {MAX_CHAT_TOOL_CALL_COUNT}.",
                ),
                "tool_call_id": fields.String(
                    description=(
                        "Tool messages only, required: the id of the tool call this message answers, which a tool "
                        "call of an earlier assistant message has to carry."
                    ),
                ),
            },
        )
        self.model_chat_horde_options = api.model(
            "ChatHordeOptions",
            {
                "only": fields.List(
                    fields.String,
                    description="Worker IDs the request is limited to. Cannot be combined with 'ignore'.",
                ),
                "ignore": fields.List(
                    fields.String,
                    description="Worker IDs that never serve the request. Cannot be combined with 'only'.",
                ),
                "trusted_workers": fields.Boolean(default=False, description="When true, only trusted workers serve the request."),
                "validated_backends": fields.Boolean(default=False, description="When true, only validated backends serve the request."),
                "slow_workers": fields.Boolean(default=True, description="When false, workers below the speed threshold are skipped."),
                "max_context_length": fields.Integer(description="An explicit context length."),
                "context_fit": fields.String(
                    default=ChatContextFit.GROW.value,
                    enum=[setting.value for setting in ChatContextFit],
                    description=(
                        "What to do when the messages do not fit the context: grow it up to what an online chat worker "
                        "serves, or reject the request."
                    ),
                ),
                "conformance": fields.String(
                    default=DEFAULT_CHAT_CONFORMANCE.value,
                    enum=[conformance.value for conformance in ChatConformance],
                    description=(
                        "How closely the serving worker has to follow the OpenAI chat contract. strict only uses "
                        "workers that declare every guarantee the request needs; best_effort drops that requirement "
                        "and keeps the feature and limit checks."
                    ),
                ),
                "dry_run": fields.Boolean(default=False, description="When true, return the kudos cost without queueing."),
                "operation_key": fields.String(
                    description=(
                        "A UUID identifying one submission of this request, claimed for the submitting user when the "
                        "request is queued. The same key with the same request returns the first request with status "
                        "200; with a changed request it is refused with OperationKeyConflict. A dry run claims nothing."
                    ),
                ),
            },
        )
        self.input_model_chat_generation = api.model(
            "ChatGenerationInput",
            {
                "model": fields.String(required=True, description="The horde text model to generate with."),
                "messages": fields.List(fields.Nested(self.model_chat_message), required=True, min_items=1),
                "max_tokens": fields.Integer(
                    description=(
                        f"How many tokens to generate, 16 to 4096. Defaults to {DEFAULT_CHAT_MAX_TOKENS}. Must equal "
                        "max_completion_tokens when both are set."
                    ),
                ),
                "max_completion_tokens": fields.Integer(description="How many tokens to generate; the newer name for max_tokens."),
                "temperature": fields.Float(description="Sampling temperature."),
                "top_p": fields.Float(description="Nucleus sampling value."),
                "n": fields.Integer(description="How many choices to return. Only 1 is served."),
                "seed": fields.Integer(description="A sampling seed. Only workers that declare the seed feature serve it."),
                "stop": fields.Raw(description="One stop sequence or an array of them."),
                "frequency_penalty": fields.Float(
                    min=-2,
                    max=2,
                    description="Frequency penalty. A non-zero value only goes to workers that declare the feature.",
                ),
                "presence_penalty": fields.Float(
                    min=-2,
                    max=2,
                    description="Presence penalty. A non-zero value only goes to workers that declare the feature.",
                ),
                "stream": fields.Boolean(description="Accepted and not applied on this route."),
                "stream_options": fields.Raw(description="Accepted and not applied on this route."),
                "user": fields.String(description="Accepted and not applied."),
                "metadata": fields.Raw(description="Accepted and not applied."),
                "tools": fields.List(
                    fields.Raw,
                    description=(
                        f"OpenAI function tools, {{type: function, function: {{name, description?, parameters?, "
                        f"strict?}}}}, at most {MAX_CHAT_TOOL_COUNT}. Forwarded to the worker as sent; a request with "
                        "tools only goes to workers that declare the tools feature. An empty list is the same as none."
                    ),
                ),
                "tool_choice": fields.Raw(
                    description=(
                        "'none', 'auto', 'required' or {type: function, function: {name}}. 'none' is the same as "
                        "sending no tools; any other value needs tools, and a named function has to be one of them, "
                        "or the request is refused with ChatRequestConflict."
                    ),
                ),
                "parallel_tool_calls": fields.Boolean(
                    description="Whether the model may call several tools in one reply; forwarded with tools, dropped without.",
                ),
                "response_format": fields.Raw(
                    description=(
                        "{type: text}, {type: json_object} or {type: json_schema, json_schema: {name, schema, "
                        "description?, strict?}}. text is the same as none; the other two only go to workers that "
                        "declare the feature of that name. "
                        f"tools, tool_choice and response_format may take {MAX_CHAT_TOOL_SPEC_BYTES} bytes of compact "
                        "JSON together; more is refused with UnsupportedChatParameter."
                    ),
                ),
                "horde": fields.Nested(self.model_chat_horde_options, description="Horde options OpenAI has no key for."),
            },
        )
        self.response_model_chat_generation_result = api.inherit(
            "GenerationChat",
            self.response_model_generation_result,
            {
                "message": fields.Raw(
                    description=(
                        "The assistant message the worker submitted, with role and content, and tool_calls when the "
                        "model called tools (content is then null or text)."
                    ),
                ),
                "finish_reason": fields.String(
                    enum=[finish_reason.value for finish_reason in ChatFinishReason],
                    description="Why the worker stopped generating.",
                ),
                "native_finish_reason": fields.String(
                    description="The backend's own name for why it stopped; null when the worker did not report it.",
                ),
                "prompt_tokens": fields.Integer(description="The prompt token count the worker reported; null when not reported."),
                "completion_tokens": fields.Integer(description="The generated token count the worker reported; null when not reported."),
                "charge_basis": fields.String(
                    enum=[charge_basis.value for charge_basis in ChargeBasis],
                    description="What the generation was charged and rewarded on.",
                ),
                "queue_wait_seconds": fields.Float(
                    description="Seconds from the request's creation to this generation's assignment to a worker.",
                ),
            },
        )
        self.response_model_chat_context_adjustment = api.model(
            "ChatContextAdjustment",
            {
                "from": fields.Integer(description="The context length the request asked for, or the default."),
                "to": fields.Integer(description="The context length the request runs under."),
                "reason": fields.String(
                    enum=[ChatContextFit.GROW.value],
                    description="Why it changed: 'grow' when the context was grown to fit the messages.",
                ),
            },
        )
        self.response_model_chat_impossible_reason = api.model(
            "ChatImpossibleReason",
            {
                "kind": fields.String(
                    enum=[impossible_kind.value for impossible_kind in ChatServiceGapKind],
                    description=(
                        "'declaration_gap': online chat workers serve the model, but none declares every capability "
                        "and guarantee the request needs, or each applies a limit the request exceeds."
                    ),
                ),
                "missing_declarations": fields.List(
                    fields.String,
                    description=(
                        "The capabilities and guarantees the refused workers lack, with 'image_transports' for a "
                        "worker declaring vision without a way to receive images."
                    ),
                ),
                "exceeded_limits": fields.List(
                    fields.String,
                    description="The declared limits the request exceeds, such as 'tools_max_temperature'.",
                ),
            },
        )
        self.response_model_chat_status_full = api.inherit(
            "RequestStatusChat",
            self.response_model_wp_status_lite,
            {
                "generations": fields.List(fields.Nested(self.response_model_chat_generation_result)),
                "context_adjustments": fields.List(
                    fields.Nested(self.response_model_chat_context_adjustment),
                    description="How the horde changed the request's context length; null when it did not.",
                ),
                "impossible_reason": fields.Nested(
                    self.response_model_chat_impossible_reason,
                    allow_null=True,
                    description=(
                        "Why no worker can take the request when the cause is the workers' declarations, which waiting "
                        "does not change while the online workers stay the same; null when is_possible is true or no "
                        "worker serves the model at all."
                    ),
                ),
                "fault_reason": fields.String(
                    description=(
                        "Why the request faulted, reported once it faulted: the message the horde refused the last "
                        "refused reply with, or the horde's message for the reason a worker gave for faulting a job, "
                        "whichever was stored last; null while it has not faulted or when neither was stored."
                    ),
                ),
                "fault_rc": fields.String(
                    description=(
                        "The return code of what fault_reason holds, such as 'ChatSubmitNonconformant' for a refusal "
                        "or 'ChatWorkerFault' for a worker's reason; null when fault_reason is."
                    ),
                ),
            },
        )
