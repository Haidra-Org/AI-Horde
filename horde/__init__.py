import os
from uuid import uuid4

horde_instance_id = str(uuid4())

from flask_dance.contrib.discord import make_discord_blueprint
from flask_dance.contrib.github import make_github_blueprint
from flask_dance.contrib.google import make_google_blueprint

from horde.routes import *  # I don't like this, we should be refactoring what things are being loaded
from horde.apis import apiv1, apiv2
from horde.argparser import args, invite_only, raid, maintenance
from horde.flask import HORDE, cache
from horde.logger import logger

from horde.limiter import limiter

HORDE.register_blueprint(apiv2)
if args.horde == 'kobold':
    HORDE.register_blueprint(apiv1)


@HORDE.after_request
def after_request(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type, Content-Length, Accept-Encoding, X-CSRF-Token, apikey, Client-Agent"
    return response


google_client_id = os.getenv("GOOGLE_CLIENT_ID")
google_client_secret = os.getenv("GLOOGLE_CLIENT_SECRET")
discord_client_id = os.getenv("DISCORD_CLIENT_ID")
discord_client_secret = os.getenv("DISCORD_CLIENT_SECRET")
github_client_id = os.getenv("GITHUB_CLIENT_ID")
github_client_secret = os.getenv("GITHUB_CLIENT_SECRET")
HORDE.secret_key = os.getenv("secret_key")
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
google_blueprint = make_google_blueprint(
    client_id=google_client_id,
    client_secret=google_client_secret,
    reprompt_consent=True,
    redirect_url='/register',
    scope=["email"],
)
HORDE.register_blueprint(google_blueprint, url_prefix="/google")
discord_blueprint = make_discord_blueprint(
    client_id=discord_client_id,
    client_secret=discord_client_secret,
    scope=["identify"],
    redirect_url='/finish_dance',
)
HORDE.register_blueprint(discord_blueprint, url_prefix="/discord")
github_blueprint = make_github_blueprint(
    client_id=github_client_id,
    client_secret=github_client_secret,
    scope=["identify"],
    redirect_url='/finish_dance',
)
HORDE.register_blueprint(github_blueprint, url_prefix="/github")
# patreon_blueprint = make_patreon_blueprint(
#     client_id=patreon_client_id,
#     client_secret=patreon_client_secret,
#     scope=["identify"],
#     redirect_url='/finish_dance',
# )
# HORDE.register_blueprint(patreon_blueprint, url_prefix="/patreon")
