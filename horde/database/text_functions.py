# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, noload, selectinload, with_expression

import horde.classes.base.stats as stats
from horde.bridge_reference import (
    is_backed_validated,
)
from horde.classes.base.waiting_prompt import WPAllowedWorkers, WPModels
from horde.classes.base.worker import WorkerPerformance
from horde.classes.kobold.processing_generation import TextProcessingGeneration

# FIXME: Renamed for backwards compat. To fix later
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt, chat_images_clause
from horde.classes.kobold.worker import (
    TextWorker,
    get_minimum_text_worker_speed,
    get_text_worker_lease_capacity,
)
from horde.database.functions import query_prioritized_wps
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger

CHAT_WORKER_ONLINE_WINDOW = timedelta(seconds=300)
"""How recently a chat worker must have checked in to count as online; the window ``get_active_workers()`` uses."""


@dataclass(frozen=True)
class ChatOperationClaim:
    """Represents the chat request that claimed an operation key."""

    request_id: str
    """The id of the request the key was claimed by."""

    kudos: float
    """The kudos the request was quoted at activation."""

    fingerprint: str | None
    """The fingerprint the key was claimed with."""


@dataclass(frozen=True)
class ChatModelOffer:
    """Represents what the online chat workers serving one model declare, taken together."""

    worker_count: int
    """How many online workers serve chat jobs with the model."""

    max_context_length: int
    """The largest chat context any of them declares."""

    max_length: int
    """The largest chat output any of them declares."""

    features: tuple[str, ...]
    """Every chat capability and guarantee at least one of them declares, sorted."""

    created: datetime | None
    """When the longest-registered of them was created; None when no row records a creation time."""


# Should be overriden
def convert_things_to_kudos(things, **kwargs):
    # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
    return round(things, 2)


def text_wp_offer_clauses(worker, models_list):
    """Return the filter clauses a text request has to meet to be offered to a worker, except the speed gate.

    Args:
        worker: The popping text worker.
        models_list: The models the worker advertises, in bridge preference order.

    Returns:
        The clauses, to be combined with [`text_wp_speed_gate_clause`][horde.database.text_functions.text_wp_speed_gate_clause]
        or its negation.
    """
    slow_speed = get_minimum_text_worker_speed(models_list)
    # The model constraint is a semi-join: joining wp_models returns one row per
    # matching model, and the pop's page LIMIT counts joined rows, so a WP
    # naming several of the worker's models would consume several page slots as
    # duplicates of itself.
    wp_serves_model = db.session.query(WPModels.id).filter(WPModels.wp_id == TextWaitingPrompt.id, WPModels.model.in_(models_list)).exists()
    wp_names_any_model = db.session.query(WPModels.id).filter(WPModels.wp_id == TextWaitingPrompt.id).exists()
    # Worker targeting is evaluated per WP, never per targeting row: joining
    # wp_allowed_workers admits a blacklisted worker whenever the blacklist
    # names anyone else, because the other rows satisfy a row-level
    # ``worker_id != x`` predicate.
    wp_targets_this_worker = (
        db.session.query(WPAllowedWorkers.id)
        .filter(WPAllowedWorkers.wp_id == TextWaitingPrompt.id, WPAllowedWorkers.worker_id == worker.id)
        .exists()
    )
    wp_has_worker_targets = db.session.query(WPAllowedWorkers.id).filter(WPAllowedWorkers.wp_id == TextWaitingPrompt.id).exists()
    # Each job family is bounded by its own limits. Chat features are checked in can_generate().
    wp_fits_worker = and_(
        TextWaitingPrompt.is_chat.is_(False),
        TextWaitingPrompt.max_length <= worker.max_length,
        TextWaitingPrompt.max_context_length <= worker.max_context_length,
    )
    worker_declares_chat = (
        worker.accepts_chat_jobs
        and worker.chat_model_id is not None
        and worker.chat_max_length is not None
        and worker.chat_max_context_length is not None
    )
    if worker_declares_chat:
        wp_names_chat_model = (
            db.session.query(WPModels.id).filter(WPModels.wp_id == TextWaitingPrompt.id, WPModels.model == worker.chat_model_id).exists()
        )
        wp_fits_chat_worker = and_(
            TextWaitingPrompt.is_chat.is_(True),
            TextWaitingPrompt.max_length <= worker.chat_max_length,
            TextWaitingPrompt.max_context_length <= worker.chat_max_context_length,
            wp_names_chat_model,
        )
        if worker.chat_tools_max_temperature is not None:
            # The SDK's violated_chat_limit rule, applied to the stored projection of the request's traits; a NULL
            # projection means the request forwards no tools.
            wp_fits_chat_worker = and_(
                wp_fits_chat_worker,
                or_(
                    TextWaitingPrompt.chat_tools_temperature.is_(None),
                    TextWaitingPrompt.chat_tools_temperature <= worker.chat_tools_max_temperature,
                ),
            )
        if not worker.serves_chat_images():
            # Only chat rows reach this test, and only for workers that cannot take images, so a vision worker's
            # scan never reads the messages column.
            wp_fits_chat_worker = and_(wp_fits_chat_worker, ~chat_images_clause())
        # A chat-only worker may leave its text limits at the pop defaults, which are not a capacity it serves at.
        wp_fits_worker = or_(wp_fits_worker, wp_fits_chat_worker) if worker.accepts_prompt_jobs else wp_fits_chat_worker
    return [
        TextWaitingPrompt.n > 0,
        wp_fits_worker,
        TextWaitingPrompt.active.is_(True),
        TextWaitingPrompt.faulted.is_(False),
        TextWaitingPrompt.expiry > datetime.utcnow(),
        or_(
            TextWaitingPrompt.safe_ip.is_(True),
            worker.allow_unsafe_ipaddr is True,
        ),
        or_(
            TextWaitingPrompt.nsfw.is_(False),
            worker.nsfw is True,
        ),
        or_(
            wp_serves_model,
            ~wp_names_any_model,
        ),
        or_(
            ~wp_has_worker_targets,
            and_(
                TextWaitingPrompt.worker_blacklist.is_(False),
                wp_targets_this_worker,
            ),
            and_(
                TextWaitingPrompt.worker_blacklist.is_(True),
                ~wp_targets_this_worker,
            ),
        ),
        or_(
            worker.speed >= slow_speed,  # Slow speed is based on the model parameters used
            TextWaitingPrompt.slow_workers.is_(True),
        ),
        or_(
            worker.maintenance is False,
            TextWaitingPrompt.user_id == worker.user_id,
        ),
        or_(
            is_backed_validated(worker.bridge_agent),
            TextWaitingPrompt.validated_backends.is_(False),
        ),
    ]


def text_wp_speed_gate_clause(worker, models_list):
    """Return the clause offering a worker only the chat requests whose ``max_length`` it finishes inside the base lease.

    A legacy text request passes the clause whatever its ``max_length``: the gate is enforced for chat requests only,
    and a legacy job assigned over the worker's capacity is recorded by
    [`TextProcessingGeneration`][horde.classes.kobold.processing_generation.TextProcessingGeneration] instead.
    [`TextWorker.can_generate_with_eligibility_inputs`][horde.classes.kobold.worker.TextWorker.can_generate_with_eligibility_inputs]
    applies the same rule through the same capacity.

    Args:
        worker: The popping text worker.
        models_list: The models the worker advertises, in bridge preference order.

    Returns:
        The clause.
    """
    # The capacity is a scalar computed once per pop from the popping worker, so the clause compares columns with a
    # bound parameter and adds no join.
    # - is_chat IS false: a legacy text request is offered at any max_length.
    # - max_length <= capacity: a chat request is offered only when the worker finishes it inside the base lease.
    # For a worker measured at 2 tokens/s (capacity 2 * 120 = 240):
    #
    # wp.id | is_chat | max_length | offered
    # A     | true    | 240        | yes
    # B     | true    | 512        | no
    # C     | false   | 512        | yes
    return or_(
        TextWaitingPrompt.is_chat.is_(False),
        TextWaitingPrompt.max_length <= get_text_worker_lease_capacity(worker.speed),
    )


def worker_may_be_speed_gated(worker, models_list) -> bool:
    """Return whether the speed gate can exclude any chat request the worker's chat declaration would otherwise admit.

    The gate does not apply to legacy text requests, so the worker's text limits do not bear on it.

    Args:
        worker: The popping text worker.
        models_list: The models the worker advertises, in bridge preference order.

    Returns:
        True when the worker accepts chat jobs and its lease capacity is below the chat output it declares.
    """
    if not worker.accepts_chat_jobs or worker.chat_max_length is None:
        return False
    return get_text_worker_lease_capacity(worker.speed) < worker.chat_max_length


def get_sorted_text_wp_filtered_to_worker(worker, models_list=None, priority_user_ids=None, page=0):
    # This is just the top 3 - Adjusted method to send Worker object. Filters to add.
    # TODO: Filter by (Worker in WP.workers) __ONLY IF__ len(WP.workers) >=1
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ Worker.user.trusted == False
    # TODO: Filter by Worker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.Worker.gan_generate())
    PER_PAGE = 3  # how many requests we're picking up to filter further
    final_wp_list = (
        db.session.query(TextWaitingPrompt)
        .options(
            noload(TextWaitingPrompt.processing_gens),
            # can_generate() reads these for every candidate; loaded lazily that is three SELECTs per candidate per pop.
            selectinload(TextWaitingPrompt.models),
            selectinload(TextWaitingPrompt.tricked_workers),
            selectinload(TextWaitingPrompt.workers),
            # Computed for the returned page only, so eligibility never loads a candidate's deferred messages.
            with_expression(TextWaitingPrompt.carries_chat_images, chat_images_clause()),
        )
        # The speed gate is kept apart from the offer clauses so each rule reads on its own. A chat request the
        # worker's speed cannot finish inside the base lease is not paged here, so it never takes one of the page's
        # slots.
        .filter(
            *text_wp_offer_clauses(worker, models_list),
            text_wp_speed_gate_clause(worker, models_list),
        )
    )
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(TextWaitingPrompt.user_id.in_(priority_user_ids))
    # logger.debug(final_wp_list)
    final_wp_list = (
        final_wp_list.order_by(TextWaitingPrompt.extra_priority.desc(), TextWaitingPrompt.created.asc())
        .offset(PER_PAGE * page)
        .limit(PER_PAGE)
    )
    # logger.debug(final_wp_list.all())
    return final_wp_list.populate_existing().with_for_update(skip_locked=True, of=TextWaitingPrompt).all()


def get_text_wp_by_id(wp_id, lite=False, *, chat=False):
    """Return a text request by id, from the family its route serves.

    Args:
        wp_id (str): The request id.
        lite (bool): When true, the request's generations are not loaded.
        chat (bool): When true, only a chat request matches; otherwise only a plain text request does. The text
            and chat routes each report the other family's requests as not found.

    Returns:
        TextWaitingPrompt | None: The request, or None when no request of that family has the id.
    """
    try:
        wp_uuid = uuid.UUID(wp_id)
    except ValueError:
        logger.debug(f"Non-UUID wp_id sent: '{wp_id}'.")
        return None
    if SQLITE_MODE:
        wp_uuid = str(wp_uuid)
    # lite version does not pull ProcGens
    if lite:
        query = db.session.query(TextWaitingPrompt).options(noload(TextWaitingPrompt.processing_gens))
    else:
        query = db.session.query(TextWaitingPrompt)
    return query.filter(TextWaitingPrompt.is_chat.is_(chat)).filter_by(id=wp_uuid).first()


def find_chat_operation_claim(user_id: int, operation_key: uuid.UUID) -> ChatOperationClaim | None:
    """Return the chat request a user's operation key was claimed by.

    Args:
        user_id: The user the claim belongs to.
        operation_key: The key.

    Returns:
        The claiming request, or None when the user has no request under the key.
    """
    claimed_row = (
        db.session.query(
            TextWaitingPrompt.id,
            TextWaitingPrompt.kudos,
            TextWaitingPrompt.operation_fingerprint,
        )
        .filter(
            TextWaitingPrompt.user_id == user_id,
            TextWaitingPrompt.operation_key == (str(operation_key) if SQLITE_MODE else operation_key),
        )
        .first()
    )
    if claimed_row is None:
        return None
    return ChatOperationClaim(
        request_id=str(claimed_row.id),
        kudos=claimed_row.kudos,
        fingerprint=claimed_row.operation_fingerprint,
    )


def is_chat_model_served(model_name: str) -> bool:
    """Return whether an online worker that accepts chat jobs serves them with this model.

    Args:
        model_name: The model a chat request asked for.

    Returns:
        Whether at least one online chat worker declared the model as its ``model_id``.
    """
    served_query = db.session.query(TextWorker.id).filter(
        TextWorker.last_check_in > datetime.utcnow() - CHAT_WORKER_ONLINE_WINDOW,
        TextWorker.accepts_chat_jobs.is_(True),
        TextWorker.chat_model_id == model_name,
    )
    return bool(db.session.query(served_query.exists()).scalar())


def get_chat_model_offers() -> dict[str, ChatModelOffer]:
    """Return, per model, what the online chat workers serving it declare, in one query.

    Workers in maintenance are left out, as the model status leaves them out of its counts.

    Returns:
        One offer per model at least one online chat worker serves, keyed by the model name.
    """
    declaration_rows = (
        db.session.query(
            TextWorker.chat_model_id,
            TextWorker.chat_max_context_length,
            TextWorker.chat_max_length,
            TextWorker.chat_features,
            TextWorker.created,
        )
        .filter(
            TextWorker.last_check_in > datetime.utcnow() - CHAT_WORKER_ONLINE_WINDOW,
            TextWorker.accepts_chat_jobs.is_(True),
            TextWorker.chat_model_id.is_not(None),
            TextWorker.maintenance.is_(False),
        )
        .all()
    )
    rows_by_model: dict[str, list] = {}
    for declaration_row in declaration_rows:
        rows_by_model.setdefault(declaration_row.chat_model_id, []).append(declaration_row)
    return {
        model_name: ChatModelOffer(
            worker_count=len(model_rows),
            max_context_length=max(model_row.chat_max_context_length or 0 for model_row in model_rows),
            max_length=max(model_row.chat_max_length or 0 for model_row in model_rows),
            features=tuple(sorted({feature for model_row in model_rows for feature in model_row.chat_features or ()})),
            created=min((model_row.created for model_row in model_rows if model_row.created is not None), default=None),
        )
        for model_name, model_rows in rows_by_model.items()
    }


def get_text_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError:
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    # The submit settlement always walks procgen -> wp -> requesting user and
    # procgen -> worker -> owning user, so loading them here folds four lazy
    # SELECT round trips into the lookup itself.
    return (
        db.session.query(TextProcessingGeneration)
        .options(
            joinedload(TextProcessingGeneration.wp).joinedload(TextWaitingPrompt.user),
            joinedload(TextProcessingGeneration.worker).joinedload(TextWorker.user),
        )
        .filter_by(id=procgen_uuid)
        .first()
    )


def get_all_text_wps():
    return (
        db.session.query(TextWaitingPrompt)
        .filter(
            TextWaitingPrompt.active.is_(True),
            TextWaitingPrompt.faulted.is_(False),
            TextWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_cached_worker_performance():
    if hr.horde_r is None:
        return [p.performance for p in db.session.query(WorkerPerformance.performance).all()]
    perf_cache = hr.horde_r.get("worker_performances_cache")
    if not perf_cache:
        return refresh_worker_performances_cache()
    try:
        models_ret = json.loads(perf_cache)
    except TypeError:
        logger.error("performance cache could not be loaded: {perf_cache}")
        return refresh_worker_performances_cache()
    if models_ret is None:
        return refresh_worker_performances_cache()
    return models_ret


# TODO: Convert below three functions into a general "cached db request" (or something) class
# Which I can reuse to cache the results of other requests
def retrieve_worker_performances():
    avg_perf = db.session.query(func.avg(WorkerPerformance.performance)).scalar()
    avg_perf = 0 if avg_perf is None else round(avg_perf, 2)
    return avg_perf  # noqa RET504


def refresh_worker_performances_cache():
    avg_perf = retrieve_worker_performances()
    try:
        hr.horde_r_setex("worker_performances_avg_cache", timedelta(seconds=30), avg_perf)
    except Exception as e:
        logger.debug(f"Error when trying to set worker performances cache: {e}. Retrieving from DB.")
    return avg_perf


def query_prioritized_text_wps():
    return query_prioritized_wps()


def prune_expired_stats():
    # clear up old requests (older than 5 mins)
    db.session.query(stats.FulfillmentPerformance).filter(
        stats.FulfillmentPerformance.created < datetime.utcnow() - timedelta(seconds=60),
    ).delete(synchronize_session=False)
    db.session.query(stats.ModelPerformance).filter(
        stats.ModelPerformance.created < datetime.utcnow() - timedelta(hours=1),
    ).delete(synchronize_session=False)
    db.session.commit()
    logger.debug("Pruned Expired Stats")
