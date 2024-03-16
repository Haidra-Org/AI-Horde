import os
import socket

from flask_dance.contrib.discord import make_discord_blueprint
from flask_dance.contrib.github import make_github_blueprint
from flask_dance.contrib.google import make_google_blueprint

import horde.routes  # noqa F401
from horde.apis import apiv2
from horde.argparser import args
from horde.consts import HORDE_VERSION
from horde.flask import HORDE
from horde.logger import logger

HORDE.register_blueprint(apiv2)


@HORDE.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE, PATCH"
    response.headers["Access-Control-Allow-Headers"] = (
        "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, apikey, Client-Agent, X-Fields"
    )
    response.headers["Horde-Node"] = f"{socket.gethostname()}:{args.port}:{HORDE_VERSION}"
    return response


google_client_id = os.getenv("GOOGLE_CLIENT_ID")
google_client_secret = os.getenv("GLOOGLE_CLIENT_SECRET")
discord_client_id = os.getenv("DISCORD_CLIENT_ID")
discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
github_client_id = os.getenv("GITHUB_CLIENT_ID")
github_client_secret = os.getenv("GITHUB_CLIENT_SECRET")
HORDE.secret_key = os.getenv("secret_key")
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
google_blueprint = make_google_blueprint(
    client_id=google_client_id,
    client_secret=google_client_secret,
    reprompt_consent=True,
    redirect_url="/register",
    scope=["email"],
)
HORDE.register_blueprint(google_blueprint, url_prefix="/google")
discord_blueprint = make_discord_blueprint(
    client_id=discord_client_id,
    client_secret=discord_client_secret,
    scope=["identify"],
    redirect_url="/finish_dance",
)
HORDE.register_blueprint(discord_blueprint, url_prefix="/discord")
github_blueprint = make_github_blueprint(
    client_id=github_client_id,
    client_secret=github_client_secret,
    scope=["identify"],
    redirect_url="/finish_dance",
)
HORDE.register_blueprint(github_blueprint, url_prefix="/github")
# patreon_blueprint = make_patreon_blueprint(
#     client_id=patreon_client_id,
#     client_secret=patreon_client_secret,
#     scope=["identify"],
#     redirect_url='/finish_dance',
# )
# HORDE.register_blueprint(patreon_blueprint, url_prefix="/patreon")


if args.force_patreon:
    from horde.ops import force_patreon_kudos

    logger.info(f"focing kudos on user_id: {args.force_patreon}")
    force_patreon_kudos(args.force_patreon, args.prevent_date_change)
    import sys

    sys.exit()


if args.test:
    from horde.sandbox import test

    test()
