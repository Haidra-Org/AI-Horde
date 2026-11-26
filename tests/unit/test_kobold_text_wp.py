# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the text (kobold) ``TextWaitingPrompt`` decision logic.

The stable/image line has dedicated tests; the text line had none. The kudos and
upfront-requirement methods here are effectively pure given their inputs. They
read a handful of attributes off ``self`` and an env var, so we exercise the
branch matrix by binding each method to a lightweight stub rather than building a
full ORM graph (``WaitingPrompt.__init__`` needs a database session). This
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
from horde import vars as hv
from horde.apis.v2 import kobold as text_api_module
from horde.apis.v2.kobold import TextAsyncGenerate
from horde.chat_contract import ChatImpossibleReason
from horde.classes.base.waiting_prompt import estimate_queue_wait_seconds
from horde.classes.kobold import processing_generation as text_processing_generation
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.request_fit import CHARS_PER_TOKEN_ESTIMATE, ContextFit
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt, UpfrontKudosDecision, compute_free_allowance
from horde.classes.kobold.worker import TextWorker
from horde.config import Config
from horde.database.functions import RequestWorkerAvailability
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

FREE_INPUT_CEILING = 16384
"""The default ceiling of the free input allowance."""

FREE_INPUT_FLOOR = 4096
"""The default floor of the free input allowance."""


def _prompt_of(tokens: int) -> str:
    """Return a prompt whose estimated token count is ``tokens``."""
    return "x" * (tokens * CHARS_PER_TOKEN_ESTIMATE)


def _set_default_allowance_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Config, "text_free_output_ceiling", FREE_OUTPUT_CEILING)
    monkeypatch.setattr(Config, "text_free_output_floor", FREE_OUTPUT_FLOOR)
    monkeypatch.setattr(Config, "text_free_input_ceiling", FREE_INPUT_CEILING)
    monkeypatch.setattr(Config, "text_free_input_floor", FREE_INPUT_FLOOR)
    monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", FULL_WAIT_SECONDS)
    monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", FLOOR_WAIT_SECONDS)
    monkeypatch.delenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", raising=False)


def _allowance(estimated_wait_seconds: float | None) -> int:
    return compute_free_allowance(
        estimated_wait_seconds=estimated_wait_seconds,
        ceiling=FREE_OUTPUT_CEILING,
        floor=FREE_OUTPUT_FLOOR,
        full_wait_seconds=FULL_WAIT_SECONDS,
        floor_wait_seconds=FLOOR_WAIT_SECONDS,
    )


class TestComputeFreeAllowance:
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


def _decide(wp: SimpleNamespace, *, estimated_wait_seconds: float | None, estimated_prompt_tokens: int = 0) -> UpfrontKudosDecision:
    return TextWaitingPrompt.require_upfront_kudos(
        wp,
        estimated_wait_seconds=estimated_wait_seconds,
        estimated_prompt_tokens=estimated_prompt_tokens,
    )


def _output_summary(decision: UpfrontKudosDecision) -> tuple[bool, int, bool]:
    """Return whether the decision holds kudos, its output allowance and whether it bars a downgrade."""
    return (decision.needs_kudos, decision.output_allowance, decision.disable_downgrade)


class TestRequireUpfrontKudos:
    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch):
        _set_default_allowance_settings(monkeypatch)

    def test_a_short_wait_serves_up_to_the_ceiling_free(self):
        result = _decide(_wp(max_length=FREE_OUTPUT_CEILING), estimated_wait_seconds=0.0)
        assert _output_summary(result) == (False, FREE_OUTPUT_CEILING, False)

    def test_over_the_allowance_requires_upfront(self):
        result = _decide(_wp(max_length=FREE_OUTPUT_CEILING + 1), estimated_wait_seconds=0.0)
        assert _output_summary(result) == (True, FREE_OUTPUT_CEILING, False)
        assert result.exceeds_output is True
        assert result.exceeds_input is False

    def test_a_long_wait_serves_only_the_floor_free(self):
        within_floor = _decide(_wp(max_length=FREE_OUTPUT_FLOOR), estimated_wait_seconds=FLOOR_WAIT_SECONDS)
        over_floor = _decide(_wp(max_length=FREE_OUTPUT_FLOOR + 1), estimated_wait_seconds=FLOOR_WAIT_SECONDS)
        assert _output_summary(within_floor) == (False, FREE_OUTPUT_FLOOR, False)
        assert _output_summary(over_floor) == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_eligible_worker_serves_only_the_floor_free(self):
        result = _decide(_wp(max_length=FREE_OUTPUT_FLOOR + 1), estimated_wait_seconds=None)
        assert _output_summary(result) == (True, FREE_OUTPUT_FLOOR, False)

    def test_no_slow_workers_always_requires_upfront(self):
        result = _decide(_wp(slow_workers=False, max_length=80), estimated_wait_seconds=0.0)
        assert _output_summary(result) == (True, FREE_OUTPUT_CEILING, False)

    def test_configured_ceiling_floor_and_thresholds_apply(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_output_ceiling", 1024)
        monkeypatch.setattr(Config, "text_free_output_floor", 256)
        monkeypatch.setattr(Config, "text_free_output_wait_full_seconds", 10.0)
        monkeypatch.setattr(Config, "text_free_output_wait_floor_seconds", 20.0)
        idle = _decide(_wp(max_length=1024), estimated_wait_seconds=10.0)
        halfway = _decide(_wp(max_length=1024), estimated_wait_seconds=15.0)
        busy = _decide(_wp(max_length=1024), estimated_wait_seconds=20.0)
        assert _output_summary(idle) == (False, 1024, False)
        assert _output_summary(halfway) == (True, 640, False)
        assert _output_summary(busy) == (True, 256, False)

    def test_workerlist_env_forces_upfront_and_blocks_downgrade(self, monkeypatch):
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = _decide(_wp(slow_workers=True, max_length=200, workers=["w"]), estimated_wait_seconds=0.0)
        assert _output_summary(result) == (True, FREE_OUTPUT_CEILING, True)

    def test_workerlist_env_does_not_block_a_downgrade_for_an_allowance_overage(self, monkeypatch):
        """The worker list bars a downgrade only when it is the sole reason for the hold."""
        monkeypatch.setenv("HORDE_UPFRONT_KUDOS_ON_WORKERLIST", "1")
        result = _decide(_wp(max_length=FREE_OUTPUT_CEILING + 1, workers=["w"]), estimated_wait_seconds=0.0)
        assert _output_summary(result) == (True, FREE_OUTPUT_CEILING, False)


class TestRequireUpfrontKudosForInput:
    """The prompt tokens processed by a worker are measured against the free input allowance on the same wait curve."""

    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch):
        _set_default_allowance_settings(monkeypatch)

    def test_a_short_wait_processes_up_to_the_input_ceiling_free(self):
        """A 16384-token prompt in a context with room for it is free at no wait."""
        result = _decide(
            _wp(max_length=512, max_context_length=32768),
            estimated_wait_seconds=0.0,
            estimated_prompt_tokens=FREE_INPUT_CEILING,
        )
        assert result.needs_kudos is False
        assert result.input_allowance == FREE_INPUT_CEILING
        assert result.processed_prompt_tokens == FREE_INPUT_CEILING

    def test_a_prompt_over_the_input_allowance_requires_upfront(self):
        """A 16385-token prompt with a short output needs kudos for its input alone."""
        result = _decide(
            _wp(max_length=80, max_context_length=32768),
            estimated_wait_seconds=0.0,
            estimated_prompt_tokens=FREE_INPUT_CEILING + 1,
        )
        assert result.needs_kudos is True
        assert result.exceeds_input is True
        assert result.exceeds_output is False
        assert result.disable_downgrade is False

    def test_a_long_wait_processes_only_the_input_floor_free(self):
        within_floor = _decide(
            _wp(max_length=80, max_context_length=32768),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
            estimated_prompt_tokens=FREE_INPUT_FLOOR,
        )
        over_floor = _decide(
            _wp(max_length=80, max_context_length=32768),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
            estimated_prompt_tokens=FREE_INPUT_FLOOR + 1,
        )
        assert (within_floor.needs_kudos, within_floor.input_allowance) == (False, FREE_INPUT_FLOOR)
        assert (over_floor.needs_kudos, over_floor.input_allowance) == (True, FREE_INPUT_FLOOR)

    def test_no_eligible_worker_processes_only_the_input_floor_free(self):
        result = _decide(
            _wp(max_length=80, max_context_length=32768),
            estimated_wait_seconds=None,
            estimated_prompt_tokens=FREE_INPUT_FLOOR + 1,
        )
        assert (result.needs_kudos, result.input_allowance) == (True, FREE_INPUT_FLOOR)

    def test_a_prompt_longer_than_its_context_is_measured_at_the_context(self):
        """A 50000-token prompt in a 2048 context with 512 to generate is measured at the 1536 tokens kept by the worker."""
        result = _decide(
            _wp(max_length=512, max_context_length=2048),
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
            estimated_prompt_tokens=50_000,
        )
        assert result.processed_prompt_tokens == 1536
        assert result.needs_kudos is False

    def test_the_input_allowance_falls_linearly_between_the_thresholds(self):
        halfway_wait = (FULL_WAIT_SECONDS + FLOOR_WAIT_SECONDS) / 2
        result = _decide(_wp(), estimated_wait_seconds=halfway_wait)
        assert result.input_allowance == (FREE_INPUT_CEILING + FREE_INPUT_FLOOR) / 2

    def test_configured_input_ceiling_and_floor_apply(self, monkeypatch):
        monkeypatch.setattr(Config, "text_free_input_ceiling", 8192)
        monkeypatch.setattr(Config, "text_free_input_floor", 1024)
        idle = _decide(_wp(), estimated_wait_seconds=0.0)
        busy = _decide(_wp(), estimated_wait_seconds=FLOOR_WAIT_SECONDS)
        assert (idle.input_allowance, busy.input_allowance) == (8192, 1024)


class TestDowngrade:
    """A downgrade lowers the request to the exceeded allowances and allows slow workers."""

    def _downgradable_wp(self, *, max_length: int, max_context_length: int) -> SimpleNamespace:
        wp = _wp(slow_workers=False, max_length=max_length, max_context_length=max_context_length)
        wp.id = "downgrade-test"
        wp.n = 2
        wp.things = max_length
        wp.total_usage = round(max_length * wp.n / hv.thing_divisors["text"], 2)
        wp.params = {"max_length": max_length, "max_context_length": max_context_length}
        wp.gen_payload = dict(wp.params)
        return wp

    def test_an_output_downgrade_lowers_the_queued_work(self, monkeypatch):
        """The queue estimate sums things, so a downgraded request counts at its lowered length."""
        monkeypatch.setattr(db.session, "commit", lambda: None)
        wp = self._downgradable_wp(max_length=6000, max_context_length=32768)

        TextWaitingPrompt.downgrade(wp, 4096)

        assert wp.things == 4096
        assert wp.total_usage == round(4096 * 2 / hv.thing_divisors["text"], 2)

    def test_an_input_only_downgrade_leaves_the_queued_work(self, monkeypatch):
        monkeypatch.setattr(db.session, "commit", lambda: None)
        wp = self._downgradable_wp(max_length=256, max_context_length=32768)

        TextWaitingPrompt.downgrade(wp, 512, 16384)

        assert (wp.things, wp.max_context_length) == (256, 16640)

    def test_an_output_downgrade_leaves_the_context(self, monkeypatch):
        monkeypatch.setattr(db.session, "commit", lambda: None)
        wp = self._downgradable_wp(max_length=2048, max_context_length=8192)

        TextWaitingPrompt.downgrade(wp, 512)

        assert (wp.max_length, wp.max_context_length, wp.slow_workers) == (512, 8192, True)
        assert wp.gen_payload == {"max_length": 512, "max_context_length": 8192}

    def test_an_input_downgrade_leaves_room_for_the_input_allowance_beside_the_output(self, monkeypatch):
        """A 32768 context with 2048 to generate, lowered to a 512 output and a 4096 input allowance, runs in 4608."""
        monkeypatch.setattr(db.session, "commit", lambda: None)
        wp = self._downgradable_wp(max_length=2048, max_context_length=32768)

        TextWaitingPrompt.downgrade(wp, 512, 4096)

        assert (wp.max_length, wp.max_context_length) == (512, 4608)
        assert wp.params == {"max_length": 512, "max_context_length": 4608}
        assert wp.gen_payload == {"max_length": 512, "max_context_length": 4608}

    def test_an_input_downgrade_never_raises_the_context(self, monkeypatch):
        monkeypatch.setattr(db.session, "commit", lambda: None)
        wp = self._downgradable_wp(max_length=512, max_context_length=2048)

        TextWaitingPrompt.downgrade(wp, 512, 4096)

        assert wp.max_context_length == 2048


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

ONE_ELIGIBLE_WORKER = RequestWorkerAvailability(
    worker_count=1,
    thread_count=1,
)
"""The availability of a request one online worker can serve, which keeps a kudos refusal a ``KudosUpfront``."""

NO_ELIGIBLE_WORKER = RequestWorkerAvailability(
    worker_count=0,
    thread_count=0,
)
"""The availability of a request no online worker can serve, without a declaration gap."""

VISION_GAP = RequestWorkerAvailability(
    worker_count=0,
    thread_count=0,
    impossible_reason=ChatImpossibleReason(
        missing_declarations=("vision",),
    ),
)
"""The availability of a request with an image that no online worker serving the model declares ``vision`` for."""


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
    availability: RequestWorkerAvailability = ONE_ELIGIBLE_WORKER,
    prompt: str = "allowance records test",
    max_context_length: int = 2048,
    context_fit: ContextFit = ContextFit.IGNORE,
) -> TextAsyncGenerate:
    """Create a text request through ``TextAsyncGenerate.initiate_waiting_prompt`` with the wait, the eligible workers
    and the kudos reservation fixed, so each allowance outcome is reached deterministically.

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
    # Validation sets both before the request is created, and the input allowance is measured from them.
    resource.prompt = prompt
    resource.context_fit = context_fit
    monkeypatch.setattr(
        resource,
        "get_waiting_prompt_arguments",
        lambda: {
            "worker_ids": [],
            "models": [ALLOWANCE_MODEL],
            "prompt": prompt,
            "user_id": user.id,
            "params": {
                "n": 1,
                "max_length": max_length,
                "max_context_length": max_context_length,
            },
            **chat_fields,
        },
    )
    monkeypatch.setattr(resource, "estimate_competing_wait_seconds", lambda: estimated_wait_seconds)
    monkeypatch.setattr(text_api_module.database, "get_worker_availability_for_request", lambda _wp: availability)
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
        _set_default_allowance_settings(monkeypatch)
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
                    "horde.prompt_bucket": "<=4096",
                    "horde.exceeded": "none",
                    "horde.context_lowered": False,
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
                    "horde.prompt_bucket": "<=4096",
                    "horde.exceeded": "output",
                    "horde.context_lowered": False,
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
                    "horde.prompt_bucket": "<=4096",
                    "horde.exceeded": "output",
                    "horde.context_lowered": False,
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
                    "horde.prompt_bucket": "<=4096",
                    "horde.exceeded": "output",
                    "horde.context_lowered": False,
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


class TestFreeInputAllowance:
    """A request whose processed prompt is over the free input allowance needs kudos held upfront, whatever its output."""

    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_default_allowance_settings(monkeypatch)
        monkeypatch.setattr(text_api_module.model_reference, "get_text_model_multiplier", lambda _model_name: 21)

    @pytest.fixture
    def decisions(self, monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
        recorder = _CounterRecorder()
        monkeypatch.setattr(metrics, "text_free_allowance_decisions", recorder)
        return recorder

    def test_an_anonymous_long_prompt_is_refused_stating_the_input_allowance(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """An anonymous 20000-token prompt with 80 tokens to generate, at no wait, is refused ``KudosUpfront``. The
        message states the 16384-token input allowance and the prompt's size, and the decision is counted as an input
        overage."""
        with pytest.raises(e.KudosUpfront) as refusal:
            _queue_allowance_request(
                monkeypatch,
                make_user(oauth_id="anon"),
                max_length=80,
                estimated_wait_seconds=0.0,
                reservation_granted=False,
                prompt=_prompt_of(20_000),
                max_context_length=32768,
            )

        message = refusal.value.specific
        assert f"prompts of up to {FREE_INPUT_CEILING} tokens" in message
        assert "This request's prompt is about 20000 tokens." in message
        assert db.session.query(TextWaitingPrompt).count() == 0
        assert [
            (attributes["horde.outcome"], attributes["horde.exceeded"], attributes["horde.prompt_bucket"])
            for _, attributes in decisions.increments
        ] == [
            ("refused", "input", "16385-32768"),
        ]

    def test_a_long_prompt_with_kudos_reserved_is_funded(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        resource = _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=80,
            estimated_wait_seconds=0.0,
            reservation_granted=True,
            prompt=_prompt_of(20_000),
            max_context_length=32768,
        )

        assert resource.wp.max_context_length == 32768
        assert [attributes["horde.outcome"] for _, attributes in decisions.increments] == ["funded"]

    def test_a_long_prompt_sent_under_ignore_with_downgrade_allowed_is_lowered_to_the_input_allowance(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """At a 300 s wait, a 20000-token prompt with 80 to generate in a 32768 context is lowered to a 4176 context,
        which is the 4096-token input floor plus the 80 tokens to generate."""
        resource = _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=80,
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
            reservation_granted=False,
            allow_downgrade=True,
            prompt=_prompt_of(20_000),
            max_context_length=32768,
        )

        assert (resource.wp.max_length, resource.wp.max_context_length) == (80, FREE_INPUT_FLOOR + 80)
        assert resource.wp.params["max_context_length"] == FREE_INPUT_FLOOR + 80
        assert [
            (attributes["horde.outcome"], attributes["horde.exceeded"], attributes["horde.context_lowered"])
            for _, attributes in decisions.increments
        ] == [
            ("downgraded", "input", True),
        ]

    def test_an_output_downgrade_under_ignore_keeps_the_processed_prompt_within_the_input_allowance(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """At a short wait, a 40000-token prompt with 16384 to generate in a 32768 context processes 16384 prompt
        tokens, which is within the input ceiling. Lowering max_length to the 4096 output ceiling would leave 28672
        tokens of the context to the prompt, so the context is lowered to 20480, the input ceiling plus 4096."""
        resource = _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=16384,
            estimated_wait_seconds=0.0,
            reservation_granted=False,
            allow_downgrade=True,
            prompt=_prompt_of(40_000),
            max_context_length=32768,
        )

        assert (resource.wp.max_length, resource.wp.max_context_length) == (
            FREE_OUTPUT_CEILING,
            FREE_INPUT_CEILING + FREE_OUTPUT_CEILING,
        )
        assert [
            (attributes["horde.outcome"], attributes["horde.exceeded"], attributes["horde.context_lowered"])
            for _, attributes in decisions.increments
        ] == [
            ("downgraded", "output", True),
        ]

    def test_a_long_prompt_sized_by_grow_is_refused_even_with_downgrade_allowed(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """Lowering the context would make the worker cut the prompt, which a client sizing its context with ``grow``
        did not accept, so the request is refused rather than downgraded."""
        with pytest.raises(e.KudosUpfront):
            _queue_allowance_request(
                monkeypatch,
                make_user(),
                max_length=80,
                estimated_wait_seconds=0.0,
                reservation_granted=False,
                allow_downgrade=True,
                prompt=_prompt_of(20_000),
                max_context_length=32768,
                context_fit=ContextFit.GROW,
            )

        assert [attributes["horde.outcome"] for _, attributes in decisions.increments] == ["refused"]

    def test_a_long_prompt_in_a_small_context_is_free(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """Under ``ignore`` the worker keeps only what fits a 2048 context, so a 50000-token prompt is free at the
        longest wait."""
        _queue_allowance_request(
            monkeypatch,
            make_user(oauth_id="anon"),
            max_length=512,
            estimated_wait_seconds=FLOOR_WAIT_SECONDS,
            reservation_granted=False,
            prompt=_prompt_of(50_000),
        )

        assert [(attributes["horde.outcome"], attributes["horde.prompt_bucket"]) for _, attributes in decisions.increments] == [
            ("free", "<=4096"),
        ]


class TestNoEligibleWorkerRefusal:
    """A request the kudos rules would refuse while no online worker can serve it is refused ``NoEligibleWorker``, with
    the declaration gap in its message and body when there is one."""

    @pytest.fixture(autouse=True)
    def default_allowance_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_default_allowance_settings(monkeypatch)
        monkeypatch.setattr(text_api_module.model_reference, "get_text_model_multiplier", lambda _model_name: 21)

    @pytest.fixture
    def decisions(self, monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
        recorder = _CounterRecorder()
        monkeypatch.setattr(metrics, "text_free_allowance_decisions", recorder)
        return recorder

    def test_an_anonymous_request_over_the_floor_with_a_declaration_gap_is_refused_with_the_gap(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """An anonymous 2048-token chat request that no online worker declares ``vision`` for is refused 403
        ``NoEligibleWorker`` stating the gap, its body carries ``impossible_reason``, its row is deleted, and the
        decision is counted ``refused``."""
        with pytest.raises(e.NoEligibleWorker) as refusal:
            _queue_allowance_request(
                monkeypatch,
                make_user(oauth_id="anon"),
                max_length=2048,
                estimated_wait_seconds=None,
                reservation_granted=False,
                is_chat=True,
                availability=VISION_GAP,
            )

        body, status_code = e.handle_bad_requests(refusal.value)
        assert status_code == 403
        assert body == {
            "message": "No online worker can serve this request: no online worker serving the model declares vision.",
            "rc": "NoEligibleWorker",
            "impossible_reason": {
                "kind": "declaration_gap",
                "missing_declarations": ["vision"],
                "exceeded_limits": [],
            },
        }
        assert db.session.query(TextWaitingPrompt).count() == 0
        assert [attributes["horde.outcome"] for _, attributes in decisions.increments] == ["refused"]

    def test_an_anonymous_request_over_the_floor_without_a_worker_or_gap_is_refused_without_a_gap(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """An anonymous 2048-token request for a model no online worker serves is refused 403 ``NoEligibleWorker``
        with only ``message`` and ``rc`` in its body."""
        with pytest.raises(e.NoEligibleWorker) as refusal:
            _queue_allowance_request(
                monkeypatch,
                make_user(oauth_id="anon"),
                max_length=2048,
                estimated_wait_seconds=None,
                reservation_granted=False,
                availability=NO_ELIGIBLE_WORKER,
            )

        body, status_code = e.handle_bad_requests(refusal.value)
        assert status_code == 403
        assert body == {
            "message": "No online worker can serve this request.",
            "rc": "NoEligibleWorker",
        }
        assert db.session.query(TextWaitingPrompt).count() == 0

    def test_a_shared_key_without_kudos_and_without_a_worker_is_refused_no_eligible_worker(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """A request whose shared key cannot cover it, for a model no online worker serves, is refused
        ``NoEligibleWorker`` in place of ``SharedKeyInsufficientKudos``."""
        empty_sharedkey = SimpleNamespace(
            id="empty-key",
            kudos=0,
            is_job_within_limits=lambda **_limits: (True, None),
        )

        with pytest.raises(e.NoEligibleWorker):
            _queue_allowance_request(
                monkeypatch,
                make_user(),
                max_length=2048,
                estimated_wait_seconds=None,
                reservation_granted=False,
                sharedkey=empty_sharedkey,
                availability=NO_ELIGIBLE_WORKER,
            )

        assert db.session.query(TextWaitingPrompt).count() == 0

    def test_an_anonymous_request_over_the_floor_with_an_eligible_worker_is_refused_for_kudos(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """An anonymous 2048-token request at a 300 s wait that one online worker can serve is refused
        ``KudosUpfront``."""
        with pytest.raises(e.KudosUpfront) as refusal:
            _queue_allowance_request(
                monkeypatch,
                make_user(oauth_id="anon"),
                max_length=2048,
                estimated_wait_seconds=FLOOR_WAIT_SECONDS,
                reservation_granted=False,
                availability=ONE_ELIGIBLE_WORKER,
            )

        assert refusal.value.rc == "KudosUpfront"

    def test_a_funded_request_without_an_eligible_worker_is_created(
        self,
        db_session: Any,
        fake_redis: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
        decisions: _CounterRecorder,
    ) -> None:
        """A registered user's 2048-token request whose kudos are reserved is created and counted ``funded`` although
        no online worker can serve it."""
        resource = _queue_allowance_request(
            monkeypatch,
            make_user(),
            max_length=2048,
            estimated_wait_seconds=None,
            reservation_granted=True,
            availability=VISION_GAP,
        )

        assert db.session.query(TextWaitingPrompt).filter_by(id=resource.wp.id).count() == 1
        assert [attributes["horde.outcome"] for _, attributes in decisions.increments] == ["funded"]


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
