import socket
import redis

hostname = "localhost"
port = 6379
address = f"redis://{hostname}:{port}"

limiter_db = 1
ipaddr_db = 2
cache_db = 3

def is_redis_up() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((hostname, port)) == 0

def ger_limiter_url():
    return(f"{address}/{limiter_db}")

def ger_cache_url():
    return(f"{address}/{cache_db}")

def get_ipaddr_db():
    rdb = redis.Redis(
        host=hostname,
        port=port,
        db = ipaddr_db)
    return(rdb)
