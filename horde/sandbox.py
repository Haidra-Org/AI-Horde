from horde.logger import logger
from horde.database import functions as database
import horde.classes.base.stats as stats
from horde.flask import HORDE
import sys
from horde.patreon import patrons
from horde.detection import prompt_checker
import pprint
from horde.discord import send_pause_notification

# with HORDE.app_context():
#     logger.debug(stats.get_model_avg("Deliberate"))
#     logger.debug(stats.get_model_avg("stable_diffusion"))
    # logger.debug(database.count_totals())


# pp = pprint.PrettyPrinter(depth=3)
# pp.pprint(patrons.get_monthly_kudos(42742))
# pp.pprint(patrons.get_ids())

# Test discord webhook
# send_pause_notification("Hello World")
sys.exit()
