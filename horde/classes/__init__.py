from .. import logger, args
from importlib import import_module
from horde.flask import db

# Should figure out an elegant way to do this with a for loop
stats = import_module(name=f'horde.classes.{args.horde}').stats
WaitingPrompt = import_module(name=f'horde.classes.{args.horde}').WaitingPrompt
ProcessingGeneration = import_module(name=f'horde.classes.{args.horde}').ProcessingGeneration
User = import_module(name=f'horde.classes.{args.horde}').User
Team = import_module(name=f'horde.classes.{args.horde}').Team
Worker = import_module(name=f'horde.classes.{args.horde}').WorkerExtended
PromptsIndex = import_module(name=f'horde.classes.{args.horde}').PromptsIndex
GenerationsIndex = import_module(name=f'horde.classes.{args.horde}').GenerationsIndex
News = import_module(name=f'horde.classes.{args.horde}').News
MonthlyKudos = import_module(name=f'horde.classes.{args.horde}').MonthlyKudos

logger.debug(Team)
db.create_all()

database = import_module(name=f'horde.classes.{args.horde}').database
monthly_kudos = MonthlyKudos()

anon = db.session.query(User).filter_by(oauth_id="anon").first()
if not anon:
    anon = User(
        id=0,
        username="Anonymous",
        oauth_id="anon",
        api_key="0000000000",
        public_workers=True,
        concurrency=500
    )
    anon.create()


# from .base import WaitingPrompt,ProcessingGeneration,Worker,PromptsIndex,GenerationsIndex,User,Database

waiting_prompts = PromptsIndex()
processing_generations = GenerationsIndex()
