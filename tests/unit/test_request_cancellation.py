# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cancelling a request that vanishes concurrently must not be an error."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from horde.apis.v2.base import commit_request_cancellation
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.flask import db

pytestmark = pytest.mark.unit


def _make_waiting_prompt(user_id: int) -> ImageWaitingPrompt:
    wp = ImageWaitingPrompt(
        worker_ids=[],
        models=[],
        prompt="a cancellation race test",
        user_id=user_id,
        params={"n": 1, "width": 512, "height": 512, "steps": 8},
    )
    db.session.commit()
    return wp


def test_vanished_request_is_treated_as_already_finished(db_session, make_user):
    """A cleanup transaction winning the race still produces a successful cancellation result."""
    wp = _make_waiting_prompt(make_user().id)
    request_id = wp.id

    # Model the production race with two real transactions: the request session
    # holds an ORM instance while the cleanup session deletes its database row.
    with Session(db.engine) as cleanup_session:
        cleanup_session.execute(delete(WaitingPrompt).where(WaitingPrompt.id == request_id))
        cleanup_session.commit()

    wp.n = 0
    assert commit_request_cancellation(wp) is False

    # The helper's False result means the request reached the intended terminal
    # state by disappearing, and its rollback left the request session usable.
    assert db.session.scalar(select(WaitingPrompt.id).where(WaitingPrompt.id == request_id)) is None


def test_normal_cancellation_commits(db_session, make_user):
    """Without a competing deletion, cancellation changes are committed."""
    wp = _make_waiting_prompt(make_user().id)
    request_id = wp.id

    wp.n = 0
    assert commit_request_cancellation(wp) is True

    db.session.expire_all()
    persisted_wp = db.session.get(ImageWaitingPrompt, request_id)
    assert persisted_wp is not None
    assert persisted_wp.n == 0
