# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coherence between ``TextWorker.set_softprompts`` and ``TextWorker.get_softprompt_names``.

Contract under test: after ``set_softprompts(softprompts)`` runs, ``get_softprompt_names()``
returns those softprompts.

``set_softprompts`` (``horde/classes/kobold/worker.py``) writes the softprompt list to
both the ``text_worker_softprompts`` table and a Redis cache
(``worker_{id}_softprompts_cache``) that ``get_softprompt_names`` consumes, refreshed
from the ``self.softprompts`` relationship collection with a 600s TTL. The mechanism
mirrors ``Worker.set_models`` / ``Worker.get_model_names``: whether the refresh observes
the newly written rows or a stale relationship collection depends on the cache and
session state when it runs. The Flask session uses ``expire_on_commit=False``
(``horde/flask.py``), so a collection loaded before a write stays loaded afterwards.
These tests exercise ``get_softprompt_names`` (the public read surface) after
``set_softprompts`` (the public write surface) across those scenarios.

All tests use ``fake_redis`` because the softprompt cache lives in Redis.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from horde.classes.kobold.worker import TextWorker, TextWorkerSoftprompts
from horde.flask import db

pytestmark = pytest.mark.unit


def _softprompt_cache_key(worker: TextWorker) -> str:
    return f"worker_{worker.id}_softprompts_cache"


def _make_text_worker(db_session: Any, user: Any, *, name: str) -> TextWorker:
    """Create and persist a bare ``TextWorker`` with no softprompts yet."""
    worker = TextWorker(user_id=user.id, name=name)
    db_session.add(worker)
    db_session.commit()
    return worker


def _worker_softprompt_rows(worker: TextWorker) -> set[str]:
    """Return the worker's softprompts from the authoritative ``text_worker_softprompts`` table."""
    rows = db.session.query(TextWorkerSoftprompts.softprompt).filter(TextWorkerSoftprompts.worker_id == worker.id).all()
    return {row.softprompt for row in rows}


class TestFreshWorkerSoftpromptPublication:
    """The first ``set_softprompts`` on a worker publishes those softprompts through ``get_softprompt_names``."""

    def test_lookup_reflects_freshly_set_softprompts(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="text_worker_fresh_sp_cache")

        worker.set_softprompts(["sp_one"])

        assert worker.get_softprompt_names() == ["sp_one"]


class TestSoftpromptChangeWithExpiredCache:
    """Changing softprompts when the cache has expired publishes the new list.

    The 600s cache entry is absent (modelled by deleting the key), as it would be
    once its TTL lapses, and the worker re-declares a different softprompt set.
    """

    def test_softprompt_change_after_cache_expiry_publishes_new_softprompts(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="text_worker_sp_change_miss")
        worker.set_softprompts(["sp_old"])

        # A fresh request context (one worker instance per request) with the
        # cache entry no longer present.
        db.session.expire_all()
        worker = db.session.query(TextWorker).filter(TextWorker.id == worker.id).one()
        fake_redis.horde_r_delete(_softprompt_cache_key(worker))

        worker.set_softprompts(["sp_new"])

        assert worker.get_softprompt_names() == ["sp_new"]


class TestSoftpromptChangeWithWarmCache:
    """Changing softprompts when the cache is warm and the relationship is unloaded publishes the new list."""

    def test_softprompt_change_with_warm_cache_publishes_new_softprompts(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="text_worker_sp_change_hit")
        worker.set_softprompts(["sp_old"])

        # Pre-warm the cache with the current value and ensure the relationship
        # collection is not already loaded in this session.
        fake_redis.horde_r_setex(_softprompt_cache_key(worker), timedelta(seconds=600), json.dumps(["sp_old"]))
        db.session.expire(worker, ["softprompts"])

        worker.set_softprompts(["sp_new"])

        assert worker.get_softprompt_names() == ["sp_new"]


class TestDatabaseRowsReflectLastCall:
    """The ``text_worker_softprompts`` table matches the last ``set_softprompts`` call."""

    def test_db_rows_match_last_set_softprompts_call(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="text_worker_sp_db_rows")

        worker.set_softprompts(["sp_alpha"])
        assert _worker_softprompt_rows(worker) == {"sp_alpha"}

        worker.set_softprompts(["sp_beta", "sp_gamma"])
        assert _worker_softprompt_rows(worker) == {"sp_beta", "sp_gamma"}
