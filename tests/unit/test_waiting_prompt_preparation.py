# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Constructing a waiting prompt prepares it without writing it.

The constructor assigns the request id, loads the requester, records the listed workers and models and builds the
job payload. It does not flush or commit. The caller's commit then writes the row with its worker and model rows, and
the row is not updated again in that commit.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.flask import db
from tests.conftest import _QueryRecorder

pytestmark = pytest.mark.unit

IMAGE_MODEL: str = "stable_diffusion"
"""The image model the requests list."""

TEXT_MODEL: str = "elinas/chronos-70b-v2"
"""The text model the requests list."""

WRITE_KINDS: frozenset[str] = frozenset({"INSERT", "UPDATE", "DELETE"})
"""The statement kinds that change rows."""


def _writes(queries: _QueryRecorder) -> list[tuple[str, str | None]]:
    return [(recorded.kind, recorded.table) for recorded in queries.recorded_statements if recorded.kind in WRITE_KINDS]


def _make_text_worker(user: Any) -> TextWorker:
    worker = TextWorker(user_id=user.id, name=f"text_worker_{uuid.uuid4().hex[:12]}")
    db.session.add(worker)
    db.session.commit()
    return worker


def _make_image_wp(user: Any) -> ImageWaitingPrompt:
    return ImageWaitingPrompt(
        [],
        [IMAGE_MODEL],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": 1, "width": 512, "height": 512, "steps": 8, "sampler_name": "k_euler_a", "cfg_scale": 1.5},
    )


def test_constructing_an_image_request_prepares_it_without_writing(db_session, fake_redis, make_user, assert_query_count):
    user = make_user()

    with assert_query_count() as queries:
        waiting_prompt = _make_image_wp(user)

    assert _writes(queries) == [], queries.describe()
    assert queries.commits == [], queries.describe()
    assert waiting_prompt.id is not None
    assert waiting_prompt.user is user
    assert waiting_prompt.get_model_names() == [IMAGE_MODEL]
    assert waiting_prompt.gen_payload["ddim_steps"] == 8
    assert waiting_prompt.total_usage > 0


def test_committing_a_prepared_request_writes_the_row_with_its_workers_and_models_once(
    db_session,
    fake_redis,
    make_user,
    assert_query_count,
):
    user = make_user()
    worker = _make_text_worker(user)
    waiting_prompt = TextWaitingPrompt(
        [worker.id],
        [TEXT_MODEL],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 2048},
    )

    with assert_query_count() as queries:
        db.session.commit()

    assert sorted(_writes(queries)) == [
        ("INSERT", "waiting_prompts"),
        ("INSERT", "wp_allowed_workers"),
        ("INSERT", "wp_models"),
    ], queries.describe()
    assert len(queries.commits) == 1, queries.describe()
    assert waiting_prompt.gen_payload["max_length"] == 80


def test_a_committed_request_reads_its_workers_and_models_without_querying(
    db_session,
    fake_redis,
    make_user,
    assert_query_count,
):
    user = make_user()
    worker = _make_text_worker(user)
    waiting_prompt = TextWaitingPrompt(
        [worker.id],
        [TEXT_MODEL],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 2048},
    )
    db.session.commit()

    with assert_query_count() as queries:
        model_names = waiting_prompt.get_model_names()
        worker_ids = [allowed.worker_id for allowed in waiting_prompt.workers]

    assert model_names == [TEXT_MODEL]
    assert worker_ids == [worker.id]
    assert len(queries) == 0, queries.describe()


def test_a_request_with_a_caller_supplied_id_keeps_it(db_session, fake_redis, make_user):
    user = make_user()
    request_id = uuid.uuid4()

    waiting_prompt = TextWaitingPrompt(
        [],
        [TEXT_MODEL],
        id=request_id,
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 2048},
    )
    db.session.commit()

    assert waiting_prompt.id == request_id
