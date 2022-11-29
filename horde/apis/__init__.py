from horde.argparser import args
from importlib import import_module
from horde.logger import logger

ModelsV2 = import_module(name=f'horde.apis.models.{args.horde}_v2').Models
ParsersV2 = import_module(name=f'horde.apis.models.{args.horde}_v2').Parsers

logger.info([ParsersV2,ModelsV2])
from .apiv2 import blueprint as apiv2
# Kobold still needs APIv1
if args.horde == 'kobold':
    from .apiv1 import blueprint as apiv1

