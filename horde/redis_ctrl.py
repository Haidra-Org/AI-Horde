# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
import socket

import redis

from horde.logger import logger

redis_hostname = os.getenv("REDIS_IP", "localhost")
redis_port = int(os.getenv("REDIS_PORT", "6379"))
# Bounded socket waits for every redis client. Without them a stalled server or a half-open connection blocks
# the calling request thread for as long as the kernel keeps retrying, holding its database connection, its
# open transaction and any advisory lock it took for the whole time. A timeout turns that into an exception the
# caller already handles (redis writes are best-effort; reads fall back to the database).
redis_socket_timeout_seconds = float(os.getenv("REDIS_SOCKET_TIMEOUT", "5"))
redis_socket_connect_timeout_seconds = float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "3"))
redis_address = (
    f"redis://{redis_hostname}:{redis_port}"
    f"?socket_timeout={redis_socket_timeout_seconds}&socket_connect_timeout={redis_socket_connect_timeout_seconds}"
)

horde_db = 0
limiter_db = 1
ipaddr_db = 2
cache_db = 3
ipaddr_supicion_db = 4
ipaddr_timeout_db = 5


def is_redis_up(hostname=redis_hostname, port=redis_port) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(3)
        try:
            return s.connect_ex((hostname, port)) == 0
        except socket.gaierror as e:
            # connect_ex suppresses exceptions from POSIX connect() call
            # but can still raise gaierror if e.g. the hostname is invalid.
            # This may be transient, so log the error and return False.
            logger.error(f"Redis server at {hostname}:{port} is not reachable: {e}")
            return False


def is_local_redis_up() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", redis_port)) == 0


def _redis_url(db: int) -> str:
    """Return the connection URL for logical database ``db``; the socket timeouts travel as query parameters."""
    host_and_query = redis_address.split("?", 1)
    return f"{host_and_query[0]}/{db}?{host_and_query[1]}"


def ger_limiter_url():
    return _redis_url(limiter_db)


def ger_cache_url():
    return _redis_url(cache_db)


def _redis_client(host: str, db: int, *, decode_responses: bool = False) -> redis.Redis:
    """Return a redis client for ``host``/``db`` with the shared socket timeouts applied."""
    return redis.Redis(
        host=host,
        port=redis_port,
        db=db,
        decode_responses=decode_responses,
        socket_timeout=redis_socket_timeout_seconds,
        socket_connect_timeout=redis_socket_connect_timeout_seconds,
    )


def get_horde_db():
    return _redis_client(redis_hostname, horde_db, decode_responses=True)


def get_local_horde_db():
    return _redis_client("127.0.0.1", 6, decode_responses=True)


def get_ipaddr_db():
    return _redis_client(redis_hostname, ipaddr_db)


def get_ipaddr_suspicion_db():
    return _redis_client(redis_hostname, ipaddr_supicion_db)


def get_ipaddr_timeout_db():
    return _redis_client(redis_hostname, ipaddr_timeout_db)


def get_redis_db_server(server_ip):
    return _redis_client(server_ip, horde_db, decode_responses=True)


_redis_db_server_clients: dict[str, redis.Redis] = {}


def _redis_db_server_client(server_ip: str) -> redis.Redis:
    """Return the shared client for ``server_ip``, creating it once; a client reconnects on its own."""
    client = _redis_db_server_clients.get(server_ip)
    if client is None:
        client = get_redis_db_server(server_ip)
        _redis_db_server_clients[server_ip] = client
    return client


def get_all_redis_db_servers():
    """An array of all the redis servers in the cluster
    We use this to always store the entries in all servers
    This allows redis to transparently failover.
    """
    try:
        working_redis = []
        for rs in json.loads(os.getenv("REDIS_SERVERS")):
            if is_redis_up(rs):
                working_redis.append(_redis_db_server_client(rs))
            else:
                logger.warning(f"redis server '{rs} appears unreachable. Will not be used set in the cluster")
        return working_redis
    except Exception:
        logger.error("Error setting up REDIS_SERVERS array. Falling back to loadbalancer.")
        return [get_horde_db()]
