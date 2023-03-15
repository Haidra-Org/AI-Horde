from horde.logger import logger
from horde.argparser import args
from importlib import import_module
from horde.flask import db, HORDE
from horde.utils import hash_api_key

# Importing for DB creation
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.stable.interrogation import Interrogation
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.classes.base.user import User
from horde.classes.base.team import Team
from horde.classes.stable.worker import ImageWorker
from horde.classes.kobold.worker import TextWorker
from horde.classes.base.settings import HordeSettings
import horde.classes.base.stats
from horde.classes.base.detection import Filter

with HORDE.app_context():

    from sqlalchemy import select
    logger.debug(select(ImageWorker.speed))

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
    settings = HordeSettings.query.first()
    if not settings:
        settings = HordeSettings()
        db.session.add(settings)
        db.session.commit()