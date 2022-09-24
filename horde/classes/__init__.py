from .. import logger, args
from importlib import import_module

thing_names = {
    "stable": "megapixelsteps",
    "kobold": "kilotokens",
}
raw_thing_names = {
    "stable": "pixelsteps",
    "kobold": "tokens",
}

thing_name = thing_names[args.horde]
raw_thing_name = raw_thing_names[args.horde]

# Should figure out an elegant way to do this with a for loop
WaitingPrompt = import_module(name=f'horde.classes.{args.horde}').WaitingPrompt
ProcessingGeneration = import_module(name=f'horde.classes.{args.horde}').ProcessingGeneration
Worker = import_module(name=f'horde.classes.{args.horde}').Worker
PromptsIndex = import_module(name=f'horde.classes.{args.horde}').PromptsIndex
GenerationsIndex = import_module(name=f'horde.classes.{args.horde}').GenerationsIndex
User = import_module(name=f'horde.classes.{args.horde}').User
Database = import_module(name=f'horde.classes.{args.horde}').Database

# from .base import WaitingPrompt,ProcessingGeneration,Worker,PromptsIndex,GenerationsIndex,User,Database

db = Database(convert_flag=args.convert_flag)
waiting_prompts = PromptsIndex()
processing_generations = GenerationsIndex()
