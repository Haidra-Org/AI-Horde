import json, os, sys
from uuid import uuid4
from datetime import datetime
import threading, time, dateutil.relativedelta, bleach
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
    CORRUPT_PROMPT = 8
    TOO_MANY_JOBS_ABORTED = 9

suspicion_logs = {
    Suspicions.WORKER_NAME_LONG: 'Worker Name too long',
    Suspicions.WORKER_NAME_EXTREME: 'Worker Name extremely long',
    Suspicions.WORKER_PROFANITY: 'Discovered profanity in worker name {}',
    Suspicions.UNSAFE_IP: 'Worker using unsafe IP',
    Suspicions.EXTREME_MAX_PIXELS: 'Worker claiming they can generate too many pixels',
    Suspicions.UNREASONABLY_FAST: 'Generation unreasonably fast ({})',
    Suspicions.USERNAME_LONG: 'Username too long',
    Suspicions.USERNAME_PROFANITY: 'Profanity in username',
    Suspicions.CORRUPT_PROMPT: 'Corrupt Prompt detected',
    Suspicions.TOO_MANY_JOBS_ABORTED: 'Too many jobs aborted in a short amount of time'
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
        self.ipaddr = kwargs.get("ipaddr", None)
        self.safe_ip = True
        self.trusted_workers = kwargs.get("trusted_workers", False)
        self.extract_params(params, **kwargs)
        self.id = str(uuid4())
        # The generations that have been created already
        self.processing_gens = []
        self.fake_gens = []
        self.last_process_time = datetime.now()
        self.workers = kwargs.get("workers", [])
        self.faulted = False
        # Prompt requests are removed after 1 mins of inactivity per n, to a max of 5 minutes
        self.stale_time = 1200
        self.set_job_ttl()
        # How many kudos this request consumed until now
        self.consumed_kudos = 0
        self.lock = threading.Lock()

    # These are typically worker-specific so they will be defined in the specific class for this horde type
    def extract_params(self, params, **kwargs):
        self.n = params.pop('n', 1)
        # We store the original amount of jobs requested as well
        self.jobs = self.n 
        # This specific per horde so it should be set in the extended class
        self.things = 0
        self.models = kwargs.get("models", ['ReadOnly'])
        self.total_usage = round(self.things * self.n / thing_divisor,2)
        self.prepare_job_payload(params)

    def prepare_job_payload(self, initial_dict = {}):
        # This is what we send to the worker
        self.gen_payload = initial_dict
    
    def get_job_payload(self,procgen):
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
        with self.lock:
            if self.n <= 0:
                return
            new_gen = self.new_procgen(worker)
            self.processing_gens.append(new_gen)
            self.n -= 1
            self.refresh()
            logger.audit(f"Procgen with ID {new_gen.id} popped from WP {self.id} by worker {worker.id} ('{worker.name}' / {worker.ipaddr})")
            return(self.get_pop_payload(new_gen))

    def fake_generation(self, worker):
        new_gen = self.new_procgen(worker)
        new_gen.fake = True
        self.fake_gens.append(new_gen)
        self.tricked_workers.append(worker)
        return(self.get_pop_payload(new_gen))
    
    def tricked_worker(self, worker):
        return(worker in self.tricked_workers)

    def get_pop_payload(self, procgen):
        prompt_payload = {
            "payload": self.get_job_payload(procgen),
            "id": procgen.id,
            "model": procgen.model,
        }
        return(prompt_payload)

    # Using this function so that I can extend it to have it grab the correct extended class
    def new_procgen(self, worker):
        return(ProcessingGeneration(self, self._processing_generations, worker))

    def is_completed(self):
        if self.faulted:
            return(True)
        if self.needs_gen():
            return(False)
        for procgen in self.processing_gens:
            if not procgen.is_completed() and not procgen.is_faulted():
                return(False)
        return(True)

    def count_processing_gens(self):
        ret_dict = {
            "finished": 0,
            "processing": 0,
            "restarted": 0,
        }
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["finished"] += 1
            elif procgen.is_faulted():
                ret_dict["restarted"] += 1
            else:
                ret_dict["processing"] += 1
        return(ret_dict)

    def get_queued_things(self):
        '''The things still queued to be generated for this waiting prompt'''
        return(round(self.things * self.n/thing_divisor,2))

    def get_status(self, lite = False):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        # This might still happen due to a race condition on parallel requests. Not sure how to avoid it.
        if ret_dict["waiting"] < 0:
            logger.error("Request was popped more times than requested!")
            ret_dict["waiting"] = 0
        ret_dict["done"] = self.is_completed()
        ret_dict["faulted"] = self.faulted
        # Lite mode does not include the generations, to spare me download size
        if not lite:
            ret_dict["generations"] = []
            for procgen in self.processing_gens:
                if procgen.is_completed():
                    ret_dict["generations"].append(procgen.get_details())
        queue_pos, queued_things, queued_n = self.get_own_queue_stats()
        # We increment the priority by 1, because it starts at -1
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
        highest_expected_time_left = 0
        for procgen in self.processing_gens:
            expected_time_left = procgen.get_expected_time_left()
            if expected_time_left > highest_expected_time_left:
                highest_expected_time_left = expected_time_left
        wait_time += highest_expected_time_left
        ret_dict["wait_time"] = round(wait_time)
        ret_dict["kudos"] = self.consumed_kudos
        ret_dict["is_possible"] = self.has_valid_workers()
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
        self.consumed_kudos = round(self.consumed_kudos + kudos,2)
        self.refresh()

    def check_for_stale(self):
        while True:
            try:
                # The below check if any jobs have been running too long and aborts them
                faulted_requests = 0
                for gen in self.processing_gens:
                    # We don't want to recheck if we've faulted already
                    if self.faulted:
                        break
                    if gen.is_stale(self.job_ttl):
                        # If the request took too long to complete, we cancel it and add it to the retry
                        gen.abort()
                        self.n += 1
                    if gen.is_faulted():
                        faulted_requests += 1
                    # If 3 or more jobs have failed, we assume there's something wrong with this request and mark it as faulted.
                    if faulted_requests >= 3:
                        self.faulted = True
                        self.log_faulted_job()
                if self._waiting_prompts.is_deleted(self):
                    break
                if self.is_stale():
                    self.delete()
                    break
                time.sleep(10)
                self.extra_priority += 50
            except Exception as e:
                logger.critical(f"Exception {e} detected. Handing to avoid crashing thread.")
                time.sleep(10)


    def log_faulted_job(self):
        '''Extendable function to log why a request was aborted'''
        logger.warning(f"Faulting waiting prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")

    def delete(self):
        for gen in self.processing_gens:
            if not self.faulted:
                gen.cancel()
            gen.delete()
        for gen in self.fake_gens:
            gen.delete()
        self._waiting_prompts.del_item(self)
        del self

    def abort_for_maintenance(self):
        '''sets all waiting requests to 0, so that all clients pick them up once the client gen is completed'''
        if self.is_completed():
            return
        self.n = 0

    def refresh(self):
        self.last_process_time = datetime.now()

    def is_stale(self):
        if (datetime.now() - self.last_process_time).seconds > self.stale_time:
            return(True)
        return(False)

    def get_priority(self):
        return(self.user.kudos + self.extra_priority)

    def set_job_ttl(self):
        '''Returns how many seconds each job request should stay waiting before considering it stale and cancelling it
        This function should be overriden by the invididual hordes depending on how the calculating ttl
        '''
        self.job_ttl = 150

    def has_valid_workers(self):
        worker_found = False
        for worker in self.db.workers.values():
            if len(self.workers) and worker.id not in self.workers:
                continue
            if worker.can_generate(self)[0]:
                worker_found = True
                break
        return(worker_found)

class ProcessingGeneration:
    generation = None
    seed = None
    fake = False
    faulted = False
 
    def __init__(self, owner, pgs, worker):
        self._processing_generations = pgs
        self.id = str(uuid4())
        self.owner = owner
        self.worker = worker
        # If there has been no explicit model requested by the user, we just choose the first available from the worker
        if len(self.worker.models):
            self.model = self.worker.models[0]
        else:
            self.model = ''
        # If we reached this point, it means there is at least 1 matching model between worker and client
        # so we pick the first one.
        for model in self.owner.models:
            if model in self.worker.models:
                self.model = model
        self.start_time = datetime.now()
        self._processing_generations.add_item(self)

    # We allow the seed to not be sent
    def set_generation(self, generation, **kwargs):
        if self.is_completed() or self.is_faulted():
            return(0)
        self.generation = generation
        # Support for two typical properties 
        self.seed = kwargs.get('seed', None)
        self.things_per_sec = self.owner.db.stats.record_fulfilment(things=self.owner.things, starting_time=self.start_time, model=self.model)
        self.kudos = self.get_gen_kudos()
        self.cancelled = False
        thread = threading.Thread(target=self.record, args=())
        thread.start()        
        return(self.kudos)

    def cancel(self):
        '''Cancelling requests in progress still rewards/burns the relevant amount of kudos'''
        if self.is_completed() or self.is_faulted():
            return
        self.faulted = True
        # We  don't want cancelled requests to raise suspicion
        self.things_per_sec = self.worker.get_performance_average()
        self.kudos = self.get_gen_kudos()
        self.cancelled = True
        thread = threading.Thread(target=self.record, args=())
        thread.start()   
        return(self.kudos)
    
    def record(self):
        cancel_txt = ""
        if self.cancelled:
            cancel_txt = " Cancelled"
        if self.fake and self.worker.user == self.owner.user:
            # We do not record usage for paused workers, unless the requestor was the same owner as the worker
            self.worker.record_contribution(raw_things = self.owner.things, kudos = self.kudos, things_per_sec = self.things_per_sec)
            logger.info(f"Fake{cancel_txt} Generation worth {self.kudos} kudos, delivered by worker: {self.worker.name}")
        else:
            self.worker.record_contribution(raw_things = self.owner.things, kudos = self.kudos, things_per_sec = self.things_per_sec)
            self.owner.record_usage(raw_things = self.owner.things, kudos = self.kudos)
            logger.info(f"New{cancel_txt} Generation worth {self.kudos} kudos, delivered by worker: {self.worker.name}")

    def abort(self):
        '''Called when this request needs to be stopped without rewarding kudos. Say because it timed out due to a worker crash'''
        if self.is_completed() or self.is_faulted():
            return        
        self.faulted = True
        self.worker.log_aborted_job()
        self.log_aborted_generation()

    def log_aborted_generation(self):
        logger.info(f"Aborted Stale Generation {self.id} from by worker: {self.worker.name} ({self.worker.id})")

    # Overridable function
    def get_gen_kudos(self):
        return(self.owner.db.convert_things_to_kudos(self.owner.things, seed = self.seed, model_name = self.model))

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def is_faulted(self):
        if self.faulted:
            return(True)
        return(False)

    def is_stale(self, ttl):
        if self.is_completed() or self.is_faulted():
            return(False)
        if (datetime.now() - self.start_time).seconds > ttl:
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
            "gen": self.generation,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
        }
        return(ret_dict)

class Worker:
    suspicion_threshold = 3
    # Every how many seconds does this worker get a kudos reward
    uptime_reward_threshold = 600
    default_maintenance_msg = "This worker has been put into maintenance mode by its owner"

    def __init__(self, db):
        self.last_reward_uptime = 0
        # Maintenance can be requested by the owner of the worker (to allow them to not pick up more requests)
        self.maintenance = False
        # Paused is set by the admins to prevent that worker from seeing any more requests
        # This can be used for stopping workers who misbhevave for example, without informing their owners
        self.paused = False
        # Extra comment about the worker, set by its owner
        self.info = None
        # The worker's team, set by its owner
        self.team = None
        self.suspicious = 0
        # Jobs which started but never completed by the worker. We only store this as a metric
        self.uncompleted_jobs = 0
        # Jobs which were started but never completed in the last hour. Used only to mark for suspicion and not otherwise reported.
        self.aborted_jobs = 0
        self.last_aborted_job = datetime.now()
        self.kudos_details = {
            "generated": 0,
            "uptime": 0,
        }
        self.suspicions = []
        self.db = db
        self.bridge_version = 1
        self.threads = 1
        self.maintenance_msg = self.default_maintenance_msg

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
        if int(reason) in self.suspicions and reason not in [Suspicions.UNREASONABLY_FAST,Suspicions.TOO_MANY_JOBS_ABORTED]:
            return
        self.suspicions.append(int(reason))
        self.suspicious += amount
        self.user.report_suspicion(amount, reason, formats)
        if reason:
            reason_log = suspicion_logs[reason].format(*formats)
            logger.warning(f"Worker '{self.id}' suspicion increased to {self.suspicious}. Reason: {reason_log}")
        if self.is_suspicious():
            self.paused = True

    def reset_suspicion(self):
        '''Clears the worker's suspicion and resets their reasons'''
        self.suspicions = []
        self.suspicious = 0

    def is_suspicious(self):
        # Trusted users are never suspicious
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
        self.name = bleach.clean(new_name)
        return("OK")

    def set_info(self,new_info):
        if self.info == new_info:
            return("OK")
        if is_profane(new_info):
            return("Profanity")
        if len(new_info) > 1000:
            return("Too Long")
        self.info = bleach.clean(new_info)
        return("OK")

    def set_team(self,new_team):
        self.team = new_team
        return("OK")

    # This should be overwriten by each specific horde
    def calculate_uptime_reward(self):
        return(100)

    def toggle_maintenance(self, is_maintenance_active, maintenance_msg = None):
        self.maintenance = is_maintenance_active
        self.maintenance_msg = self.default_maintenance_msg
        if self.maintenance and maintenance_msg is not None:
            self.maintenance_msg = bleach.clean(maintenance_msg)


    # This should be extended by each specific horde
    def check_in(self, **kwargs):
        self.models = [bleach.clean(model_name) for model_name in kwargs.get("models")]
        # We don't allow more workers to claim they can server more than 30 models atm (to prevent abuse)
        del self.models[50:]
        self.nsfw = kwargs.get("nsfw", True)
        self.blacklist = kwargs.get("blacklist", [])
        self.ipaddr = kwargs.get("ipaddr", None)
        self.bridge_version = kwargs.get("bridge_version", 1)
        self.threads = kwargs.get("threads", 1)
        if not kwargs.get("safe_ip", True):
            if not self.user.trusted:
                self.report_suspicion(reason = Suspicions.UNSAFE_IP)
        if not self.is_stale() and not self.paused and not self.maintenance:
            self.uptime += (datetime.now() - self.last_check_in).seconds
            # Every 10 minutes of uptime gets 100 kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                if self.team:
                    self.team.record_uptime(self.uptime_reward_threshold)
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
        '''Takes as an argument a WaitingPrompt class and checks if this worker is valid for generating it'''
        is_matching = True
        skipped_reason = None
        # Workers in maintenance are still allowed to generate for their owner
        if self.maintenance and waiting_prompt.user != self.user:
            is_matching = False
            return([is_matching,skipped_reason])
        if self.is_stale():
            # We don't consider stale workers in the request, so we don't need to report a reason
            is_matching = False
            return([is_matching,skipped_reason])
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
        if any(word.lower() in waiting_prompt.prompt.lower() for word in self.blacklist):
            is_matching = False
            skipped_reason = 'blacklist'
        if len(waiting_prompt.models) > 0 and not any(model in waiting_prompt.models for model in self.models):
            is_matching = False
            skipped_reason = 'models'
        # # I removed this for now as I think it might be blocking requests from generating. I will revisit later again
        # # If the worker is slower than average, and we're on the last quarter of the request, we try to utilize only fast workers
        # if self.get_performance_average() < self.db.stats.get_request_avg() and waiting_prompt.n <= waiting_prompt.jobs/4:
        #     is_matching = False
        #     skipped_reason = 'performance'
        return([is_matching,skipped_reason])

    # We split it to its own function to make it extendable
    def convert_contribution(self,raw_things):
        converted = round(raw_things/thing_divisor,2)
        self.contributions = round(self.contributions + converted,2)
        # We reurn the converted amount as well in case we need it
        return(converted)

    @logger.catch(reraise=True)
    def record_contribution(self, raw_things, kudos, things_per_sec):
        '''We record the servers newest contribution
        We do not need to know what type the contribution is, to avoid unnecessarily extending this method
        '''
        self.user.record_contributions(raw_things = raw_things, kudos = kudos)
        self.modify_kudos(kudos,'generated')
        converted_amount = self.convert_contribution(raw_things)
        self.fulfilments += 1
        if self.team:
            self.team.record_contribution(converted_amount, kudos)
        self.performances.append(things_per_sec)
        if things_per_sec / thing_divisor > things_per_sec_suspicion_threshold:
            self.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[round(things_per_sec / thing_divisor,2)])
        if len(self.performances) > 20:
            del self.performances[0]

    def modify_kudos(self, kudos, action = 'generated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + abs(kudos), 2) 

    def log_aborted_job(self):
        # We count the number of jobs aborted in an 1 hour period. So we only log the new timer each time an hour expires.
        if (datetime.now() - self.last_aborted_job).seconds > 3600:
            self.aborted_jobs = 0
            self.last_aborted_job = datetime.now()
        self.aborted_jobs += 1
        # These are accumulating too fast at 5. Increasing to 20
        dropped_job_threshold = 20
        if raid.active:
            dropped_job_threshold = 10
        if self.aborted_jobs > dropped_job_threshold:
            # if a worker drops too many jobs in an hour, we put them in maintenance
            # except during a raid, as we don't want them to know we detected them.
            if not raid.active:
                self.toggle_maintenance(
                    True, 
                    "Maintenance mode activated because worker is dropping too many jobs."
                    "Please investigate if your performance has been impacted and consider reducing your max_power or your max_threads"
                )
            self.report_suspicion(reason = Suspicions.TOO_MANY_JOBS_ABORTED)
            self.aborted_jobs = 0
        self.uncompleted_jobs += 1

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

    def delete(self):
        self.db.delete_worker(self)
        del self

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "requests_fulfilled": self.fulfilments,
            "uncompleted_jobs": self.uncompleted_jobs,
            "kudos_rewards": self.kudos,
            "kudos_details": self.kudos_details,
            "performance": self.get_performance(),
            "threads": self.threads,
            "uptime": self.uptime,
            "maintenance_mode": self.maintenance,
            "info": self.info,
            "nsfw": self.nsfw,
            "trusted": self.user.trusted,
            "models": self.models,
            "online": not self.is_stale(),
            "team": {"id": self.team.id,"name": self.team.name} if self.team else 'None',
        }
        if details_privilege >= 2:
            ret_dict['paused'] = self.paused
            ret_dict['suspicious'] = self.suspicious
        if details_privilege >= 1 or self.user.public_workers:
            ret_dict['owner'] = self.user.get_unique_alias()
            ret_dict['contact'] = self.user.contact
        return(ret_dict)

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def serialize(self):
        ret_dict = {
            "oauth_id": self.user.oauth_id,
            "name": self.name,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "uncompleted_jobs": self.uncompleted_jobs,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details.copy(),
            "performances": self.performances.copy(),
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "uptime": self.uptime,
            "paused": self.paused,
            "maintenance": self.maintenance,
            "maintenance_msg": self.maintenance_msg,
            "threads": self.threads,
            "info": self.info,
            "nsfw": self.nsfw,
            "blacklist": self.blacklist.copy(),
            "ipaddr": self.ipaddr,
            "suspicions": self.suspicions,
            "models": self.models,
            "team": self.team.id if self.team else None,
        }
        return(ret_dict)

    @logger.catch(reraise=True)
    def deserialize(self, saved_dict, convert_flag = None):
        self.user = self.db.find_user_by_oauth_id(saved_dict["oauth_id"])
        self.name = saved_dict["name"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.uncompleted_jobs = saved_dict.get("uncompleted_jobs",0)
        self.kudos = saved_dict.get("kudos",0)
        self.kudos_details = saved_dict.get("kudos_details",self.kudos_details)
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.uptime = saved_dict.get("uptime",0)
        self.maintenance = saved_dict.get("maintenance",False)
        self.maintenance_msg = saved_dict.get("maintenance_msg",self.default_maintenance_msg)
        self.threads = saved_dict.get("threads",1)
        self.paused = saved_dict.get("paused",False)
        self.info = saved_dict.get("info",None)
        team_id = saved_dict.get("team",None)
        if team_id:
            self.team = self.db.find_team_by_id(team_id)
        self.nsfw = saved_dict.get("nsfw",True)
        self.blacklist = saved_dict.get("blacklist",[])
        self.ipaddr = saved_dict.get("ipaddr", None)
        self.suspicions = saved_dict.get("suspicions", [])
        for suspicion in self.suspicions.copy():
            if convert_flag == "clean_dropped_jobs":
                self.suspicions.remove(suspicion)
                continue
            self.suspicious += 1
            logger.debug(f"Suspecting worker {self.name} for {self.suspicious} with reasons {self.suspicions}")
        old_model = saved_dict.get("model")
        self.models = saved_dict.get("models", [old_model])
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
        return(list(self._index.values()))

    def is_deleted(self,item):
        if item.id in self._index:
            return(False)
        return(True)

class PromptsIndex(Index):

    def count_waiting_requests(self, user, models = []):
        count = 0
        for wp in list(self._index.values()):
            if wp.user == user and not wp.is_completed():
                # If we pass a list of models, we want to count only the WP for these particular models.
                if len(models) > 0:
                    matching_model = False
                    for model in models:
                        if model in wp.models:
                            matching_model = True
                            break
                    if not matching_model:
                        continue
                count += wp.n
        return(count)

    def count_total_waiting_generations(self):
        count = 0
        for wp in list(self._index.values()):
            count += wp.n + wp.count_processing_gens()["processing"]
        return(count)

    def count_totals(self):
        queued_thing = f"queued_{thing_name}"
        ret_dict = {
            "queued_requests": 0,
            queued_thing: 0,
        }
        for wp in list(self._index.values()):
            current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
            ret_dict["queued_requests"] += current_wp_queue
            if current_wp_queue > 0:
                ret_dict[queued_thing] += wp.things * current_wp_queue / thing_divisor
        # We round the end result to avoid to many decimals
        ret_dict[queued_thing] = round(ret_dict[queued_thing],2)
        return(ret_dict)

    def count_things_per_model(self):
        things_per_model = {}
        org = self.organize_by_model()
        for model in org:
            for wp in org[model]:
                current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
                if current_wp_queue > 0:
                    things_per_model[model] = things_per_model.get(model,0) + wp.things
            things_per_model[model] = round(things_per_model.get(model,0),2)
        return(things_per_model)

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

    def organize_by_model(self):
        org = {}
        # We make a list here to prevent iterating when the list changes
        all_wps = list(self._index.values())
        for wp in all_wps:
            # Each wp we have will be placed on the list for each of it allowed models (in case it's selected multiple)
            # This will inflate the overall expected times, but it shouldn't be by much.
            # I don't see a way to do this calculation more accurately though
            for model in wp.models:
                if not model in org:
                    org[model] = []
                org[model].append(wp)
        return(org)    

class GenerationsIndex(Index):
    
    def organize_by_model():
        org = {}
        for procgen in self._index.values():
            if not procgen.model in org:
                org[model] = []
            org[model].append(procgen)
        return(org)

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
        self.contact = None
        self.db = db
        self.min_kudos = 0

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
        self.concurrency = 500

    def create(self, username, oauth_id, api_key, invite_id):
        self.username = username
        self.oauth_id = oauth_id
        self.api_key = api_key
        self.invite_id = invite_id
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.check_for_bad_actor()
        self.id = self.db.register_new_user(self)
        self.set_min_kudos()
        self.contributions = {
            thing_name: 0,
            "fulfillments": 0
        }
        self.usage = {
            thing_name: 0,
            "requests": 0
        }

    def set_min_kudos(self):
        if self.is_anon(): 
            self.min_kudos = -50
        elif self.is_pseudonymous():
            self.min_kudos = 14
        else:
            self.min_kudos = 25

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
        self.username = bleach.clean(new_username)
        return("OK")

    def set_contact(self,new_contact):
        if self.contact == new_contact:
            return("OK")
        if is_profane(new_contact):
            return("Profanity")
        self.contact = bleach.clean(new_contact)
        return("OK")

    def set_trusted(self,is_trusted):
        # Anonymous can never be trusted
        if self.is_anon():
            return
        self.trusted = is_trusted
        if self.trusted:
            for worker in self.get_workers():
                worker.paused = False

    def set_moderator(self,is_moderator):
        if self.is_anon():
            return
        self.moderator = is_moderator
        if self.moderator:
            logger.warning(f"{self.username} Set as moderator")
            self.set_trusted(True)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def record_usage(self, raw_things, kudos):
        self.last_active = datetime.now()
        self.usage["requests"] += 1
        self.modify_kudos(-kudos,"accumulated")
        self.usage[thing_name] = round(self.usage[thing_name] + (raw_things * self.usage_multiplier / thing_divisor),2)

    def record_contributions(self, raw_things, kudos):
        self.last_active = datetime.now()
        self.contributions["fulfillments"] += 1
        # While a worker is untrusted, half of all generated kudos go for evaluation
        if not self.trusted and not self.is_anon():
            kudos_eval = round(kudos / 2)
            kudos -= kudos_eval
            self.evaluating_kudos += kudos_eval
            self.modify_kudos(kudos,"accumulated")
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")
        self.contributions[thing_name] = round(self.contributions[thing_name] + raw_things/thing_divisor,2)

    def record_uptime(self, kudos):
        self.last_active = datetime.now()
        # While a worker is untrusted, all uptime kudos go for evaluation
        if not self.trusted and not self.is_anon():
            self.evaluating_kudos += kudos
            self.check_for_trust()
        else:
            self.modify_kudos(kudos,"accumulated")

    def check_for_trust(self):
        '''After a user passes the evaluation threshold (50000 kudos)
        All the evaluating Kudos added to their total and they automatically become trusted
        Suspicious users do not automatically pass evaluation
        '''
        if self.evaluating_kudos >= int(os.getenv("KUDOS_TRUST_THRESHOLD")) and not self.is_suspicious() and not self.is_anon():
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
        if self.kudos < self.min_kudos:
            self.kudos = self.min_kudos

    def is_anon(self):
        if self.oauth_id == 'anon':
            return(True)
        return(False)

    def is_pseudonymous(self):
        try:
            uuid.UUID(str(self.oauth_id))
            return(True)
        except ValueError:
            return(False)

    def get_concurrency(self, models_requested = [], models_dict = {}):
        if not self.is_anon() or len(models_requested) == 0:
            return(self.concurrency)
        found_workers = []
        for model_name in models_requested:
            model_dict = models_dict.get(model_name)
            if model_dict:
                for worker in model_dict["workers"]:
                    if worker not in found_workers:
                        found_workers.append(worker)
        # We allow 10 concurrency per worker serving the models requested
        allowed_concurrency = len(found_workers) * 20
        # logger.debug([allowed_concurrency,models_dict.get(model_name,{"count":0})["count"]])
        return(allowed_concurrency)
            

    @logger.catch(reraise=True)
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
            "pseudonymous": self.is_pseudonymous(),
            "worker_count": self.count_workers(),
            # unnecessary information, since the workers themselves wil be visible
            # "public_workers": self.public_workers,
        }
        if self.public_workers or details_privilege >= 1:
            workers_array = []
            for worker in self.get_workers():
                workers_array.append(worker.id)
            ret_dict["worker_ids"] = workers_array
            ret_dict['contact'] = self.contact
        if details_privilege >= 2:
            mk_dict = {
                "amount": self.calculate_monthly_kudos(),
                "last_received": self.monthly_kudos["last_received"]
            }
            ret_dict["evaluating_kudos"] = self.evaluating_kudos
            ret_dict["monthly_kudos"] = mk_dict
            ret_dict["suspicious"] = self.suspicious
        return(ret_dict)

    def report_suspicion(self, amount = 1, reason = Suspicions.USERNAME_PROFANITY, formats = []):
        # Anon is never considered suspicious
        if self.is_anon():
            return
        if int(reason) in self.suspicions and reason not in [Suspicions.UNREASONABLY_FAST,Suspicions.TOO_MANY_JOBS_ABORTED]:
            return
        self.suspicions.append(int(reason))
        self.suspicious += amount
        if reason:
            reason_log = suspicion_logs[reason].format(*formats)
            logger.warning(f"User '{self.id}' suspicion increased to {self.suspicious}. Reason: {reason}")

    def reset_suspicion(self):
        '''Clears the user's suspicion and resets their reasons'''
        if self.is_anon():
            return
        self.suspicions = []
        self.suspicious = 0
        for worker in self.db.find_workers_by_user(self):
            worker.reset_suspicion()

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

    def is_stale(self):
        # Stale users have to be inactive for a month
        days_threshold = 30
        days_inactive = (datetime.now() - self.last_active).days
        if days_inactive < days_threshold:
            return(False)
        # Stale user have to have little accumulated kudos. 
        # The longer a user account is inactive. the more kudos they need to have stored to not be deleted
        # logger.debug([days_inactive,self.kudos, 10 * (days_inactive - days_threshold)])
        if self.kudos > 10 * (days_inactive - days_threshold):
            return(False)
        # Anonymous cannot be stale
        if self.is_anon():
            return(False)
        if self.moderator:
            return(False)
        if self.trusted:
            return(False)
        return(True)

    @logger.catch(reraise=True)
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
            "contact": self.contact,
        }
        return(ret_dict)

    @logger.catch(reraise=True)
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
        self.contact = saved_dict.get("contact",None)
        for suspicion in self.suspicions.copy():
            if convert_flag == "clean_dropped_jobs":
                if suspicion == 9:
                    self.suspicions.remove(suspicion)
                    continue
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
            self.concurrency = 500
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
        self.set_min_kudos()
        self.ensure_kudos_positive()
        duplicate_user = self.db.find_user_by_id(self.id)
        if duplicate_user and duplicate_user != self:
            if duplicate_user.get_unique_alias() != self.get_unique_alias():
                logger.error(f"mismatching duplicate IDs found! {self.get_unique_alias()} != {duplicate_user.get_unique_alias()}. Please cleanup manually!")
            else:
                logger.warning(f"found duplicate ID: {[self,duplicate_user,self.get_unique_alias(),self.id,duplicate_user.id,duplicate_user.get_unique_alias()]}")
                duplicate_user.kudos += self.kudos
                if duplicate_user.last_active < self.last_active:
                    logger.warning(f"Merging {self.oauth_id} into {duplicate_user.oauth_id}")
                    duplicate_user.oauth_id = self.oauth_id
                return(True)


class Team:
    def __init__(self, db):
        self.contributions = 0
        self.fulfilments = 0
        self.kudos = 0
        self.uptime = 0
        self.db = db
        self.info = ''
        self.name = ''

    def create(self, user):
        self.id = str(uuid4())
        self.set_owner(user)
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.db.register_new_team(self)

    def get_performance(self):
        all_performances = []
        for worker in self.db.find_workers_by_team(self):
            if worker.is_stale():
                continue
            all_performances.append(worker.get_performance_average())
        if len(all_performances):
            perf_avg = round(sum(all_performances) / len(all_performances) / thing_divisor,1)
            perf_total = round(sum(all_performances) / thing_divisor,1)
        else:
            perf_avg = 0
            perf_total = 0
        return(perf_avg,perf_total)

    def get_all_models(self):
        all_models = {}
        for worker in self.db.find_workers_by_team(self):
            for model_name in worker.models:
                all_models[model_name] = all_models.get(model_name,0) + 1
        model_list = []
        for model in all_models:
            minfo = {
                "name": model,
                "count": all_models[model]
            }
            model_list.append(minfo)
        return(model_list)

    def set_name(self,new_name):
        if self.name == new_name:
            return("OK")        
        if is_profane(new_name):
            return("Profanity")
        self.name = bleach.clean(new_name)
        existing_team = self.db.find_team_by_name(self.name)
        if existing_team and existing_team != self:
            return("Already Exists")
        return("OK")

    def set_info(self, new_info):
        if self.info == new_info:
            return("OK")
        if is_profane(new_info):
            return("Profanity")
        self.info = bleach.clean(new_info)
        return("OK")

    def set_owner(self, new_owner):
        self.user = new_owner

    def delete(self):
        for worker in self.db.find_workers_by_team(self):
            worker.set_team(None)
        self.db.delete_team(self)
        del self

    def record_uptime(self, seconds):
        self.uptime += seconds
        self.last_active = datetime.now()
    
    def record_contribution(self, contributions, kudos):
        self.contributions = round(self.contributions + contributions, 2)
        self.fulfilments += 1
        self.kudos = round(self.kudos + kudos, 2)
        self.last_active = datetime.now()

   # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege = 0):
        '''We display these in the workers list json'''
        worker_list = [{"id": worker.id, "name":worker.name, "online": not worker.is_stale()} for worker in self.db.find_workers_by_team(self)]
        perf_avg, perf_total = self.get_performance()
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "creator": self.user.get_unique_alias(),
            "contributions": self.contributions,
            "requests_fulfilled": self.fulfilments,
            "kudos": self.kudos,
            "performance": perf_avg,
            "speed": perf_total,
            "uptime": self.uptime,
            "info": self.info,
            "worker_count": len(worker_list),
            "workers": worker_list,
            "models": self.get_all_models(),
        }
        return(ret_dict)

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def serialize(self):
        ret_dict = {
            "oauth_id": self.user.oauth_id,
            "name": self.name,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "kudos": self.kudos,
            "last_active": self.last_active.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "uptime": self.uptime,
            "info": self.info,
        }
        return(ret_dict)

    @logger.catch(reraise=True)
    def deserialize(self, saved_dict, convert_flag = None):
        self.user = self.db.find_user_by_oauth_id(saved_dict["oauth_id"])
        self.name = saved_dict["name"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.kudos = saved_dict.get("kudos",0)
        self.last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.uptime = saved_dict.get("uptime",0)
        self.info = saved_dict.get("info",None)


class Stats:
    worker_performances = []
    model_performances = {}
    fulfillments = []

    def __init__(self, db, convert_flag = None, interval = 60):
        self.db = db
        self.interval = interval
        self.last_pruning = datetime.now()

    def record_fulfilment(self, things, starting_time, model):
        seconds_taken = (datetime.now() - starting_time).seconds
        if seconds_taken == 0:
            things_per_sec = 1
        else:
            things_per_sec = round(things / seconds_taken,1)
        if len(self.worker_performances) >= 10:
            del self.worker_performances[0]
        self.worker_performances.append(things_per_sec)
        if model not in self.model_performances:
            self.model_performances[model] = []
        self.model_performances[model].append(things_per_sec)
        if len(self.model_performances[model]) >= 10:
            del self.model_performances[model][0]
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

    def get_model_avg(self, model):
        if len(self.model_performances.get(model,[])) == 0:
            return(0)
        avg = sum(self.model_performances[model]) / len(self.model_performances[model])
        return(round(avg,1))

    @logger.catch(reraise=True)
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
            "model_performances": self.model_performances,
            "fulfillments": serialized_fulfillments,
        }
        return(ret_dict)

    @logger.catch(reraise=True)
    def deserialize(self, saved_dict, convert_flag = None):
        # Convert old key
        if "server_performances" in saved_dict:
            self.worker_performances = saved_dict["server_performances"]
        else:
            self.worker_performances = saved_dict["worker_performances"]
        self.model_performances = saved_dict.get("model_performances", {})
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
        self.TEAMS_FILE = "db/teams.json"
        self.teams = {}
        # I'm setting this quickly here so that we do not crash when trying to detect duplicate IDs, during user deserialization
        self.anon = None
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
                    error = new_user.deserialize(user_dict,convert_flag)
                    if error:
                        continue
                    if new_user.is_stale():
                        # logger.warning(f"(Dry-Run) Deleting stale user {new_user.get_unique_alias()}")
                        pass
                    self.users[new_user.oauth_id] = new_user
                    if new_user.id > self.last_user_id:
                        self.last_user_id = new_user.id
        self.anon = self.find_user_by_oauth_id('anon')
        if not self.anon:
            self.anon = User(self)
            self.anon.create_anon()
            self.users[self.anon.oauth_id] = self.anon
        if os.path.isfile(self.TEAMS_FILE):
            with open(self.TEAMS_FILE) as db:
                serialized_teams = json.load(db)
                for team_dict in serialized_teams:
                    if not team_dict:
                        logger.error("Found null team on db load. Bypassing")
                        continue
                    new_team = self.new_team()
                    new_team.deserialize(team_dict,convert_flag)
                    self.teams[new_team.id] = new_team
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
    def new_team(self):
        return(Team(self))

    def write_files(self):
        time.sleep(4)
        logger.init_ok("Database Store Thread", status="Started")
        self.save_progress = self.interval
        while True:
            if self.interval == -1:
                logger.warning("Stopping DB save thread")
                return
            if self.save_progress >= self.interval:
                self.write_files_to_disk()
                self.save_progress = 0
            time.sleep(1)
            self.save_progress += 1

    def initiate_save(self, seconds = 3):
        logger.success(f"Initiating save in {seconds} seconds")
        if seconds > self.interval:
            second = self.interval
        self.save_progress = self.interval - seconds

    def shutdown(self, seconds):
        self.interval = -1
        if seconds > 0:
            logger.critical(f"Initiating shutdown in {seconds} seconds")
            time.sleep(seconds)
        self.write_files_to_disk()
        logger.critical(f"DB written to disk. You can now SIGTERM.")


    @logger.catch(reraise=True)
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
        teams_serialized_list = []
        for team in self.teams.copy().values():
            teams_serialized_list.append(team.serialize())
        with open(self.TEAMS_FILE, 'w') as db:
            json.dump(teams_serialized_list,db)

    def assign_monthly_kudos(self):
        time.sleep(2)
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
                count += worker.threads
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
        self.initiate_save()

    def register_new_worker(self, worker):
        self.workers[worker.name] = worker
        logger.info(f'New worker checked-in: {worker.name} by {worker.user.get_unique_alias()}')
        self.initiate_save()

    def delete_worker(self,worker):
        del self.workers[worker.name]
        self.initiate_save()

    def find_user_by_oauth_id(self,oauth_id):
        if oauth_id == 'anon' and not self.ALLOW_ANONYMOUS:
            return(None)
        return(self.users.get(oauth_id))

    def find_user_by_username(self, username):
        for user in list(self.users.values()):
            ulist = username.split('#')
            # This approach handles someone cheekily putting # in their username
            if user.username == "#".join(ulist[:-1]) and user.id == int(ulist[-1]):
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_user_by_id(self, user_id):
        for user in list(self.users.values()):
            # The arguments passed to the URL are always strings
            if str(user.id) == str(user_id):
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_user_by_api_key(self,api_key):
        for user in list(self.users.values()):
            if user.api_key == api_key:
                if user == self.anon and not self.ALLOW_ANONYMOUS:
                    return(None)
                return(user)
        return(None)

    def find_worker_by_name(self,worker_name):
        return(self.workers.get(worker_name))

    def find_worker_by_id(self,worker_id):
        for worker in list(self.workers.values()):
            if worker.id == worker_id:
                return(worker)
        return(None)

    def find_workers_by_user(self, user):
        found_workers = []
        for worker in list(self.workers.values()):
            if worker.user == user:
                found_workers.append(worker)
        return(found_workers)
    
    def find_workers_by_team(self, team):
        found_workers = []
        for worker in list(self.workers.values()):
            if worker.team == team:
                found_workers.append(worker)
        return(found_workers)
    
    def update_worker_name(self, worker, new_name):
        if new_name in self.workers:
            # If the name already exists, we return error code 1
            return(1)
        self.workers[new_name] = worker
        del self.workers[worker.name]
        logger.info(f'Worker renamed from {worker.name} to {new_name}')

    def register_new_team(self, team):
        self.teams[team.id] = team
        logger.info(f'New team created: {team.name} by {team.user.get_unique_alias()}')
        self.initiate_save()

    def find_team_by_id(self,team_id):
        return(self.teams.get(team_id))

    def find_team_by_name(self,team_name):
        for team in list(self.teams.values()):
            if team.name.lower() == team_name.lower():
                return(team)
        return(None)

    def delete_team(self, team):
        del self.teams[team.id]
        self.initiate_save()

    def get_available_models(self, waiting_prompts, lite_dict=False):
        models_dict = {}
        for worker in list(self.workers.values()):
            if worker.is_stale():
                continue
            model_name = None
            if not worker.models: continue
            for model_name in worker.models:
                if not model_name: continue
                mode_dict_template = {
                    "name": model_name,
                    "count": 0,
                    "workers": [],
                    "performance": self.stats.get_model_avg(model_name),
                    "queued": 0,
                    "eta": 0,
                }
                models_dict[model_name] = models_dict.get(model_name, mode_dict_template)
                models_dict[model_name]["count"] += worker.threads
                models_dict[model_name]["workers"].append(worker)
        if lite_dict:
            return(models_dict)
        things_per_model = waiting_prompts.count_things_per_model()
        # If we request a lite_dict, we only want worker count per model and a dict format
        for model_name in things_per_model:
            # This shouldn't happen, but I'm checking anyway
            if model_name not in models_dict:
                # logger.debug(f"Tried to match non-existent wp model {model_name} to worker models. Skipping.")
                continue
            models_dict[model_name]['queued'] = things_per_model[model_name]
            total_performance_on_model = models_dict[model_name]['count'] * models_dict[model_name]['performance']
            # We don't want a division by zero when there's no workers for this model.
            if total_performance_on_model > 0:
                models_dict[model_name]['eta'] = int(things_per_model[model_name] / total_performance_on_model)
            else:
                models_dict[model_name]['eta'] = -1
        return(list(models_dict.values()))

    def transfer_kudos(self, source_user, dest_user, amount):
        if source_user.is_suspicious():
            return([0,'Something went wrong when sending kudos. Please contact the mods.'])
        if dest_user.is_suspicious():
            return([0,'Something went wrong when receiving kudos. Please contact the mods.'])
        if amount < 0:
            return([0,'Nice try...'])
        if amount > source_user.kudos - source_user.min_kudos:
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

