from horde.logger import logger
from horde.flask import db


class Filter(db.Model):
    """For storing detection regex"""
    __tablename__ = "filters"
    id = db.Column(db.Integer, primary_key=True)
    regex = db.Column(db.Text)
    filter_type = db.Column(db.Integer, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
