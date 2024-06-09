from pathlib import Path

import horde.classes.base.stats  # noqa 401
from horde.argparser import args
from horde.classes.base.detection import Filter  # noqa 401
from horde.classes.base.settings import HordeSettings
from horde.classes.base.team import Team  # noqa 401
from horde.classes.base.user import User

# noqa 401
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt  # noqa 401
from horde.classes.kobold.worker import TextWorker  # noqa 401
from horde.classes.stable.interrogation import Interrogation  # noqa 401
from horde.classes.stable.interrogation_worker import InterrogationWorker  # noqa 401
from horde.classes.stable.known_image_models import KnownImageModel  # noqa 401

# Importing for DB creation
# noqa 401
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt  # noqa 401
from horde.classes.stable.worker import ImageWorker  # noqa 401
from horde.flask import HORDE, db
from horde.logger import logger
from horde.utils import hash_api_key

with HORDE.app_context():
    # from sqlalchemy import select
    # logger.debug(select(ImageWorker.speed))
    # q = ImageWorker.query.filter(ImageWorker.speed > 2000000)
    # logger.debug(q)
    # logger.debug(q.count())
    # import sys
    # sys.exit()
    db.create_all()

    sql_statement_dir = Path(__file__).parent.parent.parent / "sql_statements"

    # The order of these directories is important. `cron` creates a stored procedure that is
    # used by queries in all other `cron_jobs/` directories.
    all_dirs_to_run = [
        "cron/",  # Must be first
        "stored_procedures/",
        "stored_procedures/cron_jobs/",
    ]

    all_dirs_to_run = [sql_statement_dir / dir for dir in all_dirs_to_run]

    with logger.catch(reraise=True):
        for dir in all_dirs_to_run:
            logger.info(f"Running files in {dir}")
            for file in dir.iterdir():
                if file.suffix == ".sql":
                    logger.info(f"Running {file}")
                    with file.open() as f:
                        db.session.execute(f.read())

        db.session.commit()

    if args.convert_flag == "roles":
        # from horde.conversions import convert_user_roles

        # convert_user_roles()
        raise NotImplementedError("Role conversion not implemented")

    if args.convert_flag == "SQL":
        # from horde.conversions import convert_json_db

        # convert_json_db()
        raise NotImplementedError("SQL conversion not implemented")

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

__all__ = [
    "ImageProcessingGeneration",
    "TextProcessingGeneration",
    "ImageWaitingPrompt",
    "TextWaitingPrompt",
    "Interrogation",
    "InterrogationWorker",
    "KnownImageModel",
    "User",
    "Team",
    "ImageWorker",
    "TextWorker",
    "HordeSettings",
    "Filter",
    "stats",
]
