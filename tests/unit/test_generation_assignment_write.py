# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify that assigning a job writes its row, model and lease in one transaction against real PostgreSQL rows."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import Mock

import pytest

from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.flask import db
from tests.conftest import _QueryRecorder
from tests.fixture_types import MakeUser

pytestmark = pytest.mark.unit

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

ASSIGNED_MODEL = "assignment-test-model"
"""The model the request asks for and the worker serves."""


@pytest.fixture
def stored_request(make_user: MakeUser, fake_redis: object) -> tuple[TextWaitingPrompt, TextWorker]:
    """Create a committed request and worker ready for a job assignment."""
    requester = make_user()
    worker = TextWorker(user_id=requester.id, name=f"assignment_worker_{requester.id}")
    db.session.add(worker)
    db.session.flush()
    prompt = TextWaitingPrompt(
        [],
        [ASSIGNED_MODEL],
        prompt="a test prompt",
        user_id=requester.id,
        params={"n": 1, "max_length": 80, "max_context_length": 1024},
    )
    db.session.add(prompt)
    db.session.commit()
    return prompt, worker


def test_assignment_writes_the_job_in_one_transaction(
    stored_request: tuple[TextWaitingPrompt, TextWorker],
    assert_query_count: QueryCounter,
) -> None:
    """Insert the job with its model and lease under one commit and no UPDATE."""
    prompt, worker = stored_request
    with assert_query_count() as queries:
        generation = TextProcessingGeneration(wp_id=prompt.id, worker_id=worker.id, model=ASSIGNED_MODEL)
    assert len(queries.of_kind("INSERT")) == 1, queries.describe()
    assert not queries.of_kind("UPDATE"), queries.describe()
    assert len(queries.commits) == 1, queries.describe()
    db.session.refresh(generation)
    assert generation.model == ASSIGNED_MODEL
    assert generation.job_ttl > 0


def test_a_failed_assignment_leaves_no_job_behind(
    stored_request: tuple[TextWaitingPrompt, TextWorker],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back the job row when the lease calculation fails after the row was flushed."""
    prompt, worker = stored_request
    monkeypatch.setattr(TextProcessingGeneration, "set_job_ttl", Mock(side_effect=RuntimeError("lease failed")))
    with pytest.raises(RuntimeError, match="lease failed"):
        TextProcessingGeneration(wp_id=prompt.id, worker_id=worker.id, model=ASSIGNED_MODEL)
    db.session.rollback()
    stored_jobs = db.session.query(TextProcessingGeneration).filter(TextProcessingGeneration.wp_id == prompt.id).count()
    assert stored_jobs == 0
