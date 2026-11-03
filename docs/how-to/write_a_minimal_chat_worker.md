---
title: "Write a minimal chat worker"
summary: "Run the example Python chat worker with an OpenAI-compatible backend, verify the token counter and check the worker declaration."
topics: [chat, workers]
order: 61
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Write a minimal chat worker

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [chat](../topics.md#chat), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

`tools/chat_worker_example/minimal_chat_worker.py` is a chat worker in one file. It pops chat jobs from a horde, sends
each to a backend's `POST /v1/chat/completions`, and submits the reply or a fault. Any server with that route fits,
such as llama.cpp's `llama-server`. It uses `requests` and the SDK's
`horde_sdk.worker.chat_declaration`, and `tests/integration/test_minimal_chat_worker_example.py` runs it against the
in-process Horde. For the full protocol, see [Serve chat jobs from a text worker](serve_chat_jobs.md).

Before starting, have:

- Python 3.12 or later with `requests` and `horde_sdk` 0.33.3 or later installed.
- A registered API key for the Horde at `<HORDE_URL>`.
- A backend serving a model with a chat template.
- `curl` and `jq`.

End state: the worker's details show `accepts_chat_jobs: true`, `accepts_prompt_jobs: false` and its declaration, so
the horde sends it chat requests for `<model-name>`.

## 1. Start an OpenAI-compatible backend

Any server with `POST /v1/chat/completions` that reports `usage` works. With a reasoning model, the server has to
return the reasoning in `message.reasoning_content` and not inline in `content`. The worker submits the message as
the server returns it, so inline reasoning reaches the requester as reply text. One choice is llama.cpp's server:

```bash
llama-server --model <model.gguf> --ctx-size 4096 --parallel 1 --jinja --port 8080
```

`--jinja` renders each conversation with the model's chat template, and `--parallel 1` gives one conversation the
whole context. With vLLM, start it with `--served-model-name <model-name>`, since vLLM accepts only requests for a
model served by it and the worker sends `<model-name>` as `model`.

Verification: the probe sent by the worker at startup returns a prompt count:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "<model-name>", "messages": [{"role": "user", "content": ""}], "max_tokens": 1}' | jq .usage
```

```json
{"completion_tokens": 1, "prompt_tokens": 12, "total_tokens": 13}
```

`prompt_tokens` differs by model and template. A response without `usage` means the worker serves only `best_effort`
requests. Then run `check_token_counter.py` as shown under
[Fit check and the overflow guarantee](#fit-check-and-the-overflow-guarantee).

Reversal: stop the server.

## 2. Run the worker

```bash
export HORDE_URL=<HORDE_URL>
export HORDE_WORKER_API_KEY=<api-key>
python tools/chat_worker_example/minimal_chat_worker.py \
  --worker-name <worker-name> --model <model-name> \
  --backend-url http://127.0.0.1:8080 --token-counter llamacpp \
  --max-context-length 4096 --max-length 512
```

Every option falls back to an environment variable, listed by `--help`: `HORDE_URL`, `HORDE_WORKER_API_KEY`,
`HORDE_WORKER_NAME`, `HORDE_MODEL_NAME`, `BACKEND_URL`, `CHAT_TOKEN_COUNTER`, `CHAT_MAX_CONTEXT_LENGTH`,
`CHAT_MAX_LENGTH`, `CHAT_TEMPLATE_OVERHEAD` and `CHAT_FEATURES` (comma-separated). Set `--max-context-length` to the backend's
context size for one conversation and `--max-length` to the backend's maximum token count for one reply. Choose a `<model-name>`
without a colon: the OpenAI-compatible front does not route to a model whose name contains one.

Verification: the log prints the declaration without a warning about `best_effort`:

```text
<time> Declaring {'chat_jobs': True, 'prompt_jobs': False, 'model_id': '<model-name>', 'chat_features': ['prompt_overflow_refused'], 'chat_limits': {'max_context_length': 4096, 'max_length': 512}, 'image_transports': []}
```

A pop or submit that fails is logged as `A pop or submit failed:` with the HTTP status, and the worker tries again 2
seconds later. A `400` on pop is a declaration refused by the horde. The worker logs the response body as
`The horde refused the pop:` with the `ChatDeclarationInvalid` message before the failure.

Reversal: stop the worker with Ctrl+C. A worker counts as online for 300 seconds after its last pop.

## 3. Check the declaration

```bash
curl -s "$HORDE_URL/api/v2/workers/name/<worker-name>" | jq '{accepts_chat_jobs, accepts_prompt_jobs, chat}'
```

Verification: the details show the declaration of the last pop
([Worker details](../reference/chat_requests.md#worker-details)):

```json
{
  "accepts_chat_jobs": true,
  "accepts_prompt_jobs": false,
  "chat": {
    "model_id": "<model-name>",
    "features": ["prompt_overflow_refused"],
    "limits": {"max_context_length": 4096, "max_length": 512, "tools_max_temperature": null},
    "image_transports": []
  }
}
```

Worker details are cached for 30 seconds, so a changed declaration can take that long to show.

To send the worker a chat request and read the reply, follow steps 3 and 4 of
[Serve your first chat job with koboldcpp](../tutorial/first_chat_worker.md#3-send-a-chat-request) with
`HORDE_API_KEY=$HORDE_WORKER_API_KEY` and `MODEL_NAME=<model-name>`, without the `koboldcpp/` prefix. The worker logs
`Job <job-id>: stop` for a completed job. A faulted job shows in the status as `restarted` and goes to the next
eligible worker. The worker logs it with `prompt_overflow`, `generation_error` or `reply_refused` in place of the
finish reason.

## The worker

What it does on each pass of `serve_one_pop`:

- Pops `POST /api/v2/generate/text/pop` with `chat_jobs: true` and `prompt_jobs: false`, so it receives chat jobs only
  and does not need a text path. An empty pop returns `False`, and `main` waits 2 seconds before the next pop.
- Counts the prompt (see [Fit check and the overflow guarantee](#fit-check-and-the-overflow-guarantee)) and faults
  the job with `prompt_overflow` when the count plus `max_length` exceeds the job's `max_context_length`.
- Sends the job to the backend without streaming, with the sampling values as the horde sent them. It does not post
  progress.
- Submits the reply with the backend's finish reason mapped onto `stop`, `length`, `content_filter` or `tool_calls`,
  and the token counts the backend reported. A backend failure faults the job with `generation_error`. A `400` on the
  reply's submit with `ChatSubmitInvalid`, `ChatSubmitNonconformant` or `ChatModelMismatch` faults it with
  `reply_refused`. Any other refused submit, such as `AbortedGen` for a job past its 150-second lease, is logged and
  not faulted.

It does not declare an optional feature unless `--features` lists one. `--features` accepts `seed`, `presence_penalty`,
`frequency_penalty`, `tools`, `json_object`, `json_schema` and `reasoning_effort`. List only what the backend honours,
since the horde does not test a declaration.

### Fit check and the overflow guarantee

Every `strict` request needs the guarantee `prompt_overflow_refused`, and a request is strict unless the client sends
`horde.conformance: best_effort` ([step 1 of Serve chat jobs](serve_chat_jobs.md#1-declare-chat-on-pop)). The worker declares it only
when its fit check can back it. A job fits when the prompt's token count plus `max_length` is at most the job's
`max_context_length`. `--token-counter` sets how the prompt is counted:

| `--token-counter` | Backend routes per job                                    | Count                                          |
| ----------------- | --------------------------------------------------------- | ---------------------------------------------- |
| `llamacpp`        | `POST /apply-template`, then `POST /tokenize`             | Exact, tools included                          |
| `koboldcpp`       | `POST /api/extra/tokencount`                              | Exact for the messages, plus the tools' JSON bytes |
| `vllm`            | `POST /v1/chat/completions/render`                        | Exact, tools included                          |
| `bytes` (default) | none                                                      | An upper bound from the UTF-8 length of the JSON |

koboldcpp's count runs its chat transform, which generates once to choose a tool when tools are present and no Jinja
template is in use, so the worker leaves tools out of that count and adds their bytes instead.

At startup the worker sends the backend one empty user message with `max_tokens: 1` and reads `usage.prompt_tokens`,
the count used by generation:

- With an exact counter, a count below `usage.prompt_tokens` for the same body is logged as a mismatch, and the
  worker does not declare the guarantee. A counter route that does not respond stops the worker.
- With `bytes`, `usage.prompt_tokens` is added to the bound as the template's fixed part.

The byte bound counts far more tokens than generation uses, so a worker on it faults long conversations with
`prompt_overflow`. Use an exact counter on a backend that has one.
[Prompt fit on the worker](../explanation/chat_requests.md#prompt-fit-on-the-worker) explains why each counter backs
the guarantee and what the byte bound costs.

The worker does not declare the guarantee, and serves only `best_effort` requests, in these cases:

- The backend does not report `usage`.
- The counter undercounts.
- The byte bound runs with `tools` in `--features` and without `--template-overhead`. The probe does not include
  tools, so it does not measure a tool preamble.

The startup log explains why the guarantee is withheld before warning about `best_effort`.

`check_token_counter.py` beside the worker runs the same comparison on four conversations (one turn, multi-turn with
a system message, non-ASCII text, tools) and sends one prompt that alone exceeds the context, to show whether the
backend refuses it or trims it:

```bash
python tools/chat_worker_example/check_token_counter.py --token-counter llamacpp \
  --backend-url http://127.0.0.1:8080 --model <model-name> --context-size 4096
```

```text
one turn                 counter     39  generation 39      ok
multi-turn with system   counter     56  generation 56      ok
non-ASCII                counter     53  generation 53      ok
tools                    counter    175  generation 175     ok
oversized prompt         status 400  refused
```

The counts differ by model and chat template. The script exits non-zero when a count falls short or the backend
trims.

### Worker source

```python
--8<-- "tools/chat_worker_example/minimal_chat_worker.py"
```

## Extensions

Each extension adds to `serve_one_pop` or the declaration:

- Tools: add `tools` to `--features`, with an exact counter or with `--template-overhead` for the byte bound. The
  reply's `tool_calls` already pass through.
  See [step 4 of Serve chat jobs](serve_chat_jobs.md#4-serve-tools-and-structured-output).
- Structured output: add `json_object` or `json_schema` to `--features` when the backend constrains the reply. See
  [step 4 of Serve chat jobs](serve_chat_jobs.md#4-serve-tools-and-structured-output).
- Images: declare `vision` with an image transport and fetch or pass on the image parts. See
  [step 5 of Serve chat jobs](serve_chat_jobs.md#5-serve-images).
- Streaming progress: stream from the backend and post progress for a job with `stream_progress: true`. See
  [Streaming progress](serve_chat_jobs.md#streaming-progress).
- Text jobs: pop with `prompt_jobs: true` and serve jobs without `chat` through the backend's completion route. See
  [step 8 of Serve chat jobs](serve_chat_jobs.md#8-keep-serving-text-jobs).

## Related

- [Serve chat jobs from a text worker](serve_chat_jobs.md): every declaration field, fault reason and submit rule.
- [Serve your first chat job with koboldcpp](../tutorial/first_chat_worker.md): koboldcpp's embedded worker, which does
  not need a separate worker process.
- [Chat requests reference](../reference/chat_requests.md): every pop, payload and submit field.
