from datetime import datetime, timedelta

from horde import logger
from horde.flask import db
from horde.vars import thing_divisor, raw_thing_name

from horde.classes import WorkerPerformance

class ModelPerformance(db.Model):
    __tablename__ = "model_performances"
    id = db.Column(db.Integer, primary_key=True)
    model = db.Column(db.String(30), db.ForeignKey("workers.id"))
    performance = db.Column(db.Float, primary_key=False)

class FulfillmentPerformance(db.Model):
    __tablename__ = "horde_fulfillments"
    id = db.Column(db.Integer, primary_key=True)
    deliver_time = db.Column(db.DateTime, default=datetime.utcnow)
    things = db.Column(db.Float, primary_key=False)


def record_fulfilment(things, starting_time, model):
    seconds_taken = (datetime.utcnow() - starting_time).seconds
    if seconds_taken == 0:
        things_per_sec = 1
    else:
        things_per_sec = round(things / seconds_taken,1)
    worker_performances = get_worker_performances()
    model_performances = db.session.query(ModelPerformance).filter_by(model=model).asc()
    if model_performances.count() >= 20:
        model_performances.first().delete()
    new_performance = ModelPerformance(model=model,performance=things_per_sec)
    new_fulfillment = FulfillmentPerformance(things=things)
    db.session.add(new_performance)
    db.session.commit()
    return(things_per_sec)

def get_things_per_min():
    total_things = 0
    pruned_array = []
    # clear up old requests (older than 5 mins)
    db.session.query(FulfillmentPerformance).filter(
       datetime.utcnow() - FulfillmentPerformance.model.created > timedelta(seconds=60)
    ).delete(synchronize_session=False)
    db.session.commit()
    logger.debug("Pruned fulfillments")
    last_minute_fulfillments = db.session.query(FulfillmentPerformance).filter(
       datetime.utcnow() - FulfillmentPerformance.model.created <= timedelta(seconds=60)
    )
    for fulfillment in last_minute_fulfillments:
        total_things += fulfillment.things
    things_per_min = round(total_things / thing_divisor,2)
    return(things_per_min)

def get_worker_performances():
    return [p.performance for p in db.session.query(WorkerPerformance).all()]

def get_request_avg():
    worker_performances = get_worker_performances()
    if len(worker_performances) == 0:
        return(0)
    avg = sum(worker_performances) / len(worker_performances)
    return(round(avg,1))

def get_model_performance(model_name):
    return db.session.query(ModelPerformance).filter(model=model_name).desc().limit(10)

def get_model_avg(model):
    model_performances = get_model_performance(model)
    if len(model_performances) == 0:
        return(0)
    avg = sum([m.performance for m in model_performances]) / len(model_performances)
    return(round(avg,1))

    # TODO: Migrate to DB
    # @logger.catch(reraise=True)
    # def serialize(self):
    #     serialized_fulfillments = []
    #     for fulfillment in self.fulfillments.copy():
    #         json_fulfillment = {
    #             raw_thing_name: fulfillment[raw_thing_name],
    #             "start_time": fulfillment["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
    #             "deliver_time": fulfillment["deliver_time"].strftime("%Y-%m-%d %H:%M:%S"),
    #         }
    #         serialized_fulfillments.append(json_fulfillment)
    #     ret_dict = {
    #         "worker_performances": self.worker_performances,
    #         "model_performances": self.model_performances,
    #         "fulfillments": serialized_fulfillments,
    #     }
    #     return(ret_dict)
