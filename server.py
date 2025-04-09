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
from horde.flask import HORDE
from horde.logger import logger
from horde.metrics import waitress_metrics

if __name__ == "__main__":
    # Only setting this for the WSGI logs
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s",
        level=logging.WARNING,
    )
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
        HORDE,
        host=args.listen,
        port=args.port,
        url_scheme=url_scheme,
        threads=45,
        connection_limit=1024,
        asyncore_use_poll=True,
    )
    # HORDE.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
