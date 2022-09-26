from . import models
models.apply_fields_to_api(None)
from .apiv2 import blueprint as apiv2
from .apiv1 import blueprint as apiv1

