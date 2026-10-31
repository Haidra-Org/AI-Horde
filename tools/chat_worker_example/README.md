<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Minimal chat worker

`minimal_chat_worker.py` serves AI Horde chat jobs from any backend with an OpenAI-compatible
`POST /v1/chat/completions`. `tests/integration/test_minimal_chat_worker_example.py` runs it against the in-process
horde. [Write a minimal chat worker](../../docs/how-to/write_a_minimal_chat_worker.md) shows how to run it.

`--template-overhead` sets the tokens the fit check adds to every prompt count, for every `--token-counter`. It
replaces the startup measurement: the probe's prompt count for `bytes`, and 0 for an exact counter.
