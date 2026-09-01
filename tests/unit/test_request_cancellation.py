# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cancelling a request that completes at the same instant must not be an error."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm.exc import StaleDataError

from horde.apis.v2.base import commit_request_cancellation
from horde.flask import db


def test_vanished_request_is_reported_not_raised(app, monkeypatch):
    rolled_back = []

    def _commit():
        raise StaleDataError("UPDATE statement on table 'waiting_prompts' expected to update 1 row(s); 0 were matched.")

    with app.app_context():
        monkeypatch.setattr(db.session, "commit", _commit)
        monkeypatch.setattr(db.session, "rollback", lambda: rolled_back.append(True))
        assert commit_request_cancellation(SimpleNamespace(id="wp-1")) is False
    assert rolled_back == [True]


def test_normal_cancellation_commits(app, monkeypatch):
    committed = []
    with app.app_context():
        monkeypatch.setattr(db.session, "commit", lambda: committed.append(True))
        assert commit_request_cancellation(SimpleNamespace(id="wp-1")) is True
    assert committed == [True]
