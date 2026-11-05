# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timedelta

from sqlalchemy import func

from horde import vars as hv
from horde.flask import db
from horde.logger import logger


class ModelPerformance(db.Model):
    __tablename__ = "model_performances"
    id = db.Column(db.Integer, primary_key=True)
    model = db.Column(db.String(255), index=True)
    performance = db.Column(db.Float)
    created = db.Column(
        db.DateTime(timezone=False),
        default=datetime.utcnow,
    )  # Maybe index this, but I'm not actually sure how big this table is


class FulfillmentPerformance(db.Model):
    __tablename__ = "horde_fulfillments"
    id = db.Column(db.Integer, primary_key=True)
    deliver_time = db.Column(db.DateTime(timezone=False), default=datetime.utcnow)
    things = db.Column(db.Float)
    thing_type = db.Column(db.String(20), nullable=False, index=True)
    created = db.Column(db.DateTime, default=datetime.utcnow)


def calculate_things_per_sec(*, things: float, start_time: datetime) -> float:
    """Return the speed of a submitted generation, from its things and the time since its job was assigned.

    This only reads the clock. Settlement rewards the worker on this speed, so a submit measures it before the
    completion claim, and its samples are stored only once the claim succeeded.

    Args:
        things: The things the submitted generation delivered.
        start_time: When the job was assigned to the worker.

    Returns:
        The things per second rounded to one decimal, or 1 when no time has passed.
    """
    seconds_taken = (datetime.utcnow() - start_time).total_seconds()
    if seconds_taken == 0:
        return 1
    things_per_sec = round(things / seconds_taken, 1)
    logger.debug(things_per_sec)
    return things_per_sec


def build_fulfilment_records(
    *,
    model: str,
    thing_type: str,
    things: float,
    things_per_sec: float,
) -> list[ModelPerformance | FulfillmentPerformance]:
    """Create the model performance and fulfilment samples of one stored generation, without adding them to the session.

    The model performance sample feeds the models list averages (``get_model_avgs``) and the fulfilment sample feeds
    the throughput of the past minute (``get_things_per_min``).

    Args:
        model: The model that produced the generation.
        thing_type: The generation type, ``image`` or ``text``.
        things: The things the generation delivered.
        things_per_sec: The speed from [`calculate_things_per_sec`][horde.classes.base.stats.calculate_things_per_sec].

    Returns:
        The two new records.
    """
    return [
        ModelPerformance(model=model, performance=things_per_sec),
        FulfillmentPerformance(things=things, thing_type=thing_type),
    ]


def get_things_per_min(thing_type="image"):
    total_things = 0
    last_minute_fulfillments = (
        db.session.query(FulfillmentPerformance)
        .filter(
            FulfillmentPerformance.created >= datetime.utcnow() - timedelta(seconds=60),
            FulfillmentPerformance.thing_type == thing_type,
        )
        .all()
    )
    for fulfillment in last_minute_fulfillments:
        total_things += fulfillment.things
    things_per_min = round(total_things / hv.thing_divisors[thing_type], 2)
    return things_per_min


def get_model_avgs() -> dict[str, float]:
    """Return the average recorded performance of every model, keyed by model name, rounded to one decimal.

    One grouped query for the whole table; models with no recorded performance are absent from the result.
    ``get_available_models`` used to ask ``get_model_avg`` per model, which was two round-trips per model.
    """
    model_avg_rows = db.session.query(ModelPerformance.model, func.avg(ModelPerformance.performance)).group_by(ModelPerformance.model).all()
    return {model_name: round(model_avg, 1) for model_name, model_avg in model_avg_rows}


def get_model_avg(model_name):
    model_performances_count = db.session.query(ModelPerformance).filter_by(model=model_name).count()
    if model_performances_count == 0:
        return 0
    avg = db.session.query(func.avg(ModelPerformance.performance)).filter_by(model=model_name).scalar()
    return round(avg, 1)
