from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from .flask import HORDE
from .redis_ctrl import is_redis_up, ger_limiter_url

limiter = None
# Very basic DOS prevention
if is_redis_up():
    try:
        limiter = Limiter(
            HORDE,
            key_func=get_remote_address,
            storage_uri=ger_limiter_url(),
            # storage_options={"connect_timeout": 30},
            strategy="fixed-window", # or "moving-window"
            default_limits=["90 per minute"]
        )
    except:
        pass
# Allow local workatation run
if limiter == None:
    limiter = Limiter(
        HORDE,
        key_func=get_remote_address,
        default_limits=["90 per minute"]
    )
