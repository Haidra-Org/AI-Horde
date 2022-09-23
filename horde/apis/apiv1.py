from flask import Blueprint
from flask_restx import Api

from .v1 import api as v1
from .v2 import api as v2

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

api.add_namespace(v1)
api.add_namespace(v2)
