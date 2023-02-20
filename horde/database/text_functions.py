import requests
import time
import uuid
import json
from datetime import datetime, timedelta
from sqlalchemy import func, or_, and_
from sqlalchemy.exc import DataError
from sqlalchemy.orm import noload, joinedload, load_only

from horde.classes.base.waiting_prompt import WPModels
from horde.classes.base.worker import WorkerModel
from horde.flask import db, SQLITE_MODE
from horde.logger import logger
from horde.vars import thing_name,thing_divisor
from horde import vars as hv
from horde.classes.base.worker import WorkerPerformance
from horde.classes.kobold.worker import TextWorker
from horde.classes.base.user import User
# FIXME: Renamed for backwards compat. To fix later
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.processing_generation import TextProcessingGeneration
import horde.classes.base.stats as stats
from horde.utils import hash_api_key
from horde.horde_redis import horde_r
from horde.database.classes import FakeWPRow, PrimaryTimedFunction
from horde.database.functions import query_prioritized_wps
from horde.enums import State
from horde.bridge_reference import check_bridge_capability

# Should be overriden
def convert_things_to_kudos(things, **kwargs):
    # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
    kudos = round(things,2)
    return(kudos)


def count_waiting_requests(user, models = None):
    # TODO: This is incorrect. It should count the amount of waiting 'n' + in-progress generations too
    # Currently this is just counting how many requests, but each requests can have more than 1 image waiting
    if not models: models = []
    if len(models):
        return db.session.query(
            WPModels.id,
        ).join(
            WaitingPrompt
        ).filter(
            WPModels.model.in_(models),
            WaitingPrompt.user_id == user.id,
            WaitingPrompt.faulted == False,
            WaitingPrompt.n >= 1, 
        ).group_by(WPModels.id).count()
    else:
        return db.session.query(
            WaitingPrompt
        ).filter(
            WaitingPrompt.user_id == user.id,
            WaitingPrompt.faulted == False,
            WaitingPrompt.n >= 1, 
        ).count()


def get_sorted_text_wp_filtered_to_worker(worker, models_list = None, priority_user_ids=None): 
    # This is just the top 100 - Adjusted method to send Worker object. Filters to add.
    # TODO: Ensure the procgen table is NOT retrieved along with WPs (because it contains images)
    # TODO: Filter by (Worker in WP.workers) __ONLY IF__ len(WP.workers) >=1 
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ Worker.user.trusted == False
    # TODO: Filter by Worker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.Worker.gan_generate())
    final_wp_list = db.session.query(
        TextWaitingPrompt
    ).options(
        noload(TextWaitingPrompt.processing_gens)
    ).outerjoin(
        WPModels
    ).filter(
        TextWaitingPrompt.n > 0,
        
        TextWaitingPrompt.max_length <= worker.max_length,
        TextWaitingPrompt.max_content_length <= worker.max_content_length,
        TextWaitingPrompt.active == True,
        TextWaitingPrompt.faulted == False,
        TextWaitingPrompt.expiry > datetime.utcnow(),
        or_(
            TextWaitingPrompt.safe_ip == True,
            and_(
                TextWaitingPrompt.safe_ip == False,
                worker.allow_unsafe_ipaddr == True,
            ),
        ),
        or_(
            TextWaitingPrompt.nsfw == False,
            and_(
                TextWaitingPrompt.nsfw == True,
                worker.nsfw == True,
            ),
        ),
        or_(
            TextWaitingPrompt.nsfw == False,
            and_(
                TextWaitingPrompt.nsfw == True,
                worker.nsfw == True,
            ),
        ),
        or_(
            WPModels.model.in_(models_list),
            WPModels.id.is_(None),
        ),
    )
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(TextWaitingPrompt.user_id.in_(priority_user_ids))
    # logger.debug(final_wp_list)
    final_wp_list = final_wp_list.order_by(
        TextWaitingPrompt.extra_priority.desc(), 
        TextWaitingPrompt.created.asc()
    ).limit(50)
    logger.debug(final_wp_list.all())
    return final_wp_list.all()


def get_sorted_forms_filtered_to_worker(worker, forms_list = None, priority_user_ids = None, excluded_forms = None): 
    # Currently the worker is not being used, but I leave it being sent in case we need it later for filtering
    if forms_list == None:
        forms_list = []
    final_interrogation_query = db.session.query(
        InterrogationForms
    ).join(
        Interrogation
    ).filter(
        InterrogationForms.state == State.WAITING,
        InterrogationForms.name.in_(forms_list),
        InterrogationForms.expiry == None,
        Interrogation.source_image != None,
        or_(
            Interrogation.safe_ip == True,
            and_(
                Interrogation.safe_ip == False,
                worker.allow_unsafe_ipaddr == True,
            ),
        ),
        or_(
            worker.maintenance == False,
            and_(
                worker.maintenance == True,
                Interrogation.user_id == worker.user_id,
            ),
        ),
    ).order_by(
        Interrogation.extra_priority.desc(), 
        Interrogation.created.asc()
    )
    if priority_user_ids != None:
        final_interrogation_query.filter(Interrogation.user_id.in_(priority_user_ids))
    # We use this to not retrieve already retrieved with priority_users 
    retrieve_limit = 100
    if excluded_forms != None:
        excluded_form_ids = [f.id for f in excluded_forms]
        # We only want to retrieve 100 requests, so we reduce the amount to retrieve from non-prioritized
        # requests by the prioritized requests.
        retrieve_limit -= len(excluded_form_ids)
        if retrieve_limit <= 0:
            retrieve_limit = 1
        final_interrogation_query.filter(InterrogationForms.id.not_in(excluded_form_ids))
    final_interrogation_list = final_interrogation_query.limit(retrieve_limit).all()
    # logger.debug(final_interrogation_query)
    return final_interrogation_list


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
        query = db.session.query(TextWaitingPrompt
        ).options(
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


def get_interrogation_by_id(i_id):
    try:
        i_uuid = uuid.UUID(i_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID i_id sent: '{i_id}'.")
        return None
    if SQLITE_MODE:
        i_uuid = str(i_uuid)
    return db.session.query(Interrogation).filter_by(id=i_uuid).first()


def get_form_by_id(form_id):
    try:
        form_uuid = uuid.UUID(form_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID form_id sent: '{form_id}'.")
        return None
    if SQLITE_MODE:
        form_uuid = str(form_uuid)
    return db.session.query(InterrogationForms).filter_by(id=form_uuid).first()


def get_all_wps():
    return db.session.query(WaitingPrompt).filter_by(active=True).all()


def get_cached_worker_performance():
    if horde_r == None:
        return [p.performance for p in db.session.query(WorkerPerformance.performance).all()]
    perf_cache = horde_r.get(f'worker_performances_cache')
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

#TODO: Convert below three functions into a general "cached db request" (or something) class
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
        horde_r.setex(f'worker_performances_avg_cache', timedelta(seconds=30), avg_perf)
    except Exception as err:
        logger.debug(f"Error when trying to set worker performances cache: {e}. Retrieving from DB.")
    return avg_perf

def wp_has_valid_workers(wp, limited_workers_ids = None):
    if not limited_workers_ids: limited_workers_ids = []
    # FIXME: Too heavy
    # TODO: Redis cached
    return True
    worker_found = False
    for worker in get_active_workers():
        if len(limited_workers_ids) and worker not in wp.get_worker_ids():
            continue
        if worker.can_generate(wp)[0]:
            worker_found = True
            break
    return worker_found

def query_prioritized_text_wps():
    return query_prioritized_wps()


def prune_expired_stats():
    # clear up old requests (older than 5 mins)
    db.session.query(
        stats.FulfillmentPerformance
    ).filter(
        stats.FulfillmentPerformance.created < datetime.utcnow() - timedelta(seconds=60)
    ).delete(synchronize_session=False)
    db.session.query(
        stats.ModelPerformance
    ).filter(
        stats.ModelPerformance.created < datetime.utcnow() - timedelta(hours=1)
    ).delete(synchronize_session=False)
    db.session.commit()
    logger.debug("Pruned Expired Stats")

