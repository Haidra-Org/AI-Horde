# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for ``WaitingPrompt.activate``'s deadlock/telemetry safety.

Activating a WP can lose a PostgreSQL deadlock race (SQLSTATE 40P01). When that
happens the SQLAlchemy session is left in a ``PendingRollbackError`` state, so
reading an *expired* ORM attribute triggers a lazy reload that itself raises.
The telemetry ``finally`` in ``activate`` must therefore never touch the session
after a failed activation -- otherwise it masks the real ``OperationalError``
with a confusing ``PendingRollbackError`` (the production symptom this guards).

We bind the real unbound method to a lightweight stub rather than building a
persisted ORM graph (``WaitingPrompt.__init__`` commits to the DB), mirroring
``test_kobold_text_wp.py``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError, PendingRollbackError

from horde.classes.base import waiting_prompt as wp_module
from horde.classes.base.waiting_prompt import WaitingPrompt


class _StubWP:
    """Stub whose ``wp_type`` raises on any access after the first.

    The first read models the healthy up-front snapshot; a second read models
    the broken-session lazy reload that a regressed ``finally`` would trigger.
    """

    def __init__(self, activate_impl) -> None:
        self.id = "8339d75e-301d-487a-8f13-2e81217dc8f3"
        self.created = datetime.utcnow()
        self.wp_type_accesses = 0
        self.activate_calls = 0
        self._activate_impl = activate_impl

    @property
    def wp_type(self) -> str:
        self.wp_type_accesses += 1
        if self.wp_type_accesses > 1:
            raise PendingRollbackError("This Session's transaction has been rolled back")
        return "image"

    def _activate(self, *args, **kwargs) -> None:
        self.activate_calls += 1
        self._activate_impl()


@pytest.fixture
def _patched_metrics(monkeypatch):
    duration = MagicMock()
    age = MagicMock()
    monkeypatch.setattr(wp_module, "wp_activate_duration", duration)
    monkeypatch.setattr(wp_module, "wp_activation_age", age)
    return duration, age


def test_activate_success_records_telemetry(_patched_metrics):
    duration, age = _patched_metrics
    stub = _StubWP(lambda: None)

    WaitingPrompt.activate(stub)

    assert stub.activate_calls == 1
    assert stub.wp_type_accesses == 1
    duration.record.assert_called_once()
    assert duration.record.call_args.args[1] == {"horde.wp_type": "image"}
    age.record.assert_called_once()


def test_activate_retries_a_deadlock_with_a_clean_session(_patched_metrics, monkeypatch):
    class _Deadlock(Exception):
        pgcode = "40P01"

    attempts = iter([OperationalError("activate", {}, _Deadlock()), None])

    def activate_impl() -> None:
        result = next(attempts)
        if result is not None:
            raise result

    rollback = MagicMock()
    monkeypatch.setattr(wp_module.db.session, "rollback", rollback)
    monkeypatch.setattr(wp_module.time, "sleep", MagicMock())
    stub = _StubWP(activate_impl)

    WaitingPrompt.activate(stub)

    assert stub.activate_calls == 2
    rollback.assert_called_once_with()
