# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A fake text worker that speaks the chat pop and submit fields, and the requester calls shared by chat tests.

The body builders are plain functions so a suite that mocks the native API can reuse them; the
[`FakeChatWorker`][tests.integration._fake_chat_worker.FakeChatWorker] drives them through a Flask test client.
[`submit_chat_request`][tests.integration._fake_chat_worker.submit_chat_request],
[`chat_status`][tests.integration._fake_chat_worker.chat_status],
[`cancel_request`][tests.integration._fake_chat_worker.cancel_request] and
[`take_worker_offline`][tests.integration._fake_chat_worker.take_worker_offline] are the requester and cleanup calls
around it. The module also holds the request fixtures, worker feature lists and image helpers shared by several chat test
modules.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from typing import TYPE_CHECKING, Any

from horde_sdk.generation_parameters.text.chat_policy import ChatGuarantee
from PIL import Image

if TYPE_CHECKING:
    from flask.testing import FlaskClient
    from werkzeug.test import TestResponse

__all__ = [
    "ANONYMOUS_HEADERS",
    "CHAT_MESSAGES",
    "CHAT_MODEL",
    "DEFAULT_CHAT_FEATURES",
    "DEFAULT_CHAT_GUARANTEES",
    "LONG_HISTORY",
    "LONG_HISTORY_LATEST_USER",
    "LONG_HISTORY_SYSTEM",
    "LOOKUP_TOOL",
    "MESSAGES_URL_TRANSPORTS",
    "OFFLOAD_THRESHOLD_BYTES",
    "PROGRESS_ROUTE",
    "TOOLS_FEATURES",
    "TOOLS_REQUEST_EXTRAS",
    "TOOL_HISTORY",
    "VERIFIED_FEATURES",
    "VISION_FEATURES",
    "FakeChatWorker",
    "build_chat_submit_body",
    "build_faulted_chat_submit_body",
    "build_pop_body",
    "build_tool_call",
    "cancel_request",
    "chat_status",
    "image_body",
    "png_data_url",
    "post_progress",
    "serve_reply",
    "submit_chat_request",
    "submit_image_chat_request",
    "take_worker_offline",
]

CHAT_MODEL = "elinas/chronos-70b-v2"
"""A text model listed in the horde's reference, so a chat request for it is accepted."""

CHAT_MESSAGES = [
    {"role": "system", "content": "You are a terse assistant."},
    {"role": "user", "content": [{"type": "text", "text": "Name one colour."}]},
]
"""The conversation sent by a chat request unless a test supplies another."""

DEFAULT_CHAT_FEATURES = ("seed", "presence_penalty")
"""The features the fake worker declares unless told otherwise, matching what koboldcpp declares."""

DEFAULT_CHAT_GUARANTEES = tuple(guarantee.value for guarantee in ChatGuarantee)
"""The guarantees the fake worker declares unless told otherwise: every one, as a fully conforming backend, so a
strict request is refused only where a test withholds a guarantee."""


def build_pop_body(
    *,
    name: str,
    models: list[str],
    bridge_agent: str,
    chat_jobs: bool | None,
    prompt_jobs: bool | None = None,
    model_id: str | None = None,
    chat_features: list[str] | None = None,
    chat_guarantees: list[str] | None = None,
    chat_max_context_length: int = 8192,
    chat_max_length: int = 1024,
    tools_max_temperature: float | None = None,
    max_context_length: int = 4096,
    max_length: int = 512,
    image_transports: list[str] | None = None,
) -> dict[str, Any]:
    """Return a text pop body, with the chat declaration when ``chat_jobs`` is true.

    Args:
        name: The worker name.
        models: The models the worker serves.
        bridge_agent: The worker's bridge agent string.
        chat_jobs: Whether to opt into chat jobs; None leaves the key out, as a worker that predates chat does.
        prompt_jobs: Whether to accept text jobs; None leaves the key out, which accepts them.
        model_id: The chat model; the first of ``models`` when omitted.
        chat_features: The declared chat features; ``DEFAULT_CHAT_FEATURES`` when omitted.
        chat_guarantees: The declared chat guarantees, sent in the same list as the features;
            ``DEFAULT_CHAT_GUARANTEES`` when omitted.
        chat_max_context_length: The chat context limit.
        chat_max_length: The chat output limit.
        tools_max_temperature: The tool temperature limit; left out of ``chat_limits`` when None.
        max_context_length: The text context limit.
        max_length: The text output limit.
        image_transports: The declared image transports; left out when None.

    Returns:
        The JSON body for ``POST /v2/generate/text/pop``.
    """
    pop_body: dict[str, Any] = {
        "name": name,
        "models": models,
        "bridge_agent": bridge_agent,
        "amount": 1,
        "max_context_length": max_context_length,
        "max_length": max_length,
    }
    if chat_jobs is not None:
        pop_body["chat_jobs"] = chat_jobs
    if prompt_jobs is not None:
        pop_body["prompt_jobs"] = prompt_jobs
    if chat_jobs:
        pop_body["chat_features"] = [
            *(DEFAULT_CHAT_FEATURES if chat_features is None else chat_features),
            *(DEFAULT_CHAT_GUARANTEES if chat_guarantees is None else chat_guarantees),
        ]
        pop_body["chat_limits"] = {"max_context_length": chat_max_context_length, "max_length": chat_max_length}
        if tools_max_temperature is not None:
            pop_body["chat_limits"]["tools_max_temperature"] = tools_max_temperature
        pop_body["model_id"] = models[0] if model_id is None else model_id
    if image_transports is not None:
        pop_body["image_transports"] = image_transports
    return pop_body


def build_chat_submit_body(
    *,
    generation_id: str,
    content: str | None,
    model_id: str,
    finish_reason: str = "stop",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    native_finish_reason: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    reasoning_tokens: int | None = None,
) -> dict[str, Any]:
    """Return a chat job's submit body, without ``generation``.

    Args:
        generation_id: The popped generation id.
        content: The assistant reply; null for a reply that only calls tools.
        model_id: The model the job ran on.
        finish_reason: Why generation stopped.
        prompt_tokens: The prompt token count; left out when None.
        completion_tokens: The generated token count; left out when None.
        native_finish_reason: The backend's own stop reason; left out when None.
        tool_calls: The tool calls the model made; left out of the message when None.
        reasoning_content: The model's reasoning; left out of the message when None.
        reasoning_tokens: The reasoning token count; left out when None.

    Returns:
        The JSON body for ``POST /v2/generate/text/submit``.
    """
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    submit_body: dict[str, Any] = {
        "id": generation_id,
        "message": message,
        "finish_reason": finish_reason,
        "model_id": model_id,
        "state": "ok",
    }
    optional_fields = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "native_finish_reason": native_finish_reason,
    }
    submit_body.update({key: value for key, value in optional_fields.items() if value is not None})
    return submit_body


def build_tool_call(name: str = "lookup_weather", call_id: str = "call_one") -> dict[str, Any]:
    """Return one function tool call as a reply's ``tool_calls`` holds it.

    Args:
        name: The function the call invokes.
        call_id: The call's id.

    Returns:
        The tool call.
    """
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": '{"city":"Bergen"}'}}


def build_faulted_chat_submit_body(*, generation_id: str, model_id: str, fault_reason: str | None = None) -> dict[str, Any]:
    """Return the submit body of a chat job that failed, with neither ``generation`` nor ``message``.

    Args:
        generation_id: The popped generation id.
        model_id: The model the job was assigned.
        fault_reason: The ``fault_reason`` to send; left out of the body when None.

    Returns:
        The JSON body for ``POST /v2/generate/text/submit``.
    """
    submit_body: dict[str, Any] = {"id": generation_id, "state": "faulted", "model_id": model_id}
    if fault_reason is not None:
        submit_body["fault_reason"] = fault_reason
    return submit_body


@dataclass
class FakeChatWorker:
    """Represents one worker driving the pop and submit routes through a Flask test client."""

    client: Any
    """The Flask test client."""

    headers: dict[str, str]
    """The worker owner's request headers, with ``apikey`` and ``Client-Agent``."""

    name: str
    """The worker name."""

    models: list[str]
    """The models the worker serves."""

    pop_options: dict[str, Any] = field(default_factory=dict)
    """Further keyword arguments for [`build_pop_body`][tests.integration._fake_chat_worker.build_pop_body]."""

    def pop(self, *, chat_jobs: bool | None = True, **pop_overrides: Any) -> dict[str, Any]:
        """Pop once and return the response body, asserting the pop succeeded.

        Args:
            chat_jobs: Whether to opt into chat jobs; None leaves the key out.
            **pop_overrides: Overrides for the pop body builder.

        Returns:
            The pop response.
        """
        response = self.pop_response(chat_jobs=chat_jobs, **pop_overrides)
        assert response.status_code == 200, response.get_data(as_text=True)
        pop_result: dict[str, Any] = response.get_json()
        return pop_result

    def pop_response(self, *, chat_jobs: bool | None = True, **pop_overrides: Any) -> TestResponse:
        """Pop once and return the raw response.

        Args:
            chat_jobs: Whether to opt into chat jobs; None leaves the key out.
            **pop_overrides: Overrides for the pop body builder.

        Returns:
            The Flask test response.
        """
        pop_body = build_pop_body(
            name=self.name,
            models=self.models,
            bridge_agent=self.headers["Client-Agent"],
            chat_jobs=chat_jobs,
            **{**self.pop_options, **pop_overrides},
        )
        response: TestResponse = self.client.post("/api/v2/generate/text/pop", json=pop_body, headers=self.headers)
        return response

    def submit(self, submit_body: dict[str, Any]) -> TestResponse:
        """Submit a body and return the raw response.

        Args:
            submit_body: The submit body.

        Returns:
            The Flask test response.
        """
        response: TestResponse = self.client.post("/api/v2/generate/text/submit", json=submit_body, headers=self.headers)
        return response


def submit_chat_request(client: Any, request_headers: dict[str, str], **overrides: Any) -> str:
    """Queue a chat request on the native route and return its id, asserting it was accepted.

    Args:
        client: The Flask test client.
        request_headers: The requester's headers.
        **overrides: Request body keys that replace or extend the default body.

    Returns:
        The request id.
    """
    chat_body: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": CHAT_MESSAGES,
        "temperature": 0.5,
        "stop": "\nuser:",
    }
    chat_body.update(overrides)
    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    request_id: str = response.get_json()["id"]
    return request_id


def cancel_request(client: Any, request_headers: dict[str, str], route_family: str, request_id: str) -> None:
    """Cancel a request through its status route, asserting the cancel succeeded.

    Args:
        client: The Flask test client.
        request_headers: The requester's headers.
        route_family: ``chat`` or ``text``, the route family the request was queued on.
        request_id: The request id.
    """
    response = client.delete(f"/api/v2/generate/{route_family}/status/{request_id}", headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)


def chat_status(client: Any, request_headers: dict[str, str], request_id: str) -> dict[str, Any]:
    """Return a chat request's status, asserting the route responded.

    Args:
        client: The Flask test client.
        request_headers: The requester's headers.
        request_id: The request id.

    Returns:
        The status body.
    """
    response = client.get(f"/api/v2/generate/chat/status/{request_id}", headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    status: dict[str, Any] = response.get_json()
    return status


def take_worker_offline(app: Any, worker_name: str) -> None:
    """Move a worker's last check-in an hour back, so it stops counting as online.

    A worker left online makes later tests' requests for its models possible, which tests of an unserved model rely
    on not being.

    Args:
        app: The Flask app.
        worker_name: The worker's name.
    """
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = db.session.query(TextWorker).filter_by(name=worker_name).one()
        worker.last_check_in = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()


ANONYMOUS_HEADERS = {"apikey": "0000000000", "Client-Agent": "aihorde_ci_client:1:test"}
"""The anonymous key, which does not hold kudos, so it is refused any request above the free output allowance."""

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
"""A history whose estimate is far above any context served by the fake worker, while its latest turn is short."""

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

TOOLS_FEATURES = ["tools", "json_object", "tools_with_response_format"]
"""The features declared by a worker to serve ``TOOLS_REQUEST_EXTRAS``."""

VERIFIED_FEATURES = ["tools", "json_object", "json_schema", "tools_with_response_format"]
"""The features declared by a worker to pop every request the reply verification tests make."""

VISION_FEATURES = ["seed", "presence_penalty", "vision"]
"""A worker declaration with the vision feature."""

OFFLOAD_THRESHOLD_BYTES = 256
"""A threshold exceeded by a request with one small image and not reached by a short text-only request."""

MESSAGES_URL_TRANSPORTS = ["url", "messages_url"]
"""A worker declaration that takes offloaded messages, and per-image URLs for a request sent inline."""

PROGRESS_ROUTE = "/api/v2/generate/text/progress"


def serve_reply(
    client,
    request_headers: dict[str, str],
    chat_worker: FakeChatWorker,
    request_overrides: dict[str, Any],
    *,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> tuple[str, Any]:
    """Queue a chat request, pop it with every feature and guarantee declared, and submit one reply to it."""
    request_id = submit_chat_request(client, request_headers, **request_overrides)
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


def png_data_url(size: tuple[int, int] = (64, 48), colour: tuple[int, int, int] = (200, 30, 30)) -> str:
    image_buffer = BytesIO()
    Image.new("RGB", size, colour).save(image_buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(image_buffer.getvalue()).decode('ascii')}"


def image_body(*image_urls: str, **overrides: Any) -> dict[str, Any]:
    chat_body: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "Describe images briefly."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in these?"},
                    *({"type": "image_url", "image_url": {"url": image_url}} for image_url in image_urls),
                ],
            },
        ],
        "max_tokens": 64,
    }
    chat_body.update(overrides)
    return chat_body


def submit_image_chat_request(client, headers: dict[str, str], chat_body: dict[str, Any]) -> str:
    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    return response.get_json()["id"]


def post_progress(
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
