from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from .flask import REST_API

# Very basic DOS prevention
try:
    limiter = Limiter(
        REST_API,
        key_func=get_remote_address,
        storage_uri="redis://localhost:6379/1",
        # storage_options={"connect_timeout": 30},
        strategy="fixed-window", # or "moving-window"
        default_limits=["90 per minute"]
    )
# Allow local workatation run
except:
    limiter = Limiter(
        REST_API,
        key_func=get_remote_address,
        default_limits=["90 per minute"]
    )
