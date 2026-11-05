# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The single horde settings row and its mode flags."""

from flask import g, has_request_context, request

from horde.flask import db

_REQUEST_SETTINGS_ATTRIBUTE = "horde_settings_row"
"""The application-context attribute under which the settings row loaded for the current request is kept.

A request reads several mode flags in different handlers, each as its own query. One read per request is enough:
the flags change through an administrative request, and that request's own reads see the change on the same row.
"""

_REQUEST_SETTINGS_OWNER_ATTRIBUTE = "horde_settings_request"
"""The application-context attribute holding the request the kept settings row was read for.

``g`` belongs to the application context, which can outlive a request when a context is pushed around several
of them. The row is reused only for the request it was read for.
"""


class HordeSettings(db.Model):
    """For storing application-wide settings."""

    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    raid = db.Column(db.Boolean, default=False, nullable=False)
    invite_only = db.Column(db.Boolean, default=False, nullable=False)
    maintenance = db.Column(db.Boolean, default=False, nullable=False)


def get_settings() -> HordeSettings | None:
    """Return the settings row, read once per request and on every call outside a request.

    The row is kept on the request context, not the session, so the nested application context used by
    some handlers does not see it and reads the row itself.
    """
    if not has_request_context():
        return db.session.query(HordeSettings).first()
    current_request = request._get_current_object()
    settings_row = getattr(g, _REQUEST_SETTINGS_ATTRIBUTE, None)
    read_for_this_request = getattr(g, _REQUEST_SETTINGS_OWNER_ATTRIBUTE, None) is current_request
    if settings_row is None or not read_for_this_request:
        settings_row = db.session.query(HordeSettings).first()
        setattr(g, _REQUEST_SETTINGS_ATTRIBUTE, settings_row)
        setattr(g, _REQUEST_SETTINGS_OWNER_ATTRIBUTE, current_request)
    return settings_row


def mode_raid() -> bool:
    """Return whether raid mode is on."""
    return get_settings().raid


def mode_maintenance() -> bool:
    """Return whether maintenance mode is on."""
    return get_settings().maintenance


def mode_invite_only() -> bool:
    """Return whether invite-only mode is on."""
    return get_settings().invite_only
