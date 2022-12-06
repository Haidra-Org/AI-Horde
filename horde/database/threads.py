import time
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from horde.horde_redis import horde_r
from horde.classes import WaitingPrompt, User, ProcessingGeneration
from horde.flask import HORDE, db
from horde.logger import logger
from horde.database.functions import query_prioritized_wps, get_active_workers, get_available_models, count_totals, prune_expired_stats
from horde import horde_instance_id
from horde.argparser import args
from horde.r2 import delete_procgen_image
from horde.argparser import args


@logger.catch(reraise=True)
def get_quorum():
    '''Attempts to grab the primary quorum, if it's not set by a different node'''
    quorum = horde_r.get('horde_quorum')
    if not quorum:
        horde_r.setex('horde_quorum', timedelta(seconds=2), horde_instance_id)
        logger.warning(f"Quorum changed to port {args.port} with ID {horde_instance_id}")
        # We return None which will make other threads sleep one iteration to ensure no other node raced us to the quorum
        return None
    if quorum == horde_instance_id:
        horde_r.setex('horde_quorum', timedelta(seconds=2), horde_instance_id)
        logger.debug(f"Quorum retained in port {args.port} with ID {horde_instance_id}")
        # We return None which will make other threads sleep one iteration to ensure no other node raced us to the quorum
    elif args.quorum:
        horde_r.setex('horde_quorum', timedelta(seconds=2), horde_instance_id)
        logger.debug(f"Forcing Pickingh Quorum n port {args.port} with ID {horde_instance_id}")
        # We return None which will make other threads sleep one iteration to ensure no other node raced us to the quorum
    return(quorum)


@logger.catch(reraise=True)
def assign_monthly_kudos():
    with HORDE.app_context():
        or_conditions = []
        or_conditions.append(User.monthly_kudos > 0)
        or_conditions.append(User.moderator == True)
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
        expired_r_wps = expired_wps.filter(WaitingPrompt.r2 == True)
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
        all_proc_gen = db.session.query(ProcessingGeneration).filter(ProcessingGeneration.generation is None).filter().all()
        for proc_gen in all_proc_gen:
            proc_gen = proc_gen.Join(WaitingPrompt, WaitingPrompt.id == ProcessingGeneration.wp_id).filter(WaitingPrompt.faulted == False).filter(ProcessingGeneration.faulted == False)
            if proc_gen.is_stale(wp.job_ttl):
                proc_gen.abort()
                wp.n += 1
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
def store_available_models():
    '''Stores the retrieved model details as json for 5 seconds horde-wide'''
    with HORDE.app_context():
        json_models = json.dumps(get_available_models())
        try:
            horde_r.setex('model_cache', timedelta(seconds=10), json_models)
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
