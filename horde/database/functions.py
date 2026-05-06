# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta

import logfire
from sqlalchemy import Boolean, and_, case, func, not_, or_
from sqlalchemy.orm import contains_eager, noload, selectinload

import horde.classes.base.stats as stats
from horde import vars as hv
from horde.bridge_reference import (
    check_bridge_capability,
    get_supported_samplers,
)
from horde.classes.base.detection import Filter
from horde.classes.base.style import Style, StyleCollection, StyleModel, StyleTag
from horde.classes.base.user import KudosTransferLog, User, UserRecords, UserSharedKey
from horde.classes.base.waiting_prompt import WPAllowedWorkers, WPModels
from horde.classes.base.worker import WorkerMessage, WorkerModel, WorkerPerformance
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.database.classes import FakeWPRow
from horde.enums import State
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.model_reference import model_reference
from horde.metrics import pop_query_duration
from horde.utils import hash_api_key, validate_regex

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
    return find_user_by_api_key("anon")


# TODO: Switch this to take this node out of operation instead?
# Or maybe just delete this
def shutdown(seconds):
    if seconds > 0:
        logger.critical(f"Initiating shutdown in {seconds} seconds")
        time.sleep(seconds)


def get_top_contributor():
    top_contributor = None
    top_contributor = (
        db.session.query(User)
        .join(UserRecords)
        .filter(
            UserRecords.record_type == "CONTRIBUTION",
            UserRecords.record == "image",
        )
        .order_by(UserRecords.value.desc())
        .first()
    )
    return top_contributor


def get_top_worker():
    top_worker = None
    top_worker = db.session.query(ImageWorker).order_by(ImageWorker.contributions.desc()).first()
    return top_worker


def get_active_workers(worker_type=None):
    active_workers = []
    if worker_type is None or worker_type == "image":
        active_workers += db.session.query(ImageWorker).filter(ImageWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)).all()
    if worker_type is None or worker_type == "text":
        active_workers += db.session.query(TextWorker).filter(TextWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)).all()
    if worker_type is None or worker_type == "interrogation":
        active_workers += (
            db.session.query(InterrogationWorker)
            .filter(InterrogationWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300))
            .all()
        )
    return active_workers


def count_active_workers(worker_class="image"):
    worker_cache = hr.horde_r_get_json(f"count_active_workers_{worker_class}")
    if worker_cache:
        return tuple(worker_cache)
    WorkerClass = ImageWorker
    if worker_class == "interrogation":
        WorkerClass = InterrogationWorker
    if worker_class == "text":
        WorkerClass = TextWorker
    active_workers = db.session.query(WorkerClass).filter(WorkerClass.last_check_in > datetime.utcnow() - timedelta(seconds=300)).count()
    active_workers_threads = (
        db.session.query(func.sum(WorkerClass.threads).label("threads"))
        .filter(WorkerClass.last_check_in > datetime.utcnow() - timedelta(seconds=300))
        .first()
    )
    # logger.debug([worker_class,active_workers,active_workers_threads.threads])
    if active_workers and active_workers_threads.threads:
        hr.horde_r_setex_json(
            f"count_active_workers_{worker_class}",
            timedelta(seconds=300),
            [active_workers, active_workers_threads.threads],
        )
        return active_workers, active_workers_threads.threads
    return 0, 0


def count_workers_on_ip(ip_addr):
    return db.session.query(ImageWorker).filter_by(ipaddr=ip_addr).count()


def count_workers_in_ipaddr(ipaddr):
    return count_workers_on_ip(ipaddr)


def get_total_usage():
    totals = {
        hv.thing_names["image"]: 0,
        hv.thing_names["text"]: 0,
        "image_fulfilments": 0,
        "text_fulfilments": 0,
    }
    result = db.session.query(
        func.sum(ImageWorker.contributions).label("contributions"),
        func.sum(ImageWorker.fulfilments).label("fulfilments"),
    ).first()
    if result:
        totals[hv.thing_names["image"]] = result.contributions if result.contributions else 0
        totals["image_fulfilments"] = result.fulfilments if result.fulfilments else 0
    result = db.session.query(
        func.sum(TextWorker.contributions).label("contributions"),
        func.sum(TextWorker.fulfilments).label("fulfilments"),
    ).first()
    if result:
        totals[hv.thing_names["text"]] = result.contributions if result.contributions else 0
        totals["text_fulfilments"] = result.fulfilments if result.fulfilments else 0
    form_result = result = db.session.query(func.sum(InterrogationWorker.fulfilments).label("forms")).first()
    if form_result:
        totals["forms"] = result.forms if result.forms else 0
    return totals


def find_user_by_oauth_id(oauth_id):
    if oauth_id == "anon" and not ALLOW_ANONYMOUS:
        return None
    return db.session.query(User).filter_by(oauth_id=oauth_id).first()


def find_user_by_username(username):
    ulist = username.split("#")
    try:
        if int(ulist[-1]) == 0 and not ALLOW_ANONYMOUS:
            return None
    except Exception:
        return None
    # This approach handles someone cheekily putting # in their username
    user = db.session.query(User).filter_by(id=int(ulist[-1])).filter(User.oauth_id != "<wiped>").first()
    return user


def find_user_by_id(user_id):
    if int(user_id) == 0 and not ALLOW_ANONYMOUS:
        return None
    user = db.session.query(User).filter_by(id=user_id).filter(User.oauth_id != "<wiped>").first()
    return user


def find_user_by_contact(contact):
    user_query = db.session.query(User).filter_by(contact=contact).filter(User.oauth_id != "<wiped>")
    selected_user = user_query.first()
    if user_query.count() > 1:
        logger.warning(f"Multiple users found with the same contact {contact}! Returning first found {selected_user.id}")
    return selected_user


def find_user_by_api_key(api_key):
    if api_key == 0000000000 and not ALLOW_ANONYMOUS:
        return None
    user = db.session.query(User).filter_by(api_key=hash_api_key(api_key)).filter(User.oauth_id != "<wiped>").first()
    return user


def find_user_by_sharedkey(shared_key):
    try:
        sharedkey_uuid = uuid.UUID(shared_key)
    except ValueError:
        return None
    if SQLITE_MODE:
        sharedkey_uuid = str(sharedkey_uuid)
    user = db.session.query(User).join(UserSharedKey).filter(UserSharedKey.id == shared_key).first()
    return user


def find_sharedkey(shared_key):
    try:
        sharedkey_uuid = uuid.UUID(shared_key)
    except ValueError:
        return None
    if SQLITE_MODE:
        sharedkey_uuid = str(sharedkey_uuid)
    sharedkey = db.session.query(UserSharedKey).filter(UserSharedKey.id == shared_key).first()
    return sharedkey


def find_worker_by_name(worker_name, worker_class=ImageWorker):
    worker = db.session.query(worker_class).filter_by(name=worker_name).first()
    return worker


def find_worker_id_by_name(worker_name):
    for worker_class in [ImageWorker, TextWorker, InterrogationWorker]:
        worker_id = db.session.query(worker_class.id).filter_by(name=worker_name).first()
        if worker_id:
            return worker_id


def worker_name_exists(worker_name):
    for worker_class in [ImageWorker, TextWorker, InterrogationWorker]:
        worker = db.session.query(worker_class).filter_by(name=worker_name).count()
        if worker:
            return True
    return False


def find_worker_by_id(worker_id):
    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError:
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
    except ValueError:
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


def workers_exist(worker_ids):
    """Given a list of worker_id strings, return the set of IDs that do NOT exist."""
    valid_uuids = {}
    invalid_ids = set()
    for wid in worker_ids:
        try:
            valid_uuids[wid] = uuid.UUID(wid)
        except ValueError:
            invalid_ids.add(wid)
    if not valid_uuids:
        return invalid_ids
    uuid_values = list(valid_uuids.values())
    if SQLITE_MODE:
        uuid_values = [str(u) for u in uuid_values]
    # Single query across all worker types using the polymorphic base
    from horde.classes.base.worker import Worker

    found_ids = {row[0] for row in db.session.query(Worker.id).filter(Worker.id.in_(uuid_values)).all()}
    if SQLITE_MODE:
        found_id_strs = {str(fid) for fid in found_ids}
        missing = {wid for wid, uid in valid_uuids.items() if str(uid) not in found_id_strs}
    else:
        missing = {wid for wid, uid in valid_uuids.items() if uid not in found_ids}
    return missing | invalid_ids


def get_available_models(filter_model_name: str = None):
    models_dict = {}
    available_worker_models = None

    if filter_model_name is not None:
        # Decode the filter_model_name from URL encoding
        # e.g., `aphrodite%2FNeverSleep%2FNoromaid-13b-v0.3` will become `aphrodite/NeverSleep/Noromaid-13b-v0.3`.
        filter_model_name = urllib.parse.unquote(filter_model_name)

    for model_type, worker_class, wp_class, procgen_class in [
        ("image", ImageWorker, ImageWaitingPrompt, ImageProcessingGeneration),
        ("text", TextWorker, TextWaitingPrompt, TextProcessingGeneration),
    ]:
        # To avoid abuse, when looking for filtered model names, we are searching only in known models and specials
        if (
            filter_model_name
            and filter_model_name not in model_reference.stable_diffusion_names
            and filter_model_name not in model_reference.testing_models
            and filter_model_name not in model_reference.text_model_names
            and "horde_special" not in filter_model_name
            and filter_model_name != "SDXL_beta::stability.ai#6901"
        ):
            continue
        # If we're doing a filter, and we've already found the model type, we don't want to look in other worker versions
        if filter_model_name and available_worker_models and len(available_worker_models) > 0:
            continue
        available_worker_models = (
            db.session.query(
                WorkerModel.model,
                func.sum(worker_class.threads).label("total_threads"),
                # worker_class.id.label('worker_id') # TODO: make the query return a list or workers serving this model?
            )
            .join(
                worker_class,
            )
            .filter(
                worker_class.last_check_in > datetime.utcnow() - timedelta(seconds=300),
                worker_class.maintenance == False,  # noqa E712
            )
        )
        if filter_model_name:
            available_worker_models = available_worker_models.filter(WorkerModel.model == filter_model_name)
        available_worker_models = available_worker_models.group_by(WorkerModel.model).all()
        # logger.debug(available_worker_models)
        for model_row in available_worker_models:
            model_name = model_row.model
            # We don't want to publicly display special models
            if not filter_model_name and "horde_special" in model_name:
                continue
            models_dict[model_name] = {}
            models_dict[model_name]["name"] = model_name
            models_dict[model_name]["count"] = model_row.total_threads
            models_dict[model_name]["type"] = model_type

            models_dict[model_name]["queued"] = 0
            models_dict[model_name]["jobs"] = 0
            models_dict[model_name]["eta"] = 0
            models_dict[model_name]["performance"] = stats.get_model_avg(model_name)
            models_dict[model_name]["workers"] = []

        known_models = [filter_model_name] if filter_model_name else list(model_reference.stable_diffusion_names)
        ophan_models = (
            db.session.query(
                WPModels.model,
            )
            .join(
                wp_class,
            )
            .filter(
                WPModels.model.not_in(list(models_dict.keys())),
                WPModels.model.in_(known_models),
                wp_class.n > 0,
            )
            .group_by(WPModels.model)
            .all()
        )
        for model_row in ophan_models:
            model_name = model_row.model
            models_dict[model_name] = {}
            models_dict[model_name]["name"] = model_name
            models_dict[model_name]["count"] = 0
            models_dict[model_name]["queued"] = 0
            models_dict[model_name]["jobs"] = 0
            models_dict[model_name]["type"] = model_type
            models_dict[model_name]["eta"] = 0
            models_dict[model_name]["performance"] = stats.get_model_avg(model_name)
            models_dict[model_name]["workers"] = []
        if filter_model_name:
            things_per_model, jobs_per_model = count_things_for_specific_model(
                wp_class,
                procgen_class,
                filter_model_name,
            )
        else:
            things_per_model, jobs_per_model = count_things_per_model(wp_class)
        # If we request a lite_dict, we only want worker count per model and a dict format
        for model_name in things_per_model:
            # This shouldn't happen, but I'm checking anyway
            if model_name not in models_dict:
                # logger.debug(f"Tried to match non-existent wp model {model_name} to worker models. Skipping.")
                continue
            models_dict[model_name]["queued"] = things_per_model[model_name]
            models_dict[model_name]["jobs"] = jobs_per_model[model_name]
            total_performance_on_model = models_dict[model_name]["count"] * models_dict[model_name]["performance"]
            # We don't want a division by zero when there's no workers for this model.
            if total_performance_on_model > 0:
                models_dict[model_name]["eta"] = int(things_per_model[model_name] / total_performance_on_model)
            else:
                models_dict[model_name]["eta"] = 10000
    return list(models_dict.values())


def retrieve_available_models(model_type=None, min_count=None, max_count=None, model_state="known"):
    """Retrieves model details from Redis cache, or from DB if cache is unavailable"""
    if hr.horde_r is None:
        return get_available_models()
    model_cache = hr.horde_r_get("models_cache")
    try:
        models_ret = json.loads(model_cache)
    except TypeError:
        logger.error(f"Model cache could not be loaded: {model_cache}")
        return []
    if models_ret is None:
        models_ret = get_available_models()
    if model_type is not None:
        models_ret = [md for md in models_ret if md.get("type", "image") == model_type]
    if min_count is not None:
        models_ret = [md for md in models_ret if md["count"] >= min_count]
    if max_count is not None:
        models_ret = [md for md in models_ret if md["count"] <= max_count]

    def check_model_state(model_name):
        if model_type is None:
            return True
        model_check = model_reference.is_known_image_model
        if model_type == "text":
            model_check = model_reference.is_known_text_model
        if model_state == "known" and model_check(model_name):
            return True
        if model_state == "custom" and not model_check(model_name):
            return True
        if model_state == "all":
            return True
        return False

    models_ret = [md for md in models_ret if check_model_state(md["name"])]

    return models_ret


def transfer_kudos(source_user, dest_user, amount):
    reverse_transfer = hr.horde_r_get(f"kudos_transfer_{dest_user.id}-{source_user.id}")
    if reverse_transfer:
        return [
            0,
            "This user transferred kudos to you very recently. Please wait at least 1 minute.",
            "TooFastKudosTransfers",
        ]
    if source_user.is_suspicious():
        return [
            0,
            "Something went wrong when sending kudos. Please contact the mods.",
            "FaultWhenKudosSending",
        ]
    if source_user.flagged:
        return [
            0,
            "The target account has been flagged for suspicious activity and tranferring kudos to them is blocked.",
            "SourceAccountFlagged",
        ]
    if source_user.education:
        return [
            0,
            "Education accounts cannot transfer kudos away",
            "EducationCannotSendKudos",
        ]
    if dest_user.is_suspicious():
        return [
            0,
            "Something went wrong when receiving kudos. Please contact the mods.",
            "FaultWhenKudosReceiving",
        ]
    if dest_user.flagged:
        return [0, "Your account has been flagged for suspicious activity. Please contact the mods.", "TargetAccountFlagged"]
    if dest_user.deleted:
        return [0, "This destination account has been scheduled for deletion and is disabled", "DeletedUser"]
    if source_user.deleted:
        return [0, "This source account has been scheduled for deletion and is disabled", "DeletedUser"]
    if amount < 0:
        return [0, "Nice try...", "NegativeKudosTransfer"]
    if amount > source_user.kudos - source_user.get_min_kudos():
        return [0, "Not enough kudos.", "KudosTransferNotEnough"]
    hr.horde_r_setex(f"kudos_transfer_{source_user.id}-{dest_user.id}", timedelta(seconds=60), 1)
    transfer_log = KudosTransferLog(
        source_id=source_user.id,
        dest_id=dest_user.id,
        kudos=amount,
    )
    db.session.add(transfer_log)
    db.session.commit()
    transfer_type = "gifted"
    if dest_user.education:
        transfer_type = "donated"
    source_user.modify_kudos(-amount, transfer_type)
    dest_user.modify_kudos(amount, "received")
    logger.info(f"{source_user.get_unique_alias()} transfered {amount} kudos to {dest_user.get_unique_alias()}")
    return [amount, "OK"]


def transfer_kudos_to_username(source_user, dest_username, amount):
    dest_user = find_user_by_username(dest_username)
    shared_key = None
    if not dest_user:
        shared_key = find_sharedkey(dest_username)
        if not shared_key:
            return [0, "Invalid target username.", "InvalidTargetUsername"]
        if shared_key.is_expired():
            return [0, "This shared key has expired", "SharedKeyExpired"]
        dest_user = shared_key.user
    if dest_user == get_anon():
        return [0, "Tried to burn kudos via sending to Anonymous. Assuming PEBKAC and aborting.", "KudosTransferToAnon"]
    if dest_user == source_user:
        return [0, "Cannot send kudos to yourself, ya monkey!", "KudosTransferToSelf"]
    kudos = transfer_kudos(source_user, dest_user, amount)
    if kudos[0] > 0 and shared_key is not None and shared_key.kudos != -1:
        shared_key.kudos += kudos[0]
        db.session.commit()
    return kudos


def transfer_kudos_from_apikey_to_username(source_api_key, dest_username, amount):
    source_user = find_user_by_api_key(source_api_key)
    if not source_user:
        return [0, "Invalid API Key.", "InvalidAPIKey"]
    if source_user == get_anon():
        return [0, "You cannot transfer Kudos from Anonymous, smart-ass.", "KudosTransferFromAnon"]
    kudos = transfer_kudos_to_username(source_user, dest_username, amount)
    return kudos


# Should be overriden
def convert_things_to_kudos(things, **kwargs):
    # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
    kudos = round(things, 2)
    return kudos


def count_waiting_requests(user, models=None, request_type="image"):
    with logfire.span("horde.db.count_waiting_requests", request_type=request_type, model_count=len(models) if models else 0):
        return _count_waiting_requests(user, models, request_type)


def _count_waiting_requests(user, models=None, request_type="image"):
    wp_class = ImageWaitingPrompt
    if request_type == "text":
        wp_class = TextWaitingPrompt

    if not models:
        models = []
    if len(models):
        known_model_query = (
            db.session.query(func.sum(wp_class.n))
            .select_from(
                WPModels,
            )
            .join(wp_class, WPModels.wp_id == wp_class.id)
            .filter(
                WPModels.model.in_(models),
                wp_class.user_id == user.id,
                wp_class.faulted == False,  # noqa E712
                wp_class.active == True,  # noqa E712
                wp_class.n >= 1,
            )
            .scalar()
        )
        if known_model_query is None:
            return 0
        logger.debug(known_model_query)
        return known_model_query
    else:
        unknown_model_query = (
            db.session.query(func.sum(wp_class.n))
            .filter(
                wp_class.user_id == user.id,
                wp_class.faulted == False,  # noqa E712
                wp_class.n >= 1,
            )
            .scalar()
        )
        if unknown_model_query is None:
            return 0
        return unknown_model_query


def count_waiting_interrogations(user):
    found_i_forms = (
        db.session.query(InterrogationForms.state, Interrogation.user_id)
        .join(Interrogation)
        .filter(
            Interrogation.user_id == user.id,
            or_(
                InterrogationForms.state == State.WAITING,
                InterrogationForms.state == State.PROCESSING,
            ),
        )
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
    queued_forms = "queued_forms"
    ret_dict = {
        "queued_requests": 0,
        "queued_text_requests": 0,
        queued_images: 0,
        queued_text: 0,
    }
    all_image_wp_counts = (
        db.session.query(
            ImageWaitingPrompt.id,
            (func.sum(ImageWaitingPrompt.n) + func.count(ImageProcessingGeneration.wp_id)).label("total_count"),
            func.sum(ImageWaitingPrompt.things).label("total_things"),
        )
        .outerjoin(
            ImageProcessingGeneration,
            and_(
                ImageWaitingPrompt.id == ImageProcessingGeneration.wp_id,
                ImageProcessingGeneration.generation == None,  # noqa E712
            ),
        )
        .filter(
            ImageWaitingPrompt.n > 0,
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.active == True,  # noqa E712
        )
        .group_by(ImageWaitingPrompt.id)
        .subquery("all_image_wp_counts")
    )
    total_image_sum = (
        db.session.query(
            func.sum(all_image_wp_counts.c.total_count).label("total_count_sum"),
            func.sum(all_image_wp_counts.c.total_things).label("total_things_sum"),
        )
        .select_from(all_image_wp_counts)
        .one()
    )
    ret_dict["queued_requests"] = int(total_image_sum.total_count_sum) if total_image_sum.total_count_sum is not None else 0
    ret_dict[queued_images] = (
        round(int(total_image_sum.total_things_sum) / hv.thing_divisors["image"], 2) if total_image_sum.total_things_sum is not None else 0
    )
    all_text_wp_counts = (
        db.session.query(
            TextWaitingPrompt.id,
            (func.sum(TextWaitingPrompt.n) + func.count(TextProcessingGeneration.wp_id)).label("total_count"),
            func.sum(TextWaitingPrompt.things).label("total_things"),
        )
        .outerjoin(
            TextProcessingGeneration,
            and_(
                TextWaitingPrompt.id == TextProcessingGeneration.wp_id,
                TextProcessingGeneration.generation == None,  # noqa E712
            ),
        )
        .filter(
            TextWaitingPrompt.n > 0,
            TextWaitingPrompt.faulted == False,  # noqa E712
            TextWaitingPrompt.active == True,  # noqa E712
        )
        .group_by(TextWaitingPrompt.id)
        .subquery("all_text_wp_counts")
    )
    total_text_sum = (
        db.session.query(
            func.sum(all_text_wp_counts.c.total_count).label("total_count_sum"),
            func.sum(all_text_wp_counts.c.total_things).label("total_things_sum"),
        )
        .select_from(all_text_wp_counts)
        .one()
    )
    ret_dict["queued_text_requests"] = int(total_text_sum.total_count_sum) if total_text_sum.total_count_sum is not None else 0
    ret_dict[queued_text] = (
        int(total_text_sum.total_things_sum) / hv.thing_divisors["text"] if total_text_sum.total_things_sum is not None else 0
    )
    ret_dict[queued_forms] = (
        db.session.query(
            InterrogationForms.state,
        )
        .filter(
            or_(
                InterrogationForms.state == State.WAITING,
                InterrogationForms.state == State.PROCESSING,
            ),
        )
        .count()
    )
    # logger.debug(ret_dict)
    return ret_dict


def retrieve_totals(ignore_cache=False):
    """Retrieves horde totals from Redis cache"""
    if ignore_cache or hr.horde_r is None:
        return count_totals()
    totals_ret = hr.horde_r_get("totals_cache")
    if totals_ret is None:
        return {
            "queued_requests": 0,
            "queued_text_requests": 0,
            f"queued_{hv.thing_names['image']}": 0,
            f"queued_{hv.thing_names['text']}": 0,
            "queued_forms": 0,
        }
    return json.loads(totals_ret)


def get_organized_wps_by_model(wp_class):
    org = {}
    # TODO: Offload the sorting to the DB through join() + SELECT statements
    all_wps = (
        db.session.query(wp_class)
        .filter(
            wp_class.active == True,  # noqa E712
            wp_class.faulted == False,  # noqa E712
            wp_class.n >= 1,
        )
        .all()
    )  # TODO this can likely be improved
    for wp in all_wps:
        # Each wp we have will be placed on the list for each of it allowed models (in case it's selected multiple)
        # This will inflate the overall expected times, but it shouldn't be by much.
        # I don't see a way to do this calculation more accurately though
        for model in wp.get_model_names():
            if "horde_special" in model:
                continue
            if model not in org:
                org[model] = []
            org[model].append(wp)
    return org


def count_things_per_model(wp_class):
    things_per_model = {}
    jobs_per_model = {}
    org = get_organized_wps_by_model(wp_class)
    for model in org:
        for wp in org[model]:
            current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
            if current_wp_queue > 0:
                things_per_model[model] = things_per_model.get(model, 0) + wp.things
                jobs_per_model[model] = jobs_per_model.get(model, 0) + current_wp_queue
        things_per_model[model] = round(things_per_model.get(model, 0), 2)
    return things_per_model, jobs_per_model


def count_things_for_specific_model(wp_class, procgen_class, model_name):
    things = {model_name: 0}
    jobs = {model_name: 0}
    all_wps_query = (
        db.session.query(
            wp_class.id.label("wp_id"),
            wp_class.n,
            wp_class.things,
            procgen_class.id.label("procgen_id"),
        )
        .join(
            WPModels,
        )
        .outerjoin(
            procgen_class,
        )
        .filter(
            wp_class.active == True,  # noqa E712
            wp_class.faulted == False,  # noqa E712
            wp_class.n >= 0,
            WPModels.model == model_name,
            or_(
                procgen_class.id == None,  # noqa E712
                and_(
                    procgen_class.generation == None,  # noqa E712
                    procgen_class.cancelled == False,  # noqa E712
                    procgen_class.faulted == False,  # noqa E712
                ),
            ),
        )
    )
    all_wps = all_wps_query.all()
    seen_wps = set()
    for wp in all_wps:
        current_wp_queue = 0
        if wp.wp_id not in seen_wps:
            current_wp_queue = wp.n
            seen_wps.add(wp.wp_id)
        if wp.procgen_id:
            current_wp_queue += 1
        things[model_name] += wp.things * current_wp_queue
        jobs[model_name] += current_wp_queue
    things[model_name] = round(things[model_name], 2)
    return things, jobs


@logger.catch(reraise=True)
def get_sorted_wp_filtered_to_worker(worker, models_list=None, blacklist=None, priority_user_ids=None, page=0):
    import time as _time

    t0 = _time.monotonic()
    # This is just the top 3 - Adjusted method to send ImageWorker object. Filters to add.
    # TODO: Filter by ImageWorker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.ImageWorker.gan_generate())
    PER_PAGE = 10  # how many requests we're picking up to filter further
    final_wp_list = (
        db.session.query(ImageWaitingPrompt)
        .options(noload(ImageWaitingPrompt.processing_gens))
        .outerjoin(WPModels, ImageWaitingPrompt.id == WPModels.wp_id)
        .outerjoin(WPAllowedWorkers, ImageWaitingPrompt.id == WPAllowedWorkers.wp_id)
        .filter(
            ImageWaitingPrompt.n > 0,
            ImageWaitingPrompt.active == True,  # noqa E712
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.expiry > datetime.utcnow(),
            ImageWaitingPrompt.width * ImageWaitingPrompt.height <= worker.max_pixels,
            or_(
                WPModels.model.in_(models_list),
                and_(
                    WPModels.id.is_(None),
                    not any("horde_special" in mname for mname in models_list),
                    "SDXL_beta::stability.ai#6901" not in models_list,
                ),
            ),
            or_(
                ImageWaitingPrompt.source_image == None,  # noqa E712
                worker.allow_img2img == True,  # noqa E712
            ),
            or_(
                ImageWaitingPrompt.source_processing.not_in(["inpainting", "outpainting"]),
                worker.allow_painting == True,  # noqa E712
            ),
            or_(
                ImageWaitingPrompt.extra_source_images == None,  # noqa E712
                check_bridge_capability("extra_source_images", worker.bridge_agent),
            ),
            or_(
                ImageWaitingPrompt.safe_ip == True,  # noqa E712
                worker.allow_unsafe_ipaddr == True,  # noqa E712
            ),
            or_(
                ImageWaitingPrompt.nsfw == False,  # noqa E712
                worker.nsfw == True,  # noqa E712
            ),
            or_(
                check_bridge_capability("r2", worker.bridge_agent),
                ImageWaitingPrompt.r2 == False,  # noqa E712
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("loras")),
                and_(
                    worker.allow_lora == True,  # noqa E712
                    check_bridge_capability("lora", worker.bridge_agent),
                ),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("tis")),
                check_bridge_capability("textual_inversion", worker.bridge_agent),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("post-processing")),
                and_(
                    worker.allow_post_processing == True,  # noqa E712
                    check_bridge_capability("post-processing", worker.bridge_agent),
                ),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("control_type")),
                and_(
                    worker.allow_controlnet == True,  # noqa E712
                    check_bridge_capability("controlnet", worker.bridge_agent),
                ),
            ),
            or_(
                worker.speed >= 500000,  # 0.5 MPS/s
                ImageWaitingPrompt.slow_workers == True,  # noqa E712
            ),
            or_(
                worker.extra_slow_worker is False,
                and_(
                    worker.extra_slow_worker is True,
                    ImageWaitingPrompt.extra_slow_workers.is_(True),
                ),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("transparent")),
                ImageWaitingPrompt.params["transparent"].astext.cast(Boolean).is_(False),
                and_(
                    check_bridge_capability("layer_diffuse", worker.bridge_agent),
                    worker.allow_sdxl_controlnet == True,  # noqa E712
                ),
            ),
        )
    )
    # logger.debug(final_wp_list)
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(ImageWaitingPrompt.user_id.in_(priority_user_ids))
        final_wp_list = final_wp_list.filter(
            # Workers in maintenance can still pick up their owner or their friends
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id.in_(priority_user_ids),
            ),
            or_(
                WPAllowedWorkers.id.is_(None),
                and_(
                    ImageWaitingPrompt.worker_blacklist.is_(False),
                    WPAllowedWorkers.worker_id == worker.id,
                ),
                and_(
                    ImageWaitingPrompt.worker_blacklist.is_(True),
                    WPAllowedWorkers.worker_id != worker.id,
                ),
            ),
        )
    else:
        final_wp_list = final_wp_list.filter(
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id == worker.user_id,
            ),
        )
        # If HORDE_REQUIRE_MATCHED_TARGETING is set to 1, we disable using WPAllowedWorkers
        # Targeted requests will only be picked up in the condition above as it will include the
        # filter to ensure the worker also has that user as a priority
        if os.getenv("HORDE_REQUIRE_MATCHED_TARGETING", "0") == "1":
            final_wp_list = final_wp_list.filter(
                or_(
                    WPAllowedWorkers.id.is_(None),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(True),
                        WPAllowedWorkers.worker_id != worker.id,
                    ),
                ),
            )
        else:
            final_wp_list = final_wp_list.filter(
                or_(
                    WPAllowedWorkers.id.is_(None),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(False),
                        WPAllowedWorkers.worker_id == worker.id,
                    ),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(True),
                        WPAllowedWorkers.worker_id != worker.id,
                    ),
                ),
            )

    # logger.debug(final_wp_list)
    final_wp_list = (
        final_wp_list.order_by(ImageWaitingPrompt.extra_priority.desc(), ImageWaitingPrompt.created.asc())
        .offset(PER_PAGE * page)
        .limit(PER_PAGE)
    )
    with logfire.span(
        "horde.db.get_sorted_wp",
        worker_id=str(worker.id),
        page=page,
        has_priority=priority_user_ids is not None,
    ):
        results = final_wp_list.populate_existing().with_for_update(skip_locked=True, of=ImageWaitingPrompt).all()
    pop_query_duration.record(_time.monotonic() - t0, {"horde.page": page})
    return results


def count_skipped_image_wp(worker, models_list=None, blacklist=None, priority_user_ids=None):
    ## Consolidated into a single query with conditional aggregation (replaces 15+ separate count queries).
    if models_list is None:
        models_list = []

    bridge_agent = worker.bridge_agent
    can_img2img = check_bridge_capability("img2img", bridge_agent)
    can_inpainting = check_bridge_capability("inpainting", bridge_agent)
    can_lora = check_bridge_capability("lora", bridge_agent)
    can_ti = check_bridge_capability("textual_inversion", bridge_agent)
    can_pp = check_bridge_capability("post-processing", bridge_agent)
    can_controlnet = check_bridge_capability("controlnet", bridge_agent)
    can_hires = check_bridge_capability("hires_fix", bridge_agent)
    can_return_ctrl = check_bridge_capability("return_control_map", bridge_agent)
    can_tiling = check_bridge_capability("tiling", bridge_agent)
    can_layer_diffuse = check_bridge_capability("layer_diffuse", bridge_agent)

    available_samplers = get_supported_samplers(bridge_agent, karras=False)
    available_karras_samplers = get_supported_samplers(bridge_agent, karras=True)

    # Base filters (shared across all counts)
    base_filters = [
        ImageWaitingPrompt.n > 0,
        ImageWaitingPrompt.active == True,  # noqa E712
        ImageWaitingPrompt.faulted == False,  # noqa E712
        ImageWaitingPrompt.expiry > datetime.utcnow(),
    ]

    # Build all conditional count expressions
    count_exprs = {}

    def count_distinct_wp(condition):
        # Distinct-by-WP avoids overcounting from WPModels/WPAllowedWorkers join fan-out.
        return func.count(func.distinct(case((condition, ImageWaitingPrompt.id), else_=None)))

    # models: WP specifies models that worker doesn't serve
    count_exprs["models"] = count_distinct_wp(and_(WPModels.model.not_in(models_list), WPModels.id != None))  # noqa E712

    # worker_id: WP targets specific workers (allowlist/blocklist)
    count_exprs["worker_id"] = count_distinct_wp(
        or_(
            WPAllowedWorkers.id != None,  # noqa E712
            and_(
                ImageWaitingPrompt.worker_blacklist.is_(False),
                WPAllowedWorkers.worker_id != worker.id,
            ),
            and_(
                ImageWaitingPrompt.worker_blacklist.is_(True),
                WPAllowedWorkers.worker_id == worker.id,
            ),
        ),
    )

    # max_pixels
    count_exprs["max_pixels"] = count_distinct_wp(ImageWaitingPrompt.width * ImageWaitingPrompt.height >= worker.max_pixels)

    # img2img (only counted if worker can't do it)
    if worker.allow_img2img is False or not can_img2img:
        count_exprs["_img2img_raw"] = count_distinct_wp(ImageWaitingPrompt.source_image != None)  # noqa E712

    # painting (only counted if worker can't do it)
    if worker.allow_painting is False or not can_inpainting:
        count_exprs["_painting_raw"] = count_distinct_wp(ImageWaitingPrompt.source_processing.in_(["inpainting", "outpainting"]))

    # unsafe_ip
    if worker.allow_unsafe_ipaddr is False:
        count_exprs["unsafe_ip"] = count_distinct_wp(ImageWaitingPrompt.safe_ip == False)  # noqa E712

    # nsfw
    if worker.nsfw is False:
        count_exprs["nsfw"] = count_distinct_wp(ImageWaitingPrompt.nsfw == True)  # noqa E712

    # lora
    if worker.allow_lora is False or not can_lora:
        count_exprs["_lora_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("loras"))

    # TI
    if not can_ti:
        count_exprs["_ti_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("tis"))

    # post-processing
    if worker.allow_post_processing is False or not can_pp:
        count_exprs["_pp_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("post-processing"))

    # controlnet
    if worker.allow_controlnet is False or not can_controlnet:
        count_exprs["_controlnet_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("control_type"))

    # performance (slow workers)
    if worker.speed <= 500000:
        count_exprs["_perf_slow"] = count_distinct_wp(ImageWaitingPrompt.slow_workers == False)  # noqa E712

    # performance (extra slow workers)
    if worker.extra_slow_worker is True:
        count_exprs["_perf_extra_slow"] = count_distinct_wp(ImageWaitingPrompt.extra_slow_workers == False)  # noqa E712

    # untrusted
    if worker.user.trusted is False:
        count_exprs["untrusted"] = count_distinct_wp(ImageWaitingPrompt.trusted_workers == True)  # noqa E712

    # bridge_version (sampler + capability checks)
    bv_conditions = []
    bv_conditions.append(
        and_(
            ImageWaitingPrompt.params["sampler_name"].astext.not_in(available_samplers),
            ImageWaitingPrompt.params["karras"].astext.cast(Boolean).is_(False),
        ),
    )
    bv_conditions.append(
        and_(
            ImageWaitingPrompt.params["sampler_name"].astext.not_in(available_karras_samplers),
            ImageWaitingPrompt.params["karras"].astext.cast(Boolean).is_(True),
        ),
    )
    if not can_hires:
        bv_conditions.append(ImageWaitingPrompt.params["hires_fix"].astext.cast(Boolean).is_(True))
    if not can_return_ctrl:
        bv_conditions.append(ImageWaitingPrompt.params["return_control_map"].astext.cast(Boolean).is_(True))
    if not can_tiling:
        bv_conditions.append(ImageWaitingPrompt.params["tiling"].astext.cast(Boolean).is_(True))
    if not can_layer_diffuse:
        bv_conditions.append(ImageWaitingPrompt.params["transparent"].astext.cast(Boolean).is_(True))

    count_exprs["_bv_sampler"] = count_distinct_wp(or_(*bv_conditions))

    # Execute single query
    query = (
        db.session.query(*count_exprs.values())
        .select_from(ImageWaitingPrompt)
        .outerjoin(WPModels, ImageWaitingPrompt.id == WPModels.wp_id)
        .outerjoin(WPAllowedWorkers, ImageWaitingPrompt.id == WPAllowedWorkers.wp_id)
        .filter(*base_filters)
    )

    # Keep skipped-count filtering behavior aligned with pop candidate selection.
    if priority_user_ids:
        query = query.filter(ImageWaitingPrompt.user_id.in_(priority_user_ids))
        query = query.filter(
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id.in_(priority_user_ids),
            ),
            or_(
                WPAllowedWorkers.id.is_(None),
                and_(
                    ImageWaitingPrompt.worker_blacklist.is_(False),
                    WPAllowedWorkers.worker_id == worker.id,
                ),
                and_(
                    ImageWaitingPrompt.worker_blacklist.is_(True),
                    WPAllowedWorkers.worker_id != worker.id,
                ),
            ),
        )
    else:
        query = query.filter(
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id == worker.user_id,
            ),
        )
        if os.getenv("HORDE_REQUIRE_MATCHED_TARGETING", "0") == "1":
            query = query.filter(
                or_(
                    WPAllowedWorkers.id.is_(None),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(True),
                        WPAllowedWorkers.worker_id != worker.id,
                    ),
                ),
            )
        else:
            query = query.filter(
                or_(
                    WPAllowedWorkers.id.is_(None),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(False),
                        WPAllowedWorkers.worker_id == worker.id,
                    ),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(True),
                        WPAllowedWorkers.worker_id != worker.id,
                    ),
                ),
            )

    row = query.one()
    raw = dict(zip(count_exprs.keys(), row))

    # Now map raw results to the return dictionary with the bridge_version aggregation logic
    ret_dict = {}
    bridge_version_count = 0

    if raw.get("models", 0) > 0:
        ret_dict["models"] = raw["models"]
    if raw.get("worker_id", 0) > 0:
        ret_dict["worker_id"] = raw["worker_id"]
    if raw.get("max_pixels", 0) > 0:
        ret_dict["max_pixels"] = raw["max_pixels"]

    # img2img — attribute to setting or bridge depending on which is the cause
    img2img_count = raw.get("_img2img_raw", 0) or 0
    if img2img_count > 0:
        if worker.allow_img2img is False:
            ret_dict["img2img"] = img2img_count
        else:
            bridge_version_count += img2img_count

    # painting
    painting_count = raw.get("_painting_raw", 0) or 0
    if painting_count > 0:
        if worker.allow_painting is False:
            ret_dict["painting"] = painting_count
        else:
            bridge_version_count += painting_count

    if raw.get("unsafe_ip", 0) > 0:
        ret_dict["unsafe_ip"] = raw["unsafe_ip"]
    if raw.get("nsfw", 0) > 0:
        ret_dict["nsfw"] = raw["nsfw"]

    # lora
    lora_count = raw.get("_lora_raw", 0) or 0
    if lora_count > 0:
        if worker.allow_lora is False:
            ret_dict["lora"] = lora_count
        else:
            bridge_version_count += lora_count

    # TI
    ti_count = raw.get("_ti_raw", 0) or 0
    if ti_count > 0:
        bridge_version_count += ti_count

    # post-processing
    pp_count = raw.get("_pp_raw", 0) or 0
    if pp_count > 0:
        if worker.allow_post_processing is False:
            ret_dict["post-processing"] = pp_count
        else:
            bridge_version_count += pp_count

    # controlnet
    controlnet_count = raw.get("_controlnet_raw", 0) or 0
    if controlnet_count > 0:
        if worker.allow_controlnet is False:
            ret_dict["controlnet"] = controlnet_count
        else:
            bridge_version_count += controlnet_count

    # performance
    perf_count = (raw.get("_perf_slow", 0) or 0) + (raw.get("_perf_extra_slow", 0) or 0)
    if perf_count > 0:
        ret_dict["performance"] = perf_count

    if raw.get("untrusted", 0) > 0:
        ret_dict["untrusted"] = raw["untrusted"]

    # bridge_version sampler/capability
    bv_sampler = raw.get("_bv_sampler", 0) or 0
    bridge_version_count += bv_sampler
    if bridge_version_count > 0:
        ret_dict["bridge_version"] = bridge_version_count

    for key in [
        "bridge_version",
        "untrusted",
        "performance",
        "controlnet",
        "post-processing",
        "lora",
        "nsfw",
        "unsafe_ip",
        "painting",
        "img2img",
        "worker_id",
        "models",
    ]:
        if key not in ret_dict:
            ret_dict[key] = 0
    return ret_dict


def get_sorted_forms_filtered_to_worker(worker, forms_list=None, priority_user_ids=None, excluded_forms=None):
    # Currently the worker is not being used, but I leave it being sent in case we need it later for filtering
    if forms_list is None:
        forms_list = []
    final_interrogation_query = (
        db.session.query(InterrogationForms)
        .join(Interrogation)
        .filter(
            InterrogationForms.state == State.WAITING,
            InterrogationForms.name.in_(forms_list),
            InterrogationForms.expiry == None,  # noqa E712
            Interrogation.source_image != None,  # noqa E712
            Interrogation.image_tiles <= worker.max_power,
            or_(
                Interrogation.safe_ip == True,  # noqa E712
                worker.allow_unsafe_ipaddr == True,  # noqa E712
            ),
            or_(
                worker.maintenance == False,  # noqa E712
                Interrogation.user_id == worker.user_id,
            ),
            or_(
                worker.speed < 10,  # 10 seconds per form
                Interrogation.slow_workers == True,  # noqa E712
            ),
        )
        .order_by(Interrogation.extra_priority.desc(), Interrogation.created.asc())
    )
    if priority_user_ids is not None:
        final_interrogation_query.filter(Interrogation.user_id.in_(priority_user_ids))
    # We use this to not retrieve already retrieved with priority_users
    retrieve_limit = 100
    if excluded_forms is not None:
        excluded_form_ids = [f.id for f in excluded_forms]
        # We only want to retrieve 100 requests, so we reduce the amount to retrieve from non-prioritized
        # requests by the prioritized requests.
        retrieve_limit -= len(excluded_form_ids)
        if retrieve_limit <= 0:
            retrieve_limit = 1
        final_interrogation_query.filter(InterrogationForms.id.not_in(excluded_form_ids))
    return final_interrogation_query.limit(retrieve_limit).all()


# Returns the queue position of the provided WP based on kudos
# Also returns the amount of things until the wp is generated
# Also returns the amount of different gens queued
# In-process cache for pre-computed queue positions (refreshed at most once per second)
_wp_queue_positions_cache = {"image": {}, "text": {}}
_wp_queue_positions_time = {"image": 0.0, "text": 0.0}


def get_wp_queue_stats(wp):
    if not wp.needs_gen():
        return (-1, 0, 0)
    wp_type = wp.wp_type
    now = time.time()
    # Refresh in-process cache at most once per second per wp_type
    if now - _wp_queue_positions_time.get(wp_type, 0) > 1:
        cached_positions = hr.horde_r_get(f"{wp_type}_wp_queue_positions")
        if cached_positions is not None:
            try:
                parsed = json.loads(cached_positions)
                _wp_queue_positions_cache[wp_type] = parsed
                _wp_queue_positions_time[wp_type] = now
            except (TypeError, ValueError):
                pass
    positions = _wp_queue_positions_cache.get(wp_type, {})
    wp_stats = positions.get(str(wp.id))
    if wp_stats is not None:
        return tuple(wp_stats)
    # Check if we have positions data at all; if so, WP is not in the queue
    if positions:
        return (-1, 0, 0)
    # Fall back to legacy computation if pre-computed positions unavailable
    with logfire.span("horde.db.get_wp_queue_stats", wp_id=str(wp.id), wp_type=wp.wp_type):
        things_ahead_in_queue = 0
        n_ahead_in_queue = 0
        priority_sorted_list = retrieve_prioritized_wp_queue(wp.wp_type)
        if priority_sorted_list is None:
            logger.warning(
                "Cached WP priority query does not exist. Falling back to direct DB query. Please check thread on primary!",
            )
            priority_sorted_list = query_prioritized_wps(wp.wp_type)
        for riter in range(len(priority_sorted_list)):
            iter_wp = priority_sorted_list[riter]
            queued_things = round(iter_wp.things * iter_wp.n / hv.thing_divisors["image"], 2)
            things_ahead_in_queue += queued_things
            n_ahead_in_queue += iter_wp.n
            if iter_wp.id == wp.id:
                things_ahead_in_queue = round(things_ahead_in_queue, 2)
                return (riter, things_ahead_in_queue, n_ahead_in_queue)
        return (-1, 0, 0)


def get_wp_by_id(wp_id, lite=False):
    try:
        wp_uuid = uuid.UUID(wp_id)
    except ValueError:
        logger.debug(f"Non-UUID wp_id sent: '{wp_id}'.")
        return None
    if SQLITE_MODE:
        wp_uuid = str(wp_uuid)
    # lite version does not pull ProcGens
    if lite:
        query = db.session.query(ImageWaitingPrompt).options(noload(ImageWaitingPrompt.processing_gens))
    else:
        query = db.session.query(ImageWaitingPrompt)
    return query.filter_by(id=wp_uuid).first()


def get_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError:
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    return db.session.query(ImageProcessingGeneration).filter_by(id=procgen_uuid).first()


def get_interrogation_by_id(i_id):
    try:
        i_uuid = uuid.UUID(i_id)
    except ValueError:
        logger.debug(f"Non-UUID i_id sent: '{i_id}'.")
        return None
    if SQLITE_MODE:
        i_uuid = str(i_uuid)
    return db.session.query(Interrogation).filter_by(id=i_uuid).first()


def get_form_by_id(form_id):
    try:
        form_uuid = uuid.UUID(form_id)
    except ValueError:
        logger.debug(f"Non-UUID form_id sent: '{form_id}'.")
        return None
    if SQLITE_MODE:
        form_uuid = str(form_uuid)
    return db.session.query(InterrogationForms).filter_by(id=form_uuid).first()


def get_all_wps():
    return (
        db.session.query(ImageWaitingPrompt)
        .filter(
            ImageWaitingPrompt.active == True,  # noqa E712
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_all_active_wps():
    return (
        db.session.query(ImageWaitingPrompt)
        .filter(
            ImageWaitingPrompt.active == True,  # noqa E712
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.n > 0,
            ImageWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


# TODO: Convert below three functions into a general "cached db request" (or something) class
# Which I can reuse to cache the results of other requests
def retrieve_worker_performances(worker_type=ImageWorker):
    avg_perf = db.session.query(func.avg(WorkerPerformance.performance)).join(worker_type).scalar()
    avg_perf = 0 if avg_perf is None else round(avg_perf, 2)
    return avg_perf  # noqa RET504


def refresh_worker_performances_cache(request_type="image"):
    ret_dict = {
        "image": retrieve_worker_performances(ImageWorker),
        "text": retrieve_worker_performances(TextWorker),
    }
    try:
        hr.horde_r_setex("worker_performances_avg_cache", timedelta(seconds=30), ret_dict["image"])
        hr.horde_r_setex(
            "text_worker_performances_avg_cache",
            timedelta(seconds=30),
            ret_dict["text"],
        )
    except Exception as err:
        logger.debug(f"Error when trying to set worker performances cache: {err}. Retrieving from DB.")
    return ret_dict[request_type]


def get_request_avg(request_type="image"):
    if hr.horde_r is None:
        return retrieve_worker_performances(WORKER_CLASS_MAP[request_type])
    if request_type == "image":
        perf_cache = hr.horde_r_get("worker_performances_avg_cache")
    else:
        perf_cache = hr.horde_r_get("text_worker_performances_avg_cache")
    if not perf_cache:
        return refresh_worker_performances_cache(request_type)
    return float(perf_cache)


def wp_has_valid_workers(wp):
    cached_validity = hr.horde_r_get(f"wp_validity_{wp.id}")
    if cached_validity is not None:
        return bool(int(cached_validity))
    with logfire.span("horde.db.wp_has_valid_workers", wp_id=str(wp.id), wp_type=wp.wp_type):
        if wp.faulted:
            return False
        if wp.expiry < datetime.utcnow():
            return False
        worker_class = ImageWorker
        if wp.wp_type == "text":
            worker_class = TextWorker
        elif wp.wp_type == "interrogation":
            worker_class = InterrogationWorker
        models_list = wp.get_model_names()
        worker_ids = wp.get_worker_ids()
        final_worker_list = (
            db.session.query(worker_class)
            .options(
                noload(worker_class.performance),
                noload(worker_class.suspicions),
                noload(worker_class.stats),
                # Eagerly load relationships accessed by can_generate() to avoid N+1 queries
                selectinload(worker_class.blacklist),
                contains_eager(worker_class.user),
            )
            .outerjoin(
                WorkerModel,
            )
            .join(
                User,
            )
            .filter(
                worker_class.last_check_in > datetime.utcnow() - timedelta(seconds=300),
                or_(
                    len(worker_ids) == 0,
                    and_(
                        wp.worker_blacklist is False,
                        worker_class.id.in_(worker_ids),
                    ),
                    and_(
                        wp.worker_blacklist is True,
                        worker_class.id.not_in(worker_ids),
                    ),
                ),
                or_(
                    len(models_list) == 0,
                    WorkerModel.model.in_(models_list),
                ),
                or_(
                    wp.trusted_workers == False,  # noqa E712
                    and_(
                        wp.trusted_workers == True,  # noqa E712
                        User.trusted == True,  # noqa E712
                    ),
                ),
                or_(
                    wp.safe_ip == True,  # noqa E712
                    and_(
                        wp.safe_ip == False,  # noqa E712
                        worker_class.allow_unsafe_ipaddr == True,  # noqa E712
                    ),
                ),
                or_(
                    wp.nsfw == False,  # noqa E712
                    and_(
                        wp.nsfw == True,  # noqa E712
                        worker_class.nsfw == True,  # noqa E712
                    ),
                ),
                or_(
                    worker_class.maintenance == False,  # noqa E712
                    and_(
                        worker_class.maintenance == True,  # noqa E712
                        wp.user_id == worker_class.user_id,
                    ),
                ),
                or_(
                    worker_class.paused == False,  # noqa E712
                    and_(
                        worker_class.paused == True,  # noqa E712
                        wp.user_id == worker_class.user_id,
                    ),
                ),
            )
        )
        if wp.wp_type == "image":
            final_worker_list = final_worker_list.filter(
                wp.width * wp.height <= worker_class.max_pixels,
                or_(
                    wp.source_image == None,  # noqa E712
                    and_(
                        wp.source_image != None,  # noqa E712
                        worker_class.allow_img2img == True,  # noqa E712
                    ),
                ),
                or_(
                    wp.slow_workers == True,  # noqa E712
                    worker_class.speed >= 500000,
                ),
                or_(
                    "loras" not in wp.params,
                    and_(
                        worker_class.allow_lora == True,  # noqa E712
                        # TODO: Create an sql function I can call to check the worker bridge capabilities
                        "loras" in wp.params,
                    ),
                ),
                # or_(
                #     'tis' not in wp.params,
                #     and_(
                #         #TODO: Create an sql function I can call to check the worker bridge capabilities
                #         'tis' in wp.params,
                #     ),
                # ),
            )
        elif wp.wp_type == "text":
            final_worker_list = final_worker_list.filter(
                wp.max_length <= worker_class.max_length,
                wp.max_context_length <= worker_class.max_context_length,
                or_(
                    wp.slow_workers == True,  # noqa E712
                    worker_class.speed >= 2,
                ),
            )
        elif wp.wp_type == "interrogation":
            pass  # FIXME: Add interrogation filters
        worker_found = False
        for worker in final_worker_list.all():
            if worker.can_generate(wp)[0]:
                worker_found = True
                break
        hr.horde_r_setex(f"wp_validity_{wp.id}", timedelta(seconds=60), int(worker_found))
        return worker_found


@logger.catch(reraise=True)
def retrieve_prioritized_wp_queue(wp_type):
    cached_queue = hr.horde_r_get(f"{wp_type}_wp_cache")
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


def query_prioritized_wps(wp_type="image"):
    waiting_prompt_type = WP_CLASS_MAP[wp_type]
    return (
        db.session.query(
            waiting_prompt_type.id,
            waiting_prompt_type.things,
            waiting_prompt_type.n,
            waiting_prompt_type.extra_priority,
            waiting_prompt_type.created,
            waiting_prompt_type.expiry,
        )
        .filter(
            waiting_prompt_type.n > 0,
            waiting_prompt_type.faulted == False,  # noqa E712
            waiting_prompt_type.active == True,  # noqa E712
        )
        .order_by(waiting_prompt_type.extra_priority.desc(), waiting_prompt_type.created.asc())
        .all()
    )


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


def compile_regex_filter(filter_type):
    all_filter_regex_query = db.session.query(Filter.regex).filter_by(filter_type=filter_type)
    all_filter_regex = [rfilter.regex for rfilter in all_filter_regex_query.all()]
    regex_string = "|".join(all_filter_regex)
    if not validate_regex(regex_string):
        logger.error("Error when checking compiled regex!. Avoiding cache store")
        return ""
    return regex_string


def retrieve_regex_replacements(filter_type):
    all_filter_regex_query = db.session.query(Filter.regex, Filter.replacement).filter_by(filter_type=filter_type)
    return [
        {
            "regex": rfilter.regex,
            "replacement": rfilter.replacement,
        }
        for rfilter in all_filter_regex_query.all()
        if validate_regex(rfilter.regex)
    ]


def get_all_users(sort="kudos", offset=0):
    user_order_by = User.created.asc() if sort == "age" else User.kudos.desc()
    return db.session.query(User).filter(User.oauth_id != "<wiped>").order_by(user_order_by).offset(offset).limit(25).all()


def get_style_by_uuid(style_uuid: str, is_collection=None):
    try:
        style_uuid = uuid.UUID(style_uuid)
    except ValueError:
        return None
    if SQLITE_MODE:
        style_uuid = str(style_uuid)
    style = None
    if is_collection is not True:
        style = db.session.query(Style).filter_by(id=style_uuid).first()
    if is_collection is True or not style:
        collection = db.session.query(StyleCollection).filter_by(id=style_uuid).first()
        return collection
    else:
        return style


def get_style_by_name(style_name: str, is_collection=None):
    """Goes through the styles and the categories and attempts to find a
    style or category that matches the given name
    The user can pre-specify a filter for category or style and/or username
    by formatting the name like
    category::db0#1::my_stylename
    alternatively this format is also allowed to allow multiple users to use the same name
    style::my_stylename
    db0#1::my_stylename
    """
    style_split = style_name.split("::")
    user = None
    # We don't change the is_collection if it comes preset in kwargs, as we then want it explicitly to return none
    # When searching for styles in collections and vice-versa
    if len(style_split) == 3:
        style_name = style_split[2]
        if is_collection is None:
            if style_split[0] == "collection":
                is_collection = True
            elif style_split[0] == "style":
                is_collection = False
        user = find_user_by_username(style_split[1])
    if len(style_split) == 2:
        style_name = style_split[1]
        if style_split[0] == "collection":
            if is_collection is None:
                is_collection = True
        elif style_split[0] == "style":
            if is_collection is None:
                is_collection = False
        else:
            user = find_user_by_username(style_split[0])
    seek_classes = [Style, StyleCollection]
    if is_collection is True:
        seek_classes = [StyleCollection]
    elif is_collection is False:
        seek_classes = [Style]
    for class_seek in seek_classes:
        style_query = db.session.query(class_seek).filter_by(name=style_name)
        if user is not None:
            style_query = style_query.filter_by(user_id=user.id)
        style = style_query.first()
        if style:
            return style


def retrieve_available_styles(
    style_type=None,
    sort="popular",
    public_only=True,
    page=0,
    tag=None,
    model=None,
):
    """Retrieves all style details from DB."""
    style_query = db.session.query(Style).filter_by(style_type=style_type)
    if tag is not None:
        style_query = style_query.join(StyleTag)
    if model is not None:
        style_query = style_query.join(StyleModel)
    if public_only:
        style_query = style_query.filter(Style.public.is_(True))
    if tag is not None:
        style_query = style_query.filter(StyleTag.tag == tag)
    if model is not None:
        style_query = style_query.filter(StyleModel.model == model)
    style_order_by = Style.created.asc() if sort == "age" else Style.use_count.desc()
    return style_query.order_by(style_order_by).offset(page).limit(25).all()


def retrieve_available_collections(
    collection_type=None,
    sort="popular",
    public_only=True,
    page=0,
):
    """Retrieves all collection details from DB."""
    style_query = db.session.query(StyleCollection)
    if collection_type is not None:
        style_query = style_query.filter_by(style_type=collection_type)
    if public_only:
        style_query = style_query.filter(StyleCollection.public.is_(True))
    style_order_by = StyleCollection.created.asc() if sort == "age" else StyleCollection.use_count.desc()
    return style_query.order_by(style_order_by).offset(page).limit(25).all()


def get_all_active_worker_messages(worker_id):
    return (
        db.session.query(WorkerMessage)
        .filter(
            or_(
                WorkerMessage.worker_id == worker_id,
                WorkerMessage.worker_id.is_(None),
            ),
            WorkerMessage.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_worker_messages(user_id=None, worker_id=None, validity="all", page=0):
    wmquery = db.session.query(WorkerMessage)
    if user_id is not None:
        wmquery = wmquery.filter(or_(WorkerMessage.user_id == user_id, WorkerMessage.worker_id.is_(None)))
    if worker_id is not None:
        wmquery = wmquery.filter(WorkerMessage.worker_id == worker_id)
    if validity == "active":
        wmquery = wmquery.filter(WorkerMessage.expiry > datetime.utcnow())
    if validity == "expired":
        wmquery = wmquery.filter(WorkerMessage.expiry <= datetime.utcnow())
    return wmquery.offset(page).limit(50).all()


def get_all_users_passkeys():
    """Retrieves all users passkeys."""
    return {
        user.id: user.proxy_passkey
        for user in db.session.query(User.proxy_passkey, User.id, User.flagged)
        .filter(
            User.proxy_passkey.is_not(None),
        )
        .all()
        if user.flagged is False or user.flagged is None
    }
