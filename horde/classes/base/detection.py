from horde.logger import logger
from horde.flask import db


class Filter(db.Model):
    """For storing detection regex"""
    __tablename__ = "filters"
    id = db.Column(db.Integer, primary_key=True)
    regex = db.Column(db.Text)
    filter_type = db.Column(db.Integer, nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="filters")

    def get_details(self):
        return {
            "id": self.id,
            "regex": self.regex,
            "filter_type": self.filter_type,
            "description": self.description,
            "user": self.user.get_unique_alias(),
        }
