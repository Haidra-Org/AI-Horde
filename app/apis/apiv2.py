from flask import Blueprint
from flask_restx import Api

from .v1 import api as v1
from .v2 import api as v2

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

api.add_namespace(v1)
api.add_namespace(v2)
