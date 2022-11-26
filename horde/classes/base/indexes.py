
import json
from uuid import uuid4
from datetime import datetime
import threading, time, dateutil.relativedelta, bleach
from horde import logger, args, raid
from horde.vars import thing_name,raw_thing_name,thing_divisor,things_per_sec_suspicion_threshold
from horde.suspicions import Suspicions, SUSPICION_LOGS
import uuid, re, random
from horde.utils import is_profane
from horde.flask import db
from horde.classes.base.stats import record_fulfilment, get_request_avg
from horde.classes.base.database import count_active_workers, convert_things_to_kudos, MonthlyKudos


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
        self.last_process_time = datetime.utcnow()
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
        active_workers = count_active_workers()
        # If there's less requests than the number of active workers
        # Then we need to adjust the parallelization accordingly
        if queued_n < active_workers:
            active_workers = queued_n
        avg_things_per_sec = (get_request_avg() / thing_divisor) * active_workers
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
        self.last_process_time = datetime.utcnow()

    def is_stale(self):
        if (datetime.utcnow() - self.last_process_time).seconds > self.stale_time:
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
        self.start_time = datetime.utcnow()
        self._processing_generations.add_item(self)

    # We allow the seed to not be sent
    def set_generation(self, generation, **kwargs):
        if self.is_completed() or self.is_faulted():
            return(0)
        self.generation = generation
        # Support for two typical properties 
        self.seed = kwargs.get('seed', None)
        self.things_per_sec = record_fulfilment(things=self.owner.things, starting_time=self.start_time, model=self.model)
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
        return(convert_things_to_kudos(self.owner.things, seed = self.seed, model_name = self.model))

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
        if (datetime.utcnow() - self.start_time).seconds > ttl:
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
        seconds_elapsed = (datetime.utcnow() - self.start_time).seconds
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
