import threading
import time
from datetime import datetime

from sqlalchemy import func

from horde.classes import WaitingPrompt, User, ProcessingGeneration
from horde.flask import HORDE, db
from horde.logger import logger

class MonthlyKudos:
    def __init__(self):
        monthly_kudos_thread = threading.Thread(target=self.assign_monthly_kudos, args=())
        monthly_kudos_thread.daemon = True
        monthly_kudos_thread.start()

    def assign_monthly_kudos(self):
        # TODO - We dont want any sleeps on a server unless its needed
        time.sleep(2)
        logger.init_ok("Monthly Kudos Awards Thread", status="Started")
        while True:
            # TODO Make the select statement bring the users with monthly kudos only
            with HORDE.app_context():
                for user in db.session.query(User).all():
                    user.receive_monthly_kudos()
            # Check once a day
            time.sleep(86400)

class WPCleaner:
    def __init__(self):
        monthly_kudos_thread = threading.Thread(target=self.check_for_stale, args=())
        monthly_kudos_thread.daemon = True
        monthly_kudos_thread.start()

    def check_for_stale(self):
        time.sleep(1.5)
        logger.init_ok("Stale WP Cleanup Thread", status="Started")
        while True:
            with HORDE.app_context():
                db.session.query(WaitingPrompt).filter(WaitingPrompt.expiry > datetime.utcnow()).delete()
                all_proc_gen = db.session.query(ProcessingGeneration).filter(ProcessingGeneration.generation is None).filter().all()
                for proc_gen in all_proc_gen:
                    proc_gen = proc_gen.Join(WaitingPrompt, WaitingPrompt.id == ProcessingGeneration.wp_id).filter(WaitingPrompt.faulted == False)
                    if proc_gen.is_stale(wp.job_ttl):
                        proc_gen.abort()
                        wp.n += 1
                        db.session.commit()

                wp_ids = db.session.query(ProcessingGeneration.wp_id).group_by(ProcessingGeneration.wp_id).having(func.count(ProcessingGeneration.wp_id) > 2)
                wp_ids = [wp_id[0] for wp_id in wp_ids]
                waiting_prompts = db.session.query(WaitingPrompt).filter(WaitingPrompt.id.in_(wp_ids))
                waiting_prompts.update({WaitingPrompt.faulted: True}, synchronize_session=False)
                for wp in waiting_prompts.all():
                    wp.log_faulted_job()

            time.sleep(60)

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
                #                 wp.log_faulted_job()
                #
                #         # wp.extra_priority += 50
                #         # NOT IN LOOP
                #         # db.session.query(WaitingPrompt).update({WaitingPrompt.extra_priority: WaitingPrompt.extra_priority + 50})
                #         db.session.commit()
                #     except Exception as e:
                #         logger.critical(f"Exception {e} detected. Handing to avoid crashing thread.")
                #     time.sleep(10)
