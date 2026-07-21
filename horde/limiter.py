# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os

from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from horde.logger import logger
from horde.redis_ctrl import ger_limiter_url, is_redis_up

limiter = Limiter(key_func=get_remote_address, default_limits=["90 per minute"], headers_enabled=True)

# Follows the HORDE_TEST_APIKEYS test-gate precedent: these are the only string
# values that flip an opt-in test toggle on. Anything else leaves production
# behaviour untouched.
_TEST_TOGGLE_TRUE_VALUES = {"1", "true", "yes", "on"}


def _is_ratelimit_disabled() -> bool:
    # Exists so load-test scenarios (e.g. the queue-pressure rig) can drive the
    # server hard without tripping flask-limiter's per-IP/per-key caps. Defaults
    # to disabled-toggle-off, so unset env == current production behaviour.
    return os.getenv("HORDE_TEST_RATELIMIT_DISABLED", "0").strip().lower() in _TEST_TOGGLE_TRUE_VALUES


def init_limiter(app: Flask) -> None:
    logger.init("Limiter Cache", status="Connecting")
    if _is_ratelimit_disabled():
        # Setting the config before init_app makes flask-limiter honour it in
        # both the redis and memory-only branches below (init_app reads
        # RATELIMIT_ENABLED via setdefault), disabling default_limits and every
        # per-route @limiter.limit decorator at once.
        app.config["RATELIMIT_ENABLED"] = False
        logger.init_warn("Limiter Cache", status="Disabled (HORDE_TEST_RATELIMIT_DISABLED)")
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
