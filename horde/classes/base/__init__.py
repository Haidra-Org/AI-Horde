import json, os, sys
from uuid import uuid4
from datetime import datetime
import threading, time, dateutil.relativedelta, bleach
from horde import logger, args, raid
from horde.vars import thing_name,raw_thing_name,thing_divisor,things_per_sec_suspicion_threshold
from horde.suspicions import Suspicions, SUSPICION_LOGS
import uuid, re, random
from horde.utils import is_profane
from horde.classes.base.news import News
from horde.flask import db
from horde.classes.base.user import User

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
        # Other miscellaneous statistics
        self.STATS_FILE = "db/stats.json"
        self.stats = self.new_stats()
        self.TEAMS_FILE = "db/teams.json"
        self.teams = {}
        # I'm setting this quickly here so that we do not crash when trying to detect duplicate IDs, during user deserialization
        self.anon = db.session.query(User).filter_by(oauth_id="anon").first()
        # Increments any time a new user is added
        # Is appended to usernames, to ensure usernames never conflict
        self.last_user_id = 0
    
    def load(self):
        logger.init(f"Database Load", status="Starting")
        if convert_flag:
            logger.init_warn(f"Convert Flag '{convert_flag}' received.", status="Converting")
        self.anon = self.find_user_by_oauth_id('anon')
        logger.debug(self.anon)
        if not self.anon:
            self.anon = User(
                id=0,
                username="Anonymous",
                oauth_id="oauth_id",
                api_key="0000000000",
                public_workers=True,
                concurrency=500
            )
            self.anon.create()
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
        # user_serialized_list = []
        # for user in self.users.copy().values():
        #     user_serialized_list.append(user.serialize())
        # with open(self.USERS_FILE, 'w') as db:
        #     json.dump(user_serialized_list,db)
        teams_serialized_list = []
        for team in self.teams.copy().values():
            teams_serialized_list.append(team.serialize())
        with open(self.TEAMS_FILE, 'w') as db:
            json.dump(teams_serialized_list,db)

    def assign_monthly_kudos(self):
        time.sleep(2)
        logger.init_ok("Monthly Kudos Awards Thread", status="Started")
        while True:
            #TODO Make the select statement bring the users with monthly kudos only
            for user in db.session.query(User).all():
                user.receive_monthly_kudos()
            # Check once a day
            time.sleep(86400)

    def get_top_contributor(self):
        top_contribution = 0
        top_contributor = None
        #TODO Make the select statement bring automatically bring to 10 contributors sorted
        for user in db.session.query(User).all():
            if user.contributed_thing > top_contribution and user != self.anon:
                top_contributor = user
                top_contribution = user.contributed_thing
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
        user = db.session.query(User).filter_by(oauth_id=oauth_id).first()
        return(user)

    def find_user_by_username(self, username):
        ulist = username.split('#')
        # This approach handles someone cheekily putting # in their username
        user = db.session.query(User).filter_by(user_id=int(ulist[-1])).first()
        if user == self.anon and not self.ALLOW_ANONYMOUS:
            return(None)
        return(user)

    def find_user_by_id(self, user_id):
        user = db.session.query(User).filter_by(user_id=user_id).first()
        if user == self.anon and not self.ALLOW_ANONYMOUS:
            return(None)
        return(user)

    def find_user_by_api_key(self,api_key):
        user = db.session.query(User).filter_by(api_key=api_key).first()
        if user == self.anon and not self.ALLOW_ANONYMOUS:
            return(None)
        return(user)

    def find_worker_by_name(self,worker_name):
        return(self.workers.get(worker_name))

    def find_worker_by_id(self,worker_id):
        for worker in list(self.workers.values()):
            if worker.id == worker_id:
                return(worker)
        return(None)

    
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

