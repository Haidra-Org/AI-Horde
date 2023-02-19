from horde.logger import logger
from horde.argparser import args
from importlib import import_module
from horde.flask import db, HORDE
from horde.utils import hash_api_key

# Renaming now for backwards compat. Will fix later
from horde.classes.stable.processing_generation import ImageProcessingGeneration as ProcessingGeneration
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt as WaitingPrompt
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
try:
    WPAllowedWorkers = import_module(name=f'horde.classes.{args.horde}.waiting_prompt').WPAllowedWorkers
except (ModuleNotFoundError,AttributeError):
    WPAllowedWorkers = import_module(name='horde.classes.base.waiting_prompt').WPAllowedWorkers
try:
    User = import_module(name=f'horde.classes.{args.horde}.user').UserExtended
except (ModuleNotFoundError,AttributeError):
    User = import_module(name='horde.classes.base.user').User
try:
    Team = import_module(name=f'horde.classes.{args.horde}.team').TeamExtended
except (ModuleNotFoundError,AttributeError):
    Team = import_module(name='horde.classes.base.team').Team
try:
    Worker = import_module(name=f'horde.classes.{args.horde}.worker').WorkerExtended
except (ModuleNotFoundError,AttributeError):
    Worker = import_module(name='horde.classes.base.worker').Worker
try:
    WorkerPerformance = import_module(name=f'horde.classes.{args.horde}.worker').WorkerPerformanceExtended
except (ModuleNotFoundError,AttributeError):
    WorkerPerformance = import_module(name='horde.classes.base.worker').WorkerPerformance
try:
    News = import_module(name=f'horde.classes.{args.horde}.news').NewsExtended
except (ModuleNotFoundError,AttributeError):
    News = import_module(name=f'horde.classes.{args.horde}.news').News
try:
    stats = import_module(name=f'horde.classes.{args.horde}.stats')
except (ModuleNotFoundError,AttributeError):
    stats = import_module(name='horde.classes.base.stats')
from horde.classes.base.detection import Filter

with HORDE.app_context():
    db.create_all()

    if args.convert_flag == "SQL":
        from horde.conversions import convert_json_db
        convert_json_db()

    anon = db.session.query(User).filter_by(oauth_id="anon").first()
    if not anon:
        anon = User(
            id=0,
            username="Anonymous",
            oauth_id="anon",
            api_key=hash_api_key("0000000000"),
            public_workers=True,
            concurrency=500
        )
        anon.create()