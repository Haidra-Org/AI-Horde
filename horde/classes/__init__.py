from .. import logger, args
from importlib import import_module
from horde.classes.base import Suspicions
from horde.flask import db

Database = import_module(name=f'horde.classes.{args.horde}').Database
database = Database(convert_flag=args.convert_flag)

# Should figure out an elegant way to do this with a for loop
WaitingPrompt = import_module(name=f'horde.classes.{args.horde}').WaitingPrompt
ProcessingGeneration = import_module(name=f'horde.classes.{args.horde}').ProcessingGeneration
Worker = import_module(name=f'horde.classes.{args.horde}').Worker
PromptsIndex = import_module(name=f'horde.classes.{args.horde}').PromptsIndex
GenerationsIndex = import_module(name=f'horde.classes.{args.horde}').GenerationsIndex
User = import_module(name=f'horde.classes.{args.horde}').User
Team = import_module(name=f'horde.classes.{args.horde}').Team
News = import_module(name=f'horde.classes.{args.horde}').News


db.create_all()

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


database.load()
# from .base import WaitingPrompt,ProcessingGeneration,Worker,PromptsIndex,GenerationsIndex,User,Database

waiting_prompts = PromptsIndex()
processing_generations = GenerationsIndex()
