# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from horde_sdk.generation_parameters.text.chat_policy import KNOWN_CHAT_DECLARATIONS, ChatFeature, ChatLimitField
from horde_sdk.worker.chat_declaration import IMAGE_PART_TRANSPORTS, ChatImageTransport
from sqlalchemy import JSON, ScalarSelect
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.sql import expression

from horde import exceptions as e
from horde import vars as hv
from horde.bridge_reference import (
    is_backed_validated,
)
from horde.classes.base.worker import (
    SPEED_BASELINE_THINGS_PER_SEC,
    Worker,
    WorkerEligibilityInputs,
    worker_ordered_names_clause,
)
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.model_reference import model_reference
from horde.utils import sanitize_string

if TYPE_CHECKING:
    from horde.classes.base.waiting_prompt import RequestEligibilityInputs

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731
text_array_column_type = ARRAY(db.Text) if not SQLITE_MODE else JSON
"""A list of strings: a text array on Postgres, JSON where arrays are not available."""

TEXT_WORKER_DECLARATION_GENERATION_KEY = "text_worker_declaration_generation"
"""Redis counter of changes to the text worker state that decides chat eligibility: the chat declaration and the model
list. A cached text request availability records the value it was computed under and is recomputed once the counter
moves, so a worker that pops with a new declaration is reflected at the next status poll rather than when the cache
expires. A missing key reads as 0."""

TEXT_JOB_LEASE_SECONDS = 150
"""The lease a text or chat job receives at pop, and the time a renewing progress post extends it to from the post.
It matches the base generation default, since model loading and a busy worker delay every job type's start alike."""

TEXT_JOB_PREPARATION_SECONDS = 30
"""The part of the base lease set aside for reaching the job and processing its prompt, which does not scale with the
requested output length. The rest of the lease is what the speed gate measures a worker's generation against."""

UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND = 10.0
"""The speed assumed for a text worker that has not recorded one. A worker records a speed only by completing jobs,
and the speed gate offers it only jobs its assumed speed can finish, so the assumption must admit the jobs clients
commonly send: 10 tokens per second admits 1200 tokens inside the base lease, above the usual 512-token request, and
the first completed jobs replace it with the measured value."""


def bump_text_worker_declaration_generation() -> None:
    """Invalidate every cached text request availability computed before the current worker state.

    Call only after the changed worker row is committed. A poll that computes between the bump and the commit would
    read the old row and cache it under the new generation, keeping the stale verdict until the cache expires.
    Only the primary Redis is incremented because availability reads the counter from the primary alone.
    """
    if hr.horde_r is None:
        return
    try:
        hr.horde_r.incr(TEXT_WORKER_DECLARATION_GENERATION_KEY)
    except Exception as err:
        logger.warning(f"Failed to bump the text worker declaration generation: {err}")


def get_minimum_text_worker_speed(model_names: Sequence[str]) -> int:
    """Return the normal-speed threshold for a text worker's first model.

    Args:
        model_names: Models advertised in bridge preference order.

    Returns:
        Minimum tokens per second for requests that disallow slow workers.
    """

    if not model_names:
        return 3
    parameter_count = model_reference.get_text_model_multiplier(model_names[0])
    if parameter_count >= 20:
        return 3
    if parameter_count >= 13:
        return 4
    return 5


def get_effective_text_worker_speed(worker_speed: float) -> float:
    """Return the tokens per second a text worker is assumed to generate at.

    A worker that has not recorded a speed stores the speed baseline, which is far below any text threshold. Such a
    worker is taken to run at
    [`UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND`][horde.classes.kobold.worker.UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND],
    so it can receive the jobs that record the samples replacing the assumption. A worker whose recorded average is
    exactly the baseline is treated the same way.

    Args:
        worker_speed: The worker's stored ``speed``, in the raw units the column holds.

    Returns:
        The measured tokens per second, or the assumed speed when the worker has not recorded one.
    """
    if worker_speed == SPEED_BASELINE_THINGS_PER_SEC * hv.thing_divisors["text"]:
        return UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND
    return worker_speed / hv.thing_divisors["text"]


def get_text_worker_lease_capacity(worker_speed: float) -> int:
    """Return the longest ``max_length`` a text worker is offered, the tokens it generates inside the base lease.

    Args:
        worker_speed: The worker's stored ``speed``, in the raw units the column holds.
        model_names: Models the worker advertises, in bridge preference order.

    Returns:
        The tokens the worker's effective speed produces in the base lease after the preparation allowance.
    """
    generation_seconds = TEXT_JOB_LEASE_SECONDS - TEXT_JOB_PREPARATION_SECONDS
    return math.floor(get_effective_text_worker_speed(worker_speed) * generation_seconds)


class TextWorkerSoftprompts(db.Model):
    __tablename__ = "text_worker_softprompts"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    worker = db.relationship("TextWorker", back_populates="softprompts")
    softprompt = db.Column(db.String(255))
    wtype = "text"


class TextWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "text_worker",
    }
    # TODO: Switch to max_power
    max_length = db.Column(db.Integer, default=80, nullable=False)
    max_context_length = db.Column(db.Integer, default=2048, nullable=False)
    accepts_chat_jobs = db.Column(db.Boolean, default=False, nullable=False, server_default=expression.false())
    """Whether the worker's last pop opted into chat jobs, which carry ``messages`` instead of a prompt. Off by
    default because a worker that predates chat jobs would find no prompt in their payload."""
    accepts_prompt_jobs = db.Column(db.Boolean, default=True, nullable=False, server_default=expression.true())
    """Whether the worker's last pop accepts plain text jobs, which carry a prompt. On by default because every
    worker that predates the field serves them; only a worker that also accepts chat jobs may turn it off."""
    chat_features = db.Column(text_array_column_type, nullable=True)
    """The capability and guarantee names (``ChatFeature`` and ``ChatGuarantee``) the worker's last pop declared.
    NULL or empty means none; unknown names are not stored."""
    chat_max_context_length = db.Column(db.Integer, nullable=True)
    """The context length the worker serves chat jobs at, from its last pop. Independent of ``max_context_length``,
    which bounds text jobs."""
    chat_max_length = db.Column(db.Integer, nullable=True)
    """The most tokens the worker generates for a chat job, from its last pop. Independent of ``max_length``."""
    chat_tools_max_temperature = db.Column(db.Float, nullable=True)
    """The highest temperature the worker's backend applies as sent when tools are present, from its last pop's
    ``chat_limits``. NULL when the worker applies every temperature as sent or does not accept chat jobs."""
    chat_model_id = db.Column(db.String, nullable=True)
    """The model the worker serves chat jobs with, from its last pop. A chat request is only given to a worker whose
    ``chat_model_id`` is the request's model."""
    image_transports = db.Column(text_array_column_type, nullable=True)
    """The ``ChatImageTransport`` values the worker's last pop declared for receiving a chat job's images and
    offloaded messages. NULL or empty means the worker does not take images; unknown values are not stored. Only ``url``
    and ``base64`` let it receive images, since ``messages_url`` applies only to requests whose messages were
    offloaded."""

    softprompts: Mapped[list[TextWorkerSoftprompts]] = relationship(
        "TextWorkerSoftprompts",
        back_populates="worker",
        cascade="all, delete-orphan",
    )
    wtype = "text"

    def check_in(
        self,
        max_length,
        max_context_length,
        softprompts,
        *,
        accepts_chat_jobs=False,
        accepts_prompt_jobs=True,
        chat_features: Sequence[str] | None = None,
        chat_max_context_length: int | None = None,
        chat_max_length: int | None = None,
        chat_tools_max_temperature: float | None = None,
        chat_model_id: str | None = None,
        image_transports: Sequence[str] | None = None,
        **kwargs,
    ):
        # The chat declaration is replaced at every pop, outside the check-in debounce: this request's chat
        # eligibility and payload shape are decided from these fields, and a worker that stops declaring chat must
        # not receive a chat payload. Unchanged values leave the row clean, so a steady worker does not add an UPDATE or a
        # generation bump. A changed one is committed here, ahead of the pop's own commit, because the bump that
        # invalidates cached request availability must follow the commit. The plain-job opt-out is part of the
        # declaration for the same reason: a chat-only worker must not receive a prompt.
        previous_chat_declaration = self.get_chat_declaration()
        self.accepts_chat_jobs = accepts_chat_jobs
        self.accepts_prompt_jobs = accepts_prompt_jobs or not accepts_chat_jobs
        self.chat_features = sorted(set(chat_features or ()) & KNOWN_CHAT_DECLARATIONS) if accepts_chat_jobs else None
        self.chat_max_context_length = chat_max_context_length if accepts_chat_jobs else None
        self.chat_max_length = chat_max_length if accepts_chat_jobs else None
        self.chat_tools_max_temperature = chat_tools_max_temperature if accepts_chat_jobs else None
        self.chat_model_id = chat_model_id if accepts_chat_jobs else None
        self.image_transports = (
            sorted(set(image_transports or ()) & {transport.value for transport in ChatImageTransport}) if accepts_chat_jobs else None
        )
        chat_declaration_changed = self.get_chat_declaration() != previous_chat_declaration
        if not super().check_in(**kwargs):
            if chat_declaration_changed:
                db.session.commit()
                bump_text_worker_declaration_generation()
            return
        self.max_length = max_length
        self.max_context_length = max_context_length
        self.set_softprompts(softprompts)  # Does a commit as well
        paused_string = ""
        if self.paused:
            paused_string = "(Paused) "
        logger.trace(
            f"{paused_string}Text Worker {self.name} checked-in, offering models {self.models} "
            f"at {self.max_length} max tokens and {self.max_context_length} max content length.",
        )
        db.session.commit()
        if chat_declaration_changed:
            bump_text_worker_declaration_generation()

    def get_chat_declaration(self) -> tuple[Any, ...]:
        """Return the stored check-in fields that decide chat eligibility, for detecting a changed declaration.

        Returns:
            The chat opt-in, the plain-job opt-in, the chat features, limits, model and image transports.
        """
        return (
            self.accepts_chat_jobs,
            self.accepts_prompt_jobs,
            self.chat_features,
            self.chat_max_context_length,
            self.chat_max_length,
            self.chat_tools_max_temperature,
            self.chat_model_id,
            self.image_transports,
        )

    def set_models(self, models) -> bool:
        """Replace the advertised models, bumping the declaration generation when they change.

        A chat request that lists models is refused by a worker that does not advertise any of them, and the pop speed threshold
        depends on the first model, so the model list is part of eligibility. The base method commits a change
        before returning, so the bump follows the commit.

        Returns:
            True when the stored model list changed.
        """
        models_changed = super().set_models(models)
        if models_changed:
            bump_text_worker_declaration_generation()
        return models_changed

    def refresh_softprompt_cache(self) -> list[str]:
        # Read the authoritative rows straight from text_worker_softprompts rather than
        # the self.softprompts relationship collection. Under expire_on_commit=False that
        # collection can remain loaded with pre-change rows across a commit, which would
        # republish a stale list. A direct query always reflects what was written.
        softprompts_list = [row.softprompt for row in db.session.query(TextWorkerSoftprompts.softprompt).filter_by(worker_id=self.id).all()]
        try:
            hr.horde_r_setex(
                f"worker_{self.id}_softprompts_cache",
                timedelta(seconds=600),
                json.dumps(softprompts_list),
            )
        except Exception as e:
            logger.warning(f"Error when trying to set softprompts cache: {e}. Retrieving from DB.")
        return softprompts_list

    def get_softprompt_names(self):
        if hr.horde_r is None:
            return [s.softprompt for s in self.softprompts]
        softprompts_cache = hr.horde_r_get(f"worker_{self.id}_softprompts_cache")
        if not softprompts_cache:
            return self.refresh_softprompt_cache()
        try:
            softprompts_ret = json.loads(softprompts_cache)
        except TypeError:
            logger.error("Softprompts cache could not be loaded: {softprompts_cache}")
            return self.refresh_softprompt_cache()
        if softprompts_ret is None:
            return self.refresh_softprompt_cache()
        return softprompts_ret

    def set_softprompts(self, softprompts):
        softprompts = [sanitize_string(softprompt_name[0:100]) for softprompt_name in softprompts]
        del softprompts[200:]
        softprompts = set(softprompts)
        existing_softprompts_names = set(self.get_softprompt_names())
        if existing_softprompts_names == softprompts:
            return
        logger.debug(
            [
                existing_softprompts_names,
                softprompts,
                existing_softprompts_names == softprompts,
            ],
        )
        db.session.query(TextWorkerSoftprompts).filter_by(worker_id=self.id).delete()
        db.session.flush()
        for softprompt_name in softprompts:
            softprompt = TextWorkerSoftprompts(worker_id=self.id, softprompt=softprompt_name)
            db.session.add(softprompt)
        self.refresh_softprompt_cache()

    def calculate_uptime_reward(self):
        model = self.get_model_names()[0]
        # The base amount of kudos one gets is based on the max context length they've loaded
        base_kudos = 25 + (15 * min(self.max_context_length, 262_144) / 1024)
        if not model_reference.is_known_text_model(model):
            return base_kudos * 0.5
        # We consider the 7B models the baseline here
        param_multiplier = model_reference.get_text_model_multiplier(model) / 7
        if param_multiplier < 0.25:
            param_multiplier = 0.25
        # Unvalidated backends get less kudos
        if not is_backed_validated(self.bridge_agent):
            base_kudos *= 0.3
        # The uptime is based on both how much context they provide, as well as how many parameters they're serving
        return round(base_kudos * param_multiplier, 2)

    def get_eligibility_inputs(self, waiting_prompt: Any) -> WorkerEligibilityInputs:
        """Return the worker-side eligibility inputs, including the worker's softprompt names.

        Args:
            waiting_prompt: The text request being evaluated.

        Returns:
            The worker's eligibility inputs.
        """
        eligibility_inputs = super().get_eligibility_inputs(waiting_prompt)
        return replace(eligibility_inputs, softprompt_names=tuple(self.get_softprompt_names()))

    def can_generate_with_eligibility_inputs(
        self,
        waiting_prompt: Any,
        request_inputs: RequestEligibilityInputs,
        worker_inputs: WorkerEligibilityInputs,
    ) -> list[bool | str | None]:
        """Evaluate a text request from already loaded eligibility inputs.

        Args:
            waiting_prompt: Text request being evaluated.
            request_inputs: The request's eligibility inputs.
            worker_inputs: This worker's eligibility inputs.

        Returns:
            Eligibility followed by the existing rejection reason, if any.
        """

        can_generate = super().can_generate_with_eligibility_inputs(waiting_prompt, request_inputs, worker_inputs)
        if not can_generate[0]:
            return [can_generate[0], can_generate[1]]
        model_names = list(worker_inputs.model_names)
        requested_models = list(request_inputs.model_names)
        if requested_models and request_inputs.model_name_set.isdisjoint(model_names):
            return [False, "models"]
        if waiting_prompt.is_chat:
            chat_refusal = self.chat_refusal_reason(waiting_prompt, requested_models)
            if chat_refusal is not None:
                return [False, chat_refusal]
        else:
            if not self.accepts_prompt_jobs:
                return [False, "prompt_jobs"]
            if self.max_context_length < waiting_prompt.max_context_length:
                return [False, "max_context_length"]
            if self.max_length < waiting_prompt.max_length:
                return [False, "max_length"]
        if not waiting_prompt.slow_workers and self.speed < get_minimum_text_worker_speed(model_names):
            return [False, "performance"]
        # A chat job the worker cannot finish inside the base lease would only be renewed by progress posts, which a
        # worker that never posts does not send; refusing it at offer time keeps every chat assignment finishable.
        # A legacy text job is assigned whatever its length, and an over-capacity assignment is recorded at pop.
        if waiting_prompt.is_chat and waiting_prompt.max_length > get_text_worker_lease_capacity(self.speed):
            return [False, "lease_speed"]
        if waiting_prompt.validated_backends and not is_backed_validated(self.bridge_agent):
            return [False, "bridge_version"]
        matching_softprompt = True
        if waiting_prompt.softprompt:
            matching_softprompt = False
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if waiting_prompt.softprompt == "":
                matching_softprompt = True
            if waiting_prompt.softprompt in worker_inputs.softprompt_names:
                matching_softprompt = True
        if not matching_softprompt:
            return [False, "matching_softprompt"]
        return [True, None]

    def chat_refusal_reason(self, waiting_prompt: Any, requested_models: Sequence[str]) -> str | None:
        """Return why this worker cannot serve a chat request under its chat declaration, if it cannot.

        Chat jobs are bounded by the chat limits, never by the text limits, which only bound text jobs.

        Args:
            waiting_prompt: The chat request.
            requested_models: The models the request accepts.

        Returns:
            ``chat_jobs`` when the worker did not opt in or does not serve the request's model with chat,
            ``chat_features`` when it lacks a capability or guarantee the request's stored ``chat_required_features``
            lists (and an image transport besides ``vision`` for a request carrying images), ``tools_max_temperature``
            when the backend would clamp the temperature of a request that forwards tools, ``max_context_length`` or
            ``max_length`` when the request exceeds a chat limit, or None when it can serve the request.
        """
        if not self.accepts_chat_jobs or self.chat_model_id is None:
            return "chat_jobs"
        if requested_models and self.chat_model_id not in requested_models:
            return "chat_jobs"
        needed_features = set(waiting_prompt.chat_required_features or ())
        if not needed_features <= set(self.chat_features or ()):
            return "chat_features"
        if ChatFeature.VISION.value in needed_features and not self.serves_chat_images():
            return "chat_features"
        # The SDK's violated_chat_limit defines this rule; the stored projection lets the pop query and this check
        # apply it without loading the request's extras. It holds under every conformance mode.
        if (
            waiting_prompt.chat_tools_temperature is not None
            and self.chat_tools_max_temperature is not None
            and waiting_prompt.chat_tools_temperature > self.chat_tools_max_temperature
        ):
            return ChatLimitField.TOOLS_MAX_TEMPERATURE.value
        if self.chat_max_context_length is None or self.chat_max_context_length < waiting_prompt.max_context_length:
            return "max_context_length"
        if self.chat_max_length is None or self.chat_max_length < waiting_prompt.max_length:
            return "max_length"
        return None

    def missing_chat_declarations(self, waiting_prompt: Any) -> set[str]:
        """Return the declaration names the worker lacks for a chat request it refused with ``chat_features``.

        Args:
            waiting_prompt: The chat request.

        Returns:
            The capabilities and guarantees the request's stored ``chat_required_features`` lists that the worker
            does not declare, with ``image_transports`` when it declares ``vision`` without a way to receive images.
        """
        needed_features = set(waiting_prompt.chat_required_features or ())
        missing_declarations = needed_features - set(self.chat_features or ())
        if ChatFeature.VISION.value in needed_features - missing_declarations and not self.declares_image_part_transport():
            missing_declarations.add("image_transports")
        return missing_declarations

    def serves_chat_images(self) -> bool:
        """Return whether the worker's chat declaration lets it receive a chat request's images.

        Returns:
            True when it declared the ``vision`` feature and the ``url`` or ``base64`` image transport.
        """
        return ChatFeature.VISION.value in (self.chat_features or ()) and self.declares_image_part_transport()

    def declares_image_part_transport(self) -> bool:
        """Return whether the worker declared a transport that delivers every image part, ``url`` or ``base64``.

        Returns:
            True when its ``image_transports`` holds one of the SDK's ``IMAGE_PART_TRANSPORTS``.
        """
        return not {transport.value for transport in IMAGE_PART_TRANSPORTS}.isdisjoint(self.image_transports or ())

    def get_details(self, is_privileged=False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_length"] = self.max_length
        ret_dict["max_context_length"] = self.max_context_length
        return ret_dict

    def parse_models(self, unchecked_models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        del unchecked_models[200:]
        models = set()
        for model in unchecked_models:
            # # We allow custom models from trusted users
            # if model in model_reference.text_model_names or self.user.trusted:
            usermodel = model.split("::")
            if len(usermodel) == 2:
                user_alias = usermodel[1]
                if self.user.get_unique_alias() != user_alias:
                    raise e.BadRequest(f"This model can only be hosted by {user_alias}")
            models.add(model)
        if len(models) == 0:
            raise e.BadRequest("Unfortunately we cannot accept workers serving unrecognised models at this time")
        return models


def text_worker_softprompt_names_clause() -> ScalarSelect:
    """Return the correlated subquery aggregating a text worker's softprompt names, in the order they were stored.

    Returns:
        The aggregate column expression of the text worker's softprompt names, ordered by row id. See
            [`worker_ordered_names_clause`][horde.classes.base.worker.worker_ordered_names_clause] for the value
            of a worker without softprompts.
    """
    return worker_ordered_names_clause(
        name_column=TextWorkerSoftprompts.softprompt,
        id_column=TextWorkerSoftprompts.id,
        worker_id_column=TextWorkerSoftprompts.worker_id,
        worker_class=TextWorker,
    )
