# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Streamed chat requests end to end: the stream hint, the lease written at pop and the worker progress route."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
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


def _post_progress(client: FlaskClient, apikey: str, generation_id: str, seq: int, content: str) -> TestResponse:
    return client.post(
        PROGRESS_ROUTE,
        json={
            "id": generation_id,
            "seq": seq,
            "delta": {"content": content},
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
        {"accepted_seq": 1},
        {"accepted_seq": 1},
        {"accepted_seq": 2},
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
