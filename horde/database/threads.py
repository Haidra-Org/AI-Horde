import json
import os
from datetime import datetime, timedelta

import patreon
from sqlalchemy import func, or_

from horde import horde_redis as hr
from horde.argparser import args
from horde.classes.base.user import User
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.processing_generation import ImageProcessingGeneration

# FIXME: Renamed for backwards compat. To fix later
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.database.functions import (
    compile_regex_filter,
    count_totals,
    get_active_workers,
    get_available_models,
    prune_expired_stats,
    query_prioritized_wps,
    retrieve_regex_replacements,
)
from horde.enums import State
from horde.flask import HORDE, SQLITE_MODE, db
from horde.logger import logger
from horde.patreon import patrons
from horde.r2 import delete_source_image
from horde.vars import horde_instance_id


@logger.catch(reraise=True)
def get_quorum():
    """Attempts to grab the primary quorum, if it's not set by a different node"""
    # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
    if SQLITE_MODE:
        return None
    quorum = hr.horde_r.get("horde_quorum")
    if not quorum:
        hr.horde_r_setex("horde_quorum", timedelta(seconds=2), horde_instance_id)
        logger.critical(f"Quorum changed to port {args.port} with ID {horde_instance_id}")
        # We return None which will make other threads sleep
        # one iteration to ensure no other node raced us to the quorum
        return None
    if quorum == horde_instance_id:
        hr.horde_r_setex("horde_quorum", timedelta(seconds=2), horde_instance_id)
        logger.trace(f"Quorum retained in port {args.port} with ID {horde_instance_id}")
    elif args.quorum:
        hr.horde_r_setex("horde_quorum", timedelta(seconds=2), horde_instance_id)
        logger.debug(f"Forcing Pickingh Quorum n port {args.port} with ID {horde_instance_id}")
    return quorum


@logger.catch(reraise=True)
def assign_monthly_kudos():
    with HORDE.app_context():
        patron_ids = patrons.get_ids()
        # for pid in patron_ids:
        #     logger.debug(patrons.get_monthly_kudos(pid))
        or_conditions = []
        or_conditions.append(User.monthly_kudos > 0)
        or_conditions.append(User.moderator == True)  # noqa E712
        or_conditions.append(User.id.in_(patron_ids))
        users = db.session.query(User).filter(or_(*or_conditions))
        all_users = users.all()
        logger.info(f"Found {len(all_users)} users with Monthly Kudos Assignment: {[u.id for u in all_users]}")
        for user in all_users:
            user.receive_monthly_kudos()


@logger.catch(reraise=True)
def store_prioritized_wp_queue():
    """Stores the retrieved WP queue as json for 1 second horde-wide"""
    with HORDE.app_context():
        for wp_type in ["image", "text"]:
            wp_queue = query_prioritized_wps(wp_type)
            serialized_wp_list = []
            for wp in wp_queue:
                wp_json = {
                    "id": str(wp.id),
                    "things": wp.things,
                    "n": wp.n,
                    "extra_priority": wp.extra_priority,
                    "created": wp.created.strftime("%Y-%m-%d %H:%M:%S"),
                }
                serialized_wp_list.append(wp_json)
            try:
                cached_queue = json.dumps(serialized_wp_list)
                # We set the expiry in redis to 10 seconds, in case the primary thread dies
                # However the primary thread is set to set the cache every 1 second
                hr.horde_r_setex(f"{wp_type}_wp_cache", timedelta(seconds=5), cached_queue)
            except (TypeError, OverflowError) as err:
                logger.error(f"Failed serializing with error: {err}")


@logger.catch(reraise=True)
def store_worker_list():
    """Stores the retrieved worker details as json for 300 seconds horde-wide"""
    with HORDE.app_context():
        serialized_workers = []
        serialized_workers_privileged = []
        # This is too slow. Needs heavy caching currently
        # TODO: Figure out a way to get only the info I need from the DB query and format it into json by hand?
        for worker in get_active_workers():
            serialized_workers.append(worker.get_details())
            serialized_workers_privileged.append(worker.get_details(2))
        json_workers = json.dumps(serialized_workers)
        json_workers_privileged = json.dumps(serialized_workers_privileged)
        try:
            hr.horde_r_setex("worker_cache", timedelta(seconds=300), json_workers)
            hr.horde_r_setex(
                "worker_cache_privileged",
                timedelta(seconds=300),
                json_workers_privileged,
            )
        except (TypeError, OverflowError) as err:
            logger.error(f"Failed serializing workers with error: {err}")


# @logger.catch(reraise=True)
# def store_user_list():
#     '''Stores the retrieved worker details as json for 30 seconds horde-wide'''
#     with HORDE.app_context():
#         serialized_workers = []
#         # I could do this with a comprehension, but this is clearer to understand
#         for worker in get_active_workers():
#             serialized_workers.append(worker.get_details())
#         json_workers = json.dumps(serialized_workers)
#         try:
#             hr.horde_r_setex('worker_cache', timedelta(seconds=30), json_workers)
#         except (TypeError, OverflowError) as err:
#             logger.error(f"Failed serializing workers with error: {err}")


@logger.catch(reraise=True)
def check_waiting_prompts():
    with HORDE.app_context():
        # Store store the cutoff_time at the start, to avoid things expiring while cleaning
        # and therefore missing images to cleanup
        cutoff_time = datetime.utcnow()
        # Clean expired source images
        # expired_source_img_wps = db.session.query(
        #     ImageWaitingPrompt.id
        # ).filter(
        #     ImageWaitingPrompt.source_image != None,
        #     ImageWaitingPrompt.expiry < cutoff_time,
        # ).all()
        # if len(expired_source_img_wps):
        #     logger.info(f"Deleting {len(expired_source_img_wps)} expired source image.")
        # for wp in expired_source_img_wps:
        #     # logger.debug(f"{wp.id}_src")
        #     delete_source_image(f"{wp.id}_src")
        # expired_source_msk_wps = db.session.query(
        #     ImageWaitingPrompt.id
        # ).filter(
        #     ImageWaitingPrompt.source_mask != None,
        #     ImageWaitingPrompt.expiry < cutoff_time,
        # ).all()
        # # Clean expired source masks
        # if len(expired_source_msk_wps):
        #     logger.info(f"Deleting {len(expired_source_msk_wps)} expired image masks.")
        # for wp in expired_source_msk_wps:
        #     # logger.debug(f"{wp.id}_msk")
        #     delete_source_image(f"{wp.id}_msk")
        # Cleans expired generated images, but not shared images
        # expired_r2_procgens = db.session.query(
        #     ImageProcessingGeneration.id,
        # ).join(
        #     ImageWaitingPrompt,
        # ).filter(
        #     ImageWaitingPrompt.expiry < cutoff_time,
        #     ImageWaitingPrompt.shared == False,
        # ).all()
        # Will handle this with another python process as it's taking too long
        # logger.info(f"Deleting {len(expired_r2_procgens)} procgens.")
        # last_procgen = ''
        # for procgen in expired_r2_procgens:
        #     delete_procgen_image(str(procgen.id))
        #     last_procgen = str(procgen.id)
        # logger.warning(f"Check Last procgen: {last_procgen}")
        for wp_class, procgen_class in [
            (ImageWaitingPrompt, ImageProcessingGeneration),
            (TextWaitingPrompt, TextProcessingGeneration),
        ]:
            expired_wps = db.session.query(wp_class).filter(wp_class.expiry < cutoff_time)
            logger.info(f"Pruned {expired_wps.count()} expired Waiting Prompts")
            expired_wps.delete()
            db.session.commit()
            # Faults stale ProcGens
            all_proc_gen = (
                db.session.query(
                    procgen_class,
                )
                .join(
                    wp_class,
                )
                .filter(
                    procgen_class.generation == None,  # noqa E712
                    procgen_class.faulted == False,  # noqa E712
                    # cutoff_time - procgen_class.start_time > wp_class.job_ttl,
                    # How do we calculate this in the query? Maybe I need to
                    # set an expiry time iun procgen as well better?
                )
                .all()
            )
            for proc_gen in all_proc_gen:
                if proc_gen.is_stale(proc_gen.wp.job_ttl):
                    proc_gen.abort()
                    proc_gen.wp.n += 1
            if len(all_proc_gen) >= 1:
                db.session.commit()
            # Faults WP with 3 or more faulted Procgens
            wp_ids = (
                db.session.query(
                    procgen_class.wp_id,
                )
                .filter(procgen_class.faulted == True)  # noqa E712
                .group_by(procgen_class.wp_id)
                .having(func.count(procgen_class.wp_id) > 2)
            )
            wp_ids = [wp_id[0] for wp_id in wp_ids]
            waiting_prompts = db.session.query(wp_class).filter(wp_class.id.in_(wp_ids)).filter(wp_class.faulted == False)  # noqa E712
            logger.info(f"Found {waiting_prompts.count()} New faulted WPs")
            waiting_prompts.update({wp_class.faulted: True}, synchronize_session=False)
            db.session.commit()
            for wp in waiting_prompts.all():
                wp.log_faulted_prompt()


@logger.catch(reraise=True)
def check_interrogations():
    with HORDE.app_context():
        # Cleans expired interrogations
        cutoff_time = datetime.utcnow()
        expired_entries = db.session.query(Interrogation).filter(Interrogation.expiry < cutoff_time)
        expired_r_entries = expired_entries.filter(Interrogation.r2stored == True)  # noqa E712
        all_source_image_ids = [i.id for i in expired_r_entries.all()]
        for source_image_id in all_source_image_ids:
            delete_source_image(str(source_image_id))
        logger.info(f"Pruned {expired_entries.count()} expired Interrogations")
        expired_entries.delete()
        db.session.commit()
        # Restarts stale forms
        all_stale_forms = (
            db.session.query(
                InterrogationForms,
            )
            .filter(
                InterrogationForms.state == State.PROCESSING,
                cutoff_time > InterrogationForms.expiry,
            )
            .all()
        )
        for form in all_stale_forms:
            form.abort()
        if len(all_stale_forms) >= 1:
            db.session.commit()


@logger.catch(reraise=True)
def store_available_models():
    """Stores the retrieved model details as json for 5 seconds horde-wide"""
    with HORDE.app_context():
        json_models = json.dumps(get_available_models())
        try:
            hr.horde_r_setex("models_cache", timedelta(seconds=600), json_models)
        except (TypeError, OverflowError) as err:
            logger.error(f"Failed serializing workers with error: {err}")


@logger.catch(reraise=True)
def store_totals():
    """Stores the calculated totals as json.
    This is never expired to avoid ending up with massive operations in case the thread dies
    """
    with HORDE.app_context():
        json_totals = json.dumps(count_totals())
        try:
            hr.horde_r_set("totals_cache", json_totals)
        except (TypeError, OverflowError) as err:
            logger.error(f"Failed serializing totals with error: {err}")


@logger.catch(reraise=True)
def prune_stats():
    """Prunes performances which are too old"""
    with HORDE.app_context():
        prune_expired_stats()


@logger.catch(reraise=True)
def store_patreon_members():
    api_client = patreon.API(os.getenv("PATREON_CREATOR_ACCESS_TOKEN"))
    # campaign_id = api_client.get_campaigns(10).data()[0].id()
    if api_client is None:
        logger.error("Failed to get patreon API client")
        return
    cursor = None
    members = []
    while True:
        members_response = api_client.get_campaigns_by_id_members(
            77119,
            100,
            cursor=cursor,
            includes=["user"],
            fields={
                # See patreon/schemas/member.py
                "member": [
                    "patron_status",
                    "full_name",
                    "email",
                    "currently_entitled_amount_cents",
                    "note",
                ],
            },
        )
        if isinstance(members_response, dict) and "data" not in members_response:
            logger.error(f"Unexpected response received from patreon: {members_response}")
            return
        members += members_response.data()
        if members_response.json_data.get("links") is None:
            # Avoid Exception: ('Provided cursor path did not result in a link' ..
            break
        cursor = api_client.extract_cursor(members_response)
    active_members = {}
    for member in members:
        if member.attribute("patron_status") != "active_patron":
            continue
        # If we do not have a user ID, we cannot use it
        if member.attribute("note") in [None, ""]:
            continue
        member_dict = {
            "name": member.attribute("full_name"),
            "email": member.attribute("email"),
            "entitlement_amount": member.attribute("currently_entitled_amount_cents") / 100,
        }
        note = json.loads(member.attribute("note"))
        if "stable_id" not in note:
            continue
        user_id = note["stable_id"]
        if "#" in user_id:
            user_id = user_id.split("#")[-1]
        user_id = int(user_id)
        if "alias" in note:
            member_dict["alias"] = note["alias"]
        if "sponsor_link" in note:
            member_dict["sponsor_link"] = note["sponsor_link"]
        active_members[user_id] = member_dict
    cached_patreons = json.dumps(active_members)
    logger.info(f"patreon_cache ({len(active_members)}): {sorted(active_members.keys())}")
    hr.horde_r_set("patreon_cache", cached_patreons)


@logger.catch(reraise=True)
def increment_extra_priority():
    """Increases the priority of every WP currently in the queue by 50 kudos"""
    with HORDE.app_context():
        # cutoff_time = datetime.utcnow()
        for wp_class in [ImageWaitingPrompt, TextWaitingPrompt]:
            wp_ids = db.session.query(wp_class.id).filter(
                wp_class.n > 0,
                wp_class.faulted == False,  # noqa E712
                wp_class.active == True,  # noqa E712
                # Commented to avoid running into a deadlock with the WP delete thread
                # wp_class.expiry > cutoff_time,
            )
            # logger.debug(f"Found {wp_ids.count()} of class {wp_class} to increase priority")
            for wp in wp_ids.all():
                db.session.query(wp_class).filter_by(id=wp.id).update(
                    {wp_class.extra_priority: wp_class.extra_priority + 50},
                    synchronize_session=False,
                )
                db.session.commit()


@logger.catch(reraise=True)
def store_compiled_filter_regex():
    """Compiles each filter as a final regex and stores it in redit"""
    with HORDE.app_context():
        for filter_id in [10, 11, 20]:
            rfilter = compile_regex_filter(filter_id)
            # Empty string means compilation error
            if rfilter == "":
                continue
            # We don't expire filters once set, to avoid ever losing the cache and letting prompts through
            hr.horde_r_set(f"filter_{filter_id}", rfilter)


@logger.catch(reraise=True)
def store_compiled_filter_regex_replacements():
    """Compiles all regex and their replacements and stores them in redis"""
    with HORDE.app_context():
        replacements = retrieve_regex_replacements(10)
        # We don't expire filters once set, to avoid ever losing the cache and letting prompts through
        hr.horde_r_set("cached_regex_replacements", json.dumps(replacements))
