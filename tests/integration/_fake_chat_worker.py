# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A fake text worker that speaks the chat pop and submit fields.

The body builders are plain functions so a suite that mocks the native API can reuse them; the
[`FakeChatWorker`][tests.integration._fake_chat_worker.FakeChatWorker] drives them through a Flask test client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_CHAT_FEATURES",
    "FakeChatWorker",
    "build_chat_submit_body",
    "build_faulted_chat_submit_body",
    "build_pop_body",
]

DEFAULT_CHAT_FEATURES = ("seed", "presence_penalty")
"""The features the fake worker declares unless told otherwise, matching what koboldcpp declares."""


def build_pop_body(
    *,
    name: str,
    models: list[str],
    bridge_agent: str,
    chat_jobs: bool | None,
    model_id: str | None = None,
    chat_features: list[str] | None = None,
    chat_max_context_length: int = 8192,
    chat_max_length: int = 1024,
    max_context_length: int = 4096,
    max_length: int = 512,
) -> dict[str, Any]:
    """Return a text pop body, with the chat declaration when ``chat_jobs`` is true.

    Args:
        name: The worker name.
        models: The models the worker serves.
        bridge_agent: The worker's bridge agent string.
        chat_jobs: Whether to opt into chat jobs; None leaves the key out, as a worker that predates chat does.
        model_id: The chat model; the first of ``models`` when omitted.
        chat_features: The declared chat features; ``DEFAULT_CHAT_FEATURES`` when omitted.
        chat_max_context_length: The chat context limit.
        chat_max_length: The chat output limit.
        max_context_length: The text context limit.
        max_length: The text output limit.

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
    if chat_jobs:
        pop_body["chat_features"] = list(DEFAULT_CHAT_FEATURES) if chat_features is None else chat_features
        pop_body["chat_limits"] = {"max_context_length": chat_max_context_length, "max_length": chat_max_length}
        pop_body["model_id"] = models[0] if model_id is None else model_id
    return pop_body


def build_chat_submit_body(
    *,
    generation_id: str,
    content: str,
    model_id: str,
    finish_reason: str = "stop",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    native_finish_reason: str | None = None,
) -> dict[str, Any]:
    """Return a chat job's submit body, without ``generation``.

    Args:
        generation_id: The popped generation id.
        content: The assistant reply.
        model_id: The model the job ran on.
        finish_reason: Why generation stopped.
        prompt_tokens: The prompt token count; left out when None.
        completion_tokens: The generated token count; left out when None.
        native_finish_reason: The backend's own stop reason; left out when None.

    Returns:
        The JSON body for ``POST /v2/generate/text/submit``.
    """
    submit_body: dict[str, Any] = {
        "id": generation_id,
        "message": {"role": "assistant", "content": content},
        "finish_reason": finish_reason,
        "model_id": model_id,
        "state": "ok",
    }
    optional_fields = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "native_finish_reason": native_finish_reason,
    }
    submit_body.update({key: value for key, value in optional_fields.items() if value is not None})
    return submit_body


def build_faulted_chat_submit_body(*, generation_id: str, model_id: str) -> dict[str, Any]:
    """Return the submit body of a chat job that failed, with neither ``generation`` nor ``message``.

    Args:
        generation_id: The popped generation id.
        model_id: The model the job was assigned.

    Returns:
        The JSON body for ``POST /v2/generate/text/submit``.
    """
    return {"id": generation_id, "state": "faulted", "model_id": model_id}


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

    def pop_response(self, *, chat_jobs: bool | None = True, **pop_overrides: Any) -> Any:
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
        return self.client.post("/api/v2/generate/text/pop", json=pop_body, headers=self.headers)

    def submit(self, submit_body: dict[str, Any]) -> Any:
        """Submit a body and return the raw response.

        Args:
            submit_body: The submit body.

        Returns:
            The Flask test response.
        """
        return self.client.post("/api/v2/generate/text/submit", json=submit_body, headers=self.headers)
