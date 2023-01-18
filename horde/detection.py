import os
import regex as re
from datetime import datetime
import dateutil.relativedelta
from horde.logger import logger
from horde.horde_redis import horde_r
from horde.flask import HORDE, SQLITE_MODE # Local Testing
from horde.database.functions import compile_regex_filter # Local Testing

class PromptChecker:
    
    def __init__(self):
        self.regex = {
            "filter_10": None,
            "filter_11": None,
            "filter_20": None,
        }
        self.compiled = {
            "filter_10": None,
            "filter_11": None,
            "filter_20": None,
        }
        self.filters1 = ["filter_10","filter_11"]
        self.filters2 = ["filter_20"]
        self.next_refresh = datetime.utcnow()
        self.refresh_regex()

    def refresh_regex(self):
        # We don't want to be pulling the regex from redis all the time. We pull them only once per min
        if self.next_refresh > datetime.utcnow():
            return
        for id in [10, 11, 20]:
            filter_id = f"filter_{id}"
            if SQLITE_MODE:
                with HORDE.app_context():
                    stored_filter = compile_regex_filter(id)
            else:
                stored_filter = horde_r.get(filter_id)
            # Ensure we don't get catch-all regex
            if not stored_filter:
                continue
            # Ensure we recompile the regex when they have actually changed.
            if self.regex[filter_id] != stored_filter:
                self.compiled[filter_id] = re.compile(stored_filter, re.IGNORECASE)
                self.regex[filter_id] = stored_filter
                logger.debug(self.compiled[filter_id])
            self.next_refresh = datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+1)

    def __call__(self, prompt):
        self.refresh_regex()
        prompt_suspicion = 0
        if "###" in prompt:
            prompt, negprompt = prompt.split("###", 1)
        matching_groups = []
        for filters in [self.filters1, self.filters2]:
            for filter_id in filters:
                # We only need 1 of the filters in the group to match to increase suspicion
                # Suspicion does not increase further for more filters in the same group
                if self.compiled[filter_id]:
                    match_result = self.compiled[filter_id].search(prompt)
                    if match_result:
                        prompt_suspicion += 1
                        matching_groups.append(match_result.group())
                        break
        return prompt_suspicion,matching_groups

prompt_checker = PromptChecker()