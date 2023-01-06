import time
import json
import uuid
import patreon
import os
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from horde.horde_redis import horde_r
from horde.classes import WaitingPrompt, User, ProcessingGeneration
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.flask import HORDE, db, SQLITE_MODE
from horde.logger import logger
from horde.database.functions import query_prioritized_wps, get_active_workers, get_available_models, count_totals, prune_expired_stats
from horde import horde_instance_id
from horde.argparser import args
from horde.r2 import delete_procgen_image, delete_source_image
from horde.argparser import args
from horde.patreon import patrons
from horde.enums import State

@logger.catch(reraise=True)
def get_quorum():
    '''Attempts to grab the primary quorum, if it's not set by a different node'''
    # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
    if SQLITE_MODE: 
        return None
    quorum = horde_r.get('horde_quorum')
    if not quorum:
        horde_r.setex('horde_quorum', timedelta(seconds=2), horde_instance_id)
        logger.warning(f"Quorum changed to port {args.port} with ID {horde_instance_id}")
        # We return None which will make other threads sleep one iteration to ensure no other node raced us to the quorum
        return None
    if quorum == horde_instance_id:
        horde_r.setex('horde_quorum', timedelta(seconds=2), horde_instance_id)
        logger.trace(f"Quorum retained in port {args.port} with ID {horde_instance_id}")
    elif args.quorum:
        horde_r.setex('horde_quorum', timedelta(seconds=2), horde_instance_id)
        logger.debug(f"Forcing Pickingh Quorum n port {args.port} with ID {horde_instance_id}")
    return(quorum)

@logger.catch(reraise=True)
def assign_monthly_kudos():
    with HORDE.app_context():
        patron_ids = patrons.get_ids()
        # for pid in patron_ids:
        #     logger.debug(patrons.get_monthly_kudos(pid))
        or_conditions = []
        or_conditions.append(User.monthly_kudos > 0)
        or_conditions.append(User.moderator == True)
        or_conditions.append(User.id.in_(patron_ids))
        users = db.session.query(User).filter(or_(*or_conditions))
        logger.debug(f"Found {users.count()} users with Monthly Kudos Assignment")
        for user in users.all():
            user.receive_monthly_kudos()
  

@logger.catch(reraise=True)
def store_prioritized_wp_queue():
    '''Stores the retrieved WP queue as json for 1 second horde-wide'''
    with HORDE.app_context():
        wp_queue = query_prioritized_wps()
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
            horde_r.setex('wp_cache', timedelta(seconds=10), cached_queue)
        except (TypeError, OverflowError) as e:
            logger.error(f"Failed serializing with error: {e}")



@logger.catch(reraise=True)
def store_worker_list():
    '''Stores the retrieved worker details as json for 30 seconds horde-wide'''
    with HORDE.app_context():
        serialized_workers = []
        # I could do this with a comprehension, but this is clearer to understand
        for worker in get_active_workers():
            serialized_workers.append(worker.get_details())
        json_workers = json.dumps(serialized_workers)
        try:
            horde_r.setex('worker_cache', timedelta(seconds=30), json_workers)
        except (TypeError, OverflowError) as e:
            logger.error(f"Failed serializing workers with error: {e}")


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
#             horde_r.setex('worker_cache', timedelta(seconds=30), json_workers)
#         except (TypeError, OverflowError) as e:
#             logger.error(f"Failed serializing workers with error: {e}")



@logger.catch(reraise=True)
def check_waiting_prompts():
    with HORDE.app_context():
        # Cleans expired WPs
        expired_wps = db.session.query(WaitingPrompt).filter(WaitingPrompt.expiry < datetime.utcnow())
        expired_r_wps = expired_wps.filter(
            WaitingPrompt.r2 == True,
            # We do not delete shared images
            WaitingPrompt.shared == False,
        )
        all_wp_r_id = [wp.id for wp in expired_r_wps.all()]
        expired_r2_procgens = db.session.query(
            ProcessingGeneration.id,
        ).filter(
            ProcessingGeneration.wp_id.in_(all_wp_r_id)
        ).all()
        # logger.debug([expired_r_wps, expired_r2_procgens])
        for procgen in expired_r2_procgens:
            delete_procgen_image(str(procgen.id))
        logger.info(f"Pruned {expired_wps.count()} expired Waiting Prompts")
        expired_wps.delete()
        db.session.commit()
        # Faults stale ProcGens
        all_proc_gen = db.session.query(
            ProcessingGeneration,
        ).join(
            WaitingPrompt, 
        ).filter(
            ProcessingGeneration.generation == None,
            ProcessingGeneration.faulted == False,
            # datetime.utcnow() - ProcessingGeneration.start_time > WaitingPrompt.job_ttl, # How do we calculate this in the query? Maybe I need to set an expiry time iun procgen as well better?
        ).all()
        for proc_gen in all_proc_gen:
            if proc_gen.is_stale(proc_gen.wp.job_ttl):
                proc_gen.abort()
                proc_gen.wp.n += 1
        if len(all_proc_gen) >= 1:
            db.session.commit()

        # Faults WP with 3 or more faulted Procgens
        wp_ids = db.session.query(
            ProcessingGeneration.wp_id, 
        ).filter(
            ProcessingGeneration.faulted == True
        ).group_by(
            ProcessingGeneration.wp_id
        ).having(func.count(ProcessingGeneration.wp_id) > 2)
        wp_ids = [wp_id[0] for wp_id in wp_ids]
        waiting_prompts = db.session.query(WaitingPrompt).filter(WaitingPrompt.id.in_(wp_ids)).filter(WaitingPrompt.faulted == False)
        logger.debug(f"Found {waiting_prompts.count()} New faulted WPs")
        waiting_prompts.update({WaitingPrompt.faulted: True}, synchronize_session=False)
        db.session.commit()
        for wp in waiting_prompts.all():
            wp.log_faulted_prompt()

@logger.catch(reraise=True)
def check_interrogations():
    with HORDE.app_context():
        # Cleans expired WPs
        expired_entries = db.session.query(Interrogation).filter(Interrogation.expiry < datetime.utcnow())
        expired_r_entries = expired_entries.filter(Interrogation.r2stored == True)
        all_source_image_ids = [i.id for i in expired_r_entries.all()]
        for source_image_id in all_source_image_ids:
            delete_source_image(str(source_image_id))
        logger.info(f"Pruned {expired_entries.count()} expired Interrogations")
        expired_entries.delete()
        db.session.commit()
        # Restarts stale forms
        all_stale_forms = db.session.query(
            InterrogationForms,
        ).filter(
            InterrogationForms.state == State.PROCESSING,
            datetime.utcnow() > InterrogationForms.expiry,
        ).all()
        for form in all_stale_forms:
            form.abort()
        if len(all_stale_forms) >= 1:
            db.session.commit()

@logger.catch(reraise=True)
def store_available_models():
    '''Stores the retrieved model details as json for 5 seconds horde-wide'''
    with HORDE.app_context():
        json_models = json.dumps(get_available_models())
        try:
            horde_r.setex('models_cache', timedelta(seconds=60), json_models)
        except (TypeError, OverflowError) as e:
            logger.error(f"Failed serializing workers with error: {e}")

@logger.catch(reraise=True)
def store_totals():
    '''Stores the calculated totals as json. This is never expired to avoid ending up with massive operations in case the thread dies'''
    with HORDE.app_context():
        json_totals = json.dumps(count_totals())
        try:
            horde_r.set('totals_cache', json_totals)
        except (TypeError, OverflowError) as e:
            logger.error(f"Failed serializing totals with error: {e}")

@logger.catch(reraise=True)
def prune_stats():
    '''Prunes performances which are too old'''
    with HORDE.app_context():
        prune_expired_stats()


@logger.catch(reraise=True)
def store_patreon_members():
    api_client = patreon.API(os.getenv("PATREON_CREATOR_ACCESS_TOKEN"))
    # campaign_id = api_client.get_campaigns(10).data()[0].id()
    cursor = None
    members = []
    while True:
        members_response = api_client.get_campaigns_by_id_members(
            77119, 100, 
            cursor=cursor,
            includes=["user"],
            fields={
                # See patreon/schemas/member.py
                "member": ["patron_status", "full_name", "email", "currently_entitled_amount_cents", "note"]
            }
            )
        members += members_response.data()
        if members_response.json_data.get("links") is None:
            # Avoid Exception: ('Provided cursor path did not result in a link' ..
            break
        cursor = api_client.extract_cursor(members_response)
    active_members = {}
    for member in members:
        if member.attribute('patron_status') != "active_patron":
            continue
        # If we do not have a user ID, we cannot use it
        if member.attribute('note') in [None, ""]:
            continue
        member_dict = {
            "name": member.attribute('full_name'),
            "email": member.attribute('email'),
            "entitlement_amount": member.attribute('currently_entitled_amount_cents') / 100,
        }
        note = json.loads(member.attribute('note'))
        if f"{args.horde}_id" not in note:
            continue
        user_id = note[f"{args.horde}_id"]
        if '#' in user_id:
            user_id = user_id.split("#")[-1]
        user_id = int(user_id)
        if "alias" in note:
            member_dict["alias"] = note["alias"]
        active_members[user_id] = member_dict
    cached_patreons = json.dumps(active_members)
    horde_r.set('patreon_cache', cached_patreons)


@logger.catch(reraise=True)
def increment_extra_priority():
    '''Increases the priority of every WP currently in the queue by 50 kudos'''
    with HORDE.app_context():
        wp_queue = db.session.query(
            WaitingPrompt
        ).update(
            {
                WaitingPrompt.extra_priority: WaitingPrompt.extra_priority + 50
            }, synchronize_session=False
        )
        db.session.commit()
