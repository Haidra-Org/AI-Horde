
from horde.redis_ctrl import get_horde_db, is_redis_up
from horde.logger import logger

horde_r = None
logger.init("Horde Redis", status="Connecting")
if is_redis_up():
    horde_r = get_horde_db()
    logger.init_ok("Horde Redis", status="Connected")
else:
    logger.init_err("Horde Redis", status="Failed")
