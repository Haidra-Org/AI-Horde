from datetime import datetime

from sqlalchemy import Enum

from horde.enums import ImageGenState
from horde.flask import db


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
        max_context_length=procgen.wp.max_context_length,
        softprompt=procgen.wp.softprompt,
        prompt_length=len(procgen.wp.prompt),
        bridge_agent=procgen.worker.bridge_agent,
        client_agent=procgen.wp.client_agent,
        state=state,
    )
    db.session.add(statistic)
    db.session.commit()


class TextGenerationStatistic(db.Model):
    __tablename__ = "text_gen_stats"
    id = db.Column(db.Integer, primary_key=True)
    finished = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    # Created comes from the procgen
    created = db.Column(db.DateTime(timezone=False), nullable=True)
    model = db.Column(db.String(255), nullable=False, index=True)
    max_length = db.Column(db.Integer, nullable=False)
    max_context_length = db.Column(db.Integer, nullable=False)
    softprompt = db.Column(db.Integer, nullable=True)
    prompt_length = db.Column(db.Integer, nullable=False)
    client_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False, index=True)
    bridge_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False, index=True)
    state = db.Column(Enum(ImageGenState), default=ImageGenState.OK, nullable=False, index=True)


class CompiledTextGensStatsTotals(db.Model):
    __tablename__ = "compiled_text_gen_stats_totals"
    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    minute_requests = db.Column(db.Integer, nullable=False)
    minute_tokens = db.Column(db.Integer, nullable=False)
    hour_requests = db.Column(db.Integer, nullable=False)
    hour_tokens = db.Column(db.Integer, nullable=False)
    day_requests = db.Column(db.Integer, nullable=False)
    day_tokens = db.Column(db.Integer, nullable=False)
    month_requests = db.Column(db.Integer, nullable=False)
    month_tokens = db.Column(db.Integer, nullable=False)
    total_requests = db.Column(db.Integer, nullable=False)
    total_tokens = db.Column(db.BigInteger, nullable=False)


def get_compiled_textgen_stats_totals() -> dict[str, dict[str, int]]:
    """Get the compiled text generation statistics for the minute, hour, day, month, and total periods.

    Returns:
        dict[str, dict[str, int]]: A dictionary with the period as the key and the requests and tokens as the values.
    """
    query = db.session.query(CompiledTextGensStatsTotals).order_by(CompiledTextGensStatsTotals.created.desc()).first()

    periods = ["minute", "hour", "day", "month", "total"]
    stats_dict = {period: {"requests": 0, "tokens": 0} for period in periods}

    if query:
        for period in periods:
            stats_dict[period]["requests"] = getattr(query, f"{period}_requests")
            stats_dict[period]["tokens"] = getattr(query, f"{period}_tokens")

    return stats_dict


class CompiledTextGenStatsModels(db.Model):
    __tablename__ = "compiled_text_gen_stats_models"
    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    model = db.Column(db.String(255), nullable=False, index=True)
    day_requests = db.Column(db.Integer, nullable=False)
    month_requests = db.Column(db.Integer, nullable=False)
    total_requests = db.Column(db.Integer, nullable=False)


def get_compiled_textgen_stats_models() -> dict[str, dict[str, int]]:
    """Get the compiled text generation statistics for the day, month, and total periods for each model.

    Returns:
        dict[str, dict[str, int]]: A dictionary with the model as the key and the requests as the values.
    """

    models: tuple[CompiledTextGenStatsModels] = (
        db.session.query(CompiledTextGenStatsModels).order_by(CompiledTextGenStatsModels.created.desc()).all()
    )

    periods = ["day", "month", "total"]
    stats = {period: {model.model: 0 for model in models} for period in periods}

    for model in models:
        for period in periods:
            stats[period][model.model] = getattr(model, f"{period}_requests")

    return stats
