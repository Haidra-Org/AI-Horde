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
from horde.classes import User, Worker, Team, WaitingPrompt, ProcessingGeneration, WorkerPerformance, stats
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.base.detection import Filter
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.utils import hash_api_key
from horde.horde_redis import horde_r
from horde.database.classes import FakeWPRow, PrimaryTimedFunction
from horde.enums import State
from horde.bridge_reference import check_bridge_capability, check_sampler_capability


ALLOW_ANONYMOUS = True

def get_anon():
    return find_user_by_api_key('anon')

#TODO: Switch this to take this node out of operation instead?
# Or maybe just delete this
def shutdown(seconds):
    if seconds > 0:
        logger.critical(f"Initiating shutdown in {seconds} seconds")
        time.sleep(seconds)
    logger.critical(f"DB written to disk. You can now SIGTERM.")

def get_top_contributor():
    top_contribution = 0
    top_contributor = None
    #TODO Exclude anon
    top_contributor = db.session.query(User).order_by(
        User.contributed_thing.desc()
    ).first()
    return top_contributor

def get_top_worker():
    top_worker = None
    top_worker_contribution = 0
    top_worker = db.session.query(Worker).order_by(
        Worker.contributions.desc()
    ).first()
    return top_worker


def get_active_workers():
    active_workers = db.session.query(Worker).filter(
        Worker.last_check_in > datetime.utcnow() - timedelta(seconds=300)
    ).all()
    return active_workers

def count_active_workers(worker_class = "Worker"):
    WorkerClass = Worker
    if worker_class == "InterrogationWorker":
        WorkerClass = InterrogationWorker
    active_workers = db.session.query(
        WorkerClass
    ).filter(
        WorkerClass.last_check_in > datetime.utcnow() - timedelta(seconds=300)
    ).count()
    active_workers_threads = db.session.query(
        func.sum(WorkerClass.threads).label('threads')
    ).filter(
        WorkerClass.last_check_in > datetime.utcnow() - timedelta(seconds=300)
    ).first()
    # logger.debug([worker_class,active_workers,active_workers_threads.threads])
    if active_workers and active_workers_threads.threads:
        return active_workers,active_workers_threads.threads
    return 0,0


def count_workers_on_ip(ip_addr):
    return db.session.query(Worker).filter_by(ipaddr=ip_addr).count()


def count_workers_in_ipaddr(ipaddr):
    return count_workers_on_ip(ipaddr)


def get_total_usage():
    totals = {
        thing_name: 0,
        "fulfilments": 0,
    }
    result = db.session.query(func.sum(Worker.contributions).label('contributions'), func.sum(Worker.fulfilments).label('fulfilments')).first()
    if result:
        totals[thing_name] = result.contributions if result.contributions else 0
        totals["fulfilments"] = result.fulfilments if result.fulfilments else 0
    form_result = result = db.session.query(func.sum(InterrogationWorker.fulfilments).label('forms')).first()
    if form_result:
        totals["forms"] = result.forms if result.forms else 0
    return totals


def find_user_by_oauth_id(oauth_id):
    if oauth_id == 'anon' and not ALLOW_ANONYMOUS:
        return None
    return db.session.query(User).filter_by(oauth_id=oauth_id).first()


def find_user_by_username(username):
    ulist = username.split('#')
    if int(ulist[-1]) == 0 and not ALLOW_ANONYMOUS:
        return(None)
    # This approach handles someone cheekily putting # in their username
    user = db.session.query(User).filter_by(id=int(ulist[-1])).first()
    return user

def find_user_by_id(user_id):
    if int(user_id) == 0 and not ALLOW_ANONYMOUS:
        return(None)
    user = db.session.query(User).filter_by(id=user_id).first()
    return user

def find_user_by_api_key(api_key):
    if api_key == 0000000000 and not ALLOW_ANONYMOUS:
        return(None)
    user = db.session.query(User).filter_by(api_key=hash_api_key(api_key)).first()
    return user

def find_worker_by_name(worker_name, worker_class=Worker):
    worker = db.session.query(worker_class).filter_by(name=worker_name).first()
    return worker

def worker_name_exists(worker_name):
    for worker_class in [Worker, InterrogationWorker]:
        worker = db.session.query(worker_class).filter_by(name=worker_name).count()
        if worker:
            return True
    return False

def find_worker_by_id(worker_id):
    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID worker_id sent: '{worker_id}'.")
        return None
    if SQLITE_MODE:
        worker_uuid = str(worker_uuid)
    worker = db.session.query(Worker).filter_by(id=worker_uuid).first()
    if not worker:
        worker = db.session.query(InterrogationWorker).filter_by(id=worker_uuid).first()
    return worker

def worker_exists(worker_id):
    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID worker_id sent: '{worker_id}'.")
        return None
    if SQLITE_MODE:
        worker_uuid = str(worker_uuid)
    wc = db.session.query(Worker).filter_by(id=worker_uuid).count()
    if not wc:
        wc = db.session.query(InterrogationWorker).filter_by(id=worker_uuid).count()
    return wc


def get_all_teams():
    return db.session.query(Team).all()

def find_team_by_id(team_id):
    try:
        team_uuid = uuid.UUID(team_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID team_id sent: '{team_id}'.")
        return None
    team = db.session.query(Team).filter_by(id=team_id).first()
    return(team)

def find_team_by_name(team_name):
    team = db.session.query(Team).filter(func.lower(Team.name) == func.lower(team_name)).first()
    return(team)

def get_available_models():
    models_dict = {}
    available_worker_models = db.session.query(
        WorkerModel.model,
        func.count(WorkerModel.model).label('total_models'), # TODO: This needs to be multiplied by this worker's threads
        # Worker.id.label('worker_id') # TODO: make the query return a list or workers serving this model?
    ).join(
        Worker,
    ).filter(
        Worker.last_check_in > datetime.utcnow() - timedelta(seconds=300)
    ).group_by(WorkerModel.model).all()

    for model_row in available_worker_models:
        model_name = model_row.model
        models_dict[model_name] = {}
        models_dict[model_name]["name"] = model_name
        models_dict[model_name]["count"] = model_row.total_models

        models_dict[model_name]['queued'] = 0
        models_dict[model_name]['eta'] = 0
        models_dict[model_name]['performance'] = stats.get_model_avg(model_name) #TODO: Currently returns 1000000
        models_dict[model_name]['workers'] = []

    # We don't want to report on any random model name a client might request
    try:
        r = requests.get("https://raw.githubusercontent.com/Sygil-Dev/nataili-model-reference/main/db.json", timeout=2).json()
        known_models = list(r.keys())
    except Exception:
        logger.error(f"Error when downloading known models list: {e}")
        known_models = []
    ophan_models = db.session.query(
        WPModels.model,
    ).join(
        WaitingPrompt,
    ).filter(
        WPModels.model.not_in(list(models_dict.keys())),
        WPModels.model.in_(known_models),
        WaitingPrompt.n > 0,
    ).group_by(WPModels.model).all()
    for model_row in ophan_models:
        model_name = model_row.model
        models_dict[model_name] = {}
        models_dict[model_name]["name"] = model_name
        models_dict[model_name]["count"] = 0
        models_dict[model_name]['queued'] = 0
        models_dict[model_name]['eta'] = 0
        models_dict[model_name]['performance'] = stats.get_model_avg(model_name) #TODO: Currently returns 1000000
        models_dict[model_name]['workers'] = []
    things_per_model = count_things_per_model()
    # If we request a lite_dict, we only want worker count per model and a dict format
    for model_name in things_per_model:
        # This shouldn't happen, but I'm checking anyway
        if model_name not in models_dict:
            # logger.debug(f"Tried to match non-existent wp model {model_name} to worker models. Skipping.")
            continue
        models_dict[model_name]['queued'] = things_per_model[model_name]
        total_performance_on_model = models_dict[model_name]['count'] * models_dict[model_name]['performance']
        # We don't want a division by zero when there's no workers for this model.
        if total_performance_on_model > 0:
            models_dict[model_name]['eta'] = int(things_per_model[model_name] / total_performance_on_model)
        else:
            models_dict[model_name]['eta'] = 10000
    return(list(models_dict.values()))

def retrieve_available_models():
    '''Retrieves model details from Redis cache, or from DB if cache is unavailable'''
    if horde_r is None:
        return []
    model_cache = horde_r.get('models_cache')
    try:
        models_ret = json.loads(model_cache)
    except TypeError as e:
        logger.error(f"Model cache could not be loaded: {model_cache}")
        return []
    if models_ret is None:
        models_ret = get_available_models()
    return(models_ret)

def transfer_kudos(source_user, dest_user, amount):
    if source_user.is_suspicious():
        return([0,'Something went wrong when sending kudos. Please contact the mods.'])
    if source_user.flagged:
        return([0,'The target account has been flagged for suspicious activity and tranferring kudos to them is blocked.'])
    if dest_user.is_suspicious():
        return([0,'Something went wrong when receiving kudos. Please contact the mods.'])
    if dest_user.flagged:
        return([0,'Your account has been flagged for suspicious activity. Please contact the mods.'])
    if amount < 0:
        return([0,'Nice try...'])
    if amount > source_user.kudos - source_user.get_min_kudos():
        return([0,'Not enough kudos.'])
    source_user.modify_kudos(-amount, 'gifted')
    dest_user.modify_kudos(amount, 'received')
    logger.info(f"{source_user.get_unique_alias()} transfered {amount} kudos to {dest_user.get_unique_alias()}")
    return([amount,'OK'])

def transfer_kudos_to_username(source_user, dest_username, amount):
    dest_user = find_user_by_username(dest_username)
    if not dest_user:
        return([0,'Invalid target username.'])
    if dest_user == get_anon():
        return([0,'Tried to burn kudos via sending to Anonymous. Assuming PEBKAC and aborting.'])
    if dest_user == source_user:
        return([0,'Cannot send kudos to yourself, ya monkey!'])
    kudos = transfer_kudos(source_user,dest_user, amount)
    return(kudos)

def transfer_kudos_from_apikey_to_username(source_api_key, dest_username, amount):
    source_user = find_user_by_api_key(source_api_key)
    if not source_user:
        return([0,'Invalid API Key.'])
    if source_user == get_anon():
        return([0,'You cannot transfer Kudos from Anonymous, smart-ass.'])
    kudos = transfer_kudos_to_username(source_user, dest_username, amount)
    return(kudos)

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

def count_waiting_interrogations(user):
    found_i_forms = db.session.query(
        InterrogationForms.state,
        Interrogation.user_id
    ).join(
        Interrogation
    ).filter(
        Interrogation.user_id == user.id,
        or_(
            InterrogationForms.state == State.WAITING,
            InterrogationForms.state == State.PROCESSING,
        ),
    )
    return found_i_forms.count()



    # for wp in db.session.query(WaitingPrompt).all():  # TODO this can likely be improved
    #     model_names = wp.get_model_names()
    #     #logger.warning(datetime.utcnow())
    #     if wp.user == user and not wp.is_completed():
    #         #logger.warning(datetime.utcnow())
    #         # If we pass a list of models, we want to count only the WP for these particular models.
    #         if len(models) > 0:
    #             matching_model = False
    #             for model in models:
    #                 if model in model_names:
    #                     #logger.warning(datetime.utcnow())
    #                     matching_model = True
    #                     break
    #             if not matching_model:
    #                 continue
    #         count += wp.n
    # #logger.warning(datetime.utcnow())
    # return(count)

def count_totals():
    queued_thing = f"queued_{thing_name}"
    ret_dict = {
        "queued_requests": 0,
        queued_thing: 0,
    }
    # TODO this can likely be improved
    current_wps = db.session.query(
        WaitingPrompt.id,
        WaitingPrompt.n,
        WaitingPrompt.faulted,
        WaitingPrompt.things,
    ).filter(
        WaitingPrompt.n > 0,
        WaitingPrompt.faulted == False
    ).all()
    for wp in current_wps:
        # TODO: Make this in one query above
        procgens_count = db.session.query(
            ProcessingGeneration.wp_id,
        ).filter(
            ProcessingGeneration.wp_id == wp.id
        ).count()
        current_wp_queue = wp.n + procgens_count
        ret_dict["queued_requests"] += current_wp_queue
        if current_wp_queue > 0:
            ret_dict[queued_thing] += wp.things * current_wp_queue / thing_divisor
    # We round the end result to avoid to many decimals
    ret_dict[queued_thing] = round(ret_dict[queued_thing],2)
    ret_dict["queued_forms"] = db.session.query(
        InterrogationForms.state,
    ).filter(
        or_(
            InterrogationForms.state == State.WAITING,
            InterrogationForms.state == State.PROCESSING,
        ),
    ).count()
    # logger.debug(ret_dict)
    return(ret_dict)

def retrieve_totals():
    '''Retrieves horde totals from Redis cache'''
    queued_thing = f"queued_{thing_name}"
    if horde_r is None:
        return {
            "queued_requests": 0,
            queued_thing: 0,
        }
    totals_ret = horde_r.get('totals_cache')
    if totals_ret is None:
        return {
            "queued_requests": 0,
            queued_thing: 0,
        }
    return(json.loads(totals_ret))


def get_organized_wps_by_model():
    org = {}
    #TODO: Offload the sorting to the DB through join() + SELECT statements
    all_wps = db.session.query(
        WaitingPrompt
    ).filter(
        WaitingPrompt.faulted == False,
        WaitingPrompt.n >= 1,
    ).all() # TODO this can likely be improved
    for wp in all_wps:
        # Each wp we have will be placed on the list for each of it allowed models (in case it's selected multiple)
        # This will inflate the overall expected times, but it shouldn't be by much.
        # I don't see a way to do this calculation more accurately though
        for model in wp.get_model_names():
            if model not in org:
                org[model] = []
            org[model].append(wp)
    return(org)    

def count_things_per_model():
    things_per_model = {}
    org = get_organized_wps_by_model()
    for model in org:
        for wp in org[model]:
            current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
            if current_wp_queue > 0:
                things_per_model[model] = things_per_model.get(model,0) + wp.things
        things_per_model[model] = round(things_per_model.get(model,0),2)
    return(things_per_model)


def get_sorted_wp_filtered_to_worker(worker, models_list = None, blacklist = None, priority_user_ids=None): 
    # This is just the top 100 - Adjusted method to send Worker object. Filters to add.
    # TODO: Ensure the procgen table is NOT retrieved along with WPs (because it contains images)
    # TODO: Filter by (Worker in WP.workers) __ONLY IF__ len(WP.workers) >=1 
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ Worker.user.trusted == False
    # TODO: Filter by Worker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.Worker.gan_generate())
    final_wp_list = db.session.query(
        WaitingPrompt
    ).options(
        noload(WaitingPrompt.processing_gens)
    ).join(
        WPModels
    ).filter(
        WaitingPrompt.n > 0,
        WPModels.model.in_(models_list),
        WaitingPrompt.width * WaitingPrompt.height <= worker.max_pixels,
        WaitingPrompt.active == True,
        WaitingPrompt.faulted == False,
        WaitingPrompt.expiry > datetime.utcnow(),
        or_(
            WaitingPrompt.source_image == None,
            and_(
                WaitingPrompt.source_image != None,
                worker.allow_img2img == True,
            ),
            
        ),
        or_(
            WaitingPrompt.safe_ip == True,
            and_(
                WaitingPrompt.safe_ip == False,
                worker.allow_unsafe_ipaddr == True,
            ),
        ),
        or_(
            WaitingPrompt.nsfw == False,
            and_(
                WaitingPrompt.nsfw == True,
                worker.nsfw == True,
            ),
        ),
        or_(
            worker.maintenance == False,
            and_(
                worker.maintenance == True,
                WaitingPrompt.user_id == worker.user_id,
            ),
        ),
        or_(
            worker.bridge_version >= 8,
            and_(
                worker.bridge_version < 8,
                WaitingPrompt.r2 == False,
            ),
        ),
    )
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(WaitingPrompt.user_id.in_(priority_user_ids))
    # logger.debug(final_wp_list)
    final_wp_list = final_wp_list.order_by(
        WaitingPrompt.extra_priority.desc(), 
        WaitingPrompt.created.asc()
    ).limit(50)
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

# Returns the queue position of the provided WP based on kudos
# Also returns the amount of things until the wp is generated
# Also returns the amount of different gens queued
def get_wp_queue_stats(wp):
    if not wp.needs_gen():
        return(-1,0,0)
    things_ahead_in_queue = 0
    n_ahead_in_queue = 0
    priority_sorted_list = retrieve_prioritized_wp_queue()
    # In case the primary thread has borked, we fall back to the DB
    if priority_sorted_list is None:
        logger.warning("Cached WP priority query does not exist. Falling back to direct DB query. Please check thread on primary!")
        priority_sorted_list = query_prioritized_wps()
    # logger.info(priority_sorted_list)
    for iter in range(len(priority_sorted_list)):
        iter_wp = priority_sorted_list[iter]
        queued_things = round(iter_wp.things * iter_wp.n/thing_divisor,2)
        things_ahead_in_queue += queued_things
        n_ahead_in_queue += iter_wp.n
        if iter_wp.id == wp.id:
            things_ahead_in_queue = round(things_ahead_in_queue,2)
            return(iter, things_ahead_in_queue, n_ahead_in_queue)
    # -1 means the WP is done and not in the queue
    return(-1,0,0)


def get_wp_by_id(wp_id, lite=False):
    try:
        wp_uuid = uuid.UUID(wp_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID wp_id sent: '{wp_id}'.")
        return None
    if SQLITE_MODE:
        wp_uuid = str(wp_uuid)
    # lite version does not pull ProcGens
    if lite:
        query = db.session.query(WaitingPrompt
        ).options(
            noload(WaitingPrompt.processing_gens)
        )
    else:
        query = db.session.query(WaitingPrompt)
    return query.filter_by(id=wp_uuid).first()

def get_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    return db.session.query(ProcessingGeneration).filter_by(id=procgen_uuid).first()

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

def get_request_avg():
    if horde_r == None:
        return retrieve_worker_performances()
    perf_cache = horde_r.get(f'worker_performances_avg_cache')
    if not perf_cache:
        return refresh_worker_performances_cache()
    perf_cache = float(perf_cache)
    return perf_cache

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

@logger.catch(reraise=True)
def retrieve_prioritized_wp_queue():
    if horde_r is None:
        return None
    cached_queue = horde_r.get('wp_cache')
    if cached_queue is None:
        return None
    try:
        retrieved_json_list = json.loads(cached_queue)
    except (TypeError, OverflowError) as e:
        logger.error(f"Failed deserializing with error: {e}")
        return None
    deserialized_wp_list = []
    for json_row in retrieved_json_list:
        fake_wp_row = FakeWPRow(json_row)
        deserialized_wp_list.append(fake_wp_row)
    # logger.debug(len(deserialized_wp_list))
    return deserialized_wp_list

def query_prioritized_wps():
    return db.session.query(
                WaitingPrompt.id, 
                WaitingPrompt.things, 
                WaitingPrompt.n, 
                WaitingPrompt.extra_priority, 
                WaitingPrompt.created,
                WaitingPrompt.expiry,
            ).filter(
                WaitingPrompt.n > 0,
                WaitingPrompt.faulted == False,
                WaitingPrompt.active == True,
            ).order_by(
                WaitingPrompt.extra_priority.desc(), WaitingPrompt.created.asc()
            ).all()


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


def compile_regex_filter(filter_type):
    all_filter_regex_query = db.session.query(Filter.regex).filter_by(filter_type=filter_type)
    all_filter_regex = [filter.regex for filter in all_filter_regex_query.all()]
    return '|'.join(all_filter_regex)