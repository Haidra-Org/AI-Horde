# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for ``User._has_role`` consolidation.

The seven role hybrid_property getters (``trusted``/``flagged``/``moderator``/
``customizer``/``vpn``/``service``/``education``) used to issue one
``UserRole.query.filter_by(...).first()`` per access. Under load this added
5–15 ms / request via 6–7 round trips. The perf branch consolidated them onto
``_has_role`` which iterates ``self.roles`` once.

These tests pin that behaviour. If a future refactor reintroduces per-role
queries, ``test_role_access_does_not_emit_per_role_select`` fails.
"""

from __future__ import annotations

import pytest

from horde.enums import UserRoleTypes

pytestmark = pytest.mark.unit


@pytest.fixture
def user_with_roles(make_user, make_user_role, db_session):
    """Yield a user with TRUSTED + MODERATOR set true, FLAGGED set false."""
    user = make_user()
    make_user_role(user, UserRoleTypes.TRUSTED, value=True)
    make_user_role(user, UserRoleTypes.MODERATOR, value=True)
    make_user_role(user, UserRoleTypes.FLAGGED, value=False)
    db_session.flush()
    return user


class TestRoleTruthiness:
    def test_role_set_true_is_true(self, user_with_roles):
        assert user_with_roles.trusted is True
        assert user_with_roles.moderator is True

    def test_role_explicitly_false_is_false(self, user_with_roles):
        assert user_with_roles.flagged is False

    def test_role_absent_is_false(self, user_with_roles):
        # No CUSTOMIZER / VPN / SERVICE / EDUCATION rows exist for this user.
        assert user_with_roles.customizer is False
        assert user_with_roles.vpn is False
        assert user_with_roles.service is False
        assert user_with_roles.education is False

    def test_user_without_any_roles(self, make_user):
        user = make_user()
        assert user.trusted is False
        assert user.flagged is False
        assert user.moderator is False


class TestRoleAccessQueryCount:
    """Lock in the single-SELECT behaviour for the roles relationship.

    Reading all seven role properties on a user with a freshly-expired ORM
    cache should issue at most ONE SELECT against ``user_roles`. The
    ``self.roles`` collection load. Anything more means a regression to the
    per-role query pattern.
    """

    def test_all_seven_role_props_issue_one_user_roles_select(
        self,
        user_with_roles,
        db_session,
        assert_query_count,
    ):
        # Force the roles relationship to be re-fetched from the DB on next
        # access by expiring the user. Without this, the session may already
        # have the roles cached from the fixture's flush.
        db_session.expire(user_with_roles)

        with assert_query_count() as queries:
            _ = user_with_roles.trusted
            _ = user_with_roles.flagged
            _ = user_with_roles.moderator
            _ = user_with_roles.customizer
            _ = user_with_roles.vpn
            _ = user_with_roles.service
            _ = user_with_roles.education

        # Exactly one SELECT against user_roles is allowed (the relationship
        # collection load on first access). Counting *any* SELECT against the
        # user_roles table guards against the regression more reliably than
        # counting all SELECTs (which may include unrelated identity-map loads
        # for the User row itself).
        user_roles_selects = [s for s in queries.of_kind("SELECT") if "user_roles" in s.lower()]
        assert len(user_roles_selects) <= 1, (
            f"Expected ≤1 SELECT against user_roles for 7 role property reads; "
            f"got {len(user_roles_selects)}:\n" + "\n---\n".join(user_roles_selects)
        )

    def test_role_props_use_cached_collection_on_repeat_access(
        self,
        user_with_roles,
        assert_query_count,
    ):
        # Prime the roles cache.
        _ = user_with_roles.trusted

        # Subsequent reads against the warm cache should hit zero queries.
        with assert_query_count() as queries:
            for _ in range(5):
                _ = user_with_roles.trusted
                _ = user_with_roles.moderator
                _ = user_with_roles.flagged

        user_roles_selects = [s for s in queries.of_kind("SELECT") if "user_roles" in s.lower()]
        assert user_roles_selects == [], f"Cached role reads should not re-query user_roles; got {len(user_roles_selects)} queries"


class TestHasRoleHelper:
    """Direct tests on the consolidated ``_has_role`` helper."""

    def test_returns_false_when_no_roles_loaded(self, make_user):
        user = make_user()
        assert user._has_role(UserRoleTypes.TRUSTED) is False

    def test_returns_value_when_role_present(self, user_with_roles):
        assert user_with_roles._has_role(UserRoleTypes.TRUSTED) is True
        assert user_with_roles._has_role(UserRoleTypes.FLAGGED) is False

    def test_returns_false_when_role_absent(self, user_with_roles):
        assert user_with_roles._has_role(UserRoleTypes.SERVICE) is False
