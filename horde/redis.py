import socket

redis_ip = "localhost"
redis_port = 6379
redis_address = f"redis://{redis_ip}:{redis_port}"

limiter_db = 1
ipaddr_db = 2

def is_redis_up() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((redis_ip, redis_port)) == 0

def ger_limiter_db():
    return(f"{redis_address}/{limiter_db}")

def get_ipaddr_db():
    return(f"{redis_address}/{ipaddr_db}")
