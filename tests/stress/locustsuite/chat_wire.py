# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Routes, timeouts and the bridge agent every chat population of the suite shares: the scale demo in
``users/chat.py`` and the chat contract mix."""

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "CONNECT_TIMEOUT_SECONDS",
    "STREAM_READ_TIMEOUT_SECONDS",
    "TEXT_POP_PATH",
    "TEXT_PROGRESS_PATH",
    "TEXT_SUBMIT_PATH",
    "WORKER_BRIDGE_AGENT",
]

CHAT_COMPLETIONS_PATH: str = "/v1/chat/completions"
"""The front's chat completions route."""

TEXT_POP_PATH: str = "/api/v2/generate/text/pop"
"""The Horde route a text worker pops jobs from; chat jobs come through it when the pop opts in."""

TEXT_SUBMIT_PATH: str = "/api/v2/generate/text/submit"
"""The Horde route a text worker submits generations to."""

TEXT_PROGRESS_PATH: str = "/api/v2/generate/text/progress"
"""The Horde route a chat worker posts content deltas to while it generates a job whose client streams."""

CONNECT_TIMEOUT_SECONDS: float = 10.0
"""How long a client waits to connect; thousands of clients connecting at once can fill the listen backlog."""

STREAM_READ_TIMEOUT_SECONDS: float = 60.0
"""How long a streamed client waits between two lines; the front sends a keepalive every few seconds, so a minute
of silence is a stalled stream."""

WORKER_BRIDGE_AGENT: str = "KoboldAI Client:1.19.2-stress-chat:https://github.com/koboldai/koboldai-client"
"""The bridge agent simulated chat workers report."""
