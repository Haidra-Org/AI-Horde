# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The startup fit check of the minimal chat worker example under ``tools/chat_worker_example``, against a stub backend.

The loader, the stub backend session and the helpers here are shared with
``tests/integration/test_minimal_chat_worker_example.py``, which serves the worker against the in-process horde.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from json import dumps, loads
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import requests

from tests.integration._fake_chat_worker import CHAT_MODEL

pytestmark = pytest.mark.unit

EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "tools" / "chat_worker_example" / "minimal_chat_worker.py"
"""The example script, which is a standalone file and not an importable package."""

HORDE_URL = "http://horde.invalid"
"""The horde root the worker is configured with."""

BACKEND_URL = "http://backend.invalid"
"""The backend root the worker is configured with. The session routes it to the stub backend."""

PROBE_PROMPT_TOKENS = 12
"""The prompt count the stub backend reports, which becomes the measured template overhead."""

COMPLETIONS_PATH = "/v1/chat/completions"
"""The backend route that generates."""

BackendStub = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]
"""Returns a status code and a JSON body for one backend path and JSON body."""


def _load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("minimal_chat_worker", EXAMPLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules, so the module is registered before it runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker_example = _load_example()
"""The loaded example module."""

COUNTER_RESPONSES: dict[Any, dict[str, dict[str, Any]]] = {
    worker_example.TokenCounter.LLAMACPP: {
        "/apply-template": {"prompt": "<rendered>"},
        "/tokenize": {"tokens": list(range(PROBE_PROMPT_TOKENS))},
    },
    worker_example.TokenCounter.KOBOLDCPP: {
        "/api/extra/tokencount": {"value": PROBE_PROMPT_TOKENS, "ids": [], "prompt": "<rendered>"},
    },
    worker_example.TokenCounter.VLLM: {
        "/v1/chat/completions/render": {"prompt_token_ids": list(range(PROBE_PROMPT_TOKENS))},
    },
}
"""The response bodies of each exact counter's routes for a prompt of ``PROBE_PROMPT_TOKENS`` tokens."""


def build_completion(content: str | None, *, usage: bool = True, finish_reason: str = "stop", **message_extras: Any) -> dict[str, Any]:
    """Return a ``/v1/chat/completions`` response body with one choice.

    Args:
        content: The message content.
        usage: Whether the body includes ``usage`` with ``PROBE_PROMPT_TOKENS`` prompt tokens.
        finish_reason: The choice's finish reason.
        **message_extras: Further message fields, such as ``tool_calls``.

    Returns:
        The response body.
    """
    message = {"role": "assistant", "content": content, **message_extras}
    completion: dict[str, Any] = {"choices": [{"message": message, "finish_reason": finish_reason}]}
    if usage:
        completion["usage"] = {"prompt_tokens": PROBE_PROMPT_TOKENS, "completion_tokens": 3}
    return completion


def is_probe(body: dict[str, Any]) -> bool:
    """Return whether a generation body is the startup probe, the only body with ``max_tokens`` 1.

    Args:
        body: The ``/v1/chat/completions`` body.

    Returns:
        Whether the body is the probe.
    """
    probe: bool = body["max_tokens"] == 1
    return probe


def build_config(**overrides: str) -> Any:
    """Return the worker's settings parsed from flags, under a fresh worker name.

    Args:
        **overrides: Flags and values that replace or extend the defaults, such as ``{"--features": "tools"}``.

    Returns:
        The parsed ``WorkerConfig``.
    """
    settings = {
        "--horde-url": HORDE_URL,
        "--api-key": "unused",
        "--worker-name": f"CICD Minimal Chat Worker {uuid.uuid4().hex[:8]}",
        "--model": CHAT_MODEL,
        "--backend-url": BACKEND_URL,
        "--max-context-length": "4096",
        "--max-length": "512",
        **overrides,
    }
    # The flag=value form keeps a value that starts with a dash, as an API key can, from reading as a flag.
    return worker_example.parse_config([f"{flag}={value}" for flag, value in settings.items()])


@dataclass(frozen=True)
class Exchange:
    """Represents one post the worker made and the response it got."""

    url: str
    """The URL posted to."""

    body: Any
    """The JSON body posted, as it went over the wire."""

    status: int
    """The response's HTTP status."""

    response_body: Any
    """The response's JSON body."""


class BackendSession(requests.Session):
    """Sends backend URLs to a stub and other URLs to ``post_elsewhere``, recording every exchange."""

    def __init__(self, backend_factory: Callable[[BackendSession], BackendStub]) -> None:
        """Build the session and its stub backend.

        Args:
            backend_factory: Returns the stub backend for the session, so a stub can read the session's exchanges.
        """
        super().__init__()
        self.exchanges: list[Exchange] = []
        self.backend = backend_factory(self)

    def post(self, url: Any, data: Any = None, json: Any = None, **kwargs: Any) -> requests.Response:
        """Return the stub's or ``post_elsewhere``'s response to a JSON post and record the exchange."""
        body = loads(dumps(json))
        if url.startswith(BACKEND_URL):
            status, response_body = self.backend(url.removeprefix(BACKEND_URL), body)
        else:
            status, response_body = self.post_elsewhere(url, body, kwargs.get("headers"))
        response = requests.Response()
        response.url = url
        response.reason = ""
        response.status_code = status
        response._content = dumps(response_body).encode()
        self.exchanges.append(Exchange(url=url, body=body, status=status, response_body=response_body))
        return response

    def post_elsewhere(self, url: str, body: Any, headers: dict[str, str] | None) -> tuple[int, Any]:
        """Return the status and JSON body for a post to a URL outside the backend.

        Raises:
            AssertionError: Always, since a fit check does not reach the horde.
        """
        raise AssertionError(f"The worker posted to {url} outside the backend.")

    def generation_bodies(self) -> list[dict[str, Any]]:
        """Return the bodies of the backend generations other than the startup probe."""
        return [exchange.body for exchange in self.exchanges if exchange.url.endswith(COMPLETIONS_PATH) and not is_probe(exchange.body)]


def fixed_backend(stub: BackendStub) -> Callable[[BackendSession], BackendStub]:
    """Return a backend factory that ignores the session and returns ``stub``.

    Args:
        stub: The stub backend.

    Returns:
        The factory for ``BackendSession``.
    """
    return lambda session: stub


def _counter_backend(counter: Any, *, counted: int) -> BackendStub:
    """Return a backend whose generation reports ``PROBE_PROMPT_TOKENS`` and whose counter reports ``counted``."""

    def backend(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path == COMPLETIONS_PATH:
            return 200, build_completion("Blue.")
        response_body = dict(COUNTER_RESPONSES[counter][path])
        for key in ("tokens", "prompt_token_ids"):
            if key in response_body:
                response_body[key] = list(range(counted))
        if "value" in response_body:
            response_body["value"] = counted
        return 200, response_body

    return backend


@pytest.mark.parametrize(
    ("reports_usage", "features"),
    [
        pytest.param(False, "", id="probe-reports-no-usage"),
        pytest.param(True, "tools", id="tools-without-operator-overhead"),
    ],
)
def test_the_overflow_guarantee_is_withheld_without_a_known_template_overhead(reports_usage: bool, features: str) -> None:
    """Without a measured or configured template overhead the fit check is unsound, so the guarantee is withheld."""
    config = build_config(**({"--features": features} if features else {}))
    session = BackendSession(fixed_backend(lambda path, body: (200, build_completion("", usage=reports_usage))))

    declaration = worker_example.build_declaration(config, worker_example.check_fit_counting(session, config))

    assert "prompt_overflow_refused" not in declaration.to_pop_fields()["chat_features"]


@pytest.mark.parametrize(
    ("counter", "counted", "is_declared"),
    [
        *(pytest.param(counter, PROBE_PROMPT_TOKENS, True, id=f"{counter}-matches-generation") for counter in COUNTER_RESPONSES),
        pytest.param(worker_example.TokenCounter.LLAMACPP, PROBE_PROMPT_TOKENS - 1, False, id="undercounts"),
    ],
)
def test_an_exact_counter_backs_the_overflow_guarantee_only_when_it_reaches_the_generation_count(
    counter: Any,
    counted: int,
    is_declared: bool,
) -> None:
    """A counter that counts fewer prompt tokens than generation does would let an overflowing prompt through."""
    config = build_config(**{"--token-counter": counter, "--features": "tools"})
    session = BackendSession(fixed_backend(_counter_backend(counter, counted=counted)))

    declaration = worker_example.build_declaration(config, worker_example.check_fit_counting(session, config))

    assert ("prompt_overflow_refused" in declaration.to_pop_fields()["chat_features"]) is is_declared


@pytest.mark.parametrize(
    ("counter", "flags"),
    [
        pytest.param(worker_example.TokenCounter.BYTES, {}, id="bytes"),
        pytest.param(worker_example.TokenCounter.LLAMACPP, {"--token-counter": "llamacpp"}, id="llamacpp"),
    ],
)
def test_the_operator_template_overhead_replaces_the_measured_one_for_every_counter(counter: Any, flags: dict[str, str]) -> None:
    """``--template-overhead`` is the overhead the fit check adds, with the byte bound and with an exact counter."""
    config = build_config(**flags, **{"--template-overhead": "40"})
    session = BackendSession(fixed_backend(_counter_backend(worker_example.TokenCounter.LLAMACPP, counted=PROBE_PROMPT_TOKENS)))

    assert config.backend.token_counter is counter
    assert worker_example.check_fit_counting(session, config) == 40
