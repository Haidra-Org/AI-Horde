from horde.logger import logger
from horde.database import functions as database
import horde.classes.base.stats as stats
from horde.flask import HORDE
import sys
from horde.patreon import patrons
from horde.detection import prompt_checker
import pprint

# with HORDE.app_context():
#     logger.debug(stats.get_model_avg("Deliberate"))
#     logger.debug(stats.get_model_avg("stable_diffusion"))
    # logger.debug(database.count_totals())


# pp = pprint.PrettyPrinter(depth=3)
# pp.pprint(patrons.get_monthly_kudos(23734))
# pp.pprint(patrons.get_ids())

print(prompt_checker.regex["filter_10"])
logger.info(prompt_checker("This is a test 👧"))
print("👧" in prompt_checker.regex["filter_10"])
import regex as re
reg = re.compile(r"student |👧", re.IGNORECASE)
print(reg.search("This is a test 👧"))
sys.exit()
