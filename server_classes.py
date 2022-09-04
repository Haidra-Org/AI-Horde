import json, os
from uuid import uuid4
from datetime import datetime
import threading, time
import logging


class WaitingPrompt:
    # Every 10 secs we store usage data to disk
    def __init__(self, db, wps, pgs, prompt, username, models, params, **kwargs):
        self._db = db
        self._waiting_prompts = wps
        self._processing_generations = pgs
        self.prompt = prompt
        self.username = username
        self.models = models
        self.params = params
        self.n = params.get('n', 1)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logging.warning(f"User {self.username} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.tokens = len(prompt.split())
        self.max_length = params.get("max_length", 80)
        self.max_content_length = params.get("max_content_length", 1024)
        self.total_usage = 0
        self.id = str(uuid4())
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = params
        self.gen_payload["prompt"] = prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["n"] = 1
        # The generations that have been created already
        self.processing_gens = []
        self.last_process_time = datetime.now()
        self.servers = kwargs.get("servers", [])
        self.softprompts = kwargs.get("softprompts", [''])
        # Prompt requests are removed after 10 mins of inactivity, to prevent memory usage
        self.stale_time = 10


    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid server for it
        # Before we add it to the queue
        self._waiting_prompts.add_item(self)
        logging.info(f"New prompt request by user: {self.username}")
        thread = threading.Thread(target=self.check_for_stale, args=())
        thread.daemon = True
        thread.start()

    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, server, matching_softprompt):
        if self.n <= 0:
            return
        new_gen = ProcessingGeneration(self, self._processing_generations, server)
        self.processing_gens.append(new_gen)
        self.n -= 1
        self.refresh()
        prompt_payload = {
            "payload": self.gen_payload,
            "softprompt": matching_softprompt,
            "id": new_gen.id,
        }
        return(prompt_payload)

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

    def get_status(self):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        ret_dict["done"] = self.is_completed()
        ret_dict["generations"] = []
        for procgen in self.processing_gens:
            if procgen.is_completed():
                ret_dict["generations"].append(procgen.generation)
        return(ret_dict)

    def record_usage(self):
        self.total_usage += self.tokens
        self._db.add_usage(self.username, self.tokens)
        self.refresh()

    def check_for_stale(self):
        while True:
            if self.is_stale():
                self.delete()
                break
            time.sleep(10)

    def delete(self):
        for gen in self.processing_gens:
            gen.delete()
        self._waiting_prompts.del_item(self)
        del self

    def refresh(self):
        self.last_process_time = datetime.now()

    def is_stale(self):
        if (datetime.now() - self.last_process_time).seconds > self.stale_time:
            return(True)
        return(False)


class ProcessingGeneration:
    def __init__(self, owner, pgs, server):
        self._processing_generations = pgs
        self.id = str(uuid4())
        self.owner = owner
        self.server = server
        self.generation = None
        self.start_time = datetime.now()
        self._processing_generations.add_item(self)

    def set_generation(self, generation):
        if self.is_completed():
            return(0)
        self.generation = generation
        tokens = len(generation.split())
        self.server.record_contribution(tokens, (datetime.now() - self.start_time).seconds)
        self.owner.record_usage()
        logging.info(f"New Generation delivered by server: {self.server.name}")
        return(tokens)

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def delete(self):
        self._processing_generations.del_item(self)
        del self


class KAIServer:
    def __init__(self, db, username = None, name = None, password = None, softprompts = []):
        self._db = db
        self.username = username
        self.password = password
        self.name = name
        self.softprompts = softprompts
        self.contributions = 0
        self.fulfilments = 0
        self.performances = []
        self.uptime = 0
        self.id = str(uuid4())
        if name:
            self._db.servers[self.name] = self
            logging.info(f'New server checked-in: {name} by {username}')

    def check_in(self, model, max_length, max_content_length, softprompts):
        if not self.is_stale():
            self.uptime += (datetime.now() - self.last_check_in).seconds
        self.last_check_in = datetime.now()
        self.model = model
        self.max_content_length = max_content_length
        self.max_length = max_length
        self.softprompts = softprompts

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
        # takes as an argument a WaitingPrompt class and checks if this server is valid for generating it
        is_matching = True
        skipped_reason = None
        if len(waiting_prompt.servers) >= 1 and self.id not in waiting_prompt.servers:
            is_matching = False
            skipped_reason = 'server_id'
        if len(waiting_prompt.models) >= 1 and self.model not in waiting_prompt.models:
            is_matching = False
            skipped_reason = 'models'
        if self.max_content_length < waiting_prompt.max_content_length:
            is_matching = False
            skipped_reason = 'max_content_length'
        if self.max_length < waiting_prompt.max_length:
            is_matching = False
            skipped_reason = 'max_length'
        matching_softprompt = False
        for sp in waiting_prompt.softprompts:
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if sp == '':
                matching_softprompt = True
                break
            for sp_name in self.softprompts:
                if sp in sp_name: 
                    matching_softprompt = True
                    break
        if not matching_softprompt:
            is_matching = False
            skipped_reason = 'matching_softprompt'
        return([is_matching,skipped_reason])

    def record_contribution(self, tokens, seconds_taken):
        perf = round(tokens / seconds_taken,2)
        self._db.add_contribution(self.username, tokens)
        self._db.record_fulfilment(perf)
        self.contributions += tokens
        self.fulfilments += 1
        self.performances.append(perf)
        if len(self.performances) > 20:
            del self.performances[0]

    def get_performance(self):
        if len(self.performances):
            ret_str = f'{round(sum(self.performances) / len(self.performances),2)} tokens per second'
        else:
            ret_str = f'No requests fulfiled yet'
        return(ret_str)

    def is_stale(self):
        try:
            if (datetime.now() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new server, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    def serialize(self):
        ret_dict = {
            "username": self.username,
            "password": self.password,
            "name": self.name,
            "model": self.model,
            "max_length": self.max_length,
            "max_content_length": self.max_content_length,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "performances": self.performances,
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "softprompts": self.softprompts,
            "uptime": self.uptime,
        }
        return(ret_dict)

    def deserialize(self, saved_dict):
        self.username = saved_dict["username"]
        self.password = saved_dict["password"]
        self.name = saved_dict["name"]
        self.model = saved_dict["model"]
        self.max_length = saved_dict["max_length"]
        self.max_content_length = saved_dict["max_content_length"]
        self.contributions = saved_dict["contributions"]
        self.fulfilments = saved_dict["fulfilments"]
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.softprompts = saved_dict.get("softprompts",[])
        self.uptime = saved_dict.get("uptime",0)
        self._db.servers[self.name] = self


class Database:
    def __init__(self, interval = 3):
        self.interval = interval
        # This is used for synchronous generations
        self.SERVERS_FILE = "db/servers.json"
        self.servers = {}
        # How many tokens each user has requested
        self.USAGE_FILE = "db/usage.json"
        self.usage = {}
        # How many tokens each user's server has generated
        self.CONTRIBUTIONS_FILE = "db/contributions.json"
        self.contributions = {}
        # Other miscellaneous statistics
        self.STATS_FILE = "db/stats.json"
        self.stats = {
            "fulfilment_times": [],
        }
        if os.path.isfile(self.SERVERS_FILE):
            with open(self.SERVERS_FILE) as db:
                serialized_servers = json.load(db)
                for server_dict in serialized_servers:
                    new_server = KAIServer(self)
                    new_server.deserialize(server_dict)
                    self.servers[new_server.name] = new_server
        if os.path.isfile(self.USAGE_FILE):
            with open(self.USAGE_FILE) as db:
                self.usage = json.load(db)
        if os.path.isfile(self.CONTRIBUTIONS_FILE):
            with open(self.CONTRIBUTIONS_FILE) as db:
                self.contributions = json.load(db)
        if os.path.isfile(self.STATS_FILE):
            with open(self.STATS_FILE) as db:
                self.stats = json.load(db)

        thread = threading.Thread(target=self.store_usage, args=())
        thread.daemon = True
        thread.start()

    def store_usage(self):
        while True:
            self.write_files_to_disk()
            time.sleep(self.interval)

    def write_files_to_disk(self):
        if not os.path.exists('db'):
            os.mkdir('db')
        serialized_list = []
        for s in self.servers:
            serialized_list.append(self.servers[s].serialize())
        with open(self.SERVERS_FILE, 'w') as db:
            json.dump(serialized_list,db)
        with open(self.USAGE_FILE, 'w') as db:
            json.dump(self.usage,db)
        with open(self.CONTRIBUTIONS_FILE, 'w') as db:
            json.dump(self.contributions,db)
        with open(self.STATS_FILE, 'w') as db:
            json.dump(self.stats,db)

    def _ensure_user_exists(self, username):
        if username not in self.usage:
            logging.info(f'New user requested generation: {username}')
            self.usage[username] =  {
                "tokens":0, 
                "requests": 0
            }
        # Convert of style entry. Will remove eventually
        elif type(self.usage[username]) is not dict:
            old_tokens = self.usage[username]
            self.usage[username] = {
                "tokens":old_tokens, 
                "requests": 0
            }

    def _ensure_contributor_exists(self, username):
        if username not in self.contributions:
            self.contributions[username] =  {
                "tokens":0, 
                "requests": 0
            }
        # Convert of style entry. Will remove eventually
        elif type(self.contributions[username]) is not dict:
            old_tokens = self.contributions[username]
            self.contributions[username] = {
                "tokens":old_tokens, 
                "requests": 0
            }

    def add_contribution(self,username, tokens):
        self._ensure_contributor_exists(username)
        self.contributions[username]['tokens'] += tokens
        self.contributions[username]['requests'] += 1

    def add_usage(self,username, tokens):
        self._ensure_user_exists(username)
        self.usage[username]['tokens'] += tokens
        self.usage[username]['requests'] += 1

    def get_top_contributor(self):
        top_contribution = 0
        top_contributor = None
        for user in self.contributions:
            if self.contributions[user]['tokens'] > top_contribution:
                top_contributor = self.get_contributor_entry(user)
                top_contribution = self.contributions[user]['tokens']
        return(top_contributor)

    def get_top_server(self):
        top_server = None
        top_server_contribution = 0
        for server in self.servers:
            if self.servers[server].contributions > top_server_contribution:
                top_server = self.servers[server]
                top_server_contribution = self.servers[server].contributions
        return(top_server)

    def get_contributor_entry(self, username):
        self._ensure_contributor_exists(username)
        ret_dict = {
            "username": username,
            "tokens": self.contributions[username]["tokens"],
            "requests": self.contributions[username]["requests"],
        }
        return(ret_dict)
    
    def get_available_models(self):
        models_ret = {}
        for server in self.servers.values():
            if server.is_stale():
                continue
            models_ret[server.model] = models_ret.get(server.model,0) + 1
        return(models_ret)

    def get_total_usage(self):
        totals = {
            "tokens": 0,
            "fulfilments": 0,
        }
        for server in self.servers.values():
            totals["tokens"] += server.contributions
            totals["fulfilments"] += server.fulfilments
        return(totals)

    def record_fulfilment(self, token_per_sec):
        if len(self.stats["fulfilment_times"]) >= 10:
            del self.stats["fulfilment_times"][0]
        self.stats["fulfilment_times"].append(token_per_sec)
    
    def get_request_avg(self):
        if len(self.stats["fulfilment_times"]) == 0:
            return(0)
        avg = sum(self.stats["fulfilment_times"]) / len(self.stats["fulfilment_times"])
        return(round(avg,2))


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


class PromptsIndex(Index):

    def count_waiting_requests(self, username):
        count = 0
        for wp in self._index.values():
            if wp.username == username and not wp.is_completed():
                count += 1
        return(count)
    
    def count_total_waiting_generations(self):
        count = 0
        for wp in self._index.values():
            count += wp.n
        return(count)


class GenerationsIndex(Index):
    pass
