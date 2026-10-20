# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import math
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError
from flask import request
from flask_restx import Resource, marshal_with, reqparse
from flask_restx.reqparse import ParseResult
from horde_sdk.generation_parameters.text.chat_policy import ChatConformance, ChatLimitField
from horde_sdk.worker.chat_declaration import ChatFinishReason
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import horde.apis.limiter_api as lim
from horde import exceptions as e
from horde import metrics
from horde.apis.models.kobold_v2 import TextModels, TextParsers
from horde.apis.v2.base import (
    GenerateTemplate,
    JobPopTemplate,
    JobSubmitTemplate,
    api,
    assert_submitting_key,
    commit_request_cancellation,
)
from horde.chat_contract import (
    MAX_CHAT_BODY_BYTES,
    MAX_CHAT_IMAGE_COUNT_UNTRUSTED,
    ChatContextFit,
    refuse_json_constant,
)
from horde.chat_signal import ChatEvent, publish_chat_event
from horde.classes.base import settings
from horde.classes.base.style_application import count_literal_text_template_fields, format_text_style_prompt
from horde.classes.base.style_contract import (
    ParameterCeilingBound,
    StyleContractVocabulary,
    build_vocabulary,
)
from horde.classes.base.waiting_prompt import EXTRA_SOURCE_IMAGE_HORDE_TAX
from horde.classes.kobold.chat_request import (
    CHAT_WORKER_FAULT_RC,
    MAX_NATIVE_FINISH_REASON_LENGTH,
    ChatSubmittedMessage,
    build_chat_extras,
    build_chat_image_key,
    build_model_names,
    build_operation_fingerprint,
    build_stored_reply,
    build_text_params,
    build_worker_targeting,
    chat_reply_finish_reason,
    chat_worker_fault_message,
    flatten_messages,
    list_image_urls,
    parse_chat_request,
    replace_image_urls,
    rewrite_message_texts,
    serialize_messages,
    verify_chat_reply,
)
from horde.classes.kobold.genstats import (
    get_compiled_textgen_stats_models,
    get_compiled_textgen_stats_totals,
)
from horde.classes.kobold.request_fit import (
    DEFAULT_MAX_CONTEXT_LENGTH,
    DEFAULT_MAX_LENGTH,
    MAX_CONTEXT_LENGTH_LIMIT,
    MAX_CONTEXT_LENGTH_PARAMETER,
    MAX_LENGTH_PARAMETER,
    ContextFit,
    ContextFitOutcome,
    context_growth_upper_bound,
    estimate_prompt_tokens,
    fit_context_length,
    parse_context_fit,
    prompt_fits_context,
)
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt, delete_stored_chat_images
from horde.classes.kobold.worker import TextWorker
from horde.config import Config
from horde.database import functions as database
from horde.database import text_functions as text_database
from horde.database.kudos_reservations import reserve_kudos
from horde.database.text_functions import ChatOperationClaim
from horde.detection import prompt_checker
from horde.enums import KudosEntryType, RequestTerminalOutcome
from horde.flask import cache, db
from horde.image import encode_chat_image, load_chat_image
from horde.limiter import limiter
from horde.logger import logger
from horde.model_reference import model_reference
from horde.patreon import patrons
from horde.r2 import upload_chat_image
from horde.request_scheduling import record_request_cancellation_forecast
from horde.utils import get_db_uuid, hash_dictionary
from horde.validation import ParamValidator
from horde.vars import horde_title

models = TextModels(api)
parsers = TextParsers()


TEXT_CEILING_PARAMETER_NAMES = ("max_length", "max_context_length", "n")
"""The params a text style's policy may cap. The range each ceiling may take comes from the params model."""


def text_style_contract_vocabulary() -> StyleContractVocabulary:
    """Return what a text style's parameter policy may talk about.

    The params model is the vocabulary clients already generate against, so anything outside it would
    be a key the request path never sees.

    Returns:
        The params a text style may hand over, and the ones it may cap with their ranges.
    """
    payload_fields = models.input_model_generation_payload.resolved
    return build_vocabulary(
        parameter_names=payload_fields,
        ceiling_bounds={
            parameter_name: ParameterCeilingBound(
                minimum=payload_fields[parameter_name].minimum,
                maximum=payload_fields[parameter_name].maximum,
            )
            for parameter_name in TEXT_CEILING_PARAMETER_NAMES
        },
    )


class TextAsyncGenerate(GenerateTemplate):
    gentype = "text"
    requires_chat_workers = False
    """Whether only workers that accept chat jobs can serve the request, which bounds how far its context grows."""
    decorators = [
        limiter.limit(
            limit_value=lim.get_request_90min_limit_per_ip,
            key_func=lim.get_request_path,
        ),
        limiter.limit(limit_value=lim.get_request_2sec_limit_per_ip, key_func=lim.get_request_path),
        limiter.limit(
            limit_value=lim.get_request_limit_per_apikey,
            key_func=lim.get_request_api_key,
        ),
    ]

    @api.expect(parsers.generate_parser, models.input_model_request_generation, validate=True)
    @api.marshal_with(
        models.response_model_async,
        code=202,
        description="Generation Queued",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(503, "Maintenance Mode", models.response_model_error)
    @api.response(429, "Too Many Prompts", models.response_model_error)
    def post(self):
        """Initiate an Asynchronous request to generate text.
        This endpoint will immediately return with the UUID of the request for generation.
        This endpoint will always be accepted, even if there are no workers available currently to fulfill this request.
        Perhaps some will appear in the next 20 minutes.
        Asynchronous requests live for 20 minutes before being considered stale and being deleted.
        """
        return self.queue_generation()

    def parse_request_args(self):
        """Return the parsed request, in the shape the generate path reads.

        Returns:
            ParseResult: The request arguments.
        """
        return parsers.generate_parser.parse_args()

    def queue_generation(self):
        """Validate the request and queue it, or price it when it is a dry run.

        Returns:
            tuple: The response body and status code.
        """
        self.args = self.parse_request_args()
        # Parsed here rather than in validate(), which a dry run answered from the kudos cache never
        # reaches: the cache cannot answer for a request that sizes its own context.
        self.context_fit = parse_context_fit(self.args.context_fit)
        try:
            super().post()
        except KeyError as e:
            logger.error("caught missing Key.")
            logger.error(self.args)
            logger.error(self.args.params)
            raise e
            return {"message": "Internal Server Error"}, 500
        if self.args.dry_run:
            ret_dict = {"kudos": round(self.kudos), "resolved": self.get_resolved_request()}
            return ret_dict, 200
        ret_dict = {
            "id": self.wp.id,
            "kudos": round(self.kudos),
        }
        if not database.check_new_request_has_valid_workers(self.wp) and not settings.mode_raid():
            ret_dict["message"] = self.get_size_too_big_message()
        return (ret_dict, 202)

    def get_waiting_prompt_arguments(self):
        """Return the arguments the request's TextWaitingPrompt is created with.

        Returns:
            dict: Keyword arguments for TextWaitingPrompt.
        """
        return {
            "worker_ids": self.workers,
            "models": self.models,
            "prompt": self.prompt,
            "submitted_prompt": self.submitted_prompt,
            "user_id": self.user.id,
            "params": self.params,
            "softprompt": self.args.softprompt,
            "trusted_workers": self.args.trusted_workers,
            "validated_backends": self.args.validated_backends,
            "worker_blacklist": self.args.worker_blacklist,
            "slow_workers": self.args.slow_workers,
            "ipaddr": self.user_ip,
            "safe_ip": True,
            "client_agent": self.args["Client-Agent"],
            "sharedkey_id": self.sharedkey.id if self.sharedkey else None,
            "style_id": self.existing_style.id if self.existing_style is not None else None,
            "proxied_account": self.args["proxied_account"],
            "webhook": self.args.webhook,
        }

    def initiate_waiting_prompt(self):
        self.wp = TextWaitingPrompt(**self.get_waiting_prompt_arguments())
        self.upload_source_images_or_delete_waiting_prompt()
        _, total_threads = database.count_active_workers("text")
        highest_multiplier = 0
        if len(self.models) == 0:
            required_kudos = 20 * self.wp.n
        else:
            # We find the highest multiplier to avoid someone gaming the system by requesting
            # a small model along with a big model.
            for model in self.models:
                model_multiplier = model_reference.get_text_model_multiplier(model)
                if model_multiplier > highest_multiplier:
                    highest_multiplier = model_multiplier
            required_kudos = round(self.wp.max_length * highest_multiplier / 21, 2) * self.wp.n
        needs_kudos, tokens, disable_downgrade = self.wp.require_upfront_kudos(database.retrieve_totals(), total_threads)
        if self.sharedkey and self.sharedkey.kudos != -1 and required_kudos > self.sharedkey.kudos:
            if self.args.allow_downgrade and not disable_downgrade:
                self.downgrade_wp_priority = True
            else:
                self.wp.delete()
                raise e.KudosUpfront(
                    required_kudos,
                    self.username,
                    message=f"This shared key does not have enough remaining kudos ({self.sharedkey.kudos}) "
                    f"to fulfill this reques ({required_kudos}).",
                    rc="SharedKeyInsufficientKudos",
                )
        if needs_kudos and required_kudos > 0:
            reservation = reserve_kudos(
                self.user,
                required_kudos,
                business_id=f"upfront:{self.wp.id}",
            )
            if reservation is None:
                if self.args.allow_downgrade and not disable_downgrade:
                    self.wp.downgrade(tokens)
                else:
                    self.wp.delete()
                    raise e.KudosUpfront(
                        required_kudos,
                        self.username,
                        message=f"Due to heavy demand, for requests over {tokens} tokens, "
                        "the client needs to already have the required kudos. "
                        f"This request requires {required_kudos} kudos to fulfil.",
                    )

        if self.sharedkey:
            is_in_limit, fail_message = self.sharedkey.is_job_within_limits(
                text_tokens=self.wp.max_length,
            )
            if not is_in_limit:
                # If we are using the shared key assigned to a style, then we bypass the shared key requirements
                # since its owner explicitly allowed to be used with a style exceeding them
                if not (self.existing_style and self.existing_style.sharedkey and self.existing_style.sharedkey.id == self.sharedkey.id):
                    self.wp.delete()
                    raise e.BadRequest(fail_message)

    def get_size_too_big_message(self):
        return (
            "Warning: No available workers can fulfill this request. It will expire in 20 minutes. "
            "Consider reducing the amount of tokens to generate."
        )

    def dry_run_answerable_from_cache(self):
        """Report whether a cached quote can answer this dry run.

        Returns:
            bool: Whether the cached quote is the whole answer. A request that sizes its context
                against its prompt is not answerable from a key built out of the params.
        """
        return super().dry_run_answerable_from_cache() and self.context_fit is ContextFit.IGNORE

    def get_resolved_request(self):
        """Return the request as the horde resolved it, with what only a text request carries.

        Returns:
            dict: The shared keys, plus the prompt's estimated token count and the context sizing
                that was applied.
        """
        resolved_request = super().get_resolved_request()
        resolved_request["estimated_prompt_tokens"] = estimate_prompt_tokens(self.prompt)
        resolved_request["context_fit"] = self.context_fit.value
        return resolved_request

    def validate(self):
        self.prompt = self.args.prompt
        self.apikey = self.args.apikey
        self.apply_style()
        self.apply_context_fit()
        super().validate()
        self.decide_style_surcharge()
        self.report_style_application()
        param_validator = ParamValidator(self.prompt, self.args.models, self.params, self.user)
        self.warnings = param_validator.validate_text_params()
        if self.args.extra_source_images is not None and len(self.args.extra_source_images) > 0:
            raise e.BadRequest("This request type does not accept extra source images.", rc="InvalidExtraSourceImages")

    def get_hashed_params_dict(self):
        gen_payload = self.params.copy()
        ## IMPORTANT: When adjusting this, also adjust TextWaitingPrompt.calculate_kudos()
        # We need to also use the model list into our hash, as our kudos calculation is based on whichever model is first
        gen_payload["models"] = self.args.models
        # The keys match GenerateTemplate.get_hashed_params_dict, except that the models are read
        # from the request args: apply_style replaces self.models with the style's models after the
        # pre-validate cache lookup, so hashing self.models would key the lookup and the store apart.
        gen_payload["extra_source_images"] = self.get_extra_source_images_count()
        # The style surcharge is in the quote too, and whether a style applied is not visible in
        # the params themselves. The hash runs both at the pre-validate cache lookup and at the
        # post-validate store, so it reads the request arg; style_kudos is still False at lookup
        # time.
        gen_payload["styled"] = self.args.style is not None
        params_hash = hash_dictionary(gen_payload)
        # logger.debug([params_hash,gen_payload])
        return params_hash

    def style_contract_vocabulary(self) -> StyleContractVocabulary:
        """Return what a text style's parameter policy may talk about.

        Returns:
            The text vocabulary, which a stored policy is validated against when the style is applied.
        """
        return text_style_contract_vocabulary()

    def apply_style(self):
        if self.args.style is None:
            return
        # The super() ensures the common parts of applying a style
        super().apply_style()
        if self.existing_style.style_type != "text":
            raise e.BadRequest("Image styles cannot be used on text requests", rc="StyleMismatch")
        self.models = self.existing_style.get_model_names()
        resolved_template_fields = self.apply_style_contract(self.existing_style)
        self.literal_template_fields = count_literal_text_template_fields(self.existing_style.prompt)
        self.prompt = format_text_style_prompt(
            template=self.existing_style.prompt,
            prompt=self.prompt,
            field_values=resolved_template_fields.values,
        )
        self.nsfw = self.existing_style.nsfw
        self.existing_style.use_count += 1
        db.session.commit()
        logger.debug(f"Style '{self.args.style}' applied.")

    def apply_context_fit(self):
        """Size the request's context against the prompt that will actually be sent.

        Runs after any style has been applied, since a style's template changes how long the prompt
        is. Under ``ignore`` an over-long prompt is sent unchanged and the worker cuts the front of it
        away, which is how every text request has behaved.
        """
        max_length = self.params.get(MAX_LENGTH_PARAMETER, DEFAULT_MAX_LENGTH)
        max_context_length = self.params.get(MAX_CONTEXT_LENGTH_PARAMETER, DEFAULT_MAX_CONTEXT_LENGTH)
        estimated_prompt_tokens = estimate_prompt_tokens(self.prompt)
        # Checked first so a prompt that already fits does not query the worker pool.
        prompt_fits = prompt_fits_context(
            estimated_prompt_tokens=estimated_prompt_tokens,
            max_length=max_length,
            max_context_length=max_context_length,
        )
        if prompt_fits:
            self.count_context_fit(ContextFitOutcome.FITS)
            return
        if self.context_fit is ContextFit.IGNORE:
            self.count_context_fit(ContextFitOutcome.SENT_OVERLONG)
            return

        try:
            fitted_context_length = fit_context_length(
                estimated_prompt_tokens=estimated_prompt_tokens,
                max_length=max_length,
                max_context_length=max_context_length,
                context_fit=self.context_fit,
                upper_bound=self.get_context_growth_upper_bound(requested_max_context_length=max_context_length),
            )
        except e.BadRequest:
            self.count_context_fit(ContextFitOutcome.REFUSED)
            raise
        if fitted_context_length == max_context_length:
            self.count_context_fit(ContextFitOutcome.FITS)
            return
        self.count_context_fit(ContextFitOutcome.GROWN)

        # Copied before writing, since self.params may still be the parsed request body; the quote,
        # the hash and the waiting prompt all read the params back off self.
        self.params = dict(self.params)
        self.params[MAX_CONTEXT_LENGTH_PARAMETER] = fitted_context_length
        logger.debug(f"Context grown from {max_context_length} to {fitted_context_length} to fit the prompt.")

    def count_context_fit(self, outcome: ContextFitOutcome) -> None:
        """Count what sizing this request against its prompt came to.

        Args:
            outcome: The result of the sizing.
        """
        metrics.text_context_fit.add(
            1,
            {
                "horde.context_fit.mode": self.context_fit.value,
                "horde.context_fit.outcome": outcome.value,
            },
        )

    def get_context_growth_upper_bound(self, *, requested_max_context_length):
        """Return the largest context this request may grow to.

        Args:
            requested_max_context_length (int): The context the request set before any growth, which
                is also the limit when no online worker serves the request's models.

        Returns:
            int: The limit, which only growth is held to.
        """
        if self.context_fit is not ContextFit.GROW:
            return MAX_CONTEXT_LENGTH_LIMIT

        return context_growth_upper_bound(
            policy=self.style_parameter_policy,
            highest_worker_max_context_length=database.get_highest_text_worker_max_context_length(
                self.models,
                accepts_chat_jobs=self.requires_chat_workers,
            ),
            requested_max_context_length=requested_max_context_length,
        )


def add_impossible_reason(wp_status: dict[str, object], worker_availability: database.RequestWorkerAvailability) -> None:
    """Add ``impossible_reason`` to a status the request cannot progress under, when its cause is a declaration gap.

    A client can refuse at once on a gap and keep waiting on any other cause.

    Args:
        wp_status: The request's status, changed in place.
        worker_availability: The eligibility the status was built from.
    """
    if not wp_status["is_possible"] and worker_availability.impossible_reason is not None:
        wp_status["impossible_reason"] = worker_availability.impossible_reason.model_dump(mode="json")


class TextAsyncStatus(Resource):
    chat_requests = False
    """Whether this route serves chat requests. Each family's routes report the other family's requests as not found."""
    request_label = "Text Waiting Prompt"
    """How a request of this route's family is named in a not-found error."""

    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    # If I marshal it here, it overrides the marshalling of the child class unfortunately
    decorators = [limiter.limit("60/minute", key_func=lim.get_request_path)]

    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_wp_status_full,
        code=200,
        description="Async Request Full Status",
    )
    @api.response(404, "Request Not found", models.response_model_error)
    def get(self, id=""):
        """Retrieve the full status of an Asynchronous generation request.
        This request will include all already generated texts.
        """
        self.args = self.get_parser.parse_args()
        return self.get_request_status(id)

    def find_request(self, request_id, operation):
        """Return the request this route's family holds under ``request_id``.

        Args:
            request_id (str): The request id from the path.
            operation (str): The operation named in a not-found error.

        Returns:
            TextWaitingPrompt: The request.

        Raises:
            horde.exceptions.RequestNotFound: If this family holds no request with the id.
        """
        wp = text_database.get_text_wp_by_id(request_id, chat=self.chat_requests)
        if not wp:
            raise e.RequestNotFound(
                request_id,
                request_type=f"{self.request_label} ({operation})",
                client_agent=self.args["Client-Agent"],
                ipaddr=request.remote_addr,
            )
        return wp

    def get_request_status(self, request_id):
        """Return the request's full status.

        Args:
            request_id (str): The request id from the path.

        Returns:
            tuple: The status and the status code.
        """
        wp = self.find_request(request_id, "Status")
        worker_availability = database.get_worker_availability_for_request(wp)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("text"),
            has_valid_workers=worker_availability.is_possible,
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers("text"),
            eligible_workers=worker_availability.worker_count,
            eligible_worker_threads=worker_availability.thread_count,
            might_stall=worker_availability.might_stall,
        )
        add_impossible_reason(wp_status, worker_availability)
        # wp_status is a fully materialized plain dict; release the pooled
        # connection before flask_restx marshalling/JSON serialization and the
        # response write, so it is not held across non-DB work.
        db.session.remove()
        return (wp_status, 200)

    delete_parser = reqparse.RequestParser()
    delete_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    @api.expect(delete_parser)
    @api.marshal_with(
        models.response_model_wp_status_full,
        code=200,
        description="Async Request Full Status",
    )
    @api.response(404, "Request Not found", models.response_model_error)
    def delete(self, id=""):
        """Cancel an unfinished request.
        This request will include all already generated texts.
        """
        self.args = self.delete_parser.parse_args()
        return self.cancel_request(id)

    def cancel_request(self, request_id):
        """Cancel the request and return its full status.

        Args:
            request_id (str): The request id from the path.

        Returns:
            tuple: The status as it was before cancelling, and the status code.
        """
        wp = self.find_request(request_id, "Delete")
        worker_availability = database.get_worker_availability_for_request(wp)
        wp_status = wp.get_status(
            request_avg=database.get_request_avg("text"),
            has_valid_workers=worker_availability.is_possible,
            wp_queue_stats=database.get_wp_queue_stats(wp),
            active_worker_count=database.count_active_workers("text"),
            eligible_workers=worker_availability.worker_count,
            eligible_worker_threads=worker_availability.thread_count,
            might_stall=worker_availability.might_stall,
        )
        add_impossible_reason(wp_status, worker_availability)
        logger.info(f"Request with ID {wp.id} has been cancelled.")
        # FIXME: I pevent it at the moment due to the race conditions
        # The WPCleaner is going to clean it up anyway
        cancelled_at = datetime.utcnow()
        cancellation_claimed = wp.claim_terminal_outcome(
            RequestTerminalOutcome.CANCELLED,
            recorded_at=cancelled_at,
            commit=False,
        )
        wp.n = 0
        if not commit_request_cancellation(wp):
            return (wp_status, 200)
        if cancellation_claimed:
            record_request_cancellation_forecast(request_id=str(wp.id), cancelled_at=cancelled_at)
            if wp.is_chat:
                publish_chat_event(str(wp.id), ChatEvent.CANCELLED, wp.count_stored_generations())
        return (wp_status, 200)


class TextAsyncRequest(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "apikey",
        type=str,
        required=True,
        help="The API key which submitted the request.",
        location="headers",
    )
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    decorators = [limiter.limit("60/minute", key_func=lim.get_request_path)]

    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_submitted_request,
        code=200,
        description="Submitted Request Parameters",
        skip_none=True,
    )
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Request Not found", models.response_model_error)
    def get(self, id=""):
        """Retrieve the parameters of a submitted asynchronous generation request.

        Unlike the status and check endpoints, the request ID alone is not enough: the `apikey` header
        must be the key used to submit the request.

        The response has the same shape as the generation input, holding the values the horde recorded
        after filling in parameter defaults. The prompt is the original submission, before styles or
        moderation, and is omitted for legacy rows. Styled requests are not an exact replay.
        """
        self.args = self.get_parser.parse_args()
        wp = text_database.get_text_wp_by_id(id)
        if not wp:
            raise e.RequestNotFound(
                id,
                request_type="Text Waiting Prompt (Parameters)",
                client_agent=self.args["Client-Agent"],
                ipaddr=request.remote_addr,
            )
        assert_submitting_key(self.args["apikey"], wp)
        submitted_request = wp.get_submitted_request()
        # The dict is fully materialized; release the pooled connection before
        # marshalling and the response write, as the status endpoints do.
        db.session.remove()
        return (submitted_request, 200, {"Cache-Control": "private, no-store"})


def read_chat_request_body() -> object:
    """Return the decoded JSON body of a chat request, refusing one above the size limit before it is parsed.

    A body that states its length is refused on the ``Content-Length`` header alone. A chunked body, which states
    none, is read only up to one byte past the limit, so an oversized one is refused without being read whole.

    Returns:
        The decoded body, or None when it is not JSON or holds a non-JSON constant such as ``NaN``.

    Raises:
        horde.exceptions.RequestTooLarge: The body is larger than
            [`MAX_CHAT_BODY_BYTES`][horde.chat_contract.MAX_CHAT_BODY_BYTES].
    """
    too_large_message = f"A chat request body may be at most {MAX_CHAT_BODY_BYTES} bytes."
    if request.content_length is not None:
        if request.content_length > MAX_CHAT_BODY_BYTES:
            raise e.RequestTooLarge(too_large_message)
        body = request.get_data()
    else:
        body_chunks: list[bytes] = []
        body_size = 0
        while body_chunk := request.stream.read(MAX_CHAT_BODY_BYTES + 1 - body_size):
            body_chunks.append(body_chunk)
            body_size += len(body_chunk)
            if body_size > MAX_CHAT_BODY_BYTES:
                raise e.RequestTooLarge(too_large_message)
        body = b"".join(body_chunks)
    if not request.is_json:
        return None
    try:
        return json.loads(body, parse_constant=refuse_json_constant)
    except ValueError:
        return None


class ClaimedOperation(Exception):
    """Raised when a concurrent submission claimed the same operation key first, so this one returns that request."""

    def __init__(self, claim: ChatOperationClaim) -> None:
        super().__init__(claim.request_id)
        self.claim = claim
        """The request that holds the key."""


class ChatAsyncGenerate(TextAsyncGenerate):
    requires_chat_workers = True
    context_adjustments = None
    """The changes context fitting made to the request's context length, as stored for the chat status, or None."""
    chat_images: tuple[bytes, ...] = ()
    """The request's images, validated and encoded as the JPEG to store, in message order. Empty for a dry run,
    which neither fetches nor stores them."""
    chat_image_keys: tuple[str, ...] = ()
    """The object keys this submission stored images under, deleted again when activation fails."""
    chat_extras = None
    """The tools and response format the request applies, from ``build_chat_extras``, or None."""

    @api.expect(parsers.chat_generate_parser, models.input_model_chat_generation)
    @api.marshal_with(
        models.response_model_async,
        code=202,
        description="Chat Request Queued",
        skip_none=True,
    )
    @api.response(200, "Operation Key Already Claimed; the request that claimed it", models.response_model_async)
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(404, "Unknown Chat Model", models.response_model_error)
    @api.response(413, "Request Body Too Large", models.response_model_error)
    @api.response(503, "Maintenance Mode or Chat Admission Disabled", models.response_model_error)
    @api.response(429, "Too Many Prompts", models.response_model_error)
    def post(self):
        """Initiate an asynchronous chat request.

        The body is an OpenAI chat-completions request with text messages and image parts on user messages. Logprobs,
        audio and file content are not supported. An optional `horde` object holds the options that OpenAI does not
        define a key for.

        `tools`, `tool_choice`, `parallel_tool_calls` and `response_format` are stored and sent to the worker as
        the client sent them. A request offering tools, or whose history carries tool calls or tool messages, only
        goes to workers declaring the `tools` feature; a `json_object` or `json_schema` response format only to
        workers declaring that feature. Each image is a base64 data URL or an http(s) URL. The horde validates and
        stores each image and charges the extra source image tax for it. Only workers declaring the `vision` feature
        and an image transport serve a request with images.

        Refused with:

        - `UnsupportedChatParameter` (400): a key, value or content part outside the supported set, or a tool
          message that does not answer an earlier tool call.
        - `ChatRequestConflict` (400): a `tool_choice` other than `none` without tools, or one forcing a function
          that the tools do not define.
        - `RequestTooLarge` (413): a body above the size limit.
        - `SourceImageUrlRefused` (400): an image URL resolving or redirecting to a private address.
        - `SourceImageUrlInvalid`, `SourceImageUnreadable`, `SourceImageSizeExceeded` or
          `SourceImageResolutionExceeded` (400): an image that cannot be fetched or read, or is above the size or
          resolution limit.
        - `TooManyExtraSourceImages` (400): more images than a chat request can carry.
        - `MoreThanMinExtraSourceImage` (400): more images than the limit for users who are not trusted or patrons.
        - `UnknownChatModel` (404): a model that the text model reference does not list and that no online chat
          worker serves.
        - `OperationKeyConflict` (400): `horde.operation_key` reused with a changed request.
        - `ChatAdmissionDisabled` (503): the horde is not accepting new chat requests.
        - `ChatImageStorageUnavailable` (503): the request's images could not be stored.

        A request carrying `horde.operation_key` is claimed under that key for its user. Submitting the same key
        with the same request returns the first request's id and kudos with status 200 and does not queue a new
        request.

        The request is queued with text requests and served by the text workers that accept chat jobs. The
        response is the request's UUID; poll `/generate/chat/status` for the result.
        """
        self.chat_request = parse_chat_request(read_chat_request_body())
        self.operation_claim = self.get_operation_claim()
        # A repeated submission creates nothing, so it is answered even while admission is off: the caller
        # recovering it may not otherwise learn the id of a request that is queued and charged.
        if self.operation_claim is not None:
            existing_claim = self.find_existing_claim()
            if existing_claim is not None:
                return self.answer_with_claimed_request(existing_claim)
        if not Config.chat_admission_enabled:
            raise e.ServiceUnavailable(
                "This horde is not accepting new chat requests. Requests already queued are still served.",
                rc="ChatAdmissionDisabled",
            )
        try:
            return self.queue_generation()
        except ClaimedOperation as claimed_operation:
            return self.answer_with_claimed_request(claimed_operation.claim)

    def get_operation_claim(self):
        """Return the operation key and fingerprint this submission claims, or None when it claims none.

        Returns:
            tuple | None: The key and the fingerprint of the normalised request; None without a key and for a dry
                run, which quotes without claiming.
        """
        horde_options = self.chat_request.horde
        if horde_options.operation_key is None or horde_options.dry_run:
            return None
        return horde_options.operation_key, build_operation_fingerprint(self.chat_request)

    def find_existing_claim(self):
        """Return the request the submitting user already claimed this operation key with.

        Runs before validation, so a repeated submission is answered before the concurrency limit, which its own
        first submission counts towards, or any availability check. The user is resolved from the ``apikey``
        header as validation resolves it; a key that resolves to no user is left for validation to refuse.

        Returns:
            ChatOperationClaim | None: The claiming request, or None when there is none.
        """
        apikey = parsers.chat_generate_parser.parse_args()["apikey"]
        if not apikey:
            return None
        sharedkey = database.find_sharedkey(apikey)
        user = sharedkey.user if sharedkey is not None else database.find_user_by_api_key(apikey)
        if user is None:
            return None
        operation_key, _ = self.operation_claim
        return text_database.find_chat_operation_claim(user.id, operation_key)

    def answer_with_claimed_request(self, claim):
        """Return the request that holds this submission's operation key, when both carry the same request.

        Args:
            claim (ChatOperationClaim): The request that holds the key.

        Returns:
            tuple: The request's id and kudos, and status 200.

        Raises:
            horde.exceptions.BadRequest: With ``OperationKeyConflict`` when the key was claimed with another request.
        """
        _, operation_fingerprint = self.operation_claim
        if claim.fingerprint != operation_fingerprint:
            raise e.BadRequest(
                "This operation key was already used for a different chat request.",
                rc="OperationKeyConflict",
            )
        return {"id": claim.request_id, "kudos": round(claim.kudos)}, 200

    def parse_request_args(self):
        """Return the chat request mapped onto the arguments a text request parses to.

        Returns:
            ParseResult: The text-shaped request arguments, with the flattened messages as the prompt.

        Raises:
            horde.exceptions.BadRequest: If the mapped params are outside the text params' ranges.
        """
        header_args = parsers.chat_generate_parser.parse_args()
        chat_request = self.chat_request
        text_params = build_text_params(chat_request)
        # Checked against the text params model, so a chat request accepts exactly the ranges a text request does.
        models.input_model_generation_payload.validate(text_params, self.api.refresolver, self.api.format_checker)
        self.messages = serialize_messages(chat_request)
        self.chat_extras = build_chat_extras(chat_request)
        horde_options = chat_request.horde
        worker_ids, worker_blacklist = build_worker_targeting(chat_request)
        return ParseResult(
            {
                "apikey": header_args["apikey"],
                "Client-Agent": header_args["Client-Agent"],
                "prompt": flatten_messages(self.messages, self.chat_extras),
                "params": text_params,
                "models": build_model_names(chat_request),
                "softprompt": None,
                "context_fit": horde_options.context_fit.value,
                "trusted_workers": horde_options.trusted_workers,
                "validated_backends": horde_options.validated_backends,
                "workers": worker_ids,
                "worker_blacklist": worker_blacklist,
                "nsfw": True,
                "slow_workers": horde_options.slow_workers,
                "extra_slow_workers": False,
                "dry_run": horde_options.dry_run,
                "proxied_account": None,
                "disable_batching": False,
                "allow_downgrade": False,
                "webhook": None,
                "style": None,
                "template_fields": None,
                "extra_source_images": None,
            },
        )

    def validate(self):
        """Validate the request in the text order, with the model check between the shared checks and sizing.

        A text request sizes its context before the shared checks. A chat request runs the shared checks
        (maintenance, authentication, the concurrency limit, countermeasures and moderation) first, then the model
        check, then context sizing, so an unknown model is refused as unknown rather than as a context that cannot
        grow, and only after the refusals every other route gives first. A chat request carries no style, so the
        style steps only report that none applied.
        """
        self.prompt = self.args.prompt
        self.apikey = self.args.apikey
        GenerateTemplate.validate(self)
        self.ensure_model_known()
        self.apply_context_fit()
        self.decide_style_surcharge()
        self.report_style_application()
        param_validator = ParamValidator(self.prompt, self.args.models, self.params, self.user)
        self.warnings = param_validator.validate_text_params()
        self.mirror_prompt_rewrites_onto_messages()
        self.load_chat_images()

    def get_extra_source_images_count(self):
        """Return how many images the request carries, each of which is charged the extra source image tax.

        Returns:
            int: The number of image parts in the messages.
        """
        return len(list_image_urls(self.messages))

    def get_activation_kudos_adjustment(self):
        """Return the activation charge, with the extra source image tax for each image the request carries.

        The images are charged here rather than passed to activation as extra source images, which would store them
        on the request row and send them to workers as extra source images.

        Returns:
            float: The base adjustment plus the tax for every image.
        """
        return super().get_activation_kudos_adjustment() + EXTRA_SOURCE_IMAGE_HORDE_TAX * self.get_extra_source_images_count()

    def load_chat_images(self):
        """Fetch or decode every image, enforcing the count, size and resolution caps, and encode each as JPEG.

        Runs after every other check, so a refused request never fetches. A dry run counts its images for the quote
        and refuses too many, but fetches and decodes nothing.

        Raises:
            horde.exceptions.BadRequest: With ``MoreThanMinExtraSourceImage`` when a user who is neither trusted nor
                a patron sends more than one image, or an image validation failure for an image that cannot be
                fetched or read or breaks a cap.
        """
        image_urls = list_image_urls(self.messages)
        if len(image_urls) > MAX_CHAT_IMAGE_COUNT_UNTRUSTED and not self.user.trusted and not patrons.is_patron(self.user.id):
            raise e.BadRequest(
                f"Only trusted users and patrons can send more than {MAX_CHAT_IMAGE_COUNT_UNTRUSTED} image on a chat request.",
                rc="MoreThanMinExtraSourceImage",
            )
        self.chat_images = ()
        if self.args.dry_run:
            return
        self.chat_images = tuple(encode_chat_image(load_chat_image(image_url)) for image_url in image_urls)

    def upload_source_images(self):
        """Store the request's images under keys derived from its id, after its inactive row exists.

        The row's messages already reference these keys, so a request whose handling stops after this point leaves
        no image that its row's deletion does not remove.

        Raises:
            horde.exceptions.ServiceUnavailable: With ``ChatImageStorageUnavailable`` when the store refuses an image;
                the row and the images already stored are deleted first.
        """
        if not self.chat_images:
            return
        stored_keys: list[str] = []
        try:
            for ordinal, jpeg_bytes in enumerate(self.chat_images):
                image_key = build_chat_image_key(str(self.wp.id), ordinal)
                upload_chat_image(jpeg_bytes, image_key)
                stored_keys.append(image_key)
        except (BotoCoreError, ClientError) as err:
            logger.warning(f"Could not store the images of chat request {self.wp.id}: {err}")
            self.wp.delete()
            raise e.ServiceUnavailable(
                "The chat request's images could not be stored. Try again later.",
                rc="ChatImageStorageUnavailable",
            ) from err
        self.chat_image_keys = tuple(stored_keys)

    def ensure_model_known(self):
        """Refuse a model this horde does not know before any request row exists or any kudos is charged.

        A model is known when the text model reference lists it or an online worker serves chat jobs with it. A
        known model that no online worker can serve is still accepted and waits, as a text request does.

        Raises:
            horde.exceptions.UnknownChatModel: The model is unknown to both.
        """
        model_name = self.chat_request.model
        # The reference is in memory, so the worker table is only queried for a model it does not list.
        if model_reference.is_known_text_model(model_name) or text_database.is_chat_model_served(model_name):
            return
        raise e.UnknownChatModel(model_name)

    def apply_context_fit(self):
        """Size the request's context as a text request's, and record a change for the chat status."""
        requested_max_context_length = self.params.get(MAX_CONTEXT_LENGTH_PARAMETER, DEFAULT_MAX_CONTEXT_LENGTH)
        super().apply_context_fit()
        fitted_max_context_length = self.params.get(MAX_CONTEXT_LENGTH_PARAMETER, DEFAULT_MAX_CONTEXT_LENGTH)
        self.context_adjustments = None
        # Only growth changes the context length; a request that does not fit under reject is refused instead.
        if fitted_max_context_length != requested_max_context_length:
            self.context_adjustments = [
                {
                    "from": requested_max_context_length,
                    "to": fitted_max_context_length,
                    "reason": ChatContextFit.GROW.value,
                },
            ]

    def activate_waiting_prompt(self):
        """Activate the request, deleting the images this submission stored when activation fails.

        An inactive row a failed activation leaves behind then references no stored image.

        Raises:
            ClaimedOperation: Another submission claimed the key first.
        """
        try:
            self.activate_claiming_operation_key()
        except ClaimedOperation:
            # The losing row was deleted, and its images with it.
            raise
        except Exception:
            delete_stored_chat_images(self.chat_image_keys)
            raise

    def activate_claiming_operation_key(self):
        """Activate the request, claiming its operation key in the activation's transaction when it carries one.

        Two submissions with one key can both pass the lookup in ``post``. The unique index lets only one
        activation commit; the other's transaction rolls back with its tax, its row and its upfront kudos hold
        are removed, and it returns the request that won.

        Raises:
            ClaimedOperation: Another submission claimed the key first.
        """
        if self.operation_claim is None:
            super().activate_waiting_prompt()
            return

        user_id = self.user.id
        self.wp.operation_claim = self.operation_claim
        try:
            super().activate_waiting_prompt()
        except IntegrityError:
            db.session.rollback()
            operation_key, _ = self.operation_claim
            existing_claim = text_database.find_chat_operation_claim(user_id, operation_key)
            if existing_claim is None:
                raise
            self.wp.delete()
            raise ClaimedOperation(existing_claim) from None

    def mirror_prompt_rewrites_onto_messages(self):
        """Apply validation's rewrite of the flattened prompt to every message text.

        The flattened prompt is an estimate only and is never sent to a worker, so every rewrite validation
        makes to it has to be mirrored onto the messages, or the worker receives text that was not validated.
        The only rewrite text validation makes is the replacement filter applied for flagged users and NSFW
        models; the same filter is applied to each text, and the prompt is flattened again from the result.
        """
        if self.prompt == flatten_messages(self.messages, self.chat_extras):
            return

        self.messages = rewrite_message_texts(
            self.messages,
            lambda message_text: prompt_checker.nsfw_model_prompt_replace(message_text, self.models),
        )
        self.prompt = flatten_messages(self.messages, self.chat_extras)

    def get_waiting_prompt_arguments(self):
        """Return the arguments the chat request's TextWaitingPrompt is created with.

        Returns:
            dict: Keyword arguments for TextWaitingPrompt, marking it a chat request carrying the messages.
        """
        waiting_prompt_arguments = super().get_waiting_prompt_arguments()
        waiting_prompt_arguments["is_chat"] = True
        waiting_prompt_arguments["messages"] = self.messages
        waiting_prompt_arguments["chat_extras"] = self.chat_extras
        waiting_prompt_arguments["chat_conformance"] = self.chat_request.horde.conformance.value
        waiting_prompt_arguments["context_adjustments"] = self.context_adjustments
        if list_image_urls(self.messages):
            # The id is chosen here so the row is written with each image already replaced by the reference to the
            # key it is stored under; the submitted image bytes and URLs never reach the database.
            request_id = get_db_uuid()
            waiting_prompt_arguments["id"] = request_id
            waiting_prompt_arguments["messages"] = replace_image_urls(self.messages, str(request_id))
        return waiting_prompt_arguments

    def get_size_too_big_message(self):
        return (
            "Warning: No available worker that accepts chat jobs can fulfill this request. "
            "It will expire in 20 minutes. Consider reducing the amount of tokens to generate."
        )


class ChatAsyncStatus(TextAsyncStatus):
    chat_requests = True
    request_label = "Chat Waiting Prompt"

    @api.expect(TextAsyncStatus.get_parser)
    @api.marshal_with(
        models.response_model_chat_status_full,
        code=200,
        description="Chat Request Full Status",
    )
    @api.response(404, "Request Not found", models.response_model_error)
    def get(self, id=""):
        """Retrieve the full status of a chat request, with every finished generation.

        Each generation carries the assistant message, the finish reasons, the token counts the worker reported
        (null when it did not report them), the charge basis, and how long the request queued before the generation
        was assigned to a worker.

        `context_adjustments` lists the changes the horde made to the request's context length. When the workers'
        declarations are the reason no worker can take the request, `impossible_reason` lists the declarations they
        lack and the limits the request exceeds. Once the request faults, `fault_reason` and `fault_rc` hold the
        message and return code of the last refused reply or worker fault.

        A text request's id returns 404 here; poll `/generate/text/status` for text requests.
        """
        self.args = self.get_parser.parse_args()
        return self.get_request_status(id)

    @api.expect(TextAsyncStatus.delete_parser)
    @api.marshal_with(
        models.response_model_chat_status_full,
        code=200,
        description="Chat Request Full Status",
    )
    @api.response(404, "Request Not found", models.response_model_error)
    def delete(self, id=""):
        """Cancel an unfinished chat request.

        The response is the request's full status from before the cancellation, with every generation already
        finished. A text request's id returns 404 here.
        """
        self.args = self.delete_parser.parse_args()
        return self.cancel_request(id)


class TextJobPop(JobPopTemplate):
    worker_class = TextWorker
    gentype = "text"
    decorators = [limiter.limit("60/second")]

    @api.expect(parsers.job_pop_parser, models.input_model_job_pop, validate=True)
    @api.response(200, "Generation Popped", models.response_model_job_pop)
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def post(self):
        """Check if there are generation requests queued for fulfillment.

        This endpoint is used by registered workers only.

        A worker that sets `chat_jobs` also receives chat jobs. A chat job's response carries `chat: true` and a
        payload with `messages` in place of a prompt (the `GenerationPayloadChat` model). A worker that also sets
        `prompt_jobs` to false receives only chat jobs.

        Refused with `ChatDeclarationInvalid` (400) when `chat_jobs` is set without `chat_limits` or `model_id`,
        `model_id` is not one of `models`, `chat_limits.tools_max_temperature` is negative or not a number, or
        `prompt_jobs` is false without `chat_jobs`.
        """
        # Splitting the post to its own function so that I can have the decorators of post on each extended class
        # Without copying the whole post() code
        self.args = parsers.job_pop_parser.parse_args()
        self.validate_chat_declaration()
        pop_response, status_code = super().post()
        # A text job's response is marshalled by the text model alone, so it never carries the chat key.
        response_model = models.response_model_chat_job_pop if pop_response.get("chat") else models.response_model_job_pop
        return marshal_with(response_model)(lambda: (pop_response, status_code))()

    def validate_chat_declaration(self):
        """Refuse a pop whose chat declaration is incomplete or names a model it cannot serve chat jobs with.

        Raises:
            horde.exceptions.BadRequest: With ``ChatDeclarationInvalid`` when ``chat_jobs`` is set without
                ``chat_limits`` or ``model_id``, ``chat_limits.tools_max_temperature`` is set and is not a
                non-negative number, ``model_id`` is not one of ``models``, or ``prompt_jobs`` is false without
                ``chat_jobs``.
        """
        if not self.args.chat_jobs:
            if self.args.prompt_jobs is False:
                raise e.BadRequest(
                    "A worker that sets prompt_jobs to false has to set chat_jobs, or it can receive no job.",
                    rc="ChatDeclarationInvalid",
                )
            return
        chat_limits = self.args.chat_limits or {}
        if not isinstance(chat_limits.get("max_context_length"), int) or not isinstance(chat_limits.get("max_length"), int):
            raise e.BadRequest(
                "A worker that sets chat_jobs has to send chat_limits with max_context_length and max_length.",
                rc="ChatDeclarationInvalid",
            )
        tools_max_temperature = chat_limits.get(ChatLimitField.TOOLS_MAX_TEMPERATURE.value)
        if tools_max_temperature is not None and (
            isinstance(tools_max_temperature, bool)
            or not isinstance(tools_max_temperature, int | float)
            or not math.isfinite(tools_max_temperature)
            or tools_max_temperature < 0
        ):
            raise e.BadRequest(
                "chat_limits.tools_max_temperature has to be a non-negative number when it is set.",
                rc="ChatDeclarationInvalid",
            )
        model_id = self.args.model_id
        if not model_id:
            raise e.BadRequest("A worker that sets chat_jobs has to send model_id.", rc="ChatDeclarationInvalid")
        if model_id not in (self.args.models or []):
            raise e.BadRequest(f"model_id '{model_id}' has to be one of the worker's models.", rc="ChatDeclarationInvalid")

    def check_in(self):
        self.softprompts = []
        if self.args.softprompts:
            self.softprompts = self.args.softprompts
        models = self.models
        chat_limits = self.args.chat_limits or {}
        self.worker.check_in(
            self.args["max_length"],
            self.args["max_context_length"],
            self.softprompts,
            models=models,
            nsfw=self.args.nsfw,
            safe_ip=self.safe_ip,
            ipaddr=self.worker_ip,
            threads=self.args.threads,
            bridge_agent=self.args.bridge_agent,
            accepts_chat_jobs=self.args.chat_jobs,
            # JSON null leaves the default, as an absent key does.
            accepts_prompt_jobs=self.args.prompt_jobs is not False,
            chat_features=self.args.chat_features,
            chat_max_context_length=chat_limits.get("max_context_length"),
            chat_max_length=chat_limits.get("max_length"),
            chat_tools_max_temperature=chat_limits.get(ChatLimitField.TOOLS_MAX_TEMPERATURE.value),
            chat_model_id=self.args.model_id,
            image_transports=self.args.image_transports,
        )

    def get_sorted_wp(self, priority_user_ids=None):
        """We're sending the lists directly, to avoid having to join tables"""
        sorted_wps = text_database.get_sorted_text_wp_filtered_to_worker(
            self.worker,
            self.models,
            priority_user_ids=priority_user_ids,
            page=self.wp_page,
        )
        return sorted_wps


CHAT_SUBMIT_KEYS = (
    "message",
    "model_id",
    "finish_reason",
    "fault_reason",
)
"""Keys only a chat job's submit carries. A body with any of them is parsed without requiring ``generation``."""


class TextJobSubmit(JobSubmitTemplate):
    gentype = "text"
    decorators = [limiter.limit("60/second")]
    dropped_tool_call_count = 0
    """The tool calls verification dropped from a chat reply under ``parallel_tool_calls: false``, for the submit
    log; zero for every other submit."""

    @api.expect(parsers.job_submit_parser, models.input_model_job_submit, validate=True)
    @api.marshal_with(models.response_model_job_submit, code=200, description="Generation Submitted")
    @api.response(400, "Generation Already Submitted", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Request Not Found", models.response_model_error)
    def post(self):
        """Submit generated text.

        This endpoint is used by registered workers only.

        A chat job is submitted with `message`, `finish_reason` and `model_id` in place of `generation`. A faulted
        chat job is submitted with `state` set to `faulted` and does not need a `message`; an optional
        `fault_reason` says why the worker faulted it.

        Refused with:

        - `ChatSubmitInvalid` (400): a chat submission without a valid `message` or `finish_reason`, a reply that
          calls a function the request did not offer or repeats a call id, or a text job's submission carrying
          `message`.
        - `ChatSubmitNonconformant` (400): a reply that a `strict` request's `tool_choice` or `response_format`
          rules out.
        - `ChatModelMismatch` (400): a `model_id` other than the model the job was assigned.

        A refused reply's message and return code are stored on the request. Once the request faults, its status
        reports them as `fault_reason` and `fault_rc`.
        """
        # We have to parse the args here, to ensure we use the correct parser class
        submit_body = request.get_json(silent=True)
        is_chat_submit = isinstance(submit_body, dict) and any(chat_key in submit_body for chat_key in CHAT_SUBMIT_KEYS)
        submit_parser = parsers.chat_job_submit_parser if is_chat_submit else parsers.job_submit_parser
        self.args = submit_parser.parse_args()
        return super().post()

    def get_progen(self):
        """Set to its own function to it can be overwritten depending on the class"""
        return text_database.get_text_progen_by_id(self.args["id"])

    def set_generation(self):
        """Check a chat job's structured submission, then store the generation.

        For a chat job, the message content becomes the ``generation`` string, an empty string when the content is
        null, written in the same update as the message itself, so every predicate that reads ``generation`` treats
        chat and text alike.

        Raises:
            horde.exceptions.BadRequest: With ``ChatSubmitInvalid`` when a chat job's submission lacks a valid
                message or finish reason, calls a function the request did not offer or repeats a call id, or a text
                job's carries a message; ``ChatSubmitNonconformant`` when a ``strict`` request's ``tool_choice`` or
                ``response_format`` rules the reply out; ``ChatModelMismatch`` when a chat job's ``model_id`` is not
                the model it was assigned.
        """
        if self.procgen.wp.is_chat:
            self.prepare_chat_submission()
        elif self.args.message is not None:
            raise e.BadRequest("'message' is only accepted for chat jobs; send 'generation'.", rc="ChatSubmitInvalid")
        elif self.args.generation is None:
            raise e.BadRequest("'generation' is required for text jobs.")
        super().set_generation()

    def prepare_chat_submission(self):
        """Validate a chat job's submission and store its message content as the generation.

        A faulted submission needs no message; it is handled as any faulted text submission, after
        [`record_chat_worker_fault`][horde.apis.v2.kobold.TextJobSubmit.record_chat_worker_fault] stores the reason it
        carries. A refused reply's message and return code are stored by
        [`record_chat_refusal`][horde.apis.v2.kobold.TextJobSubmit.record_chat_refusal] before the refusal is sent.

        Raises:
            horde.exceptions.BadRequest: With ``ChatSubmitInvalid``, ``ChatSubmitNonconformant`` or
                ``ChatModelMismatch``.
        """
        if self.args.state == "faulted":
            self.chat_message = None
            self.args["generation"] = self.args.generation or ""
            self.record_chat_worker_fault()
            return
        try:
            self.verify_chat_submission()
        except e.BadRequest as refusal:
            self.record_chat_refusal(refusal)
            raise

    def record_chat_refusal(self, refusal: e.BadRequest) -> None:
        """Store a refused chat reply's message and return code on its request and generation.

        A failed write is logged and the refusal is still sent, since the worker needs the refusal more than the
        client needs the reason.

        Args:
            refusal: The refusal the submit is answered with.
        """
        try:
            self.procgen.wp.record_chat_refusal(self.procgen, refusal.specific, refusal.rc)
        except SQLAlchemyError as write_error:
            db.session.rollback()
            logger.warning(f"Unable to store the refusal of chat generation {self.procgen.id}: {write_error}")

    def record_chat_worker_fault(self) -> None:
        """Store the Horde's message for the ``fault_reason`` of a faulted chat submit on its request.

        A missing or unknown reason stores nothing, so a worker that sends no reason or one this Horde does not know
        is handled as before. It is stored before the fault is applied, so the request status and the client signal
        of a fault past the limit carry it. A failed write is logged and the fault is still applied.
        """
        fault_message = chat_worker_fault_message(self.args.fault_reason)
        if fault_message is None:
            return
        try:
            self.procgen.wp.record_chat_worker_fault(self.procgen, fault_message, CHAT_WORKER_FAULT_RC)
        except SQLAlchemyError as write_error:
            db.session.rollback()
            logger.warning(f"Unable to store the fault reason of chat generation {self.procgen.id}: {write_error}")

    def verify_chat_submission(self):
        """Verify a chat job's reply and store its message content as the generation.

        The reply is verified against the request it answers by
        [`verify_chat_reply`][horde.classes.kobold.chat_request.verify_chat_reply], and the reply it returns is the
        one stored, with the finish reason from
        [`chat_reply_finish_reason`][horde.classes.kobold.chat_request.chat_reply_finish_reason].

        Raises:
            horde.exceptions.BadRequest: With ``ChatSubmitInvalid``, ``ChatSubmitNonconformant`` or
                ``ChatModelMismatch``.
        """
        if self.args.message is None or self.args.generation is not None:
            raise e.BadRequest("A chat job is submitted with 'message' in place of 'generation'.", rc="ChatSubmitInvalid")
        try:
            chat_message = ChatSubmittedMessage.model_validate(self.args.message)
        except ValidationError as validation_error:
            raise e.BadRequest(
                f"'message' is not a valid assistant message: {validation_error.errors()[0]['msg']}.",
                rc="ChatSubmitInvalid",
            ) from None
        if self.args.finish_reason is None:
            raise e.BadRequest("A chat job's submission needs 'finish_reason'.", rc="ChatSubmitInvalid")
        # The other direction is not enforced: a named tool_choice finishes with 'stop' and its call under OpenAI.
        if self.args.finish_reason == ChatFinishReason.TOOL_CALLS and not chat_message.tool_calls:
            raise e.BadRequest("A 'tool_calls' finish reason needs 'message.tool_calls'.", rc="ChatSubmitInvalid")
        native_finish_reason = self.args.native_finish_reason
        if native_finish_reason is not None:
            native_finish_reason = native_finish_reason.replace("\x00", "\ufffd")
            self.args["native_finish_reason"] = native_finish_reason
        if native_finish_reason is not None and len(native_finish_reason) > MAX_NATIVE_FINISH_REASON_LENGTH:
            raise e.BadRequest(
                f"'native_finish_reason' cannot be longer than {MAX_NATIVE_FINISH_REASON_LENGTH} characters.",
                rc="ChatSubmitInvalid",
            )
        if self.args.model_id != self.procgen.assigned_model:
            raise e.BadRequest(
                f"'model_id' has to be '{self.procgen.assigned_model}', the model this job was assigned.",
                rc="ChatModelMismatch",
            )
        waiting_prompt = self.procgen.wp
        verified_message = verify_chat_reply(chat_message, waiting_prompt.chat_extras, ChatConformance(waiting_prompt.chat_conformance))
        self.dropped_tool_call_count = len(chat_message.tool_calls or ()) - len(verified_message.tool_calls or ())
        chat_message = verified_message
        self.args["finish_reason"] = chat_reply_finish_reason(self.args.finish_reason, chat_message, waiting_prompt.chat_extras)
        # PostgreSQL JSONB rejects escaped NUL as well as text columns rejecting literal NUL. Keep the structured
        # message and generation identical in the atomic claim, using the text submission's replacement policy.
        self.chat_message = build_stored_reply(chat_message)
        self.args["generation"] = self.chat_message["content"] or ""

    def get_submission_log_details(self) -> dict[str, object]:
        """Return how many tool calls verification dropped from a chat reply, when it dropped any.

        Returns:
            dict: ``dropped_tool_calls`` with the count, or nothing.
        """
        if not self.dropped_tool_call_count:
            return {}
        return {"dropped_tool_calls": self.dropped_tool_call_count}

    def get_submission_details(self):
        """Return a chat job's structured reply for ``set_generation``; a text job carries none.

        Returns:
            dict: The message, finish reasons and reported token counts of a chat job, or nothing.
        """
        if not self.procgen.wp.is_chat:
            return {}
        return {
            "message": self.chat_message,
            "finish_reason": self.args.finish_reason,
            "native_finish_reason": self.args.native_finish_reason,
            "prompt_tokens": self.args.prompt_tokens,
            "completion_tokens": self.args.completion_tokens,
        }


class TextHordeStatsTotals(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_stats_img_totals,
        code=200,
        description=f"{horde_title} generated text statistics",
    )
    def get(self):
        """Details how many texts have been generated in the past minux,hour,day,month and total
        Also shows the amount of pixelsteps for the same timeframe.
        """
        return get_compiled_textgen_stats_totals(), 200


class TextHordeStatsModels(Resource):
    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )

    @logger.catch(reraise=True)
    @cache.cached(timeout=50)
    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_stats_models,
        code=200,
        description=f"{horde_title} generated text statistics per model",
    )
    def get(self):
        """Details how many texts were generated per model for the past day, month and total"""
        return get_compiled_textgen_stats_models(), 200


class KoboldKudosTransfer(Resource):
    post_parser = reqparse.RequestParser()
    post_parser.add_argument("kai_id", type=int, required=True, location="json")
    post_parser.add_argument("kudos_amount", type=int, required=True, location="json")
    post_parser.add_argument("trusted", type=bool, default=False, required=True, location="json")

    @api.expect(post_parser)
    def post(self, user_id=""):
        """Receives kudos from the KoboldAI Horde"""
        if request.remote_addr != "167.86.124.45":
            raise e.BadRequest("Access Denied")
        user = database.find_user_by_id(user_id)
        if not user:
            raise e.UserNotFound(user_id)
        self.args = self.post_parser.parse_args()
        logger.warning(
            f"{user.get_unique_alias()} Started {self.args.kudos_amount}Kudos Transfer from KAI ID {self.args.kai_id}",
        )
        if user.trusted is False and self.args.trusted is True:
            user.set_trusted(self.args.trusted)
        user.modify_kudos(self.args.kudos_amount, "koboldai", entry_type=KudosEntryType.AWARD)
        return {"new_kudos": user.kudos}, 200
