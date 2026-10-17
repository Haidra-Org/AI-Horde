# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import math
import os
from decimal import Decimal
from typing import Any

from sqlalchemy.sql import expression

from horde import vars as hv
from horde.chat_signal import ChatEvent, publish_chat_event
from horde.classes.base.kudos import StyleReward
from horde.classes.base.waiting_prompt import WaitingPrompt, json_column_type, uuid_column_type
from horde.classes.kobold.chat_request import build_chat_job_payload
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.model_reference import model_reference


class TextWaitingPrompt(WaitingPrompt):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }
    max_length = db.Column(
        db.Integer,
        default=80,
        nullable=False,
        index=True,
        server_default=expression.literal(80),
    )
    max_context_length = db.Column(
        db.Integer,
        default=2048,
        nullable=False,
        index=True,
        server_default=expression.literal(2048),
    )
    softprompt = db.Column(db.String(255), default=None, nullable=True)
    is_chat = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())
    """Whether this is a chat request, which every query that tells chat and text requests apart reads. Chat
    requests share the text queue, kudos and stats, and only workers that accept chat jobs pop them."""
    messages = db.deferred(db.Column(json_column_type(none_as_null=True), nullable=True))
    """The OpenAI ``messages`` array of a chat request, sent to the worker in place of the prompt; NULL on every
    other text request. Deferred, so only the request a worker is assigned loads it. ``prompt`` then holds the
    flattened messages, which only the token estimate, kudos and moderation read."""
    operation_key = db.deferred(db.Column(uuid_column_type(), nullable=True))
    """The key a native caller sent to identify one submission of a chat request, claimed when the request is
    activated; NULL on every other request. Unique per user while the row exists, so a repeated submission finds the
    request it already created (ADR 0026)."""
    operation_fingerprint = db.deferred(db.Column(db.CHAR(64), nullable=True))
    """The SHA-256 hex digest of the normalised request the operation key was claimed with, from
    [`build_operation_fingerprint`][horde.classes.kobold.chat_request.build_operation_fingerprint]. A repeated key with
    another fingerprint is refused rather than answered with this request."""
    context_adjustments = db.Column(json_column_type(none_as_null=True), nullable=True)
    """How the horde changed a chat request's context length to fit its messages, as ``{from, to, reason}`` entries
    for the chat status; NULL when nothing changed and on every text request. Not deferred, since every status read
    reports it."""
    operation_claim = None
    """The ``(operation_key, operation_fingerprint)`` pair activation claims, or None. A plain attribute rather than
    a column, so it survives the rollback a deadlock retry of activation makes and is written again in the retried
    transaction."""
    processing_gens = db.relationship(
        "TextProcessingGeneration",
        back_populates="wp",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )

    def extract_params(self, **kwargs):
        self.n = self.params.pop("n", 1)
        self.jobs = self.n
        self.max_length = self.params.get("max_length", 80)
        self.max_context_length = self.params.get("max_context_length", 2048)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.max_length
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.max_length * self.n / hv.thing_divisors["text"], 2)
        self.softprompt = kwargs.get("softprompt")
        self.prepare_job_payload(self.params)

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict=None):
        """Prepares the default job payload. This might be further adjusted per job in get_job_payload()"""
        if not initial_dict:
            initial_dict = {}
        if self.is_chat:
            # A chat job's messages are added at pop time from their own column rather than copied in here, so
            # the queue scan, which loads gen_payload for every candidate, does not carry them twice.
            self.gen_payload = build_chat_job_payload(
                initial_dict,
                max_length=self.max_length,
                max_context_length=self.max_context_length,
            )
        else:
            self.gen_payload = initial_dict.copy()
            self.gen_payload["prompt"] = self.prompt
            self.gen_payload["n"] = 1
        db.session.commit()

    def get_generation_assignment(self, worker) -> dict[str, Any]:
        """Return the columns a new generation records about the worker it is assigned to.

        A chat generation keeps the worker's chat declaration as it stood at assignment, so a later change to the
        declaration does not redefine work already handed out.

        Args:
            worker: The worker the generation is assigned to.

        Returns:
            The assigned model and chat limits for a chat request; nothing for a text request.
        """
        if not self.is_chat:
            return {}
        return {
            "assigned_model": worker.chat_model_id,
            "assigned_max_context_length": worker.chat_max_context_length,
            "assigned_max_length": worker.chat_max_length,
        }

    def count_stored_generations(self) -> int:
        """Return how many generations of this request hold a submitted result, as the status route reports them.

        Returns:
            The number of generations, fake ones excluded, whose result has been stored.
        """
        return (
            db.session.query(TextProcessingGeneration.id)
            .filter(
                TextProcessingGeneration.wp_id == self.id,
                TextProcessingGeneration.fake.is_(False),
                TextProcessingGeneration.generation.is_not(None),
            )
            .count()
        )

    def get_pop_payload(self, procgen_list, payload):
        """Return the pop response, with a chat job's messages in the payload and the top-level ``chat`` flag set.

        Args:
            procgen_list: The generations assigned in this pop.
            payload: The job payload, which for a chat job carries no prompt.

        Returns:
            The pop response. A chat job's payload carries ``messages`` and the response carries ``chat: true``;
            a text job's response is unchanged.
        """
        if not self.is_chat:
            return super().get_pop_payload(procgen_list, payload)

        chat_payload = dict(payload)
        chat_payload["messages"] = self.messages
        pop_payload = super().get_pop_payload(procgen_list, chat_payload)
        pop_payload["chat"] = True
        return pop_payload

    def activate(
        self,
        downgrade_wp_priority=False,
        source_image=None,
        source_mask=None,
        extra_source_images=None,
        kudos_adjustment=0,
        *,
        style_reward: StyleReward | None = None,
    ):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate(
            downgrade_wp_priority,
            extra_source_images=extra_source_images,
            kudos_adjustment=kudos_adjustment,
            style_reward=style_reward,
        )
        proxied_account = ""
        if self.proxied_account:
            proxied_account = f":{self.proxied_account}"
        logger.info(
            f"New text2text prompt with ID {self.id} by {self.user.get_unique_alias()}{proxied_account}: "
            f"max_length:{self.max_length} * n:{self.n} == {self.total_usage} Total Tokens",
        )

    def _activate(self, *args, **kwargs) -> None:
        """Activate the request, claiming its operation key in the same transaction as the activation and its tax.

        The claim is written before anything else in the transaction, so the first commit in
        [`WaitingPrompt._activate`][horde.classes.base.waiting_prompt.WaitingPrompt._activate], which also carries the
        activation and the horde tax, either commits all three or fails on the unique index with none of them.
        """
        if self.operation_claim is not None:
            operation_key, self.operation_fingerprint = self.operation_claim
            self.operation_key = str(operation_key) if SQLITE_MODE else operation_key
        super()._activate(*args, **kwargs)

    def get_submitted_request(self) -> dict[str, Any]:
        ret_dict = super().get_submitted_request()
        ret_dict["softprompt"] = self.softprompt
        if self.is_chat:
            # The messages as sent to workers, after any moderation rewrite; the original submission is the
            # flattened prompt above.
            ret_dict["messages"] = self.messages
        return ret_dict

    def abort_for_maintenance(self):
        if not self.is_chat:
            super().abort_for_maintenance()
            return

        # A completed request also has n == 0, so completion is read before the abort can be told apart from it.
        was_completed = self.is_completed()
        super().abort_for_maintenance()
        if not was_completed and self.n == 0:
            publish_chat_event(str(self.id), ChatEvent.CANCELLED, self.count_stored_generations())

    def calculate_extra_kudos_burn(self, kudos):
        # This represents the cost of using the resources of the horde
        return kudos + 1

    def log_faulted_prompt(self):
        logger.warning(
            f"Faulting waiting text prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs",
        )

    def get_status(self, **kwargs):
        ret_dict = super().get_status(**kwargs)
        if self.is_chat and self.context_adjustments:
            ret_dict["context_adjustments"] = self.context_adjustments
        return ret_dict

    def record_usage(
        self,
        raw_things,
        kudos,
        usage_type="text",
        avoid_burn=False,
        commit=True,
        *,
        style_reward: StyleReward | None = None,
    ) -> Decimal:
        """I need to extend this to point it to record_text_usage()"""
        return super().record_usage(
            raw_things,
            kudos,
            usage_type,
            avoid_burn=avoid_burn,
            commit=commit,
            style_reward=style_reward,
        )

    def require_upfront_kudos(self, counted_totals, total_threads):
        """Returns A tuple
        First entry in the tuple is True if this wp requires that the user already has the required kudos to fulfil it
        else is False
        Second entry in the tuple is the max tokens that can be used without upfront kudos
        Third entry in the tuple is whether the upfront kudos requirement prevents downgrading to resolve this.
        """
        queue = counted_totals["queued_text_requests"]
        max_tokens = 512 + (total_threads * 5) - round(queue * 0.9)
        # logger.debug([queue,max_tokens])
        if not self.slow_workers:
            return (True, max_tokens, False)
        if max_tokens < 256:
            max_tokens = 256
        if max_tokens > 512:
            max_tokens = 512
        if self.max_length > max_tokens:
            return (True, max_tokens, False)
        if os.getenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "0") == "1" and len(self.workers) > 0:
            return (True, max_tokens, True)
        return (False, max_tokens, False)

    def downgrade(self, max_tokens):
        """Ensures this WP requirements are not exceeding upfront kudos requirements"""
        self.slow_workers = True
        while self.max_length > max_tokens:
            self.max_length = max_tokens
            self.params["max_length"] = self.max_length
            self.gen_payload["max_length"] = self.max_length
            logger.info(f"Text WP {self.id} was downgraded to {self.max_length} tokens")
        db.session.commit()

    def calculate_kudos(self):
        if os.getenv("HORDE_REQUIRE_MATCHED_TARGETING", "0") == "1" and len(self.workers) > 0:
            self.kudos = 0.1
            return self.kudos
        # Slimmed down version of procgen.get_gen_kudos()
        # As we don't know the worker's trusted status.
        # It exists here in order to allow us to calculate dry_runs
        context_multiplier = 1.2 + (2.2 ** (math.log2(self.max_context_length / 1024)))
        # Prevent shenanigans
        if context_multiplier > 30:
            context_multiplier = 30
        if context_multiplier < 0.1:
            context_multiplier = 0.1
        if len(self.models) > 0:
            model_name = self.models[0].model
        else:
            # For empty model lists, we assume they're going to run into a 13B model
            return round(self.max_length * 13 * context_multiplier / 100, 2)
        if not model_reference.is_known_text_model(model_name):
            return self.max_length * (2.7 / 100) * context_multiplier
        model_multiplier = model_reference.get_text_model_multiplier(model_name)
        parameter_bonus = (max(model_multiplier, 13) / 13) ** 0.20
        self.kudos = round(
            self.max_length * parameter_bonus * model_multiplier * context_multiplier / 100,
            2,
        )
        return self.kudos


# Declared after the class because the columns belong to the text subtype of the shared waiting_prompts table. The
# claim holds one operation key per user; the predicate keeps the many requests without a key out of the index.
db.Index(
    "ix_waiting_prompts_user_operation_key",
    TextWaitingPrompt.__table__.c.user_id,
    TextWaitingPrompt.__table__.c.operation_key,
    unique=True,
    postgresql_where=TextWaitingPrompt.__table__.c.operation_key.is_not(None),
)
