# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fake chat worker thread, reply picker and constants the front conformance modules share.

[`FixedReplyWorker`][tests.integration._front_stack.FixedReplyWorker] pops chat jobs on a thread and submits the reply
[`build_reply_message`][tests.integration._front_stack.build_reply_message] picks for each. The constants are the
worker's declarations and replies, the front's timing settings and the requests the SDK and wire-example modules send.
The fixtures that start the Horde, the worker and the front are in ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from tests.integration._fake_chat_worker import CHAT_MODEL, DEFAULT_CHAT_FEATURES, FakeChatWorker, build_chat_submit_body

if TYPE_CHECKING:
    from flask import Flask
    from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

LOOPBACK_HOST: str = "127.0.0.1"
"""The address both servers bind; an ephemeral port on loopback keeps them unreachable from elsewhere."""

WORKER_NAME: str = "CICD SDK Conformance Scribe"
"""The fake worker's name."""

FIXED_REPLY: str = "Blue."
"""The reply the fake worker submits for every chat job."""

WORKER_PROMPT_TOKENS: int = 21
"""The prompt token count the fake worker reports."""

WORKER_COMPLETION_TOKENS: int = 3
"""The completion token count the fake worker reports."""

FRONT_CHAT_MESSAGES: list[ChatCompletionMessageParam] = [
    {"role": "system", "content": "You are a terse assistant."},
    {"role": "user", "content": "Name one colour."},
]
"""The messages of every completion request."""

WORKER_CHAT_FEATURES: list[str] = [
    *DEFAULT_CHAT_FEATURES,
    "vision",
    "tools",
    "json_schema",
]
"""The chat features the fake worker declares: koboldcpp's, plus ``vision``, ``tools`` and ``json_schema`` so image,
tool and structured output requests are eligible for it."""

WEATHER_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Read the current weather of a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
"""The tool the tool rounds offer."""

WEATHER_CALL_ID: str = "call_weather_1"
"""The id the fake worker gives the tool call it returns."""

WEATHER_ARGUMENTS: str = '{"city":"Paris"}'
"""The arguments of the tool call the fake worker returns."""

TOOL_ANSWER_REPLY: str = "It is 18C and clear in Paris."
"""The reply the fake worker submits for a job whose last message is a tool result."""

TOOL_MESSAGES: list[ChatCompletionMessageParam] = [
    {"role": "user", "content": "What is the weather in Paris?"},
]
"""The opening messages of the tool rounds."""

STRUCTURED_REPLY: dict[str, str] = {"colour": "blue"}
"""The JSON document the fake worker submits for a structured output job."""

WORKER_IMAGE_TRANSPORTS: list[str] = ["url"]
"""The image transports the fake worker declares; only ``url``, so an image reaches it as a signed URL."""

CHAT_SIGNAL_REDIS_DB: int = 7
"""The Redis database the Horde publishes chat signals on, as ``horde.redis_ctrl.chat_signal_db``."""

POP_INTERVAL_SECONDS: float = 0.2
"""How long the fake worker waits after a pop that found no job."""

SERVER_START_TIMEOUT_SECONDS: float = 20.0
"""How long a fixture waits for a server or the worker's first pop before failing."""

SERVER_STOP_TIMEOUT_SECONDS: float = 30.0
"""How long teardown waits for a server thread to finish."""

FAIL_FAST_GRACE_SECONDS: float = 1.0
"""The front's no-eligible-worker grace, short so the fail-fast case finishes quickly."""

FALLBACK_POLL_SECONDS: float = 30.0
"""The front's fallback poll interval, longer than a served completion may take, so no answer waits on a fallback poll.

The fail-fast read is scheduled from the grace, not the poll interval, so the long interval does not slow that case.
"""

REQUEST_BUDGET_SECONDS: float = 60.0
"""The front's request budgets; well above what a served request takes, so a stall fails rather than hangs."""

SDK_TIMEOUT_SECONDS: float = 90.0
"""The SDK's request timeout, above the front's budget so the front's own answer arrives first."""


def build_reply_message(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return the assistant message and finish reason the fake worker submits for a popped chat payload.

    A job offering tools whose last message is not a tool result gets one call of
    [`WEATHER_TOOL`][tests.integration._front_stack.WEATHER_TOOL] with null content; a tool result gets
    [`TOOL_ANSWER_REPLY`][tests.integration._front_stack.TOOL_ANSWER_REPLY]; a ``json_schema`` job gets
    [`STRUCTURED_REPLY`][tests.integration._front_stack.STRUCTURED_REPLY]; anything else gets
    [`FIXED_REPLY`][tests.integration._front_stack.FIXED_REPLY].
    """
    last_role = payload["messages"][-1]["role"]
    if payload.get("tools") and last_role != "tool":
        tool_call = {"id": WEATHER_CALL_ID, "type": "function", "function": {"name": "get_weather", "arguments": WEATHER_ARGUMENTS}}
        return {"role": "assistant", "content": None, "tool_calls": [tool_call]}, "tool_calls"
    if last_role == "tool":
        return {"role": "assistant", "content": TOOL_ANSWER_REPLY}, "stop"
    response_format = payload.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        return {"role": "assistant", "content": json.dumps(STRUCTURED_REPLY)}, "stop"
    return {"role": "assistant", "content": FIXED_REPLY}, "stop"


class FixedReplyWorker:
    """Pop chat jobs on a thread and submit the reply
    [`build_reply_message`][tests.integration._front_stack.build_reply_message] picks for each.

    Thread Safety:
        The thread builds its own Flask test client; each of its requests pushes and pops its own app context, so it
        shares no ORM session with the test thread.
    """

    def __init__(self, *, app: Flask, headers: dict[str, str]) -> None:
        """Create the worker without starting it.

        Args:
            app: The native Flask app.
            headers: The owner's ``apikey`` and ``Client-Agent`` headers.
        """
        self._app = app
        self._headers = headers
        self._stop_event = threading.Event()
        self._first_pop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sdk-conformance-worker", daemon=True)
        self.failures: list[str] = []
        """Each unexpected pop or submit answer, or an exception that ended the thread."""
        self.served_payloads: list[dict[str, Any]] = []
        """The ``payload`` of every chat job the worker popped, in pop order."""

    def start(self) -> None:
        """Start popping and wait until the first pop registered the worker with the Horde."""
        self._thread.start()
        assert self._first_pop_event.wait(SERVER_START_TIMEOUT_SECONDS), self.failures

    def stop(self) -> None:
        """Stop popping and wait for the thread to finish."""
        self._stop_event.set()
        self._thread.join(timeout=SERVER_STOP_TIMEOUT_SECONDS)

    def _run(self) -> None:
        worker = FakeChatWorker(
            client=self._app.test_client(),
            headers=self._headers,
            name=WORKER_NAME,
            models=[CHAT_MODEL],
            pop_options={"chat_features": WORKER_CHAT_FEATURES, "image_transports": WORKER_IMAGE_TRANSPORTS},
        )
        try:
            while not self._stop_event.is_set():
                if not self._serve_one(worker):
                    self._stop_event.wait(POP_INTERVAL_SECONDS)
        except Exception as error:
            self.failures.append(f"The worker thread stopped: {error!r}")
            raise

    def _serve_one(self, worker: FakeChatWorker) -> bool:
        """Pop once and submit the fixed reply for a job; return whether a job was served."""
        pop_response = worker.pop_response()
        self._first_pop_event.set()
        if pop_response.status_code != 200:
            self.failures.append(f"pop {pop_response.status_code}: {pop_response.get_data(as_text=True)}")
            return False
        pop_result = pop_response.get_json()
        generation_id = pop_result["id"]
        if generation_id is None:
            return False
        payload = pop_result["payload"]
        self.served_payloads.append(payload)
        reply_message, finish_reason = build_reply_message(payload)
        submit_body = build_chat_submit_body(
            generation_id=generation_id,
            content=reply_message["content"] or "",
            model_id=CHAT_MODEL,
            finish_reason=finish_reason,
            prompt_tokens=WORKER_PROMPT_TOKENS,
            completion_tokens=WORKER_COMPLETION_TOKENS,
        )
        submit_body["message"] = reply_message
        submit_response = worker.submit(submit_body)
        if submit_response.status_code != 200:
            self.failures.append(f"submit {submit_response.status_code}: {submit_response.get_data(as_text=True)}")
        return True
