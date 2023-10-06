from horde.logger import logger
from horde.database import functions as database
import horde.classes.base.stats as stats
from horde.flask import HORDE
import sys
from horde.patreon import patrons
from horde.detection import prompt_checker
import pprint
from horde.discord import send_pause_notification
from horde.classes.stable.worker import ImageWorker
from horde.suspicions import Suspicions
from horde.database import threads as threads
from horde.model_reference import model_reference

# with HORDE.app_context():
#     logger.debug(stats.get_model_avg("Deliberate"))
#     logger.debug(stats.get_model_avg("stable_diffusion"))
    # logger.debug(database.count_totals())


# pp = pprint.PrettyPrinter(depth=3)
# pp.pprint(patrons.get_monthly_kudos(42742))
# pp.pprint(patrons.get_ids())

# Test discord webhook
# send_pause_notification("Hello World")
# with HORDE.app_context():
#     worker = database.find_worker_by_name("Db0_Test_Worker", worker_class=ImageWorker)
#     worker.report_suspicion(amount = 1, reason = Suspicions.UNREASONABLY_FAST, formats = [9999])
# threads.store_patreon_members()

# Cache testing
# with HORDE.app_context():
    # logger.info(database.retrieve_totals(True))
    # logger.info(database.retrieve_totals())
    # database.get_available_models()


logger.debug(model_reference.get_text_model_multiplier("koboldcpp/mythalion-13b"))
import json
logger.debug(json.dumps(model_reference.text_reference["koboldcpp/mythalion-13b"],indent=4))

sys.exit()
