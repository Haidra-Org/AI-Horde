<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Chat stack tools

Scripts for bringing up a local chat stack (one Horde, the OpenAI-compatible front and a worker) and for checking its
behavior with real clients. They are investigation and support tools: each prints what it sees, and the round trips
end with an `OK` line or an assertion error. None of them is part of the test suite.

## Requirements

- A POSIX shell with Docker for the test backends. Git Bash works as Linux, macOS or WSL do: the test-stack bootstrap handles MSYS path conversion itself, and `--python .venv/Scripts/python.exe` selects the Windows interpreter.
- The repository's development environment: `uv sync` installs `openai`, `httpx`, `requests`, Pillow and `pytest`
  into `.venv`. Run the Python scripts with `.venv/bin/python` from any directory; the scripts locate the repository
  from their own path where they need it.
- A chat worker for every tool that sends a chat request: koboldcpp with `--hordechat` (see
  [Serve your first chat job](../../docs/tutorial/first_chat_worker.md) and [Serve chat jobs](../../docs/how-to/serve_chat_jobs.md)),
  or `fake_chat_worker_driver.py` for the tools and structured output paths without a GPU.
- An OpenAI API key for `openai_shape_probe.py` only.

## Common arguments

Every Python script takes its endpoints and key from arguments, with an environment variable as the fallback. No
script reads a key from a file.

| Argument | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--base-url` | `CHAT_FRONT_URL` | `http://127.0.0.1:7002/v1` | The front's OpenAI base URL, as `start_front.sh` serves it. |
| `--horde-url` | `HORDE_URL` | `http://127.0.0.1:7101` | The Horde root, as `start_horde.sh` serves it. |
| `--api-key` | `HORDE_API_KEY` | none (required) | The requester's API key. |
| `--model` | none | per script, below | The model to request, as `/v1/models` lists it. |

`fake_chat_worker_driver.py` reads the worker key from `HORDE_WORKER_API_KEY`, and `openai_shape_probe.py` reads
`OPENAI_API_KEY` and `OPENAI_BASE_URL`. `validate_chat_worker.py` and `coding_demo/agent.py` fall back to the
anonymous key `0000000000`.

## Local stack

Start each long-running script in its own terminal or background shell and wait on its endpoint, since the process
starts long before it serves. `<run-dir>` is any directory for logs and state.

1. `bash tools/chat_stack/start_horde.sh <run-dir>` brings up Postgres, Redis and Garage through
   `tests/run_with_test_stack.sh` as the Compose project `chatlocal` and runs one quorum Horde on port 7101 with
   `HORDE_TEST_APIKEYS=1` and rate limits disabled. Wait on `GET http://127.0.0.1:7101/api/v2/status/heartbeat`.
2. `eval "$(.venv/bin/python tools/chat_stack/native_roundtrip.py --mode bootstrap)"` creates the requester and
   worker users and exports `HORDE_API_KEY` and `HORDE_WORKER_API_KEY`.
3. Start a worker against `http://127.0.0.1:7101` with `HORDE_WORKER_API_KEY`: koboldcpp, or
   `fake_chat_worker_driver.py`.
4. `bash tools/chat_stack/start_front.sh <run-dir>` runs the front on port 7002. Wait on
   `GET http://127.0.0.1:7002/v1/models`.

| `start_horde.sh` option | Environment variable | Default |
| --- | --- | --- |
| `--port` | `CHAT_STACK_HORDE_PORT` | `7101` |
| `--python` | `CHAT_STACK_PYTHON` | `.venv/bin/python` in the repository |
| `--compose-project` | `COMPOSE_PROJECT_NAME` | `chatlocal` |
| none | `AI_HORDE_TEST_POSTGRES_PORT` | `45432` |
| none | `AI_HORDE_TEST_REDIS_PORT` | `46379` |
| none | `AI_HORDE_TEST_GARAGE_S3_PORT` | `43900` |
| none | `AI_HORDE_TEST_GARAGE_RPC_PORT` | `43901` |
| none | `AI_HORDE_TEST_GARAGE_ADMIN_PORT` | `43903` |

The backend ports differ from the default test stack's, so a test run and this stack do not share data. The script
writes the injected backend env to `<run-dir>/stack.env`, and the Horde runs from `<run-dir>/horde-<port>/`, where it
writes its log.

| `start_front.sh` option | Environment variable | Default |
| --- | --- | --- |
| `--port` | `HORDE_OAI_PORT` | `7002` |
| `--listen` | `HORDE_OAI_LISTEN` | `127.0.0.1` |
| `--horde-url` | `HORDE_OAI_HORDE_URLS` | `http://127.0.0.1:7101/api` |
| `--redis-url` | `HORDE_OAI_REDIS_URL` | `redis://127.0.0.1:<AI_HORDE_TEST_REDIS_PORT or 46379>/7` |
| `--python` | `CHAT_STACK_PYTHON` | `.venv/bin/python` in the repository |

Every other `HORDE_OAI_*` variable reaches the front unchanged; [Run the OpenAI-compatible chat
front](../../docs/how-to/run_openai_chat_front.md) lists them. The front runs from `<run-dir>/front/`.

To stop, end the two foreground processes and the worker, then stop the backends:
`COMPOSE_PROJECT_NAME=chatlocal docker compose -f tests/docker-compose.yml down`. A koboldcpp worker pauses itself
after a run of failed pops, so restart it after restarting the Horde. A Compose volume that outlives a branch change
keeps its old schema; apply the branch's `sql_statements/` file to the `chatlocal` Postgres by hand, or remove the
volume with `down -v`.

The whole test suite against an isolated stack needs no script of its own:

```sh
COMPOSE_PROJECT_NAME=chatfull AI_HORDE_TEST_POSTGRES_PORT=55432 AI_HORDE_TEST_REDIS_PORT=56379 \
AI_HORDE_TEST_GARAGE_S3_PORT=53900 AI_HORDE_TEST_GARAGE_RPC_PORT=53901 AI_HORDE_TEST_GARAGE_ADMIN_PORT=53903 \
AI_HORDE_REQUIRE_TEST_DEPS=1 \
bash tests/run_with_test_stack.sh -- .venv/bin/python -m pytest tests -q -rfEs 2>&1 | tee full-suite.log
```

## Native route and workers

### native_roundtrip.py

Creates the test users, or drives one chat request through the native route and polls its status.

| Argument | Default | Meaning |
| --- | --- | --- |
| `--mode` | required | `bootstrap` or `request`. |
| `--horde-url` | `HORDE_URL`, `http://127.0.0.1:7101` | The Horde root. |
| `--api-key` | `HORDE_API_KEY` | The requester key; `request` only. |
| `--model` | none | The model to request; required by `request`. |
| `--max-tokens` | `64` | `max_tokens` of the request. |

`bootstrap` calls `/api/v2/dev/test-user` for `chat_worker_user` and `chat_requester` (100000 kudos, trusted), prints
each user without its key on stderr, and prints the keys as `export HORDE_WORKER_API_KEY=...` and
`export HORDE_API_KEY=...` on stdout. `request` prints the submit response and one status line every 3 seconds,
for up to 300 seconds.

### fake_chat_worker_driver.py

Pops chat jobs as a worker and answers each with a scripted reply, without a model.

```sh
.venv/bin/python tools/chat_stack/fake_chat_worker_driver.py <model> <log.jsonl> [options]
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `model` | required | The model the worker declares. |
| `log` | required | JSON lines file each pop, payload, submit body and submit response is appended to. |
| `--api-key` | `HORDE_WORKER_API_KEY` | The worker key. |
| `--horde-url` | `HORDE_URL`, `http://127.0.0.1:7101` | The Horde root. |
| `--twrf` | off | Also declare `tools_with_response_format`. |
| `--name` | `fake-chat-worker` | Worker name; a suffix is added each time the worker enters maintenance. |
| `--seconds` | `3600` | How long to run. |

The worker declares every chat feature and guarantee and `tools_max_temperature: 1.0`. A `[reply:<tag>]` marker in
the last user message selects a scripted reply (`two_calls`, `undeclared`, `dup_ids`, `prose`, `nonconformant`,
`text_and_call`); otherwise the reply is a well-behaved one for the payload. A refused submit is followed by a faulted
submit, as koboldcpp does. The driver imports `tests/integration/_fake_chat_worker.py` from the repository.

## Front checks

Each script below talks to the front at `--base-url` with `--api-key`, and prints its findings.

| Script | Purpose | Extra arguments | Default `--model` |
| --- | --- | --- | --- |
| `sdk_tools_roundtrip.py` | OpenAI SDK tool calls (non-streamed, streamed, forced), a tool-result follow-up, `json_schema`, `json_object` and a refused `tool_choice` without tools; prints `TOOLS ROUND TRIP OK`. | none | `koboldcpp/Meta-Llama-3.1-8B-Instruct` |
| `sdk_policy_roundtrip.py` | The chat conformance policy cases a to g of [ADR 0036](../../docs/decisions/0036-route-chat-requests-by-declared-guarantees-under-a-conformance-mode.md), one `RESULT` and one `EXPECT` line each, without stopping on a failed expectation. | `--only a,b,...` selects cases; j and k run only when it is given. | `koboldcpp/Meta-Llama-3.1-8B-Instruct` |
| `sdk_image_roundtrip.py` | Waits up to 5 minutes for a vision model in `/v1/models`, then sends a generated PNG as a data URL, non-streamed and streamed; the reply describes a blue rectangle. | none | `koboldcpp/gemma-3-4b-it` |
| `lite_shaped_roundtrip.py` | Requests shaped as KoboldAI Lite's OpenAI mode builds them, after the browser's CORS preflight; prints `LITE-SHAPED ROUND TRIP OK`. | `--origin`, default `https://lite.koboldai.net`. | `koboldcpp/Meta-Llama-3.1-8B-Instruct` |
| `stream_demo.py` | Live delta relay of a streamed request, a non-streamed request, buffering of a streamed tool request, and a request padded past the messages offload threshold, checked on the native status. | `--horde-url` | `koboldcpp/Llama-3.2-3B-Instruct` |
| `sse_probe.py` | Arrival time and shape of every server-sent event of one streamed request, then its request id. | none | `koboldcpp/Llama-3.2-3B-Instruct` |
| `verify_doc_claims.py` | The client documentation's claims: model card limits, `horde.only`, `max_tokens` around the worker's limit and image eligibility. | `--max-length`, default `1024`: the output limit the model's only worker declares. | `koboldcpp/Meta-Llama-3.1-8B-Instruct` |

KoboldAI Lite's default output length is 768 tokens, so `lite_shaped_roundtrip.py` and a real Lite client need a
worker with `--hordegenlen` of at least 768.

### validate_chat_worker.py

Runs one worker's model through every chat path of a front and writes the raw responses: plain, streamed, vision,
each tool mode, a tool replay, both structured output formats, and the expected refusals (tools above the worker's
temperature limit, tools with a response format, an oversized prompt). Use it after starting a new worker or worker
build; point `--base-url` at a deployed front, such as `https://oai-dev.aihorde.net/v1`, to check a worker there.

| Argument | Default | Meaning |
| --- | --- | --- |
| `--model` | required | The worker's model. |
| `--base-url` | `CHAT_FRONT_URL`, `http://127.0.0.1:7002/v1` | The front. |
| `--api-key` | `HORDE_API_KEY`, else `0000000000` | The requester key. |
| `--out` | `validate_chat_worker_results.json` | Raw results file, relative to the current directory. |
| `--cases` | all | Comma-separated case names, such as `plain,tools_auto`. |

### logging_proxy.py

Forwards every request to a front and writes each exchange to a file, for seeing exactly what a client sends.

| Argument | Default | Meaning |
| --- | --- | --- |
| `log_dir` | required | Directory for `<n>-<method>-<path>.json` per exchange. |
| `--listen` | `127.0.0.1` | Address to bind. |
| `--port` | `8790` | Port to bind. |
| `--upstream` | `CHAT_PROXY_UPSTREAM`, `http://127.0.0.1:7002` | Front root every path is forwarded to. |

Point the client at `http://127.0.0.1:8790/v1`. Each record holds the request headers with the key masked, the JSON
body, and the response status, headers and body or raw stream text; stdout gets one summary line per exchange.

## OpenAI reference behavior

### openai_shape_probe.py

Sends small requests to OpenAI's API to record how it treats the request shapes the front mirrors: unknown keys at
each level, tool message fields, tool control and history validation, structured output, sampling bounds and the
streamed tool call shape. Each case uses a nano model and at most 60 output tokens.

| Argument | Default | Meaning |
| --- | --- | --- |
| `groups` | all | Groups to run: `tool_message`, `request`, `tool_control`, `nested_keys`, `strict_tools`. |
| `--case` | none | One case as `group.case`; repeatable. |
| `--list` | off | Print every group and case, then exit. |
| `--api-key` | `OPENAI_API_KEY` | The OpenAI key; never printed. |
| `--base-url` | `OPENAI_BASE_URL`, `https://api.openai.com/v1` | The API base URL. |
| `--model` | `gpt-4.1-nano` | The model every case requests. |
| `--out` | `openai_shape_probe_results.json` | Raw responses file, relative to the current directory. |

## Coding agent demo

`coding_demo/agent.py` is a minimal coding agent on the OpenAI Python SDK. It copies `coding_demo/seed_project/` into
`<out>/workspace/`, then lets the model call `list_files`, `read_file`, `write_file` (test files are read-only) and
`run_tests` (pytest) until it stops calling tools or 14 turns pass. Every turn, tool call, written file and test
output goes to `<out>/transcript.md`.

| Argument | Default | Meaning |
| --- | --- | --- |
| `--model` | `koboldcpp/Qwen3.8-27B` | The model to request. |
| `--base-url` | `CHAT_FRONT_URL`, `http://127.0.0.1:7002/v1` | The front. |
| `--api-key` | `HORDE_API_KEY`, else `0000000000` | The requester key. |
| `--out` | `coding_demo_run` | Output directory; its `workspace/` is deleted and recreated on each run. |

The seed project is a checkout pricing module with two bugs, failing 3 of its 5 tests: `apply_discount` treats a
whole-number percentage as a fraction, and the free-shipping check uses the subtotal before the discount, although
the docstring specifies the discounted subtotal. Requests use `temperature` 0.2 and `max_tokens` 512, so one
`write_file` cannot carry more than about 512 tokens of file. `run_tests` executes model-written code on the machine
that runs the agent, confined only by the workspace path check.
