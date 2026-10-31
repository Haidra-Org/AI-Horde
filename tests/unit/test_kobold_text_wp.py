# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the text (kobold) ``TextWaitingPrompt`` decision logic.

The stable/image line has dedicated tests; the text line had none. The kudos and
upfront-requirement methods here are effectively pure given their inputs. They
read a handful of attributes off ``self`` and an env var, so we exercise the
branch matrix by binding each method to a lightweight stub rather than building a
full persisted ORM graph (``WaitingPrompt.__init__`` commits to the DB). This
keeps the test hermetic and fast while still locking the real arithmetic.

The allowance and queue wait records are read at request creation and at assignment, so their tests create
persisted requests and generations.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from horde import exceptions as e
from horde import metrics
from horde.apis.v2 import kobold as text_api_module
from horde.apis.v2.kobold import TextAsyncGenerate
from horde.classes.base.waiting_prompt import estimate_queue_wait_seconds
from horde.classes.kobold import processing_generation as text_processing_generation
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt, compute_free_output_allowance
from horde.classes.kobold.worker import TextWorker
from horde.config import Config
from horde.flask import db


def _wp(slow_workers=True, max_length=80, max_context_length=2048, workers=(), models=()):
    return SimpleNamespace(
        slow_workers=slow_workers,
        max_length=max_length,
        max_context_length=max_context_length,
        workers=list(workers),
        models=list(models),
    )


FREE_OUTPUT_CEILING = 4096
"""The default ceiling of the free output allowance, which the curve tests pass explicitly."""

FREE_OUTPUT_FLOOR = 512
"""The default floor of the free output allowance."""

FULL_WAIT_SECONDS = 60.0
"""The default longest estimated wait that still gives the ceiling."""

FLOOR_WAIT_SECONDS = 300.0
"""The default estimated wait from which the allowance is the floor."""


def _allowance(estimated_wait_seconds: float | None) -> int:
    return compute_free_output_allowance(
        estimated_wait_seconds=estimated_wait_seconds,
        ceiling=FREE_OUTPUT_CEILING,
        floor=FREE_OUTPUT_FLOOR,
        full_wait_seconds=FULL_WAIT_SECONDS,
        floor_wait_seconds=FLOOR_WAIT_SECONDS,
    )


class TestComputeFreeOutputAllowance:
    def test_no_wait_gives_the_ceiling(self):
        assert _allowance(0.0) == FREE_OUTPUT_CEILING

    def test_a_wait_up_to_the_full_threshold_gives_the_ceiling(self):
        assert _allowance(FULL_WAIT_SECONDS) == FREE_OUTPUT_CEILING

    def test_the_allowance_falls_linearly_between_the_thresholds(self):
        """Halfway between the thresholds the allowance is halfway between the ceiling and the floor."""
        halfway_wait = (FULL_WAIT_SECONDS + FLOOR_WAIT_SECONDS) / 2
        assert _allowance(halfway_wait) == (FREE_OUTPUT_CEILING + FREE_OUTPUT_FLOOR) / 2
        assert _allowance(120.0) == 3200
        assert FREE_OUTPUT_FLOOR < _allowance(FULL_WAIT_SECONDS + 1) < FREE_OUTPUT_CEILING

    def test_a_wait_at_the_floor_threshold_gives_the_floor(self):
        assert _allowance(FLOOR_WAIT_SECONDS) == FREE_OUTPUT_FLOOR

    def test_a_wait_beyond_the_floor_threshold_gives_the_floor(self):
        assert _allowance(FLOOR_WAIT_SECONDS * 10) == FREE_OUTPUT_FLOOR

    def test_no_eligible_worker_gives_the_floor(self):
        assert _allowance(None) == FREE_OUTPUT_FLOOR


class TestEstimateQueueWaitSeconds:
    def test_the_wait_is_the_queued_work_over_the_combined_throughput(self):
        assert estimate_queue_wait_seconds(queued_things=1200, queued_jobs=6, worker_threads=3, things_per_second=60) == 20

    def test_fewer_jobs_than_threads_count_only_the_threads_they_occupy(self):
        """One job on a three-thread pool runs at one thread's share of the combined throughput."""
        assert estimate_queue_wait_seconds(queued_things=200, queued_jobs=1, worker_threads=3, things_per_second=60) == 10

    def test_an_empty_queue_has_no_wait(self):
        assert estimate_queue_wait_seconds(queued_things=0, queued_jobs=0, worker_threads=3, things_per_second=60) == 0

    def test_no_throughput_divides_the_work_by_one(self):
        assert estimate_queue_wait_seconds(queued_things=80, queued_jobs=1, worker_threads=0, things_per_second=0) == 80


class TestRequireUpfrontKudos:
    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", FREE_OUTPUT_CEILING)
        monkeypatch.setattr(Config, "text_free_output_floor", FREE_OUTPUT_FLOOR)
        monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", FULL_WAIT_SECONDS)
        monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", FLOOR_WAIT_SECONDS)
        monkeypatch.delenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", raising=False)

    def test_a_short_wait_serves_up_to_the_ceiling_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=FREE_OUTPUT_CEILING), estimated_wait_seconds=0.0)
        assert result == (False, FREE_OUTPUT_CEILING, False)

    def test_over_the_allowance_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=FREE_OUTPUT_CEILING + 1), estimated_wait_seconds=0.0)
        assert result == (True, FREE_OUTPUT_CEILING, False)

    def test_a_long_wait_serves_only_the_floor_free(self):
        within_floor = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
        )
        over_floor = TextWaitingPrompt.require_upfront_kudos(
            _wp(max_length=FREE_OUTPUT_FLOOR + 1),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
        )
        assert within_floor == (False, FREE_OUTPUT_FLOOR, False)
        assert over_floor == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_eligible_worker_serves_only_the_floor_free(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=FREE_OUTPUT_FLOOR + 1), estimated_wait_seconds=None)
        assert result == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_slow_workers_always_requires_upfront(self):
        result = TextWaitingPrompt.require_upfront_kudos(_wp(slow_workers=False, max_length=80), estimated_wait_seconds=0.0)
        assert result == (True, FREE_OUTPUT_CEILING, False)

    def test_configured_ceiling_floor_and_thresholds_apply(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", 1024)
        monkeypatch.setattr(Config, "text_free_output_floor", 256)
        monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", 10.0)
        monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", 20.0)
        idle = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), estimated_wait_seconds=10.0)
        halfway = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), estimated_wait_seconds=15.0)
        busy = TextWaitingPrompt.require_upfront_kudos(_wp(max_length=1024), estimated_wait_seconds=20.0)
        assert idle == (False, 1024, False)
        assert halfway == (True, 640, False)
        assert busy == (True, 256, False)

    def test_workerlist_env_forces_upfront_and_blocks_downgrade(self, monkeypatch):
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = TextWaitingPrompt.require_upfront_kudos(
            _wp(slow_workers=True, max_length=200, workers=["w"]),
            estimated_wait_seconds=0.0,
        )
        assert result == (True, FREE_OUTPUT_CEILING, True)


class TestKudos:
    def test_extra_kudos_burn_adds_one(self):
        assert TextWaitingPrompt.calculate_extra_kudos_burn(SimpleNamespace(), 10) == 11

    def test_calculate_kudos_empty_model_list_uses_13b_assumption(self):
        # context_multiplier = 1.2 + 2.2**log2(2048/1024) = 3.4
        # round(80 * 13 * 3.4 / 100, 2) = 35.36
        assert TextWaitingPrompt.calculate_kudos(_wp(max_length=80, max_context_length=2048)) == pytest.approx(35.36)

    def test_calculate_kudos_matched_targeting_env_floors_to_min(self, monkeypatch):
        monkeypatch.setenv("HORDE_REQUIRE_MATCHED_TARGETING", "1")
        wp = _wp(workers=["w"])
        assert TextWaitingPrompt.calculate_kudos(wp) == 0.1


class TestCalculateKudos:
    def test_unknown_model_quotes_from_the_prompt_itself(self, monkeypatch):
        """The unknown-model branch prices the request from its own max_length; the object has no ``wp``."""
        from horde.classes.kobold import waiting_prompt as text_wp_module

        monkeypatch.setattr(text_wp_module.model_reference, "is_known_text_model", lambda model_name: False)
        wp = _wp(max_length=100, max_context_length=1024, models=(SimpleNamespace(model="not-in-reference"),))

        kudos = TextWaitingPrompt.calculate_kudos(wp)

        context_multiplier = 1.2 + (2.2**0)
        assert kudos == pytest.approx(100 * (2.7 / 100) * context_multiplier)


ALLOWANCE_MODEL = "allowance-model"
"""The model every request in the allowance records tests asks for."""


class _CounterRecorder:
    """Stands in for a metric counter and keeps every increment with its attributes."""

    def __init__(self) -> None:
        self.increments: list[tuple[int, dict[str, object]]] = []

    def add(self, amount: int, attributes: dict[str, object]) -> None:
        self.increments.append((amount, attributes))


class _HistogramRecorder:
    """Stands in for a metric histogram and keeps every recorded value with its attributes."""

    def __init__(self) -> None:
        self.records: list[tuple[float, dict[str, object]]] = []

    def record(self, amount: float, attributes: dict[str, object]) -> None:
        self.records.append((amount, attributes))


def _queue_allowance_request(
    monkeypatch: pytest.MonkeyPatch,
    user: Any,
    *,
    max_length: int,
    estimated_wait_seconds: float | None,
    reservation_granted: bool,
    allow_downgrade: bool = False,
    is_chat: bool = False,
    sharedkey: object | None = None,
) -> TextAsyncGenerate:
    """Create a text request through ``TextAsyncGenerate.initiate_waiting_prompt`` with the wait and the kudos
    reservation fixed, so each allowance outcome is reached deterministically.

    A request costs one kudo per token, so any request that needs kudos upfront needs a reservation.
    """
    chat_fields: dict[str, object] = (
        {
            "is_chat": True,
            "messages": [
                {
                    "role": "user",
                    "content": "Hello?",
                },
            ],
        }
        if is_chat
        else {}
    )
    resource = TextAsyncGenerate()
    resource.args = SimpleNamespace(
        allow_downgrade=allow_downgrade,
        dry_run=False,
        extra_source_images=None,
    )
    resource.models = [ALLOWANCE_MODEL]
    resource.user = user
    resource.username = user.get_unique_alias()
    resource.sharedkey = sharedkey
    resource.existing_style = None
    # The route sets this before any style is applied; the refusal path quotes the request through it.
    resource.style_kudos = False
    monkeypatch.setattr(
        resource,
        "get_waiting_prompt_arguments",
        lambda: {
            "worker_ids": [],
            "models": [ALLOWANCE_MODEL],
            "prompt": "allowance records test",
            "user_id": user.id,
            "params": {
                "n": 1,
                "max_length": max_length,
                "max_context_length": 2048,
            },
            **chat_fields,
        },
    )
    monkeypatch.setattr(resource, "estimate_competing_wait_seconds", lambda: estimated_wait_seconds)
    monkeypatch.setattr(
        text_api_module,
        "reserve_kudos",
        lambda *_args, **_kwargs: object() if reservation_granted else None,
    )
    resource.initiate_waiting_prompt()
    return resource


class TestFreeAllowanceRecords:
    """Every text request creation counts its allowance decision once, with the estimated wait it was sized by."""

    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Config, "text_free_output_ceiling", FREE_OUTPUT_CEILING)
        monkeypatch.setattr(Config, "text_free_output_floor", FREE_OUTPUT_FLOOR)
        monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", FULL_WAIT_SECONDS)
        monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", FLOOR_WAIT_SECONDS)
        monkeypatch.delenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", raising=False)
        monkeypatch.setattr(text_api_module.model_reference, "get_text_model_multiplier", lambda _model_name: 21)

    @pytest.fixture
    def decisions(self, monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
        recorder = _CounterRecorder()
        monkeypatch.setattr(metrics, "text_free_allowance_decisions", recorder)
        return recorder

    @pytest.fixture
    def estimated_waits(self, monkeypatch: pytest.MonkeyPatch) -> _HistogramRecorder:
        recorder = _HistogramRecorder()
        monkeypatch.setattr(metrics, "text_free_allowance_estimated_wait_seconds", recorder)
        return recorder

    def test_a_request_within_the_allowance_is_counted_free(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
        estimated_waits: _HistogramRecorder,
    ) -> None:
        """A registered user's 512-token request at no wait gets the 4096 ceiling and is counted ``free``."""
        _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=512,
            estimated_wait_seconds=0.0,
            reservation_granted=False,
        )

        assert decisions.increments == [
            (
                1,
                {
                    "horde.requester": "registered",
                    "horde.outcome": "free",
                    "horde.requested_bucket": "<=512",
                    "horde.allowance_bucket": "2049-4096",
                    "horde.chat": False,
                },
            ),
        ]
        assert estimated_waits.records == [
            (
                0.0,
                {
                    "horde.chat": False,
                    "horde.requester": "registered",
                },
            ),
        ]

    def test_a_request_over_the_allowance_with_kudos_reserved_is_counted_funded(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """A 6000-token request over the 4096 ceiling whose kudos are reserved is counted ``funded``."""
        _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=6000,
            estimated_wait_seconds=0.0,
            reservation_granted=True,
        )

        assert decisions.increments == [
            (
                1,
                {
                    "horde.requester": "registered",
                    "horde.outcome": "funded",
                    "horde.requested_bucket": ">4096",
                    "horde.allowance_bucket": "2049-4096",
                    "horde.chat": False,
                },
            ),
        ]

    def test_a_request_over_the_allowance_without_kudos_is_counted_refused(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """An anonymous 2048-token request at a 300 s wait, over the 512 floor without kudos and without downgrade, is
        refused with ``KudosUpfront`` and counted ``refused``."""
        with pytest.raises(e.KudosUpfront):
            _queue_allowance_request(
                monkeypatch,
                make_user(oauth_id="anon"),
                max_length=2048,
                estimated_wait_seconds=FLOOR_WAIT_SECONDS,
                reservation_granted=False,
            )

        assert decisions.increments == [
            (
                1,
                {
                    "horde.requester": "anonymous",
                    "horde.outcome": "refused",
                    "horde.requested_bucket": "1025-2048",
                    "horde.allowance_bucket": "<=512",
                    "horde.chat": False,
                },
            ),
        ]

    def test_a_downgraded_request_is_counted_downgraded_by_its_requested_length(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """A 1024-token request at a 300 s wait without kudos that allows downgrade is lowered to the 512 floor and
        counted ``downgraded`` with the length it asked for."""
        resource = _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=1024,
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
            reservation_granted=False,
            allow_downgrade=True,
        )

        assert resource.wp.max_length == FREE_OUTPUT_FLOOR
        assert decisions.increments == [
            (
                1,
                {
                    "horde.requester": "registered",
                    "horde.outcome": "downgraded",
                    "horde.requested_bucket": "513-1024",
                    "horde.allowance_bucket": "<=512",
                    "horde.chat": False,
                },
            ),
        ]

    def test_a_shared_key_chat_request_is_counted_by_its_requester_and_family(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """A chat request made with a shared key is counted with ``shared_key`` and ``horde.chat`` true."""
        unlimited_sharedkey = SimpleNamespace(
            id="unlimited-key",
            kudos=-1,
            is_job_within_limits=lambda **_limits: (True, None),
        )

        _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=512,
            estimated_wait_seconds=0.0,
            reservation_granted=False,
            is_chat=True,
            sharedkey=unlimited_sharedkey,
        )

        assert [(attributes["horde.requester"], attributes["horde.chat"]) for _, attributes in decisions.increments] == [
            ("shared_key", True),
        ]

    def test_the_wait_is_not_recorded_when_no_worker_is_eligible(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
        estimated_waits: _HistogramRecorder,
    ) -> None:
        """Without an eligible worker the allowance takes its floor; the decision is counted and no wait is recorded."""
        _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=512,
            estimated_wait_seconds=None,
            reservation_granted=False,
        )

        assert [attributes["horde.allowance_bucket"] for _, attributes in decisions.increments] == ["<=512"]
        assert estimated_waits.records == []


class TestQueueWaitRecord:
    """Every real assignment records the time from its request's creation to the pop."""

    def test_an_assignment_records_its_queue_wait(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A legacy 1500-token request created 30 s before its pop records about 30 s by requester, family and size."""
        recorder = _HistogramRecorder()
        monkeypatch.setattr(text_processing_generation, "text_queue_wait_seconds", recorder)
        user = make_user()
        worker = TextWorker(
            user_id=user.id,
            name=f"queue-wait-{uuid.uuid4().hex[:12]}",
            max_length=4096,
            max_context_length=4096,
            last_check_in=datetime.utcnow(),
        )
        db.session.add(worker)
        db.session.commit()
        request = TextWaitingPrompt(
            worker_ids=[],
            models=[ALLOWANCE_MODEL],
            prompt="queue wait test",
            user_id=user.id,
            params={
                "n": 1,
                "max_length": 1500,
                "max_context_length": 2048,
            },
        )
        request.created = datetime.utcnow() - timedelta(seconds=30)
        db.session.commit()

        TextProcessingGeneration(
            wp_id=request.id,
            worker_id=worker.id,
            model=ALLOWANCE_MODEL,
        )

        assert [attributes for _, attributes in recorder.records] == [
            {
                "horde.requester": "registered",
                "horde.chat": False,
                "horde.requested_bucket": "1025-2048",
            },
        ]
        assert 30 <= recorder.records[0][0] < 60

    def test_a_fake_generation_does_not_record_a_queue_wait(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fake generation handed to a tricked worker is not an assignment and is not recorded."""
        recorder = _HistogramRecorder()
        monkeypatch.setattr(text_processing_generation, "text_queue_wait_seconds", recorder)
        user = make_user()
        worker = TextWorker(
            user_id=user.id,
            name=f"queue-wait-{uuid.uuid4().hex[:12]}",
            max_length=4096,
            max_context_length=4096,
            last_check_in=datetime.utcnow(),
        )
        db.session.add(worker)
        db.session.commit()
        request = TextWaitingPrompt(
            worker_ids=[],
            models=[ALLOWANCE_MODEL],
            prompt="queue wait test",
            user_id=user.id,
            params={
                "n": 1,
                "max_length": 80,
                "max_context_length": 2048,
            },
        )

        TextProcessingGeneration(
            wp_id=request.id,
            worker_id=worker.id,
            model=ALLOWANCE_MODEL,
            fake=True,
        )

        assert recorder.records == []
