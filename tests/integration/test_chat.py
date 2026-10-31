# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Chat requests end to end: the chat routes, the chat declaration on text pop, structured submit and signals."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import fakeredis
import pytest

from horde import chat_signal
from horde.chat_contract import KudosCapExcess
from horde.chat_signal import chat_signal_channel
from horde.classes.kobold.request_fit import MAX_LENGTH_LIMIT
from tests.integration._fake_chat_worker import (
    FakeChatWorker,
    build_chat_submit_body,
    build_faulted_chat_submit_body,
)

CHAT_MODEL = "elinas/chronos-70b-v2"
WORKER_NAME = "CICD Fake Chat Scribe"
CHAT_MESSAGES = [
    {"role": "system", "content": "You are a terse assistant."},
    {"role": "user", "content": [{"type": "text", "text": "Name one colour."}]},
]
TEXT_POP_RESPONSE_KEYS = {"payload", "id", "ids", "messages", "ttl", "extra_source_images", "skipped", "softprompt", "model"}
"""The top-level keys of a text job's pop response, which chat support leaves unchanged."""


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address submit limit, which this module's request volume would otherwise trip."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
def chat_events() -> Iterator[Any]:
    """Route chat signals to an in-memory Redis and return a subscription to every chat channel."""
    fake_publisher = fakeredis.FakeStrictRedis(decode_responses=True)
    previous_publisher = chat_signal._publisher
    chat_signal._publisher = fake_publisher
    subscription = fake_publisher.pubsub()
    subscription.psubscribe(f"{chat_signal.CHAT_SIGNAL_CHANNEL_PREFIX}*")
    subscription.get_message(timeout=1)
    try:
        yield subscription
    finally:
        subscription.close()
        chat_signal._publisher = previous_publisher


@pytest.fixture
def chat_worker(client, request_headers: dict[str, str]) -> FakeChatWorker:
    """A worker serving the chat model, declaring the koboldcpp feature set."""
    return FakeChatWorker(client=client, headers=request_headers, name=WORKER_NAME, models=[CHAT_MODEL])


def _received_events(subscription: Any) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    while True:
        message = subscription.get_message(timeout=0.2)
        if message is None:
            return events
        if message["type"] == "pmessage":
            events.append((message["channel"], json.loads(message["data"])))


def _submit_chat_request(client, request_headers: dict[str, str], **overrides: Any) -> str:
    chat_body: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": CHAT_MESSAGES,
        "temperature": 0.5,
        "stop": "\nuser:",
    }
    chat_body.update(overrides)
    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    return response.get_json()["id"]


def _submit_text_request(client, request_headers: dict[str, str], prompt: str = "a plain text prompt for a plain text worker") -> str:
    text_body = {
        "prompt": prompt,
        "params": {"max_length": 80},
        "models": [CHAT_MODEL],
    }
    response = client.post("/api/v2/generate/text/async", json=text_body, headers=request_headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    return response.get_json()["id"]


def _cancel(client, request_headers: dict[str, str], route_family: str, request_id: str) -> None:
    response = client.delete(f"/api/v2/generate/{route_family}/status/{request_id}", headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)


def _chat_status(client, request_headers: dict[str, str], request_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_a_chat_request_completes_with_its_structured_reply_and_signals(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    chat_events: Any,
) -> None:
    request_id = _submit_chat_request(client, request_headers)

    pop_result = chat_worker.pop()

    assert pop_result["id"] is not None, pop_result
    assert pop_result["chat"] is True
    assert pop_result["model"] == CHAT_MODEL
    assert pop_result["payload"] == {
        "messages": CHAT_MESSAGES,
        "temperature": 0.5,
        "top_p": 1.0,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "rep_pen": 1.0,
        "top_k": 0,
        "min_p": 0.0,
        "stop_sequence": ["\nuser:"],
        "max_length": 512,
        "max_context_length": 2048,
        "n": 1,
    }

    submit_response = chat_worker.submit(
        build_chat_submit_body(
            generation_id=pop_result["id"],
            content="Blue.",
            model_id=CHAT_MODEL,
            prompt_tokens=21,
            completion_tokens=3,
            native_finish_reason="eos_token",
        ),
    )
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)

    status = _chat_status(client, request_headers, request_id)
    assert status["done"] is True
    [generation] = status["generations"]
    assert generation["text"] == "Blue."
    assert generation["message"] == {"role": "assistant", "content": "Blue."}
    assert generation["worker_name"] == WORKER_NAME
    assert generation["finish_reason"] == "stop"
    assert generation["native_finish_reason"] == "eos_token"
    assert generation["prompt_tokens"] == 21
    assert generation["completion_tokens"] == 3
    assert generation["charge_basis"] == "output_estimate"
    assert generation["queue_wait_seconds"] >= 0

    channel = chat_signal_channel(request_id)
    assert _received_events(chat_events) == [
        (channel, {"id": request_id, "event": "generation", "generations": 1}),
        (channel, {"id": request_id, "event": "done", "generations": 1}),
    ]


def test_unreported_token_counts_and_backend_reason_are_null_on_the_status(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    submit_response = chat_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="Red.", model_id=CHAT_MODEL))
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)

    [generation] = _chat_status(client, request_headers, request_id)["generations"]
    assert generation["native_finish_reason"] is None
    assert generation["prompt_tokens"] is None
    assert generation["completion_tokens"] is None
    assert generation["reasoning_tokens"] is None
    assert "reasoning_content" not in generation["message"]


def test_the_structured_reply_is_written_with_the_generation_and_charged_as_text(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    chat_request_id = _submit_chat_request(client, request_headers, max_tokens=80)
    chat_pop = chat_worker.pop()
    chat_submit = chat_worker.submit(build_chat_submit_body(generation_id=chat_pop["id"], content="Blue.", model_id=CHAT_MODEL))
    assert chat_submit.status_code == 200, chat_submit.get_data(as_text=True)

    with app.app_context():
        chat_request = db.session.get(TextWaitingPrompt, uuid.UUID(chat_request_id))
        flattened_prompt = chat_request.prompt
        stored = db.session.get(TextProcessingGeneration, uuid.UUID(chat_pop["id"]))
        assert stored.generation == "Blue."
        assert stored.message == {"role": "assistant", "content": "Blue."}
        assert stored.charge_basis == "output_estimate"
        assert stored.assigned_model == CHAT_MODEL

    text_request_id = _submit_text_request(client, request_headers, prompt=flattened_prompt)
    text_pop = chat_worker.pop()
    assert text_pop["id"] is not None, text_pop
    text_submit = chat_worker.submit({"id": text_pop["id"], "generation": "Blue.", "state": "ok", "seed": 0})
    assert text_submit.status_code == 200, text_submit.get_data(as_text=True)

    assert chat_submit.get_json()["reward"] == text_submit.get_json()["reward"]
    text_status = client.get(f"/api/v2/generate/text/status/{text_request_id}", headers=request_headers).get_json()
    assert "charge_basis" not in text_status["generations"][0]


def test_an_empty_reply_completes_charges_and_is_counted_as_empty(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    monkeypatch,
) -> None:
    from horde.classes.kobold import processing_generation

    empty_generation_counts: list[dict[str, str]] = []

    class RecordingCounter:
        def add(self, amount: int, attributes: dict[str, str]) -> None:
            empty_generation_counts.append(attributes)

    monkeypatch.setattr(processing_generation, "text_empty_generations", RecordingCounter())
    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    submit_response = chat_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="", model_id=CHAT_MODEL))

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    assert submit_response.get_json()["reward"] > 0
    status = _chat_status(client, request_headers, request_id)
    assert status["done"] is True
    assert status["generations"][0]["message"] == {"role": "assistant", "content": ""}
    assert [attributes["model"] for attributes in empty_generation_counts] == [CHAT_MODEL]


def test_the_settlement_log_line_ends_with_the_gentype_of_a_chat_or_text_request(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """The settlement line of a chat reply ends with ``gentype=chat`` and that of a text generation with ``gentype=text``."""
    from horde.logger import logger

    settlement_lines: list[str] = []
    sink_id = logger.add(
        lambda message: settlement_lines.append(message.record["message"]),
        level="INFO",
        filter=lambda record: " kudos, delivered by worker: " in record["message"],
    )
    try:
        _submit_chat_request(client, request_headers, max_tokens=80)
        chat_pop = chat_worker.pop()
        chat_submit = chat_worker.submit(build_chat_submit_body(generation_id=chat_pop["id"], content="Blue.", model_id=CHAT_MODEL))
        assert chat_submit.status_code == 200, chat_submit.get_data(as_text=True)
        _submit_text_request(client, request_headers)
        text_pop = chat_worker.pop()
        assert text_pop["id"] is not None, text_pop
        text_submit = chat_worker.submit({"id": text_pop["id"], "generation": "Blue.", "state": "ok", "seed": 0})
        assert text_submit.status_code == 200, text_submit.get_data(as_text=True)
    finally:
        logger.remove(sink_id)

    assert [line.split(" ")[-1] for line in settlement_lines] == [
        "gentype=chat",
        "gentype=text",
    ]
    assert f"Generation {chat_pop['id']} worth " in settlement_lines[0]


def test_a_submit_naming_another_model_than_the_assigned_one_is_refused(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    response = chat_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="Blue.", model_id="another/model"))

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatModelMismatch"
    _cancel(client, request_headers, "chat", request_id)


def test_the_assigned_model_and_limits_survive_a_later_change_of_declaration(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop(chat_max_context_length=8192, chat_max_length=1024)
    chat_worker.pop(chat_max_context_length=4096, chat_max_length=600)

    with app.app_context():
        stored = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert (stored.assigned_model, stored.assigned_max_context_length, stored.assigned_max_length) == (CHAT_MODEL, 8192, 1024)
    response = chat_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="Blue.", model_id=CHAT_MODEL))
    assert response.status_code == 200, response.get_data(as_text=True)
    assert _chat_status(client, request_headers, request_id)["done"] is True


@pytest.mark.parametrize(
    "submit_body",
    [
        {"generation": "Blue.", "state": "ok"},
        {"message": {"role": "assistant", "content": "Blue."}, "model_id": CHAT_MODEL, "state": "ok"},
        {"message": {"role": "assistant", "content": "Blue.", "audio": {"id": "a"}}, "finish_reason": "stop", "model_id": CHAT_MODEL},
        {"message": {"role": "assistant", "content": "Blue."}, "generation": "Blue.", "finish_reason": "stop", "model_id": CHAT_MODEL},
        {"message": {"role": "assistant", "content": "Blue.", "tool_calls": []}, "finish_reason": "stop", "model_id": CHAT_MODEL},
        {"message": {"role": "assistant", "content": None}, "finish_reason": "stop", "model_id": CHAT_MODEL},
        {"message": {"role": "assistant", "content": "Blue."}, "finish_reason": "tool_calls", "model_id": CHAT_MODEL},
    ],
    ids=[
        "generation-only",
        "no-finish-reason",
        "extra-message-key",
        "message-and-generation",
        "empty-tool-calls",
        "null-content-without-tool-calls",
        "tool-calls-finish-without-tool-calls",
    ],
)
def test_a_chat_submit_without_a_valid_structured_reply_is_refused(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    submit_body: dict[str, Any],
) -> None:
    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    response = chat_worker.submit({"id": pop_result["id"], **submit_body})

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatSubmitInvalid"
    _cancel(client, request_headers, "chat", request_id)


def test_a_submitted_message_from_another_role_than_assistant_is_refused(client, request_headers: dict[str, str]) -> None:
    submit_body = build_chat_submit_body(generation_id="00000000-0000-0000-0000-000000000000", content="Blue.", model_id=CHAT_MODEL)
    submit_body["message"]["role"] = "user"

    response = client.post("/api/v2/generate/text/submit", json=submit_body, headers=request_headers)

    assert response.status_code == 400, response.get_data(as_text=True)


def test_a_faulted_chat_submit_needs_neither_generation_nor_message(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    response = chat_worker.submit(build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["reward"] == 0
    reissued = chat_worker.pop()
    assert reissued["id"] not in (None, pop_result["id"])
    _cancel(client, request_headers, "chat", request_id)


def test_a_request_faults_on_the_submit_that_takes_it_past_the_faulted_generations_limit(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    chat_events: Any,
) -> None:
    """The submit route faults the request and signals its client without waiting for the stale-generation thread."""
    from horde.classes.base.waiting_prompt import FAULTED_GENERATIONS_LIMIT

    request_id = _submit_chat_request(client, request_headers)
    for _ in range(FAULTED_GENERATIONS_LIMIT + 1):
        assert _chat_status(client, request_headers, request_id)["faulted"] is False
        pop_result = chat_worker.pop()
        assert pop_result["id"] is not None, pop_result
        response = chat_worker.submit(build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL))
        assert response.status_code == 200, response.get_data(as_text=True)

    status = _chat_status(client, request_headers, request_id)
    assert status["faulted"] is True
    assert chat_worker.pop()["id"] is None
    assert (chat_signal_channel(request_id), {"id": request_id, "event": "faulted", "generations": 0}) in _received_events(chat_events)


def test_a_text_submit_without_generation_is_still_refused_by_the_parser(client, request_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/v2/generate/text/submit",
        json={"id": "00000000-0000-0000-0000-000000000000", "state": "faulted"},
        headers=request_headers,
    )

    assert response.status_code == 400
    assert "generation" in response.get_data(as_text=True)


def test_a_worker_without_the_chat_declaration_never_pops_a_chat_job(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers)

    for chat_jobs in (None, False):
        pop_result = chat_worker.pop(chat_jobs=chat_jobs)
        assert pop_result["id"] is None, pop_result

    _cancel(client, request_headers, "chat", request_id)


def test_a_chat_worker_still_pops_text_jobs_with_an_unchanged_response(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_text_request(client, request_headers)

    pop_result = chat_worker.pop()

    assert pop_result["id"] is not None, pop_result
    assert set(pop_result) == TEXT_POP_RESPONSE_KEYS
    assert pop_result["payload"]["prompt"] == "a plain text prompt for a plain text worker"
    assert "messages" not in pop_result["payload"]
    submit_response = chat_worker.submit({"id": pop_result["id"], "generation": "done", "state": "ok", "seed": 0})
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    text_status = client.get(f"/api/v2/generate/text/status/{request_id}", headers=request_headers).get_json()
    assert "finish_reason" not in text_status["generations"][0]


def test_chat_limits_and_not_text_limits_admit_a_chat_request(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, horde={"max_context_length": 6144})

    too_small = chat_worker.pop(chat_max_context_length=4096, max_context_length=8192)
    assert too_small["id"] is None, too_small
    fits = chat_worker.pop(chat_max_context_length=8192, max_context_length=2048)
    assert fits["id"] is not None, fits
    assert fits["payload"]["max_context_length"] == 6144
    _cancel(client, request_headers, "chat", request_id)


def test_a_request_needing_an_undeclared_feature_waits_for_a_worker_that_declares_it(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, frequency_penalty=0.5)

    without_feature = chat_worker.pop()
    assert without_feature["id"] is None, without_feature
    with_feature = chat_worker.pop(chat_features=["frequency_penalty", "not_a_known_feature"])
    assert with_feature["id"] is not None, with_feature
    assert with_feature["payload"]["frequency_penalty"] == 0.5
    _cancel(client, request_headers, "chat", request_id)


def test_a_reasoning_effort_waits_for_a_worker_that_declares_it_and_reaches_the_payload_unchanged(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """``none`` needs the feature as any other value does, since a backend that ignores the key would reason anyway."""
    request_id = _submit_chat_request(client, request_headers, reasoning_effort="none")

    without_feature = chat_worker.pop()
    assert without_feature["id"] is None, without_feature
    with_feature = chat_worker.pop(chat_features=["reasoning_effort"])
    assert with_feature["id"] is not None, with_feature
    assert with_feature["payload"]["reasoning_effort"] == "none"
    _cancel(client, request_headers, "chat", request_id)


def _age_worker_into_the_check_in_debounce(app, worker_name: str) -> None:
    """Make every following pop within 30 seconds a debounced check-in, as for a long-running worker."""
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = db.session.query(TextWorker).filter_by(name=worker_name).one()
        worker.created = datetime.utcnow() - timedelta(minutes=10)
        worker.last_check_in = datetime.utcnow()
        db.session.commit()


def _stored_accepts_chat_jobs(app, worker_name: str) -> bool:
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        return bool(db.session.query(TextWorker.accepts_chat_jobs).filter_by(name=worker_name).scalar())


def test_each_pop_is_judged_by_its_own_chat_declaration_inside_the_check_in_debounce(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """A debounced check-in still replaces the chat declaration, so the pop's eligibility follows what it sent."""
    fitting_declaration: dict[str, Any] = {"chat_features": ["frequency_penalty"], "chat_max_context_length": 8192}
    chat_worker.pop(**fitting_declaration)
    _age_worker_into_the_check_in_debounce(app, WORKER_NAME)
    request_id = _submit_chat_request(client, request_headers, frequency_penalty=0.5, horde={"max_context_length": 6144})

    for chat_jobs in (None, False):
        undeclared = chat_worker.pop(chat_jobs=chat_jobs)
        assert undeclared["id"] is None, undeclared
    assert not _stored_accepts_chat_jobs(app, WORKER_NAME)
    too_small = chat_worker.pop(chat_features=["frequency_penalty"], chat_max_context_length=4096)
    assert too_small["id"] is None, too_small
    without_feature = chat_worker.pop(chat_max_context_length=8192)
    assert without_feature["id"] is None, without_feature
    fits = chat_worker.pop(**fitting_declaration)

    assert fits["id"] is not None, fits
    assert fits["payload"]["frequency_penalty"] == 0.5
    assert _stored_accepts_chat_jobs(app, WORKER_NAME)
    _cancel(client, request_headers, "chat", request_id)


def test_a_request_at_default_values_needs_no_feature(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, frequency_penalty=0, presence_penalty=0)

    pop_result = chat_worker.pop(chat_features=[])

    assert pop_result["id"] is not None, pop_result
    _cancel(client, request_headers, "chat", request_id)


@pytest.mark.parametrize(
    ("pop_overrides", "message_fragment"),
    [
        ({"model_id": "koboldcpp/model_Q4"}, "one of the worker's models"),
        ({"model_id": ""}, "model_id"),
    ],
    ids=["not-a-declared-model", "no-model-id"],
)
def test_an_invalid_chat_declaration_is_refused_on_pop(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    pop_overrides: dict[str, Any],
    message_fragment: str,
) -> None:
    response = chat_worker.pop_response(**pop_overrides)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatDeclarationInvalid"
    assert message_fragment in response.get_json()["message"]


def test_a_chat_model_id_containing_a_colon_is_accepted_on_pop(client, request_headers: dict[str, str]) -> None:
    """The native pop treats ``model_id`` as an opaque name, so a colon in it is not refused."""
    colon_model = "koboldcpp/model:Q4"
    colon_worker = FakeChatWorker(client=client, headers=request_headers, name=WORKER_NAME, models=[colon_model])

    response = colon_worker.pop_response(model_id=colon_model)

    assert response.status_code == 200, response.get_data(as_text=True)


def test_chat_jobs_without_chat_limits_is_refused_on_pop(client, request_headers: dict[str, str]) -> None:
    pop_body = {
        "name": WORKER_NAME,
        "models": [CHAT_MODEL],
        "bridge_agent": request_headers["Client-Agent"],
        "chat_jobs": True,
        "model_id": CHAT_MODEL,
    }

    response = client.post("/api/v2/generate/text/pop", json=pop_body, headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatDeclarationInvalid"


def test_prompt_jobs_off_without_chat_jobs_is_refused_on_pop(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """A worker that turns text jobs off without opting into chat jobs could receive no job, so the pop is refused."""
    for chat_jobs in (None, False):
        response = chat_worker.pop_response(chat_jobs=chat_jobs, prompt_jobs=False)

        assert response.status_code == 400
        assert response.get_json()["rc"] == "ChatDeclarationInvalid"
        assert "prompt_jobs" in response.get_json()["message"]


def test_a_chat_only_worker_pops_chat_jobs_and_never_text_jobs(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """With prompt_jobs false a worker receives the queued chat job and leaves the queued text job waiting."""
    text_request_id = _submit_text_request(client, request_headers)
    chat_request_id = _submit_chat_request(client, request_headers)

    chat_only = chat_worker.pop(prompt_jobs=False)
    nothing_else = chat_worker.pop(prompt_jobs=False)
    text_pop = chat_worker.pop()

    assert chat_only["id"] is not None, chat_only
    assert chat_only["chat"] is True
    assert nothing_else["id"] is None, nothing_else
    assert text_pop["id"] is not None, text_pop
    assert text_pop["payload"]["prompt"] == "a plain text prompt for a plain text worker"
    _cancel(client, request_headers, "chat", chat_request_id)
    _cancel(client, request_headers, "text", text_request_id)


def test_a_chat_only_pop_needs_no_text_limits(client, request_headers: dict[str, str]) -> None:
    """max_length and max_context_length bound only text jobs, so a chat-only pop may leave them out."""
    pop_body = {
        "name": WORKER_NAME,
        "models": [CHAT_MODEL],
        "bridge_agent": request_headers["Client-Agent"],
        "chat_jobs": True,
        "prompt_jobs": False,
        "chat_limits": {"max_context_length": 8192, "max_length": 1024},
        "model_id": CHAT_MODEL,
    }

    response = client.post("/api/v2/generate/text/pop", json=pop_body, headers=request_headers)

    assert response.status_code == 200, response.get_data(as_text=True)


def test_the_text_job_opt_out_is_applied_inside_the_check_in_debounce(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """A debounced check-in still stores prompt_jobs, so the next pop's text eligibility follows what it sent."""
    chat_worker.pop()
    _age_worker_into_the_check_in_debounce(app, WORKER_NAME)
    text_request_id = _submit_text_request(client, request_headers)

    chat_only = chat_worker.pop(prompt_jobs=False)
    assert chat_only["id"] is None, chat_only
    assert not _stored_accepts_prompt_jobs(app, WORKER_NAME)
    text_pop = chat_worker.pop()

    assert text_pop["id"] is not None, text_pop
    assert _stored_accepts_prompt_jobs(app, WORKER_NAME)
    _cancel(client, request_headers, "text", text_request_id)


def _stored_accepts_prompt_jobs(app, worker_name: str) -> bool:
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        return bool(db.session.query(TextWorker.accepts_prompt_jobs).filter_by(name=worker_name).scalar())


def test_a_developer_message_reaches_the_worker_as_a_system_message(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(
        client,
        request_headers,
        messages=[{"role": "developer", "content": "Be terse."}, {"role": "user", "content": "Hi"}],
    )

    pop_result = chat_worker.pop()

    assert pop_result["payload"]["messages"] == [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Hi"}]
    _cancel(client, request_headers, "chat", request_id)


def test_each_route_family_reports_the_other_familys_requests_as_not_found(client, request_headers: dict[str, str]) -> None:
    chat_request_id = _submit_chat_request(client, request_headers)
    text_request_id = _submit_text_request(client, request_headers)

    for method, path in (
        (client.get, f"/api/v2/generate/text/status/{chat_request_id}"),
        (client.delete, f"/api/v2/generate/text/status/{chat_request_id}"),
        (client.get, f"/api/v2/generate/text/request/{chat_request_id}"),
        (client.get, f"/api/v2/generate/chat/status/{text_request_id}"),
        (client.delete, f"/api/v2/generate/chat/status/{text_request_id}"),
    ):
        response = method(path, headers=request_headers)
        assert response.status_code == 404, path
        assert response.get_json()["rc"] == "RequestNotFound"

    _cancel(client, request_headers, "chat", chat_request_id)
    _cancel(client, request_headers, "text", text_request_id)


def test_there_is_no_chat_stored_parameters_route(client, request_headers: dict[str, str]) -> None:
    request_id = _submit_chat_request(client, request_headers)

    response = client.get(f"/api/v2/generate/chat/request/{request_id}", headers=request_headers)

    assert response.status_code == 404
    _cancel(client, request_headers, "chat", request_id)


def test_cancelling_a_chat_request_publishes_a_cancelled_event(client, request_headers: dict[str, str], chat_events: Any) -> None:
    request_id = _submit_chat_request(client, request_headers)

    _cancel(client, request_headers, "chat", request_id)

    assert _received_events(chat_events) == [
        (chat_signal_channel(request_id), {"id": request_id, "event": "cancelled", "generations": 0}),
    ]
    status = _chat_status(client, request_headers, request_id)
    assert status["done"] is True
    assert status["faulted"] is False
    assert status["processing"] == status["waiting"] == 0
    assert status["generations"] == []


def test_faulted_chat_requests_publish_a_faulted_event_and_text_requests_publish_none(
    client,
    request_headers: dict[str, str],
    chat_events: Any,
) -> None:
    from horde.database.threads import collect_chat_faults, publish_chat_faults

    chat_request_id = _submit_chat_request(client, request_headers)
    text_request_id = _submit_text_request(client, request_headers)

    publish_chat_faults(collect_chat_faults([uuid.UUID(chat_request_id), uuid.UUID(text_request_id)]))

    assert _received_events(chat_events) == [
        (chat_signal_channel(chat_request_id), {"id": chat_request_id, "event": "faulted", "generations": 0}),
    ]
    _cancel(client, request_headers, "chat", chat_request_id)
    _cancel(client, request_headers, "text", text_request_id)


def test_an_expired_chat_request_is_signalled_after_its_row_is_gone(
    app,
    client,
    request_headers: dict[str, str],
    monkeypatch,
) -> None:
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.database import threads
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers)
    with app.app_context():
        expired_request = db.session.get(TextWaitingPrompt, uuid.UUID(request_id))
        expired_request.expiry = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
    published: list[tuple[str, str, bool]] = []

    def record_publish(published_request_id: str, event: Any, generation_count: int, **_fault_details: Any) -> None:
        row_still_exists = db.session.query(TextWaitingPrompt.id).filter_by(id=uuid.UUID(published_request_id)).first() is not None
        published.append((published_request_id, event.value, row_still_exists))

    monkeypatch.setattr(threads, "publish_chat_event", record_publish)

    threads.check_waiting_prompts()

    assert (request_id, "faulted", False) in published
    assert client.get(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers).status_code == 404


def test_turning_admission_off_refuses_new_chat_requests_and_keeps_serving_queued_ones(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    monkeypatch,
) -> None:
    from horde.config import Config

    request_id = _submit_chat_request(client, request_headers)
    monkeypatch.setattr(Config, "chat_admission_enabled", False)

    refused = client.post("/api/v2/generate/chat/async", json={"model": CHAT_MODEL, "messages": CHAT_MESSAGES}, headers=request_headers)
    assert refused.status_code == 503
    assert refused.get_json()["rc"] == "ChatAdmissionDisabled"

    assert _chat_status(client, request_headers, request_id)["done"] is False
    pop_result = chat_worker.pop()
    assert pop_result["id"] is not None, pop_result
    submit_response = chat_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="Blue.", model_id=CHAT_MODEL))
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    assert _chat_status(client, request_headers, request_id)["done"] is True


@pytest.mark.parametrize(
    "chat_body_overrides",
    [
        {"logprobs": True},
        {"n": 2},
        {"horde": {"models": ["other/model"]}},
        {"messages": [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]},
        {"messages": [{"role": "user", "content": "Hi"}, {"role": "tool", "content": "Rain", "tool_call_id": "call_unknown"}]},
    ],
    ids=["logprobs", "n", "horde-models", "assistant-image-part", "unanswered-tool-message"],
)
def test_an_unsupported_chat_parameter_is_refused_with_its_return_code(
    client,
    request_headers: dict[str, str],
    chat_body_overrides: dict[str, Any],
) -> None:
    chat_body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, **chat_body_overrides}

    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "UnsupportedChatParameter"


@pytest.mark.parametrize(
    ("chat_body_overrides", "named_key"),
    [
        ({"temperature": 9}, "temperature"),
        ({"max_tokens": 8}, "max_length"),
        ({"max_tokens": MAX_LENGTH_LIMIT + 1}, "max_length"),
    ],
)
def test_a_chat_parameter_outside_the_text_range_is_refused_unclamped(
    client,
    request_headers: dict[str, str],
    chat_body_overrides: dict[str, Any],
    named_key: str,
) -> None:
    chat_body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, **chat_body_overrides}

    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)

    assert response.status_code == 400
    assert named_key in response.get_data(as_text=True)


def test_differing_token_limits_are_refused(client, request_headers: dict[str, str]) -> None:
    chat_body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, "max_tokens": 100, "max_completion_tokens": 200}

    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatRequestConflict"


def test_a_chat_dry_run_prices_the_request_without_queueing_it(client, request_headers: dict[str, str]) -> None:
    chat_body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, "horde": {"dry_run": True}}

    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    quote = response.get_json()
    assert quote["kudos"] > 0
    assert quote["resolved"]["context_fit"] == "grow"
    assert quote["resolved"]["models"] == [CHAT_MODEL]


@pytest.mark.parametrize(
    "usage_field, reported_value",
    [
        ("prompt_tokens", -1),
        ("completion_tokens", 10_000_001),
        ("reasoning_tokens", -1),
        ("finish_reason", "tool_call"),
        ("native_finish_reason", "x" * 33),
    ],
)
def test_a_submit_with_an_invalid_reported_usage_is_refused(
    client,
    request_headers: dict[str, str],
    usage_field: str,
    reported_value: object,
) -> None:
    submit_body = {
        "id": "00000000-0000-0000-0000-000000000000",
        "message": {"role": "assistant", "content": "x"},
        "model_id": CHAT_MODEL,
        "finish_reason": "stop",
        "state": "ok",
        usage_field: reported_value,
    }

    response = client.post("/api/v2/generate/text/submit", json=submit_body, headers=request_headers)

    assert response.status_code == 400, response.get_data(as_text=True)


def test_a_validation_rewrite_of_the_prompt_is_applied_to_every_message_text(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    make_api_user,
    monkeypatch,
) -> None:
    from horde.classes.base.user import User
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.detection import prompt_checker
    from horde.flask import db

    flagged_user = make_api_user(kudos=1000)
    with app.app_context():
        db.session.get(User, flagged_user.id).set_flagged(True)

    def replace_colour(prompt: str, models: list[str], already_replaced: bool = False) -> str:
        return prompt.replace("colour", "hue")

    # Flagged users always get the replacement filter; its regexes are not loaded in tests, so the filter is
    # stood in for by a replacement that is visible in the result.
    monkeypatch.setattr(prompt_checker, "nsfw_model_prompt_replace", replace_colour)
    flagged_headers = {**request_headers, "apikey": flagged_user.api_key}

    request_id = _submit_chat_request(client, flagged_headers)

    pop_result = chat_worker.pop()
    assert pop_result["payload"]["messages"] == [
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": [{"type": "text", "text": "Name one hue."}]},
    ]
    with app.app_context():
        submitted_prompt = db.session.query(TextWaitingPrompt.submitted_prompt).filter_by(id=uuid.UUID(request_id)).scalar()
        assert submitted_prompt == "system: You are a terse assistant.\nuser: Name one colour."
    _cancel(client, flagged_headers, "chat", request_id)


def _count_chat_requests(app, user_id: int) -> int:
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    with app.app_context():
        return db.session.query(TextWaitingPrompt).filter(TextWaitingPrompt.user_id == user_id, TextWaitingPrompt.is_chat.is_(True)).count()


def _count_generation_debits(app, user_id: int) -> int:
    """Count the requester debits posted for a user, which an activation's horde tax is one of."""
    from horde.classes.base.kudos import KudosLedger
    from horde.enums import KudosEntryType
    from horde.flask import db

    with app.app_context():
        return (
            db.session.query(KudosLedger)
            .filter(KudosLedger.user_id == user_id, KudosLedger.entry_type == KudosEntryType.GENERATION)
            .count()
        )


def _chat_body(model: str = CHAT_MODEL, **horde_options: Any) -> dict[str, Any]:
    chat_body: dict[str, Any] = {"model": model, "messages": CHAT_MESSAGES, "max_tokens": 64}
    if horde_options:
        chat_body["horde"] = horde_options
    return chat_body


def _post_chat(client, headers: dict[str, str], chat_body: dict[str, Any]) -> Any:
    return client.post("/api/v2/generate/chat/async", json=chat_body, headers=headers)


def _serve_custom_chat_model(client, headers: dict[str, str]) -> tuple[str, FakeChatWorker]:
    """Bring a worker online that serves chat jobs with a model the text reference does not list."""
    custom_model = f"custom/chat-only-{uuid.uuid4().hex[:8]}"
    worker_name = f"CICD Custom Chat Scribe {uuid.uuid4().hex[:8]}"
    custom_worker = FakeChatWorker(client=client, headers=headers, name=worker_name, models=[custom_model])
    custom_worker.pop()
    return custom_model, custom_worker


def _take_worker_offline(app, worker_name: str) -> None:
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = db.session.query(TextWorker).filter_by(name=worker_name).one()
        worker.last_check_in = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()


def _set_worker_speed(app, worker_name: str, *, tokens_per_second: float) -> None:
    """Record a measured speed for a worker, as the speed refresh would after it completed jobs at that rate."""
    from horde import vars as hv
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = db.session.query(TextWorker).filter_by(name=worker_name).one()
        worker.speed = tokens_per_second * hv.thing_divisors["text"]
        db.session.commit()


def test_a_model_unknown_to_the_reference_and_every_online_chat_worker_is_404_with_no_row_and_no_tax(app, client, make_api_user) -> None:
    from horde.model_reference import model_reference

    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    unknown_model = f"custom/unknown-{uuid.uuid4().hex[:8]}"
    assert not model_reference.is_known_text_model(unknown_model)

    response = _post_chat(client, headers, _chat_body(unknown_model))

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "UnknownChatModel"
    assert _count_chat_requests(app, requester.id) == 0
    assert _count_generation_debits(app, requester.id) == 0


def test_an_unknown_model_is_refused_after_authentication(client) -> None:
    headers = {"apikey": "not-a-registered-key", "Client-Agent": "aihorde_ci_client:1:test"}

    response = _post_chat(client, headers, _chat_body(f"custom/unknown-{uuid.uuid4().hex[:8]}"))

    assert response.status_code == 401, response.get_data(as_text=True)


def test_an_unknown_model_is_refused_as_unknown_rather_than_as_a_context_that_cannot_grow(client, request_headers: dict[str, str]) -> None:
    chat_body = _chat_body(f"custom/unknown-{uuid.uuid4().hex[:8]}", context_fit="reject")
    chat_body["messages"] = [{"role": "user", "content": "word " * 1700}]

    response = _post_chat(client, request_headers, chat_body)

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "UnknownChatModel"


def _quote_kudos(client, headers: dict[str, str]) -> int:
    """Return the kudos a dry run of the default chat body quotes, the estimate ``horde.max_kudos`` is checked against."""
    response = _post_chat(client, headers, _chat_body(dry_run=True))
    assert response.status_code == 200, response.get_data(as_text=True)
    # The response model marshals kudos as a number with a fraction, though the route rounds it to a whole one.
    quoted_kudos = int(response.get_json()["kudos"])
    assert quoted_kudos > 1
    return quoted_kudos


def test_a_kudos_estimate_over_max_kudos_is_400_stating_both_with_no_row_and_no_tax(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    kudos_estimate = _quote_kudos(client, headers)
    max_kudos = kudos_estimate - 1

    response = _post_chat(client, headers, _chat_body(max_kudos=max_kudos))

    assert response.status_code == 400, response.get_data(as_text=True)
    refusal = response.get_json()
    assert refusal["rc"] == "ChatKudosCapExceeded"
    assert KudosCapExcess.read_from_message(refusal["message"]) == KudosCapExcess(kudos_estimate=kudos_estimate, max_kudos=max_kudos)
    assert _count_chat_requests(app, requester.id) == 0
    assert _count_generation_debits(app, requester.id) == 0


def test_a_kudos_estimate_at_max_kudos_is_queued(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    kudos_estimate = _quote_kudos(client, headers)

    response = _post_chat(client, headers, _chat_body(max_kudos=kudos_estimate))

    assert response.status_code == 202, response.get_data(as_text=True)
    assert _count_chat_requests(app, requester.id) == 1


def test_a_dry_run_over_max_kudos_is_refused_as_its_submission_would_be(app, client, make_api_user) -> None:
    """A quote does not report a request that its submission would refuse."""
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    kudos_estimate = _quote_kudos(client, headers)

    response = _post_chat(client, headers, _chat_body(dry_run=True, max_kudos=kudos_estimate - 1))

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "ChatKudosCapExceeded"
    assert _count_chat_requests(app, requester.id) == 0


ANONYMOUS_HEADERS = {"apikey": "0000000000", "Client-Agent": "aihorde_ci_client:1:test"}
"""The anonymous key, which holds no kudos, so it is refused any request above the free output allowance."""


def _long_output_chat_body(max_tokens: int) -> dict[str, Any]:
    """Return a chat body asking for ``max_tokens`` under a context large enough for it."""
    return {
        "model": CHAT_MODEL,
        "messages": CHAT_MESSAGES,
        "max_tokens": max_tokens,
        "horde": {"max_context_length": 8192},
    }


@pytest.fixture
def idle_long_output_worker(app, client, request_headers: dict[str, str]) -> Iterator[FakeChatWorker]:
    """An online chat worker with no queued work that serves outputs past the free output ceiling.

    It is taken offline afterwards, since its limits are larger than the ones other tests expect of the online pool.
    """
    worker = FakeChatWorker(
        client=client,
        headers=request_headers,
        name=f"CICD Long Reply Scribe {uuid.uuid4().hex[:8]}",
        models=[CHAT_MODEL],
        pop_options={"chat_max_length": 8192, "chat_max_context_length": 8192},
    )
    worker.pop()
    # A worker is offered only the output its measured speed finishes inside the base lease, so a worker that is
    # eligible for its whole advertised length needs a recorded speed to match: 8192 tokens in 120 s is about 69
    # tokens per second.
    _set_worker_speed(app, worker.name, tokens_per_second=80)
    yield worker
    _take_worker_offline(app, worker.name)


def test_an_anonymous_chat_request_within_the_ceiling_is_free_with_an_idle_eligible_worker(
    client,
    idle_long_output_worker: FakeChatWorker,
) -> None:
    response = _post_chat(client, ANONYMOUS_HEADERS, _long_output_chat_body(2048))

    assert response.status_code == 202, response.get_data(as_text=True)
    _cancel(client, ANONYMOUS_HEADERS, "chat", response.get_json()["id"])


def test_an_anonymous_chat_request_above_the_ceiling_is_refused_stating_the_allowance(
    client,
    idle_long_output_worker: FakeChatWorker,
) -> None:
    from horde.config import Config

    response = _post_chat(client, ANONYMOUS_HEADERS, _long_output_chat_body(Config.text_free_output_ceiling + 1))

    assert response.status_code == 403, response.get_data(as_text=True)
    refusal = response.get_json()
    assert refusal["rc"] == "KudosUpfront"
    assert f"up to {Config.text_free_output_ceiling} tokens" in refusal["message"]
    # The reservation and the dry-run quote are different figures for the same request, and the refusal states both.
    figures = re.search(r"needs ([\d.]+) kudos held upfront; its quoted cost is ([\d.]+) kudos", refusal["message"])
    assert figures is not None, refusal["message"]
    reservation, quote = figures.groups()
    assert float(reservation) > 0
    assert float(quote) > 0


def test_a_quote_cached_for_a_funded_requester_does_not_answer_an_anonymous_text_dry_run_above_the_allowance(
    client,
    make_api_user,
    settle_kudos,
) -> None:
    """The quote cache is keyed by the params alone, so it must not answer a dry run the requester would be refused.

    No worker serves the model, so the allowance is at its floor, 2048 tokens is above it, and the anonymous dry run is
    refused ``NoEligibleWorker`` in place of ``KudosUpfront``, without a declaration gap to state.
    """
    funded = make_api_user(kudos=100000)
    settle_kudos()
    funded_headers = {"apikey": funded.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    text_body = {
        "prompt": "Rivers.",
        "params": {"max_length": 2048, "max_context_length": 8192},
        "models": [f"koboldcpp/quote-cache-probe-{uuid.uuid4().hex[:8]}"],
        "dry_run": True,
    }
    funded_quote = client.post("/api/v2/generate/text/async", json=text_body, headers=funded_headers)
    assert funded_quote.status_code == 200, funded_quote.get_data(as_text=True)

    anonymous_quote = client.post("/api/v2/generate/text/async", json=text_body, headers=ANONYMOUS_HEADERS)

    assert anonymous_quote.status_code == 403, anonymous_quote.get_data(as_text=True)
    assert anonymous_quote.get_json() == {
        "message": "No online worker can serve this request.",
        "rc": "NoEligibleWorker",
    }


def test_a_repeated_operation_key_is_answered_while_admission_is_off(client, make_api_user, monkeypatch) -> None:
    from horde.config import Config

    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    chat_body = _chat_body(operation_key=str(uuid.uuid4()))
    first_response = _post_chat(client, headers, chat_body)
    assert first_response.status_code == 202, first_response.get_data(as_text=True)
    monkeypatch.setattr(Config, "chat_admission_enabled", False)

    repeated_response = _post_chat(client, headers, chat_body)

    assert repeated_response.status_code == 200, repeated_response.get_data(as_text=True)
    assert repeated_response.get_json()["id"] == first_response.get_json()["id"]
    _cancel(client, headers, "chat", first_response.get_json()["id"])


def test_a_model_only_an_online_chat_worker_serves_is_accepted(client, request_headers: dict[str, str]) -> None:
    from horde.model_reference import model_reference

    custom_model, _ = _serve_custom_chat_model(client, request_headers)
    assert not model_reference.is_known_text_model(custom_model)

    response = _post_chat(client, request_headers, _chat_body(custom_model))

    assert response.status_code == 202, response.get_data(as_text=True)
    _cancel(client, request_headers, "chat", response.get_json()["id"])


def test_a_model_only_the_reference_lists_is_accepted_and_reported_as_not_possible(client, request_headers: dict[str, str]) -> None:
    from horde.model_reference import model_reference

    unserved_model = next(
        model_name for model_name in sorted(model_reference.text_model_names) if model_name != CHAT_MODEL and "::" not in model_name
    )

    response = _post_chat(client, request_headers, _chat_body(unserved_model))

    assert response.status_code == 202, response.get_data(as_text=True)
    request_id = response.get_json()["id"]
    status = _chat_status(client, request_headers, request_id)
    assert status["is_possible"] is False
    assert status["impossible_reason"] is None
    _cancel(client, request_headers, "chat", request_id)


def test_a_repeated_operation_key_returns_the_first_request_after_its_model_went_offline(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    custom_model, custom_worker = _serve_custom_chat_model(client, headers)
    operation_key = str(uuid.uuid4())

    first_response = _post_chat(client, headers, _chat_body(custom_model, operation_key=operation_key))
    assert first_response.status_code == 202, first_response.get_data(as_text=True)
    first_submission = first_response.get_json()
    _take_worker_offline(app, custom_worker.name)
    assert _post_chat(client, headers, _chat_body(custom_model)).status_code == 404

    # max_completion_tokens in place of max_tokens is the same effective request.
    repeated_body = _chat_body(custom_model, operation_key=operation_key)
    repeated_body["max_completion_tokens"] = repeated_body.pop("max_tokens")
    repeated_response = _post_chat(client, headers, repeated_body)

    assert repeated_response.status_code == 200, repeated_response.get_data(as_text=True)
    assert repeated_response.get_json() == {"id": first_submission["id"], "kudos": first_submission["kudos"]}
    assert _count_chat_requests(app, requester.id) == 1
    assert _count_generation_debits(app, requester.id) == 1
    _cancel(client, headers, "chat", first_submission["id"])


def test_a_repeated_operation_key_with_a_changed_request_is_refused(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    operation_key = str(uuid.uuid4())
    first_response = _post_chat(client, headers, _chat_body(operation_key=operation_key))
    assert first_response.status_code == 202, first_response.get_data(as_text=True)

    changed_body = _chat_body(operation_key=operation_key)
    changed_body["temperature"] = 0.2
    refused = _post_chat(client, headers, changed_body)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["rc"] == "OperationKeyConflict"
    assert _count_chat_requests(app, requester.id) == 1
    _cancel(client, headers, "chat", first_response.get_json()["id"])


def _listing_body(models: list[str], **horde_options: Any) -> dict[str, Any]:
    """Return the default chat body with ``models`` in place of ``model``."""
    chat_body = _chat_body(**horde_options)
    del chat_body["model"]
    chat_body["models"] = models
    return chat_body


def test_a_request_listing_models_is_served_by_a_worker_of_one_and_reports_the_served_model(client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    custom_model, custom_worker = _serve_custom_chat_model(client, headers)
    response = _post_chat(client, headers, _listing_body([CHAT_MODEL, custom_model]))
    assert response.status_code == 202, response.get_data(as_text=True)
    request_id = response.get_json()["id"]

    pop_result = custom_worker.pop()

    assert pop_result["id"] is not None, pop_result
    assert pop_result["model"] == custom_model
    submit_response = custom_worker.submit(build_chat_submit_body(generation_id=pop_result["id"], content="Blue.", model_id=custom_model))
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    [generation] = _chat_status(client, headers, request_id)["generations"]
    assert generation["model"] == custom_model


def test_a_dry_run_of_a_request_listing_models_resolves_every_listed_model(client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    custom_model, _ = _serve_custom_chat_model(client, headers)

    response = _post_chat(client, headers, _listing_body([CHAT_MODEL, custom_model], dry_run=True))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["resolved"]["models"] == [CHAT_MODEL, custom_model]


def test_an_unknown_entry_of_models_refuses_the_whole_request_at_its_index(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    unknown_model = f"custom/unknown-{uuid.uuid4().hex[:8]}"

    response = _post_chat(client, headers, _listing_body([CHAT_MODEL, unknown_model]))

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "UnknownChatModel"
    assert response.get_json()["message"].startswith(f"'models[1]': The model '{unknown_model}'")
    assert _count_chat_requests(app, requester.id) == 0
    assert _count_generation_debits(app, requester.id) == 0


def test_a_repeated_operation_key_with_the_models_reordered_returns_the_first_request(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    custom_model, _ = _serve_custom_chat_model(client, headers)
    operation_key = str(uuid.uuid4())
    first_response = _post_chat(client, headers, _listing_body([CHAT_MODEL, custom_model], operation_key=operation_key))
    assert first_response.status_code == 202, first_response.get_data(as_text=True)
    first_submission = first_response.get_json()

    repeated_response = _post_chat(client, headers, _listing_body([custom_model, CHAT_MODEL], operation_key=operation_key))

    assert repeated_response.status_code == 200, repeated_response.get_data(as_text=True)
    assert repeated_response.get_json() == {"id": first_submission["id"], "kudos": first_submission["kudos"]}
    assert _count_chat_requests(app, requester.id) == 1
    _cancel(client, headers, "chat", first_submission["id"])


def test_one_operation_key_from_two_users_creates_two_requests(client, make_api_user) -> None:
    operation_key = str(uuid.uuid4())
    request_ids = []
    for _ in range(2):
        requester = make_api_user()
        headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
        response = _post_chat(client, headers, _chat_body(operation_key=operation_key))
        assert response.status_code == 202, response.get_data(as_text=True)
        request_ids.append(response.get_json()["id"])
        _cancel(client, headers, "chat", request_ids[-1])

    assert request_ids[0] != request_ids[1]


def test_a_dry_run_with_an_operation_key_quotes_without_claiming_it(app, client, make_api_user) -> None:
    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    operation_key = str(uuid.uuid4())

    quote = _post_chat(client, headers, _chat_body(operation_key=operation_key, dry_run=True))
    assert quote.status_code == 200, quote.get_data(as_text=True)
    assert "id" not in quote.get_json()

    queued = _post_chat(client, headers, _chat_body(operation_key=operation_key))
    assert queued.status_code == 202, queued.get_data(as_text=True)
    assert _count_chat_requests(app, requester.id) == 1
    _cancel(client, headers, "chat", queued.get_json()["id"])


def test_two_concurrent_submits_with_one_operation_key_make_one_activation_one_tax_and_one_job(
    app,
    client,
    make_api_user,
    monkeypatch,
) -> None:
    import threading

    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt

    requester = make_api_user()
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:test"}
    chat_body = _chat_body(operation_key=str(uuid.uuid4()))

    # Both submissions pass the claim lookup before either activates, so only the unique index can tell them apart.
    both_activating = threading.Barrier(2, timeout=30)
    waited_threads: set[int] = set()
    original_activate = TextWaitingPrompt._activate

    def activate_together(waiting_prompt: TextWaitingPrompt, *args: Any, **kwargs: Any) -> None:
        if threading.get_ident() not in waited_threads:
            waited_threads.add(threading.get_ident())
            both_activating.wait()
        original_activate(waiting_prompt, *args, **kwargs)

    monkeypatch.setattr(TextWaitingPrompt, "_activate", activate_together)
    responses: list[Any] = []

    def submit() -> None:
        # Each thread pushes an application context of its own, and so works on a database session of its own.
        with app.app_context():
            responses.append(_post_chat(client, headers, chat_body))

    submitters = [threading.Thread(target=submit) for _ in range(2)]
    for submitter in submitters:
        submitter.start()
    for submitter in submitters:
        submitter.join(timeout=60)

    assert sorted(response.status_code for response in responses) == [200, 202], [response.get_data(as_text=True) for response in responses]
    assert responses[0].get_json()["id"] == responses[1].get_json()["id"]
    assert _count_chat_requests(app, requester.id) == 1
    assert _count_generation_debits(app, requester.id) == 1
    _cancel(client, headers, "chat", responses[0].get_json()["id"])


def test_a_body_with_a_length_above_the_limit_is_refused_before_it_is_parsed(client, request_headers: dict[str, str]) -> None:
    from horde.chat_contract import MAX_CHAT_BODY_BYTES

    response = client.post(
        "/api/v2/generate/chat/async",
        data=b"{" * (MAX_CHAT_BODY_BYTES + 1),
        content_type="application/json",
        headers=request_headers,
    )

    assert response.status_code == 413, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "RequestTooLarge"


def test_a_chunked_body_above_the_limit_is_refused(client, request_headers: dict[str, str]) -> None:
    import io

    from horde.chat_contract import MAX_CHAT_BODY_BYTES

    response = client.post(
        "/api/v2/generate/chat/async",
        input_stream=io.BytesIO(b"{" * (MAX_CHAT_BODY_BYTES + 1)),
        content_type="application/json",
        headers=request_headers,
    )

    assert response.status_code == 413, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "RequestTooLarge"


def test_a_chunked_body_within_the_limit_is_parsed(client, request_headers: dict[str, str]) -> None:
    import io

    response = client.post(
        "/api/v2/generate/chat/async",
        input_stream=io.BytesIO(json.dumps(_chat_body(dry_run=True)).encode()),
        content_type="application/json",
        headers=request_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["kudos"] > 0


@pytest.mark.parametrize("chunked", [False, True], ids=["content_length", "chunked"])
def test_a_body_holding_a_non_json_constant_is_refused_like_a_non_json_body(
    client,
    request_headers: dict[str, str],
    chunked: bool,
) -> None:
    """``NaN`` and ``Infinity`` are not JSON; a body holding one is refused the same way as a body that is not JSON."""
    import io

    body = json.dumps(_chat_body(dry_run=True, temperature=1.0)).replace("1.0", "NaN").encode()
    post_kwargs = {"input_stream": io.BytesIO(body)} if chunked else {"data": body}

    not_json = client.post("/api/v2/generate/chat/async", data=b"not json", content_type="application/json", headers=request_headers)
    response = client.post("/api/v2/generate/chat/async", content_type="application/json", headers=request_headers, **post_kwargs)

    assert not_json.status_code == 400, not_json.get_data(as_text=True)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == not_json.get_json()["rc"]


def test_a_grown_context_is_reported_on_the_chat_status(client, request_headers: dict[str, str], chat_worker: FakeChatWorker) -> None:
    chat_worker.pop()
    long_messages = [{"role": "user", "content": "word " * 1700}]

    grown_id = _submit_chat_request(client, request_headers, messages=long_messages, max_tokens=64, horde={"context_fit": "grow"})
    fitting_id = _submit_chat_request(client, request_headers, max_tokens=64)

    [adjustment] = _chat_status(client, request_headers, grown_id)["context_adjustments"]
    assert adjustment["from"] == 2048
    assert 2048 < adjustment["to"] <= 8192
    assert adjustment["reason"] == "grow"
    assert _chat_status(client, request_headers, fitting_id)["context_adjustments"] is None
    _cancel(client, request_headers, "chat", grown_id)
    _cancel(client, request_headers, "chat", fitting_id)


LONG_HISTORY_SYSTEM = {"role": "system", "content": "You are a terse assistant."}
LONG_HISTORY_LATEST_USER = {"role": "user", "content": "And which colour comes last?"}
LONG_HISTORY = [
    LONG_HISTORY_SYSTEM,
    *(
        message
        for turn in range(8)
        for message in (
            {"role": "user", "content": f"Turn {turn}: " + "tell me about colours " * 200},
            {"role": "assistant", "content": f"Reply {turn}: " + "red green blue " * 200},
        )
    ),
    LONG_HISTORY_LATEST_USER,
]
"""A history whose estimate is far above any context the fake worker serves, while its latest turn is short."""


@pytest.mark.parametrize(
    "context_fit_options",
    [{"context_fit": "truncate_oldest"}, {}],
    ids=["explicit", "default"],
)
def test_a_long_history_under_truncate_oldest_is_admitted_with_its_oldest_turns_dropped(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    context_fit_options: dict[str, str],
) -> None:
    """A long history is admitted with its oldest turns dropped, whether ``truncate_oldest`` is sent or is the default."""
    chat_worker.pop()

    request_id = _submit_chat_request(
        client,
        request_headers,
        messages=LONG_HISTORY,
        max_tokens=64,
        horde={"max_context_length": 2048, **context_fit_options},
    )

    grow_adjustment, truncate_adjustment = _chat_status(client, request_headers, request_id)["context_adjustments"]
    assert grow_adjustment["reason"] == "grow"
    assert grow_adjustment["from"] == 2048
    assert 2048 < grow_adjustment["to"] <= 8192
    assert truncate_adjustment["reason"] == "truncate_oldest"
    assert truncate_adjustment["dropped_messages"] > 0
    assert truncate_adjustment["dropped_messages"] % 2 == 0
    assert truncate_adjustment["from"] > grow_adjustment["to"]
    assert truncate_adjustment["to"] + 64 <= grow_adjustment["to"]

    popped = chat_worker.pop()
    assert popped["id"] is not None, popped
    kept_messages = popped["payload"]["messages"]
    assert popped["payload"]["max_context_length"] == grow_adjustment["to"]
    assert len(kept_messages) == len(LONG_HISTORY) - truncate_adjustment["dropped_messages"]
    assert kept_messages[0] == LONG_HISTORY_SYSTEM
    assert kept_messages[-1] == LONG_HISTORY_LATEST_USER
    assert kept_messages[1:] == LONG_HISTORY[1 + truncate_adjustment["dropped_messages"] :]
    _cancel(client, request_headers, "chat", request_id)


def test_a_long_history_under_reject_is_refused(client, request_headers: dict[str, str], chat_worker: FakeChatWorker) -> None:
    chat_worker.pop()

    response = client.post(
        "/api/v2/generate/chat/async",
        json={
            "model": CHAT_MODEL,
            "messages": LONG_HISTORY,
            "max_tokens": 64,
            "horde": {"max_context_length": 2048, "context_fit": "reject"},
        },
        headers=request_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "PromptExceedsContext"


def test_a_latest_turn_too_long_for_any_context_is_refused_under_truncate_oldest(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    chat_worker.pop()
    overlong_latest_user = {"role": "user", "content": "describe every colour " * 2000}

    response = client.post(
        "/api/v2/generate/chat/async",
        json={
            "model": CHAT_MODEL,
            "messages": [*LONG_HISTORY[:-1], overlong_latest_user],
            "max_tokens": 64,
            "horde": {"max_context_length": 2048, "context_fit": "truncate_oldest"},
        },
        headers=request_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "PromptExceedsContext"


@pytest.mark.parametrize("field", ["temperature", "top_p", "frequency_penalty", "presence_penalty"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_native_http_refuses_nonfinite_sampling_values(client, request_headers: dict[str, str], field: str, value: float) -> None:
    body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, field: value}
    response = client.post("/api/v2/generate/chat/async", data=json.dumps(body), content_type="application/json", headers=request_headers)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "BadRequest"


@pytest.mark.parametrize(("content", "reason"), [("Blue.\x00", "eos"), ("Blue.", "eos\x00")])
def test_worker_nuls_are_sanitised_in_the_atomic_structured_submission(
    client, request_headers: dict[str, str], chat_worker: FakeChatWorker, content: str, reason: str
) -> None:
    request_id = _submit_chat_request(client, request_headers)
    popped = chat_worker.pop()
    response = chat_worker.submit(
        build_chat_submit_body(generation_id=popped["id"], content=content, model_id=CHAT_MODEL, native_finish_reason=reason)
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["reward"] > 0
    status = _chat_status(client, request_headers, request_id)
    assert status["done"]
    [generation] = status["generations"]
    assert generation["text"] == generation["message"]["content"] == content.replace("\x00", "\ufffd")
    assert generation["native_finish_reason"] == reason.replace("\x00", "\ufffd")
    duplicate = chat_worker.submit(
        build_chat_submit_body(generation_id=popped["id"], content=content, model_id=CHAT_MODEL, native_finish_reason=reason),
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["rc"] == "DuplicateGen"
    assert "reward" not in duplicate.get_json()
    after_duplicate = _chat_status(client, request_headers, request_id)
    assert after_duplicate["generations"] == status["generations"]
    assert after_duplicate["kudos"] == status["kudos"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"messages": [{"role": "user", "content": "Blue\x00"}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "Blue\x00"}]}]},
        {"messages": [{"role": "user", "content": "Blue", "name": "name\x00"}]},
        {"stop": "stop\x00"},
        {"tools": [{"type": "function", "function": {"name": "lookup", "description": "Look\x00"}}]},
        {"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"properties": {"bad\x00key": {}}}}}]},
        {"response_format": {"type": "json_schema", "json_schema": {"name": "n", "schema": {"properties": {"bad\x00key": {}}}}}},
    ],
)
def test_client_nuls_are_refused_before_storage_and_leave_the_session_usable(
    app, client, request_headers: dict[str, str], overrides: dict[str, Any]
) -> None:
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    with app.app_context():
        before = db.session.query(TextWaitingPrompt).count()
    body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, **overrides}
    response = client.post("/api/v2/generate/chat/async", json=body, headers=request_headers)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "NulByteInPayload"
    with app.app_context():
        assert db.session.query(TextWaitingPrompt).count() == before
    valid_id = _submit_chat_request(client, request_headers)
    _cancel(client, request_headers, "chat", valid_id)


LOOKUP_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_weather",
        "description": "Look up the weather.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}
"""A function tool as an OpenAI client sends it."""

TOOL_HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "Weather in Oslo, then Bergen?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_oslo", "type": "function", "function": {"name": "lookup_weather", "arguments": '{"city":"Oslo"}'}}],
    },
    {"role": "tool", "content": "Rain, 7C", "tool_call_id": "call_oslo"},
]
"""A conversation in which the model called a tool once and the client answered it."""

TOOLS_REQUEST_EXTRAS: dict[str, Any] = {
    "tools": [LOOKUP_TOOL],
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "response_format": {"type": "json_object"},
}
"""The four extra keys of a tools request, as the client sends them and as the worker receives them."""

LONG_ARGUMENTS = '{"city":"Bergen","detail":"' + "d" * 160 + '"}'
"""Tool call arguments long enough that their characters, and not the minimum, set the output estimate."""

TOOLS_FEATURES = ["tools", "json_object", "tools_with_response_format"]
"""The features a worker declares to serve ``TOOLS_REQUEST_EXTRAS``."""


def test_a_tools_request_waits_for_a_worker_declaring_tools_and_is_not_possible_without_one(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    assert chat_worker.pop(chat_features=["json_object"])["id"] is None
    request_id = _submit_chat_request(client, request_headers, messages=TOOL_HISTORY, **TOOLS_REQUEST_EXTRAS)

    assert chat_worker.pop(chat_features=["json_object"])["id"] is None
    assert _chat_status(client, request_headers, request_id)["is_possible"] is False
    assert chat_worker.pop(chat_features=TOOLS_FEATURES)["id"] is not None
    _cancel(client, request_headers, "chat", request_id)


def test_a_tool_history_alone_needs_a_worker_declaring_tools(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, messages=TOOL_HISTORY)

    assert chat_worker.pop(chat_features=[])["id"] is None
    pop_result = chat_worker.pop(chat_features=["tools"])
    assert pop_result["id"] is not None, pop_result
    assert not {"tools", "tool_choice", "parallel_tool_calls", "response_format"} & set(pop_result["payload"])
    _cancel(client, request_headers, "chat", request_id)


def test_a_tool_call_reply_is_popped_submitted_shown_and_charged_by_its_arguments(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    monkeypatch,
) -> None:
    from horde.classes.kobold import processing_generation
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    empty_generation_counts: list[dict[str, str]] = []

    class RecordingCounter:
        def add(self, amount: int, attributes: dict[str, str]) -> None:
            empty_generation_counts.append(attributes)

    monkeypatch.setattr(processing_generation, "text_empty_generations", RecordingCounter())
    request_id = _submit_chat_request(client, request_headers, messages=TOOL_HISTORY, max_tokens=80, **TOOLS_REQUEST_EXTRAS)

    pop_result = chat_worker.pop(chat_features=TOOLS_FEATURES)

    assert pop_result["id"] is not None, pop_result
    assert pop_result["payload"]["messages"] == TOOL_HISTORY
    assert {key: pop_result["payload"][key] for key in TOOLS_REQUEST_EXTRAS} == TOOLS_REQUEST_EXTRAS
    tool_call = {"id": "call_bergen", "type": "function", "function": {"name": "lookup_weather", "arguments": LONG_ARGUMENTS}}
    submit_response = chat_worker.submit(
        build_chat_submit_body(
            generation_id=pop_result["id"],
            content=None,
            tool_calls=[tool_call],
            finish_reason="tool_calls",
            model_id=CHAT_MODEL,
        ),
    )
    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)

    status = _chat_status(client, request_headers, request_id)
    assert status["done"] is True
    [generation] = status["generations"]
    assert generation["message"] == {"role": "assistant", "content": None, "tool_calls": [tool_call]}
    assert generation["finish_reason"] == "tool_calls"
    assert generation["text"] == ""
    assert empty_generation_counts == []

    with app.app_context():
        flattened_prompt = db.session.get(TextWaitingPrompt, uuid.UUID(request_id)).prompt
    assert "tool: Rain, 7C" in flattened_prompt
    assert '"call_oslo"' in flattened_prompt
    assert '"lookup_weather","description"' in flattened_prompt
    _submit_text_request(client, request_headers, prompt=flattened_prompt)
    text_pop = chat_worker.pop()
    assert text_pop["id"] is not None, text_pop
    charged_characters = "x" * (len("lookup_weather") + len(LONG_ARGUMENTS))
    text_submit = chat_worker.submit({"id": text_pop["id"], "generation": charged_characters, "state": "ok", "seed": 0})
    assert text_submit.status_code == 200, text_submit.get_data(as_text=True)
    assert submit_response.get_json()["reward"] == text_submit.get_json()["reward"]


def test_a_json_schema_request_goes_only_to_a_worker_declaring_json_schema(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "colour", "schema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    }
    request_id = _submit_chat_request(client, request_headers, response_format=response_format)

    assert chat_worker.pop(chat_features=["json_object", "tools"])["id"] is None
    pop_result = chat_worker.pop(chat_features=["json_schema"])

    assert pop_result["id"] is not None, pop_result
    assert pop_result["payload"]["response_format"] == response_format
    _cancel(client, request_headers, "chat", request_id)


@pytest.mark.parametrize(
    "chat_body_overrides",
    [
        {"tool_choice": "auto"},
        {"tool_choice": "none"},
        {"parallel_tool_calls": False},
        {"tools": [LOOKUP_TOOL], "tool_choice": {"type": "function", "function": {"name": "send_email"}}},
    ],
    ids=["tool-choice-without-tools", "tool-choice-none-without-tools", "parallel-tool-calls-without-tools", "unknown-forced-function"],
)
def test_a_tool_choice_the_tools_do_not_allow_is_a_conflict(
    client,
    request_headers: dict[str, str],
    chat_body_overrides: dict[str, Any],
) -> None:
    chat_body = {"model": CHAT_MODEL, "messages": CHAT_MESSAGES, **chat_body_overrides}

    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatRequestConflict"


EMAIL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {"name": "send_email", "parameters": {"type": "object", "properties": {"to": {"type": "string"}}}},
}
"""A second function tool, beside ``LOOKUP_TOOL``."""

NAMED_LOOKUP_CHOICE: dict[str, Any] = {"type": "function", "function": {"name": "lookup_weather"}}
"""A ``tool_choice`` forcing ``LOOKUP_TOOL``."""

TOOL_KEYS = frozenset(
    {
        "tools",
        "tool_choice",
        "parallel_tool_calls",
    },
)
"""The payload keys that carry tool definitions and constraints."""


def _stored_chat_declaration(app, worker_name: str) -> tuple[list[str], float | None]:
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        stored_row = db.session.query(TextWorker.chat_features, TextWorker.chat_tools_max_temperature).filter_by(name=worker_name).one()
        return list(stored_row.chat_features or ()), stored_row.chat_tools_max_temperature


def test_a_pop_stores_declared_guarantees_beside_features_and_the_tool_temperature_limit(app, chat_worker: FakeChatWorker) -> None:
    chat_worker.pop(chat_features=["tools", "not_a_known_name"], chat_guarantees=["tool_choice_enforced"], tools_max_temperature=1.0)

    assert _stored_chat_declaration(app, WORKER_NAME) == (["tool_choice_enforced", "tools"], 1.0)

    chat_worker.pop(chat_features=["tools"], chat_guarantees=[])

    assert _stored_chat_declaration(app, WORKER_NAME) == (["tools"], None)


def test_a_negative_tool_temperature_limit_is_refused_on_pop(chat_worker: FakeChatWorker) -> None:
    response = chat_worker.pop_response(tools_max_temperature=-0.5)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "ChatDeclarationInvalid"


def test_a_strict_named_tool_choice_waits_for_a_worker_enforcing_tool_choice_and_receives_only_that_tool(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, tools=[EMAIL_TOOL, LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE)

    assert chat_worker.pop(chat_features=["tools"], chat_guarantees=["prompt_overflow_refused"])["id"] is None
    pop_result = chat_worker.pop(chat_features=["tools"])

    assert pop_result["id"] is not None, pop_result
    assert pop_result["payload"]["tools"] == [LOOKUP_TOOL]
    assert pop_result["payload"]["tool_choice"] == "required"
    _cancel(client, request_headers, "chat", request_id)


def test_a_best_effort_named_tool_choice_is_served_by_a_worker_without_guarantees(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(
        client,
        request_headers,
        tools=[EMAIL_TOOL, LOOKUP_TOOL],
        tool_choice=NAMED_LOOKUP_CHOICE,
        horde={"conformance": "best_effort"},
    )

    pop_result = chat_worker.pop(chat_features=["tools"], chat_guarantees=[])

    assert pop_result["id"] is not None, pop_result
    assert pop_result["payload"]["tools"] == [LOOKUP_TOOL]
    _cancel(client, request_headers, "chat", request_id)


def test_tool_choice_none_forwards_no_tool_fields(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, tools=[LOOKUP_TOOL], tool_choice="none", parallel_tool_calls=False)

    pop_result = chat_worker.pop(chat_features=[], chat_guarantees=["prompt_overflow_refused"])

    assert pop_result["id"] is not None, pop_result
    assert not TOOL_KEYS & set(pop_result["payload"])
    _cancel(client, request_headers, "chat", request_id)


def test_a_tool_request_hotter_than_a_worker_tool_temperature_limit_waits_for_another_worker(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, tools=[LOOKUP_TOOL], temperature=1.5)

    assert chat_worker.pop(chat_features=["tools"], tools_max_temperature=1.0)["id"] is None
    assert chat_worker.pop(chat_features=["tools"])["id"] is not None
    _cancel(client, request_headers, "chat", request_id)


def test_a_strict_named_tool_choice_no_online_worker_enforces_reports_the_missing_guarantee(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, tools=[EMAIL_TOOL, LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE)

    assert chat_worker.pop(chat_features=["tools"], chat_guarantees=["prompt_overflow_refused"])["id"] is None
    status = _chat_status(client, request_headers, request_id)

    assert status["is_possible"] is False
    assert status["impossible_reason"] == {
        "kind": "declaration_gap",
        "missing_declarations": ["tool_choice_enforced"],
        "exceeded_limits": [],
    }
    _cancel(client, request_headers, "chat", request_id)


def test_a_tool_request_hotter_than_every_worker_tool_temperature_limit_reports_the_limit(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, tools=[LOOKUP_TOOL], temperature=1.5)

    assert chat_worker.pop(chat_features=["tools"], tools_max_temperature=1.0)["id"] is None
    status = _chat_status(client, request_headers, request_id)

    assert status["is_possible"] is False
    assert status["impossible_reason"] == {
        "kind": "declaration_gap",
        "missing_declarations": [],
        "exceeded_limits": ["tools_max_temperature"],
    }
    _cancel(client, request_headers, "chat", request_id)


def test_a_request_over_every_worker_chat_output_limit_reports_the_limit(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, max_tokens=400)

    assert chat_worker.pop(chat_max_length=300)["id"] is None
    status = _chat_status(client, request_headers, request_id)

    assert status["is_possible"] is False
    assert status["impossible_reason"] == {
        "kind": "declaration_gap",
        "missing_declarations": [],
        "exceeded_limits": ["max_length"],
    }
    _cancel(client, request_headers, "chat", request_id)


@pytest.mark.parametrize("conformance", ["strict", "best_effort"])
def test_tools_beside_a_response_format_without_a_worker_serving_the_combination_report_the_gap(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    conformance: str,
) -> None:
    request_id = _submit_chat_request(
        client,
        request_headers,
        tools=[LOOKUP_TOOL],
        response_format={"type": "json_object"},
        horde={"conformance": conformance},
    )

    worker_guarantees = ["prompt_overflow_refused"]
    assert chat_worker.pop(chat_features=["tools", "json_object"], chat_guarantees=worker_guarantees)["id"] is None
    status = _chat_status(client, request_headers, request_id)

    assert status["is_possible"] is False
    assert status["impossible_reason"] == {
        "kind": "declaration_gap",
        "missing_declarations": ["tools_with_response_format"],
        "exceeded_limits": [],
    }
    assert chat_worker.pop(chat_features=TOOLS_FEATURES, chat_guarantees=worker_guarantees)["id"] is not None
    _cancel(client, request_headers, "chat", request_id)


def test_a_request_a_worker_can_take_reports_no_impossible_reason(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    chat_worker.pop(chat_features=["tools"])
    request_id = _submit_chat_request(client, request_headers, tools=[LOOKUP_TOOL])

    status = _chat_status(client, request_headers, request_id)

    assert status["is_possible"] is True
    assert status["impossible_reason"] is None
    _cancel(client, request_headers, "chat", request_id)


VERIFIED_FEATURES = ["tools", "json_object", "json_schema", "tools_with_response_format"]
"""The features a worker declares to pop every request the reply verification tests make."""

COLOUR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}
"""A JSON Schema for one named colour."""


def _colour_format(*, strict: bool) -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": {"name": "colour", "schema": COLOUR_SCHEMA, "strict": strict}}


def _tool_call(name: str = "lookup_weather", call_id: str = "call_one") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": '{"city":"Bergen"}'}}


def _serve_reply(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    request_overrides: dict[str, Any],
    *,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> tuple[str, Any]:
    """Queue a chat request, pop it with every feature and guarantee declared, and submit one reply to it."""
    request_id = _submit_chat_request(client, request_headers, **request_overrides)
    pop_result = chat_worker.pop(chat_features=VERIFIED_FEATURES)
    assert pop_result["id"] is not None, pop_result
    submit_response = chat_worker.submit(
        build_chat_submit_body(
            generation_id=pop_result["id"],
            content=content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            model_id=CHAT_MODEL,
        ),
    )
    return request_id, submit_response


NONCONFORMANT_REPLIES = [
    pytest.param({"tools": [LOOKUP_TOOL], "tool_choice": "none"}, None, [_tool_call()], id="none-with-call"),
    pytest.param({"tools": [LOOKUP_TOOL], "tool_choice": "required"}, "Sunny.", None, id="required-without-call"),
    pytest.param({"tools": [EMAIL_TOOL, LOOKUP_TOOL], "tool_choice": NAMED_LOOKUP_CHOICE}, "Sunny.", None, id="named-without-call"),
    pytest.param(
        {"tools": [EMAIL_TOOL, LOOKUP_TOOL], "tool_choice": NAMED_LOOKUP_CHOICE},
        None,
        [_tool_call("send_email")],
        id="named-other-function",
    ),
    pytest.param({"response_format": {"type": "json_object"}}, "Blue.", None, id="json-object-not-json"),
    pytest.param({"response_format": _colour_format(strict=True)}, '{"name": 7}', None, id="strict-schema-mismatch"),
]
"""Requests with a reply each that the request's ``tool_choice`` or ``response_format`` rules out."""


@pytest.mark.parametrize(("request_overrides", "content", "tool_calls"), NONCONFORMANT_REPLIES)
def test_a_strict_request_refuses_a_reply_its_tool_choice_or_response_format_rules_out(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    request_overrides: dict[str, Any],
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
) -> None:
    request_id, submit_response = _serve_reply(
        client, request_headers, chat_worker, request_overrides, content=content, tool_calls=tool_calls
    )

    assert submit_response.status_code == 400
    assert submit_response.get_json()["rc"] == "ChatSubmitNonconformant"
    assert _chat_status(client, request_headers, request_id)["done"] is False
    _cancel(client, request_headers, "chat", request_id)


@pytest.mark.parametrize(("request_overrides", "content", "tool_calls"), NONCONFORMANT_REPLIES)
def test_a_best_effort_request_shows_that_reply_as_submitted(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    request_overrides: dict[str, Any],
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
) -> None:
    best_effort_overrides = {**request_overrides, "horde": {"conformance": "best_effort"}}

    request_id, submit_response = _serve_reply(
        client, request_headers, chat_worker, best_effort_overrides, content=content, tool_calls=tool_calls
    )

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    [generation] = _chat_status(client, request_headers, request_id)["generations"]
    expected_message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        expected_message["tool_calls"] = tool_calls
    assert generation["message"] == expected_message


@pytest.mark.parametrize("conformance", ["strict", "best_effort"])
@pytest.mark.parametrize(
    "tool_calls",
    [[_tool_call("send_email")], [_tool_call(), _tool_call()]],
    ids=["function-not-offered", "repeated-call-id"],
)
def test_a_reply_no_client_could_use_is_refused_under_every_conformance_mode(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    tool_calls: list[dict[str, Any]],
    conformance: str,
) -> None:
    request_overrides = {"tools": [LOOKUP_TOOL], "horde": {"conformance": conformance}}

    request_id, submit_response = _serve_reply(client, request_headers, chat_worker, request_overrides, content=None, tool_calls=tool_calls)

    assert submit_response.status_code == 400
    assert submit_response.get_json()["rc"] == "ChatSubmitInvalid"
    _cancel(client, request_headers, "chat", request_id)


def test_a_strict_single_call_request_shows_only_the_first_of_several_calls(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    monkeypatch,
) -> None:
    from horde.apis.v2 import base

    log_records: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(base.logfire, "info", lambda event_name, **attributes: log_records.append((event_name, attributes)))
    first_call, second_call = _tool_call(call_id="call_one"), _tool_call("send_email", "call_two")
    request_overrides = {"tools": [EMAIL_TOOL, LOOKUP_TOOL], "parallel_tool_calls": False}

    request_id, submit_response = _serve_reply(
        client, request_headers, chat_worker, request_overrides, content=None, tool_calls=[first_call, second_call]
    )

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    [generation] = _chat_status(client, request_headers, request_id)["generations"]
    assert generation["message"] == {"role": "assistant", "content": None, "tool_calls": [first_call]}
    assert generation["finish_reason"] == "tool_calls"
    [submit_record] = [attributes for event_name, attributes in log_records if event_name == "horde.job_submit.done"]
    assert submit_record["dropped_tool_calls"] == 1


def test_a_strict_request_without_parallel_tool_calls_goes_to_a_worker_without_the_single_call_guarantee(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """The Horde reduces a strict reply's tool calls to the first itself, so routing does not need
    ``single_tool_call_enforced``."""
    request_id = _submit_chat_request(client, request_headers, tools=[LOOKUP_TOOL], parallel_tool_calls=False)

    pop_result = chat_worker.pop(chat_features=["tools"], chat_guarantees=["prompt_overflow_refused"])

    assert pop_result["id"] is not None, pop_result
    assert pop_result["payload"]["parallel_tool_calls"] is False
    _cancel(client, request_headers, "chat", request_id)


@pytest.mark.parametrize("conformance", ["strict", "best_effort"])
def test_a_named_tool_choice_reply_finishes_with_stop_as_under_openai(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    conformance: str,
) -> None:
    request_overrides = {"tools": [EMAIL_TOOL, LOOKUP_TOOL], "tool_choice": NAMED_LOOKUP_CHOICE, "horde": {"conformance": conformance}}

    request_id, submit_response = _serve_reply(
        client, request_headers, chat_worker, request_overrides, content=None, tool_calls=[_tool_call()]
    )

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    [generation] = _chat_status(client, request_headers, request_id)["generations"]
    assert generation["finish_reason"] == "stop"
    assert generation["message"]["tool_calls"] == [_tool_call()]


def test_a_strict_schema_reply_matching_the_schema_completes(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_overrides = {"response_format": _colour_format(strict=True)}

    request_id, submit_response = _serve_reply(client, request_headers, chat_worker, request_overrides, content='{"name": "blue"}')

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    assert _chat_status(client, request_headers, request_id)["generations"][0]["text"] == '{"name": "blue"}'


def test_a_non_strict_schema_accepts_any_json_object(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_overrides = {"response_format": _colour_format(strict=False)}

    request_id, submit_response = _serve_reply(client, request_headers, chat_worker, request_overrides, content='{"name": 7}')

    assert submit_response.status_code == 200, submit_response.get_data(as_text=True)
    assert _chat_status(client, request_headers, request_id)["done"] is True


def _refuse_then_fault(
    chat_worker: FakeChatWorker,
    *,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    fault_reason: str | None = None,
) -> Any:
    """Pop one job, submit a reply to it, then submit the fault a worker sends after a refusal; return the reply's response."""
    pop_result = chat_worker.pop(chat_features=VERIFIED_FEATURES)
    assert pop_result["id"] is not None, pop_result
    reply_response = chat_worker.submit(
        build_chat_submit_body(
            generation_id=pop_result["id"],
            content=content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            model_id=CHAT_MODEL,
        ),
    )
    fault_response = chat_worker.submit(
        build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL, fault_reason=fault_reason),
    )
    assert fault_response.status_code == 200, fault_response.get_data(as_text=True)
    return reply_response


def _worker_counters(app, worker_name: str) -> tuple[int, int, int]:
    """Return the worker's stored ``aborted_jobs``, ``uncompleted_jobs`` and ``nonconformant_replies``."""
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        aborted_jobs, uncompleted_jobs, nonconformant_replies = (
            db.session.query(TextWorker.aborted_jobs, TextWorker.uncompleted_jobs, TextWorker.nonconformant_replies)
            .filter_by(name=worker_name)
            .one()
        )
        return aborted_jobs, uncompleted_jobs, nonconformant_replies


def test_a_request_faulted_after_refused_replies_reports_the_last_refusal(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    chat_events: Any,
) -> None:
    from horde.classes.base.waiting_prompt import FAULTED_GENERATIONS_LIMIT

    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})
    replies = ["Blue."] * FAULTED_GENERATIONS_LIMIT + ["[1]"]
    refusals = [_refuse_then_fault(chat_worker, content=reply) for reply in replies]

    assert [refusal.status_code for refusal in refusals] == [400] * len(replies)
    last_refusal = refusals[-1].get_json()
    assert refusals[0].get_json()["message"] != last_refusal["message"]
    status = _chat_status(client, request_headers, request_id)
    assert status["faulted"] is True
    assert status["fault_reason"] == last_refusal["message"]
    assert status["fault_rc"] == "ChatSubmitNonconformant"
    faulted_event = {
        "id": request_id,
        "event": "faulted",
        "generations": 0,
        "fault_reason": last_refusal["message"],
        "fault_rc": "ChatSubmitNonconformant",
    }
    assert (chat_signal_channel(request_id), faulted_event) in _received_events(chat_events)


def test_an_unfaulted_request_reports_no_fault_reason_after_a_refused_reply(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})

    assert _refuse_then_fault(chat_worker, content="Blue.").status_code == 400

    status = _chat_status(client, request_headers, request_id)
    assert status["faulted"] is False
    assert status["fault_reason"] is None
    assert status["fault_rc"] is None
    _cancel(client, request_headers, "chat", request_id)


@pytest.mark.parametrize(
    ("request_overrides", "content", "tool_calls", "refusal_rc", "dropped_jobs", "nonconformant_replies"),
    [
        pytest.param({"response_format": {"type": "json_object"}}, "Blue.", None, "ChatSubmitNonconformant", 0, 1, id="nonconformant"),
        pytest.param({"tools": [LOOKUP_TOOL]}, None, [_tool_call("send_email")], "ChatSubmitInvalid", 1, 0, id="invalid"),
    ],
)
def test_a_fault_after_a_refused_reply_counts_as_a_dropped_job_unless_the_reply_was_nonconformant(
    app,
    client,
    request_headers: dict[str, str],
    request_overrides: dict[str, Any],
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    refusal_rc: str,
    dropped_jobs: int,
    nonconformant_replies: int,
) -> None:
    worker = FakeChatWorker(client=client, headers=request_headers, name=f"CICD Chat Refusals {uuid.uuid4().hex[:8]}", models=[CHAT_MODEL])
    request_id = _submit_chat_request(client, request_headers, **request_overrides)

    refusal = _refuse_then_fault(worker, content=content, tool_calls=tool_calls)

    assert refusal.get_json()["rc"] == refusal_rc
    assert _worker_counters(app, worker.name) == (dropped_jobs, dropped_jobs, nonconformant_replies)
    details = client.get(f"/api/v2/workers/name/{worker.name}", headers=request_headers)
    assert details.status_code == 200, details.get_data(as_text=True)
    assert details.get_json()["nonconformant_replies"] == nonconformant_replies
    _cancel(client, request_headers, "chat", request_id)


def test_a_nonconformant_reply_for_a_stale_aborted_generation_is_refused_as_aborted_without_a_stored_refusal(
    app,
    client,
    request_headers: dict[str, str],
) -> None:
    """A reply for a generation the stale-generation path faulted returns ``AbortedGen`` before its conformance check:
    the request does not store a refusal, the abort counts as a dropped job and the worker's nonconformant replies do
    not change."""
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    worker = FakeChatWorker(client=client, headers=request_headers, name=f"CICD Chat Stale {uuid.uuid4().hex[:8]}", models=[CHAT_MODEL])
    _submit_chat_request(client, request_headers, response_format={"type": "json_object"})
    pop_result = worker.pop(chat_features=VERIFIED_FEATURES)
    assert pop_result["id"] is not None, pop_result
    with app.app_context():
        generation = db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert generation is not None
        request_id = str(generation.wp_id)
        generation.start_time = datetime.utcnow() - timedelta(seconds=generation.job_ttl + 1)
        db.session.commit()
        assert generation.is_stale()
        generation.fault_and_restart(stale=True)

    response = worker.submit(
        build_chat_submit_body(
            generation_id=pop_result["id"],
            content="Blue.",
            finish_reason="stop",
            model_id=CHAT_MODEL,
        ),
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "AbortedGen"
    assert _worker_counters(app, worker.name) == (1, 1, 0)
    with app.app_context():
        stored_refusal = (
            db.session.query(TextWaitingPrompt.chat_fault_reason, TextWaitingPrompt.chat_fault_rc)
            .filter(TextWaitingPrompt.id == uuid.UUID(request_id))
            .one()
        )
        stored_refusal_rc = (
            db.session.query(TextProcessingGeneration.chat_refusal_rc)
            .filter(TextProcessingGeneration.id == uuid.UUID(pop_result["id"]))
            .scalar()
        )
    assert tuple(stored_refusal) == (None, None)
    assert stored_refusal_rc is None
    _cancel(client, request_headers, "chat", request_id)


def _fault_past_the_limit(chat_worker: FakeChatWorker, fault_reason: str) -> None:
    """Pop and fault the queued request's job with ``fault_reason`` until the request faults."""
    from horde.classes.base.waiting_prompt import FAULTED_GENERATIONS_LIMIT

    for _ in range(FAULTED_GENERATIONS_LIMIT + 1):
        pop_result = chat_worker.pop()
        assert pop_result["id"] is not None, pop_result
        response = chat_worker.submit(
            build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL, fault_reason=fault_reason),
        )
        assert response.status_code == 200, response.get_data(as_text=True)


def test_a_request_faulted_with_a_worker_fault_reason_reports_the_horde_message_for_it(
    app,
    client,
    request_headers: dict[str, str],
    chat_events: Any,
) -> None:
    """The status and the faulted signal carry the Horde's sentence for the reason, and each fault is a dropped job."""
    from horde_sdk.worker.chat_declaration import ChatFaultReason

    from horde.classes.base.waiting_prompt import FAULTED_GENERATIONS_LIMIT
    from horde.classes.kobold.chat_request import CHAT_WORKER_FAULT_MESSAGES

    worker = FakeChatWorker(client=client, headers=request_headers, name=f"CICD Chat Faults {uuid.uuid4().hex[:8]}", models=[CHAT_MODEL])
    request_id = _submit_chat_request(client, request_headers)

    _fault_past_the_limit(worker, "prompt_overflow")

    expected_message = CHAT_WORKER_FAULT_MESSAGES[ChatFaultReason.PROMPT_OVERFLOW]
    status = _chat_status(client, request_headers, request_id)
    assert status["faulted"] is True
    assert status["fault_reason"] == expected_message
    assert status["fault_rc"] == "ChatWorkerFault"
    faulted_event = {
        "id": request_id,
        "event": "faulted",
        "generations": 0,
        "fault_reason": expected_message,
        "fault_rc": "ChatWorkerFault",
    }
    assert (chat_signal_channel(request_id), faulted_event) in _received_events(chat_events)
    dropped_jobs = FAULTED_GENERATIONS_LIMIT + 1
    assert _worker_counters(app, worker.name) == (dropped_jobs, dropped_jobs, 0)


def test_an_unknown_worker_fault_reason_is_ignored(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    request_id = _submit_chat_request(client, request_headers)

    _fault_past_the_limit(chat_worker, "reactor_meltdown")

    status = _chat_status(client, request_headers, request_id)
    assert status["faulted"] is True
    assert status["fault_reason"] is None
    assert status["fault_rc"] is None


def test_a_reply_refused_fault_keeps_the_message_of_the_refusal_it_follows(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    from horde.classes.base.waiting_prompt import FAULTED_GENERATIONS_LIMIT

    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})
    refusals = [
        _refuse_then_fault(chat_worker, content="Blue.", fault_reason="reply_refused") for _ in range(FAULTED_GENERATIONS_LIMIT + 1)
    ]

    assert [refusal.status_code for refusal in refusals] == [400] * len(refusals)
    status = _chat_status(client, request_headers, request_id)
    assert status["faulted"] is True
    assert status["fault_reason"] == refusals[-1].get_json()["message"]
    assert status["fault_rc"] == "ChatSubmitNonconformant"


class _CounterRecorder:
    """Stands in for a metric counter and keeps every increment with its attributes."""

    def __init__(self) -> None:
        self.increments: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str]) -> None:
        self.increments.append((amount, attributes))


@pytest.fixture
def worker_fault_counter(monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
    """Replace the chat worker fault counter with a recorder."""
    from horde import metrics

    recorder = _CounterRecorder()
    monkeypatch.setattr(metrics, "chat_worker_faults", recorder)
    return recorder


@pytest.mark.parametrize(
    ("fault_reason", "expected_label"),
    [
        ("prompt_overflow", "prompt_overflow"),
        ("reactor_meltdown", "unknown"),
        (None, "none"),
    ],
)
def test_a_faulted_chat_submit_is_counted_once_by_its_reason_label(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    worker_fault_counter: _CounterRecorder,
    fault_reason: str | None,
    expected_label: str,
) -> None:
    """A known reason is counted as itself, any other text as ``unknown`` and a missing reason as ``none``."""
    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    response = chat_worker.submit(
        build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL, fault_reason=fault_reason),
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert worker_fault_counter.increments == [
        (
            1,
            {
                "horde.reason": expected_label,
                "horde.worker_trusted": "true",
            },
        ),
    ]
    _cancel(client, request_headers, "chat", request_id)


def test_a_refused_reply_and_the_fault_after_it_are_counted_once_by_the_workers_reason(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    worker_fault_counter: _CounterRecorder,
) -> None:
    """The refusal the Horde stores is not counted; the worker's fault after it is counted with the worker's reason."""
    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})

    refusal = _refuse_then_fault(chat_worker, content="Blue.", fault_reason="reply_refused")

    assert refusal.status_code == 400, refusal.get_data(as_text=True)
    assert worker_fault_counter.increments == [
        (
            1,
            {
                "horde.reason": "reply_refused",
                "horde.worker_trusted": "true",
            },
        ),
    ]
    _cancel(client, request_headers, "chat", request_id)


def _status_of_loaded_request(app, loaded_request: Any) -> dict[str, Any]:
    """Return the full status the status route would build from ``loaded_request``, an instance loaded earlier."""
    with app.app_context():
        return loaded_request.get_status(
            request_avg=0,
            active_worker_count=(0, 0),
            has_valid_workers=True,
            wp_queue_stats=(0, 0, 0),
        )


@pytest.mark.parametrize("in_flight_result", [None, "success", "fault"])
def test_cancelled_chat_status_settles_only_after_in_flight_work(
    client,
    request_headers,
    chat_worker,
    in_flight_result,
):
    request_id = _submit_chat_request(client, request_headers)
    popped = chat_worker.pop() if in_flight_result else None
    _cancel(client, request_headers, "chat", request_id)
    status = _chat_status(client, request_headers, request_id)
    assert status["waiting"] == 0
    assert status["done"] is (in_flight_result is None)
    if popped:
        assert status["processing"] == 1
        if in_flight_result == "success":
            response = chat_worker.submit(
                build_chat_submit_body(
                    generation_id=popped["id"],
                    content="Finished after cancellation.",
                    model_id=CHAT_MODEL,
                )
            )
        else:
            response = chat_worker.submit(build_faulted_chat_submit_body(generation_id=popped["id"], model_id=CHAT_MODEL))
        assert response.status_code == 200, response.get_data(as_text=True)
        status = _chat_status(client, request_headers, request_id)
        assert status["done"] is True
        assert status["processing"] == status["waiting"] == 0
        assert len(status["generations"]) == int(in_flight_result == "success")


def test_a_status_built_from_a_request_loaded_before_a_refused_reply_is_not_done_without_a_generation(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """The status route loads the request, then spends time on worker availability and the queue before it reads the
    generations. A worker's fault committed in between must not make the status done with no generation and no fault:
    the job is restarted in the fault's commit and the status reads the request's state in one statement."""
    from sqlalchemy.orm import Session

    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})
    pop_result = chat_worker.pop(chat_features=VERIFIED_FEATURES)
    assert pop_result["id"] is not None, pop_result
    with app.app_context():
        status_reader_session = Session(db.engine)
    try:
        loaded_request = status_reader_session.get(TextWaitingPrompt, uuid.UUID(request_id))
        assert loaded_request is not None
        assert loaded_request.n == 0

        refusal = chat_worker.submit(
            build_chat_submit_body(generation_id=pop_result["id"], content="Blue.", finish_reason="stop", model_id=CHAT_MODEL),
        )
        assert refusal.get_json()["rc"] == "ChatSubmitNonconformant"
        fault = chat_worker.submit(build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL))
        assert fault.status_code == 200, fault.get_data(as_text=True)

        status = _status_of_loaded_request(app, loaded_request)
    finally:
        status_reader_session.close()
    assert (status["done"], status["faulted"], status["restarted"], status["waiting"]) == (False, False, 1, 1)
    _cancel(client, request_headers, "chat", request_id)


def test_a_restarted_request_is_not_completed_while_a_worker_takes_its_job_again(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a refused reply's fault, the pop that takes the restarted job commits the taken job with its generation, so
    another session never sees the request with no job waiting, none processing and only the faulted one finished."""
    from sqlalchemy.orm import Session

    from horde.classes.base.waiting_prompt import RequestCompletionSnapshot, WaitingPrompt
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})
    assert _refuse_then_fault(chat_worker, content="Blue.").get_json()["rc"] == "ChatSubmitNonconformant"
    completed_while_taken: list[bool] = []
    select_model = WaitingPrompt._select_model

    def read_completion_then_select_model(waiting_prompt: Any, worker: Any, declared_models: list[str] | None = None) -> str:
        # Runs inside the pop, between taking the job and creating its generation.
        with Session(db.engine) as other_session:
            stored_request = other_session.get(TextWaitingPrompt, uuid.UUID(request_id))
            stored_generations = other_session.query(TextProcessingGeneration).filter_by(wp_id=stored_request.id, fake=False).all()
            snapshot = RequestCompletionSnapshot(
                n=stored_request.n,
                faulted=stored_request.faulted,
                completed_jobs=sum(1 for generation in stored_generations if not generation.faulted and generation.generation is not None),
                processing_jobs=sum(1 for generation in stored_generations if not generation.faulted and generation.generation is None),
                fault_reason=None,
                fault_rc=None,
            )
            completed_while_taken.append(snapshot.is_completed(stored_request.jobs))
        return select_model(waiting_prompt, worker, declared_models)

    monkeypatch.setattr(WaitingPrompt, "_select_model", read_completion_then_select_model)
    reissued = chat_worker.pop(chat_features=VERIFIED_FEATURES)

    assert reissued["id"] is not None, reissued
    assert completed_while_taken == [False]
    _cancel(client, request_headers, "chat", request_id)


def test_a_repeated_faulted_submit_restarts_the_job_once(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """Only the submit that moves the generation from pending to faulted restarts its job: a repeated faulted submit,
    and a fault from an instance loaded before the first fault committed, leave the restarted count alone."""
    from sqlalchemy.orm import Session

    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    request_id = _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()
    assert pop_result["id"] is not None, pop_result
    with app.app_context():
        stale_session = Session(db.engine)
    try:
        stale_generation = stale_session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"]))
        assert stale_generation is not None
        assert stale_generation.faulted is False

        for _ in range(2):
            response = chat_worker.submit(build_faulted_chat_submit_body(generation_id=pop_result["id"], model_id=CHAT_MODEL))
            assert response.status_code == 200, response.get_data(as_text=True)
        with app.app_context():
            stale_generation.fault_and_restart()
            stored_n = db.session.query(TextWaitingPrompt.n).filter(TextWaitingPrompt.id == uuid.UUID(request_id)).scalar()
    finally:
        stale_session.close()

    assert stored_n == 1
    status = _chat_status(client, request_headers, request_id)
    assert (status["restarted"], status["waiting"], status["done"], status["faulted"]) == (1, 1, False, False)
    _cancel(client, request_headers, "chat", request_id)


def test_a_request_on_its_third_attempt_is_not_done_while_the_job_is_processing(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """Faulted generations do not count toward completion: two faulted attempts and a third one processing leave the
    request open, not done without a generation for the whole third attempt."""
    request_id = _submit_chat_request(client, request_headers, response_format={"type": "json_object"})
    for _ in range(2):
        assert _refuse_then_fault(chat_worker, content="Blue.").get_json()["rc"] == "ChatSubmitNonconformant"
    third_attempt = chat_worker.pop(chat_features=VERIFIED_FEATURES)
    assert third_attempt["id"] is not None, third_attempt

    status = _chat_status(client, request_headers, request_id)

    assert (status["done"], status["faulted"], status["processing"], status["restarted"]) == (False, False, 1, 2)
    _cancel(client, request_headers, "chat", request_id)


def test_reasoning_is_stored_and_shown_beside_the_reply_and_neither_it_nor_its_count_is_charged(
    app,
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    """The Horde charges and rewards on the reply it measures, so a reply with reasoning earns what the same reply
    without it earns."""
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    reasoning_request_id = _submit_chat_request(client, request_headers, max_tokens=400)
    reasoning_pop = chat_worker.pop()
    reasoning_submit = chat_worker.submit(
        build_chat_submit_body(
            generation_id=reasoning_pop["id"],
            content="Blue.",
            model_id=CHAT_MODEL,
            completion_tokens=300,
            reasoning_content="The user asked for a colour. " * 40,
            reasoning_tokens=297,
        ),
    )
    assert reasoning_submit.status_code == 200, reasoning_submit.get_data(as_text=True)

    _submit_chat_request(client, request_headers, max_tokens=400)
    plain_pop = chat_worker.pop()
    plain_submit = chat_worker.submit(build_chat_submit_body(generation_id=plain_pop["id"], content="Blue.", model_id=CHAT_MODEL))
    assert plain_submit.status_code == 200, plain_submit.get_data(as_text=True)

    [generation] = _chat_status(client, request_headers, reasoning_request_id)["generations"]
    assert generation["text"] == "Blue."
    assert generation["message"] == {
        "role": "assistant",
        "content": "Blue.",
        "reasoning_content": "The user asked for a colour. " * 40,
    }
    assert generation["completion_tokens"] == 300
    assert generation["reasoning_tokens"] == 297
    with app.app_context():
        stored = db.session.get(TextProcessingGeneration, uuid.UUID(reasoning_pop["id"]))
        assert stored.generation == "Blue."
        assert stored.reasoning_tokens == 297
    assert reasoning_submit.get_json()["reward"] == plain_submit.get_json()["reward"]


def test_a_submit_with_reasoning_over_the_text_cap_is_refused_as_invalid(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
) -> None:
    from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_TEXT_BYTES

    _submit_chat_request(client, request_headers)
    pop_result = chat_worker.pop()

    response = chat_worker.submit(
        build_chat_submit_body(
            generation_id=pop_result["id"],
            content="Blue.",
            model_id=CHAT_MODEL,
            reasoning_content="x" * (MAX_CHAT_TEXT_BYTES + 1),
        ),
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "ChatSubmitInvalid"
