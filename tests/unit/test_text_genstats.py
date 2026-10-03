# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the statistic row stored for a finished text generation.

The row is written after the generation and its kudos payout are committed, so a
failed insert is rolled back and logged without failing the worker's submit.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from horde.classes.kobold import genstats as genstats_module
from horde.classes.kobold.genstats import TextGenerationStatistic, record_text_statistic
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.flask import db

pytestmark = pytest.mark.unit

_MODEL = "elinas/chronos-70b-v2"


def _make_text_wp(user: Any, *, style_id: uuid.UUID | None = None) -> TextWaitingPrompt:
    wp = TextWaitingPrompt(
        [],
        [_MODEL],
        prompt="a unit-test text prompt",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 1024},
        style_id=style_id,
    )
    db.session.commit()
    return wp


def _make_text_worker(user: Any) -> TextWorker:
    worker = TextWorker(user_id=user.id, name=f"genstats_worker_{user.id}", bridge_agent="AI Horde Worker:24:https://example.invalid")
    db.session.add(worker)
    db.session.commit()
    worker.set_models([_MODEL])
    return worker


class TestStatisticWriteIsBestEffort:
    """The statistic is written after the payout commits, so its failure must not reach the submit."""

    @staticmethod
    def _capture_errors(monkeypatch) -> list[str]:
        errors: list[str] = []
        monkeypatch.setattr(genstats_module.logger, "error", lambda message, *args, **kwargs: errors.append(message))
        return errors

    def test_a_failed_insert_is_rolled_back_and_logged_without_raising(self, fake_redis, make_user, monkeypatch) -> None:
        user = make_user()
        wp = _make_text_wp(user)
        worker = _make_text_worker(user)
        procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model=_MODEL)
        db.session.commit()
        procgen_id, wp_id, worker_id = procgen.id, wp.id, worker.id
        rows_before = db.session.query(TextGenerationStatistic).count()
        errors = self._capture_errors(monkeypatch)
        rollbacks: list[bool] = []
        real_rollback = db.session.rollback

        def _failing_commit() -> None:
            raise OperationalError("INSERT INTO text_gen_stats", {}, Exception("connection lost"))

        def _recording_rollback() -> None:
            rollbacks.append(True)
            real_rollback()

        with monkeypatch.context() as session_patch:
            session_patch.setattr(db.session, "commit", _failing_commit)
            session_patch.setattr(db.session, "rollback", _recording_rollback)
            record_text_statistic(procgen)

        assert rollbacks == [True]
        assert len(errors) == 1
        assert str(procgen_id) in errors[0]
        assert str(wp_id) in errors[0]
        assert str(worker_id) in errors[0]
        assert db.session.query(TextGenerationStatistic).count() == rows_before

    def test_a_successful_insert_stores_the_row_and_logs_nothing(self, fake_redis, make_user, monkeypatch) -> None:
        user = make_user()
        wp = _make_text_wp(user)
        procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=_make_text_worker(user).id, model=_MODEL)
        db.session.commit()
        rows_before = db.session.query(TextGenerationStatistic).count()
        errors = self._capture_errors(monkeypatch)

        record_text_statistic(procgen)

        assert errors == []
        assert db.session.query(TextGenerationStatistic).count() == rows_before + 1


class TestStatisticStyle:
    """The statistic records the style its request ran under."""

    @staticmethod
    def _newest_statistic_style(procgen: TextProcessingGeneration) -> uuid.UUID | None:
        record_text_statistic(procgen)
        newest = db.session.query(TextGenerationStatistic).order_by(TextGenerationStatistic.id.desc()).first()
        return newest.style_id

    def test_a_styled_request_records_its_style(self, fake_redis, make_user) -> None:
        user = make_user()
        style_id = uuid.uuid4()
        wp = _make_text_wp(user, style_id=style_id)
        procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=_make_text_worker(user).id, model=_MODEL)
        db.session.commit()

        assert self._newest_statistic_style(procgen) == style_id

    def test_an_unstyled_request_records_no_style(self, fake_redis, make_user) -> None:
        user = make_user()
        wp = _make_text_wp(user)
        procgen = TextProcessingGeneration(wp_id=wp.id, worker_id=_make_text_worker(user).id, model=_MODEL)
        db.session.commit()

        assert self._newest_statistic_style(procgen) is None
