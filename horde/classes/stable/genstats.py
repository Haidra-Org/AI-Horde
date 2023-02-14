
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
    post_processors = db.Column(db.Integer, nullable=False)
    upscaled = db.Column(db.Boolean, nullable=False)
    face_fixed = db.Column(db.Boolean, nullable=False)
    hires_fix = db.Column(db.Boolean, nullable=False)
    tiling = db.Column(db.Boolean, nullable=False)
    nsfw = db.Column(db.Boolean, nullable=False)
    state = db.Column(Enum(ImageGenState), default=ImageGenState.OK, nullable=False, index=True) 


def record_image_statistic(procgen):
    state = ImageGenState.OK
    if procgen.censored: 
        state = ImageGenState.CENSORED
    # Currently there's no way to record cancelled images, but maybe there will be in the future
    elif procgen.cancelled: 
        state = ImageGenState.CANCELLED
    elif procgen.faulted: 
        state = ImageGenState.FAULTED
    face_fixers = ["GFPGAN", "CodeFormers"]
    upscalers = ["RealESRGAN_x4plus"]
    statistic = ImageGenerationStatistic(
        created=procgen.start_time,
        model=procgen.model,
        width=procgen.wp.width,
        height=procgen.wp.height,
        steps=procgen.wp.params["steps"],
        sampler=procgen.wp.params["sampler_name"],
        prompt_length=len(procgen.wp.prompt),
        negprompt='###' in procgen.wp.prompt,
        post_processors=len(procgen.wp.params.get("post_processing",[])),
        upscaled=any(u in procgen.wp.params.get("post_processing",[]) for u in upscalers),
        face_fixed=any(ff in procgen.wp.params.get("post_processing",[]) for ff in face_fixers),
        hires_fix=procgen.wp.params.get("hires_fix", False),
        tiling=procgen.wp.params.get("tiling", False),
        img2img=procgen.wp.source_image != None,
        nsfw=procgen.wp.nsfw,
        state=state,
    )
    db.session.add(statistic)
    db.session.commit()