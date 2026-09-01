# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wiping accounts must work for more than one account, and wiped accounts stay invisible to lookups."""

from __future__ import annotations

from horde.classes.base.user import User
from horde.database.functions import find_user_by_id


def test_two_accounts_can_be_wiped(db_session, make_user):
    first = make_user()
    second = make_user()

    first.wipe()
    second.wipe()

    assert first.is_wiped and second.is_wiped
    assert first.oauth_id != second.oauth_id
    assert find_user_by_id(first.id) is None
    assert find_user_by_id(second.id) is None


def test_legacy_wiped_marker_is_still_recognised(db_session, make_user):
    legacy = make_user(oauth_id="<wiped>")

    assert legacy.is_wiped
    assert db_session.query(User).filter(User.is_wiped).filter(User.id == legacy.id).count() == 1
    assert find_user_by_id(legacy.id) is None
