import time
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from horde.horde_redis import horde_r
from horde.classes import WaitingPrompt, User, ProcessingGeneration
from horde.flask import HORDE, db
from horde.logger import logger
from horde.database.functions import query_prioritized_wps, get_active_workers

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
            # We set the expiry in redis to 5 seconds, in case the primary thread dies
            # However the primary thread is set to set the cache every 1 second
            horde_r.setex('wp_cache', timedelta(seconds=5), cached_queue)
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



@logger.catch(reraise=True)
def check_waiting_prompts():
    with HORDE.app_context():
        # Cleans expired WPs
        expired_wps = db.session.query(WaitingPrompt).filter(WaitingPrompt.expiry < datetime.utcnow())
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

                # for wp in db.session.query(WaitingPrompt).all():  # TODO - all this logic can likely be moved into a prompt when it gets processed, ie, how many did i fail first, then delete
                #     try:
                #         # The below check if any jobs have been running too long and aborts them
                #         faulted_requests = 0
                #         for gen in wp.processing_gens:
                #             # We don't want to recheck if we've faulted already
                #             if wp.faulted:
                #                 break
                #             if gen.is_stale(wp.job_ttl):    # if completed or faulted - it's not stale - else (datetime.utcnow() - self.start_time).seconds > ttl
                #                 # If the request took too long to complete, we cancel it and add it to the retry
                #                 gen.abort()
                #                 wp.n += 1
                #             if gen.is_faulted():
                #                 faulted_requests += 1
                #             # If 3 or more jobs have failed, we assume there's something wrong with this request and mark it as faulted.
                #             if faulted_requests >= 3:
                #                 wp.faulted = True
                #                 wp.log_faulted_prompt()
                #
                #         # wp.extra_priority += 50
                #         # NOT IN LOOP
                #         # db.session.query(WaitingPrompt).update({WaitingPrompt.extra_priority: WaitingPrompt.extra_priority + 50})
                #         db.session.commit()
                #     except Exception as e:
                #         logger.critical(f"Exception {e} detected. Handing to avoid crashing thread.")
                #     time.sleep(10)
