import os
import json
import regex as re
from datetime import datetime
import dateutil.relativedelta
from horde.logger import logger
from horde.horde_redis import horde_r_get
from horde.flask import HORDE, SQLITE_MODE # Local Testing
from horde.database.functions import compile_regex_filter, retrieve_regex_replacements
from horde.model_reference import model_reference
from unidecode import unidecode

class PromptChecker:
    
    def __init__(self):
        # I am using a string instead of just the integer ID of the filter
        # Because I am using the same ID as they key in redis to keep thigs well organized
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
        self.replacements = []
        # Used for scripting outside of this class
        self.known_ids = [10,11,20]
        self.filters1 = ["filter_10","filter_11"]
        self.filters2 = ["filter_20"]
        self.next_refresh = datetime.utcnow()
        self.refresh_regex()
        # These are checked on top of the normal
        self.nsfw_model_regex = re.compile(r"girl|\bboy\b|student|\byoung\b|lit[tl]le|\blil\b|small|\btiny|nina", re.IGNORECASE)
        self.nsfw_model_anime_regex = re.compile(r"(?<!1)girl|\b(?<!1)boy\b|student|\byoung\b|lit[tl]le|\blil\b|small|\btiny|nina", re.IGNORECASE)
        self.weight_remover = re.compile(r'\((.*?):\d+\.\d+\)')
        self.whitespace_remover = re.compile(r'(\s(\w)){3,}\b')
        self.whitespace_converter = re.compile(r"([^\w\s]|_)")
        self.csam_triggers = re.compile(r'\b(0?[0-9]|1[0-9]|2[0-2])(?![0-9]) *years? *old')

    def refresh_regex(self):
        # We don't want to be pulling the regex from redis all the time. We pull them only once per min
        if self.next_refresh > datetime.utcnow():
            return
        if SQLITE_MODE:
            with HORDE.app_context():
                stored_replacements = retrieve_regex_replacements(filter_type=10)
        else:
            cached_replacements = horde_r_get("cached_regex_replacements")
            if not cached_replacements:
                logger.warning("No cached regex replacements found in redis! Check threads!")
                stored_replacements = []
            try:
                stored_replacements = json.loads(cached_replacements)
            except:
                logger.warning("Errors when loading cached regex replacements in redis! Check threads!")
                stored_replacements = []
        for id in [10, 11, 20]:
            filter_id = f"filter_{id}"
            if SQLITE_MODE:
                with HORDE.app_context():
                    stored_filter = compile_regex_filter(id)
            else:
                stored_filter = horde_r_get(filter_id)
            # Ensure we don't get catch-all regex
            if not stored_filter:
                continue
            # Ensure we recompile the regex when they have actually changed.
            if self.regex[filter_id] != stored_filter:
                self.compiled[filter_id] = re.compile(stored_filter, re.IGNORECASE)
                self.regex[filter_id] = stored_filter
                logger.debug(self.compiled[filter_id])
            self.replacements = [
                {
                    "regex": re.compile(f_entry["regex"], re.IGNORECASE),
                    "replacement": f_entry["replacement"],
                }
                for f_entry in stored_replacements
            ]
            self.next_refresh = datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+1)

    def __call__(self, prompt, id = None):
        self.refresh_regex()
        prompt_suspicion = 0
        if "###" in prompt:
            prompt, negprompt = prompt.split("###", 1)
        prompt = self.normalize_prompt(prompt)
        # logger.debug(prompt)
        matching_groups = []
        for filters in [self.filters1, self.filters2]:
            for filter_id in filters:
                # This allows to check only a specific filter ID
                if id and filter_id != f"filter_{id}":
                    continue
                # We only need 1 of the filters in the group to match to increase suspicion
                # Suspicion does not increase further for more filters in the same group
                if self.compiled[filter_id]:
                    match_result = self.compiled[filter_id].search(prompt)
                    if match_result:
                        prompt_suspicion += 1
                        matching_groups.append(match_result.group())
                        break
        return prompt_suspicion,matching_groups

    def check_nsfw_model_block(self, prompt, models):
        # logger.debug([prompt, models])
        if not any(m in model_reference.nsfw_models for m in models):
            return False
        if "###" in prompt:
            prompt, negprompt = prompt.split("###", 1)
        prompt = self.normalize_prompt(prompt)
        if "Hentai Diffusion" in models and len(models) == 1:
            nsfw_match = self.nsfw_model_anime_regex.search(prompt)
        else:
            nsfw_match = self.nsfw_model_regex.search(prompt)
        if nsfw_match:
            return True
        prompt_10_suspicion, _ = self(prompt, 10)
        if prompt_10_suspicion:
            return True
        return False

    def check_csam_triggers(self, prompt):
        # logger.debug([prompt, models])
        if "###" in prompt:
            prompt, negprompt = prompt.split("###", 1)
        prompt = self.normalize_prompt(prompt)
        trigger_match = self.csam_triggers.search(prompt)
        if trigger_match:
            return trigger_match.group()
        return False


    # tests if the prompt is short enough to apply replacement filter on
    # negative prompt part is excluded. limit set to 350 chars (not tokens!).
    def check_prompt_replacement_length(self,prompt):
        if "###" in prompt:
            prompt, negprompt = prompt.split("###", 1)
        return len(prompt) < 350

    # this function takes a prompt input, and returns a filtered prompt instead
    # when a prompt is sanitized this way, additional negative prompts are also added
    def apply_replacement_filter(self,prompt):
        negprompt = ""
        if "###" in prompt:
            prompt, negprompt = prompt.split("###", 1)
            negprompt = ", "+negprompt

        # since this prompt was already flagged, ALWAYS force some additional NEGATIVE prompts to steer the generation
        #TODO: Remove "old", "mature", "middle-aged" from existing negprompt
        replacednegprompt = "###child, infant, underage, immature, teenager, tween" + negprompt

        # we also force the prompt to be normalized to avoid tricks, so nothing will escape the replacement regex
        # this means prompt weights are lost, but it is fine for textgen image prompts
        prompt = self.normalize_prompt(prompt) 
        #go through each filter rule and replace any matches sequentially
        for filter_entry in self.replacements:
            prompt = re.sub(
                filter_entry['regex'], 
                filter_entry['replacement'], 
                prompt
            ) 
        
        #if regex has eaten the entire prompt, we return None, which will use the previous approach of IP block.
        if prompt.strip() == '':
            return None

        #at this point all the matching stuff will be filtered out of the prompt. reconstruct sanitized prompt and return
        logger.debug(prompt + replacednegprompt)
        return prompt + replacednegprompt

        
        #you can decide if you want to strip punctuation as part of the prompt normalization. Either should work.
        #normal non-suspicious prompts will not touch this replacement filter anyway

    def normalize_prompt(self,prompt):
        """Prepares the prompt to be scanned by the regex, by removing tricks one might use to avoid the filters
        """
        prompt = self.weight_remover.sub(r'\1', prompt)
        prompt = self.whitespace_converter.sub(' ', prompt)
        for match in re.finditer(self.whitespace_remover, prompt):
            trim_match = match.group(0).strip()
            replacement = re.sub(r'\s+', '', trim_match)
            prompt = prompt.replace(trim_match, replacement)
        prompt = re.sub('\s+', ' ', prompt)
        # Remove all accents
        prompt = unidecode(prompt)
        return prompt


prompt_checker = PromptChecker()
# Test
# import sys
# sys.exit()