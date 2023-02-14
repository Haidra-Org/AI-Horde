
from datetime import datetime, timedelta

from horde.logger import logger
from horde.flask import db
from horde.enums import ImageGenState
from sqlalchemy import Enum

class ImageGenerationStatisticPP(db.Model):
    __tablename__ = "image_request_post_processors"
    id = db.Column(db.Integer, primary_key=True)
    imgstat_id = db.Column(db.Integer, db.ForeignKey("image_request_stats.id", ondelete="CASCADE"), nullable=False)
    imgstat = db.relationship(f"ImageGenerationStatistic", back_populates="models")
    pp = db.Column(db.String(40), nullable=False)


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
    hires_fix = db.Column(db.Boolean, nullable=False)
    tiling = db.Column(db.Boolean, nullable=False)
    nsfw = db.Column(db.Boolean, nullable=False)
    state = db.Column(Enum(ImageGenState), default=ImageGenState.OK, nullable=False, index=True) 
    post_processors = db.relationship("ImageGenerationStatisticPP", back_populates="imgstat", cascade="all, delete-orphan")


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
        post_processors=len(pp),
        upscaled=any(u in pp for u in upscalers),
        face_fixed=any(ff in pp for ff in face_fixers),
        hires_fix=procgen.wp.params.get("hires_fix", False),
        tiling=procgen.wp.params.get("tiling", False),
        img2img=procgen.wp.source_image != None,
        nsfw=procgen.wp.nsfw,
        state=state,
    )
    db.session.add(statistic)
    db.session.commit()
    # face_fixers = ["GFPGAN", "CodeFormers"]
    # upscalers = ["RealESRGAN_x4plus"]
    post_processors = procgen.wp.params.get("post_processing",[])
    if len(post_processors) > 0:
        for pp in post_processors:
            new_pp_entry = ImageGenerationStatisticPP(imgstat_id=statistic.id,pp=pp)
            db.session.add(new_pp_entry)
        db.session.commit()
