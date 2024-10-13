# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime

from sqlalchemy import JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.sql import expression

from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.utils import get_db_uuid, is_profane, sanitize_string

json_column_type = JSONB if not SQLITE_MODE else JSON
uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731


class StyleCollectionMapping(db.Model):
    __tablename__ = "style_collection_mapping"
    id = db.Column(db.Integer, primary_key=True)
    style_id = db.Column(uuid_column_type(), db.ForeignKey("styles.id", ondelete="CASCADE"), nullable=False)
    collection_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("style_collections.id", ondelete="CASCADE"),
        nullable=False,
    )


class StyleCollection(db.Model):
    __tablename__ = "style_collections"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "name",
            name="collection_user_id_name",
        ),
    )
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    style_type = db.Column(db.String(30), nullable=False, index=True)
    info = db.Column(db.String(1000), default="")
    name = db.Column(db.String(100), default="", unique=False, nullable=False, index=True)
    uses = db.Column(db.Integer, default=0, nullable=False, server_default=expression.literal(0), index=True)
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    owner = db.relationship("User", back_populates="style_collections")
    styles = db.relationship("StyleCollectionMapping", secondary="style_collection_mapping")


class Style(db.Model):
    __tablename__ = "styles"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "name",
            name="style_user_id_name",
        ),
    )
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    style_type = db.Column(db.String(30), nullable=False, index=True)
    info = db.Column(db.String(1000), default="")
    name = db.Column(db.String(100), default="", unique=False, nullable=False, index=True)
    uses = db.Column(db.Integer, default=0, nullable=False, server_default=expression.literal(0), index=True)

    prompt = db.Column(db.Text, nullable=False)
    params = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)

    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    owner = db.relationship("User", back_populates="styles")
    collections = db.relationship("StyleCollectionMapping", secondary="style_collection_mapping")

    def create(self):
        db.session.add(self)
        db.session.commit()

    def set_name(self, new_name):
        if self.name == new_name:
            return "OK"
        if is_profane(new_name):
            return "Profanity"
        self.name = sanitize_string(new_name)
        db.session.commit()
        return "OK"

    def set_info(self, new_info):
        if self.info == new_info:
            return "OK"
        if is_profane(new_info):
            return "Profanity"
        self.info = sanitize_string(new_info)
        db.session.commit()
        return "OK"

    def delete(self):
        db.session.delete(self)
        for worker in self.workers:
            worker.set_team(None)
        db.session.commit()

    def record_usage(self):
        self.uses += 1
        db.session.commit()

    def record_contribution(self, contributions, kudos):
        self.contributions = round(self.contributions + contributions, 2)
        self.fulfilments += 1
        self.kudos = round(self.kudos + kudos, 2)
        self.last_active = datetime.utcnow()
        db.session.commit()

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege=0):
        """We display these in the workers list json"""
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "creator": self.owner.get_unique_alias(),
            "uses": self.uses,
        }
        return ret_dict
