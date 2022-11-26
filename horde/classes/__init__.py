from .. import logger, args
from importlib import import_module
from horde.flask import db

# To avoid imports on uninitialized vars
stats = None
ProcessingGeneration = None
WaitingPrompt = None
User = None
Team = None
Worker = None
WorkerPerformance = None
News = None
WPCleaner = None
MonthlyKudos = None

from horde.classes import database
# Should figure out an elegant way to do this with a for loop
stats = import_module(name=f'horde.classes.{args.horde}').stats
ProcessingGeneration = import_module(name=f'horde.classes.{args.horde}').ProcessingGeneration
WaitingPrompt = import_module(name=f'horde.classes.{args.horde}').WaitingPrompt
User = import_module(name=f'horde.classes.{args.horde}').User
Team = import_module(name=f'horde.classes.{args.horde}').Team
Worker = import_module(name=f'horde.classes.{args.horde}').Worker
WorkerPerformance = import_module(name=f'horde.classes.{args.horde}').WorkerPerformance
News = import_module(name=f'horde.classes.{args.horde}').News
WPCleaner = import_module(name=f'horde.classes.{args.horde}').WPCleaner
MonthlyKudos = import_module(name=f'horde.classes.{args.horde}').MonthlyKudos

logger.debug(Team)
db.create_all()

wp_cleaner = WPCleaner()
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
