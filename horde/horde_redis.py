# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import threading
import time
from datetime import datetime, timedelta
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
    def __init__(self):
        self.locks = {}
        self.horde_r = None
        self.all_horde_redis = []
        self.horde_local_r = None
        self.check_redis_thread = None

    def connect(self):
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

    def horde_r_incrbyfloat(self, key, amount):
        """Atomically add `amount` (may be negative) to a shared float counter on
        every cluster redis server. Used for high-frequency accumulators (e.g.
        anonymous-user kudos) that replace a contended DB row update. Writes to
        the shared cluster only, never the per-instance local cache; read and
        reset the counter with horde_r_getset_float().
        """
        for hr in self.all_horde_redis:
            try:
                hr.incrbyfloat(key, amount)
            except Exception as err:
                logger.warning(f"Exception when incrementing in redis servers {hr}: {err}")

    def horde_r_getset_float(self, key):
        """Atomically read and reset (to 0) a shared float counter across every
        cluster redis server, returning the largest-magnitude value observed.
        The servers agree when all are healthy; taking the max magnitude and
        resetting each tolerates a briefly lagging mirror. Returns 0.0 when the
        counter is absent everywhere.
        """
        best = 0.0
        for hr in self.all_horde_redis:
            try:
                raw = hr.getset(key, 0)
            except Exception as err:
                logger.warning(f"Exception when reading/resetting redis servers {hr}: {err}")
                continue
            if raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if abs(val) > abs(best):
                best = val
        return best

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

        def default_converter(o):
            if isinstance(o, datetime):
                return o.strftime("%a, %d %b %Y %H:%M:%S +0000")
            raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

        self.horde_r_setex(key, expiry, json.dumps(value, default=default_converter))

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
