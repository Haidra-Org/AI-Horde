# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify completion SQL and identity-map state against real PostgreSQL rows.

Call the base completion method on text/chat instances to isolate its claim and
commit from subclass statistics. Accounting and post-commit hooks are observed
separately, while the completion UPDATE, flush and transaction remain real.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import Mock

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.flask import db
from tests.fixture_types import MakeUser
from tests.unit.conftest import _QueryRecorder

pytestmark = pytest.mark.unit

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]


@pytest.fixture
def pending_generation(make_user: MakeUser, fake_redis: object, monkeypatch: pytest.MonkeyPatch) -> TextProcessingGeneration:
    """Create a stored pending job with observable settlement and delivery hooks."""
    requester = make_user()
    worker = TextWorker(user_id=requester.id, name=f"completion_worker_{requester.id}")
    db.session.add(worker)
    db.session.flush()
    prompt = TextWaitingPrompt(
        [],
        ["completion-test-model"],
        prompt="a test prompt",
        user_id=requester.id,
        params={"n": 1, "max_length": 80, "max_context_length": 1024},
    )
    generation = TextProcessingGeneration(wp_id=prompt.id, worker_id=worker.id, model="completion-test-model")
    generation.seed = 37
    generation.gen_metadata = [{"type": "information", "value": "existing"}]
    generation.cancelled = True
    db.session.commit()
    # Load relationships before SQL counting; completion itself needs no refresh.
    assert generation.wp is prompt
    assert generation.worker is worker
    monkeypatch.setattr(generation, "get_gen_kudos", Mock(return_value=12.5))
    monkeypatch.setattr(generation, "record", Mock())
    monkeypatch.setattr(generation, "_record_capacity_return", Mock())
    monkeypatch.setattr(generation, "send_webhook", Mock())
    monkeypatch.setattr(worker, "record_performance", Mock())
    monkeypatch.setattr(prompt, "is_completed", Mock(return_value=False))
    return generation


@pytest.mark.parametrize("is_chat", [False, True])
@pytest.mark.parametrize("submitted", [{}, {"seed": 0, "gen_metadata": []}, {"seed": 99, "gen_metadata": None}])
def test_completion_writes_once_and_populates_reward_inputs(
    pending_generation: TextProcessingGeneration,
    assert_query_count: QueryCounter,
    submitted: dict[str, object],
    is_chat: bool,
) -> None:
    """Store one sanitized completion, preserving omitted values and chat fields.

    Settlement still sees the job's pre-claim cancelled flag, so its log can
    report a submission that arrived after cancellation. The flag is synchronized afterwards.
    """
    generation = pending_generation
    generation.wp.is_chat = is_chat
    db.session.commit()
    expected_columns = {
        "generation": "result\ufffdtext",
        "seed": submitted.get("seed", generation.seed),
        "gen_metadata": submitted.get("gen_metadata", generation.gen_metadata),
    }
    submission = dict(submitted)
    if is_chat:
        submission.update(
            message={"role": "assistant", "content": "result text"},
            finish_reason="stop",
            native_finish_reason="eos",
            prompt_tokens=20,
            completion_tokens=5,
            reasoning_tokens=0,
        )
        expected_columns.update(generation.get_submission_columns(**submission))

    def check_reward_inputs() -> float:
        for column_name, expected_value in expected_columns.items():
            assert getattr(generation, column_name) == expected_value
            assert not inspect(generation).attrs[column_name].history.has_changes()
        assert generation.cancelled is True
        return 12.5

    def check_settlement_inputs(things_per_sec: float, kudos: float) -> None:
        assert generation.cancelled is True

    generation.get_gen_kudos.side_effect = check_reward_inputs
    generation.record.side_effect = check_settlement_inputs
    with assert_query_count() as queries:
        reward = ProcessingGeneration.set_generation(generation, "result\x00text", 4.0, **submission)
    assert reward == 12.5
    assert len(queries.of_kind("UPDATE")) == 1
    assert not queries.of_kind("SELECT")
    assert generation.cancelled is False
    assert not inspect(generation).attrs["cancelled"].history.has_changes()
    expected_columns["cancelled"] = False
    generation.record.assert_called_once_with(4.0, 12.5)
    generation.worker.record_performance.assert_called_once_with(4.0)
    generation.send_webhook.assert_called_once_with(12.5)
    db.session.refresh(generation)
    for column_name, expected_value in expected_columns.items():
        assert getattr(generation, column_name) == expected_value


def test_completion_keeps_unrelated_pending_changes(pending_generation: TextProcessingGeneration, assert_query_count: QueryCounter) -> None:
    """Keep changes outside the claimed columns dirty until the normal commit."""
    generation = pending_generation
    generation.job_ttl = 4321
    with db.session.no_autoflush, assert_query_count() as queries:
        ProcessingGeneration.set_generation(generation, "result", 4.0)
    updates = queries.of_kind("UPDATE")
    assert len(updates) == 2
    assert sum("generation=" in statement for statement in updates) == 1
    db.session.refresh(generation)
    assert generation.job_ttl == 4321
    assert generation.generation == "result"


def test_explicit_null_seed_retains_database_validation(pending_generation: TextProcessingGeneration) -> None:
    """Reject an explicitly null seed rather than treating it as omitted."""
    generation = pending_generation
    with pytest.raises(IntegrityError):
        ProcessingGeneration.set_generation(generation, "result", 4.0, seed=None)
    db.session.rollback()
    assert generation.generation is None
    assert generation.seed == 37
    generation.record.assert_not_called()


def test_duplicate_completion_does_not_repeat_accounting(
    pending_generation: TextProcessingGeneration, assert_query_count: QueryCounter
) -> None:
    """Keep the winning result and settle it once when a submission is retried."""
    generation = pending_generation
    ProcessingGeneration.set_generation(generation, "winner", 4.0, seed=123, gen_metadata=[])
    with assert_query_count() as queries:
        result = ProcessingGeneration.set_generation(generation, "loser", 9.0, seed=456, gen_metadata=None)
        db.session.commit()
    assert result == 0
    assert len(queries.of_kind("UPDATE")) == 1
    assert len(queries.of_kind("SELECT")) == 1
    assert (generation.generation, generation.seed, generation.gen_metadata) == ("winner", 123, [])
    generation.get_gen_kudos.assert_called_once()
    generation.record.assert_called_once_with(4.0, 12.5)
    generation.worker.record_performance.assert_called_once_with(4.0)
    generation.send_webhook.assert_called_once_with(12.5)


@pytest.mark.parametrize("winner", ["completion", "fault"])
def test_stale_submission_cannot_overwrite_an_independent_claim(
    pending_generation: TextProcessingGeneration, assert_query_count: QueryCounter, winner: str
) -> None:
    """Refresh a lost compare-and-set without writing its result or accounting."""
    generation = pending_generation
    generation.wp.is_chat = True
    db.session.commit()
    winning_message = {"role": "assistant", "content": "winner"}
    with Session(db.engine) as other_session:
        other_session.execute(
            update(TextProcessingGeneration)
            .where(TextProcessingGeneration.id == generation.id)
            .values(
                generation="winner" if winner == "completion" else None,
                faulted=winner == "fault",
                seed=123,
                gen_metadata=[],
                message=winning_message,
            ),
        )
        other_session.commit()
    assert generation.generation is None
    assert generation.faulted is False
    with assert_query_count() as queries:
        result = ProcessingGeneration.set_generation(
            generation, "loser", 9.0, seed=456, gen_metadata=None, message={"role": "assistant", "content": "loser"}
        )
        db.session.commit()
    assert result == (0 if winner == "completion" else -1)
    assert len(queries.of_kind("UPDATE")) == 1
    assert len(queries.of_kind("SELECT")) == 1
    assert generation.generation == ("winner" if winner == "completion" else None)
    assert generation.seed == 123
    assert generation.gen_metadata == []
    assert generation.message == winning_message
    generation.get_gen_kudos.assert_not_called()
    generation.record.assert_not_called()
    generation.worker.record_performance.assert_not_called()
    generation.send_webhook.assert_not_called()


def test_accounting_failure_rolls_back_completion(pending_generation: TextProcessingGeneration) -> None:
    """Keep the claim and accounting writes in the same rollback boundary."""
    generation = pending_generation
    worker = generation.worker
    previous_kudos = worker.kudos

    def fail_accounting(things_per_sec: float, kudos: float) -> None:
        worker.kudos += kudos
        db.session.flush()
        raise RuntimeError("accounting failed")

    generation.record.side_effect = fail_accounting
    with pytest.raises(RuntimeError, match="accounting failed"):
        ProcessingGeneration.set_generation(generation, "result", 4.0)
    db.session.rollback()
    assert generation.generation is None
    assert generation.cancelled is True
    assert worker.kudos == previous_kudos
    worker.record_performance.assert_not_called()
    generation.send_webhook.assert_not_called()
