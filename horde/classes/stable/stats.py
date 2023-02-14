
from datetime import datetime, timedelta

from horde.logger import logger
from horde.flask import db
from horde.enums import ImageGenState
from sqlalchemy import Enum

class ImageGenerationStatistic(db.Model):
    __tablename__ = "image_request_stats"
    id = db.Column(db.Integer, primary_key=True)
    finished = db.Column(db.DateTime(timezone=False), default=datetime.utcnow)
    # Created comes from the procgen
    created = db.Column(db.DateTime(timezone=False), nullable=True)
    model = db.Column(db.String(30), index=True, nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    steps = db.Column(db.Integer, nullable=False)
    sampler = db.Column(db.String(30), nullable=False, index=True)
    prompt_length = db.Column(db.Integer, nullable=False)
    negprompt = db.Column(db.Boolean, nullable=False)
    img2img = db.Column(db.Boolean, nullable=False, index=True)
    post_processed = db.Column(db.Boolean, nullable=False)
    state = db.Column(Enum(ImageGenState), default=State.OK, nullable=False, index=True) 


def record_image_statistic(procgen):
    state = ImageGenState.OK
    if procgen.censored: 
        state = ImageGenState.CENSORED
    # Currently there's no way to record cancelled images, but maybe there will be in the future
    elif procgen.cancelled: 
        state = ImageGenState.CANCELLED
    elif procgen.faulted: 
        state = ImageGenState.FAULTED
    statistic = ImageGenerationStatistic(
        created=procgen.start_time,
        model=procgen.model,
        width=procgen.wp.width,
        height=procgen.wp.height,
        steps=procgen.wp.params["steps"],
        sampler=procgen.wp.params["sampler_name"],
        prompt_length=len(procgen.wp.prompt),
        negprompt='###' in procgen.wp.prompt,
        post_processed="post_processing" in procgen.wp.params and len(procgen.wp.params["post_processing"]) != 0,
        img2img=procgen.wp.height != None,
        state=state,
    )
    db.session.add(statistic)
    db.session.commit()
