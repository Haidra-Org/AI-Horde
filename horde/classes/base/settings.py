# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from horde.flask import db


class HordeSettings(db.Model):
    """For storing settings"""

    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    raid = db.Column(db.Boolean, default=False, nullable=False)
    invite_only = db.Column(db.Boolean, default=False, nullable=False)
    maintenance = db.Column(db.Boolean, default=False, nullable=False)


def get_settings():
    return db.session.query(HordeSettings).first()


def mode_raid():
    query = db.session.query(HordeSettings.raid).first()
    return query.raid


def mode_maintenance():
    query = db.session.query(HordeSettings.maintenance).first()
    return query.maintenance


def mode_invite_only():
    query = db.session.query(HordeSettings.invite_only).first()
    return query.invite_only
