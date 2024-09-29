# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import threading
import time
from datetime import timedelta
from threading import Lock

from horde.logger import logger
from horde.redis_ctrl import (
    get_all_redis_db_servers,
    get_horde_db,
    get_local_horde_db,
    is_local_redis_up,
    is_redis_up,
)


class HordeRedis:
    locks = {}
    horde_r = None
    all_horde_redis = []
    horde_local_r = None
    check_redis_thread = None

    def __init__(self):
        logger.init("Horde Redis", status="Connecting")
        if is_redis_up():
            self.horde_r = get_horde_db()
            self.all_horde_redis = get_all_redis_db_servers()
            logger.init_ok("Horde Redis", status="Connected")
        else:
            logger.init_err("Horde Redis", status="Failed")
        logger.init("Horde Local Redis", status="Connecting")
        if is_local_redis_up():
            self.horde_local_r = get_local_horde_db()
            logger.init_ok("Horde Local Redis", status="Connected")
        else:
            logger.init_err("Horde Local Redis", status="Failed")
        self.check_redis_thread = threading.Thread(target=self.check_redis_backends, args=(), daemon=True)
        self.check_redis_thread.start()

    def check_redis_backends(self):
        while True:
            time.sleep(10)
            self.all_horde_redis = get_all_redis_db_servers()

    def horde_r_set(self, key, value):
        for hr in self.all_horde_redis:
            try:
                hr.set(key, value)
            except Exception as err:
                logger.warning(f"Exception when writing in redis servers {hr}: {err}")
        if self.horde_local_r:
            self.horde_local_r.setex(key, timedelta(10), value)

    def horde_r_setex(self, key, expiry, value):
        for hr in self.all_horde_redis:
            try:
                hr.setex(key, expiry, value)
            except Exception as err:
                logger.warning(f"Exception when writing in redis servers {hr}: {err}")
        # We don't keep local cache for more than 5 seconds
        if expiry > timedelta(5):
            expiry = timedelta(5)
        if self.horde_local_r:
            self.horde_local_r.setex(key, expiry, value)

    def horde_r_setex_json(self, key, expiry, value):
        """Same as horde_r_setex()
        but also converts the python builtin value to json
        """
        self.horde_r_setex(key, expiry, json.dumps(value))

    def horde_r_local_set_to_json(self, key, value):
        if self.horde_local_r:
            if key not in self.locks:
                self.locks[key] = Lock()
            self.locks[key].acquire()
            try:
                self.horde_local_r.set(key, json.dumps(value))
            except Exception as err:
                logger.error(f"Something went wrong when setting local redis: {err}")
            self.locks[key].release()

    def horde_local_setex_to_json(self, key, seconds, value):
        if self.horde_local_r:
            if key not in self.locks:
                self.locks[key] = Lock()
            self.locks[key].acquire()
            try:
                self.horde_local_r.setex(key, timedelta(seconds=seconds), json.dumps(value))
            except Exception as err:
                logger.error(f"Something went wrong when setting local redis: {err}")
            self.locks[key].release()

    def horde_r_get(self, key):
        """Retrieves the value from local redis if it exists
        If it doesn't exist retrieves it from remote redis
        If it exists in remote redis, also stores it in local redis
        """
        value = None
        if self.horde_local_r:
            # if key in ["worker_cache","worker_cache_privileged"]:
            #     logger.warning(f"Got {key} from Local")
            value = self.horde_local_r.get(key)
        if value is None and self.horde_r:
            value = self.horde_r.get(key)
            if value is not None and self.horde_local_r is not None:
                ttl = self.horde_r.ttl(key)
                if ttl > 5:
                    ttl = 5
                if ttl <= 0:
                    ttl = 2
                # The local redis cache is always very temporary
                if value is not None:
                    self.horde_local_r.setex(key, timedelta(seconds=abs(ttl)), value)
        return value

    def horde_r_get_json(self, key):
        """Same as horde_r_get()
        but also converts the json to python built-ins
        """
        value = self.horde_r_get(key)
        if value is None:
            return None
        return json.loads(value)

    def horde_r_delete(self, key):
        for hr in self.all_horde_redis:
            try:
                hr.delete(key)
            except Exception as err:
                logger.warning(f"Exception when deleting from redis servers {hr}: {err}")
        if self.horde_local_r:
            self.horde_local_r.delete(key)


horde_redis = HordeRedis()
