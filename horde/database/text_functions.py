import requests
import time
import uuid
import json
from datetime import datetime, timedelta
from sqlalchemy import func, or_, and_
from sqlalchemy.exc import DataError
from sqlalchemy.orm import noload, joinedload, load_only

from horde.classes.base.waiting_prompt import WPModels, WPAllowedWorkers
from horde.classes.base.worker import WorkerModel
from horde.flask import db, SQLITE_MODE
from horde.logger import logger
from horde.vars import thing_name, thing_divisor
from horde import vars as hv
from horde.classes.base.worker import WorkerPerformance
from horde.classes.kobold.worker import TextWorker
from horde.classes.base.user import User

# FIXME: Renamed for backwards compat. To fix later
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.processing_generation import TextProcessingGeneration
import horde.classes.base.stats as stats
from horde.utils import hash_api_key
from horde import horde_redis as hr
from horde.database.classes import FakeWPRow, PrimaryTimedFunction
from horde.database.functions import query_prioritized_wps
from horde.enums import State
from horde.bridge_reference import check_bridge_capability
from horde.model_reference import model_reference


# Should be overriden
def convert_things_to_kudos(things, **kwargs):
    # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
    kudos = round(things, 2)
    return kudos


def get_sorted_text_wp_filtered_to_worker(
    worker, models_list=None, priority_user_ids=None, page=0
):
    # This is just the top 100 - Adjusted method to send Worker object. Filters to add.
    # TODO: Filter by (Worker in WP.workers) __ONLY IF__ len(WP.workers) >=1
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ Worker.user.trusted == False
    # TODO: Filter by Worker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.Worker.gan_generate())
    PER_PAGE = 3  # how many requests we're picking up to filter further
    if len(models_list) >= 1:
        params = model_reference.get_text_model_multiplier(models_list[0])
        if params >= 20:
            slow_speed = 3
        elif params >= 13:
            slow_speed = 4
        else:
            slow_speed = 5
    else:
        slow_speed = 3
    final_wp_list = (
        db.session.query(TextWaitingPrompt)
        .options(noload(TextWaitingPrompt.processing_gens))
        .outerjoin(
            WPModels,
            WPAllowedWorkers,
        )
        .filter(
            TextWaitingPrompt.n > 0,
            TextWaitingPrompt.max_length <= worker.max_length,
            TextWaitingPrompt.max_context_length <= worker.max_context_length,
            TextWaitingPrompt.active == True,
            TextWaitingPrompt.faulted == False,
            TextWaitingPrompt.expiry > datetime.utcnow(),
            or_(
                TextWaitingPrompt.safe_ip == True,
                worker.allow_unsafe_ipaddr == True,
            ),
            or_(
                TextWaitingPrompt.nsfw == False,
                worker.nsfw == True,
            ),
            or_(
                WPModels.model.in_(models_list),
                WPModels.id.is_(None),
            ),
            or_(
                WPAllowedWorkers.id.is_(None),
                and_(
                    TextWaitingPrompt.worker_blacklist.is_(False),
                    WPAllowedWorkers.worker_id == worker.id,
                ),
                and_(
                    TextWaitingPrompt.worker_blacklist.is_(True),
                    WPAllowedWorkers.worker_id != worker.id,
                ),
            ),
            or_(
                worker.speed
                >= slow_speed,  # Slow speed is based on the model parameters used
                TextWaitingPrompt.slow_workers == True,
            ),
            or_(
                worker.maintenance == False,
                TextWaitingPrompt.user_id == worker.user_id,
            ),
        )
    )
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(
            TextWaitingPrompt.user_id.in_(priority_user_ids)
        )
    # logger.debug(final_wp_list)
    final_wp_list = (
        final_wp_list.order_by(
            TextWaitingPrompt.extra_priority.desc(), TextWaitingPrompt.created.asc()
        )
        .offset(PER_PAGE * page)
        .limit(PER_PAGE)
    )
    # logger.debug(final_wp_list.all())
    return (
        final_wp_list.populate_existing()
        .with_for_update(skip_locked=True, of=TextWaitingPrompt)
        .all()
    )


def get_text_wp_by_id(wp_id, lite=False):
    try:
        wp_uuid = uuid.UUID(wp_id)
    except ValueError as e:
        logger.debug(f"Non-UUID wp_id sent: '{wp_id}'.")
        return None
    if SQLITE_MODE:
        wp_uuid = str(wp_uuid)
    # lite version does not pull ProcGens
    if lite:
        query = db.session.query(TextWaitingPrompt).options(
            noload(TextWaitingPrompt.processing_gens)
        )
    else:
        query = db.session.query(TextWaitingPrompt)
    return query.filter_by(id=wp_uuid).first()


def get_text_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError as e:
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    return db.session.query(TextProcessingGeneration).filter_by(id=procgen_uuid).first()


def get_all_text_wps():
    return (
        db.session.query(TextWaitingPrompt)
        .filter(
            TextWaitingPrompt.active == True,
            TextWaitingPrompt.faulted == False,
            TextWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_cached_worker_performance():
    if hr.horde_r == None:
        return [
            p.performance for p in db.session.query(WorkerPerformance.performance).all()
        ]
    perf_cache = hr.horde_r.get(f"worker_performances_cache")
    if not perf_cache:
        return refresh_worker_performances_cache()
    try:
        models_ret = json.loads(perf_cache)
    except TypeError as e:
        logger.error(f"performance cache could not be loaded: {perf_cache}")
        return refresh_worker_performances_cache()
    if models_ret is None:
        return refresh_worker_performances_cache()
    return models_ret


# TODO: Convert below three functions into a general "cached db request" (or something) class
# Which I can reuse to cache the results of other requests
def retrieve_worker_performances():
    avg_perf = db.session.query(func.avg(WorkerPerformance.performance)).scalar()
    if avg_perf is None:
        avg_perf = 0
    else:
        avg_perf = round(avg_perf, 2)
    return avg_perf


def refresh_worker_performances_cache():
    avg_perf = retrieve_worker_performances()
    try:
        hr.horde_r_setex(
            f"worker_performances_avg_cache", timedelta(seconds=30), avg_perf
        )
    except Exception as e:
        logger.debug(
            f"Error when trying to set worker performances cache: {e}. Retrieving from DB."
        )
    return avg_perf


def query_prioritized_text_wps():
    return query_prioritized_wps()


def prune_expired_stats():
    # clear up old requests (older than 5 mins)
    db.session.query(stats.FulfillmentPerformance).filter(
        stats.FulfillmentPerformance.created < datetime.utcnow() - timedelta(seconds=60)
    ).delete(synchronize_session=False)
    db.session.query(stats.ModelPerformance).filter(
        stats.ModelPerformance.created < datetime.utcnow() - timedelta(hours=1)
    ).delete(synchronize_session=False)
    db.session.commit()
    logger.debug("Pruned Expired Stats")
