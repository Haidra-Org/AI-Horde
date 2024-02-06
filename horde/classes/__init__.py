from horde.argparser import args
from horde.flask import db, HORDE
from horde.utils import hash_api_key

# Importing for DB creation
from horde.classes.stable.processing_generation import ImageProcessingGeneration #noqa 401
from horde.classes.kobold.processing_generation import TextProcessingGeneration #noqa 401
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt #noqa 401
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt #noqa 401
from horde.classes.stable.interrogation import Interrogation #noqa 401
from horde.classes.stable.interrogation_worker import InterrogationWorker #noqa 401
from horde.classes.base.user import User
from horde.classes.base.team import Team #noqa 401
from horde.classes.stable.worker import ImageWorker #noqa 401
from horde.classes.kobold.worker import TextWorker #noqa 401
from horde.classes.base.settings import HordeSettings
import horde.classes.base.stats #noqa 401
from horde.classes.base.detection import Filter #noqa 401

with HORDE.app_context():
    # from sqlalchemy import select
    # logger.debug(select(ImageWorker.speed))
    # q = ImageWorker.query.filter(ImageWorker.speed > 2000000)
    # logger.debug(q)
    # logger.debug(q.count())
    # import sys
    # sys.exit()
    db.create_all()

    if args.convert_flag == "roles":
        from horde.conversions import convert_user_roles

        convert_user_roles()

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
            concurrency=500,
        )
        anon.create()
    settings = HordeSettings.query.first()
    if not settings:
        settings = HordeSettings()
        db.session.add(settings)
        db.session.commit()
