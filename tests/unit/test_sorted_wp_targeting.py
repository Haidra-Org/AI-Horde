# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for worker targeting in the sorted waiting-prompt fetch.

A request may target workers through ``wp_allowed_workers`` rows in one of two
modes selected by ``worker_blacklist``: an allowlist (only listed workers may
serve it) or a blacklist (every listed worker is excluded). The pop candidate
query (``get_sorted_text_wp_filtered_to_worker`` in
``horde/database/text_functions.py``; the image variant shares the predicate
shape) must evaluate that membership per request, never per targeting row: a
row-level ``worker_id != x`` comparison against a joined targeting table admits
a blacklisted worker whenever the blacklist also names anyone else, because the
other rows satisfy the comparison.

The text variant is exercised because its candidate query is expressible on the
SQLite test backend (the image variant filters on PostgreSQL-only JSONB
operators).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.database import text_functions as tf
from horde.flask import db

pytestmark = pytest.mark.unit


def _make_text_worker(user: Any) -> TextWorker:
    """Create and persist a ``TextWorker`` that passes every non-targeting gate.

    Capacity is set above the request defaults, speed above every slow-worker
    tier, and NSFW/unsafe-IP permissions are opened so targeting membership is
    the only lever the tests move.
    """
    worker = TextWorker(
        user_id=user.id,
        name=f"text_worker_{uuid.uuid4().hex[:12]}",
        max_length=512,
        max_context_length=4096,
    )
    worker.speed = 100
    worker.nsfw = True
    worker.allow_unsafe_ipaddr = True
    db.session.add(worker)
    db.session.commit()
    return worker


def _make_text_wp(user: Any, *, worker_ids: list, worker_blacklist: bool) -> TextWaitingPrompt:
    """Create and persist an active ``TextWaitingPrompt`` targeting ``worker_ids``.

    The constructor records ``worker_ids`` as ``wp_allowed_workers`` rows;
    ``worker_blacklist`` selects whether they form an allowlist or a blacklist.
    Activation state is written directly to keep the test independent of the
    kudos and notification machinery that ``activate()`` carries.
    """
    wp = TextWaitingPrompt(
        worker_ids,
        [],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 2048},
        worker_blacklist=worker_blacklist,
    )
    wp.active = True
    wp.validated_backends = False
    wp.expiry = datetime.utcnow() + timedelta(minutes=10)
    db.session.commit()
    return wp


def _fetched_wp_ids(worker: TextWorker) -> set:
    return {wp.id for wp in tf.get_sorted_text_wp_filtered_to_worker(worker, models_list=[])}


class TestBlacklistExcludesEveryListedWorker:
    """A blacklisted worker is excluded regardless of who else the blacklist names."""

    def test_multi_entry_blacklist_excludes_a_listed_worker(self, db_session, fake_redis, make_user):
        # The blacklist names two workers. The listed worker must not receive
        # the request even though the other blacklist row's worker_id differs
        # from its own.
        user = make_user()
        listed = _make_text_worker(user)
        also_listed = _make_text_worker(user)
        _make_text_wp(user, worker_ids=[also_listed.id, listed.id], worker_blacklist=True)

        assert _fetched_wp_ids(listed) == set()

    def test_blacklist_admits_an_unlisted_worker(self, db_session, fake_redis, make_user):
        user = make_user()
        listed = _make_text_worker(user)
        unlisted = _make_text_worker(user)
        wp = _make_text_wp(user, worker_ids=[listed.id], worker_blacklist=True)

        assert wp.id in _fetched_wp_ids(unlisted)


class TestAllowlistAdmitsOnlyListedWorkers:
    """An allowlist request is served only by the workers it names."""

    def test_allowlist_admits_the_listed_worker(self, db_session, fake_redis, make_user):
        user = make_user()
        listed = _make_text_worker(user)
        wp = _make_text_wp(user, worker_ids=[listed.id], worker_blacklist=False)

        assert wp.id in _fetched_wp_ids(listed)

    def test_allowlist_excludes_an_unlisted_worker(self, db_session, fake_redis, make_user):
        user = make_user()
        listed = _make_text_worker(user)
        unlisted = _make_text_worker(user)
        _make_text_wp(user, worker_ids=[listed.id], worker_blacklist=False)

        assert _fetched_wp_ids(unlisted) == set()


class TestUntargetedRequestIsOpenToAll:
    """A request with no targeting rows is served by any capable worker."""

    def test_untargeted_wp_is_fetched(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(user)
        wp = _make_text_wp(user, worker_ids=[], worker_blacklist=False)

        assert wp.id in _fetched_wp_ids(worker)
