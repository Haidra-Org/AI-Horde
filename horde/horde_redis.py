from datetime import timedelta
import json
from threading import Lock

from horde.redis_ctrl import get_horde_db, is_redis_up, get_local_horde_db, is_local_redis_up, get_all_redis_db_servers
from horde.logger import logger

locks = {}

horde_r = None
all_horde_redis = []
logger.init("Horde Redis", status="Connecting")
if is_redis_up():
    horde_r = get_horde_db()
    all_horde_redis = get_all_redis_db_servers()
    logger.init_ok("Horde Redis", status="Connected")
else:
    logger.init_err("Horde Redis", status="Failed")


horde_local_r = None
logger.init("Horde Local Redis", status="Connecting")
if is_local_redis_up():
    horde_local_r = get_local_horde_db()
    logger.init_ok("Horde Local Redis", status="Connected")
else:
    logger.init_err("Horde Local Redis", status="Failed")

def horde_r_set(key, value):
    for hr in all_horde_redis:
        try:
            hr.set(key, value)
        except Exception as err:
            logger.warning("Exception when writing in redis servers {hr}: {err}")
    if horde_local_r:
        horde_local_r.setex(key, timedelta(10), value)

def horde_r_setex(key, expiry, value):
    for hr in all_horde_redis:
        try:
            hr.setex(key, expiry, value)
        except Exception as err:
            logger.warning(f"Exception when writing in redis servers {hr}: {err}")
    # We don't keep local cache for more than 5 seconds
    if expiry > timedelta(5):
        expiry = timedelta(5)
    if horde_local_r:
        horde_local_r.setex(key, expiry, value)


def horde_r_setex_json(key, expiry, value):
    """Same as horde_r_setex()
    but also converts the python builtin value to json
    """
    horde_r_setex(key, expiry, json.dumps(value))


def horde_r_local_set_to_json(key, value):
    if horde_local_r:
        if key not in locks:
            locks[key] = Lock()
        locks[key].acquire()
        try:
            horde_local_r.set(key, json.dumps(value))
        except Exception as err:
            logger.error(f"Something went wrong when setting local redis: {e}")
        locks[key].release()

def horde_local_setex_to_json(key, seconds, value):
    if horde_local_r:
        if key not in locks:
            locks[key] = Lock()
        locks[key].acquire()
        try:
            horde_local_r.setex(key, timedelta(seconds=seconds), json.dumps(value))
        except Exception as err:
            logger.error(f"Something went wrong when setting local redis: {e}")
        locks[key].release()

def horde_r_get(key):
    """Retrieves the value from local redis if it exists
    If it doesn't exist retrieves it from remote redis
    If it exists in remote redis, also stores it in local redis
    """
    value = None
    if horde_local_r:
        # if key in ["worker_cache","worker_cache_privileged"]:
        #     logger.warning(f"Got {key} from Local")
        value = horde_local_r.get(key)
    if value is None and horde_r:
        value = horde_r.get(key)
        if value is not None and horde_local_r is not None:
            ttl = horde_r.ttl(key)
            if ttl > 5:
                ttl = 5
            if ttl <= 0:
                ttl = 2
            # The local redis cache is always very temporary
            if value is not None:
                horde_local_r.setex(key, timedelta(seconds=abs(ttl)), value)
    return value

def horde_r_get_json(key):
    """Same as horde_r_get()
    but also converts the json to python built-ins
    """
    value = horde_r_get(key)
    if value is None:
        return None
    return json.loads(value)
