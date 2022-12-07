import time
import uuid
import json
from datetime import datetime, timedelta
from sqlalchemy import func, or_, and_

from horde.classes.base.waiting_prompt import WPModels
from horde.classes.base.worker import WorkerModel
from horde.flask import db
from horde.logger import logger
from horde.vars import thing_name,thing_divisor
from horde.classes import User, Worker, Team, WaitingPrompt, ProcessingGeneration, WorkerPerformance, stats
from horde.utils import hash_api_key
from horde.horde_redis import horde_r
from horde.database.classes import FakeWPRow, PrimaryTimedFunction

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

def count_active_workers():
    active_workers = db.session.query(func.sum(Worker.threads).label('threads')).filter(
        Worker.last_check_in > datetime.utcnow() - timedelta(seconds=300)
    ).first()
    if active_workers and active_workers.threads:
        return active_workers.threads
    return 0


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
    return(user)

def find_user_by_id(user_id):
    if int(user_id) == 0 and not ALLOW_ANONYMOUS:
        return(None)
    user = db.session.query(User).filter_by(id=user_id).first()
    return(user)

def find_user_by_api_key(api_key):
    if api_key == 0000000000 and not ALLOW_ANONYMOUS:
        return(None)
    user = db.session.query(User).filter_by(api_key=hash_api_key(api_key)).first()
    return(user)

def find_worker_by_name(worker_name):
    worker = db.session.query(Worker).filter_by(name=worker_name).first()
    return(worker)

def find_worker_by_id(worker_id):
    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID worker_id sent: '{worker_id}'.")
        return None
    worker = db.session.query(Worker).filter_by(id=uuid.UUID(worker_id)).first()
    return(worker)

def get_all_teams():
    return db.session.query(Team).all()

def find_team_by_id(team_id):
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

        # TODO
        models_dict[model_name]['queued'] = 0
        models_dict[model_name]['eta'] = 0
        models_dict[model_name]['performance'] = stats.get_model_avg(model_name) #TODO: Currently returns 1000000
        models_dict[model_name]['workers'] = []
    logger.trace(list(models_dict.values()))
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
            models_dict[model_name]['eta'] = -1
    return(list(models_dict.values()))

def retrieve_available_models():
    '''Retrieves model details from Redis cache, or from DB if cache is unavailable'''
    models_ret = json.loads(horde_r.get('models_cache'))
    if models_ret is None:
        models_ret = get_available_models()
    return(models_ret)

def transfer_kudos(source_user, dest_user, amount):
    if source_user.is_suspicious():
        return([0,'Something went wrong when sending kudos. Please contact the mods.'])
    if dest_user.is_suspicious():
        return([0,'Something went wrong when receiving kudos. Please contact the mods.'])
    if amount < 0:
        return([0,'Nice try...'])
    if amount > source_user.kudos - source_user.get_min_kudos():
        return([0,'Not enough kudos.'])
    source_user.modify_kudos(-amount, 'gifted')
    dest_user.modify_kudos(amount, 'received')
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
    return(ret_dict)

def retrieve_totals():
    '''Retrieves horde totals from Redis cache'''
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


def get_sorted_wp_filtered_to_worker(worker, models_list = None, blacklist = None): 
    # This is just the top 50 - Adjusted method to send Worker object. Filters to add.
    # TODO: Ensure the procgen table is NOT retrieved along with WPs (because it contains images)
    # TODO: Filter by (Worker in WP.workers) __ONLY IF__ len(WP.workers) >=1 
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ Worker.user.trusted == False
    # TODO: Filter by Worker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.Worker.gan_generate())
    worker_models = worker.get_model_names()
    return db.session.query(
        WaitingPrompt
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
    ).order_by(
        WaitingPrompt.extra_priority.desc(), 
        WaitingPrompt.created.asc()
    ).limit(100).all()
    # sorted_wp_list = sorted(wplist, key=lambda x: x.get_priority(), reverse=True)
    # final_wp_list = []
    # for wp in sorted_wp_list:
    #     if wp.needs_gen():
    #         final_wp_list.append(wp)
    # logger.debug([(wp,wp.get_priority()) for wp in final_wp_list])

    return final_wp_list

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


def get_wp_by_id(uuid):
    return db.session.query(WaitingPrompt).filter_by(id=uuid).first()

def get_progen_by_id(uuid):
    return db.session.query(ProcessingGeneration).filter_by(id=uuid).first()

def get_all_wps():
    return db.session.query(WaitingPrompt).filter_by(active=True).all()


def get_worker_performances():
    return [p.performance for p in db.session.query(WorkerPerformance.performance).all()]

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
