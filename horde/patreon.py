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

    def is_patron(self, user_id):
        return user_id in self.patrons

    def get_ids(self, entitlement_min = 0):
        found_ids = []
        for pid in self.patrons:
            if self.patrons[pid]["entitlement_amount"] >= entitlement_min:
                found_ids.append(pid)
        return(found_ids)

    def get_monthly_kudos(self, user_id):
        if not self.is_patron(user_id):
            return 0
        eamount = int(self.patrons[user_id]["entitlement_amount"] )
        if eamount == 25:
            return(300000)
        elif eamount == 10:
            return(50000)
        elif eamount == 5:
            return(5000)
        elif eamount == 1:
            return(1000)
        else:
            logger.warning(f"Found patron '{user_id}' with non-standard entitlement: {eamount}")
            return(0)


patrons = PatreonCache(3600, None)
# We call it now to ensure the cache if full when the monthly kudos assignment is done because the thread take a second longer to fire than the import
patrons.call_function()