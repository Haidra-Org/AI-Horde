# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check that a backend's token counter matches its generation, before the minimal chat worker relies on it.

Usage: check_token_counter.py --token-counter COUNTER --backend-url URL --model MODEL --context-size TOKENS

For each test conversation the script prints the counter's prompt count beside the ``usage.prompt_tokens`` of a
one-token generation, then sends one prompt that alone exceeds the context and reports whether the backend refused
it or trimmed it. It exits non-zero when the counter undercounts any conversation or the backend trims.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Final

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import minimal_chat_worker as worker  # noqa: E402 - the worker sits beside this script and is not a package.

DESCRIPTION: Final = "Check that a backend's token counter matches its generation."
"""The summary ``--help`` prints."""

LOOKUP_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "lookup_weather",
        "description": "Look up the weather.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
"""A tool definition, so the check covers how the counter renders tools."""

CONVERSATIONS: Final[dict[str, dict[str, Any]]] = {
    "one turn": {"messages": [{"role": "user", "content": "Name one colour."}]},
    "multi-turn with system": {
        "messages": [
            {"role": "system", "content": "You are terse."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello."},
            {"role": "user", "content": "Name one colour."},
        ],
    },
    "non-ASCII": {"messages": [{"role": "user", "content": "Nenne eine Farbe: grün, blå, 赤, 🌈?"}]},
    "tools": {"messages": [{"role": "user", "content": "Weather in Oslo?"}], "tools": [LOOKUP_TOOL]},
}
"""The conversations counted both ways. A counter has to reach the generation count for each."""


def main() -> int:
    """Compare the counter with generation and probe the overflow behaviour.

    Returns:
        The exit status: 1 when the counter undercounts a conversation or the backend trims, otherwise 0.

    Raises:
        requests.RequestException: The backend did not respond to a generation or a count.
    """
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    exact_counters = [counter for counter in worker.TokenCounter if counter is not worker.TokenCounter.BYTES]
    parser.add_argument("--token-counter", type=worker.TokenCounter, choices=exact_counters, required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context-size", type=int, required=True, help="the backend's context for one conversation")
    args = parser.parse_args()
    backend = worker.BackendConfig(
        model=args.model,
        backend_url=args.backend_url.rstrip("/"),
        token_counter=args.token_counter,
        template_overhead=None,
    )
    session = requests.Session()
    failed = False
    for name, conversation in CONVERSATIONS.items():
        body = {"model": args.model, **conversation, "max_tokens": 1, "temperature": 0}
        deadline = time.monotonic() + worker.JOB_DEADLINE_SECONDS
        generated = session.post(f"{backend.backend_url}/v1/chat/completions", json=body, timeout=worker.JOB_DEADLINE_SECONDS)
        generated.raise_for_status()
        reference = (generated.json().get("usage") or {}).get("prompt_tokens")
        counted = worker.count_prompt_tokens(session, backend=backend, body=body, deadline=deadline)
        reaches_generation = reference is not None and counted >= reference
        failed |= not reaches_generation
        verdict = "no usage reported" if reference is None else ("ok" if reaches_generation else "UNDERCOUNTS")
        print(f"{name:24} counter {counted:6}  generation {reference!s:6}  {verdict}")
    # A prompt whose words each cost at least one token runs past the context on its own.
    oversized = {"model": args.model, "messages": [{"role": "user", "content": "colour " * (args.context_size + 100)}], "max_tokens": 16}
    response = session.post(f"{backend.backend_url}/v1/chat/completions", json=oversized, timeout=worker.JOB_DEADLINE_SECONDS)
    trimmed = response.ok
    failed |= trimmed
    print(f"{'oversized prompt':24} status {response.status_code}  {'TRIMMED' if trimmed else 'refused'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
