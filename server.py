# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import os

from dotenv import load_dotenv

profile = os.environ.get("PROFILE")

if profile is not None:
    env_file = f".env_{profile}"
    load_dotenv(env_file)
else:
    load_dotenv()

from horde.argparser import args
from horde.flask import create_app
from horde.logger import logger, reconfigure_from_args
from horde.metrics import waitress_metrics
from horde.telemetry import init_telemetry_late

reconfigure_from_args(args)
app = create_app()
init_telemetry_late(app)

# CLI modes (moved from horde/__init__.py)
if args.force_subscription:
    from horde.ops import force_subscription_kudos

    logger.info(f"forcing kudos on user_id: {args.force_subscription}")
    force_subscription_kudos(args.force_subscription, args.prevent_date_change)
    import sys

    sys.exit()

if args.test:
    from horde.sandbox import test

    test()

if args.check_prompts:
    import horde.database.threads as threads

    threads.check_waiting_prompts()
    import sys

    sys.exit()

if args.new_patreons:
    import sys

    sys.exit()

if __name__ == "__main__":
    # Only setting this for the WSGI logs
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s",
        level=logging.WARNING,
    )
    # waitress.queue logs "Task queue depth is N" at WARNING for every task
    # enqueued while no worker thread is idle. Under load this fires thousands
    # of times per minute and drowns out useful signal. Saturation is already
    # tracked via the waitress_metrics histogram + telemetry, so silence the
    # per-event log.
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    import waitress

    # Monkeypatch to get metrics until below is done
    # https://github.com/Pylons/waitress/issues/182
    _create_server = waitress.create_server

    def create_server(*args, **kwargs):
        server = _create_server(*args, **kwargs)
        waitress_metrics.setup(server.task_dispatcher)
        return server

    waitress.create_server = create_server

    logger.init("WSGI Server", status="Starting")
    url_scheme = "https"
    if args.insecure:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # Disable this on prod
        url_scheme = "http"
        logger.init_warn("WSGI Mode", status="Insecure")

    waitress.serve(
        app,
        host=args.listen,
        port=args.port,
        url_scheme=url_scheme,
        threads=args.waitress_threads,
        connection_limit=args.waitress_connection_limit,
        asyncore_use_poll=True,
    )
    logger.init("WSGI Server", status="Stopped")
