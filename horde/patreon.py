import json

from horde.logger import logger
from horde.horde_redis import horde_r
from horde.threads import PrimaryTimedFunction


class PatreonCache(PrimaryTimedFunction):
    patrons = {}

    def call_function(self):
        try:
            self.patrons = json.loads(horde_r.get("patreon_cache"))
            # logger.debug(self.patrons)
        except TypeError:
            logger.warning("Patreon cache could not be retrieved from redis. Leaving existing cache.")

    def is_patron(user_id):
        return user_id in self.patrons

patrons = PatreonCache(3600, None)
    