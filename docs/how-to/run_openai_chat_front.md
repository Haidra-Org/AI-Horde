---
title: "Run the OpenAI-compatible chat front"
summary: "Configure and run horde.oai, verify its model list and completions, and diagnose availability errors and lost submissions."
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

Before starting, have:

- A running Horde with the `/v2/generate/chat` routes reachable over HTTP from the front.
- Access to the Horde's primary Redis server.
- At least one text worker with chat enabled.
- The project's dependencies installed with `uv sync`.

End state: a `python -m horde.oai` process answering `GET /healthz`, `GET /v1/models` and
`POST /v1/chat/completions`, subscribed to the horde's chat completion signals, and serving its API documentation at
`/docs`, `/redoc` and `/openapi.json`.

The front is a separate ASGI process. It does not hold a database connection or import anything from the Flask
application: it submits each chat request to the horde's native `/v2/generate/chat/async`, waits for the horde's
Redis signal on `horde:chat:<request id>` (polling the status route as a fallback), and returns an OpenAI response.

## Client authentication and attribution

A client's `Authorization: Bearer <key>` becomes the horde `apikey`. A request without one uses the anonymous key.
The front's `Client-Agent` header is `<application>:<version>:<user agent> via horde-oai-front`.
Horde statistics use it to attribute traffic to the application and distinguish it from direct native calls.

The application value is selected in this order: `X-Title`, `X-OpenRouter-Title`, the host of `HTTP-Referer`,
then the host of `Referer`. These values use version `0`. Without these headers, the value is
`horde-oai-front` with the Horde version. The user agent is the client's `User-Agent`, or `unknown`.

Control characters and colons are removed from each part. The whole value is at most 256 characters, with the
user agent truncated first.

## 1. Configure the front

Every setting is an environment variable, read at start through `pydantic-settings`. A `.env` file in the working
directory is read too. A variable that is set but invalid stops the start with a validation error that identifies it.

### Connections and listening

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_HORDE_URLS` | `http://127.0.0.1:7001/api` | Comma-separated primary horde API roots, used round-robin. A root that refuses the connection is skipped for that call. |
| `HORDE_OAI_FALLBACK_HORDE_URLS` | empty | Comma-separated horde API roots used round-robin only when every primary root is unusable. A root cannot be in both lists. |
| `HORDE_OAI_DRAIN_AGENT_PORT` | unset | TCP port of the drain agent on each root's host. The front sends the root's port and a newline, and reads `ready` or `drain`, as HAProxy's `agent-check` does. The front does not send new calls to a drained root. Unset, the front does not poll and every root counts as ready. |
| `HORDE_OAI_DRAIN_AGENT_POLL_SECONDS` | `1` | How often every root's agent is polled, concurrently. One query may take half of this. |
| `HORDE_OAI_LISTEN` | `0.0.0.0` | Address to bind. |
| `HORDE_OAI_PORT` | `7002` | Port to bind. |
| `HORDE_OAI_REDIS_URL` | unset | Redis URL of the horde's primary Redis, such as `redis://redis:6379/7`. Pub/sub ignores the logical database, so only the host matters. Unset, every request waits on the fallback poll alone. |
| `HORDE_OAI_STREAM_REDIS_DB` | `7` | The logical database for chat content deltas on the host of `HORDE_OAI_REDIS_URL`. This is the Horde's chat signal database. The front reads it on a separate client, whatever database the URL selects. Unused without `HORDE_OAI_REDIS_URL`. |
| `HORDE_OAI_REDIS_LIVENESS_SECONDS` | `30` | How long the signal subscription may remain idle before the front sends a PING, and how long the PONG may take. A missing PONG sets `signal_subscribed: false` on `/healthz` and the front subscribes again, so an unresponsive connection is detected even when the subscription is idle. The front also turns on TCP keepalive for this connection. |
| `HORDE_OAI_PROXY_PASSKEY` | unset | The front operator's service account proxy passkey. When set, the front sends it as `Proxy-Authorization` with the client's address as `Proxied-For`, so IP timeouts and suspicion apply to the client. |

### Admission and request limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_MAX_INFLIGHT` | `1000` | Open requests per process before 503 `FrontAtCapacity`. A request holds its slot until it ends, including its cancel, so when the horde stalls the cap trips at this value divided by the request budget per second. It detects a stall and does not limit throughput. It also sizes the cancel pool: one cancel slot per 20 open requests, at least 16. |
| `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE` | `5` | Open requests per client address per process before 503 `FrontAtCapacity`. Checked before the global cap, and before any of the body is read. Users behind one egress address share it. |
| `HORDE_OAI_MAX_BODY_BYTES` | `37748736` | Largest request body, counted while it is received and before JSON is parsed. Larger is 413. The default is the native route's 36 MiB, room for five 5 MiB images as data URLs plus the text limit. |
| `HORDE_OAI_MAX_MESSAGE_COUNT` | `1024` | Most messages per request. More is 400. |
| `HORDE_OAI_MAX_TEXT_BYTES` | `1048576` | Most UTF-8 bytes of message text per request. More is 400. |
| `HORDE_OAI_BODY_READ_TIMEOUT_SECONDS` | `30` | Time allowed to receive the body. Slower is 408 `RequestBodyTimeout`. |

### Request budgets and cancellation

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS` | `300` | Total budget of a non-streamed request, from before the submit. |
| `HORDE_OAI_STREAM_TIMEOUT_SECONDS` | `600` | Total budget of a streamed request, from before the submit. |
| `HORDE_OAI_FINAL_STATUS_TIMEOUT_SECONDS` | `2` | Allowance for the one status read made at the deadline. |
| `HORDE_OAI_CANCEL_TIMEOUT_SECONDS` | `10` | Budget of one cancel's HTTP call. The wait for a free cancel slot is bounded by the same length. A cancel that does not get a slot in time is not sent and is retried. |
| `HORDE_OAI_CANCEL_RETRY_SECONDS` | `30` | How often cancels that failed, timed out or did not start are sent again, until 20 minutes after the submit, when the horde has expired the request. |
| `HORDE_OAI_SHUTDOWN_DRAIN_SECONDS` | `30` | How long uvicorn waits for open requests on a clean stop before cancelling their handlers. |
| `HORDE_OAI_SHUTDOWN_CANCEL_SECONDS` | `15` | Process-wide bound for cancelling every request still open at stop. |

### Streaming and status polling

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_HEARTBEAT_SECONDS` | `5` | Interval of the keepalive comment on a streamed response. |
| `HORDE_OAI_DELTA_PACING_MAX_WINDOW_SECONDS` | `2` | Longest window over which a relayed stream spreads the text of one worker progress post. The window is the estimated interval between the worker's posts, up to this length. `0` sends each post as one chunk when it arrives. |
| `HORDE_OAI_DELTA_PACING_MAX_CHUNKS_PER_SECOND` | `20` | Maximum number of content chunks a paced stream sends per second of its window. |
| `HORDE_OAI_FALLBACK_POLL_SECONDS` | `15` | Base status poll interval when no signal arrives. It stretches to half the horde's `wait_time`, at most 60 seconds, and is shortened at random by up to a fifth. |
| `HORDE_OAI_SIGNAL_RECHECK_SECONDS` | `0.5` | Delay of the one extra status read after a terminal signal whose outcome the first read did not show. |
| `HORDE_OAI_FAIL_FAST_GRACE_SECONDS` | `20` | How long the Horde may report a lack of eligible workers, without a processing generation, before the request is cancelled and returns 503. |
| `HORDE_OAI_SUBMIT_RECOVERY_ATTEMPTS` | `2` | How many times a submit with a lost response is sent again with the same operation key, within the request's budget. `0` returns 502 at once. See [Lost submits](#lost-submits). |

### Connection pools and proxy addresses

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_NATIVE_MAX_CONNECTIONS` | `200` | Simultaneous submits, status reads and model lists to the horde, and the size of their pool. 32 of these are reserved for submits, so status reads cannot hold back a new request's submit. The value must be above 32. Cancels use a separate pool sized from `HORDE_OAI_MAX_INFLIGHT`. |
| `HORDE_OAI_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Addresses uvicorn trusts for `X-Forwarded-For`. Set it to the listener's addresses: the resulting client address is the per-source admission key and the `Proxied-For` value. |

The proxy passkey belongs to a service account: `PUT /v2/users/<user id>` with `{"generate_proxy_passkey": true}`,
sent with that account's API key, returns it. Without it, the horde sees every request as coming from the front's
address, and one abusive client can put that address in timeout for all of them.

Verification: `uv run python -c "from horde.oai.settings import load_settings; print(load_settings())"` prints the
settings used by the front, without the Redis URL or the passkey. An invalid setting produces an error with the
variable's name.

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

The interactive API documentation is at `http://127.0.0.1:7002/docs`, and `/` redirects there. `/redoc` shows the same
description, and `/openapi.json` is the OpenAPI document itself. `/healthz` and `/routing` are left out of it, since
only operators call them.

Reversal: stop the process, or `docker compose stop aihorde_oai`. On a clean stop the front waits up to
`HORDE_OAI_SHUTDOWN_DRAIN_SECONDS` for open requests, then cancels every request still open on the horde within
`HORDE_OAI_SHUTDOWN_CANCEL_SECONDS` before closing its Redis subscription. A request whose earlier cancel failed or
timed out, even one with a returned response, gets its cancel sent once more. A cancel still in progress is not sent twice.

While the process runs, the same failed cancels are sent again every `HORDE_OAI_CANCEL_RETRY_SECONDS`.
At most 10,000 requests with returned responses are held for this. Past that the oldest is dropped with a warning.

After a crash, open connections are dropped. Their Horde jobs run to completion and are charged. Their replies
are not collected.

### Root routing

A root is usable when its drain agent's last reply is `ready` and it is not left out of the rotation after a refused
or dropped connection. Calls rotate across the usable primary roots. When every primary root is unusable, they rotate
across the usable fallback roots. When every fallback root is unusable too, the call fails with `HordeUnavailable`. The
front does not call a drained root, even for a recovering submit.

Each root starts as `ready`. Only a `ready` or `drain` reply changes it. The state does not change on an unreachable
agent, a timeout or any other reply. A warning is logged on the first failure in a row.

```bash
curl -s http://127.0.0.1:7002/routing
```

returns each root's `horde_url`, `tier` (`primary` or `fallback`), `agent_state`, `usable`, `unhealthy` and
`inflight_calls`, which counts submits, status reads, model lists and cancels sent to that root and not yet finished.
`drain_agents` lists each polled agent's `agent_address` and `last_success_unix_seconds`. A request with
`X-Forwarded-For` gets a 404 response. Listeners set that header, so the view can only be read on the front's address.

## 3. Check the model list

```bash
curl -s http://127.0.0.1:7002/v1/models | jq '.data[].id'
```

Verification: the names match the horde's `GET /v2/status/models?type=text&chat=true`, which lists only models that
an online chat worker serves. An empty list means no chat worker is online. `GET /v1/models/<model name>` returns one
entry. The name may contain slashes. The list is cached for five seconds per process.

Each entry is built from the model's `chat` object in that list:

| Field | Value |
| --- | --- |
| `id`, `object`, `owned_by` | The model name, `model`, `aihorde`. |
| `created` | The horde's `chat.created`, or `0` when it records none. |
| `context_length`, `top_provider.context_length` | `chat.max_context_length`, the largest an online chat worker declares. |
| `top_provider.max_completion_tokens` | `chat.max_length`, the largest an online chat worker declares. |
| `architecture` | Text in and out. `input_modalities` adds `image`, and `modality` becomes `text+image->text`, when a worker declares the `vision` feature. |
| `supported_parameters` | `max_tokens`, `temperature`, `top_p` and `stop`, then `seed`, `frequency_penalty`, `presence_penalty` and `reasoning_effort` for each a worker declares as a feature. `tools` and `parallel_tool_calls` with the `tools` feature, and with them `tool_choice` under the `tool_choice_enforced` guarantee. `response_format` with `json_object` or `json_schema`. |
| `x_horde.workers` | `chat.workers`, the online chat workers serving the model. |
| `x_horde.queued` | The model's queue, which chat requests share with text requests. Absent when the horde reports none. |

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

Verification: the response is a `chat.completion`. Its `id` is `chatcmpl-` plus the Horde request id, and its
`model` is the model that generated the reply.
 `x_horde` includes `request_id`, `worker_id`, `worker_name`, `kudos` (what the request
cost), `charge_basis` (the Horde's pricing basis, passed through unchanged),
`estimated_usage` and, when the horde supplies them, `queue_wait_seconds` and `context_adjustments`.

`estimated_usage: true` means the worker did not report token counts and `usage` is an estimate at three characters
per token. `x_horde.notice` appears when the worker did not return visible text. A sampling setting or the model choice
is the likely cause. Each choice includes the worker's `native_finish_reason` beside `finish_reason`.

The front logs one line per request:
`Chat request <id> finished: streamed=False submitted_at=... woke_at=... woke_by=signal waited=...s`. `woke_by=poll`
on most requests means signals are not arriving.

### Streaming check

Add `"stream": true` (and optionally `"stream_options": {"include_usage": true}`) to receive server-sent events
instead: `: horde queue_position=7 wait_time=42` comments while the request waits, then a role chunk, content
chunks, a finish chunk with `x_horde`, the usage chunk with `choices: []` when asked for, and
`data: [DONE]`. A plain request gets its content as the worker posts progress. The text of each post is spread over the
estimated interval until the next post. A request with
tools or structured output gets the whole text in one chunk. The response includes `Cache-Control: no-cache`,
`X-Accel-Buffering: no` and `x-should-retry: false`.

The same request works from an OpenAI SDK by pointing its base URL at `http://<front-host>:7002/v1` and using the horde
API key as the SDK's API key.

Reversal: none needed. A request the client abandons is cancelled on the horde.

## 5. Check the front against the OpenAI SDK

The conformance module drives the pinned official `openai` Python SDK against the real front and the real native app,
each served on a loopback port, with a fake chat worker answering every job, so it does not need an inference
backend. It runs
under the integration test stack, which provides Postgres, the front's Redis subscription and object storage for
chat images:

```bash
AI_HORDE_REQUIRE_TEST_DEPS=1 bash tests/run_with_test_stack.sh --   .venv/Scripts/python.exe -m pytest tests/integration/test_chat_front_sdk.py -p no:cacheprovider
```

On Linux or macOS the interpreter is `.venv/bin/python`.

The runner works from Git Bash as from Linux or macOS. When the Garage bootstrap fails it exits with an error and
does not run the command, so a test run does not silently provision separate databases in place of the stack.

Verification: every test passes: the model list includes the worker's chat model and leaves out a model that is not served by any online
worker. A completion and a streamed completion
return the worker's reply, `finish_reason` `stop` and the worker's token counts. `logit_bias` is a `BadRequestError`
whose `param` is `logit_bias`. A model without an online worker returns 503 `no_eligible_worker` at once. And an
image sent as a data URL reaches the worker, which declares `vision` and the `url` image transport, as
a signed `http(s)` URL.

Reversal: the run creates and drops its database schema. The test stack stays up for later runs unless
`run_with_test_stack.sh` is given `--down`.

## Request keys the front accepts

The front accepts `model` (or `models`), `messages`, `stream`, `stream_options`, `max_tokens`,
`max_completion_tokens`, `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`,
`reasoning_effort`, `n`, `tools`, `tool_choice`, `parallel_tool_calls` and `response_format`.
It applies streaming options locally and forwards generation parameters to the Horde.
`max_tokens` and `max_completion_tokens` must agree when both are sent and lie between 16 and 16384. With neither, the
horde generates up to 512 tokens. `n` must be 1. `stop` is a string or up to four strings. `stream_options` needs
`"stream": true`.

Message roles are `system`, `developer`, `user`, `assistant` and `tool`. `developer` is stored as
`system`, so the developer role loses precedence over system messages. An `assistant` message may include `tool_calls` and a `tool` message
answers one by `tool_call_id` (see the [chat front reference](../reference/chat_front.md)). Content is a string or an
array of `text` parts. A `user` message may also include `image_url` parts (see [Image inputs](#image-inputs)).

Stock clients send some keys at their defaults. These are accepted and dropped: empty or null `tools`, null
`tool_choice` or `parallel_tool_calls`, `response_format: {"type": "text"}`, `logprobs: false`, `top_logprobs: 0`,
`store: false` and `service_tier` `"auto"` or `"default"`. `user` and `metadata` are accepted and unused.

A non-null `tool_choice`, including `"none"`, or `parallel_tool_calls` without tools returns 400
`ChatRequestConflict`. Unsupported top-level keys and content part types return 400 `UnsupportedChatParameter`
with the path in `param`, such as `messages[1].content[0].type=input_audio`. Unknown keys inside messages,
tool definitions and `tool_choice` are dropped. See [Parameter screening](../reference/chat_front.md#parameter-screening).

The `horde` object takes worker restrictions and context options, along with conformance, reasoning visibility,
availability and kudos limits. `dry_run` and `operation_key` are refused on the front. See
[Horde options](send_chat_completions.md#sampling-defaults-and-the-horde-object) for the complete list and defaults.

## Image inputs

A `user` message may include `{"type": "image_url", "image_url": {"url": ..., "detail": ...}}` parts, with `url` a
`data:image/...;base64,` URL or an `http(s)` URL and `detail` optionally `auto`, `low` or `high`. The front forwards
them unchanged. It refuses before the submit a request with more than 20 image parts (400
`TooManyExtraSourceImages`), a data URL that decodes to more than 5 MiB, or data URLs that decode to more than 25 MiB
in all (400 `SourceImageSizeExceeded`, with `param` the path of the part's `url`). Data URLs do not count toward
`HORDE_OAI_MAX_TEXT_BYTES`.

The front does not have the user's trust status. The Horde therefore enforces the 5 MiB total for users who are
neither trusted nor patrons. It fetches, checks and stores each image, charges the extra
source image tax per image, refuses images over the user's total (400 `SourceImageSizeExceeded`), and sends the
image only to a worker that declares the `vision`
feature and an image transport.

Image refusals pass through with the horde's `rc` as `code`, such as
`SourceImageUrlRefused` for a URL at a private address or `SourceImageResolutionExceeded`, and 503
`ChatImageStorageUnavailable` when the image store refuses the image. A model whose list entry shows `image` in
`architecture.input_modalities` has such a worker online.

## Errors the front returns

Every error is an OpenAI envelope, `{"error": {"message", "type", "param", "code", "metadata"}}`, with `param` and
`code` written as null when they do not apply. `metadata` holds what is known: `request_id`, the horde's `rc`,
`retry_after`, and `submit_outcome: "unknown"` with the request's `operation_key` when a submit was lost and recovery
did not settle it.

A Horde error keeps the Horde's status, and its
`rc` becomes `code`. A horde 429 keeps `Retry-After` and the `X-RateLimit-*` headers, and has code
`rate_limit_exceeded` when the Horde does not send an `rc`, such as for its per-route request limit. A horde 503 without
`Retry-After` (such as `ChatAdmissionDisabled`) gets `Retry-After: 30`. A status read that ends the wait with a
horde error other than 404, 429 or a 5xx, such as 401 or 403, cancels the request before the error is returned. A 404
means the horde no longer holds it.

Once the horde holds a request, or may hold one, every error includes `x-should-retry: false`, so OpenAI SDKs do not
retry into a second charged job. Errors before the submit do not include the header and are safe to retry.

| Status | `code` | `x-should-retry` | Cause |
| --- | --- | --- | --- |
| 400 | `UnsupportedChatParameter` | | A key, role, content part, value or size the service does not serve. `param` holds its path. |
| 400 | `ChatRequestConflict` | | `max_tokens` and `max_completion_tokens` differ, or `only` and `ignore` are both set. |
| 400 | `BadRequest` | | The body is not a JSON object or a value has the wrong type. |
| 400 | `TooManyExtraSourceImages` | | More than 20 image parts. |
| 400 | `SourceImageSizeExceeded` | | An image data URL decodes to more than 5 MiB. `param` is the path of the part's `url`. |
| 401 | `InvalidAuthorizationHeader` | | `Authorization` is present but not `Bearer <key>`. Omit it to use the anonymous key. |
| 404 | `model_not_found` | | The model id contains `:`. The front does not serve model variants. A horde 404 keeps the horde's `rc` as `code`. |
| 408 | `ChatRequestTimeout` | `false` | The request was still waiting after `HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS` and one final status read. It is cancelled. An outcome reached by the deadline is returned instead. `Retry-After: 30`. |
| 408 | `RequestBodyTimeout` | | The body took longer than `HORDE_OAI_BODY_READ_TIMEOUT_SECONDS` to arrive. The request was not submitted. |
| 413 | `RequestTooLarge` | | The body exceeds `HORDE_OAI_MAX_BODY_BYTES`. |
| 502 | `ChatRequestFaulted` | `false` | The horde reports the request as faulted, or it expired on the horde. When the fault followed a refused worker reply, the message states the refusal and `metadata.rc` includes its return code. |
| 502 | `ChatRequestNoGeneration` | `false` | The horde reported the request done without a generation and not faulted, after a short recheck or a `cancelled` signal, for example after a maintenance cancel. It is cancelled. |
| 502 | `HordeUnavailable` | | Every usable horde root in `HORDE_OAI_HORDE_URLS` and `HORDE_OAI_FALLBACK_HORDE_URLS` refused the connection, or every root is drained. |
| 502 | `HordeUnavailable` | `false` | The submit was sent, a response did not arrive, and a recovery response did not arrive. The request may exist. `metadata` holds `submit_outcome: "unknown"` and `operation_key`. |
| 502 | `OperationKeyConflict` | `false` | A recovery attempt was refused because the operation key holds a different request. The front re-sends the identical body, so this is a front defect. `metadata` holds the `rc` and `operation_key`. |
| 503 | `FrontAtCapacity` | | The process or the client's address holds its maximum of open requests. `Retry-After: 5`. |
| 503 | `no_eligible_worker` | `false` | No online worker could serve the request for `HORDE_OAI_FAIL_FAST_GRACE_SECONDS`. It is cancelled. `Retry-After: 30`. |
| 503 | `queue_unavailable` | `false` | A `horde.availability_mode: "fail_fast"` request whose first status read showed zero eligible worker threads, or an estimated wait over the time left in its budget. It is cancelled. A first status without queue information, which a request just submitted reads until the horde's cached queue positions refresh, is read again for up to 2 s (`POSITIONED_READ_WAIT_SECONDS`) before applying the wait estimate. `Retry-After` is the estimated wait, or 30 with zero threads. |

A streamed request's headers go out after the first status read, so a refusal at that read (a declaration or limit
gap, `queue_unavailable`, or a status error) is the HTTP error in the table, as on a non-streamed request. In a
stream, an error after the headers arrives as one `data:` event with the same envelope and no choice, then
`data: [DONE]`. A streamed request has a separate budget, `HORDE_OAI_STREAM_TIMEOUT_SECONDS`.

A client retry after a lost response is a new request and can create a second charged job. The front does not
deduplicate requests.

## Lost submits

The front sends each client request with a fresh UUID as `horde.operation_key`. The horde claims the key for the
submitting user when it activates the request. A repeated key with the same body returns the first
request's `{id, kudos}` with status 200, without queuing or charging anything.

When a submit was sent and a response did not arrive (a read timeout, a reset or a dropped connection), the front sends the
identical body with the same key again to the next root, up to `HORDE_OAI_SUBMIT_RECOVERY_ATTEMPTS` times and only
while the request's budget lasts. When every root is out of the rotation after the failure, the attempt goes to them
anyway. Then:

- A response with an id, 200 for an existing claim or 202 when the lost submit did not reach the Horde, continues the
  request as any submit does. The log shows `Recovered the lost submit of operation <key> on attempt <n>`.
- A connection failure, a 429 or a 5xx moves on to the next attempt.
- `OperationKeyConflict` returns 502, as in the table above.
- Any other horde refusal is passed through with `x-should-retry: false`, `submit_outcome: "unknown"` and the key,
  since the lost submit may still have queued the request.
- With the attempts or the budget spent, the response is 502 `HordeUnavailable` with `submit_outcome: "unknown"` and the
  key. The key finds the request, if one exists, in `waiting_prompts.operation_key`.

A refused connection on the first submit is not a lost submit: the request did not reach a horde, and the 502 does
not include an `x-should-retry` header.

Two metrics count these cases (see [Chat front reference](../reference/chat_front.md#metrics)):

| Metric | Counts |
| --- | --- |
| `horde.oai.submit.recoveries` | Recovered submits. `horde.existing_claim` is true when the Horde returned an existing claim (200) and false when it returned 202. First-try submits are not counted. |
| `horde.oai.client_retry_hints` | Chat requests arriving with `x-stainless-retry-count` above zero, the header OpenAI SDKs send on their own retries. |

Neither metric measures prevented duplicate requests from clients. A claim match shows that the Horde queued a
request whose submit response was lost. A retry hint shows that a client SDK retried, for any cause.

## Export telemetry

The front exports OpenTelemetry traces (one server span per request, `/healthz` excluded, and a client span per
call to the horde that the horde's spans continue), metrics, and its Loguru log records as OpenTelemetry logs,
through the same environment variables as the horde's Flask application. With none of them set it does not export
anything.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP endpoint for every signal. Setting it, or one of the per-signal endpoints below, turns export on. |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | unset | Per-signal OTLP endpoints. |
| `AI_HORDE_TELEMETRY_ENABLED` | unset | `true`, `1` or `yes` turns export on without an endpoint variable. |
| `OTEL_SDK_DISABLED` | unset | `true` keeps export off whatever else is set. |
| `OTEL_SERVICE_NAME` | `horde-oai` | Service name of the front's telemetry. Leave it unset where the horde's environment sets it, so the front is not reported as `ai-horde`. |
| `DEPLOYMENT_ENVIRONMENT` | `development` | The `deployment.environment` resource attribute. |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Head sampling ratio, as for the horde. |

`PYROSCOPE_ENABLED=true` starts Pyroscope continuous profiling with the horde's `PYROSCOPE_*` variables (see
`horde.telemetry`). The application name is the front's service name, so its profiles are separate from the horde's.

Verification: with an endpoint set, a collector receives `horde.oai.*` metrics within the SDK's export interval
after the first request, one `Chat request <id> finished: ...` log record per finished request, and a trace per
request whose root is the front's `POST /v1/chat/completions` span with the horde's `POST /api/v2/generate/chat/async`
span beneath it. The metric and log field lists are in the [Chat front reference](../reference/chat_front.md).

Reversal: unset the variables and restart the front.
