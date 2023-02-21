import sys
import os
import json
from datetime import datetime

from horde.database import functions as database
from horde.logger import logger
from horde.flask import db
from horde.vars import thing_name, thing_divisor, raw_thing_name
from horde.suspicions import Suspicions, SUSPICION_LOGS
from horde.classes.base.user import User
from horde.classes.base.team import Team, stats
import horde.classes.base.stats as stats
from horde.classes.stable.worker import ImageWorker
from horde.utils import hash_api_key

## Add FulfillmentPerformance fields
# ALTER TABLE horde_fulfillments ADD COLUMN thing_type VARCHAR(20) NOT NULL DEFAULT 'image';
# CREATE INDEX idx_horde_fulfillments_thing_type ON public.horde_fulfillments USING btree (thing_type);
## Add WP fields
# ALTER TABLE waiting_prompts ADD COLUMN wp_type VARCHAR(30) NOT NULL DEFAULT 'image';
# CREATE INDEX idx_waiting_prompts_wp_type ON public.waiting_prompts USING btree (wp_type);
# ALTER TABLE waiting_prompts ADD COLUMN max_length INTEGER NOT NULL DEFAULT 80;
# CREATE INDEX idx_waiting_prompts_max_length ON public.waiting_prompts USING btree(max_length);
# ALTER TABLE waiting_prompts ADD COLUMN max_content_length INTEGER NOT NULL DEFAULT 1024;
# CREATE INDEX idx_waiting_prompts_max_content_length ON public.waiting_prompts USING btree(max_content_length);
# ALTER TABLE waiting_prompts ADD COLUMN softprompt VARCHAR(255);
# CREATE INDEX idx_waiting_prompts_faulted ON public.waiting_prompts USING btree(faulted);
# ALTER TABLE processing_gens ADD COLUMN procgen_type VARCHAR(30) NOT NULL DEFAULT 'image';
# CREATE INDEX idx_processing_gens_procgen_type ON public.processing_gens USING btree (procgen_type);


def convert_json_db():
    convert_json("db/users.json", convert_user)
    convert_json("db/teams.json",convert_team)
    convert_json("db/workers.json",convert_worker)
    convert_stats("db/stats.json")
    sys.exit()

def convert_json(file, method):
    if os.path.isfile(file):
        with open(file) as db:
            serialized = json.load(db)
            for sdict in serialized:
                if not sdict:
                    logger.error("Found null on db load. Bypassing")
                    continue
                method(sdict)    

@logger.catch(reraise=True)
def convert_user(saved_dict):
    kudos_details = saved_dict.get("kudos_details", {})
    contributions = saved_dict["contributions"]
    usage = saved_dict["usage"]
    suspicions = saved_dict.get("suspicions", [])
    for suspicion in suspicions.copy():
        if suspicion == 9:
            suspicions.remove(suspicion)
            continue
    monthly_kudos = {}
    serialized_monthly_kudos = saved_dict.get("monthly_kudos")
    if serialized_monthly_kudos and serialized_monthly_kudos['last_received'] != None:
        monthly_kudos['amount'] = serialized_monthly_kudos['amount']
        monthly_kudos['last_received'] = datetime.strptime(serialized_monthly_kudos['last_received'],"%Y-%m-%d %H:%M:%S")
    new_user = User(
        id=saved_dict["id"],
        username = saved_dict["username"],
        oauth_id = saved_dict["oauth_id"],
        api_key = hash_api_key(saved_dict["api_key"]),
        created = datetime.strptime(saved_dict["creation_date"],"%Y-%m-%d %H:%M:%S"),
        last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S"),
        contact = saved_dict.get("contact",None),
        kudos = saved_dict["kudos"],
        monthly_kudos = monthly_kudos.get('amount', 0),
        monthly_kudos_last_received = monthly_kudos.get('last_received', datetime.utcnow()),
        evaluating_kudos = saved_dict.get("evaluating_kudos", 0),
        usage_multiplier = saved_dict.get("usage_multiplier", 1.0),
        contributed_thing = contributions[thing_name],
        contributed_fulfillments = contributions["fulfillments"],
        usage_thing = usage[thing_name],
        usage_requests = usage["requests"],
        worker_invited = int(saved_dict.get("worker_invited", 0)),
        moderator = saved_dict.get("moderator", False),
        public_workers = saved_dict.get("public_workers", False),
        trusted = saved_dict.get("trusted", False),
        concurrency = saved_dict.get("concurrency", 30),
    )
    db.session.add(new_user)
    db.session.commit()
    if new_user.is_stale():
        db.session.delete(new_user)
        db.session.commit()
        logger.message(f"Stale user {new_user.get_unique_alias()} Skipped")
        return
    new_user.import_kudos_details(kudos_details)
    new_user.import_suspicions(suspicions)
    logger.message(f"Converted User: {new_user.get_unique_alias()}")


@logger.catch(reraise=True)
def convert_team(saved_dict):
    user = database.find_user_by_oauth_id(saved_dict["oauth_id"])
    new_team = Team(
        id=saved_dict["id"],
        info = saved_dict.get("info",None),
        name = saved_dict["name"],
        owner_id=user.id,
        contributions=saved_dict["contributions"],
        fulfilments = saved_dict["fulfilments"],
        kudos = saved_dict.get("kudos",0),
        last_active = datetime.strptime(saved_dict["last_active"],"%Y-%m-%d %H:%M:%S"),
        uptime = saved_dict.get("uptime",0),

    )
    db.session.add(new_team)
    db.session.commit()
    logger.message(f"Converted Team: {new_team.name}")

@logger.catch(reraise=True)
def convert_worker(saved_dict):
    last_check_in = datetime.strptime(saved_dict["last_check_in"],"%Y-%m-%d %H:%M:%S")
    if (datetime.now() - last_check_in).days > 30: 
        logger.message(f"Skipping Stale Worker {saved_dict['name']}")
        return
    user = database.find_user_by_oauth_id(saved_dict["oauth_id"])
    new_worker = ImageWorker(
        id = saved_dict["id"],
        user_id = user.id,
        name = saved_dict["name"],
        info = saved_dict.get("info",None),
        ipaddr = saved_dict.get("ipaddr", None),
        last_check_in = last_check_in,
        kudos = saved_dict.get("kudos",0),
        fulfilments = saved_dict["fulfilments"],
        contributions = saved_dict["contributions"],
        uncompleted_jobs = saved_dict.get("uncompleted_jobs",0),
        uptime = saved_dict.get("uptime",0),
        threads = saved_dict.get("threads",1),
        paused = saved_dict.get("paused",False),
        maintenance = saved_dict.get("maintenance",False),
        maintenance_msg = saved_dict.get("maintenance_msg", "This worker has been put into maintenance mode by its owner"),
        nsfw = saved_dict.get("nsfw",True),
        team_id=saved_dict.get("team",None),
    )
    db.session.add(new_worker)
    db.session.commit()
    new_worker.import_kudos_details(saved_dict.get("kudos_details",{}))
    new_worker.import_performances(saved_dict.get("performances",[]))
    new_worker.import_suspicions(saved_dict.get("suspicions",[]))
    logger.message(f"Converted Worker: {new_worker.name}")



@logger.catch(reraise=True)
def convert_stats(filename):
    if not os.path.isfile(filename):
        return
    with open(filename) as filedb:
        saved_dict = json.load(filedb)
    model_performances = saved_dict.get("model_performances", {})
    for model_name in model_performances:
        for m_p in model_performances[model_name]:
            new_m = stats.ModelPerformance(
                model = model_name,
                performance = m_p
            )
            db.session.add(new_m)
    logger.message("Converted Model Performances")
    for fulfillment in saved_dict.get("fulfillments", []):
        new_f = stats.FulfillmentPerformance(
            created=datetime.strptime(fulfillment["start_time"],"%Y-%m-%d %H:%M:%S"),
            deliver_time=datetime.strptime(fulfillment["deliver_time"],"%Y-%m-%d %H:%M:%S"),
            things=fulfillment[raw_thing_name],
            )
        db.session.add(new_f)
    logger.message("Converted Fulfillments")
    db.session.commit()
