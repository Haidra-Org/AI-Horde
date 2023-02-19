from flask import Blueprint
from flask_restx import Api
from horde.argparser import args
from importlib import import_module
from horde.vars import horde_title

from horde.apis.v2 import api as v2

blueprint = Blueprint('apiv2', __name__, url_prefix='/api')
api = Api(blueprint,
    version='2.0', 
    title=f'{horde_title} Horde',
    description=f'The API documentation for the {horde_title} Horde',
    contact_email="mail@dbzer0.com",
    default="v2",
    default_label="Latest Version",
    ordered=True,
)

api.add_namespace(v2)
