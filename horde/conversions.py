import sys
import os
import json
from datetime import datetime

from horde.logger import logger
from horde.flask import db
from horde.vars import thing_name, thing_divisor
from horde.suspicions import Suspicions, SUSPICION_LOGS
from horde.classes import User, Worker, Team, stats, database
from horde.utils import hash_api_key


def convert_json_db():
    convert_user_json()
    sys.exit()

def convert_user_json():
    USERS_FILE = "db/users.json"
    if os.path.isfile(USERS_FILE):
        with open(USERS_FILE) as db:
            serialized_users = json.load(db)
            for user_dict in serialized_users:
                if not user_dict:
                    logger.error("Found null user on db load. Bypassing")
                    continue
                convert_user(user_dict)    

@logger.catch(reraise=True)
def convert_user(saved_dict):
    username = saved_dict["username"]
    oauth_id = saved_dict["oauth_id"]
    api_key = saved_dict["api_key"]
    kudos = saved_dict["kudos"]
    kudos_details = saved_dict.get("kudos_details", {})
    id = saved_dict["id"]
    unique_alias = f"{username}#{id}"
    invite_id = saved_dict["invite_id"]
    contributions = saved_dict["contributions"]
    usage = saved_dict["usage"]
    moderator=saved_dict.get("moderator", False)
    concurrency = saved_dict.get("concurrency", 30)
    usage_multiplier = saved_dict.get("usage_multiplier", 1.0)
    # I am putting int() here, to convert a boolean entry I had in the past
    worker_invited = int(saved_dict.get("worker_invited", 0))
    suspicions = saved_dict.get("suspicions", [])
    contact = saved_dict.get("contact",None)
    for suspicion in suspicions.copy():
        if suspicion == 9:
            suspicions.remove(suspicion)
            continue
    public_workers = saved_dict.get("public_workers", False)
    trusted = saved_dict.get("trusted", False)
    evaluating_kudos = saved_dict.get("evaluating_kudos", 0)
    monthly_kudos = {}
    serialized_monthly_kudos = saved_dict.get("monthly_kudos")
    if serialized_monthly_kudos and serialized_monthly_kudos['last_received'] != None:
        monthly_kudos['amount'] = serialized_monthly_kudos['amount']
        monthly_kudos['last_received'] = datetime.strptime(serialized_monthly_kudos['last_received'],"%Y-%m-%d %H:%M:%S")
    creation_date = datetime.strptime(saved_dict["creation_date"],"%Y-%m-%d %H:%M:%S")
    last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")
    duplicate_user = database.find_user_by_id(id)
    if duplicate_user:
        logger.debug(duplicate_user)
        if duplicate_user.get_unique_alias() != unique_alias:
            logger.error(f"mismatching duplicate IDs found! {unique_alias} != {duplicate_user.get_unique_alias()}. Please cleanup manually!")
        else:
            logger.warning(f"found duplicate ID: {[duplicate_user,unique_alias,id,duplicate_user.id,duplicate_user.get_unique_alias()]}")
            duplicate_user.kudos += kudos
            if duplicate_user.last_active < last_active:
                logger.warning(f"Merging {oauth_id} into {duplicate_user.oauth_id}")
                duplicate_user.oauth_id = oauth_id
    new_user = User(
        id=id,
        username=username,
        oauth_id=oauth_id,
        api_key=hash_api_key(api_key),
        created=creation_date,
        last_active=last_active,
        contact=contact,
        kudos=kudos,
        monthly_kudos=monthly_kudos.get('amount', 0),
        monthly_kudos_last_received=monthly_kudos.get('last_received', datetime.utcnow()),
        evaluating_kudos=evaluating_kudos,
        usage_multiplier=usage_multiplier,
        contributed_thing=contributions[thing_name],
        contributed_fulfillments=contributions["fulfillments"],
        usage_thing=usage[thing_name],
        usage_requests=usage["requests"],
        moderator=moderator,
        public_workers=public_workers,
        trusted=trusted,
        concurrency=concurrency,
        # min_kudos=min_kudos,
    )
    new_user.set_min_kudos()
    db.session.add(new_user)
    db.session.commit()
    if new_user.is_stale():
        db.session.delete(new_user)
        db.session.commit()
        logger.message(f"Stale user {new_user.get_unique_alias()} Skipped")
    new_user.import_kudos_details(kudos_details)
    new_user.import_suspicions(suspicions)
    logger.message(f"Converted {new_user.get_unique_alias()}")



@logger.catch(reraise=True)
def onboard_worker(saved_dict):
    user = database.find_user_by_oauth_id(saved_dict["oauth_id"])
    name = saved_dict["name"]
    contributions = saved_dict["contributions"]
    fulfilments = saved_dict["fulfilments"]
    uncompleted_jobs = saved_dict.get("uncompleted_jobs",0)
    kudos = saved_dict.get("kudos",0)
    kudos_details = saved_dict.get("kudos_details",kudos_details)
    performances = saved_dict.get("performances",[])
    last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
    id = saved_dict["id"]
    uptime = saved_dict.get("uptime",0)
    maintenance = saved_dict.get("maintenance",False)
    maintenance_msg = saved_dict.get("maintenance_msg",default_maintenance_msg)
    threads = saved_dict.get("threads",1)
    paused = saved_dict.get("paused",False)
    info = saved_dict.get("info",None)
    team_id = saved_dict.get("team",None)
    if team_id:
        team = database.find_team_by_id(team_id)
    nsfw = saved_dict.get("nsfw",True)
    blacklist = saved_dict.get("blacklist",[])
    ipaddr = saved_dict.get("ipaddr", None)
    # suspicions = saved_dict.get("suspicions", [])
    # for suspicion in suspicions.copy():
    #     if suspicion == "clean_dropped_jobs":
    #         suspicions.remove(suspicion)
    #         continue
    #     suspicious += 1
    #     logger.debug(f"Suspecting worker {name} for {suspicious} with reasons {suspicions}")
    old_model = saved_dict.get("model")
    models = saved_dict.get("models", [old_model])
    check_for_bad_actor()
    if is_suspicious():
        db.workers[name] = self
    if convert_flag == "kudos_fix":
        multiplier = 20
        # Average kudos in the kobold horde is much bigger
        if args.horde == 'kobold':
            multiplier = 100
        recalc_kudos =  (fulfilments) * multiplier
        kudos = recalc_kudos + kudos_details.get("uptime",0)
        kudos_details['generated'] = recalc_kudos
        user.kudos_details['accumulated'] += kudos_details['uptime']
        user.kudos += kudos_details['uptime']


@logger.catch(reraise=True)
def convert_team(saved_dict):
    user = db.find_user_by_oauth_id(saved_dict["oauth_id"])
    name = saved_dict["name"]
    contributions = saved_dict["contributions"]
    fulfilments = saved_dict["fulfilments"]
    kudos = saved_dict.get("kudos",0)
    last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S")
    id = saved_dict["id"]
    uptime = saved_dict.get("uptime",0)
    info = saved_dict.get("info",None)

@logger.catch(reraise=True)
def convert_stats(saved_dict):
    # Convert old key
    if "server_performances" in saved_dict:
        worker_performances = saved_dict["server_performances"]
    else:
        worker_performances = saved_dict["worker_performances"]
    model_performances = saved_dict.get("model_performances", {})
    deserialized_fulfillments = []
    for fulfillment in saved_dict.get("fulfillments", []):
        class_fulfillment = {
            raw_thing_name: fulfillment[raw_thing_name],
            "start_time": datetime.strptime(fulfillment["start_time"],"%Y-%m-%d %H:%M:%S"),
            "deliver_time":datetime.strptime(fulfillment["deliver_time"],"%Y-%m-%d %H:%M:%S"),
        }
        deserialized_fulfillments.append(class_fulfillment)
    fulfillments = deserialized_fulfillments    