from flask import Blueprint
from flask_restx import Api
from .. import args
from importlib import import_module

v1 = import_module(name=f'.{args.horde}_v1', package=f'horde.apis.v1').api
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
