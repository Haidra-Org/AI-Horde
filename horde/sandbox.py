from horde.logger import logger
from horde.database import functions as database
import horde.classes.base.stats as stats
from horde.flask import HORDE
import sys
from horde.patreon import patrons
from horde.detection import prompt_checker
import pprint
from horde.discord import send_pause_notification, send_problem_user_notification
from horde.classes.stable.worker import ImageWorker
from horde.suspicions import Suspicions
from horde.database import threads as threads
from horde.model_reference import model_reference
from horde.countermeasures import CounterMeasures


def test():
    # with HORDE.app_context():
    #     logger.debug(stats.get_model_avg("Deliberate"))
    #     logger.debug(stats.get_model_avg("stable_diffusion"))
    # logger.debug(database.count_totals())

    # pp = pprint.PrettyPrinter(depth=3)
    # pp.pprint(patrons.get_monthly_kudos(42742))
    # pp.pprint(patrons.get_ids())

    # Test sus discord webhook
    # send_pause_notification("Hello World")
    # with HORDE.app_context():
    #     worker = database.find_worker_by_name("Db0_Test_Worker", worker_class=ImageWorker)
    #     logger.debug(worker.get_bridge_kudos_multiplier())
    # worker.report_suspicion(amount = 1, reason = Suspicions.UNREASONABLY_FAST, formats = [9999])
    # threads.store_patreon_members()

    # Test problem userdiscord webhook
    # with HORDE.app_context():
    #     worker = database.find_worker_by_name("Db0_Test_Worker", worker_class=ImageWorker)
    #     worker.report_suspicion(amount = 1, reason = Suspicions.UNREASONABLY_FAST, formats = [9999])

    # Cache testing
    # with HORDE.app_context():
    # logger.info(database.retrieve_totals(True))
    # logger.info(database.retrieve_totals())
    # database.get_available_models()

    # IP timeout testing
    # CounterMeasures.set_block_timeout("2001:db8::/64",1)
    # logger.debug(CounterMeasures.get_block_timeouts())
    # logger.debug(CounterMeasures.retrieve_timeout('2001:db8:0000:0000:0000:0000:0000:0001'))
    # logger.debug(CounterMeasures.extract_ipv6_subnet('2001:db8:0000:0000:0000:0000:0000:0001'))

    # Worker rewards test
    # with HORDE.app_context():
    #     w = database.find_worker_by_id("43f5f639-134f-4687-a130-b4bf13821d8c")
    #     logger.debug(w.max_context_length)
    #     logger.debug(w.calculate_uptime_reward())

    sys.exit()
