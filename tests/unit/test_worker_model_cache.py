# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coherence between ``Worker.set_models`` and ``Worker.get_model_names``.

Contract under test: after ``set_models(models)`` commits, ``get_model_names()``
returns those models.

``set_models`` (``horde/classes/base/worker.py``) writes the model list to both
the ``worker_models`` table and a Redis cache (``worker_{id}_model_cache``) that
``get_model_names`` and the registry consume. The cache entry is refreshed from
the ``self.models`` relationship collection, with a 600s TTL. The Flask session
uses ``expire_on_commit=False`` (``horde/flask.py``), so an ORM collection loaded
before a commit remains loaded afterward; whether ``set_models`` reads the cache
or the collection therefore depends on the cache state when it runs. These tests
exercise ``get_model_names`` (the public read surface) after ``set_models`` (the
public write surface) across those scenarios.

All tests use ``fake_redis`` because the model cache lives in Redis.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from horde.classes.base.worker import WorkerModel
from horde.classes.kobold.worker import TextWorker
from horde.flask import db

pytestmark = pytest.mark.unit


def _model_cache_key(worker: TextWorker) -> str:
    return f"worker_{worker.id}_model_cache"


def _make_text_worker(db_session: Any, user: Any, *, name: str) -> TextWorker:
    """Create and persist a bare ``TextWorker`` with no models yet."""
    worker = TextWorker(user_id=user.id, name=name)
    db_session.add(worker)
    db_session.commit()
    return worker


def _worker_model_rows(worker: TextWorker) -> set[str]:
    """Return the worker's models from the authoritative ``worker_models`` table."""
    return {row.model for row in db.session.query(WorkerModel.model).filter(WorkerModel.worker_id == worker.id).all()}


class TestFreshWorkerModelPublication:
    """The first ``set_models`` on a worker publishes those models through ``get_model_names``."""

    def test_lookup_reflects_freshly_set_models(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_fresh_cache")

        worker.set_models(["model_one"])

        assert worker.get_model_names() == ["model_one"]


class TestModelChangeWithExpiredCache:
    """Changing models when the cache has expired publishes the new list.

    The 600s cache entry is absent (modelled by deleting the key), as it would be
    once its TTL lapses, and the worker re-declares a different model set.
    """

    def test_model_change_after_cache_expiry_publishes_new_models(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_change_miss")
        worker.set_models(["model_old"])

        # A fresh request context (one worker instance per request) with the
        # cache entry no longer present.
        db.session.expire_all()
        worker = db.session.query(TextWorker).filter(TextWorker.id == worker.id).one()
        fake_redis.horde_r_delete(_model_cache_key(worker))

        worker.set_models(["model_new"])

        assert worker.get_model_names() == ["model_new"]


class TestModelChangeWithWarmCache:
    """Changing models when the cache is warm publishes the new list."""

    def test_model_change_with_warm_cache_publishes_new_models(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_change_hit")
        worker.set_models(["model_old"])

        # Pre-warm the cache with the current value and ensure the relationship
        # collection is not already loaded in this session.
        fake_redis.horde_r_setex(_model_cache_key(worker), timedelta(seconds=600), json.dumps(["model_old"]))
        db.session.expire(worker, ["models"])

        worker.set_models(["model_new"])

        assert worker.get_model_names() == ["model_new"]


class TestDatabaseRowsReflectLastCall:
    """The ``worker_models`` table matches the last ``set_models`` call."""

    def test_db_rows_match_last_set_models_call(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_db_rows")

        worker.set_models(["model_alpha"])
        assert _worker_model_rows(worker) == {"model_alpha"}

        worker.set_models(["model_beta", "model_gamma"])
        assert _worker_model_rows(worker) == {"model_beta", "model_gamma"}
