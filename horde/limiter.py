from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from .flask import HORDE

def is_redis_up() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', 6379)) == 0

# Very basic DOS prevention
if is_redis_up():
    limiter = Limiter(
        HORDE,
        key_func=get_remote_address,
        storage_uri="redis://localhost:6379/1",
        # storage_options={"connect_timeout": 30},
        strategy="fixed-window", # or "moving-window"
        default_limits=["90 per minute"]
    )
# Allow local workatation run
else:
    limiter = Limiter(
        HORDE,
        key_func=get_remote_address,
        default_limits=["90 per minute"]
    )
