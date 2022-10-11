import json, os, sys
from uuid import uuid4
from datetime import datetime
import threading, time, dateutil.relativedelta
from .. import logger, args
from ...vars import thing_name,raw_thing_name,thing_divisor,things_per_sec_suspicion_threshold
import uuid, re, random
from ...utils import is_profane
from ... import raid
from enum import IntEnum
from .news import News

class Suspicions(IntEnum):
    WORKER_NAME_LONG = 0
    WORKER_NAME_EXTREME = 1
    WORKER_PROFANITY = 2
    UNSAFE_IP = 3
    EXTREME_MAX_PIXELS = 4
    UNREASONABLY_FAST = 5
    USERNAME_LONG = 6
    USERNAME_PROFANITY = 7

suspicion_logs = {
    Suspicions.WORKER_NAME_LONG: 'Worker Name too long',
    Suspicions.WORKER_NAME_EXTREME: 'Worker Name extremely long',
    Suspicions.WORKER_PROFANITY: 'Discovered profanity in worker name {}',
    Suspicions.UNSAFE_IP: 'Worker using unsafe IP',
    Suspicions.EXTREME_MAX_PIXELS: 'Worker claiming they can generate too many pixels',
    Suspicions.UNREASONABLY_FAST: 'Generation unreasonably fast ({})',
    Suspicions.USERNAME_LONG: 'Username too long',
    Suspicions.USERNAME_PROFANITY: 'Profanity in username'
}

class WaitingPrompt:
    extra_priority = 0
    def __init__(self, db, wps, pgs, prompt, user, params, **kwargs):
        self.tricked_workers = []
        self.db = db
        self._waiting_prompts = wps
        self._processing_generations = pgs
        self.prompt = prompt
        self.user = user
        self.params = params
        self.total_usage = 0
        self.nsfw = kwargs.get("nsfw", False)
        self.trusted_workers = kwargs.get("trusted_workers", True)
        self.extract_params(params, **kwargs)
        self.id = str(uuid4())
        # The generations that have been created already
        self.processing_gens = []
        self.fake_gens = []
        self.last_process_time = datetime.now()
        self.workers = kwargs.get("workers", [])
        # Prompt requests are removed after 1 mins of inactivity per n, to a max of 5 minutes
        self.stale_time = 1200

    # These are typically worker-specific so they will be defined in the specific class for this horde type
    def extract_params(self, params, **kwargs):
        self.n = params.pop('n', 1)
        # This specific per horde so it should be set in the extended class
        self.things = 0
        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.prepare_job_payload(params)

    def prepare_job_payload(self, initial_dict = {}):
        # This is what we send to the worker
        self.gen_payload = initial_dict
    
    def get_job_payload(self):
        return(self.gen_payload)

    def activate(self):
        '''We separate the activation from __init__ as often we want to check if there's a valid worker for it
        Before we add it to the queue
        '''
        self._waiting_prompts.add_item(self)
        thread = threading.Thread(target=self.check_for_stale, args=())
        thread.daemon = True
        thread.start()

    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, worker):
        if self.n <= 0:
            return
        new_gen = self.new_procgen(worker)
        self.processing_gens.append(new_gen)
        self.n -= 1
        self.refresh()
        return(self.get_pop_payload(new_gen.id))

    def fake_generation(self, worker):
        new_gen = self.new_procgen(worker)
        new_gen.fake = True
        self.fake_gens.append(new_gen)
        self.tricked_workers.append(worker)
        return(self.get_pop_payload(new_gen.id))
    
    def tricked_worker(self, worker):
        return(worker in self.tricked_workers)

    def get_pop_payload(self, procgen_id):
        prompt_payload = {
            "payload": self.get_job_payload(),
            "id": procgen_id,
        }
        return(prompt_payload)

    # Using this function so that I can extend it to have it grab the correct extended class
    def new_procgen(self, worker):
        return(ProcessingGeneration(self, self._processing_generations, worker))

    def is_completed(self):
        if self.needs_gen():
            return(False)
        for procgen in self.processing_gens:
            if not procgen.is_completed():
                return(False)
        return(True)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
        }
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["finished"] += 1
            else:
                ret_dict["processing"] += 1
        return(ret_dict)

    def get_queued_things(self):
        '''The things still queued to be generated for this waiting prompt'''
        return(round(self.things * self.n/thing_divisor,2))

    def get_status(self, lite = False):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        ret_dict["done"] = self.is_completed()
        # Lite mode does not include the generations, to spare me download size
        if not lite:
            ret_dict["generations"] = []
            for procgen in self.processing_gens:
                if procgen.is_completed():
                    ret_dict["generations"].append(procgen.get_details())
        queue_pos, queued_things, queued_n = self.get_own_queue_stats()
        # We increment the priority by 1, because it starts at 0
        # This means when all our requests are currently processing or done, with nothing else in the queue, we'll show queue position 0 which is appropriate.
        ret_dict["queue_position"] = queue_pos + 1
        active_workers = self.db.count_active_workers()
        # If there's less requests than the number of active workers
        # Then we need to adjust the parallelization accordingly
        if queued_n < active_workers:
            active_workers = queued_n
        avg_things_per_sec = (self.db.stats.get_request_avg() / thing_divisor) * active_workers
        # Is this is 0, it means one of two things:
        # 1. This horde hasn't had any requests yet. So we'll initiate it to 1 avg_things_per_sec
        # 2. All gens for this WP are being currently processed, so we'll just set it to 1 to avoid a div by zero, but it's not used anyway as it will just divide 0/1
        if avg_things_per_sec == 0:
            avg_things_per_sec = 1
        wait_time = queued_things / avg_things_per_sec
        # We add the expected running time of our processing gens
        for procgen in self.processing_gens:
            wait_time += procgen.get_expected_time_left()
        ret_dict["wait_time"] = round(wait_time)
        return(ret_dict)

    def get_lite_status(self):
        '''Same as get_status(), but without the images to avoid unnecessary size'''
        ret_dict = self.get_status(True)
        return(ret_dict)

    def get_own_queue_stats(self):
        '''Get out position in the working prompts queue sorted by kudos
        If this gen is completed, we return (-1,-1) which represents this, to avoid doing operations.
        '''
        if self.needs_gen():
            return(self._waiting_prompts.get_wp_queue_stats(self))
        return(-1,0,0)

    def record_usage(self, raw_things, kudos):
        '''Record that we received a requested generation and how much kudos it costs us
        We use 'thing' here as we do not care what type of thing we're recording at this point
        This avoids me having to extend this just to change a var name
        '''
        self.user.record_usage(raw_things, kudos)
        self.refresh()

    def check_for_stale(self):
        while True:
            if self._waiting_prompts.is_deleted(self):
                break
            if self.is_stale():
                self.delete()
                break
            time.sleep(10)
            self.extra_priority += 50

    def delete(self):
        for gen in self.processing_gens:
            gen.delete()
        for gen in self.fake_gens:
            gen.delete()
        self._waiting_prompts.del_item(self)
        del self

    def refresh(self):
        self.last_process_time = datetime.now()

    def is_stale(self):
        if (datetime.now() - self.last_process_time).seconds > self.stale_time:
            return(True)
        return(False)

    def get_priority(self):
        return(self.user.kudos + self.extra_priority)

class ProcessingGeneration:
    generation = None
    seed = None
    fake = False
 
    def __init__(self, owner, pgs, worker):
        self._processing_generations = pgs
        self.id = str(uuid4())
        self.owner = owner
        self.worker = worker
        self.model = worker.model
        self.start_time = datetime.now()
        self._processing_generations.add_item(self)

    # We allow the seed to not be sent
    def set_generation(self, generation, **kwargs):
        if self.is_completed():
            return(0)
        self.generation = generation
        # Support for two typical properties 
        self.seed = kwargs.get('seed', None)
        things_per_sec = self.owner.db.stats.record_fulfilment(self.owner.things, self.start_time)
        self.kudos = self.owner.db.convert_things_to_kudos(self.owner.things, seed = self.seed, model_name = self.model)
        if self.fake and self.worker.user != self.owner.user:
            # We do not record usage for paused workers, unless the requestor was the same owner as the worker
            self.worker.record_contribution(raw_things = self.owner.things, kudos = self.kudos, things_per_sec = things_per_sec)
            logger.info(f"Fake Generation worth {self.kudos} kudos, delivered by worker: {self.worker.name}")
        else:
            self.worker.record_contribution(raw_things = self.owner.things, kudos = self.kudos, things_per_sec = things_per_sec)
            self.owner.record_usage(raw_things = self.owner.things, kudos = self.kudos)
            logger.info(f"New Generation worth {self.kudos} kudos, delivered by worker: {self.worker.name}")
        return(self.kudos)

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def delete(self):
        self._processing_generations.del_item(self)
        del self

    def get_seconds_needed(self):
        return(self.owner.things / self.worker.get_performance_average())

    def get_expected_time_left(self):
        if self.is_completed():
            return(0)
        seconds_needed = self.get_seconds_needed()
        seconds_elapsed = (datetime.now() - self.start_time).seconds
        expected_time = seconds_needed - seconds_elapsed
        # In case we run into a slow request
        if expected_time < 0:
            expected_time = 0
        return(expected_time)

    # This should be extended by every horde type
    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "gen": procgen.generation,
            "worker_id": procgen.worker.id,
            "worker_name": procgen.worker.name,
        }
        return(ret_dict)

class Worker:
    suspicion_threshold = 3
    # Every how many seconds does this worker get a kudos reward
    uptime_reward_threshold = 600

    def __init__(self, db):
        self.last_reward_uptime = 0
        # Maintenance can be requested by the owner of the worker (to allow them to not pick up more requests)
        self.maintenance = False
        # Paused is set by the admins to prevent that worker from seeing any more requests
        # This can be used for stopping workers who misbhevave for example, without informing their owners
        self.paused = False
        # Extra comment about the worker, set by its owner
        self.info = None
        self.suspicious = 0
        self.kudos_details = {
            "generated": 0,
            "uptime": 0,
        }
        self.suspicions = []
        self.db = db

    def create(self, user, name, **kwargs):
        self.user = user
        self.name = name
        self.id = str(uuid4())
        self.contributions = 0
        self.fulfilments = 0
        self.kudos = 0
        self.performances = []
        self.uptime = 0
        self.check_for_bad_actor()
        if not self.is_suspicious():
            self.db.register_new_worker(self)

    def check_for_bad_actor(self):
        # Each worker starts at the suspicion level of its user
        self.suspicious = self.user.suspicious
        if len(self.name) > 100:
            if len(self.name) > 200:
                self.report_suspicion(reason = Suspicions.WORKER_NAME_EXTREMELY_LONG)
            self.name = self.name[:100]
            self.report_suspicion(reason = Suspicions.WORKER_NAME_LONG)
        if is_profane(self.name):
            self.report_suspicion(reason = Suspicions.WORKER_PROFANITY, formats = [self.name])

    def report_suspicion(self, amount = 1, reason = Suspicions.WORKER_PROFANITY, formats = []):
        # Unreasonable Fast can be added multiple times and it increases suspicion each time
        if int(reason) in self.suspicions and reason != Suspicions.UNREASONABLY_FAST:
            return
        self.suspicions.append(int(reason))
        self.suspicious += amount
        self.user.report_suspicion(amount, reason, formats)
        if reason:
            reason_log = suspicion_logs[reason].format(*formats)
            logger.warning(f"Worker '{self.id}' suspicion increased to {self.suspicious}. Reason: {reason_log}")
        if self.is_suspicious():
            self.paused = True

    def is_suspicious(self): 
        if self.user.trusted:
            return(False)       
        if self.suspicious >= self.suspicion_threshold:
            return(True)
        return(False)

    def set_name(self,new_name):
        if self.name == new_name:
            return("OK")        
        if is_profane(new_name):
            return("Profanity")
        if len(new_name) > 100:
            return("Too Long")
        ret = self.db.update_worker_name(self, new_name)
        if ret == 1:
            return("Already Exists")
        self.name = new_name
        return("OK")

    def set_info(self,new_info):
        if self.info == new_info:
            return("OK")
        if is_profane(new_info):
            return("Profanity")
        if len(new_info) > 1000:
            return("Too Long")
        self.info = new_info
        return("OK")

    # This should be overwriten by each specific horde
    def calculate_uptime_reward(self):
        return(100)

    # This should be extended by each specific horde
    def check_in(self, **kwargs):
        self.model = kwargs.get("model")
        self.nsfw = kwargs.get("nsfw", True)
        self.blacklist = kwargs.get("blacklist", [])
        self.ipaddr = kwargs.get("ipaddr", None)
        if not kwargs.get("safe_ip", True):
            if not self.user.trusted:
                self.report_suspicion(reason = Suspicions.UNSAFE_IP)
        if kwargs.get("max_pixels", 512*512) > 2048 * 2048:
            if not self.user.trusted:
                self.report_suspicion(reason = Suspicions.EXTREME_MAX_PIXELS)
        if not self.is_stale() and not self.paused:
            self.uptime += (datetime.now() - self.last_check_in).seconds
            # Every 10 minutes of uptime gets 100 kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                kudos = self.calculate_uptime_reward()
                self.modify_kudos(kudos,'uptime')
                self.user.record_uptime(kudos)
                logger.debug(f"Worker '{self.name}' received {kudos} kudos for uptime of {self.uptime_reward_threshold} seconds.")
                self.last_reward_uptime = self.uptime
        else:
            # If the worker comes back from being stale, we just reset their last_reward_uptime
            # So that they have to stay up at least 10 mins to get uptime kudos
            self.last_reward_uptime = self.uptime
        self.last_check_in = datetime.now()

    def get_human_readable_uptime(self):
        if self.uptime < 60:
            return(f"{self.uptime} seconds")
        elif self.uptime < 60*60:
            return(f"{round(self.uptime/60,2)} minutes")
        elif self.uptime < 60*60*24:
            return(f"{round(self.uptime/60/60,2)} hours")
        else:
            return(f"{round(self.uptime/60/60/24,2)} days")

    def can_generate(self, waiting_prompt):
        # takes as an argument a WaitingPrompt class and checks if this worker is valid for generating it
        is_matching = True
        skipped_reason = None
        if self.is_stale():
            # We don't consider stale workers in the request, so we don't need to report a reason
            is_matching = False
        # If the request specified only specific workers to fulfill it, and we're not one of them, we skip
        if len(waiting_prompt.workers) >= 1 and self.id not in waiting_prompt.workers:
            is_matching = False
            skipped_reason = 'worker_id'
        if waiting_prompt.nsfw and not self.nsfw:
            is_matching = False
            skipped_reason = 'nsfw'
        if waiting_prompt.trusted_workers and not self.user.trusted:
            is_matching = False
            skipped_reason = 'untrusted'
        # If the worker has been tricked once by this prompt, we don't want to resend it it
        # as it may give up the jig
        if waiting_prompt.tricked_worker(self):
            is_matching = False
            skipped_reason = 'secret'
        if any(word in waiting_prompt.prompt for word in self.blacklist):
            is_matching = False
            skipped_reason = 'blacklist'
        return([is_matching,skipped_reason])

    # We split it to its own function to make it extendable
    def convert_contribution(self,raw_things):
        self.contributions = round(self.contributions + raw_things/thing_divisor,2)

    @logger.catch
    def record_contribution(self, raw_things, kudos, things_per_sec):
        '''We record the servers newest contribution
        We do not need to know what type the contribution is, to avoid unnecessarily extending this method
        '''
        self.user.record_contributions(raw_things = raw_things, kudos = kudos)
        self.modify_kudos(kudos,'generated')
        self.convert_contribution(raw_things)
        self.fulfilments += 1
        self.performances.append(things_per_sec)
        if things_per_sec / thing_divisor > things_per_sec_suspicion_threshold:
            self.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[round(things_per_sec / thing_divisor,2)])
        if len(self.performances) > 20:
            del self.performances[0]

    def modify_kudos(self, kudos, action = 'generated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + abs(kudos), 2) 

    def get_performance_average(self):
        if len(self.performances):
            ret_num = sum(self.performances) / len(self.performances)
        else:
            # Always sending at least 1 thing per second, to avoid divisions by zero
            ret_num = 1
        return(ret_num)

    def get_performance(self):
        if len(self.performances):
            ret_str = f'{round(sum(self.performances) / len(self.performances) / thing_divisor,1)} {thing_name} per second'
        else:
            ret_str = f'No requests fulfilled yet'
        return(ret_str)

    def is_stale(self):
        try:
            if (datetime.now() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new worker, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    # Should be extended by each specific horde
    @logger.catch
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "requests_fulfilled": self.fulfilments,
            "kudos_rewards": self.kudos,
            "kudos_details": self.kudos_details,
            "performance": self.get_performance(),
            "uptime": self.uptime,
            "maintenance_mode": self.maintenance,
            "info": self.info,
            "nsfw": self.nsfw,
            "trusted": self.user.trusted,
        }
        if details_privilege >= 2:
            ret_dict['paused'] = self.paused
            ret_dict['suspicious'] = self.suspicious
        if details_privilege >= 1 or self.user.public_workers:
            ret_dict['owner'] = self.user.get_unique_alias()
        return(ret_dict)

    # Should be extended by each specific horde
    @logger.catch
    def serialize(self):
        ret_dict = {
            "oauth_id": self.user.oauth_id,
            "name": self.name,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details.copy(),
            "performances": self.performances.copy(),
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "uptime": self.uptime,
            "paused": self.paused,
            "maintenance": self.maintenance,
            "info": self.info,
            "nsfw": self.nsfw,
            "blacklist": self.blacklist.copy(),
            "ipaddr": self.ipaddr,
            "suspicions": self.suspicions,
        }
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        self.user = self.db.find_user_by_oauth_id(saved_dict["oauth_id"])
        self.name = saved_dict["name"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.kudos = saved_dict.get("kudos",0)
        self.kudos_details = saved_dict.get("kudos_details",self.kudos_details)
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.uptime = saved_dict.get("uptime",0)
        self.maintenance = saved_dict.get("maintenance",False)
        self.paused = saved_dict.get("paused",False)
        self.info = saved_dict.get("info",None)
        self.nsfw = saved_dict.get("nsfw",True)
        self.blacklist = saved_dict.get("blacklist",[])
        self.ipaddr = saved_dict.get("ipaddr", None)
        self.suspicions = saved_dict.get("suspicions", [])
        for suspicion in self.suspicions:
            self.suspicious += 1
            logger.debug(f"Suspecting worker {self.name} for {self.suspicious} with reasons {self.suspicions}")
        self.check_for_bad_actor()
        if convert_flag == "prune_bad_worker" and not self.is_suspicious():
            self.db.workers[self.name] = self
        if convert_flag == "kudos_fix":
            multiplier = 20
            # Average kudos in the kobold horde is much bigger
            if args.horde == 'kobold':
                multiplier = 100
            recalc_kudos =  (self.fulfilments) * multiplier
            self.kudos = recalc_kudos + self.kudos_details.get("uptime",0)
            self.kudos_details['generated'] = recalc_kudos
            self.user.kudos_details['accumulated'] += self.kudos_details['uptime']
            self.user.kudos += self.kudos_details['uptime']



class Index:
    def __init__(self):
        self._index = {}

    def add_item(self, item):
        self._index[item.id] = item

    def get_item(self, uuid):
        return(self._index.get(uuid))

    def del_item(self, item):
        del self._index[item.id]

    def get_all(self):
        return(self._index.values())

    def is_deleted(self,item):
        if item.id in self._index:
            return(False)
        return(True)

class PromptsIndex(Index):

    def count_waiting_requests(self, user):
        count = 0
        for wp in self._index.values():
            if wp.user == user and not wp.is_completed():
                count += wp.n
        return(count)

    def count_total_waiting_generations(self):
        count = 0
        for wp in self._index.values():
            count += wp.n + wp.count_processing_gens()["processing"]
        return(count)

    def count_totals(self):
        queued_thing = f"queued_{thing_name}"
        ret_dict = {
            "queued_requests": 0,
            queued_thing: 0,
        }
        for wp in self._index.values():
            current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
            ret_dict["queued_requests"] += current_wp_queue
            if current_wp_queue > 0:
                ret_dict[queued_thing] += wp.things * current_wp_queue / thing_divisor
        # We round the end result to avoid to many decimals
        ret_dict[queued_thing] = round(ret_dict[queued_thing],2)
        return(ret_dict)


    def get_waiting_wp_by_kudos(self):
        sorted_wp_list = sorted(self._index.values(), key=lambda x: x.get_priority(), reverse=True)
        final_wp_list = []
        for wp in sorted_wp_list:
            if wp.needs_gen():
                final_wp_list.append(wp)
        # logger.debug([(wp,wp.get_priority()) for wp in final_wp_list])
        return(final_wp_list)

    # Returns the queue position of the provided WP based on kudos
    # Also returns the amount of things until the wp is generated
    # Also returns the amount of different gens queued
    def get_wp_queue_stats(self, wp):
        things_ahead_in_queue = 0
        n_ahead_in_queue = 0
        priority_sorted_list = self.get_waiting_wp_by_kudos()
        for iter in range(len(priority_sorted_list)):
            things_ahead_in_queue += priority_sorted_list[iter].get_queued_things()
            n_ahead_in_queue += priority_sorted_list[iter].n
            if priority_sorted_list[iter] == wp:
                things_ahead_in_queue = round(things_ahead_in_queue,2)
                return(iter, things_ahead_in_queue, n_ahead_in_queue)
        # -1 means the WP is done and not in the queue
        return(-1,0,0)
                

class GenerationsIndex(Index):
    pass


class User:
    suspicion_threshold = 3

    def __init__(self, db):
        self.suspicious = 0
        self.worker_invited = 0
        self.moderator = False
        self.concurrency = 30
        self.usage_multiplier = 1.0
        self.kudos = 0
        self.same_ip_worker_threshold = 3
        self.public_workers = False
        self.trusted = False
        self.evaluating_kudos = 0
        self.kudos_details = {
            "accumulated": 0,
            "gifted": 0,
            "admin": 0,
            "received": 0,
            "recurring": 0,
        }
        self.monthly_kudos = {
            "amount": 0,
            "last_received": None,
        }
        self.suspicions = []
        self.db = db

    def create_anon(self):
        self.username = 'Anonymous'
        self.oauth_id = 'anon'
        self.api_key = '0000000000'
        self.invite_id = ''
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = 0
        self.public_workers = True
        self.contributions = {
            thing_name: 0,
            "fulfillments": 0
        }
        self.usage = {
            thing_name: 0,
            "requests": 0
        }
        # We allow anonymous users more leeway for the max amount of concurrent requests
        # This is balanced by their lower priority
        self.concurrency = 200

    def create(self, username, oauth_id, api_key, invite_id):
        self.username = username
        self.oauth_id = oauth_id
        self.api_key = api_key
        self.invite_id = invite_id
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.check_for_bad_actor()
        self.id = self.db.register_new_user(self)
        self.contributions = {
            thing_name: 0,
            "fulfillments": 0
        }
        self.usage = {
            thing_name: 0,
            "requests": 0
        }

    def check_for_bad_actor(self):
        if len(self.username) > 30:
            self.username = self.username[:30]
            self.report_suspicion(reason = Suspicions.USERNAME_LONG)
        if is_profane(self.username):
            self.report_suspicion(reason = Suspicions.USERNAME_PROFANITY)

    # Checks that this user matches the specified API key
    def check_key(api_key):
        if self.api_key and self.api_key == api_key:
            return(True)
        return(False)

    def set_username(self,new_username):
        if is_profane(new_username):
            return("Profanity")
        if len(new_username) > 30:
            return("Too Long")
        self.username = new_username
        return("OK")

    def set_trusted(self,is_trusted):
        self.trusted = is_trusted
        if self.trusted:
            for worker in self.get_workers():
                worker.paused = False

    def set_moderator(self,is_moderator):
        self.moderator = is_moderator
        if self.moderator:
            logger.warning(f"{self.username} Set as moderator")
            self.set_trusted(True)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def record_usage(self, raw_things, kudos):
        self.usage["requests"] += 1
        self.modify_kudos(-kudos,"accumulated")
        self.usage[thing_name] = round(self.usage[thing_name] + (raw_things * self.usage_multiplier / thing_divisor),2)

    def record_contributions(self, raw_things, kudos):
        self.contributions["fulfillments"] += 1
        # While a worker is untrusted, half of all generated kudos go for evaluation
        if not self.trusted:
            kudos_eval = round(kudos / 2)
            kudos -= kudos_eval
            self.evaluating_kudos += kudos_eval
            self.modify_kudos(kudos,"accumulated")
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")
        self.contributions[thing_name] = round(self.contributions[thing_name] + raw_things/thing_divisor,2)

    def record_uptime(self, kudos):
        # While a worker is untrusted, all uptime kudos go for evaluation
        if not self.trusted:
            self.evaluating_kudos += kudos
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")

    def check_for_trust(self):
        '''After a user passes the evaluation threshold (50000 kudos)
        All the evaluating Kudos added to their total and they automatically become trusted
        Suspicious users do not automatically pass evaluation
        '''
        if self.evaluating_kudos >= int(os.getenv("KUDOS_TRUST_THRESHOLD")) and not self.is_suspicious():
            self.modify_kudos(self.evaluating_kudos,"accumulated")
            self.evaluating_kudos = 0
            self.set_trusted(True)

    def modify_monthly_kudos(self, monthly_kudos):
        # We always give upfront the monthly kudos to the user once.
        # If they already had some, we give the difference but don't change the date
        if monthly_kudos > 0:
            self.modify_kudos(monthly_kudos, "recurring")
        if not self.monthly_kudos["last_received"]:
            self.monthly_kudos["last_received"] = datetime.now()
        self.monthly_kudos["amount"] += monthly_kudos
        if self.monthly_kudos["amount"] < 0:
            self.monthly_kudos["amount"] = 0

    def receive_monthly_kudos(self):
        kudos_amount = self.calculate_monthly_kudos()
        if kudos_amount == 0:
            return
        if self.monthly_kudos["last_received"]:
            has_month_passed = datetime.now() > self.monthly_kudos["last_received"] + dateutil.relativedelta.relativedelta(months=+6)
        else:
            # If the user is supposed to receive Kudos, but doesn't have a last received date, it means it is a moderator who hasn't received it the first time
            has_month_passed = True
        if has_month_passed:
            self.modify_kudos(kudos_amount, "recurring")
            self.monthly_kudos["last_received"] = datetime.now()
            logger.info(f"User {self.get_unique_alias()} received their {kudos_amount} monthly Kudos")

    def calculate_monthly_kudos(self):
        base_amount = self.monthly_kudos['amount']
        if self.moderator:
            base_amount += 100000
        return(base_amount)

    def modify_kudos(self, kudos, action = 'accumulated'):
        logger.debug(f"modifying existing {self.kudos} kudos of {self.get_unique_alias()} by {kudos} for {action}")
        self.kudos = round(self.kudos + kudos, 2)
        self.ensure_kudos_positive()
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + kudos, 2)

    def ensure_kudos_positive(self):
        if self.kudos < 0 and self.is_anon():
            self.kudos = 0
        elif self.kudos < 1 and self.is_pseudonymus():
            self.kudos = 1
        elif self.kudos < 2:
            self.kudos = 2

    def is_anon(self):
        if self.oauth_id == 'anon':
            return(True)
        return(False)

    def is_pseudonymus(self):
        try:
            uuid.UUID(str(self.oauth_id))
            return(True)
        except ValueError:
            return(False)

    @logger.catch
    def get_details(self, details_privilege = 0):
        ret_dict = {
            "username": self.get_unique_alias(),
            "id": self.id,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details,
            "usage": self.usage,
            "contributions": self.contributions,
            "concurrency": self.concurrency,
            "worker_invited": self.worker_invited,
            "moderator": self.moderator,
            "trusted": self.trusted,
            "suspicious": self.suspicious,
            "worker_count": self.count_workers(),
            # unnecessary information, since the workers themselves wil be visible
            # "public_workers": self.public_workers,
        }
        if self.public_workers or details_privilege >= 1:
            workers_array = []
            for worker in self.get_workers():
                workers_array.append(worker.id)
            ret_dict["worker_ids"] = workers_array
        if details_privilege >= 2:
            mk_dict = {
                "amount": self.calculate_monthly_kudos(),
                "last_received": self.monthly_kudos["last_received"]
            }
            ret_dict["evaluating_kudos"] = self.evaluating_kudos
            ret_dict["monthly_kudos"] = mk_dict
        return(ret_dict)

    def report_suspicion(self, amount = 1, reason = Suspicions.USERNAME_PROFANITY, formats = []):
        # Anon is never considered suspicious
        if self.is_anon():
            return
        if int(reason) in self.suspicions and reason != Suspicions.UNREASONABLY_FAST:
            return
        self.suspicions.append(int(reason))
        self.suspicious += amount
        if reason:
            reason_log = suspicion_logs[reason].format(*formats)
            logger.warning(f"User '{self.id}' suspicion increased to {self.suspicious}. Reason: {reason}")

    def get_workers(self):
        return(self.db.find_workers_by_user(self))
    
    def count_workers(self):
        return(len(self.get_workers()))

    def is_suspicious(self): 
        if self.trusted:
            return(False)       
        if self.suspicious >= self.suspicion_threshold:
            return(True)
        return(False)

    def exceeding_ipaddr_restrictions(self, ipaddr):
        '''Checks that the ipaddr of the new worker does not have too many other workers
        to prevent easy spamming of new workers with a script
        '''
        ipcount = 0
        for worker in self.get_workers():
            if worker.ipaddr == ipaddr:
                ipcount += 1
        if ipcount > self.same_ip_worker_threshold and ipcount > self.worker_invited:
            return(True)
        return(False)

    @logger.catch
    def serialize(self):
        serialized_monthly_kudos = {
            "amount": self.monthly_kudos["amount"],
        }
        if self.monthly_kudos["last_received"]:
            serialized_monthly_kudos["last_received"] = self.monthly_kudos["last_received"].strftime("%Y-%m-%d %H:%M:%S")
        else:
            serialized_monthly_kudos["last_received"] = None
        ret_dict = {
            "username": self.username,
            "oauth_id": self.oauth_id,
            "api_key": self.api_key,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details.copy(),
            "id": self.id,
            "invite_id": self.invite_id,
            "contributions": self.contributions.copy(),
            "usage": self.usage.copy(),
            "usage_multiplier": self.usage_multiplier,
            "concurrency": self.concurrency,
            "worker_invited": self.worker_invited,
            "moderator": self.moderator,
            "suspicions": self.suspicions,
            "public_workers": self.public_workers,
            "trusted": self.trusted,
            "creation_date": self.creation_date.strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": self.last_active.strftime("%Y-%m-%d %H:%M:%S"),
            "monthly_kudos": serialized_monthly_kudos,
            "evaluating_kudos": self.evaluating_kudos,
        }
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        self.username = saved_dict["username"]
        self.oauth_id = saved_dict["oauth_id"]
        self.api_key = saved_dict["api_key"]
        self.kudos = saved_dict["kudos"]
        self.kudos_details = saved_dict.get("kudos_details", self.kudos_details)
        self.id = saved_dict["id"]
        self.invite_id = saved_dict["invite_id"]
        self.contributions = saved_dict["contributions"]
        self.usage = saved_dict["usage"]
        self.concurrency = saved_dict.get("concurrency", 30)
        self.usage_multiplier = saved_dict.get("usage_multiplier", 1.0)
        # I am putting int() here, to convert a boolean entry I had in the past
        self.worker_invited = int(saved_dict.get("worker_invited", 0))
        self.suspicions = saved_dict.get("suspicions", [])
        for suspicion in self.suspicions:
            self.suspicious += 1
            logger.debug(f"Suspecting user {self.get_unique_alias()} for {self.suspicious} with reasons {self.suspicions}")
        self.public_workers = saved_dict.get("public_workers", False)
        self.trusted = saved_dict.get("trusted", False)
        self.evaluating_kudos = saved_dict.get("evaluating_kudos", 0)
        self.set_moderator(saved_dict.get("moderator", False))
        serialized_monthly_kudos = saved_dict.get("monthly_kudos")
        if serialized_monthly_kudos and serialized_monthly_kudos['last_received'] != None:
            self.monthly_kudos['amount'] = serialized_monthly_kudos['amount']
            self.monthly_kudos['last_received'] = datetime.strptime(serialized_monthly_kudos['last_received'],"%Y-%m-%d %H:%M:%S")
        if self.is_anon():
            self.concurrency = 200
            self.public_workers = True
        self.creation_date = datetime.strptime(saved_dict["creation_date"],"%Y-%m-%d %H:%M:%S")
        self.last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")
        if convert_flag == "kudos_fix":
            multiplier = 20
            if args.horde == 'kobold':
                multiplier = 100
            recalc_kudos =  (self.contributions['fulfillments'] - self.usage['requests']) * multiplier
            self.kudos = recalc_kudos + self.kudos_details.get('admin',0) + self.kudos_details.get('received',0) - self.kudos_details.get('gifted',0)
            self.kudos_details['accumulated'] = recalc_kudos
        self.ensure_kudos_positive()


class Stats:
    worker_performances = []
    fulfillments = []

    def __init__(self, db, convert_flag = None, interval = 60):
        self.db = db
        self.interval = interval
        self.last_pruning = datetime.now()

    def record_fulfilment(self, things, starting_time):
        seconds_taken = (datetime.now() - starting_time).seconds
        if seconds_taken == 0:
            things_per_sec = 1
        else:
            things_per_sec = round(things / seconds_taken,1)
        if len(self.worker_performances) >= 10:
            del self.worker_performances[0]
        self.worker_performances.append(things_per_sec)
        fulfillment_dict = {
            raw_thing_name: things,
            "start_time": starting_time,
            "deliver_time": datetime.now(),
        }
        self.fulfillments.append(fulfillment_dict)
        return(things_per_sec)

    def get_things_per_min(self):
        total_things = 0
        pruned_array = []
        for fulfillment in self.fulfillments:
            if (datetime.now() - fulfillment["deliver_time"]).seconds <= 60:
                pruned_array.append(fulfillment)
                total_things += fulfillment[raw_thing_name]
        if (datetime.now() - self.last_pruning).seconds > self.interval:
            self.last_pruning = datetime.now()
            self.fulfillments = pruned_array
            logger.debug("Pruned fulfillments")
        things_per_min = round(total_things / thing_divisor,2)
        return(things_per_min)

    def get_request_avg(self):
        if len(self.worker_performances) == 0:
            return(0)
        avg = sum(self.worker_performances) / len(self.worker_performances)
        return(round(avg,1))

    @logger.catch
    def serialize(self):
        serialized_fulfillments = []
        for fulfillment in self.fulfillments.copy():
            json_fulfillment = {
                raw_thing_name: fulfillment[raw_thing_name],
                "start_time": fulfillment["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "deliver_time": fulfillment["deliver_time"].strftime("%Y-%m-%d %H:%M:%S"),
            }
            serialized_fulfillments.append(json_fulfillment)
        ret_dict = {
            "worker_performances": self.worker_performances,
            "fulfillments": serialized_fulfillments,
        }
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        # Convert old key
        if "server_performances" in saved_dict:
            self.worker_performances = saved_dict["server_performances"]
        else:
            self.worker_performances = saved_dict["worker_performances"]
        deserialized_fulfillments = []
        for fulfillment in saved_dict.get("fulfillments", []):
            class_fulfillment = {
                raw_thing_name: fulfillment[raw_thing_name],
                "start_time": datetime.strptime(fulfillment["start_time"],"%Y-%m-%d %H:%M:%S"),
                "deliver_time":datetime.strptime(fulfillment["deliver_time"],"%Y-%m-%d %H:%M:%S"),
            }
            deserialized_fulfillments.append(class_fulfillment)
        self.fulfillments = deserialized_fulfillments
       
class Database:
    def __init__(self, convert_flag = None, interval = 60):
        self.interval = interval
        self.ALLOW_ANONYMOUS = True
        # This is used for synchronous generations
        self.WORKERS_FILE = "db/workers.json"
        self.workers = {}
        # Other miscellaneous statistics
        self.STATS_FILE = "db/stats.json"
        self.stats = self.new_stats()
        self.USERS_FILE = "db/users.json"
        self.users = {}
        # Increments any time a new user is added
        # Is appended to usernames, to ensure usernames never conflict
        self.last_user_id = 0
        logger.init(f"Database Load", status="Starting")
        if convert_flag:
            logger.init_warn(f"Convert Flag '{convert_flag}' received.", status="Converting")
        if os.path.isfile(self.USERS_FILE):
            with open(self.USERS_FILE) as db:
                serialized_users = json.load(db)
                for user_dict in serialized_users:
                    if not user_dict:
                        logger.error("Found null user on db load. Bypassing")
                        continue
                    new_user = self.new_user()
                    new_user.deserialize(user_dict,convert_flag)
                    self.users[new_user.oauth_id] = new_user
                    if new_user.id > self.last_user_id:
                        self.last_user_id = new_user.id
        self.anon = self.find_user_by_oauth_id('anon')
        if not self.anon:
            self.anon = User(self)
            self.anon.create_anon()
            self.users[self.anon.oauth_id] = self.anon
        if os.path.isfile(self.WORKERS_FILE):
            with open(self.WORKERS_FILE) as db:
                serialized_workers = json.load(db)
                for worker_dict in serialized_workers:
                    if not worker_dict:
                        logger.error("Found null worker on db load. Bypassing")
                        continue
                    # This should not be possible. If its' there, it's a bad actor we want to remove
                    new_worker = self.new_worker()
                    new_worker.deserialize(worker_dict,convert_flag)
                    self.workers[new_worker.name] = new_worker
        if os.path.isfile(self.STATS_FILE):
            with open(self.STATS_FILE) as stats_db:
                self.stats.deserialize(json.load(stats_db),convert_flag)

        if convert_flag:
            self.write_files_to_disk()
            logger.init_ok(f"Convertion complete.", status="Exiting")
            sys.exit()
        thread = threading.Thread(target=self.write_files, args=())
        thread.daemon = True
        thread.start()
        monthly_kudos_thread = threading.Thread(target=self.assign_monthly_kudos, args=())
        monthly_kudos_thread.daemon = True
        monthly_kudos_thread.start()
        logger.init_ok(f"Database Load", status="Completed")

    # I don't know if I'm doing this right,  but I'm using these so that I can extend them from the extended DB
    # So that it will grab the extended classes from each horde type, and not the internal classes in this package
    def new_worker(self):
        return(Worker(self))
    def new_user(self):
        return(User(self))
    def new_stats(self):
        return(Stats(self))

    def write_files(self):
        logger.init_ok("Database Store Thread", status="Started")
        while True:
            self.write_files_to_disk()
            time.sleep(self.interval)

    @logger.catch
    def write_files_to_disk(self):
        if not os.path.exists('db'):
            os.mkdir('db')
        worker_serialized_list = []
        logger.debug("Saving DB")
        for worker in self.workers.copy().values():
            # We don't store data for anon workers
            if worker.user == self.anon: continue
            worker_serialized_list.append(worker.serialize())
        with open(self.WORKERS_FILE, 'w') as db:
            json.dump(worker_serialized_list,db)
        with open(self.STATS_FILE, 'w') as db:
            json.dump(self.stats.serialize(),db)
        user_serialized_list = []
        for user in self.users.copy().values():
            user_serialized_list.append(user.serialize())
        with open(self.USERS_FILE, 'w') as db:
            json.dump(user_serialized_list,db)

    def assign_monthly_kudos(self):
        logger.init_ok("Monthly Kudos Awards Thread", status="Started")
        while True:
            for user in self.users.values():
                user.receive_monthly_kudos()
            # Check once a day
            time.sleep(86400)

    def get_top_contributor(self):
        top_contribution = 0
        top_contributor = None
        user = None
        for user in self.users.values():
            if user.contributions[thing_name] > top_contribution and user != self.anon:
                top_contributor = user
                top_contribution = user.contributions[thing_name]
        return(top_contributor)

    def get_top_worker(self):
        top_worker = None
        top_worker_contribution = 0
        for worker in self.workers:
            if self.workers[worker].contributions > top_worker_contribution:
                top_worker = self.workers[worker]
                top_worker_contribution = self.workers[worker].contributions
        return(top_worker)

    def count_active_workers(self):
        count = 0
        for worker in self.workers.values():
            if not worker.is_stale():
                count += 1
        return(count)

    def compile_workers_by_ip(self):
        workers_per_ip = {}
        for worker in self.workers.values():
            if worker.ipaddr not in workers_per_ip:
                workers_per_ip[worker.ipaddr] = []
            workers_per_ip[worker.ipaddr].append(worker)
        return(workers_per_ip)

    def count_workers_in_ipaddr(self,ipaddr):
        workers_per_ip = self.compile_workers_by_ip()
        found_workers = workers_per_ip.get(ipaddr,[])
        return(len(found_workers))

    def get_total_usage(self):
        totals = {
            thing_name: 0,
            "fulfilments": 0,
        }
        for worker in self.workers.values():
            totals[thing_name] += worker.contributions
            totals["fulfilments"] += worker.fulfilments
        return(totals)


    def register_new_user(self, user):
        self.last_user_id += 1
        self.users[user.oauth_id] = user
        logger.info(f'New user created: {user.username}#{self.last_user_id}')
        return(self.last_user_id)

    def register_new_worker(self, worker):
        self.workers[worker.name] = worker
        logger.info(f'New worker checked-in: {worker.name} by {worker.user.get_unique_alias()}')

    def find_user_by_oauth_id(self,oauth_id):
        if oauth_id == 'anon' and not self.ALLOW_ANONYMOUS:
            return(None)
        return(self.users.get(oauth_id))

    def find_user_by_username(self, username):
        for user in self.users.values():
            ulist = username.split('#')
            # This approach handles someone cheekily putting # in their username
            if user.username == "#".join(ulist[:-1]) and user.id == int(ulist[-1]):
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_user_by_id(self, user_id):
        for user in self.users.values():
            # The arguments passed to the URL are always strings
            if str(user.id) == user_id:
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_user_by_api_key(self,api_key):
        for user in self.users.values():
            if user.api_key == api_key:
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_worker_by_name(self,worker_name):
        return(self.workers.get(worker_name))

    def find_worker_by_id(self,worker_id):
        for worker in self.workers.values():
            if worker.id == worker_id:
                return(worker)
        return(None)

    def get_available_models(self):
        models_dict = {}
        for worker in self.workers.values():
            if worker.is_stale():
                continue
            mode_dict_template = {
                "name": worker.model,
                "count": 0,
            }
            models_dict[worker.model] = models_dict.get(worker.model, mode_dict_template)
            models_dict[worker.model]["count"] += 1
        return(list(models_dict.values()))

    def transfer_kudos(self, source_user, dest_user, amount):
        if amount > source_user.kudos:
            return([0,'Not enough kudos.'])
        source_user.modify_kudos(-amount, 'gifted')
        dest_user.modify_kudos(amount, 'received')
        return([amount,'OK'])

    def transfer_kudos_to_username(self, source_user, dest_username, amount):
        dest_user = self.find_user_by_username(dest_username)
        if not dest_user:
            return([0,'Invalid target username.'])
        if dest_user == self.anon:
            return([0,'Tried to burn kudos via sending to Anonymous. Assuming PEBKAC and aborting.'])
        if dest_user == source_user:
            return([0,'Cannot send kudos to yourself, ya monkey!'])
        kudos = self.transfer_kudos(source_user,dest_user, amount)
        return(kudos)

    def transfer_kudos_from_apikey_to_username(self, source_api_key, dest_username, amount):
        source_user = self.find_user_by_api_key(source_api_key)
        if not source_user:
            return([0,'Invalid API Key.'])
        if source_user == self.anon:
            return([0,'You cannot transfer Kudos from Anonymous, smart-ass.'])
        kudos = self.transfer_kudos_to_username(source_user, dest_username, amount)
        return(kudos)

    # Should be overriden
    def convert_things_to_kudos(self, things, **kwargs):
        # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
        kudos = round(things,2)
        return(kudos)


    def find_workers_by_user(self, user):
        found_workers = []
        for worker in self.workers.values():
            if worker.user == user:
                found_workers.append(worker)
        return(found_workers)
    
    def update_worker_name(self, worker, new_name):
        if new_name in self.workers:
            # If the name already exists, we return error code 1
            return(1)
        self.workers[new_name] = worker
        del self.workers[worker.name]
        logger.info(f'Worker renamed from {worker.name} to {new_name}')
