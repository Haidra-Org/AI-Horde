from flask import Blueprint
from flask_restx import Api
from .. import args
from importlib import import_module

v1 = import_module(name=f'.{args.horde}_v1', package=f'horde.apis.v1').api
v2 = import_module(name=f'.{args.horde}', package=f'horde.apis.v2').api

blueprint = Blueprint('apiv2', __name__, url_prefix='/api')
api = Api(blueprint,
    version='2.0', 
    title='Stable Horde',
    description='The API documentation for the Stable Horde',
    contact_email="mail@dbzer0.com",
    default="v2",
    default_label="Latest Version",
    ordered=True,
)

api.add_namespace(v2)
api.add_namespace(v1)
