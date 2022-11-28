import threading
import time

from horde.classes import WaitingPrompt, User
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
                for wp in db.session.query(WaitingPrompt).all():  # TODO - all this logic can likely be moved into a prompt when it gets processed, ie, how many did i fail first, then delete
                    try:
                        # The below check if any jobs have been running too long and aborts them
                        faulted_requests = 0
                        for gen in wp.processing_gens:
                            # We don't want to recheck if we've faulted already
                            if wp.faulted:
                                break
                            if gen.is_stale(wp.job_ttl):
                                # If the request took too long to complete, we cancel it and add it to the retry
                                gen.abort()
                                wp.n += 1
                            if gen.is_faulted():
                                faulted_requests += 1
                            # If 3 or more jobs have failed, we assume there's something wrong with this request and mark it as faulted.
                            if faulted_requests >= 3:
                                wp.faulted = True
                                wp.log_faulted_job()
                        if wp.is_stale():
                            wp.delete()
                            break
                        wp.extra_priority += 50
                        db.session.commit()
                    except Exception as e:
                        logger.critical(f"Exception {e} detected. Handing to avoid crashing thread.")
                    time.sleep(10)

