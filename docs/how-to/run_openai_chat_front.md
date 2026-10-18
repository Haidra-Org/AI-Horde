---
title: "Run the OpenAI-compatible chat front"
summary: "Configure and start the horde.oai process that serves /v1/chat/completions and /v1/models in front of a horde, then check it with a real request."
topics: [operations, requests]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Run the OpenAI-compatible chat front

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [operations](../topics.md#operations), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

Preconditions: a running horde that serves the `/v2/generate/chat` routes, reachable over HTTP from where the front
runs; the Redis server that horde's cache uses; at least one text worker that opted into chat jobs, or the requests
will wait; and the project's dependencies installed (`uv sync`).

End state: a `python -m horde.oai` process answering `GET /healthz`, `GET /v1/models` and
`POST /v1/chat/completions`, subscribed to the horde's chat completion signals.

The front is a separate ASGI process. It does not hold a database connection or import anything from the Flask
application: it submits each chat request to the horde's native `/v2/generate/chat/async`, waits for the horde's
Redis signal on `horde:chat:<request id>` (polling the status route as a fallback), and answers in the OpenAI shape.
A client's `Authorization: Bearer <key>` becomes the horde `apikey`; a request without one uses the anonymous key.
The `Client-Agent` the front sends is `<application>:<version>:<user agent> via horde-oai-front`, so horde statistics
attribute traffic to the client application and still tell it from that application's direct native calls: the
application is the client's `X-Title`, else `X-OpenRouter-Title`, else the host of `HTTP-Referer`, else the host of
`Referer`, with version `0`; without any of them it is `horde-oai-front` with the horde version. The user agent is the
client's `User-Agent`, or `unknown`. Control characters and colons are removed from each part and the whole value is at
most 256 characters; the user agent is truncated first.

## 1. Configure the front

Every setting is an environment variable, read at start through `pydantic-settings`. A `.env` file in the working
directory is read too. A variable that is set but invalid stops the start with a validation error that identifies it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_HORDE_URLS` | `http://127.0.0.1:7001/api` | Comma-separated horde API roots, used round-robin. A root that refuses the connection is skipped for that call. |
| `HORDE_OAI_LISTEN` | `0.0.0.0` | Address to bind. |
| `HORDE_OAI_PORT` | `7002` | Port to bind. |
| `HORDE_OAI_REDIS_URL` | unset | Redis URL of the horde's primary Redis, such as `redis://redis:6379/7`. Pub/sub ignores the logical database, so only the host matters. Unset, every request waits on the fallback poll alone. |
| `HORDE_OAI_REDIS_LIVENESS_SECONDS` | `30` | How long the signal subscription may receive nothing before the front sends a PING, and how long the PONG may take. A missing PONG sets `signal_subscribed: false` on `/healthz` and the front subscribes again, so a connection a network fault black-holes does not pass for an idle one. The front also turns on TCP keepalive for this connection. |
| `HORDE_OAI_PROXY_PASSKEY` | unset | The front operator's service account proxy passkey. When set, the front sends it as `Proxy-Authorization` with the client's address as `Proxied-For`, so IP timeouts and suspicion apply to the client and not to the front. |
| `HORDE_OAI_MAX_INFLIGHT` | `100` | Open requests per process before 503 `FrontAtCapacity`. |
| `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE` | `5` | Open requests per client address per process before 503 `FrontAtCapacity`; checked before the global cap, and before any of the body is read. Users behind one egress address share it. |
| `HORDE_OAI_MAX_BODY_BYTES` | `37748736` | Largest request body, counted while it is received and before JSON is parsed; larger is 413. The default is the native route's 36 MiB, room for five 5 MiB images as data URLs plus the text limit. |
| `HORDE_OAI_MAX_MESSAGE_COUNT` | `1024` | Most messages per request; more is 400. |
| `HORDE_OAI_MAX_TEXT_BYTES` | `1048576` | Most UTF-8 bytes of message text per request; more is 400. |
| `HORDE_OAI_BODY_READ_TIMEOUT_SECONDS` | `30` | Time allowed to receive the body; slower is 408 `RequestBodyTimeout`. |
| `HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS` | `300` | Total budget of a non-streamed request, from before the submit. |
| `HORDE_OAI_STREAM_TIMEOUT_SECONDS` | `600` | Total budget of a streamed request, from before the submit. |
| `HORDE_OAI_FINAL_STATUS_TIMEOUT_SECONDS` | `2` | Allowance for the one status read made at the deadline. |
| `HORDE_OAI_CANCEL_TIMEOUT_SECONDS` | `10` | Budget of one cancel. |
| `HORDE_OAI_SHUTDOWN_DRAIN_SECONDS` | `30` | How long uvicorn waits for open requests on a clean stop before cancelling their handlers. |
| `HORDE_OAI_SHUTDOWN_CANCEL_SECONDS` | `15` | Process-wide bound for cancelling every request still open at stop. |
| `HORDE_OAI_HEARTBEAT_SECONDS` | `5` | Interval of the keepalive comment on a streamed response. |
| `HORDE_OAI_FALLBACK_POLL_SECONDS` | `15` | Base status poll interval when no signal arrives; it stretches to half the horde's `wait_time`, at most 60 seconds, and is shortened at random by up to a fifth. |
| `HORDE_OAI_SIGNAL_RECHECK_SECONDS` | `0.5` | Delay of the one extra status read after a terminal signal whose outcome the first read did not show. |
| `HORDE_OAI_FAIL_FAST_GRACE_SECONDS` | `20` | How long the horde may report no eligible worker, with nothing processing, before the request is cancelled and answered 503. |
| `HORDE_OAI_SUBMIT_RECOVERY_ATTEMPTS` | `2` | How many times a submit whose answer was lost is sent again with the same operation key, within the request's budget. `0` answers a lost submit with 502 at once. See [Lost submits](#lost-submits). |
| `HORDE_OAI_NATIVE_MAX_CONNECTIONS` | `200` | Simultaneous submits, status reads and model lists to the horde, and the size of their pool. Cancels use a separate pool of 16. |
| `HORDE_OAI_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Addresses uvicorn trusts for `X-Forwarded-For`. Set it to the listener's addresses: the resulting client address is the per-source admission key and the `Proxied-For` value. |

The proxy passkey belongs to a service account: `PUT /v2/users/<user id>` with `{"generate_proxy_passkey": true}`,
sent with that account's API key, returns it. Without it, the horde sees every request as coming from the front's
address, and one abusive client can put that address in timeout for all of them.

Verification: `uv run python -c "from horde.oai.settings import load_settings; print(load_settings())"` prints the
settings as the front will use them, without the Redis URL or the passkey, or fails with the variable it could not
validate.

Reversal: unset the variables.

## 2. Start the front

From the repository root:

```bash
uv run python -m horde.oai
```

With docker compose, the `aihorde_oai` service runs the same command from the horde image and publishes port 7002:

```bash
docker compose up -d aihorde_oai
```

Verification: the log shows `Subscribed to chat signals on horde:chat:*`, and

```bash
curl -s http://127.0.0.1:7002/healthz
```

returns `{"live": true, "ready": true, "signal_subscribed": true, "inflight": 0}`. `ready: false` means the
process is at `HORDE_OAI_MAX_INFLIGHT` or stopping. `signal_subscribed: false` means Redis is unset or unreachable;
requests still complete, but only at the fallback poll interval.

Reversal: stop the process, or `docker compose stop aihorde_oai`. On a clean stop the front waits up to
`HORDE_OAI_SHUTDOWN_DRAIN_SECONDS` for open requests, then cancels every request still open on the horde within
`HORDE_OAI_SHUTDOWN_CANCEL_SECONDS` before closing its Redis subscription. A request whose earlier cancel failed or
timed out, even one already answered, gets its cancel sent once more; a cancel still in progress is not sent twice.
At most 10,000 answered requests are held for this; past that the oldest is dropped with a warning.
After a crash, open requests are dropped; their horde jobs run to completion, are charged and are never collected.

## 3. Check the model list

```bash
curl -s http://127.0.0.1:7002/v1/models | jq '.data[].id'
```

Verification: the names match the horde's `GET /v2/status/models?type=text&chat=true`, which lists only models that
an online chat worker serves. An empty list means no chat worker is online. `GET /v1/models/<model name>` returns one
entry; the name may contain slashes. The list is cached for five seconds per process.

Each entry is built from the model's `chat` object in that list:

| Field | Value |
| --- | --- |
| `id`, `object`, `owned_by` | The model name, `model`, `aihorde`. |
| `created` | The horde's `chat.created`, or `0` when it records none. |
| `context_length`, `top_provider.context_length` | `chat.max_context_length`, the largest an online chat worker declares. |
| `top_provider.max_completion_tokens` | `chat.max_length`, the largest an online chat worker declares. |
| `architecture` | Text in and out; `input_modalities` adds `image`, and `modality` becomes `text+image->text`, when a worker declares the `vision` feature. |
| `supported_parameters` | `max_tokens`, `temperature`, `top_p` and `stop`, then `seed`, `frequency_penalty` and `presence_penalty` for each a worker declares as a feature; `tools`, `tool_choice` and `parallel_tool_calls` with the `tools` feature; `response_format` with `json_object` or `json_schema`. |
| `x_horde.workers` | `chat.workers`, the online chat workers serving the model. |
| `x_horde.queued` | The model's queue, which chat requests share with text requests; absent when the horde reports none. |

The limits and parameters are availability hints: a request can still lack an eligible worker a moment later.

## 4. Send a chat request

```bash
curl -s http://127.0.0.1:7002/v1/chat/completions \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-name from step 3>",
    "messages": [{"role": "user", "content": "Say hello."}],
    "max_tokens": 64
  }' | jq .
```

Verification: a `chat.completion` body whose `id` is `chatcmpl-` plus the horde request id and whose `model` is the
model that generated the reply. `x_horde` carries `request_id`, `worker_id`, `worker_name`, `kudos` (what the request
cost), `charge_basis` (the horde's name for the count the kudos were computed on, passed through unchanged),
`estimated_usage` and, when the horde supplies them, `queue_wait_seconds` and `context_adjustments`.
`estimated_usage: true` means the worker did not report token counts and `usage` is an estimate at three characters
per token. `x_horde.notice` appears when the worker returned no visible text; a sampling setting or the model choice
is the likely cause. Each choice carries the worker's `native_finish_reason` beside `finish_reason`.
The front's log carries one line per request:
`Chat request <id> finished: streamed=False submitted_at=... woke_at=... woke_by=signal waited=...s`. `woke_by=poll`
on most requests means signals are not arriving.

Add `"stream": true` (and optionally `"stream_options": {"include_usage": true}`) to receive server-sent events
instead: `: horde queue_position=7 wait_time=42` comments while the request waits, then a role chunk, one content
chunk with the whole text, a finish chunk carrying `x_horde`, the usage chunk with `choices: []` when asked for, and
`data: [DONE]`. The horde generates the whole text before it reports it, so the content arrives in one chunk. The
response carries `Cache-Control: no-cache`, `X-Accel-Buffering: no` and `x-should-retry: false`.

The same request works from an OpenAI SDK by pointing its base URL at `http://<front-host>:7002/v1` and using the horde
API key as the SDK's API key.

Reversal: none needed; a request the client abandons is cancelled on the horde.

## 5. Check the front against the OpenAI SDK

The conformance module drives the pinned official `openai` Python SDK against the real front and the real native app,
each served on a loopback port, with a fake chat worker answering every job, so it does not need an inference
backend. It runs
under the integration test stack, which provides Postgres, the Redis the front subscribes to and the object store
chat images are kept in:

```bash
AI_HORDE_REQUIRE_TEST_DEPS=1 bash tests/run_with_test_stack.sh --   .venv/Scripts/python.exe -m pytest tests/integration/test_chat_front_sdk.py -p no:cacheprovider
```

On Linux or macOS the interpreter is `.venv/bin/python`.

The runner works from Git Bash as from Linux or macOS. When the Garage bootstrap fails it exits with an error and
does not run the command, so a test run does not silently provision separate databases in place of the stack.

Verification: every test passes: the model list includes the worker's chat model and leaves out a model no online
worker serves; a completion and a streamed completion
return the worker's reply, `finish_reason` `stop` and the worker's token counts; `logit_bias` is a `BadRequestError`
whose `param` is `logit_bias`; a model without an online worker is a 503 `no_eligible_worker` the SDK does not
retry; and an image sent as a data URL reaches the worker, which declares `vision` and the `url` image transport, as
a signed `http(s)` URL.

Reversal: the run creates and drops its database schema; the test stack stays up for later runs unless
`run_with_test_stack.sh` is given `--down`.

## Request keys the front accepts

The core OpenAI keys are forwarded: `model`, `messages`, `stream`, `stream_options`, `max_tokens`,
`max_completion_tokens`, `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed` and `n`.
`max_tokens` and `max_completion_tokens` must agree when both are sent and lie between 16 and 4096; with neither, the
horde generates up to 512 tokens. `n` must be 1. `stop` is a string or up to four strings. `stream_options` needs
`"stream": true`. Message roles are `system`, `developer`, `user`, `assistant` and `tool`; `developer` is stored as
`system`, which loses its precedence over `system`; an `assistant` message may carry `tool_calls` and a `tool` message
answers one by `tool_call_id` (see the [chat front reference](../reference/chat_front.md)). Content is a string or an
array of `text` parts; a `user` message may also carry `image_url` parts (see [Image inputs](#image-inputs)).

Stock clients send some keys at their defaults; these are accepted and dropped: empty or null `tools`, null or
`"none"` `tool_choice`, `response_format: {"type": "text"}`, `logprobs: false`, `top_logprobs: 0`,
`parallel_tool_calls` without tools, `store: false` and `service_tier` `"auto"` or `"default"`. `user` and `metadata`
are accepted and unused. Any other key or value, audio and file content parts and image parts outside a `user`
message among them, is 400 `UnsupportedChatParameter` with `param` holding its path; a refused content part is
reported as `messages[1].content[0].type=input_audio`, the form the native route uses.

The `horde` object takes `only` or `ignore` (worker ids, not both), `trusted_workers`, `validated_backends`,
`slow_workers`, `max_context_length` and `context_fit` (`grow` or `reject`). `dry_run` and `operation_key` are refused.

## Image inputs

A `user` message may carry `{"type": "image_url", "image_url": {"url": ..., "detail": ...}}` parts, with `url` a
`data:image/...;base64,` URL or an `http(s)` URL and `detail` optionally `auto`, `low` or `high`. The front forwards
them unchanged. It refuses before the submit a request with more than five image parts (400
`TooManyExtraSourceImages`) or a data URL that decodes to more than 5 MiB (400 `SourceImageSizeExceeded`, with
`param` the path of the part's `url`); data URLs do not count toward `HORDE_OAI_MAX_TEXT_BYTES`. The horde fetches,
checks and stores each image, charges the extra source image tax per image, refuses more than one image from an
untrusted user (400 `MoreThanMinExtraSourceImage`), and sends the image only to a worker that declares the `vision`
feature and an image transport. Its refusals pass through with the horde's `rc` as `code`, such as
`SourceImageUrlRefused` for a URL at a private address or `SourceImageResolutionExceeded`, and 503
`ChatImageStorageUnavailable` when the image store refuses the image. A model whose list entry shows `image` in
`architecture.input_modalities` has such a worker online.

## Errors the front returns

Every error is an OpenAI envelope, `{"error": {"message", "type", "param", "code", "metadata"}}`, with `param` and
`code` written as null when they do not apply. `metadata` holds what is known: `request_id`, the horde's `rc`,
`retry_after`, and `submit_outcome: "unknown"` with the request's `operation_key` when a submit was lost and recovery
did not settle it. A horde error keeps the horde's status, and its
`rc` becomes `code`; a horde 429 keeps `Retry-After` and the `X-RateLimit-*` headers, and a horde 503 without
`Retry-After` (such as `ChatAdmissionDisabled`) gets `Retry-After: 30`. A status read that ends the wait with a
horde error other than 404, 429 or a 5xx, such as 401 or 403, cancels the request before the error is returned; a 404
means the horde no longer holds it.

Once the horde holds a request, or may hold one, every error carries `x-should-retry: false`, so OpenAI SDKs do not
retry into a second charged job. Errors before the submit do not carry the header and are safe to retry.

| Status | `code` | `x-should-retry` | Cause |
| --- | --- | --- | --- |
| 400 | `UnsupportedChatParameter` | | A key, role, content part, value or size the service does not serve. `param` holds its path. |
| 400 | `ChatRequestConflict` | | `max_tokens` and `max_completion_tokens` differ, or `only` and `ignore` are both set. |
| 400 | `BadRequest` | | The body is not a JSON object or a value has the wrong type. |
| 400 | `TooManyExtraSourceImages` | | More than five image parts. |
| 400 | `SourceImageSizeExceeded` | | An image data URL decodes to more than 5 MiB. `param` is the path of the part's `url`. |
| 401 | `InvalidAuthorizationHeader` | | `Authorization` is present but not `Bearer <key>`. Omit it to use the anonymous key. |
| 404 | `model_not_found` | | The model id contains `:`; no model variant is served. A horde 404 keeps the horde's `rc` as `code`. |
| 408 | `ChatRequestTimeout` | `false` | The request was still waiting after `HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS` and one final status read; it is cancelled. An outcome reached by the deadline is returned instead. |
| 408 | `RequestBodyTimeout` | | The body took longer than `HORDE_OAI_BODY_READ_TIMEOUT_SECONDS` to arrive. Nothing was submitted. |
| 413 | `RequestTooLarge` | | The body exceeds `HORDE_OAI_MAX_BODY_BYTES`. |
| 502 | `ChatRequestFaulted` | `false` | The horde reports the request as faulted, or it expired on the horde. |
| 502 | `HordeUnavailable` | | No horde root in `HORDE_OAI_HORDE_URLS` accepted the connection. |
| 502 | `HordeUnavailable` | `false` | The submit was sent, no answer came, and no recovery attempt was answered; the request may exist. `metadata` holds `submit_outcome: "unknown"` and `operation_key`. |
| 502 | `OperationKeyConflict` | `false` | A recovery attempt was refused because the operation key holds a different request. The front re-sends the identical body, so this is a front defect; `metadata` holds the `rc` and `operation_key`. |
| 503 | `FrontAtCapacity` | | The process or the client's address holds its maximum of open requests. `Retry-After: 5`. |
| 503 | `no_eligible_worker` | `false` | No online worker could serve the request for `HORDE_OAI_FAIL_FAST_GRACE_SECONDS`; it is cancelled. `Retry-After: 30`. |

In a stream, an error after the headers arrives as one `data:` event with the same envelope and no choice, then
`data: [DONE]`. A streamed request has its own budget, `HORDE_OAI_STREAM_TIMEOUT_SECONDS`.

A client retry after a lost response is a new request and can create a second charged job; the front does not
deduplicate requests.

## Lost submits

The front sends each client request with a fresh UUID as `horde.operation_key`. The horde claims the key for the
submitting user when it activates the request, and answers a repeated key with the same body by returning the first
request's `{id, kudos}` with status 200, without queuing or charging anything.

When a submit was sent and no answer came (a read timeout, a reset or a dropped connection), the front sends the
identical body with the same key again to the next root, up to `HORDE_OAI_SUBMIT_RECOVERY_ATTEMPTS` times and only
while the request's budget lasts. When every root is out of the rotation after the failure, the attempt goes to them
anyway. Then:

- An answer with an id, 200 for an existing claim or 202 when the lost submit never reached the horde, continues the
  request as any submit does. The log shows `Recovered the lost submit of operation <key> on attempt <n>`.
- A connection failure, a 429 or a 5xx moves on to the next attempt.
- `OperationKeyConflict` is answered 502, as in the table above.
- Any other horde refusal is passed through with `x-should-retry: false`, `submit_outcome: "unknown"` and the key,
  since the lost submit may still have queued the request.
- With the attempts or the budget spent, the answer is 502 `HordeUnavailable` with `submit_outcome: "unknown"` and the
  key. The key finds the request, if one exists, in `waiting_prompts.operation_key`.

A refused connection on the first submit is not a lost submit: the request did not reach a horde, and the 502 does
not carry an `x-should-retry` header.

Two metrics count these cases (see [Chat front reference](../reference/chat_front.md#metrics)):

| Metric | Counts |
| --- | --- |
| `horde.oai.submit.recoveries` | Recovered submits; `horde.existing_claim` is true when the horde answered with an existing claim (200) and false when it answered 202. First-try submits are not counted. |
| `horde.oai.client_retry_hints` | Chat requests arriving with `x-stainless-retry-count` above zero, the header OpenAI SDKs send on their own retries. |

Neither measures duplicate client work prevented. A claim match shows a lost answer whose request the horde had
queued; a retry hint shows that a client SDK retried, for whatever cause.

## Export telemetry

The front exports OpenTelemetry metrics, and its Loguru log records as OpenTelemetry logs, through the same
environment variables as the horde's Flask application. With none of them set it exports nothing.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP endpoint for every signal; setting it, or one of the per-signal endpoints below, turns export on. |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | unset | Per-signal OTLP endpoints. |
| `AI_HORDE_TELEMETRY_ENABLED` | unset | `true`, `1` or `yes` turns export on without an endpoint variable. |
| `OTEL_SDK_DISABLED` | unset | `true` keeps export off whatever else is set. |
| `OTEL_SERVICE_NAME` | `horde-oai` | Service name of the front's telemetry. Leave it unset where the horde's environment sets it, so the front is not reported as `ai-horde`. |
| `DEPLOYMENT_ENVIRONMENT` | `development` | The `deployment.environment` resource attribute. |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Head sampling ratio, as for the horde. |

The front does not start Pyroscope, and `PYROSCOPE_ENABLED` alone does not turn its export on.

Verification: with an endpoint set, a collector receives `horde.oai.*` metrics within the SDK's export interval
after the first request, and one `Chat request <id> finished: ...` log record per finished request. The metric and
log field lists are in the [Chat front reference](../reference/chat_front.md).

Reversal: unset the variables and restart the front.
