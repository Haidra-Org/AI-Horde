from flask import Blueprint
from flask_restx import Api

from horde.apis.v2 import api as v2
from horde.vars import horde_title, horde_contact_email

blueprint = Blueprint("apiv2", __name__, url_prefix="/api")
api = Api(
    blueprint,
    version="2.0",
    title=f"{horde_title}",
    description=f"The API documentation for the {horde_title}",
    contact_email=horde_contact_email,
    default="v2",
    default_label="Latest Version",
    ordered=True,
)

api.add_namespace(v2)
