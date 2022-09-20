import json, os, sys
from uuid import uuid4
from datetime import datetime
import threading, time
from logger import logger


class WaitingPrompt:
    def __init__(self, db, wps, pgs, prompt, user, params, **kwargs):
        self._db = db
        self._waiting_prompts = wps
        self._processing_generations = pgs
        self.prompt = prompt
        self.user = user
        self.params = params
        self.n = params.pop('n', 1)
        self.steps = params.pop('steps', 50)
        # We assume more than 20 is not needed. But I'll re-evalute if anyone asks.
        if self.n > 20:
            logger.warning(f"User {self.user.get_unique_alias()} requested {self.n} gens per action. Reducing to 20...")
            self.n = 20
        self.width = params.get("width", 512)
        self.height = params.get("height", 512)
        # To avoid unnecessary calculations, we do it once here.
        self.pixelsteps = self.width * self.height * self.steps
        self.total_usage = round(self.pixelsteps * self.n/1000000,2)
        # The total amount of to pixelsteps requested.
        self.id = str(uuid4())
        # This is what we send to KoboldAI to the /generate/ API
        self.gen_payload = params
        self.gen_payload["prompt"] = prompt
        # We always send only 1 iteration to KoboldAI
        self.gen_payload["batch_size"] = 1
        self.gen_payload["ddim_steps"] = self.steps
        # The generations that have been created already
        self.processing_gens = []
        self.last_process_time = datetime.now()
        self.servers = kwargs.get("servers", [])
        # Prompt requests are removed after 1 mins of inactivity per n, to a max of 5 minutes
        self.stale_time = 180 * self.n
        if self.stale_time > 300:
            self.stale_time = 300


    def activate(self):
        # We separate the activation from __init__ as often we want to check if there's a valid server for it
        # Before we add it to the queue
        self._waiting_prompts.add_item(self)
        logger.info(f"New prompt by {self.user.get_unique_alias()}: w:{self.width} * h:{self.height} * s:{self.steps} * n:{self.n} == {self.total_usage} Total MPs")
        # Remove the threading, because I can't figure out the race conditions
        # thread = threading.Thread(target=self.check_for_stale, args=())
        # thread.daemon = True
        # thread.start()

    # The mps still queued to be generated for this WP
    def get_queued_megapixelsteps(self):
        return(round(self.pixelsteps * self.n/1000000,2))

    def needs_gen(self):
        if self.n > 0:
            return(True)
        return(False)

    def start_generation(self, server):
        if self.n <= 0:
            return
        new_gen = ProcessingGeneration(self, self._processing_generations, server)
        self.processing_gens.append(new_gen)
        self.n -= 1
        self.refresh()
        prompt_payload = {
            "payload": self.gen_payload,
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

    def get_status(self, lite = False):
        ret_dict = self.count_processing_gens()
        ret_dict["waiting"] = self.n
        ret_dict["done"] = self.is_completed()
        queue_pos, queued_mps = self.get_own_queue_stats()
        if queue_pos >= 0:
            # We increment the priority by 1, because it starts at 0
            # And that makes no sense in a queue context
            ret_dict["queue_position"] = queue_pos + 1
            mpsm = self._db.stats.get_megapixelsteps_per_min()
            # Avoid Div/0
            if mpsm > 0:
                ret_dict["wait_time"] = queue_mps / (self._db.stats.get_megapixelsteps_per_min() * 60)
            else:
                ret_dict["wait_time"] = "Unknown"
        # Lite mode does not include the generations, to spare me download size
        if not lite:
            ret_dict["generations"] = []
            for procgen in self.processing_gens:
                if procgen.is_completed():
                    gen_dict = {
                        "img": procgen.generation,
                        "seed": procgen.seed,
                        "server_id": procgen.server.id,
                        "server_name": procgen.server.name,
                    }
                    ret_dict["generations"].append(gen_dict)
        return(ret_dict)

    # Same as status, but without the images to avoid unnecessary size
    def get_lite_status(self):
        ret_dict = self.get_status(True)
        return(ret_dict)

    # Get out position in the working prompts queue sorted by kudos
    # If this gen is completed, we return (-1,-1) which represents this, to avoid doing operations.
    def get_own_queue_stats(self):
        if self.needs_gen():
            return(self._waiting_prompts.get_wp_queue_stats(self))
        return(-1,-1)

    # Record that we received a requested generation and how much kudos it costs us
    def record_usage(self, pixelsteps, kudos):
        self.user.record_usage(pixelsteps, kudos)
        self.refresh()

    def check_for_stale(self):
        while True:
            if self._waiting_prompts.is_deleted(self):
                break
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
        self.seed = None
        self.kudos = 0
        self.start_time = datetime.now()
        self._processing_generations.add_item(self)

    def set_generation(self, generation, seed):
        if self.is_completed():
            return(0)
        self.generation = generation
        self.seed = seed
        pixelsteps_per_sec = self.owner._db.stats.record_fulfilment(self.owner.pixelsteps, self.start_time)
        self.kudos = self.owner._db.convert_pixelsteps_to_kudos(self.owner.pixelsteps)
        self.server.record_contribution(self.owner.pixelsteps, self.kudos, pixelsteps_per_sec)
        self.owner.record_usage(self.owner.pixelsteps, self.kudos)
        
        logger.info(f"New Generation worth {self.kudos} kudos, delivered by server: {self.server.name}")
        return(self.kudos)

    def is_completed(self):
        if self.generation:
            return(True)
        return(False)

    def delete(self):
        self._processing_generations.del_item(self)
        del self


class KAIServer:
    def __init__(self, db):
        self._db = db
        self.kudos_details = {
            "generated": 0,
            "uptime": 0,
        }
        self.last_reward_uptime = 0
        # Every how many seconds does this server get a kudos reward
        self.uptime_reward_threshold = 600

    def create(self, user, name):
        self.user = user
        self.name = name
        self.id = str(uuid4())
        self.contributions = 0
        self.fulfilments = 0
        self.kudos = 0
        self.performances = []
        self.uptime = 0
        self._db.register_new_server(self)

    def check_in(self, max_pixels):
        if not self.is_stale():
            self.uptime += (datetime.now() - self.last_check_in).seconds
            # Every 10 minutes of uptime gets 100 kudos rewarded
            if self.uptime - self.last_reward_uptime > self.uptime_reward_threshold:
                kudos = 100
                self.modify_kudos(kudos,'uptime')
                self.user.record_uptime(kudos)
                logger.debug(f"server '{self.name}' received {kudos} kudos for uptime of {self.uptime_reward_threshold} seconds.")
                self.last_reward_uptime = self.uptime
        else:
            # If the server comes back from being stale, we just reset their last_reward_uptime
            # So that they have to stay up at least 10 mins to get uptime kudos
            self.last_reward_uptime = self.uptime
        self.last_check_in = datetime.now()
        self.max_pixels = max_pixels
        logger.debug(f"Server {self.name} checked-in")

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
        if self.max_pixels < waiting_prompt.width * waiting_prompt.height:
            is_matching = False
            skipped_reason = 'max_pixels'
        return([is_matching,skipped_reason])

    @logger.catch
    def record_contribution(self, pixelsteps, kudos, pixelsteps_per_sec):
        self.user.record_contributions(pixelsteps, kudos)
        self.modify_kudos(kudos,'generated')
        self.contributions = round(self.contributions + pixelsteps/1000000,2) # We store them as Megapixelsteps
        self.fulfilments += 1
        self.performances.append(pixelsteps_per_sec)
        if len(self.performances) > 20:
            del self.performances[0]

    def modify_kudos(self, kudos, action = 'generated'):
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + abs(kudos), 2) 

    def get_performance(self):
        if len(self.performances):
            ret_str = f'{round(sum(self.performances) / len(self.performances),1)} pixelsteps per second'
        else:
            ret_str = f'No requests fulfilled yet'
        return(ret_str)

    def is_stale(self):
        try:
            if (datetime.now() - self.last_check_in).seconds > 300:
                return(True)
        # If the last_check_in isn't set, it's a new server, so it's stale by default
        except AttributeError:
            return(True)
        return(False)

    @logger.catch
    def serialize(self):
        ret_dict = {
            "oauth_id": self.user.oauth_id,
            "name": self.name,
            "max_pixels": self.max_pixels,
            "contributions": self.contributions,
            "fulfilments": self.fulfilments,
            "kudos": self.kudos,
            "kudos_details": self.kudos_details.copy(),
            "performances": self.performances.copy(),
            "last_check_in": self.last_check_in.strftime("%Y-%m-%d %H:%M:%S"),
            "id": self.id,
            "uptime": self.uptime,
        }
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        self.user = self._db.find_user_by_oauth_id(saved_dict["oauth_id"])
        self.name = saved_dict["name"]
        self.max_pixels = saved_dict["max_pixels"]
        self.contributions = saved_dict["contributions"]
        if convert_flag == 'pixelsteps':
            self.contributions = round(self.contributions / 50,2)
        self.fulfilments = saved_dict["fulfilments"]
        self.kudos = saved_dict.get("kudos",0)
        self.kudos_details = saved_dict.get("kudos_details",self.kudos_details)
        self.performances = saved_dict.get("performances",[])
        self.last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
        self.id = saved_dict["id"]
        self.uptime = saved_dict.get("uptime",0)
        self._db.servers[self.name] = self


class Index:
    def __init__(self):
        self._index = {}

    def add_item(self, item):
        logger.debug(item)
        self._index[item.id] = item

    def get_item(self, uuid):
        return(self._index.get(uuid))

    def del_item(self, item):
        logger.debug(item)
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
                count += 1
        return(count)

    def count_total_waiting_generations(self):
        count = 0
        for wp in self._index.values():
            count += wp.n
        return(count)

    def count_totals(self):
        ret_dict = {
            "queued_requests": 0,
            # mps == Megapixelsteps
            "queued_megapixelsteps": 0,
        }
        for wp in self._index.values():
            ret_dict["queued_requests"] += wp.n
            if wp.n > 0:
                ret_dict["queued_megapixelsteps"] += wp.pixelsteps / 1000000
        # We round the end result to avoid to many decimals
        ret_dict["queued_megapixelsteps"] = round(ret_dict["queued_megapixelsteps"],2)
        return(ret_dict)

    def get_waiting_wp_by_kudos(self):
        sorted_wp_list = sorted(self._index.values(), key=lambda x: x.user.kudos, reverse=True)
        final_wp_list = []
        for wp in sorted_wp_list:
            if wp.needs_gen():
                final_wp_list.append(wp)
        return(final_wp_list)

    # Returns the queue position of the provided WP based on kudos
    # Also returns the amount of mps until the wp is generated
    def get_wp_queue_stats(self, wp):
        mps_ahead_in_queue = 0
        priority_sorted_list = self.get_waiting_wp_by_kudos()
        for iter in range(len(priority_sorted_list)):
            mps_ahead_in_queue += priority_sorted_list[iter].get_queued_megapixelsteps()
            if priority_sorted_list[iter] == wp:
                mps_ahead_in_queue = round(mps_ahead_in_queue,2)
                return(iter, mps_ahead_in_queue)
        # -1 means the WP is done and not in the queue
        return(-1,-1)
                

class GenerationsIndex(Index):
    pass


class User:
    def __init__(self, db):
        self._db = db
        self.kudos = 0
        self.kudos_details = {
            "accumulated": 0,
            "gifted": 0,
            "received": 0,
        }

    def create_anon(self):
        self.username = 'Anonymous'
        self.oauth_id = 'anon'
        self.api_key = '0000000000'
        self.invite_id = ''
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = 0
        self.contributions = {
            "megapixelsteps": 0,
            "fulfillments": 0
        }
        self.usage = {
            "megapixelsteps": 0,
            "requests": 0
        }

    def create(self, username, oauth_id, api_key, invite_id):
        self.username = username
        self.oauth_id = oauth_id
        self.api_key = api_key
        self.invite_id = invite_id
        self.creation_date = datetime.now()
        self.last_active = datetime.now()
        self.id = self._db.register_new_user(self)
        self.contributions = {
            "megapixelsteps": 0,
            "fulfillments": 0
        }
        self.usage = {
            "megapixelsteps": 0,
            "requests": 0
        }

    # Checks that this user matches the specified API key
    def check_key(api_key):
        if self.api_key and self.api_key == api_key:
            return(True)
        return(False)

    def get_unique_alias(self):
        return(f"{self.username}#{self.id}")

    def record_usage(self, pixelsteps, kudos):
        self.usage["megapixelsteps"] = round(self.usage["megapixelsteps"] + pixelsteps/1000000,2)
        self.usage["requests"] += 1
        self.modify_kudos(-kudos,"accumulated")

    def record_contributions(self, pixelsteps, kudos):
        self.contributions["megapixelsteps"] = round(self.contributions["megapixelsteps"] + pixelsteps/1000000,2)
        self.contributions["fulfillments"] += 1
        self.modify_kudos(kudos,"accumulated")

    def record_uptime(self, kudos):
        self.modify_kudos(kudos,"accumulated")

    def modify_kudos(self, kudos, action = 'accumulated'):
        logger.debug(f"modifying existing {self.kudos} kudos of {self.get_unique_alias()} by {kudos} for {action}")
        self.kudos = round(self.kudos + kudos, 2)
        self.kudos_details[action] = round(self.kudos_details.get(action,0) + kudos, 2)


    @logger.catch
    def serialize(self):
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
            "creation_date": self.creation_date.strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": self.last_active.strftime("%Y-%m-%d %H:%M:%S"),
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
        if convert_flag == 'pixelsteps':
            # I average to 25 steps, to convert pixels to pixelsteps, since I wasn't tracking it until now
            self.contributions['megapixelsteps'] = round(self.contributions['pixels'] / 50,2)
            del self.contributions['pixels']
            self.usage['megapixelsteps'] = round(self.usage['pixels'] / 50,2)
            del self.usage['pixels']
        self.creation_date = datetime.strptime(saved_dict["creation_date"],"%Y-%m-%d %H:%M:%S")
        self.last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")


class Stats:
    def __init__(self, db, convert_flag = None, interval = 60):
        self.db = db
        self.server_performances = []
        self.fulfillments = []
        self.interval = interval
        self.last_pruning = datetime.now()

    def record_fulfilment(self, pixelsteps, starting_time):
        seconds_taken = (datetime.now() - starting_time).seconds
        if seconds_taken == 0:
            pixelsteps_per_sec = 1
        else:
            pixelsteps_per_sec = round(pixelsteps / seconds_taken,1)
        if len(self.server_performances) >= 10:
            del self.server_performances[0]
        self.server_performances.append(pixelsteps_per_sec)
        fulfillment_dict = {
            "pixelsteps": pixelsteps,
            "start_time": starting_time,
            "deliver_time": datetime.now(),
        }
        self.fulfillments.append(fulfillment_dict)
        return(pixelsteps_per_sec)

    def get_megapixelsteps_per_min(self):
        total_pixelsteps = 0
        pruned_array = []
        for fulfillment in self.fulfillments:
            if (datetime.now() - fulfillment["deliver_time"]).seconds <= 60:
                pruned_array.append(fulfillment)
                total_pixelsteps += fulfillment["pixelsteps"]
        if (datetime.now() - self.last_pruning).seconds > self.interval:
            self.last_pruning = datetime.now()
            self.fulfillments = pruned_array
            logger.debug("Pruned fulfillments")
        megapixelsteps_per_min = round(total_pixelsteps / 1000000,2)
        return(megapixelsteps_per_min)

    def get_request_avg(self):
        if len(self.server_performances) == 0:
            return(0)
        avg = sum(self.server_performances) / len(self.server_performances)
        return(round(avg,1))

    @logger.catch
    def serialize(self):
        serialized_fulfillments = []
        for fulfillment in self.fulfillments.copy():
            json_fulfillment = {
                "pixelsteps": fulfillment["pixelsteps"],
                "start_time": fulfillment["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "deliver_time": fulfillment["deliver_time"].strftime("%Y-%m-%d %H:%M:%S"),
            }
            serialized_fulfillments.append(json_fulfillment)
        ret_dict = {
            "server_performances": self.server_performances,
            "model_mulitpliers": self.model_mulitpliers,
            "fulfillments": serialized_fulfillments,
        }
        return(ret_dict)

    @logger.catch
    def deserialize(self, saved_dict, convert_flag = None):
        # Convert old key
        if "fulfilment_times" in saved_dict:
            self.server_performances = saved_dict["fulfilment_times"]
        else:
            self.server_performances = saved_dict["server_performances"]
        deserialized_fulfillments = []
        for fulfillment in saved_dict.get("fulfillments", []):
            class_fulfillment = {
                "pixelsteps": fulfillment["pixelsteps"],
                "start_time": datetime.strptime(fulfillment["start_time"],"%Y-%m-%d %H:%M:%S"),
                "deliver_time":datetime.strptime(fulfillment["deliver_time"],"%Y-%m-%d %H:%M:%S"),
            }
            deserialized_fulfillments.append(class_fulfillment)
        self.model_mulitpliers = saved_dict["model_mulitpliers"]
        self.fulfillments = deserialized_fulfillments
       
class Database:
    def __init__(self, convert_flag = None, interval = 60):
        self.interval = interval
        self.ALLOW_ANONYMOUS = True
        # This is used for synchronous generations
        self.SERVERS_FILE = "db/servers.json"
        self.servers = {}
        # Other miscellaneous statistics
        self.STATS_FILE = "db/stats.json"
        self.stats = Stats(self)
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
                    new_user = User(self)
                    new_user.deserialize(user_dict,convert_flag)
                    self.users[new_user.oauth_id] = new_user
                    if new_user.id > self.last_user_id:
                        self.last_user_id = new_user.id
        self.anon = self.find_user_by_oauth_id('anon')
        if not self.anon:
            self.anon = User(self)
            self.anon.create_anon()
            self.users[self.anon.oauth_id] = self.anon
        if os.path.isfile(self.SERVERS_FILE):
            with open(self.SERVERS_FILE) as db:
                serialized_servers = json.load(db)
                for server_dict in serialized_servers:
                    new_server = KAIServer(self)
                    new_server.deserialize(server_dict,convert_flag)
                    self.servers[new_server.name] = new_server
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
        logger.init_ok(f"Database Load", status="Completed")

    def write_files(self):
        logger.init_ok("Database Store Thread", status="Started")
        while True:
            self.write_files_to_disk()
            time.sleep(self.interval)

    def write_files_to_disk(self):
        if not os.path.exists('db'):
            os.mkdir('db')
        server_serialized_list = []
        logger.debug("Saving DB")
        for server in self.servers.copy().values():
            # We don't store data for anon servers
            if server.user == self.anon: continue
            server_serialized_list.append(server.serialize())
        with open(self.SERVERS_FILE, 'w') as db:
            json.dump(server_serialized_list,db)
        with open(self.STATS_FILE, 'w') as db:
            json.dump(self.stats.serialize(),db)
        user_serialized_list = []
        for user in self.users.copy().values():
            user_serialized_list.append(user.serialize())
        with open(self.USERS_FILE, 'w') as db:
            json.dump(user_serialized_list,db)

    def get_top_contributor(self):
        top_contribution = 0
        top_contributor = None
        user = None
        for user in self.users.values():
            if user.contributions['megapixelsteps'] > top_contribution and user != self.anon:
                top_contributor = user
                top_contribution = user.contributions['megapixelsteps']
        return(top_contributor)

    def get_top_server(self):
        top_server = None
        top_server_contribution = 0
        for server in self.servers:
            if self.servers[server].contributions > top_server_contribution:
                top_server = self.servers[server]
                top_server_contribution = self.servers[server].contributions
        return(top_server)

    def count_active_servers(self):
        count = 0
        for server in self.servers.values():
            if not server.is_stale():
                count += 1
        return(count)

    def get_total_usage(self):
        totals = {
            "megapixelsteps": 0,
            "fulfilments": 0,
        }
        for server in self.servers.values():
            totals["megapixelsteps"] += server.contributions
            totals["fulfilments"] += server.fulfilments
        return(totals)


    def register_new_user(self, user):
        self.last_user_id += 1
        self.users[user.oauth_id] = user
        logger.info(f'New user created: {user.username}#{self.last_user_id}')
        return(self.last_user_id)

    def register_new_server(self, server):
        self.servers[server.name] = server
        logger.info(f'New server checked-in: {server.name} by {server.user.get_unique_alias()}')

    def find_user_by_oauth_id(self,oauth_id):
        if oauth_id == 'anon' and not self.ALLOW_ANONYMOUS:
            return(None)
        return(self.users.get(oauth_id))

    def find_user_by_username(self, username):
        for user in self.users.values():
            uniq_username = username.split('#')
            if user.username == uniq_username[0] and user.id == int(uniq_username[1]):
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

    def find_server_by_name(self,server_name):
        return(self.servers.get(server_name))

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

    def convert_pixelsteps_to_kudos(self, pixelsteps):
        # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
        kudos = round(pixelsteps / (512*512*5),2)
        # logger.info([pixels,multiplier,kudos])
        return(kudos)
