# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the user/worker lookup helpers in ``horde.database.functions``.

These sit on the hottest request paths - ``find_user_by_api_key`` runs for every
authenticated call - so alongside correctness we lock the query *count* to guard
against an N+1 regression sneaking onto the auth path.
"""

from __future__ import annotations

from horde.database import functions as f
from horde.utils import hash_api_key


class TestFindUserByApiKey:
    def test_found_by_hashed_key(self, db_session, make_user):
        raw_key = "my-secret-raw-key"
        user = make_user(api_key=hash_api_key(raw_key))
        assert f.find_user_by_api_key(raw_key).id == user.id

    def test_wrong_key_returns_none(self, db_session, make_user):
        make_user(api_key=hash_api_key("the-right-key"))
        assert f.find_user_by_api_key("the-wrong-key") is None

    def test_wiped_account_excluded(self, db_session, make_user):
        raw_key = "wiped-user-key"
        make_user(api_key=hash_api_key(raw_key), oauth_id="<wiped>")
        assert f.find_user_by_api_key(raw_key) is None

    def test_auth_lookup_is_single_select(self, db_session, make_user, assert_query_count):
        raw_key = "hot-path-key"
        make_user(api_key=hash_api_key(raw_key))
        with assert_query_count() as queries:
            f.find_user_by_api_key(raw_key)
        # The auth hot path must stay a single SELECT. No per-attribute fan-out.
        assert len(queries.of_kind("SELECT")) == 1


class TestFindUserById:
    def test_found(self, db_session, make_user):
        user = make_user()
        assert f.find_user_by_id(user.id).id == user.id

    def test_missing_returns_none(self, db_session, make_user):
        make_user()
        assert f.find_user_by_id(999_999) is None

    def test_wiped_account_excluded(self, db_session, make_user):
        user = make_user(oauth_id="<wiped>")
        assert f.find_user_by_id(user.id) is None


class TestFindUserByUsername:
    def test_resolves_by_trailing_id(self, db_session, make_user):
        user = make_user()
        # Only the numeric suffix after the final '#' is authoritative.
        assert f.find_user_by_username(f"AnyDisplayName#{user.id}").id == user.id

    def test_non_numeric_suffix_returns_none(self, db_session, make_user):
        make_user()
        assert f.find_user_by_username("no-hash-here") is None

    def test_anon_blocked_when_disallowed(self, db_session, monkeypatch):
        monkeypatch.setattr(f, "ALLOW_ANONYMOUS", False)
        assert f.find_user_by_username("Anonymous#0") is None


class TestFindUserByOauthId:
    def test_found(self, db_session, make_user):
        user = make_user(oauth_id="oauth-distinct-123")
        assert f.find_user_by_oauth_id("oauth-distinct-123").id == user.id

    def test_anon_blocked_when_disallowed(self, db_session, monkeypatch):
        monkeypatch.setattr(f, "ALLOW_ANONYMOUS", False)
        assert f.find_user_by_oauth_id("anon") is None


class TestWorkerLookups:
    """Negative paths exercise the multi-class query loop without needing a
    full worker object graph."""

    def test_worker_name_exists_false_for_unknown(self, db_session):
        assert f.worker_name_exists("no-such-worker-name") is False

    def test_find_worker_by_id_rejects_non_uuid(self, db_session):
        assert f.find_worker_by_id("not-a-uuid") is None

    def test_find_worker_by_name_unknown_returns_none(self, db_session):
        assert f.find_worker_by_name("no-such-worker") is None
