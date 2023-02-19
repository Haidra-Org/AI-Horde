
from datetime import datetime, timedelta

from horde.logger import logger
from horde.flask import db
from horde.enums import ImageGenState
from sqlalchemy import Enum, func

class TextGenerationStatistic(db.Model):
    __tablename__ = "text_gen_stats"
    id = db.Column(db.Integer, primary_key=True)
    finished = db.Column(db.DateTime(timezone=False), default=datetime.utcnow)
    # Created comes from the procgen
    created = db.Column(db.DateTime(timezone=False), nullable=True)
    model = db.Column(db.String(30), index=True, nullable=False)
    max_length = db.Column(db.Integer, nullable=False)
    max_content_length = db.Column(db.Integer, nullable=False)
    softprompt = db.Column(db.Integer, nullable=True)
    prompt_length = db.Column(db.Integer, nullable=False)
    state = db.Column(Enum(ImageGenState), default=ImageGenState.OK, nullable=False, index=True) 


def record_text_statistic(procgen):
    state = ImageGenState.OK
    # Currently there's no way to record cancelled images, but maybe there will be in the future
    if procgen.cancelled: 
        state = ImageGenState.CANCELLED
    elif procgen.faulted: 
        state = ImageGenState.FAULTED
    statistic = TextGenerationStatistic(
        created=procgen.start_time,
        model=procgen.model,
        max_length=procgen.wp.max_length,
        max_content_length=procgen.wp.max_content_length,
        softprompt=procgen.wp.softprompt,
        prompt_length=len(procgen.wp.prompt),
        state=state,
    )
    db.session.add(statistic)
    db.session.commit()

def compile_textgen_stats_totals():
    count_query = db.session.query(TextGenerationStatistic)
    count_minute = count_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(minutes=1)).count()
    count_hour = count_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(hours=1)).count()
    count_day = count_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=1)).count()
    count_month = count_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=30)).count()
    count_total = count_query.count()
    tokens_query = db.session.query(TextGenerationStatistic.max_length)
    tokens_minute = tokens_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(minutes=1)).scalar()
    tokens_hour = tokens_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(hours=1)).scalar()
    tokens_day = tokens_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=1)).scalar()
    tokens_month = tokens_query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=30)).scalar()
    tokens_total = tokens_query.scalar()
    stats_dict = {
        "minute": {
            "images": count_minute,
            "tokens": tokens_minute,
        },
        "hour": {
            "images": count_hour,
            "tokens": tokens_hour,
        },
        "day": {
            "images": count_day,
            "tokens": tokens_day,
        },
        "month": {
            "images": count_month,
            "tokens": tokens_month,
        },
        "total": {
            "images": count_total,
            "tokens": tokens_total,
        },
    }
    return(stats_dict)

def compile_imagegen_stats_models():
    query = db.session.query(
        TextGenerationStatistic.model, func.count()
    ).group_by(
        TextGenerationStatistic.model
    )
    return {
        "total": {model: count for model, count in query.all()},
        "day": {model: count for model, count in query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=1)).all()},
        "month": {model: count for model, count in query.filter(TextGenerationStatistic.finished >= datetime.utcnow() - timedelta(days=30)).all()},
    }