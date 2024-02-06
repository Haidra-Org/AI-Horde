from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from horde.flask import HORDE
from horde.logger import logger
from .redis_ctrl import is_redis_up, ger_limiter_url

limiter = None
# Very basic DOS prevention
logger.init("Limiter Cache", status="Connecting")
if is_redis_up():
    # if is_redis_up():
    try:
        limiter = Limiter(
            HORDE,
            key_func=get_remote_address,
            storage_uri=ger_limiter_url(),
            # storage_options={"connect_timeout": 30},
            strategy="fixed-window",  # or "moving-window"
            default_limits=["90 per minute"],
            headers_enabled=True,
        )
        logger.init_ok("Limiter Cache", status="Connected")
    except Exception as e:
        logger.error(f"Failed to connect to Limiter Cache: {e}")

# Allow local workstation run
if limiter is None:
    limiter = Limiter(
        HORDE,
        key_func=get_remote_address,
        default_limits=["90 per minute"],
        headers_enabled=True,
    )
    logger.init_warn("Limiter Cache", status="Memory Only")
