# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The lease of a text or chat job, its renewal by progress and the speed gate that decides who is offered the job.

A job is leased a flat base at pop. A chat job's progress post that adds output extends the lease to end the base
after the post, up to a cap measured from pop that depends on whether the worker's owner is trusted. The pop offers a
chat job first to the workers whose speed finishes its ``max_length`` inside the base lease after the preparation
allowance, and to a slower worker when no capable chat worker is online. A legacy text job is assigned whatever its
length, and an assignment over the worker's capacity is recorded with its outcome.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import fakeredis
import pytest
from flask.testing import FlaskClient
from horde_sdk.generation_parameters.text.chat_policy import ChatFeature
from horde_sdk.worker.chat_declaration import ChatImageTransport
from sqlalchemy import JSON, Text, create_engine, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from horde import chat_progress
from horde.chat_progress import StreamLease, chat_lease_key, read_stream_lease, write_stream_lease
from horde.classes.base.settings import HordeSettings
from horde.classes.base.sql_constructs import TextArrayContainsAll
from horde.classes.base.user import UserRoleTypes
from horde.classes.base.worker import WorkerModel
from horde.classes.kobold import processing_generation as text_processing_generation
from horde.classes.kobold.processing_generation import (
    TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS,
    UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS,
    LeaseRenewal,
    TextProcessingGeneration,
    renew_text_job_lease,
    renew_text_job_ttl,
    text_job_lease_cap,
)
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import (
    TEXT_JOB_LEASE_SECONDS,
    TEXT_JOB_PREPARATION_SECONDS,
    UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND,
    TextWorker,
    get_effective_text_worker_speed,
    get_text_worker_lease_capacity,
)
from horde.database import text_functions
from horde.flask import db
from horde.utils import hash_api_key
from tests.fixture_types import MakeUser, MakeUserRole

pytestmark = pytest.mark.unit

MODEL_NAME = "model_lease"
"""The model every request and worker in this module serves."""

GENERATION_SECONDS = TEXT_JOB_LEASE_SECONDS - TEXT_JOB_PREPARATION_SECONDS
"""The part of the base lease the speed gate measures generation against."""

WORKER_API_KEY = "worker-owner-plain-key"
"""The key a worker posts progress with; the stream lease holds the digest of its stored hash."""

PROGRESS_ROUTE = "/api/v2/generate/text/progress"

CHAT_MESSAGES = [{"role": "user", "content": "a unit-test prompt"}]
"""The conversation of every chat request in this module."""


class _CounterRecorder:
    """Stands in for a metric counter and keeps every increment with its attributes."""

    def __init__(self) -> None:
        self.increments: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.increments.append((amount, attributes))


class _HistogramRecorder:
    """Stands in for a metric histogram and keeps every recorded value with its attributes."""

    def __init__(self) -> None:
        self.records: list[tuple[float, dict[str, str]]] = []

    def record(self, amount: float, attributes: dict[str, str]) -> None:
        self.records.append((amount, attributes))


@pytest.fixture(autouse=True)
def _large_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the module's model the parameter count of a large model, whose normal-speed threshold is 3 tokens/s."""
    monkeypatch.setattr(
        "horde.classes.kobold.worker.model_reference.get_text_model_multiplier",
        lambda _model_name: 20,
    )


@pytest.fixture
def progress_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeStrictRedis:
    """Route the stream lease and the delta stream to an in-memory Redis."""
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(chat_progress, "_client", fake)
    monkeypatch.setattr(chat_progress, "_append_script", None)
    return fake


@pytest.fixture
def lua_redis(progress_redis: fakeredis.FakeStrictRedis) -> fakeredis.FakeStrictRedis:
    """The in-memory Redis, for tests that reach the append script; fakeredis runs Lua only with ``lupa``."""
    pytest.importorskip("lupa", reason="fakeredis runs Lua scripts only with lupa installed (fakeredis[lua]).")
    return progress_redis


@pytest.fixture
def no_rate_limit() -> Iterator[None]:
    """Lift the per-address limits for tests that post to the progress route."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
def renewal_counter(monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
    recorder = _CounterRecorder()
    monkeypatch.setattr(text_processing_generation, "text_lease_renewals", recorder)
    return recorder


def _make_worker(user: Any, *, speed: float | None = None, max_length: int = 4096) -> TextWorker:
    worker = TextWorker(
        user_id=user.id,
        name=f"lease-worker-{uuid.uuid4().hex[:12]}",
        max_length=max_length,
        max_context_length=32768,
        last_check_in=datetime.utcnow(),
        nsfw=True,
        allow_unsafe_ipaddr=True,
    )
    if speed is not None:
        worker.speed = speed
    db.session.add(worker)
    db.session.commit()
    db.session.add(WorkerModel(worker_id=worker.id, model=MODEL_NAME))
    db.session.commit()
    return worker


def _make_request(user: Any, max_length: int, *, is_chat: bool = False) -> TextWaitingPrompt:
    chat_fields: dict[str, Any] = (
        {
            "is_chat": True,
            "messages": CHAT_MESSAGES,
        }
        if is_chat
        else {}
    )
    waiting_prompt = TextWaitingPrompt(
        worker_ids=[],
        models=[MODEL_NAME],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={
            "n": 1,
            "max_length": max_length,
            "max_context_length": 2048,
        },
        **chat_fields,
    )
    # Activation is written directly, keeping the tests independent of the kudos machinery activate() carries.
    waiting_prompt.active = True
    waiting_prompt.validated_backends = False
    waiting_prompt.expiry = datetime.utcnow() + timedelta(minutes=10)
    db.session.commit()
    return waiting_prompt


def _declare_chat(worker: TextWorker, waiting_prompt: TextWaitingPrompt) -> None:
    """Declare chat jobs on ``worker`` with every feature ``waiting_prompt`` needs and limits above its own."""
    worker.accepts_chat_jobs = True
    worker.chat_model_id = MODEL_NAME
    worker.chat_max_length = 4096
    worker.chat_max_context_length = 32768
    worker.chat_features = list(waiting_prompt.chat_required_features or ())
    db.session.commit()


def _create_generation(
    make_user: MakeUser,
    *,
    max_length: int = 80,
    speed: float | None = None,
    is_chat: bool = False,
) -> TextProcessingGeneration:
    user = make_user()
    worker = _make_worker(user, speed=speed)
    waiting_prompt = _make_request(user, max_length, is_chat=is_chat)
    return TextProcessingGeneration(
        wp_id=waiting_prompt.id,
        worker_id=worker.id,
        model=MODEL_NAME,
    )


def _write_lease(generation: TextProcessingGeneration, *, started_at: float, trusted: bool = False) -> None:
    write_stream_lease(
        str(generation.id),
        request_id=str(generation.wp_id),
        worker_id=str(generation.worker_id),
        model=MODEL_NAME,
        owner_id=1,
        owner_api_key=hash_api_key(WORKER_API_KEY),
        max_length=512,
        job_ttl=generation.job_ttl,
        started_at=started_at,
        lease_cap=text_job_lease_cap(trusted),
        trusted=trusted,
        fake=False,
    )


def _stored_job_ttl(generation: TextProcessingGeneration) -> int:
    db.session.expire_all()
    return db.session.get(TextProcessingGeneration, generation.id).job_ttl


class TestBaseLease:
    """Every text and chat job is leased the same flat base at pop."""

    @pytest.mark.parametrize("max_length", [80, 16384])
    def test_a_job_is_leased_the_base_whatever_its_max_length(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        max_length: int,
    ) -> None:
        """The lease does not grow with the requested output, so a worker that never submits holds a job briefly."""
        generation = _create_generation(make_user, max_length=max_length)

        assert generation.job_ttl == TEXT_JOB_LEASE_SECONDS


class TestRenewalRule:
    """A progress post that adds output extends the lease to end the base after it, up to the cap from pop."""

    def test_a_post_extends_the_lease_to_end_the_base_after_it(self) -> None:
        """A post 100.2 seconds after pop extends the lease to 251 seconds, rounded up to whole seconds."""
        outcome, job_ttl = renew_text_job_ttl(100.2, TEXT_JOB_LEASE_SECONDS, TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS)

        assert outcome is LeaseRenewal.GRANTED
        assert job_ttl == 101 + TEXT_JOB_LEASE_SECONDS

    def test_a_trusted_workers_lease_stops_at_its_cap(self) -> None:
        """A trusted worker's lease is extended up to its cap and no further."""
        outcome, job_ttl = renew_text_job_ttl(500, 590, TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS)

        assert outcome is LeaseRenewal.GRANTED
        assert job_ttl == TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS

    def test_an_untrusted_workers_lease_stops_at_its_lower_cap(self) -> None:
        """A post that would extend an untrusted worker's lease past its cap extends it to the cap."""
        outcome, job_ttl = renew_text_job_ttl(100, TEXT_JOB_LEASE_SECONDS, UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS)

        assert outcome is LeaseRenewal.GRANTED
        assert job_ttl == UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS

    @pytest.mark.parametrize(
        "lease_cap",
        [
            TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS,
            UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS,
        ],
    )
    def test_a_post_after_the_cap_is_reached_does_not_extend_the_lease(self, lease_cap: int) -> None:
        """Once the lease reaches the cap, further posts leave it there."""
        outcome, job_ttl = renew_text_job_ttl(lease_cap - 20, lease_cap, lease_cap)

        assert outcome is LeaseRenewal.AT_CAP
        assert job_ttl == lease_cap

    def test_a_post_after_the_lease_lapsed_does_not_revive_it(self) -> None:
        """A lapsed lease stays lapsed, so the stale-generation thread aborts the job."""
        outcome, job_ttl = renew_text_job_ttl(TEXT_JOB_LEASE_SECONDS + 1, TEXT_JOB_LEASE_SECONDS, 600)

        assert outcome is LeaseRenewal.LAPSED
        assert job_ttl == TEXT_JOB_LEASE_SECONDS

    def test_a_post_within_the_same_second_as_the_last_extension_leaves_the_lease(self) -> None:
        """A lease that already ends the base after this second is not extended again."""
        outcome, job_ttl = renew_text_job_ttl(40.5, 41 + TEXT_JOB_LEASE_SECONDS, 600)

        assert outcome is LeaseRenewal.UNCHANGED
        assert job_ttl == 41 + TEXT_JOB_LEASE_SECONDS

    def test_the_cap_follows_the_owners_trust(self) -> None:
        """A trusted owner's worker is capped at 600 seconds from pop, an untrusted one's at 200."""
        assert text_job_lease_cap(True) == 600
        assert text_job_lease_cap(False) == 200


class TestRenewalWrites:
    """An extension is written to the generation and its stream lease, and counted."""

    def test_an_extension_is_written_to_the_generation_and_the_stream_lease(
        self,
        db_session: Session,
        fake_redis: object,
        progress_redis: fakeredis.FakeStrictRedis,
        renewal_counter: _CounterRecorder,
        make_user: MakeUser,
    ) -> None:
        """The generation's ``job_ttl`` and the stream lease both carry the extended lease."""
        generation = _create_generation(make_user)
        now = time.time()
        _write_lease(generation, started_at=now - 30, trusted=True)

        outcome = renew_text_job_lease(_read_lease(generation), now=now)

        assert outcome is LeaseRenewal.GRANTED
        assert _stored_job_ttl(generation) == 30 + TEXT_JOB_LEASE_SECONDS
        assert _read_lease(generation).job_ttl == 30 + TEXT_JOB_LEASE_SECONDS
        assert progress_redis.ttl(chat_lease_key(str(generation.id))) > TEXT_JOB_LEASE_SECONDS
        assert renewal_counter.increments == [(1, {"horde.outcome": "granted", "horde.worker_trusted": "true"})]

    def test_a_post_at_the_cap_is_counted_and_writes_nothing(
        self,
        db_session: Session,
        fake_redis: object,
        progress_redis: fakeredis.FakeStrictRedis,
        renewal_counter: _CounterRecorder,
        make_user: MakeUser,
    ) -> None:
        """A refused extension leaves the generation's ``job_ttl`` and is counted with the worker's trust."""
        generation = _create_generation(make_user)
        generation.job_ttl = UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS
        db.session.commit()
        now = time.time()
        _write_lease(generation, started_at=now - 120)

        outcome = renew_text_job_lease(_read_lease(generation), now=now)

        assert outcome is LeaseRenewal.AT_CAP
        assert _stored_job_ttl(generation) == UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS
        assert renewal_counter.increments == [(1, {"horde.outcome": "at_cap", "horde.worker_trusted": "false"})]

    def test_a_submitted_generation_is_not_extended(
        self,
        db_session: Session,
        fake_redis: object,
        progress_redis: fakeredis.FakeStrictRedis,
        renewal_counter: _CounterRecorder,
        make_user: MakeUser,
    ) -> None:
        """A post read before the submit does not extend the lease of a generation that is no longer pending."""
        generation = _create_generation(make_user)
        now = time.time()
        _write_lease(generation, started_at=now - 30)
        lease = _read_lease(generation)
        generation.generation = "finished reply"
        db.session.commit()

        outcome = renew_text_job_lease(lease, now=now)

        assert outcome is LeaseRenewal.UNCHANGED
        assert _stored_job_ttl(generation) == TEXT_JOB_LEASE_SECONDS
        assert renewal_counter.increments == []


class TestRenewalByProgressRoute:
    """The progress route extends the lease for a post that adds output and not for a repeat."""

    def test_only_a_post_that_adds_output_extends_the_lease(
        self,
        db_session: Session,
        fake_redis: object,
        lua_redis: fakeredis.FakeStrictRedis,
        no_rate_limit: None,
        client: FlaskClient,
        make_user: MakeUser,
    ) -> None:
        """A retried post repeats the last delta and does not extend the lease; the next new delta does."""
        generation = _create_generation(make_user)
        generation_key = chat_lease_key(str(generation.id))
        _write_lease(generation, started_at=time.time() - 10, trusted=True)

        first = _post_progress(client, generation, 1, "Bl")
        after_first = _stored_job_ttl(generation)
        # Moves the lease's start back, as if 30 more seconds passed, so any renewal would be visible.
        lua_redis.hset(generation_key, "started", time.time() - 40)
        retried = _post_progress(client, generation, 1, "Bl")
        after_retry = _stored_job_ttl(generation)
        second = _post_progress(client, generation, 2, "ue")
        after_second = _stored_job_ttl(generation)

        assert first.status_code == 200
        assert retried.status_code == 200
        assert second.status_code == 200
        assert 10 + TEXT_JOB_LEASE_SECONDS <= after_first <= 12 + TEXT_JOB_LEASE_SECONDS
        assert after_retry == after_first
        assert 40 + TEXT_JOB_LEASE_SECONDS <= after_second <= 42 + TEXT_JOB_LEASE_SECONDS


class TestStaleAbortMetric:
    """A job aborted because its lease lapsed is counted with its worker's trust and whether it is a chat job."""

    @pytest.fixture(autouse=True)
    def _settings_row(self, db_session: Session) -> None:
        """Seed the settings row the abort's raid-mode check reads."""
        if db_session.query(HordeSettings).first() is None:
            db_session.add(HordeSettings())
            db_session.flush()

    @pytest.mark.parametrize("is_chat", [False, True])
    def test_a_stale_abort_is_counted_by_trust_and_chat(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
        is_chat: bool,
    ) -> None:
        """The stale-generation thread's fault of a legacy text or chat job increments the stale-abort counter once."""
        recorder = _CounterRecorder()
        monkeypatch.setattr(text_processing_generation, "text_stale_aborts", recorder)
        generation = _create_generation(make_user, is_chat=is_chat)

        generation.fault_and_restart(stale=True)

        assert recorder.increments == [
            (
                1,
                {
                    "horde.worker_trusted": "false",
                    "horde.chat": is_chat,
                },
            ),
        ]

    def test_a_worker_fault_is_not_counted_as_stale(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker that reports a fault itself does not increment the stale-abort counter."""
        recorder = _CounterRecorder()
        monkeypatch.setattr(text_processing_generation, "text_stale_aborts", recorder)
        generation = _create_generation(make_user)

        generation.fault_and_restart()

        assert recorder.increments == []


def _make_capable_chat_worker(user: Any, waiting_prompt: TextWaitingPrompt, **declaration: Any) -> TextWorker:
    """Create an online chat worker at 50 tokens/s (capacity 6000) for ``waiting_prompt``, with ``declaration`` set
    on it afterwards."""
    worker = _make_worker(user, speed=50)
    _declare_chat(worker, waiting_prompt)
    for column_name, value in declaration.items():
        setattr(worker, column_name, value)
    db.session.commit()
    return worker


class TestSpeedGate:
    """A chat job goes first to the workers whose speed finishes its ``max_length`` inside the base lease.

    A slower worker is offered it when no capable chat worker for its model is online. A legacy text job is offered
    whatever its length.
    """

    def test_a_measured_worker_is_gated_by_its_own_speed(self) -> None:
        """A worker measured at 10 tokens/s finishes 1200 tokens in the 120 seconds the gate allows."""
        assert get_effective_text_worker_speed(10) == 10
        assert get_text_worker_lease_capacity(10) == 10 * GENERATION_SECONDS

    def test_an_unmeasured_worker_is_taken_at_the_models_normal_speed_threshold(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
    ) -> None:
        """A new worker stores the speed baseline and is gated as if it ran at the assumed unmeasured speed."""
        worker = _make_worker(make_user())

        assert get_effective_text_worker_speed(worker.speed) == UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND
        assert get_text_worker_lease_capacity(worker.speed) == UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND * GENERATION_SECONDS

    @pytest.mark.parametrize(
        ("is_chat", "max_length", "capable_worker_online", "offered"),
        [
            (True, 2 * GENERATION_SECONDS, True, True),
            (True, 2 * GENERATION_SECONDS + 1, True, False),
            (True, 2 * GENERATION_SECONDS + 1, False, True),
            (False, 2 * GENERATION_SECONDS, True, True),
            (False, 2 * GENERATION_SECONDS + 1, True, True),
        ],
    )
    def test_the_pop_and_the_eligibility_check_apply_the_same_gate(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        is_chat: bool,
        max_length: int,
        capable_worker_online: bool,
        offered: bool,
    ) -> None:
        """A worker at 2 tokens/s is refused a chat job a token longer than it finishes in time while a capable chat
        worker is online, is offered that job when none is, and is offered a legacy job of either length."""
        user = make_user()
        make_user_role(user, UserRoleTypes.TRUSTED, value=True)
        worker = _make_worker(user, speed=2)
        waiting_prompt = _make_request(user, max_length, is_chat=is_chat)
        _declare_chat(worker, waiting_prompt)
        if capable_worker_online:
            _make_capable_chat_worker(user, waiting_prompt)

        popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])
        eligibility = worker.can_generate(waiting_prompt)

        assert [wp.id for wp in popped] == ([waiting_prompt.id] if offered else [])
        assert eligibility == ((True, None) if offered else (False, "lease_speed"))

    @pytest.mark.parametrize(
        "capable_worker_state",
        [
            {"last_check_in": datetime.utcnow() - timedelta(minutes=10)},
            {"maintenance": True},
            {"paused": True},
            {"accepts_chat_jobs": False},
            {"chat_max_length": 256},
            {"chat_max_context_length": 1024},
            {"chat_model_id": "another_model"},
        ],
        ids=[
            "offline",
            "maintenance",
            "paused",
            "no_chat_jobs",
            "chat_max_length_below_the_request",
            "chat_max_context_length_below_the_request",
            "another_chat_model",
        ],
    )
    def test_a_faster_worker_that_cannot_take_the_chat_request_does_not_hold_it_back(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        capable_worker_state: dict[str, Any],
    ) -> None:
        """A chat request for 512 tokens reaches a worker at 2 tokens/s (capacity 240) when the only faster worker is
        offline, unavailable, or declares chat limits or a chat model that do not admit the request."""
        user = make_user()
        worker = _make_worker(user, speed=2)
        waiting_prompt = _make_request(user, 512, is_chat=True)
        _declare_chat(worker, waiting_prompt)
        _make_capable_chat_worker(user, waiting_prompt, **capable_worker_state)

        popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])

        assert [wp.id for wp in popped] == [waiting_prompt.id]
        assert worker.can_generate(waiting_prompt) == (True, None)

    @pytest.mark.parametrize(
        ("faster_worker_features", "slow_worker_offered"),
        [
            ([ChatFeature.SEED.value], True),
            ([ChatFeature.SEED.value, ChatFeature.TOOLS.value], False),
        ],
        ids=["faster_worker_lacks_tools", "faster_worker_declares_tools"],
    )
    def test_a_faster_worker_counts_as_capable_only_when_it_declares_every_required_feature(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        faster_worker_features: list[str],
        slow_worker_offered: bool,
    ) -> None:
        """A 512-token chat request that requires ``tools`` reaches a worker at 2 tokens/s (capacity 240) with
        ``tools`` when the only faster worker does not declare ``tools``, and is left to the faster worker when it
        does."""
        user = make_user()
        worker = _make_worker(user, speed=2)
        waiting_prompt = _make_request(user, 512, is_chat=True)
        waiting_prompt.chat_required_features = [ChatFeature.TOOLS.value]
        db.session.commit()
        _declare_chat(worker, waiting_prompt)
        _make_capable_chat_worker(user, waiting_prompt, chat_features=faster_worker_features)

        popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])

        assert [wp.id for wp in popped] == ([waiting_prompt.id] if slow_worker_offered else [])
        assert worker.can_generate(waiting_prompt) == ((True, None) if slow_worker_offered else (False, "lease_speed"))

    @pytest.mark.parametrize(
        ("faster_worker_transports", "slow_worker_offered"),
        [
            (None, True),
            ([], True),
            ([ChatImageTransport.MESSAGES_URL.value], True),
            ([ChatImageTransport.URL.value], False),
            ([ChatImageTransport.BASE64.value], False),
        ],
        ids=[
            "faster_worker_declares_no_transport",
            "faster_worker_declares_an_empty_transport_list",
            "faster_worker_declares_only_messages_url",
            "faster_worker_declares_url",
            "faster_worker_declares_base64",
        ],
    )
    def test_a_faster_vision_worker_counts_as_capable_only_with_an_image_transport(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        faster_worker_transports: list[str] | None,
        slow_worker_offered: bool,
    ) -> None:
        """A 512-token chat request that requires ``vision`` reaches a worker at 2 tokens/s (capacity 240) that
        receives images when the only faster worker declares ``vision`` without the ``url`` or ``base64`` transport,
        and is left to the faster worker when it declares one of them. ``messages_url`` alone delivers no image."""
        user = make_user()
        worker = _make_worker(user, speed=2)
        waiting_prompt = _make_request(user, 512, is_chat=True)
        waiting_prompt.chat_required_features = [ChatFeature.VISION.value]
        db.session.commit()
        _declare_chat(worker, waiting_prompt)
        worker.image_transports = [ChatImageTransport.URL.value]
        db.session.commit()
        _make_capable_chat_worker(user, waiting_prompt, image_transports=faster_worker_transports)

        popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])

        assert [wp.id for wp in popped] == ([waiting_prompt.id] if slow_worker_offered else [])
        assert worker.can_generate(waiting_prompt) == ((True, None) if slow_worker_offered else (False, "lease_speed"))

    def test_an_unmeasured_worker_counts_as_capable_at_the_assumed_speed(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
    ) -> None:
        """A chat worker without a recorded speed is taken at 10 tokens/s (capacity 1200), so it holds a 1200-token
        chat request back from a slower worker and does not hold back a 1201-token one."""
        user = make_user()
        slow_worker = _make_worker(user, speed=2)
        within_request = _make_request(user, 1200, is_chat=True)
        over_request = _make_request(user, 1201, is_chat=True)
        _declare_chat(slow_worker, within_request)
        unmeasured_worker = _make_worker(user)
        _declare_chat(unmeasured_worker, within_request)

        popped = text_functions.get_sorted_text_wp_filtered_to_worker(slow_worker, [MODEL_NAME])

        assert [wp.id for wp in popped] == [over_request.id]
        assert slow_worker.can_generate(within_request) == (False, "lease_speed")
        assert slow_worker.can_generate(over_request) == (True, None)

    def test_a_slow_worker_is_refused_a_chat_request_a_capable_worker_can_take_and_assigned_a_legacy_one(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """Of a chat and a legacy request for 512 tokens, a worker at 2 tokens/s (capacity 240) pops only the legacy
        one while a capable chat worker is online, and the assignment creates its generation."""
        user = make_user()
        make_user_role(user, UserRoleTypes.TRUSTED, value=True)
        worker = _make_worker(user, speed=2)
        chat_request = _make_request(user, 512, is_chat=True)
        legacy_request = _make_request(user, 512)
        _declare_chat(worker, chat_request)
        _make_capable_chat_worker(user, chat_request)

        popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])
        legacy_request.start_generation(worker, declared_models=[MODEL_NAME])

        generation_request_ids = [generation.wp_id for generation in db.session.query(TextProcessingGeneration).all()]
        assert [wp.id for wp in popped] == [legacy_request.id]
        assert worker.can_generate(chat_request) == (False, "lease_speed")
        assert generation_request_ids == [legacy_request.id]

    def test_only_a_chat_declaration_above_the_capacity_makes_a_pop_capacity_limited(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
    ) -> None:
        """A pop is counted as capacity-limited only when the worker's capacity is below the chat length it declares.
        A text limit above the capacity does not count, since legacy text jobs are not gated."""
        user = make_user()
        chat_request = _make_request(user, 80, is_chat=True)
        fast_chat_worker = _make_worker(user, speed=50, max_length=4096)
        slow_chat_worker = _make_worker(user, speed=2, max_length=4096)
        slow_text_worker = _make_worker(user, speed=2, max_length=4096)
        _declare_chat(fast_chat_worker, chat_request)
        _declare_chat(slow_chat_worker, chat_request)

        assert not text_functions.worker_may_be_speed_gated(fast_chat_worker, [MODEL_NAME])
        assert text_functions.worker_may_be_speed_gated(slow_chat_worker, [MODEL_NAME])
        assert not text_functions.worker_may_be_speed_gated(slow_text_worker, [MODEL_NAME])


class TestGateShadow:
    """A text or chat job assigned over its worker's lease capacity is marked at assignment and counted with its
    outcome."""

    @pytest.fixture(autouse=True)
    def _settings_row(self, db_session: Session) -> None:
        """Seed the settings row the abort's raid-mode check reads."""
        if db_session.query(HordeSettings).first() is None:
            db_session.add(HordeSettings())
            db_session.flush()

    @pytest.fixture
    def assignments(self, monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
        recorder = _CounterRecorder()
        monkeypatch.setattr(text_processing_generation, "text_gate_shadow_assignments", recorder)
        return recorder

    @pytest.fixture
    def outcomes(self, monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
        recorder = _CounterRecorder()
        monkeypatch.setattr(text_processing_generation, "text_gate_shadow_outcomes", recorder)
        return recorder

    @pytest.fixture
    def service_seconds(self, monkeypatch: pytest.MonkeyPatch) -> _HistogramRecorder:
        recorder = _HistogramRecorder()
        monkeypatch.setattr(text_processing_generation, "text_gate_shadow_service_seconds", recorder)
        return recorder

    def test_an_over_capacity_legacy_assignment_is_marked_and_counted(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        assignments: _CounterRecorder,
    ) -> None:
        """A 512-token legacy job assigned to a worker at 2 tokens/s (capacity 240) stores the marker and is counted
        once by trust and size."""
        generation = _create_generation(make_user, max_length=512, speed=2)

        db.session.expire_all()
        stored = db.session.get(TextProcessingGeneration, generation.id)
        assert stored.over_capacity_at_assignment is True
        assert assignments.increments == [
            (
                1,
                {
                    "horde.worker_trusted": "false",
                    "horde.size_bucket": "<=512",
                    "horde.chat": False,
                },
            ),
        ]

    def test_an_over_capacity_chat_assignment_is_marked_and_counted_as_chat(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        assignments: _CounterRecorder,
        outcomes: _CounterRecorder,
    ) -> None:
        """A 512-token chat job assigned to a worker at 2 tokens/s (capacity 240), the only chat worker online, stores
        the marker, and its assignment and stale abort are counted with ``horde.chat`` true."""
        generation = _create_generation(make_user, max_length=512, speed=2, is_chat=True)

        generation.fault_and_restart(stale=True)

        db.session.expire_all()
        stored = db.session.get(TextProcessingGeneration, generation.id)
        assert stored is not None
        expected_attributes = {
            "horde.worker_trusted": "false",
            "horde.size_bucket": "<=512",
            "horde.chat": True,
        }
        assert stored.over_capacity_at_assignment is True
        assert assignments.increments == [(1, expected_attributes)]
        assert outcomes.increments == [
            (
                1,
                {
                    **expected_attributes,
                    "horde.outcome": "aborted",
                },
            ),
        ]

    def test_its_completed_submit_records_the_outcome_and_the_service_time(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        outcomes: _CounterRecorder,
        service_seconds: _HistogramRecorder,
    ) -> None:
        """A stored submit of an over-capacity legacy job counts ``completed`` and records its pop to submit time."""
        generation = _create_generation(make_user, max_length=1500, speed=2)

        generation.set_generation("a unit-test reply", things_per_sec=2, seed=1)

        expected_attributes = {
            "horde.worker_trusted": "false",
            "horde.size_bucket": "1025-2048",
            "horde.chat": False,
        }
        assert outcomes.increments == [
            (
                1,
                {
                    **expected_attributes,
                    "horde.outcome": "completed",
                },
            ),
        ]
        assert [attributes for _, attributes in service_seconds.records] == [expected_attributes]
        assert service_seconds.records[0][0] >= 0

    def test_its_faulted_submit_records_the_outcome_once(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        outcomes: _CounterRecorder,
        service_seconds: _HistogramRecorder,
    ) -> None:
        """A fault submit of an over-capacity legacy job counts ``faulted``; a repeated fault submit is not counted."""
        generation = _create_generation(make_user, max_length=512, speed=2)

        generation.set_generation("", things_per_sec=0, state="faulted")
        generation.set_generation("", things_per_sec=0, state="faulted")

        assert [attributes["horde.outcome"] for _, attributes in outcomes.increments] == ["faulted"]
        assert service_seconds.records == []

    def test_its_stale_abort_records_the_outcome(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        outcomes: _CounterRecorder,
    ) -> None:
        """The stale-generation thread's abort of an over-capacity legacy job counts ``aborted``."""
        generation = _create_generation(make_user, max_length=4096, speed=2)

        generation.fault_and_restart(stale=True)

        assert outcomes.increments == [
            (
                1,
                {
                    "horde.worker_trusted": "false",
                    "horde.size_bucket": "2049-4096",
                    "horde.chat": False,
                    "horde.outcome": "aborted",
                },
            ),
        ]

    def test_an_under_capacity_assignment_is_not_recorded(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        assignments: _CounterRecorder,
        outcomes: _CounterRecorder,
        service_seconds: _HistogramRecorder,
    ) -> None:
        """A 240-token legacy job on a worker at 2 tokens/s fits its capacity, so neither its assignment nor its submit
        is marked or counted."""
        generation = _create_generation(make_user, max_length=2 * GENERATION_SECONDS, speed=2)

        generation.set_generation("a unit-test reply", things_per_sec=2, seed=1)

        assert generation.over_capacity_at_assignment is False
        assert assignments.increments == []
        assert outcomes.increments == []
        assert service_seconds.records == []


def _read_lease(generation: TextProcessingGeneration) -> StreamLease:
    lease = read_stream_lease(str(generation.id))
    assert lease is not None
    return lease


def _post_progress(client: FlaskClient, generation: TextProcessingGeneration, seq: int, content: str) -> Any:
    return client.post(
        PROGRESS_ROUTE,
        json={
            "id": str(generation.id),
            "seq": seq,
            "delta": {"content": content},
        },
        headers={"apikey": WORKER_API_KEY},
    )


class TestTrustedOnlyRequests:
    """A request for trusted workers only is paged only to workers whose owner is trusted."""

    def test_a_trusted_only_request_is_paged_only_to_a_trusted_owners_worker(
        self,
        db_session: Session,
        fake_redis: object,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """The pop query of a worker whose owner is not trusted does not page a trusted-only request, so the request
        takes no slot in that worker's page. Once the owner is trusted, the same worker pages and can take it."""
        user = make_user()
        worker = _make_worker(user)
        waiting_prompt = _make_request(user, 80)
        waiting_prompt.trusted_workers = True
        db.session.commit()

        untrusted_popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])
        untrusted_verdict = worker.can_generate(waiting_prompt)
        make_user_role(user, UserRoleTypes.TRUSTED, value=True)
        db.session.expire_all()
        trusted_popped = text_functions.get_sorted_text_wp_filtered_to_worker(worker, [MODEL_NAME])
        trusted_verdict = worker.can_generate(waiting_prompt)

        assert [wp.id for wp in untrusted_popped] == []
        assert untrusted_verdict == (False, "untrusted")
        assert [wp.id for wp in trusted_popped] == [waiting_prompt.id]
        assert trusted_verdict == (True, None)


_FEATURE_CONTAINMENT_CASES = [
    pytest.param(["seed", "tools"], ["tools"], True, id="holds_the_required_feature"),
    pytest.param(["seed"], ["tools"], False, id="lacks_the_required_feature"),
    pytest.param(["tools"], ["seed", "tools"], False, id="lacks_one_of_two"),
    pytest.param(["seed"], [], True, id="nothing_required"),
    pytest.param(["seed"], None, True, id="null_required"),
    pytest.param(None, ["tools"], False, id="null_held"),
    pytest.param(None, None, True, id="both_null"),
]
"""Held and required feature arrays, with whether the held array contains every required feature."""


class TestTextArrayContainsAll:
    """The feature containment of the capable worker test gives the same result on PostgreSQL and SQLite."""

    @pytest.mark.parametrize(("held", "required", "contained"), _FEATURE_CONTAINMENT_CASES)
    def test_postgres_containment(
        self,
        db_session: Session,
        held: list[str] | None,
        required: list[str] | None,
        contained: bool,
    ) -> None:
        statement = select(
            TextArrayContainsAll(literal(held, type_=ARRAY(Text)), literal(required, type_=ARRAY(Text))),
        )

        assert db_session.scalar(statement) is contained

    @pytest.mark.parametrize(("held", "required", "contained"), _FEATURE_CONTAINMENT_CASES)
    def test_sqlite_containment(self, held: list[str] | None, required: list[str] | None, contained: bool) -> None:
        """A JSON ``null``, which a JSON column stores for Python None, holds no feature, as SQL NULL does."""
        statement = select(TextArrayContainsAll(literal(held, type_=JSON), literal(required, type_=JSON)))
        engine = create_engine("sqlite://")

        with engine.connect() as connection:
            assert bool(connection.scalar(statement)) is contained
