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

if __name__ == "__main__":
    # Only setting this for the WSGI logs
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s",
        level=logging.WARNING,
    )
    from waitress import serve

    logger.init("WSGI Server", status="Starting")
    url_scheme = "https"
    if args.insecure:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # Disable this on prod
        url_scheme = "http"
    allowed_host = "stablehorde.net"
    if args.insecure:
        allowed_host = "0.0.0.0"
        logger.init_warn("WSGI Mode", status="Insecure")
    serve(
        HORDE,
        port=args.port,
        url_scheme=url_scheme,
        threads=45,
        connection_limit=1024,
        asyncore_use_poll=True,
    )
    # HORDE.run(debug=True,host="0.0.0.0",port="5001")
    logger.init("WSGI Server", status="Stopped")
