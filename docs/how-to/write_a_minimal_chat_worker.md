---
title: "Write a minimal chat worker"
summary: "Run a tested one-file Python worker that serves chat jobs from any OpenAI-compatible backend, check its declaration, send it a chat request and read the reply."
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
each to a backend's `POST /v1/chat/completions`, and submits the reply or a fault. Any server with that route fits:
llama.cpp's `llama-server`, vLLM and koboldcpp among them. It uses `requests` and the SDK's
`horde_sdk.worker.chat_declaration`, and `tests/integration/test_minimal_chat_worker_example.py` runs it against the
in-process horde. [Serve chat jobs from a text worker](serve_chat_jobs.md) is the full procedure with every option;
this page is the shortest path to a working worker.

Preconditions: Python 3.12 or later with `requests` and `horde_sdk` installed; a registered API key for the horde at
`<HORDE_URL>`; a model that the backend serves with a chat template; and `curl` and `jq`.

End state: the worker's details show `accepts_chat_jobs: true`, `accepts_prompt_jobs: false` and its declaration, and
a chat request for `<model-name>` on the native route completes with the backend's reply.

## The worker

```python
--8<-- "tools/chat_worker_example/minimal_chat_worker.py"
```

What it does on each pass of `serve_one_pop`:

- Pops `POST /api/v2/generate/text/pop` with `chat_jobs: true` and `prompt_jobs: false`, so it receives chat jobs only
  and needs no text path. An empty pop returns `False`, and `main` sleeps 2 seconds before the next one.
- Counts the prompt (see [Fit check and the overflow guarantee](#fit-check-and-the-overflow-guarantee)) and faults
  the job with `prompt_overflow` when the count plus `max_length` exceeds the job's `max_context_length`.
- Sends the job to the backend without streaming, with the sampling values as the horde sent them. It does not post
  progress.
- Submits the reply with the backend's finish reason mapped onto `stop`, `length`, `content_filter` or `tool_calls`,
  and the token counts the backend reported. A backend failure faults the job with `generation_error`. A `400` on the
  reply's submit with `ChatSubmitInvalid`, `ChatSubmitNonconformant` or `ChatModelMismatch` faults it with
  `reply_refused`. Any other refused submit, such as `AbortedGen` for a job past its 150-second lease, is logged and
  not faulted.

It declares no optional feature unless `--features` lists one. `--features` accepts `seed`, `presence_penalty`,
`frequency_penalty`, `tools`, `json_object`, `json_schema` and `reasoning_effort`; list only what the backend honours,
since the horde does not test a declaration.

### Fit check and the overflow guarantee

Every `strict` request needs the guarantee `prompt_overflow_refused`, and a request is strict unless the client asks
for `best_effort` ([step 1 of Serve chat jobs](serve_chat_jobs.md#1-declare-chat-on-pop)). The worker declares it only
when its fit check can back it. A job fits when the prompt's token count plus `max_length` is at most the job's
`max_context_length`; `--token-counter` sets how the prompt is counted:

| `--token-counter` | Backend routes per job                                    | Count                                          |
| ----------------- | --------------------------------------------------------- | ---------------------------------------------- |
| `llamacpp`        | `POST /apply-template`, then `POST /tokenize`             | Exact, tools included                          |
| `koboldcpp`       | `POST /api/extra/tokencount`                              | Exact for the messages, plus the tools' JSON bytes |
| `vllm`            | `POST /v1/chat/completions/render`                        | Exact, tools included                          |
| `bytes` (default) | none                                                      | An upper bound from the UTF-8 length of the JSON |

koboldcpp's count runs its chat transform, which generates once to choose a tool when tools are present, so the
worker leaves tools out of that count and adds their bytes instead.

At startup the worker sends the backend one empty user message with `max_tokens: 1` and reads `usage.prompt_tokens`,
the count generation uses:

- With an exact counter, the counter has to reach that count for the same body, or the worker logs the mismatch and
  does not declare the guarantee. This catches a counter that renders with another template or without the BOS token.
  A counter route that does not answer stops the worker.
- With `bytes`, the count is the template's fixed part (a BOS token, a system prompt the template injects, the
  generation prompt), added to the bound. A byte-level BPE tokenizer emits at least one UTF-8 byte per token, so the
  JSON's length bounds the message text, and the JSON keys cover each message's role markup. For English the bound
  is about four times the real count. The horde sizes context at about 3 characters per token, so a worker on the
  byte bound faults long conversations the horde sends it with `prompt_overflow`, and more than 20 faults in an hour
  put a worker into maintenance. Use an exact counter on a backend that has one.

The worker does not declare the guarantee, and serves only `best_effort` requests, when the backend reports no
`usage`, when the counter undercounts, or when the byte bound runs with `tools` in `--features` and without
`--template-overhead`: the probe carries no tools, so it does not measure a tool preamble. The log states which of
these applies before the warning about `best_effort`.

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

That output is from llama.cpp build b12020 serving Llama 3.2 3B Instruct; the counts differ by model. It exits
non-zero when a count falls short or the backend trims.

## 1. Start an OpenAI-compatible backend

Any server with `POST /v1/chat/completions` that reports `usage` works. With a reasoning model, the server has to
return the reasoning in `message.reasoning_content` and not inline in `content`. The worker submits the message as
the server returns it, so inline reasoning reaches the requester as reply text. One choice is llama.cpp's server:

```bash
llama-server --model <model.gguf> --ctx-size 4096 --parallel 1 --jinja --port 8080
```

`--jinja` renders each conversation with the model's chat template, and `--parallel 1` gives one conversation the
whole context. With vLLM, start it with `--served-model-name <model-name>`, since vLLM answers only requests for a
model it serves and the worker sends `<model-name>` as `model`.

Verification: the probe the worker sends at startup returns a prompt count:

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
`CHAT_MAX_LENGTH`, `CHAT_TEMPLATE_OVERHEAD` and `CHAT_FEATURES` (comma-separated). Set `--max-context-length` to the context the backend
has for one conversation and `--max-length` to the most tokens it generates for one reply. Choose a `<model-name>`
without a colon: the OpenAI-compatible front does not route to a model whose name contains one.

Verification: the log prints the declaration and no warning about `best_effort`:

```text
<time> Declaring {'chat_jobs': True, 'prompt_jobs': False, 'model_id': '<model-name>', 'chat_features': ['prompt_overflow_refused'], 'chat_limits': {'max_context_length': 4096, 'max_length': 512}, 'image_transports': []}
```

A pop or submit that fails is logged as `A pop or submit failed:` with the HTTP status, and the worker tries again 2
seconds later. A `400` on pop is a declaration the horde does not accept. The worker logs the response body as
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

## 4. Send a chat request

```bash
REQUEST_ID=$(curl -s "$HORDE_URL/api/v2/generate/chat/async" \
  -H "apikey: $HORDE_WORKER_API_KEY" \
  -H "Client-Agent: minimal-chat-worker-howto:1:unknown" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-name>",
    "messages": [
      {"role": "system", "content": "Answer in one short sentence."},
      {"role": "user", "content": "What colour is a clear daytime sky?"}
    ],
    "max_tokens": 64
  }' | tee /dev/stderr | jq -r .id)
```

Verification: the horde answers `202` with `{"id": "<request-id>", "kudos": <kudos>}`, and within a few seconds the
worker logs `Job <job-id>: stop`.

## 5. Read the reply

```bash
curl -s "$HORDE_URL/api/v2/generate/chat/status/$REQUEST_ID" -H "apikey: $HORDE_WORKER_API_KEY" \
  | jq '{done, generations: [.generations[] | {worker_name, message, finish_reason, prompt_tokens, completion_tokens}]}'
```

Verification: once `done` is true, the generation carries the backend's reply:

```json
{
  "done": true,
  "generations": [
    {
      "worker_name": "<worker-name>",
      "message": {"role": "assistant", "content": "A clear daytime sky is blue."},
      "finish_reason": "stop",
      "prompt_tokens": 38,
      "completion_tokens": 9
    }
  ]
}
```

A faulted job shows in the status as `restarted` and goes to the next eligible worker. The worker logs the job with
`prompt_overflow`, `generation_error` or `reply_refused` in place of the finish reason.

## Extensions

Each extension adds to `serve_one_pop` or the declaration:

- Tools: add `tools` to `--features`, with an exact counter or with `--template-overhead` for the byte bound; the
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
- [Serve your first chat job with koboldcpp](../tutorial/first_chat_worker.md): koboldcpp's embedded worker, which needs
  no separate worker process.
- [Chat requests reference](../reference/chat_requests.md): every pop, payload and submit field.
