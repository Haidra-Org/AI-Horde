# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify priority-user selection during worker-pop setup."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from horde import exceptions
from horde.apis.v2 import base
from horde.classes.base.user import User
from horde.flask import db

pytestmark = pytest.mark.unit


def _prepare_pop(owner: User, priority_usernames: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Mock, Mock]:
    """Build a pop request for the owner with its database and scheduling collaborators observed.

    Returns:
        The pop resource, a mock counting priority-user lookups, and the mock standing in for the sorted request scan.
    """
    original_query = db.session.query
    priority_lookup = Mock()

    def track_query(*entities: Any, **kwargs: Any) -> Any:
        if len(entities) == 1 and entities[0] is User.id:
            priority_lookup()
        return original_query(*entities, **kwargs)

    monkeypatch.setattr(db.session, "query", track_query)
    monkeypatch.setattr(base.database, "get_all_active_worker_messages", lambda worker_id: [])
    pop = base.JobPopTemplate()
    pop.args = SimpleNamespace(priority_usernames=priority_usernames, models=[])
    pop.user = owner
    pop.worker = SimpleNamespace(id="priority-test-worker", maintenance=False)
    pop.skipped = {}
    monkeypatch.setattr(pop, "validate", lambda: None)
    monkeypatch.setattr(pop, "check_in", lambda: None)
    get_sorted_prompts = Mock(return_value=[])
    monkeypatch.setattr(pop, "get_sorted_wp", get_sorted_prompts)
    return pop, priority_lookup, get_sorted_prompts


@pytest.mark.parametrize("has_priority_usernames", [False, True], ids=["empty", "nonempty"])
def test_pop_queries_priority_users_only_when_requested(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    has_priority_usernames: bool,
) -> None:
    """Retain owner priority and select only existing, explicitly requested users."""
    owner = make_user()
    requested_user = make_user()
    unrequested_user = make_user()
    owner_id, requested_user_id = owner.id, requested_user.id
    missing_user_id = max(owner_id, requested_user_id, unrequested_user.id) + 1000
    priority_usernames = [requested_user.get_unique_alias(), f"missing#{missing_user_id}"] if has_priority_usernames else []
    pop, priority_lookup, get_sorted_prompts = _prepare_pop(owner, priority_usernames, monkeypatch)

    with app.test_request_context("/api/v2/generate/pop", method="POST"):
        response, status = pop._post_inner()

    expected_user_ids = [owner_id, requested_user_id] if has_priority_usernames else [owner_id]
    assert priority_lookup.call_count == int(has_priority_usernames)
    assert pop.priority_user_ids == expected_user_ids
    assert get_sorted_prompts.call_args_list[0].args == (expected_user_ids,)
    assert response["ids"] == []
    assert status == 200


@pytest.mark.parametrize("malformed_username", ["db0#abc", "db0#", "db0"], ids=["letters", "empty_suffix", "no_suffix"])
def test_pop_rejects_malformed_priority_usernames(
    app: Any,
    db_session: Any,
    make_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    malformed_username: str,
) -> None:
    """Refuse a priority username without a numeric user id before any lookup or scan."""
    owner = make_user()
    pop, priority_lookup, get_sorted_prompts = _prepare_pop(owner, [owner.get_unique_alias(), malformed_username], monkeypatch)

    with app.test_request_context("/api/v2/generate/pop", method="POST"), pytest.raises(exceptions.BadRequest) as refusal:
        pop._post_inner()

    assert refusal.value.rc == "InvalidPriorityUsername"
    priority_lookup.assert_not_called()
    get_sorted_prompts.assert_not_called()
