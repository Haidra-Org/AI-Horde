# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model recorded by ``ProcessingGeneration.__init__``.

Contract under test: a processing generation records a model that the assigned
worker hosts and that is consistent with the waiting prompt's model constraint.

When no explicit model is passed, ``ProcessingGeneration.__init__``
(``horde/classes/base/processing_generation.py``) derives the model from two
data sources read at different points: the waiting prompt's model list and the
worker's persisted model list (via ``Worker.get_model_names``, a Redis-cached
read). These tests pin the recorded model against the worker's authoritative
hosted-model set across a range of overlap, batching, and model-change scenarios.

All tests use ``fake_redis`` because the worker model lookup is Redis-cached via
``Worker.get_model_names`` / ``refresh_model_cache``.
"""

from __future__ import annotations

from typing import Any

import pytest

from horde.classes.base.worker import WorkerModel
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.flask import db

pytestmark = pytest.mark.unit


def _make_text_worker(db_session: Any, user: Any, *, name: str, models: list[str]) -> TextWorker:
    """Create and persist a ``TextWorker`` hosting ``models`` via the public write path."""
    worker = TextWorker(user_id=user.id, name=name)
    db_session.add(worker)
    db_session.commit()
    if models:
        worker.set_models(models)
    return worker


def _make_text_wp(db_session: Any, user: Any, *, models: list[str], jobs: int = 1) -> TextWaitingPrompt:
    """Create and persist a ``TextWaitingPrompt`` constrained to ``models``."""
    wp = TextWaitingPrompt(
        worker_ids=[],
        models=models,
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": jobs, "max_length": 80, "max_context_length": 2048},
    )
    db_session.commit()
    return wp


def _procgens_for_wp(wp: TextWaitingPrompt) -> list[TextProcessingGeneration]:
    return db.session.query(TextProcessingGeneration).filter(TextProcessingGeneration.wp_id == wp.id).all()


def _worker_hosted_models(worker: TextWorker) -> list[str]:
    """Return the worker's hosted models from the ``worker_models`` table.

    The ``worker_models`` table is authoritative for what a worker hosts, so it
    is the correct set against which a recorded model's validity is judged.
    """
    return [row.model for row in db.session.query(WorkerModel.model).filter(WorkerModel.worker_id == worker.id).all()]


class TestRecordedModelIsHostedByWorker:
    """A recorded model is always one the assigned worker hosts."""

    def test_single_wp_model_disjoint_from_worker(self, db_session, fake_redis, make_user):
        # Worker hosts model_alpha; the prompt is constrained to model_beta.
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_alpha_only", models=["model_alpha"])
        wp = _make_text_wp(db_session, user, models=["model_beta"])

        wp.start_generation(worker)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == 1
        hosted_models = _worker_hosted_models(worker)
        assert procgens[0].model in hosted_models

    def test_multiple_wp_models_disjoint_from_worker(self, db_session, fake_redis, make_user):
        # Worker hosts model_alpha; the prompt allows model_beta or model_gamma.
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_alpha_only_multi", models=["model_alpha"])
        wp = _make_text_wp(db_session, user, models=["model_beta", "model_gamma"])

        wp.start_generation(worker)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == 1
        hosted_models = _worker_hosted_models(worker)
        assert procgens[0].model in hosted_models


class TestRecordedModelAfterWorkerModelsChange:
    """A recorded model is one the worker hosts even when its model set changes
    between the prompt match and the generation's creation.

    A worker running multiple bridges pops concurrently; ``set_models`` runs on
    every pop, so the worker's persisted model set can change between the match
    (made against the prompt's model list) and ``ProcessingGeneration.__init__``.
    The scenario is modelled by changing the worker's models after the prompt is
    created but before ``start_generation``.
    """

    def test_worker_models_changed_before_generation(self, db_session, fake_redis, make_user):
        user = make_user()
        # At match time the worker hosts model_beta and the prompt requests it.
        worker = _make_text_worker(db_session, user, name="worker_multibridge", models=["model_beta"])
        wp = _make_text_wp(db_session, user, models=["model_beta"])

        # The worker's model set changes before this generation is created.
        worker.set_models(["model_alpha"])

        wp.start_generation(worker)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == 1
        hosted_models = _worker_hosted_models(worker)
        assert procgens[0].model in hosted_models


class TestSingleSharedModel:
    """A single shared model between worker and prompt is the one recorded."""

    def test_shared_model_is_recorded(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_shared", models=["model_shared"])
        wp = _make_text_wp(db_session, user, models=["model_shared"])

        wp.start_generation(worker)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == 1
        assert procgens[0].model == "model_shared"


class TestMultipleOverlap:
    """With several shared models, the recorded model is one of the shared set."""

    def test_recorded_model_is_within_overlap(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_overlap", models=["model_a", "model_b", "model_c"])
        wp = _make_text_wp(db_session, user, models=["model_b", "model_c"])

        wp.start_generation(worker)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == 1
        assert procgens[0].model in {"model_b", "model_c"}


class TestNoModelConstraint:
    """An unconstrained prompt records one of the worker's own models."""

    def test_recorded_model_is_a_worker_model(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_unconstrained", models=["model_x", "model_y"])
        wp = _make_text_wp(db_session, user, models=[])

        wp.start_generation(worker)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == 1
        assert procgens[0].model in set(_worker_hosted_models(worker))


class TestExplicitModelKwarg:
    """An explicit ``model=`` argument is recorded verbatim."""

    def test_explicit_model_is_recorded_verbatim(self, db_session, fake_redis, make_user):
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_explicit", models=["model_a", "model_b"])
        wp = _make_text_wp(db_session, user, models=["model_a"])

        procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="model_explicit")

        assert procgen.model == "model_explicit"


class TestBatching:
    """All procgens in a single batched pop record the same model."""

    def test_batched_procgens_share_a_single_model(self, db_session, fake_redis, make_user):
        batch_size = 3
        user = make_user()
        worker = _make_text_worker(db_session, user, name="worker_batch", models=["model_a", "model_b", "model_c"])
        wp = _make_text_wp(db_session, user, models=["model_b", "model_c"], jobs=batch_size)

        wp.start_generation(worker, amount=batch_size)

        procgens = _procgens_for_wp(wp)
        assert len(procgens) == batch_size
        recorded_models = {procgen.model for procgen in procgens}
        assert len(recorded_models) == 1
        assert recorded_models.pop() in {"model_b", "model_c"}
