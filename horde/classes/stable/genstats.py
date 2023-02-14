
from datetime import datetime, timedelta

from horde.logger import logger
from horde.flask import db
from horde.enums import ImageGenState
from sqlalchemy import Enum, func

class ImageGenerationStatisticPP(db.Model):
    __tablename__ = "image_gen_stats_post_processors"
    id = db.Column(db.Integer, primary_key=True)
    imgstat_id = db.Column(db.Integer, db.ForeignKey("image_gen_stats.id", ondelete="CASCADE"), nullable=False)
    imgstat = db.relationship(f"ImageGenerationStatistic", back_populates="post_processors")
    pp = db.Column(db.String(40), nullable=False)


class ImageGenerationStatistic(db.Model):
    __tablename__ = "image_gen_stats"
    id = db.Column(db.Integer, primary_key=True)
    finished = db.Column(db.DateTime(timezone=False), default=datetime.utcnow)
    # Created comes from the procgen
    created = db.Column(db.DateTime(timezone=False), nullable=True)
    model = db.Column(db.String(30), index=True, nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    steps = db.Column(db.Integer, nullable=False)
    cfg = db.Column(db.Integer, nullable=False)
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
        cfg=procgen.wp.params["cfg_scale"],
        sampler=procgen.wp.params["sampler_name"],
        prompt_length=len(procgen.wp.prompt),
        negprompt='###' in procgen.wp.prompt,
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

def compile_imagegen_stats_totals():
    count_query = db.session.query(ImageGenerationStatistic)
    count_minute = count_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(minutes=1)).count()
    count_hour = count_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(hours=1)).count()
    count_day = count_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=1)).count()
    count_month = count_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=30)).count()
    count_total = count_query.count()
    ps_query = db.session.query(func.sum(ImageGenerationStatistic.width * ImageGenerationStatistic.height * ImageGenerationStatistic.steps))
    ps_minute = ps_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(minutes=1)).scalar()
    ps_hour = ps_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(hours=1)).scalar()
    ps_day = ps_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=1)).scalar()
    ps_month = ps_query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=30)).scalar()
    ps_total = ps_query.scalar()
    stats_dict = {
        "minute": {
            "images": count_minute,
            "ps": ps_minute,
        },
        "hour": {
            "images": count_hour,
            "ps": ps_hour,
        },
        "day": {
            "images": count_day,
            "ps": ps_day,
        },
        "month": {
            "images": count_month,
            "ps": ps_month,
        },
        "total": {
            "images": count_total,
            "ps": ps_total,
        },
    }
    return(stats_dict)

def compile_imagegen_stats_models():
    query = db.session.query(
        ImageGenerationStatistic.model, func.count()
    ).group_by(
        ImageGenerationStatistic.model
    )
    return {
        "total": {model: count for model, count in query.all()},
        "day": {model: count for model, count in query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=1)).all()},
        "month": {model: count for model, count in query.filter(ImageGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=30)).all()},
    }