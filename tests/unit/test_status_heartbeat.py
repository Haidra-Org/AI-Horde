# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavioral tests for the node heartbeat endpoint.

The heartbeat backs the load balancer's per-node health checks, so it must
report only node-local state. A signal derived from shared database state
(such as kudos applier queue lag) would flip on every node simultaneously and
remove the entire fleet from rotation at once.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient
    from flask_sqlalchemy.session import Session
    from sqlalchemy.orm import scoped_session

    from tests.fixture_types import MakeUser

HEARTBEAT_PATH = "/api/v2/status/heartbeat"


class _StubTaskDispatcher:
    """Minimal stand-in for waitress's task dispatcher outside a waitress run."""

    def __init__(self, queued: int = 0) -> None:
        self.queue = [object()] * queued
        self.threads = [object()] * 8
        self.active_count = 1


@pytest.fixture
def _stub_waitress(monkeypatch: pytest.MonkeyPatch) -> _StubTaskDispatcher:
    from horde.metrics import waitress_metrics

    dispatcher = _StubTaskDispatcher()
    monkeypatch.setattr(waitress_metrics, "task_dispatcher", dispatcher)
    return dispatcher


class TestHeartbeatIsNodeLocal:
    def test_heartbeat_reports_node_local_health(
        self,
        client: FlaskClient,
        db_session: scoped_session[Session],
        _stub_waitress: _StubTaskDispatcher,
    ) -> None:
        """A healthy node answers OK with its own thread-pool and DB fields only."""
        response = client.get(HEARTBEAT_PATH)
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["message"] == "OK"
        assert payload["db_connection"] is True
        assert payload["queue"] == 0
        assert "kudos_ledger" not in payload

    def test_heartbeat_ignores_kudos_applier_backlog(
        self,
        client: FlaskClient,
        db_session: scoped_session[Session],
        make_user: MakeUser,
        assert_query_count,
        _stub_waitress: _StubTaskDispatcher,
    ) -> None:
        """A deep, old kudos fold backlog does not degrade node health.

        The backlog is shared database state: every node observes the same
        value, so surfacing it here would fail health checks fleet-wide in
        unison. The endpoint must neither report it nor pay queries for it.
        """
        from horde.classes.base.kudos import KudosLedger, KudosStatEvent
        from horde.enums import KudosEntryType, KudosUnit

        user = make_user()
        stale = datetime.utcnow() - timedelta(minutes=10)
        db_session.add(
            KudosLedger(
                created=stale,
                event_id=uuid.uuid4(),
                entry_type=KudosEntryType.GENERATION,
                user_id=user.id,
                amount=1,
                applied=False,
            ),
        )
        db_session.add(
            KudosStatEvent(
                created=stale,
                event_id=uuid.uuid4(),
                entry_type=KudosEntryType.GENERATION,
                user_id=user.id,
                amount=1,
                unit=KudosUnit.KUDOS,
                applied=False,
            ),
        )
        db_session.commit()

        with assert_query_count() as queries:
            response = client.get(HEARTBEAT_PATH)

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["message"] == "OK"
        assert "kudos_ledger" not in payload
        kudos_queries = [s for s in queries.statements if "kudos_" in s]
        assert kudos_queries == []

    def test_heartbeat_reports_overloaded_from_own_queue(
        self,
        client: FlaskClient,
        db_session: scoped_session[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A backed-up local request queue is the node's own signal to report."""
        from horde.metrics import waitress_metrics

        monkeypatch.setattr(waitress_metrics, "task_dispatcher", _StubTaskDispatcher(queued=3))
        response = client.get(HEARTBEAT_PATH)
        assert response.status_code == 200
        assert response.get_json()["message"] == "OVERLOADED"
