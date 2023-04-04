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
from horde.vars import thing_name
from horde import vars as hv
from horde.classes.base.worker import WorkerPerformance
from horde.classes.stable.worker import ImageWorker
from horde.classes.kobold.worker import TextWorker
from horde.classes.base.user import User
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.processing_generation import TextProcessingGeneration
import horde.classes.base.stats as stats
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.base.team import Team
from horde.classes.base.detection import Filter
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.utils import hash_api_key, validate_regex
from horde import horde_redis as hr
from horde.database.classes import FakeWPRow, PrimaryTimedFunction
from horde.enums import State
from horde.bridge_reference import check_bridge_capability, check_sampler_capability

from horde.classes.base.team import find_team_by_id, find_team_by_name, get_all_teams
from horde.model_reference import model_reference
from horde.classes.base.settings import HordeSettings

ALLOW_ANONYMOUS = True
WORKER_CLASS_MAP = {
    "image": ImageWorker,
    "text": TextWorker,
    "interrogation": InterrogationWorker,
}
WP_CLASS_MAP = {
    "image": ImageWaitingPrompt,
    "text": TextWaitingPrompt,
}

def get_anon():
    return find_user_by_api_key('anon')

#TODO: Switch this to take this node out of operation instead?
# Or maybe just delete this
def shutdown(seconds):
    if seconds > 0:
        logger.critical(f"Initiating shutdown in {seconds} seconds")
        time.sleep(seconds)

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
    top_worker = db.session.query(ImageWorker).order_by(
        ImageWorker.contributions.desc()
    ).first()
    return top_worker


def get_active_workers(worker_type=None):
    active_workers = []
    if worker_type is None or worker_type == "image":
        active_workers += db.session.query(ImageWorker).filter(
            ImageWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)
        ).all()
    if worker_type is None or worker_type == "text":
        active_workers += db.session.query(TextWorker).filter(
            TextWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)
        ).all()
    if worker_type is None or worker_type == "interrogation":
        active_workers += db.session.query(InterrogationWorker).filter(
            InterrogationWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)
        ).all()
    return active_workers

def count_active_workers(worker_class = "image"):
    worker_cache = hr.horde_r_get_json(f"count_active_workers_{worker_class}")
    if worker_cache:
        return tuple(worker_cache)
    WorkerClass = ImageWorker
    if worker_class == "interrogation":
        WorkerClass = InterrogationWorker
    if worker_class == "text":
        WorkerClass = TextWorker
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
        hr.horde_r_setex_json(f"count_active_workers_{worker_class}", timedelta(seconds=300), [active_workers,active_workers_threads.threads])
        return active_workers,active_workers_threads.threads
    return 0,0


def count_workers_on_ip(ip_addr):
    return db.session.query(ImageWorker).filter_by(ipaddr=ip_addr).count()


def count_workers_in_ipaddr(ipaddr):
    return count_workers_on_ip(ipaddr)


def get_total_usage():
    totals = {
        hv.thing_names['image']: 0,
        hv.thing_names['text']: 0,
        "image_fulfilments": 0,
        "text_fulfilments": 0,
    }
    result = db.session.query(func.sum(ImageWorker.contributions).label('contributions'), func.sum(ImageWorker.fulfilments).label('fulfilments')).first()
    if result:
        totals[hv.thing_names['image']] = result.contributions if result.contributions else 0
        totals["image_fulfilments"] = result.fulfilments if result.fulfilments else 0
    result = db.session.query(func.sum(TextWorker.contributions).label('contributions'), func.sum(TextWorker.fulfilments).label('fulfilments')).first()
    if result:
        totals[hv.thing_names['text']] = result.contributions if result.contributions else 0
        totals["text_fulfilments"] = result.fulfilments if result.fulfilments else 0
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

def find_worker_by_name(worker_name, worker_class=ImageWorker):
    worker = db.session.query(worker_class).filter_by(name=worker_name).first()
    return worker

def worker_name_exists(worker_name):
    for worker_class in [ImageWorker, TextWorker, InterrogationWorker]:
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
    worker = db.session.query(ImageWorker).filter_by(id=worker_uuid).first()
    if not worker:
        worker = db.session.query(TextWorker).filter_by(id=worker_uuid).first()
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
    wc = db.session.query(ImageWorker).filter_by(id=worker_uuid).count()
    if not wc:
        wc = db.session.query(TextWorker).filter_by(id=worker_uuid).count()
    if not wc:
        wc = db.session.query(InterrogationWorker).filter_by(id=worker_uuid).count()
    return wc

def get_available_models():
    models_dict = {}
    for model_type, worker_class, wp_class in [
        ("image", ImageWorker, ImageWaitingPrompt), 
        ("text", TextWorker, TextWaitingPrompt),
    ]:
        available_worker_models = db.session.query(
            WorkerModel.model,
            func.count(WorkerModel.model).label('total_models'), # TODO: This needs to be multiplied by this worker's threads
            # worker_class.id.label('worker_id') # TODO: make the query return a list or workers serving this model?
        ).join(
            worker_class,
        ).filter(
            worker_class.last_check_in > datetime.utcnow() - timedelta(seconds=300)
        ).group_by(WorkerModel.model).all()
        # logger.debug(available_worker_models)
        for model_row in available_worker_models:
            model_name = model_row.model
            models_dict[model_name] = {}
            models_dict[model_name]["name"] = model_name
            models_dict[model_name]["count"] = model_row.total_models
            models_dict[model_name]["type"] = model_type

            models_dict[model_name]['queued'] = 0
            models_dict[model_name]['eta'] = 0
            models_dict[model_name]['performance'] = stats.get_model_avg(model_name) #TODO: Currently returns 1000000
            models_dict[model_name]['workers'] = []

        # We don't want to report on any random model name a client might request
        known_models = list(model_reference.stable_diffusion_names)
        ophan_models = db.session.query(
            WPModels.model,
        ).join(
            wp_class,
        ).filter(
            WPModels.model.not_in(list(models_dict.keys())),
            WPModels.model.in_(known_models),
            wp_class.n > 0,
        ).group_by(WPModels.model).all()
        for model_row in ophan_models:
            model_name = model_row.model
            models_dict[model_name] = {}
            models_dict[model_name]["name"] = model_name
            models_dict[model_name]["count"] = 0
            models_dict[model_name]['queued'] = 0
            models_dict[model_name]["type"] = model_type
            models_dict[model_name]['eta'] = 0
            models_dict[model_name]['performance'] = stats.get_model_avg(model_name) #TODO: Currently returns 1000000
            models_dict[model_name]['workers'] = []
        things_per_model = count_things_per_model(wp_class)
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

def retrieve_available_models(model_type=None,min_count=None,max_count=None):
    '''Retrieves model details from Redis cache, or from DB if cache is unavailable'''
    if hr.horde_r is None:
        return get_available_models()
    model_cache = hr.horde_r_get('models_cache')
    try:
        models_ret = json.loads(model_cache)
    except TypeError as e:
        logger.error(f"Model cache could not be loaded: {model_cache}")
        return []
    if models_ret is None:
        models_ret = get_available_models()
    if model_type is not None:
        models_ret = [md for md in models_ret if md.get("type", "image")== model_type]
    if min_count is not None:
        models_ret = [md for md in models_ret if md["count"] >= min_count]
    if max_count is not None:
        models_ret = [md for md in models_ret if md["count"] <= max_count]
    return models_ret

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

def count_waiting_requests(user, models = None, request_type = "image"):
    wp_class = ImageWaitingPrompt
    if request_type == "text":
        wp_class = TextWaitingPrompt
       
    # TODO: This is incorrect. It should count the amount of waiting 'n' + in-progress generations too
    # Currently this is just counting how many requests, but each requests can have more than 1 image waiting
    if not models: models = []
    if len(models):
        return db.session.query(
            WPModels.id,
        ).join(
            wp_class
        ).filter(
            WPModels.model.in_(models),
            wp_class.user_id == user.id,
            wp_class.faulted == False,
            wp_class.n >= 1, 
        ).group_by(WPModels.id).count()
    else:
        return db.session.query(
            wp_class
        ).filter(
            wp_class.user_id == user.id,
            wp_class.faulted == False,
            wp_class.n >= 1, 
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



    # for wp in db.session.query(ImageWaitingPrompt).all():  # TODO this can likely be improved
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
    queued_images = f"queued_{hv.thing_names['image']}"
    queued_text = f"queued_{hv.thing_names['text']}"
    queued_forms = f"queued_forms"
    ret_dict = {
        "queued_requests": 0,
        "queued_text_requests": 0,
        queued_images: 0,
        queued_text: 0,
    }
    all_image_wp_counts = db.session.query(
        ImageWaitingPrompt.id,
        (func.sum(ImageWaitingPrompt.n) + func.count(ImageProcessingGeneration.wp_id)).label("total_count"),
        func.sum(ImageWaitingPrompt.things).label("total_things")
    ).outerjoin(
        ImageProcessingGeneration,
        and_(
            ImageWaitingPrompt.id == ImageProcessingGeneration.wp_id,
            ImageProcessingGeneration.generation == None
        )
    ).filter(
        ImageWaitingPrompt.n > 0,
        ImageWaitingPrompt.faulted == False
    ).group_by(
        ImageWaitingPrompt.id
    ).subquery('all_image_wp_counts')
    total_image_sum = db.session.query(
        func.sum(all_image_wp_counts.c.total_count).label("total_count_sum"),
        func.sum(all_image_wp_counts.c.total_things).label("total_things_sum")
    ).select_from(all_image_wp_counts).one()
    ret_dict["queued_requests"] = int(total_image_sum.total_count_sum) if total_image_sum.total_count_sum is not None else 0
    ret_dict[queued_images] = round(int(total_image_sum.total_things_sum) / hv.thing_divisors["image"], 2) if total_image_sum.total_things_sum is not None else 0
    all_text_wp_counts = db.session.query(
        TextWaitingPrompt.id,
        (func.sum(TextWaitingPrompt.n) + func.count(TextProcessingGeneration.wp_id)).label("total_count"),
        func.sum(TextWaitingPrompt.things).label("total_things")
    ).outerjoin(
        TextProcessingGeneration,
        and_(
            TextWaitingPrompt.id == TextProcessingGeneration.wp_id,
            TextProcessingGeneration.generation == None
        )
    ).filter(
        TextWaitingPrompt.n > 0,
        TextWaitingPrompt.faulted == False
    ).group_by(
        TextWaitingPrompt.id
    ).subquery('all_text_wp_counts')
    total_text_sum = db.session.query(
        func.sum(all_text_wp_counts.c.total_count).label("total_count_sum"),
        func.sum(all_text_wp_counts.c.total_things).label("total_things_sum")
    ).select_from(all_text_wp_counts).one()
    ret_dict["queued_text_requests"] = int(total_text_sum.total_count_sum) if total_text_sum.total_count_sum is not None else 0
    ret_dict[queued_text] = int(total_text_sum.total_things_sum) / hv.thing_divisors["text"] if total_text_sum.total_things_sum is not None else 0
    ret_dict[queued_forms] = db.session.query(
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
    if hr.horde_r is None:
        return count_totals()
    totals_ret = hr.horde_r_get('totals_cache')
    if totals_ret is None:
        return {
            "queued_requests": 0,
            "queued_text_requests": 0,
            f"queued_{hv.thing_names['image']}": 0,
            f"queued_{hv.thing_names['text']}": 0,
            f"queued_forms": 0,
        }
    return(json.loads(totals_ret))


def get_organized_wps_by_model(wp_class):
    org = {}
    #TODO: Offload the sorting to the DB through join() + SELECT statements
    all_wps = db.session.query(
        wp_class
    ).filter(
        wp_class.faulted == False,
        wp_class.n >= 1,
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

def count_things_per_model(wp_class):
    things_per_model = {}
    org = get_organized_wps_by_model(wp_class)
    for model in org:
        for wp in org[model]:
            current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
            if current_wp_queue > 0:
                things_per_model[model] = things_per_model.get(model,0) + wp.things
        things_per_model[model] = round(things_per_model.get(model,0),2)
    return(things_per_model)


def get_sorted_wp_filtered_to_worker(worker, models_list = None, blacklist = None, priority_user_ids=None): 
    # This is just the top 100 - Adjusted method to send ImageWorker object. Filters to add.
    # TODO: Ensure the procgen table is NOT retrieved along with WPs (because it contains images)
    # TODO: Filter by (ImageWorker in WP.workers) __ONLY IF__ len(WP.workers) >=1 
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ ImageWorker.user.trusted == False
    # TODO: Filter by ImageWorker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.ImageWorker.gan_generate())
    final_wp_list = db.session.query(
        ImageWaitingPrompt
    ).options(
        noload(ImageWaitingPrompt.processing_gens)
    ).outerjoin(
        WPModels
    ).filter(
        ImageWaitingPrompt.n > 0,
        or_(
            WPModels.model.in_(models_list),
            WPModels.id.is_(None),
        ),
        ImageWaitingPrompt.width * ImageWaitingPrompt.height <= worker.max_pixels,
        ImageWaitingPrompt.active == True,
        ImageWaitingPrompt.faulted == False,
        ImageWaitingPrompt.expiry > datetime.utcnow(),
        or_(
            ImageWaitingPrompt.source_image == None,
            and_(
                ImageWaitingPrompt.source_image != None,
                worker.allow_img2img == True,
            ),
            
        ),
        or_(
            ImageWaitingPrompt.safe_ip == True,
            and_(
                ImageWaitingPrompt.safe_ip == False,
                worker.allow_unsafe_ipaddr == True,
            ),
        ),
        or_(
            ImageWaitingPrompt.nsfw == False,
            and_(
                ImageWaitingPrompt.nsfw == True,
                worker.nsfw == True,
            ),
        ),
        or_(
            worker.maintenance == False,
            and_(
                worker.maintenance == True,
                ImageWaitingPrompt.user_id == worker.user_id,
            ),
        ),
        or_(
            worker.bridge_version >= 8,
            and_(
                worker.bridge_version < 8,
                ImageWaitingPrompt.r2 == False,
            ),
        ),
        or_(
            worker.speed >= 500000, # 0.5 MPS/s
            ImageWaitingPrompt.slow_workers == True,
        ),
    )
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(ImageWaitingPrompt.user_id.in_(priority_user_ids))
    # logger.debug(final_wp_list)
    final_wp_list = final_wp_list.order_by(
        ImageWaitingPrompt.extra_priority.desc(), 
        ImageWaitingPrompt.created.asc()
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
        Interrogation.image_tiles <= worker.max_power,
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
        # or_(
        #     worker.speed < 10, # 10 seconds per form
        #     Interrogation.slow_workers == True,
        # ),
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
    logger.debug(final_interrogation_query)
    logger.debug(worker.speed)
    return final_interrogation_list

# Returns the queue position of the provided WP based on kudos
# Also returns the amount of things until the wp is generated
# Also returns the amount of different gens queued
def get_wp_queue_stats(wp):
    if not wp.needs_gen():
        return(-1,0,0)
    things_ahead_in_queue = 0
    n_ahead_in_queue = 0
    priority_sorted_list = retrieve_prioritized_wp_queue(wp.wp_type)
    # In case the primary thread has borked, we fall back to the DB
    if priority_sorted_list is None:
        logger.warning("Cached WP priority query does not exist. Falling back to direct DB query. Please check thread on primary!")
        priority_sorted_list = query_prioritized_wps(wp.wp_type)
    # logger.info(priority_sorted_list)
    for iter in range(len(priority_sorted_list)):
        iter_wp = priority_sorted_list[iter]
        queued_things = round(iter_wp.things * iter_wp.n/hv.thing_divisors["image"],2)
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
        query = db.session.query(ImageWaitingPrompt
        ).options(
            noload(ImageWaitingPrompt.processing_gens)
        )
    else:
        query = db.session.query(ImageWaitingPrompt)
    return query.filter_by(id=wp_uuid).first()

def get_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError as e: 
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    return db.session.query(ImageProcessingGeneration).filter_by(id=procgen_uuid).first()

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
    return db.session.query(ImageWaitingPrompt).filter(
        ImageWaitingPrompt.active == True,
        ImageWaitingPrompt.faulted == False,
        ImageWaitingPrompt.expiry > datetime.utcnow(),
    ).all()    

#TODO: Convert below three functions into a general "cached db request" (or something) class
# Which I can reuse to cache the results of other requests
def retrieve_worker_performances(worker_type = ImageWorker):
    avg_perf = db.session.query(
        func.avg(WorkerPerformance.performance)
    ).join(
        worker_type
    ).scalar()
    if avg_perf is None:
        avg_perf = 0
    else:
        avg_perf = round(avg_perf, 2)
    return avg_perf

def refresh_worker_performances_cache(request_type = "image"):
    ret_dict = {
        "image":retrieve_worker_performances(ImageWorker),
        "text": retrieve_worker_performances(TextWorker),
    }
    try:
        hr.horde_r_setex(f'worker_performances_avg_cache', timedelta(seconds=30), ret_dict["image"])
        hr.horde_r_setex(f'text_worker_performances_avg_cache', timedelta(seconds=30), ret_dict["text"])
    except Exception as e:
        logger.debug(f"Error when trying to set worker performances cache: {e}. Retrieving from DB.")
    return ret_dict[request_type]

def get_request_avg(request_type = "image"):
    if hr.horde_r == None:
        return retrieve_worker_performances(WORKER_CLASS_MAP[request_type])
    if request_type == "image":
        perf_cache = hr.horde_r_get(f'worker_performances_avg_cache')
    else:
        perf_cache = hr.horde_r_get(f'text_worker_performances_avg_cache')
    if not perf_cache:
        return refresh_worker_performances_cache(request_type)
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
def retrieve_prioritized_wp_queue(wp_type):
    cached_queue = hr.horde_r_get(f'{wp_type}_wp_cache')
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

def query_prioritized_wps(wp_type = "image"):
    waiting_prompt_type = WP_CLASS_MAP[wp_type]
    return db.session.query(
                waiting_prompt_type.id, 
                waiting_prompt_type.things, 
                waiting_prompt_type.n, 
                waiting_prompt_type.extra_priority, 
                waiting_prompt_type.created,
                waiting_prompt_type.expiry,
            ).filter(
                waiting_prompt_type.n > 0,
                waiting_prompt_type.faulted == False,
                waiting_prompt_type.active == True,
            ).order_by(
                waiting_prompt_type.extra_priority.desc(), waiting_prompt_type.created.asc()
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
    regex_string = '|'.join(all_filter_regex)
    if not validate_regex(regex_string):
        logger.error("Error when checking compiled regex!. Avoiding cache store")
        return ""
    return regex_string

def retrieve_regex_replacements(filter_type):
    all_filter_regex_query = db.session.query(Filter.regex, Filter.replacement).filter_by(filter_type=filter_type)
    all_filter_regex_dict = [
        {
            "regex": filter.regex,
            "replacement": filter.replacement,
        }
        for filter in all_filter_regex_query.all()
        if validate_regex(filter.regex)
    ]
    return all_filter_regex_dict
