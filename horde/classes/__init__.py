from horde.logger import logger
from horde.argparser import args
from importlib import import_module
from horde.flask import db, HORDE
from horde.utils import hash_api_key

# Should figure out an elegant way to do this with a for loop
# stats = import_module(name=f'horde.classes.{args.horde}').stats
try:
    ProcessingGeneration = import_module(name=f'horde.classes.{args.horde}.processing_generation').ProcessingGenerationExtended
except (ModuleNotFoundError,AttributeError):
    ProcessingGeneration = import_module(name='horde.classes.base.processing_generation').ProcessingGeneration
try:
    WaitingPrompt = import_module(name=f'horde.classes.{args.horde}.waiting_prompt').WaitingPromptExtended
except (ModuleNotFoundError,AttributeError):
    WaitingPrompt = import_module(name='horde.classes.base.waiting_prompt').WaitingPrompt
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
    WPCleaner = import_module(name=f'horde.classes.{args.horde}.threads').WPCleanerExtended
except (ModuleNotFoundError,AttributeError):
    WPCleaner = import_module(name='horde.classes.base.threads').WPCleaner
try:
    MonthlyKudos = import_module(name=f'horde.classes.{args.horde}.threads').MonthlyKudosExtended
except (ModuleNotFoundError,AttributeError):
    MonthlyKudos = import_module(name='horde.classes.base.threads').MonthlyKudos
try:
    stats = import_module(name=f'horde.classes.{args.horde}.stats')
except (ModuleNotFoundError,AttributeError):
    stats = import_module(name='horde.classes.base.stats')

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

if args.primary:
    wp_cleaner = WPCleaner()
    monthly_kudos = MonthlyKudos()