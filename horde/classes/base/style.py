# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped
from sqlalchemy.sql import expression

from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.utils import ensure_clean, get_db_uuid

json_column_type = JSONB if not SQLITE_MODE else JSON
uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731


style_collection_mapping = Table(
    "style_collection_mapping",
    db.Model.metadata,
    db.Column("style_id", db.ForeignKey("styles.id", ondelete="CASCADE"), primary_key=True),
    db.Column("collection_id", db.ForeignKey("style_collections.id", ondelete="CASCADE"), primary_key=True),
)


class StyleCollection(db.Model):
    __tablename__ = "style_collections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "name",
            name="user_id_name",
        ),
    )
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    style_type = db.Column(db.String(30), nullable=False, index=True)
    info = db.Column(db.String(1000), default="")
    name = db.Column(db.String(100), default="", unique=False, nullable=False, index=True)
    use_count = db.Column(db.Integer, default=0, nullable=False, server_default=expression.literal(0), index=True)
    public = db.Column(db.Boolean, default=False, nullable=False)

    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User", back_populates="style_collections")
    styles: Mapped[list[Style]] = db.relationship(secondary="style_collection_mapping", back_populates="collections")

    def create(self, styles):
        for st in styles:
            self.styles.append(st)
        db.session.add(self)
        db.session.commit()

    # Should be extended by each specific horde
    @logger.catch(reraise=True)
    def get_details(self, details_privilege=0):
        """We display these in the collections list json"""
        ret_dict = {
            "name": self.name,
            "id": self.id,
            "creator": self.user.get_unique_alias(),
            "use_count": self.use_count,
            "public": self.public,
            "type": self.style_type,
        }
        styles_array = []
        for s in self.styles:
            styles_array.append(
                {
                    "name": s.get_unique_name(),
                    "id": str(s.id),
                },
            )
        ret_dict["styles"] = styles_array
        return ret_dict

    def get_model_names(self):
        return [m.model for m in self.models]

    def delete(self):
        db.session.delete(self)
        db.session.commit()


class StyleTag(db.Model):
    __tablename__ = "style_tags"
    id = db.Column(db.Integer, primary_key=True)
    style_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("styles.id", ondelete="CASCADE"),
        nullable=False,
    )
    style = db.relationship("Style", back_populates="tags")
    tag = db.Column(db.String(255), nullable=False, index=True)


class StyleModel(db.Model):
    __tablename__ = "style_models"
    id = db.Column(db.Integer, primary_key=True)
    style_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("styles.id", ondelete="CASCADE"),
        nullable=False,
    )
    style = db.relationship("Style", back_populates="models")
    model = db.Column(db.String(255), nullable=False, index=True)


class StyleExample(db.Model):
    __tablename__ = "style_examples"
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    style_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("styles.id", ondelete="CASCADE"),
        nullable=False,
    )
    style = db.relationship("Style", back_populates="examples")
    url = db.Column(db.Text, nullable=False, index=True)
    primary = db.Column(db.Boolean, default=False, nullable=False)


class Style(db.Model):
    __tablename__ = "styles"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "name",
            name="style_user_id_name",
        ),
    )
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    style_type = db.Column(db.String(30), nullable=False, index=True)
    info = db.Column(db.String(1000), nullable=True)
    showcase = db.Column(db.String(1000), nullable=True)
    name = db.Column(db.String(100), unique=False, nullable=False, index=True)
    public = db.Column(db.Boolean, default=False, nullable=False)
    nsfw = db.Column(db.Boolean, default=False, nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    params = db.Column(MutableDict.as_mutable(json_column_type), default={}, nullable=False)

    use_count = db.Column(db.Integer, default=0, nullable=False, server_default=expression.literal(0), index=True)
    votes = db.Column(db.Integer, default=0, nullable=False, server_default=expression.literal(0), index=True)

    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, onupdate=datetime.utcnow)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User", back_populates="styles")
    sharedkey_id = db.Column(uuid_column_type(), db.ForeignKey("user_sharedkeys.id"), nullable=True)
    sharedkey = db.relationship("UserSharedKey", back_populates="styles")
    collections: Mapped[list[StyleCollection]] = db.relationship(secondary="style_collection_mapping", back_populates="styles")
    models = db.relationship("StyleModel", back_populates="style", cascade="all, delete-orphan")
    tags = db.relationship("StyleTag", back_populates="style", cascade="all, delete-orphan")
    examples = db.relationship("StyleExample", back_populates="style", cascade="all, delete-orphan")

    def create(self):
        db.session.add(self)
        db.session.commit()

    def set_name(self, new_name):
        if self.name == new_name:
            return "OK"
        self.name = ensure_clean(new_name, "style name")
        db.session.commit()
        return "OK"

    def set_info(self, new_info):
        if self.info == new_info:
            return "OK"
        self.info = ensure_clean(new_info, "style info")
        db.session.commit()
        return "OK"

    def delete(self):
        db.session.delete(self)
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
        """We display these in the styles list json"""
        ret_dict = {
            "name": self.name,
            "info": self.info,
            "id": self.id,
            "params": self.params,
            "prompt": self.prompt,
            "tags": self.get_tag_names(),
            "models": self.get_model_names(),
            "examples": self.examples,
            "creator": self.user.get_unique_alias(),
            "use_count": self.use_count,
            "public": self.public,
            "nsfw": self.nsfw,
            "shared_key": self.sharedkey.get_details() if self.sharedkey else None,
        }
        return ret_dict

    def get_model_names(self):
        return [m.model for m in self.models]

    def get_tag_names(self):
        return [t.tag for t in self.tags]

    def parse_tags(self, tags):
        """Parses the tags provided for the style into a set"""
        tags = [ensure_clean(tag[0:100], "style tag") for tag in tags]
        del tags[10:]
        return set(tags)

    def parse_models(self, models):
        """Parses the models provided for the style into a set"""
        models = [ensure_clean(model_name[0:100], "style model") for model_name in models]
        del models[5:]
        return set(models)

    def set_models(self, models):
        models = self.parse_models(models)
        existing_model_names = set(self.get_model_names())
        if existing_model_names == models:
            return
        db.session.query(StyleModel).filter_by(style_id=self.id).delete()
        db.session.flush()
        for model_name in models:
            model = StyleModel(style_id=self.id, model=model_name)
            db.session.add(model)
        db.session.commit()

    def set_tags(self, tags):
        tags = self.parse_tags(tags)
        existing_tags = set(self.get_tag_names())
        if existing_tags == tags:
            return
        db.session.query(StyleTag).filter_by(style_id=self.id).delete()
        db.session.flush()
        for tag_name in tags:
            tag = StyleTag(style_id=self.id, tag=tag_name)
            db.session.add(tag)
        db.session.commit()

    def get_unique_name(self):
        return f"{self.user.get_unique_alias()}::style::{self.name}"
