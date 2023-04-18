from horde.logger import logger
from horde.database import functions as database
import horde.classes.base.stats as stats
from horde.flask import HORDE
import sys

with HORDE.app_context():
    logger.debug(stats.get_model_avg("Deliberate"))
    logger.debug(stats.get_model_avg("stable_diffusion"))
    # logger.debug(database.count_totals())

sys.exit()
