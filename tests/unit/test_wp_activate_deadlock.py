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


def _deadlock_error() -> OperationalError:
    orig = Exception("deadlock detected")
    orig.pgcode = "40P01"  # type: ignore[attr-defined]
    return OperationalError("UPDATE waiting_prompts ...", {}, orig)


class _StubWP:
    """Stub whose ``wp_type`` raises on any access after the first.

    The first read models the healthy up-front snapshot; a second read models
    the broken-session lazy reload that a regressed ``finally`` would trigger.
    """

    def __init__(self, retry_impl) -> None:
        self.id = "8339d75e-301d-487a-8f13-2e81217dc8f3"
        self.created = datetime.utcnow()
        self.wp_type_accesses = 0
        self.retry_calls = 0
        self._retry_impl = retry_impl

    @property
    def wp_type(self) -> str:
        self.wp_type_accesses += 1
        if self.wp_type_accesses > 1:
            raise PendingRollbackError("This Session's transaction has been rolled back")
        return "image"

    def _activate_with_deadlock_retry(self, *args, **kwargs) -> None:
        self.retry_calls += 1
        self._retry_impl()


@pytest.fixture
def _patched_metrics(monkeypatch):
    duration = MagicMock()
    age = MagicMock()
    monkeypatch.setattr(wp_module, "wp_activate_duration", duration)
    monkeypatch.setattr(wp_module, "wp_activation_age", age)
    return duration, age


def test_activate_failure_propagates_deadlock_without_masking(_patched_metrics):
    duration, age = _patched_metrics
    err = _deadlock_error()

    def _raise():
        raise err

    stub = _StubWP(_raise)

    with pytest.raises(OperationalError) as excinfo:
        WaitingPrompt.activate(stub)

    # The real deadlock error propagates -- not a PendingRollbackError from the
    # telemetry finally re-reading the session.
    assert excinfo.value is err
    # wp_type was snapshotted exactly once, before the failure; the finally must
    # not touch the (now broken) session again.
    assert stub.wp_type_accesses == 1
    # Telemetry is still recorded from the snapshot, tagged with the wp_type.
    duration.record.assert_called_once()
    assert duration.record.call_args.args[1] == {"horde.wp_type": "image"}


def test_activate_success_records_telemetry(_patched_metrics):
    duration, age = _patched_metrics
    stub = _StubWP(lambda: None)

    WaitingPrompt.activate(stub)

    assert stub.retry_calls == 1
    assert stub.wp_type_accesses == 1
    duration.record.assert_called_once()
    assert duration.record.call_args.args[1] == {"horde.wp_type": "image"}
    age.record.assert_called_once()
