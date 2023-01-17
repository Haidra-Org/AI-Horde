from horde.logger import logger
from horde.flask import db


class Filter(db.Model):
    """For storing detection regex"""
    __tablename__ = "filters"
    id = db.Column(db.Integer, primary_key=True)
    regex = db.Column(db.String(255))
    filter_type = db.Column(db.Integer, nullable=False, index=True)

    # def get_details(self):
    #     return {
    #         "id": self.id,
    #         "regex": self.regex
    #         "filter_type": self.filter_type
    #     }
