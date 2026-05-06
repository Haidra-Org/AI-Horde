# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from horde.logger import logger
from horde.redis_ctrl import ger_limiter_url, is_redis_up

limiter = Limiter(key_func=get_remote_address, default_limits=["90 per minute"], headers_enabled=True)


def init_limiter(app):
    logger.init("Limiter Cache", status="Connecting")
    if is_redis_up():
        try:
            app.config["RATELIMIT_STORAGE_URI"] = ger_limiter_url()
            app.config["RATELIMIT_STRATEGY"] = "fixed-window"
            limiter.init_app(app)
            logger.init_ok("Limiter Cache", status="Connected")
            return
        except Exception as e:
            logger.error(f"Failed to connect to Limiter Cache: {e}")

    limiter.init_app(app)
    logger.init_warn("Limiter Cache", status="Memory Only")
