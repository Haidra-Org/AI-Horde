# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify completion SQL and identity-map state against real PostgreSQL rows.

Call the base completion method on text/chat instances to isolate its claim and
commit from the text-specific state handling. Accounting and post-commit hooks are
observed separately, while the completion UPDATE, flush and transaction remain real.
The submit statistics are real rows, so their transaction and counts are checked
against the database.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from unittest.mock import Mock

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import horde.classes.base.processing_generation as processing_generation_module
import horde.classes.base.worker as worker_module
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.base.settings import HordeSettings
from horde.classes.base.stats import FulfillmentPerformance, ModelPerformance
from horde.classes.base.user import UserSuspicions
from horde.classes.base.worker import WorkerPerformance, WorkerSuspicionEvent, WorkerSuspicions
from horde.classes.kobold.genstats import TextGenerationStatistic
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.enums import ImageGenState
from horde.flask import db
from horde.suspicions import Suspicions
from tests.conftest import _QueryRecorder
from tests.fixture_types import MakeUser

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
    monkeypatch.setattr(worker, "record_performance", Mock(wraps=worker.record_performance))
    monkeypatch.setattr(prompt, "is_completed", Mock(return_value=False))
    return generation


@dataclass(frozen=True)
class StatisticCounts:
    """Represents how many rows of each submit statistic exist for one generation's model, worker and type."""

    model_performances: int
    """Rows in ``model_performances`` for the generation's model."""
    fulfilments: int
    """Rows in ``horde_fulfillments`` for the generation's type."""
    worker_performances: int
    """Rows in ``worker_performances`` for the generation's worker."""
    generation_statistics: int
    """Rows in ``text_gen_stats`` for the generation's model."""


NO_STATISTICS = StatisticCounts(model_performances=0, fulfilments=0, worker_performances=0, generation_statistics=0)
"""The statistic counts of a submission that does not store anything."""

ONE_OF_EACH_STATISTIC = StatisticCounts(model_performances=1, fulfilments=1, worker_performances=1, generation_statistics=1)
"""The statistic counts of one stored submission."""

SUBMIT_STATISTIC_INSERTS = 4
"""The INSERTs of a stored text submission's statistics: one row of each kind in ``StatisticCounts``."""


def count_statistics(generation: TextProcessingGeneration) -> StatisticCounts:
    """Return the stored submit statistics for the generation's model, type and worker, read in a separate session."""
    with Session(db.engine) as reader:
        return StatisticCounts(
            model_performances=reader.query(ModelPerformance).filter_by(model=generation.model).count(),
            fulfilments=reader.query(FulfillmentPerformance).filter_by(thing_type=generation.procgen_type).count(),
            worker_performances=reader.query(WorkerPerformance).filter_by(worker_id=generation.worker_id).count(),
            generation_statistics=reader.query(TextGenerationStatistic).filter_by(model=generation.model).count(),
        )


def statistics_added(before: StatisticCounts, after: StatisticCounts) -> StatisticCounts:
    """Return the rows added between two readings of ``count_statistics``."""
    return StatisticCounts(
        model_performances=after.model_performances - before.model_performances,
        fulfilments=after.fulfilments - before.fulfilments,
        worker_performances=after.worker_performances - before.worker_performances,
        generation_statistics=after.generation_statistics - before.generation_statistics,
    )


def stored_generation_text(generation: TextProcessingGeneration) -> str | None:
    """Return the generation column as committed, read in a separate session."""
    with Session(db.engine) as reader:
        stored_text: str | None = reader.query(TextProcessingGeneration.generation).filter_by(id=generation.id).scalar()
        return stored_text


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
    generation.worker.record_performance.assert_called_once_with(4.0, commit=False)
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
    """Keep the winning result, and settle and record its statistics once, when a submission is retried."""
    generation = pending_generation
    statistics_before = count_statistics(generation)
    ProcessingGeneration.set_generation(generation, "winner", 4.0, seed=123, gen_metadata=[])
    assert statistics_added(statistics_before, count_statistics(generation)) == ONE_OF_EACH_STATISTIC
    with assert_query_count() as queries:
        result = ProcessingGeneration.set_generation(generation, "loser", 9.0, seed=456, gen_metadata=None)
        db.session.commit()
    assert result == 0
    assert len(queries.of_kind("UPDATE")) == 1
    assert len(queries.of_kind("SELECT")) == 1
    assert not queries.of_kind("INSERT")
    assert statistics_added(statistics_before, count_statistics(generation)) == ONE_OF_EACH_STATISTIC
    assert (generation.generation, generation.seed, generation.gen_metadata) == ("winner", 123, [])
    generation.get_gen_kudos.assert_called_once()
    generation.record.assert_called_once_with(4.0, 12.5)
    generation.worker.record_performance.assert_called_once_with(4.0, commit=False)
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
    statistics_before = count_statistics(generation)
    with assert_query_count() as queries:
        result = ProcessingGeneration.set_generation(
            generation, "loser", 9.0, seed=456, gen_metadata=None, message={"role": "assistant", "content": "loser"}
        )
        db.session.commit()
    assert result == (0 if winner == "completion" else -1)
    assert len(queries.of_kind("UPDATE")) == 1
    assert len(queries.of_kind("SELECT")) == 1
    assert not queries.of_kind("INSERT")
    assert statistics_added(statistics_before, count_statistics(generation)) == NO_STATISTICS
    assert generation.generation == ("winner" if winner == "completion" else None)
    assert generation.seed == 123
    assert generation.gen_metadata == []
    assert generation.message == winning_message
    generation.get_gen_kudos.assert_not_called()
    generation.record.assert_not_called()
    generation.worker.record_performance.assert_not_called()
    generation.send_webhook.assert_not_called()


def test_accounting_failure_rolls_back_completion(pending_generation: TextProcessingGeneration) -> None:
    """Keep the claim and accounting writes in the same rollback boundary, and do not record statistics for them."""
    generation = pending_generation
    worker = generation.worker
    previous_kudos = worker.kudos
    statistics_before = count_statistics(generation)

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
    assert statistics_added(statistics_before, count_statistics(generation)) == NO_STATISTICS


def test_stored_submission_records_its_statistics_in_one_transaction_after_settlement(
    pending_generation: TextProcessingGeneration, assert_query_count: QueryCounter
) -> None:
    """Commit the claim and settlement first, then every statistic of the submission once, in one transaction."""
    generation = pending_generation
    statistics_before = count_statistics(generation)
    with assert_query_count() as queries:
        reward = ProcessingGeneration.set_generation(generation, "result", 4.0, submitted_things=30)
    assert reward == 12.5
    settlement, statistics = queries.commits
    assert "UPDATE" in settlement.statement_kinds
    assert statistics.statement_kinds == ["INSERT"] * SUBMIT_STATISTIC_INSERTS
    assert statistics_added(statistics_before, count_statistics(generation)) == ONE_OF_EACH_STATISTIC
    with Session(db.engine) as reader:
        newest_fulfilment = reader.query(FulfillmentPerformance).order_by(FulfillmentPerformance.id.desc()).first()
        newest_model_performance = reader.query(ModelPerformance).filter_by(model=generation.model).one()
        newest_statistic = reader.query(TextGenerationStatistic).filter_by(model=generation.model).one()
    assert newest_fulfilment.things == 30
    assert newest_model_performance.performance == 4.0
    assert newest_statistic.state == ImageGenState.OK


def test_statistics_failure_keeps_the_settlement_and_a_retry_records_nothing(
    pending_generation: TextProcessingGeneration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the settled submission when its statistics transaction fails, and treat a retry as a duplicate.

    The failure is logged without raising. None of the submission's statistics are stored, since they share the
    failed transaction. The repeated submit is refused as a duplicate, so it does not settle again or record the
    statistics.
    """
    generation = pending_generation
    statistics_before = count_statistics(generation)
    errors: list[str] = []
    monkeypatch.setattr(processing_generation_module.logger, "error", lambda message, *args, **kwargs: errors.append(message))
    # A statistic without its required model fails the statistics INSERT in the database.
    statistic_without_model = TextGenerationStatistic(model=None, max_length=80, max_context_length=1024, prompt_length=1)
    monkeypatch.setattr(generation, "build_generation_statistic", Mock(return_value=statistic_without_model))

    reward = ProcessingGeneration.set_generation(generation, "result", 4.0)

    assert reward == 12.5
    assert stored_generation_text(generation) == "result"
    generation.record.assert_called_once_with(4.0, 12.5)
    generation.send_webhook.assert_called_once_with(12.5)
    assert statistics_added(statistics_before, count_statistics(generation)) == NO_STATISTICS
    assert len(errors) == 1
    assert "Submit statistics insert failed after settlement" in errors[0]
    assert str(generation.id) in errors[0]

    monkeypatch.setattr(generation, "build_generation_statistic", partial(TextProcessingGeneration.build_generation_statistic, generation))
    retry_reward = ProcessingGeneration.set_generation(generation, "result", 4.0)
    db.session.commit()

    assert retry_reward == 0
    generation.record.assert_called_once_with(4.0, 12.5)
    generation.send_webhook.assert_called_once_with(12.5)
    assert statistics_added(statistics_before, count_statistics(generation)) == NO_STATISTICS


def test_faulted_submission_keeps_one_faulted_statistic(pending_generation: TextProcessingGeneration) -> None:
    """Record a worker's fault by one FAULTED generation statistic and nothing else, even when the fault is resent."""
    generation = pending_generation
    # A cancelled generation is recorded as CANCELLED, so the fixture's flag is cleared.
    generation.cancelled = False
    # The worker's dropped-job count reads raid mode from the settings row.
    if db.session.query(HordeSettings).first() is None:
        db.session.add(HordeSettings())
    db.session.commit()
    statistics_before = count_statistics(generation)

    first_result = generation.set_generation("", things_per_sec=0, state="faulted")
    repeated_result = generation.set_generation("", things_per_sec=0, state="faulted")

    assert (first_result, repeated_result) == (-1, -1)
    assert statistics_added(statistics_before, count_statistics(generation)) == StatisticCounts(
        model_performances=0,
        fulfilments=0,
        worker_performances=0,
        generation_statistics=1,
    )
    with Session(db.engine) as reader:
        faulted_statistic = reader.query(TextGenerationStatistic).filter_by(model=generation.model).one()
    assert faulted_statistic.state == ImageGenState.FAULTED
    generation.record.assert_not_called()


@pytest.mark.parametrize(
    ("submitted_text", "state"),
    [
        ("", "ok"),
        ("a censored reply", "censored"),
    ],
)
def test_empty_and_censored_submissions_keep_their_statistics(
    pending_generation: TextProcessingGeneration,
    submitted_text: str,
    state: str,
) -> None:
    """Record the statistics of an empty or censored text submission like those of any other stored submission."""
    generation = pending_generation
    statistics_before = count_statistics(generation)

    reward = generation.set_generation(submitted_text, things_per_sec=4.0, state=state)

    assert reward == 12.5
    assert stored_generation_text(generation) == submitted_text
    assert generation.censored is (state == "censored")
    generation.record.assert_called_once_with(4.0, 12.5)
    assert statistics_added(statistics_before, count_statistics(generation)) == ONE_OF_EACH_STATISTIC


FAST_TEXT_THINGS_PER_SEC = 200.0
"""A text speed above the text suspicion threshold of 150 things per second."""


def count_suspicions(generation: TextProcessingGeneration) -> tuple[int, int, int, bool]:
    """Return the worker's suspicions, its suspicion events, its owner's suspicions and its pause, as committed."""
    owner_id = generation.worker.user_id
    with Session(db.engine) as reader:
        worker_suspicions = reader.query(WorkerSuspicions).filter_by(worker_id=generation.worker_id).count()
        worker_events = reader.query(WorkerSuspicionEvent).filter_by(worker_id=generation.worker_id).count()
        owner_suspicions = reader.query(UserSuspicions).filter_by(user_id=owner_id).count()
        paused: bool = reader.query(TextWorker.paused).filter_by(id=generation.worker_id).scalar()
    return worker_suspicions, worker_events, owner_suspicions, paused


def test_fast_submission_commits_its_suspicion_with_the_settlement(
    pending_generation: TextProcessingGeneration, assert_query_count: QueryCounter
) -> None:
    """Commit the suspicion of an unreasonably fast submission in the settlement transaction, not on its own."""
    generation = pending_generation
    generation.record.side_effect = partial(TextProcessingGeneration.record, generation)

    with assert_query_count() as queries:
        reward = ProcessingGeneration.set_generation(generation, "result", FAST_TEXT_THINGS_PER_SEC)

    assert reward == 12.5
    settlement, statistics = queries.commits
    assert "UPDATE" in settlement.statement_kinds
    assert statistics.statement_kinds == ["INSERT"] * SUBMIT_STATISTIC_INSERTS
    assert count_suspicions(generation) == (1, 1, 1, False)


def test_fast_submission_suspicion_and_pause_roll_back_with_a_failed_settlement(
    pending_generation: TextProcessingGeneration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back the suspicion and the automatic pause of a fast submission together with its failed settlement."""
    generation = pending_generation
    worker = generation.worker
    pause_notification = Mock()
    monkeypatch.setattr(worker_module, "send_pause_notification", pause_notification)
    # One suspicion short of the worker threshold, so the fast submission pauses the worker.
    for _ in range(worker.suspicion_threshold - 1):
        db.session.add(WorkerSuspicions(worker_id=worker.id, suspicion_id=int(Suspicions.UNREASONABLY_FAST)))
    db.session.commit()
    statistics_before = count_statistics(generation)
    generation.record.side_effect = partial(TextProcessingGeneration.record, generation)
    monkeypatch.setattr(generation.wp, "record_usage", Mock(side_effect=RuntimeError("usage failed")))

    with pytest.raises(RuntimeError, match="usage failed"):
        ProcessingGeneration.set_generation(generation, "result", FAST_TEXT_THINGS_PER_SEC)
    db.session.rollback()

    pause_notification.assert_called_once()
    assert count_suspicions(generation) == (worker.suspicion_threshold - 1, 0, 0, False)
    assert stored_generation_text(generation) is None
    assert statistics_added(statistics_before, count_statistics(generation)) == NO_STATISTICS
