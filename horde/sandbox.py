from horde.logger import logger
from horde.database import functions as database
from horde.flask import HORDE
import sys

with HORDE.app_context():
    logger.debug(database.count_totals())

sys.exit()
