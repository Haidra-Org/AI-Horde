import json

from horde import horde_redis as hr
from horde.logger import logger
from horde.threads import PrimaryTimedFunction


class PatreonCache(PrimaryTimedFunction):
    patrons = {}

    def call_function(self):
        try:
            patrons_json = json.loads(hr.horde_r_get("patreon_cache"))
            # json keys are always strings, so we need to convert them to ints to easily index user ids later
            for pid in patrons_json:
                self.patrons[int(pid)] = patrons_json[pid]
            # logger.debug(self.patrons)
        except (TypeError, AttributeError):
            logger.warning("Patreon cache could not be retrieved from redis. Leaving existing cache.")

    def is_patron(self, user_id):
        return user_id in self.patrons

    def get_patrons(self, min_entitlement=0, max_entitlement=1000, exact_entitlement=None):
        matching_patrons = {}
        for pid in self.patrons:
            if exact_entitlement is not None:
                if self.patrons[pid]["entitlement_amount"] == exact_entitlement:
                    matching_patrons[pid] = self.patrons[pid]
            elif (
                self.patrons[pid]["entitlement_amount"] >= min_entitlement
                and self.patrons[pid]["entitlement_amount"] <= max_entitlement
            ):
                matching_patrons[pid] = self.patrons[pid]
        return matching_patrons

    def get_ids(self, **kwargs):
        return list(self.get_patrons(**kwargs).keys())

    def get_names(self, **kwargs):
        """Returns the name of each patron, unless they have an alias defined in their note
        in which case it returns their alias instead
        """
        return [p.get("alias", p["name"]) for p in self.get_sorted_patrons(**kwargs)]

    def get_sorted_patrons(self, **kwargs):
        all_patrons = self.get_patrons(**kwargs)
        return sorted(all_patrons.values(), key=lambda x: x["entitlement_amount"], reverse=True)

    def get_monthly_kudos(self, user_id):
        if not self.is_patron(user_id):
            return 0
        eamount = int(self.patrons[user_id]["entitlement_amount"])
        # Yearly amounts with discounts
        # 10 per month
        # Amount changes a bit randomly
        if eamount > 105 and eamount < 109:
            return 150_000
        # Monthly amounts
        if eamount == 100:
            return 1_500_000
        if eamount == 50:
            return 1_500_000
        if eamount == 25:
            return 900_000
        if eamount == 24:
            return 600_000
        elif eamount == 10:
            return 150_000
        elif eamount < 10:
            return eamount * 10_000
        else:
            logger.warning(f"Found patron '{user_id}' with non-standard entitlement: {eamount}")
            return 0

    def get_sponsors(self):
        sponsors = []
        for p in self.get_sorted_patrons(min_entitlement=100):
            sponsors.append(
                {
                    "name": p.get("alias", p["name"]),
                    "url": p.get("sponsor_link"),
                },
            )
        return sponsors


patrons = PatreonCache(3600, None)
# We call it now to ensure the cache if full when the monthly kudos assignment is done because the thread take a second longer to fire than the import
if hr.horde_r:
    patrons.call_function()
    # logger.debug(json.dumps(patrons.patrons, indent=4))
    # logger.debug(json.dumps(patrons.get_ids(), indent=4))
    # logger.debug(len(patrons.patrons))
