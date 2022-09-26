from flask import Blueprint
from flask_restx import Api
from .. import args
from importlib import import_module

from .v1.v1 import api as v1
ModelsV2 = import_module(name=f'horde.apis.models.{args.horde}_v2').Models
v2 = import_module(name=f'.{args.horde}', package=f'horde.apis.v2').api

blueprint = Blueprint('apiv1', __name__, url_prefix='/api')
api = Api(blueprint,
    version='1.0', 
    title='Stable Horde',
    description='The API documentation for the Stable Horde',
    contact_email="mail@dbzer0.com",
    default="v1",
    default_label="Obsolete Version",
    ordered=True,
)

api.add_namespace(v2)
api.add_namespace(v1)
