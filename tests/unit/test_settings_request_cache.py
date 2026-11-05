# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the settings row is read once per request and on every call outside a request."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest

from horde.classes.base import settings as horde_settings
from horde.classes.base.settings import HordeSettings
from horde.flask import db
from tests.unit.conftest import _QueryRecorder

pytestmark = pytest.mark.unit

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]


@pytest.fixture
def settings_row(app: Any, db_session: Any) -> HordeSettings:
    """Return the single settings row with every mode flag off."""
    settings = db.session.query(HordeSettings).first()
    if settings is None:
        settings = HordeSettings()
        db.session.add(settings)
    settings.raid = False
    settings.maintenance = False
    settings.invite_only = False
    db.session.commit()
    return settings


def test_mode_flags_are_read_once_per_request(app: Any, settings_row: HordeSettings, assert_query_count: QueryCounter) -> None:
    """Serve every mode flag of one request from a single read of the settings row."""
    with app.test_request_context("/"), assert_query_count() as queries:
        assert horde_settings.mode_raid() is False
        assert horde_settings.mode_maintenance() is False
        assert horde_settings.mode_invite_only() is False
    assert len(queries.of_kind("SELECT")) == 1


def test_each_request_reads_the_row_itself(app: Any, settings_row: HordeSettings, assert_query_count: QueryCounter) -> None:
    """Do not keep the row beyond the request that read it."""
    with app.test_request_context("/"):
        horde_settings.mode_raid()
    with app.test_request_context("/"), assert_query_count() as queries:
        horde_settings.mode_raid()
    assert len(queries.of_kind("SELECT")) == 1


def test_each_call_outside_a_request_reads_the_row(settings_row: HordeSettings, assert_query_count: QueryCounter) -> None:
    """Read the row on every call when there is no request to keep it on."""
    with assert_query_count() as queries:
        horde_settings.mode_raid()
        horde_settings.mode_maintenance()
    assert len(queries.of_kind("SELECT")) == 2


def test_a_change_within_the_request_is_visible_to_later_reads(app: Any, settings_row: HordeSettings) -> None:
    """Report a flag changed by the current request from the same row."""
    with app.test_request_context("/"):
        assert horde_settings.mode_raid() is False
        horde_settings.get_settings().raid = True
        db.session.commit()
        assert horde_settings.mode_raid() is True
    settings_row.raid = False
    db.session.commit()
