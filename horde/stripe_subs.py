# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os

from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.threads import PrimaryTimedFunction


class StripeCache(PrimaryTimedFunction):
    patrons = {}

    def call_function(self):
        if not os.environ.get("STRIPE_API_KEY"):
            logger.warning("STRIPE_API_KEY not set. No stripe cache will be retrieved.")
            return
        try:
            patrons_json = json.loads(hr.horde_r_get("stripe_cache"))
            # json keys are always strings, so we need to convert them to ints to easily index user ids later
            for pid in patrons_json:
                self.patrons[int(pid)] = patrons_json[pid]
            # logger.debug(self.patrons)
        except (TypeError, AttributeError):
            logger.warning("Stripe cache could not be retrieved from redis. Leaving existing cache.")
        except Exception as e:
            logger.error(f"Error retrieving stripe cache from redis: {e}")

    def is_patron(self, user_id):
        return user_id in self.patrons

    def get_patrons(self, product_name=None):
        matching_patrons = {}
        for pid in self.patrons:
            if product_name is None or self.patrons[pid]["product_name"] == product_name:
                matching_patrons[pid] = self.patrons[pid]
        return matching_patrons

    def get_ids(self, **kwargs):
        return list(self.get_patrons(**kwargs).keys())

    def get_names(self, **kwargs):
        """Returns the name of each patron, unless they have an alias defined in their note
        in which case it returns their alias instead
        """
        return [p.get("alias", p["name"]) for p in self.get_sorted_patrons(**kwargs)]

    def get_monthly_kudos(self, user_id):
        if not self.is_patron(user_id):
            return 0
        product_name = self.patrons[user_id]["product_name"]
        if product_name == "Superior Person":
            return 20_000
        if product_name == "Recognised":
            return 75_000
        if product_name == "Cherised":
            return 200_000
        if product_name == "Treasured":
            return 700_000
        if product_name == "Celebrated":
            return 1_500_000
        if product_name == "Sponsor":
            return 1_500_000
        logger.warning(f"Found patron '{user_id}' with non-standard product: {product_name}")
        return 0

    def get_sponsors(self):
        sponsors = []
        for product in {"Recognised", "Cherised", "Treasured", "Celebrated", "Sponsor"}:
            for patron in self.get_patrons(product_name=product).values():
                logger.debug(patron)
                sponsors.append(
                    {
                        "name": patron.get("alias") if patron.get("alias") else patron["name"],
                        "url": patron.get("sponsor_link"),
                    },
                )
        return sponsors


stripe_subs = StripeCache(1800, None)
# We call it now to ensure the cache if full when the monthly kudos assignment
# is done because the thread take a second longer to fire than the import
if hr.horde_r:
    stripe_subs.call_function()
    # logger.debug(json.dumps(stripe_subs.patrons, indent=4))
    # logger.debug(json.dumps(stripe_subs.get_ids(), indent=4))
    # logger.debug(json.dumps(stripe_subs.get_sponsors(), indent=4))
    # logger.debug(len(stripe_subs.patrons))
