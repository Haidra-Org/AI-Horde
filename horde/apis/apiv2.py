from flask import Blueprint
from flask_restx import Api
from .. import args
from importlib import import_module
from ..vars import horde_title

v2 = import_module(name=f'.{args.horde}', package='horde.apis.v2').api

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
