# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Streamed chat requests end to end: the stream hint, the lease written at pop and the worker progress route."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import fakeredis
import pytest

from horde import chat_progress, chat_signal
from horde.chat_progress import (
    CHAT_PROGRESS_LEASE_GRACE_SECONDS,
    chat_lease_key,
    chat_stream_key,
)
from tests.fixture_types import MakeApiUser
from tests.integration._fake_chat_worker import (
    FakeChatWorker,
    build_chat_submit_body,
    build_faulted_chat_submit_body,
)

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from werkzeug.test import TestResponse

CHAT_MODEL = "elinas/chronos-70b-v2"
WORKER_NAME = "CICD Fake Streaming Scribe"
CHAT_MESSAGES = [
    {"role": "user", "content": "Name one colour."},
]
PROGRESS_ROUTE = "/api/v2/generate/text/progress"


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address limits, which this module's request volume would otherwise trip."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
def progress_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeStrictRedis:
    """Route the stream lease, the delta stream and the chat signals to one in-memory Redis."""
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(chat_progress, "_client", fake)
    monkeypatch.setattr(chat_progress, "_append_script", None)
    monkeypatch.setattr(chat_signal, "_publisher", fake)
    return fake


@pytest.fixture
def lua_redis(progress_redis: fakeredis.FakeStrictRedis) -> fakeredis.FakeStrictRedis:
    """The in-memory Redis, for tests that reach the append script; fakeredis runs Lua only with ``lupa``."""
    pytest.importorskip("lupa", reason="fakeredis runs Lua scripts only with lupa installed (fakeredis[lua]).")
    return progress_redis


@pytest.fixture
def chat_worker(client: FlaskClient, request_headers: dict[str, str]) -> FakeChatWorker:
    """A worker serving the chat model, owned by the requesting user."""
    return FakeChatWorker(client=client, headers=request_headers, name=WORKER_NAME, models=[CHAT_MODEL])


def _submit_chat_request(client: FlaskClient, request_headers: dict[str, str], **overrides: Any) -> str:
    chat_body: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": CHAT_MESSAGES,
    }
    chat_body.update(overrides)
    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    request_id: str = response.get_json()["id"]
    return request_id


def _post_progress(
    client: FlaskClient,
    apikey: str,
    generation_id: str,
    seq: int,
    content: str,
    reasoning_content: str | None = None,
) -> TestResponse:
    delta = {"content": content}
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    return client.post(
        PROGRESS_ROUTE,
        json={
            "id": generation_id,
            "seq": seq,
            "delta": delta,
        },
        headers={"apikey": apikey},
    )


def test_a_streamed_chat_request_stores_the_hint_and_its_pop_carries_it_with_a_lease(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers, stream=True)

    pop_result = chat_worker.pop()

    assert pop_result["payload"]["stream_progress"] is True
    with app.app_context():
        chat_request = db.session.get(TextWaitingPrompt, uuid.UUID(request_id))
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert chat_request is not None
        assert generation is not None
        assert chat_request.chat_extras == {"stream_progress": True}
        worker_id = str(generation.worker_id)
        model = generation.model
        owner_id = generation.worker.user_id
        job_ttl = generation.job_ttl
    lease = progress_redis.hgetall(chat_lease_key(pop_result["id"]))
    assert (lease["request"], lease["worker"], lease["model"], lease["owner"]) == (request_id, worker_id, model, str(owner_id))
    assert job_ttl < progress_redis.ttl(chat_lease_key(pop_result["id"])) <= job_ttl + CHAT_PROGRESS_LEASE_GRACE_SECONDS


def test_a_chat_request_that_does_not_stream_gets_no_hint_and_no_lease(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    _submit_chat_request(client, request_headers)

    pop_result = chat_worker.pop()

    assert "stream_progress" not in pop_result["payload"]
    assert not progress_redis.exists(chat_lease_key(pop_result["id"]))


def test_progress_for_a_generation_without_a_lease_is_refused_as_not_streaming(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    response = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Blue")

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "ChatProgressNotStreaming"


def test_progress_for_a_streamed_generation_faulted_for_a_lapsed_lease_is_refused_as_aborted(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    """The stale-generation fault deletes a streamed generation's lease, and the next post returns ``ChatJobAborted``
    so its worker stops generating and does not submit."""
    from datetime import datetime, timedelta

    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    request_id = progress_redis.hget(chat_lease_key(pop_result["id"]), "request")
    assert request_id is not None
    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert generation is not None
        generation.start_time = datetime.utcnow() - timedelta(seconds=generation.job_ttl + 1)
        db.session.commit()
        assert generation.is_stale()
        generation.fault_and_restart(stale=True)

    response = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Blue")
    client.delete(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json() == {
        "message": f"Generation {pop_result['id']} was aborted; stop generating and do not submit.",
        "rc": "ChatJobAborted",
    }


def test_progress_for_a_generation_whose_request_was_deleted_is_refused_as_aborted(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    """A generation whose row went with its deleted request, as on expiry, is refused with ``ChatJobAborted``."""
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()
    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert generation is not None
        generation.wp.delete()

    response = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Blue")

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "ChatJobAborted"


def test_progress_with_a_key_other_than_the_worker_owners_is_refused(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
    make_api_user: MakeApiUser,
) -> None:
    request_id = _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    other_user = make_api_user()

    response = _post_progress(client, other_user.api_key, pop_result["id"], 1, "Blue")

    assert response.status_code == 403, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "WrongCredentials"
    assert not progress_redis.exists(chat_stream_key(request_id))


def test_progress_in_sequence_is_appended_to_the_request_stream_without_a_signal(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert generation is not None
        serving = {"worker": str(generation.worker_id), "model": generation.model}
    subscription = lua_redis.pubsub()
    subscription.psubscribe(f"{chat_signal.CHAT_SIGNAL_CHANNEL_PREFIX}*")
    subscription.get_message(timeout=1)

    first = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Bl")
    retried = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Bl")
    second = _post_progress(client, request_headers["apikey"], pop_result["id"], 2, "ue")

    assert [response.get_json() for response in (first, retried, second)] == [
        {"accepted_seq": 1, "cancelled": False},
        {"accepted_seq": 1, "cancelled": False},
        {"accepted_seq": 2, "cancelled": False},
    ]
    entries = [fields for _, fields in lua_redis.xrange(chat_stream_key(request_id))]
    assert entries == [
        {"gen": pop_result["id"], "seq": "1", "content": "Bl", **serving},
        {"gen": pop_result["id"], "seq": "2", "content": "ue", **serving},
    ]
    signals = []
    while (message := subscription.get_message(timeout=0.2)) is not None:
        if message["type"] == "pmessage":
            signals.append(json.loads(message["data"]))
    assert signals == []
    subscription.close()


def test_a_gap_is_refused_with_the_seq_expected_next(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Bl")

    response = _post_progress(client, request_headers["apikey"], pop_result["id"], 3, "ue")

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "ChatProgressOutOfSequence"
    assert response.get_json()["expected_seq"] == 2


def test_a_submitted_reply_releases_the_lease_and_keeps_the_stream(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    request_id = _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Blue.")

    submit_response = chat_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="Blue.", model_id=CHAT_MODEL))

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    assert not lua_redis.exists(chat_lease_key(pop_result["id"]))
    assert lua_redis.exists(chat_stream_key(request_id))
    late_post = _post_progress(client, request_headers["apikey"], pop_result["id"], 2, " More.")
    assert late_post.status_code == 404, late_post.get_data(as_text=True)


def test_a_faulted_generation_ends_its_stream_and_loses_its_lease(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    request_id = _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()

    response = chat_worker.submit(build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL))

    assert response.status_code == 200, response.get_data(as_text=True)
    entries = [fields for _, fields in progress_redis.xrange(chat_stream_key(request_id))]
    assert entries == [{"gen": pop_result["id"], "ended": "1"}]
    assert not progress_redis.exists(chat_lease_key(pop_result["id"]))
    reissued = chat_worker.pop()
    assert progress_redis.exists(chat_lease_key(reissued["id"]))


def test_a_stale_generation_ends_its_stream(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    from datetime import datetime, timedelta

    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.database import threads
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert generation is not None
        generation.start_time = datetime.utcnow() - timedelta(seconds=generation.job_ttl + 1)
        db.session.commit()

        threads.check_waiting_prompts()

    entries = [fields for _, fields in progress_redis.xrange(chat_stream_key(request_id))]
    assert entries == [{"gen": pop_result["id"], "ended": "1"}]
    assert not progress_redis.exists(chat_lease_key(pop_result["id"]))


def test_a_cancel_of_a_streamed_request_tells_its_worker_through_the_next_progress_post(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    """Requests left queued by earlier tests may be popped first, so the request cancelled is the popped one."""
    _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()
    request_id = lua_redis.hget(chat_lease_key(pop_result["id"]), "request")
    assert request_id is not None
    first = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "Bl")

    cancel_response = client.delete(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)
    after_cancel = _post_progress(client, request_headers["apikey"], pop_result["id"], 2, "ue")

    assert cancel_response.status_code == 200, cancel_response.get_data(as_text=True)
    assert first.get_json() == {"accepted_seq": 1, "cancelled": False}
    assert after_cancel.status_code == 200, after_cancel.get_data(as_text=True)
    assert after_cancel.get_json() == {"accepted_seq": 2, "cancelled": True}
    entries = [fields for _, fields in lua_redis.xrange(chat_stream_key(request_id))]
    assert [(entry["gen"], entry.get("ended"), entry.get("content")) for entry in entries] == [
        (pop_result["id"], None, "Bl"),
        (pop_result["id"], "1", None),
        (pop_result["id"], None, "ue"),
    ]


POSTED_REPLY = "Blue is the colour of a clear sky at noon, of deep water and of the cornflowers in a summer field."
"""More than 80 characters, so the reply is priced above the output estimate's floor of 20 tokens."""


@dataclass(frozen=True)
class Settlement:
    """Represents the kudos postings recorded for one generation and the amounts the text formula gives it."""

    credits: list[Decimal]
    """The positive postings: the worker's pay."""

    debits: list[Decimal]
    """The negative postings: the requesting user's charge."""

    formula_kudos: Decimal
    """The text formula's kudos on the expected text."""

    formula_charge: Decimal
    """``formula_kudos`` with the request's extra burn, as the user is charged it on any settlement."""

    formula_pay: Decimal
    """``formula_kudos`` with the worker's bridge multiplier, as the worker is paid it."""

    floor_kudos: Decimal
    """The text formula's kudos on an empty text, the output estimate's floor."""


def _serve_own_chat_model(client: FlaskClient, request_headers: dict[str, str]) -> FakeChatWorker:
    """Bring a worker online for a model no other test requests, so its pops take only this test's requests."""
    own_model = f"custom/settling-{uuid.uuid4().hex[:8]}"
    worker = FakeChatWorker(
        client=client,
        headers=request_headers,
        name=f"CICD Settling Scribe {uuid.uuid4().hex[:8]}",
        models=[own_model],
    )
    worker.pop()
    return worker


def _pop_own_request(
    client: FlaskClient,
    request_headers: dict[str, str],
    worker: FakeChatWorker,
    *,
    stream: bool,
) -> tuple[str, str]:
    """Submit a chat request for the worker's model, pop it and return the generation and request ids."""
    request_id = _submit_chat_request(client, request_headers, model=worker.models[0], stream=stream, max_tokens=512)
    generation_id: str = worker.pop()["id"]
    return generation_id, request_id


def _read_settlement(app: Flask, generation_id: str, expected_text: str) -> Settlement:
    """Read the generation's kudos postings and price the expected text by the text formula on the same generation."""
    from horde.classes.base.kudos import KudosLedger
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    def price(generation: TextProcessingGeneration, text: str) -> Decimal:
        generation.generation = text
        return Decimal(str(generation.get_gen_kudos())).quantize(Decimal("0.01"))

    with app.app_context():
        amounts = [
            amount for (amount,) in db.session.query(KudosLedger.amount).filter(KudosLedger.job_id == uuid.UUID(generation_id)).all()
        ]
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(generation_id))
        assert generation is not None
        # The text is set on the loaded instance only to price it; the rollback leaves the stored row as it was.
        with db.session.no_autoflush:
            bridge_multiplier = Decimal(str(generation.worker.get_bridge_kudos_multiplier()))
            formula_kudos = price(generation, expected_text)
            formula_charge = Decimal(str(generation.wp.calculate_extra_kudos_burn(float(formula_kudos)))).quantize(Decimal("0.01"))
            floor_kudos = price(generation, "")
        db.session.rollback()
    return Settlement(
        credits=[amount for amount in amounts if amount > 0],
        debits=[amount for amount in amounts if amount < 0],
        formula_kudos=formula_kudos,
        formula_charge=formula_charge,
        formula_pay=(formula_kudos * bridge_multiplier).quantize(Decimal("0.01")),
        floor_kudos=floor_kudos,
    )


def _read_generation_state(app: Flask, generation_id: str) -> tuple[bool, bool, str | None, int, int | None]:
    """Return the generation's faulted and cancelled flags, its stored text, its worker's dropped jobs and the
    character count it was settled on."""
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(generation_id))
        assert generation is not None
        return (
            generation.faulted,
            generation.cancelled,
            generation.generation,
            generation.worker.aborted_jobs,
            generation.settled_character_count,
        )


def test_a_cancel_settles_a_streamed_generation_on_the_content_its_worker_posted(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    """The worker is paid and the user charged the text formula on the posted content, reasoning aside. The
    generation is faulted as cancelled without a dropped job and keeps the posted character count it was settled on,
    and neither a later submit nor the stale pass pays it again."""
    from datetime import datetime, timedelta

    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.database import threads
    from horde.flask import db

    worker = _serve_own_chat_model(client, request_headers)
    generation_id, request_id = _pop_own_request(client, request_headers, worker, stream=True)
    _post_progress(client, request_headers["apikey"], generation_id, 1, "", reasoning_content="The sky is clear today.")
    _post_progress(client, request_headers["apikey"], generation_id, 2, POSTED_REPLY[:40])
    _post_progress(client, request_headers["apikey"], generation_id, 3, POSTED_REPLY[40:])
    aborted_jobs_before = _read_generation_state(app, generation_id)[3]

    cancel_response = client.delete(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)

    assert cancel_response.status_code == 200, cancel_response.get_data(as_text=True)
    settlement = _read_settlement(app, generation_id, POSTED_REPLY)
    assert settlement.formula_kudos > settlement.floor_kudos
    assert settlement.credits == [settlement.formula_pay]
    assert settlement.debits == [-settlement.formula_charge]
    assert _read_generation_state(app, generation_id) == (True, True, None, aborted_jobs_before, len(POSTED_REPLY))
    assert lua_redis.hget(chat_lease_key(generation_id), "cancelled") == "1"

    late_submit = worker.submit(build_chat_submit_body(generation_id=generation_id, content=POSTED_REPLY, model_id=worker.models[0]))

    assert late_submit.status_code == 400, late_submit.get_data(as_text=True)
    assert late_submit.get_json()["rc"] == "AbortedGen"

    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(generation_id))
        assert generation is not None
        generation.start_time = datetime.utcnow() - timedelta(seconds=generation.job_ttl + 1)
        db.session.commit()
        threads.check_waiting_prompts()

    assert _read_settlement(app, generation_id, POSTED_REPLY).credits == [settlement.formula_pay]
    assert _read_generation_state(app, generation_id) == (True, True, None, aborted_jobs_before, len(POSTED_REPLY))


def test_a_cancel_before_any_post_settles_a_streamed_generation_on_the_formula_floor(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    worker = _serve_own_chat_model(client, request_headers)
    generation_id, request_id = _pop_own_request(client, request_headers, worker, stream=True)

    cancel_response = client.delete(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)

    assert cancel_response.status_code == 200, cancel_response.get_data(as_text=True)
    settlement = _read_settlement(app, generation_id, "")
    assert settlement.formula_kudos == settlement.floor_kudos
    assert settlement.debits == [-settlement.formula_charge]
    assert settlement.credits == [settlement.formula_pay]
    generation_state = _read_generation_state(app, generation_id)
    assert generation_state[:2] == (True, True)
    assert generation_state[4] == 0


def test_a_cancel_of_a_request_that_does_not_stream_leaves_its_generation_pending_and_unpaid(
    app: Flask,
    client: FlaskClient,
    request_headers: dict[str, str],
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    worker = _serve_own_chat_model(client, request_headers)
    generation_id, request_id = _pop_own_request(client, request_headers, worker, stream=False)

    cancel_response = client.delete(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)

    assert cancel_response.status_code == 200, cancel_response.get_data(as_text=True)
    settlement = _read_settlement(app, generation_id, "")
    assert (settlement.credits, settlement.debits) == ([], [])
    generation_state = _read_generation_state(app, generation_id)
    assert generation_state[:3] == (False, False, None)
    assert generation_state[4] is None


def test_a_reasoning_delta_is_appended_with_its_reasoning_beside_empty_content(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    """Requests left queued by earlier tests may be popped first, so the entries are read by the popped generation."""
    _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()

    reasoning = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "", reasoning_content="Think")
    text = _post_progress(client, request_headers["apikey"], pop_result["id"], 2, "Blue")

    assert [response.get_json() for response in (reasoning, text)] == [
        {"accepted_seq": 1, "cancelled": False},
        {"accepted_seq": 2, "cancelled": False},
    ]
    entries = [
        fields
        for stream_key in lua_redis.scan_iter(match=chat_stream_key("*"))
        for _, fields in lua_redis.xrange(stream_key)
        if fields.get("gen") == pop_result["id"]
    ]
    assert [(entry["content"], entry.get("reasoning_content")) for entry in entries] == [("", "Think"), ("Blue", None)]


@pytest.mark.parametrize("reasoning_content", [None, ""])
def test_a_delta_without_content_or_reasoning_is_refused(
    client: FlaskClient,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    lua_redis: fakeredis.FakeStrictRedis,
    reasoning_content: str | None,
) -> None:
    _submit_chat_request(client, request_headers, stream=True)
    pop_result = chat_worker.pop()

    response = _post_progress(client, request_headers["apikey"], pop_result["id"], 1, "", reasoning_content=reasoning_content)

    assert response.status_code == 400, response.get_data(as_text=True)
    assert lua_redis.hget(chat_lease_key(pop_result["id"]), "seq") is None
