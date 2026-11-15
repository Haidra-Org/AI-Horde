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

Run the OpenAI-compatible chat front as a separate process alongside an existing Horde deployment. You need:

- A running Horde with the `/v2/generate/chat` routes reachable over HTTP from the front.
- Access to the Horde's primary Redis server.
- At least one text worker with chat enabled.
- The project's dependencies installed with `uv sync`.

After starting the process, check its health endpoint and model list, then send a chat request to verify that it can
return a worker's reply. API documentation is available at `/docs`, `/redoc` and `/openapi.json`.

The front is a separate ASGI process. It does not hold a database connection or import anything from the Flask
application. It submits each chat request to the horde's native `/v2/generate/chat/async` and waits for the horde's
Redis signal on `horde:chat:<request id>`, polling the status route as a fallback. It then returns an OpenAI response.
Every route, setting, error code and metric is listed in the [Chat front reference](../reference/chat_front.md).

## 1. Set the connection settings

The front reads settings from environment variables and a `.env` file in the working directory at startup.
Most settings have defaults, but these four need deployment-specific values:

| Variable | Value |
| --- | --- |
| `HORDE_OAI_HORDE_URLS` | Comma-separated API root URLs of the horde instances, such as `http://aihorde:7001/api`. The default is `http://127.0.0.1:7001/api`. |
| `HORDE_OAI_REDIS_URL` | URL of the horde's primary Redis, such as `redis://redis:6379/7`. Without it, every request waits for the fallback poll, and streams are not relayed as the worker writes them. |
| `HORDE_OAI_PROXY_PASSKEY` | The proxy passkey of the front operator's service account. |
| `HORDE_OAI_FORWARDED_ALLOW_IPS` | The listener's addresses, when the front runs behind a listener that sets `X-Forwarded-For`. The default is `127.0.0.1`. |

The `aihorde_oai` service in `docker-compose.yaml` sets the first two.

To get the proxy passkey, send `PUT /v2/users/<user id>` with `{"generate_proxy_passkey": true}` and the service
account's API key. With the passkey, the front sends each client's address as `Proxied-For`, so IP timeouts and
suspicion apply to the client. Without it, the horde records the front's address as the source of every request, and
one abusive client can put that address in timeout for all clients.

The full list, with limits, budgets, polling and pool sizes, is in [Settings](../reference/chat_front.md#settings).

Verification: `uv run python -c "from horde.oai.settings import load_settings; print(load_settings())"` prints the
settings used by the front, without the Redis URL or the passkey. When a variable is set but invalid, the command
fails with an error that gives the variable's name, and the front does not start.

## 2. Start the front

From the repository root:

```bash
uv run python -m horde.oai
```

With docker compose, the `aihorde_oai` service runs the same command from the horde image and publishes port 7002:

```bash
docker compose up -d aihorde_oai
```

Check that the log includes `Subscribed to chat signals on horde:chat:*`, then query the health endpoint:

```bash
curl -s http://127.0.0.1:7002/healthz
```

Before any requests are running, the response should be
`{"live": true, "ready": true, "signal_subscribed": true, "inflight": 0}`. `ready: false` means the
process is at `HORDE_OAI_MAX_INFLIGHT` or stopping. `signal_subscribed: false` means Redis is unset or unreachable.
Requests still complete, but only at the fallback poll interval.

The interactive API documentation is at `http://127.0.0.1:7002/docs`, and `/` redirects there. `/redoc` shows the same
description, and `/openapi.json` is the OpenAPI document itself. `/healthz` and `/routing` are left out of it, since
only operators call them.

To stop the front, stop the process or `docker compose stop aihorde_oai`. On a clean stop the front waits up to
`HORDE_OAI_SHUTDOWN_DRAIN_SECONDS` for open requests, then cancels every request still open on the horde within
`HORDE_OAI_SHUTDOWN_CANCEL_SECONDS` before closing its Redis subscription. The cancel is sent once more for a request
whose earlier cancel failed or timed out, even when the request's response has been returned. A cancel still in
progress is not sent twice.

While the process runs, the same failed cancels are sent again every `HORDE_OAI_CANCEL_RETRY_SECONDS`. At most 10,000
requests with returned responses are held for this. Past that, the oldest is dropped with a warning.

After a crash, open connections are dropped. Their Horde jobs run to completion and are charged. Their replies
are not collected.

## 3. Check the model list

```bash
curl -s http://127.0.0.1:7002/v1/models | jq '.data[].id'
```

Verification: the names match the horde's `GET /v2/status/models?type=text&chat=true`, which lists only models served
by an online chat worker. If the list is empty, check that a worker has registered with chat enabled. `GET /v1/models/<model name>` returns one
entry. The name may contain slashes. The list is cached for five seconds per process.

Each entry's context length, completion limit, input modalities and `supported_parameters` come from the declarations
of the online chat workers. [Model card fields](../reference/chat_front.md#model-card-fields) lists every field.

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
`model` is the model that generated the reply. `x_horde` includes `request_id`, `worker_id`, `worker_name`, `kudos`
(what the request cost), `charge_basis` (the Horde's pricing basis, passed through unchanged), `estimated_usage` and,
when the horde supplies them, `queue_wait_seconds` and `context_adjustments`.

`estimated_usage: true` means the worker did not report token counts and `usage` is an estimate at three characters
per token. `x_horde.notice` appears when the worker did not return visible text. Inspect the finish reason and the selected model's
reasoning budget before changing settings. Each choice includes the worker's `native_finish_reason` beside
`finish_reason`.

If the response is an error envelope, use [Errors the front returns](#errors-the-front-returns) to diagnose
it before resending; a retry can create another charged job.

The front logs one line per request:
`Chat request <id> finished: streamed=False submitted_at=... woke_at=... woke_by=signal waited=...s`. `woke_by=poll`
on most requests means signals are not arriving.

### Streaming check

Add `"stream": true` (and optionally `"stream_options": {"include_usage": true}`) to receive server-sent events
instead. The stream has `: horde queue_position=7 wait_time=42` comments while the request waits, then a role chunk,
content chunks, a finish chunk with `x_horde`, the usage chunk with `choices: []` when requested, and `data: [DONE]`.
A plain request gets its content as the worker posts progress. The text of each post is spread over the estimated
interval until the next post. A request with tools or structured output gets the whole text in one chunk. The
response includes `Cache-Control: no-cache`, `X-Accel-Buffering: no` and `x-should-retry: false`.

The same request works from an OpenAI SDK by pointing its base URL at `http://<front-host>:7002/v1` and using the horde
API key as the SDK's API key. The keys a client may send, and the keys the front refuses, are in
[Send chat completions](send_chat_completions.md#request-keys-the-front-accepts) and
[Request keys](../reference/chat_front.md#request-keys).

Closing the client connection triggers cancellation on the Horde. In-flight streamed work is charged for
the content already posted; an in-flight non-streamed job can still settle when its worker submits. See
[Settlement on cancellation](../reference/chat_requests.md#settlement-on-cancellation).

## Horde instance routing

A horde instance is usable when the last reply from its drain agent is `ready` and that Horde instance is not out of the
rotation because of a refused or dropped connection. Calls rotate across the usable primary horde instances. When
every primary horde instance is unusable, calls rotate across the usable fallback horde instances. When every fallback
horde instance is unusable too, the call fails with `HordeUnavailable`. The front does not call a drained horde
instance, even to recover a lost submit.

Each horde instance starts as `ready`. Only a `ready` or `drain` reply changes that state. The state does not change
on an unreachable agent, a timeout or any other reply. A warning is logged on the first failure in a row. Drain agent
polling is off unless `HORDE_OAI_DRAIN_AGENT_PORT` is set.

```bash
curl -s http://127.0.0.1:7002/routing
```

returns `instances`, with each horde instance's `horde_url`, `tier` (`primary` or `fallback`), `agent_state`, `usable`,
`unhealthy` and `inflight_calls`. `inflight_calls` counts the submits, status reads, model lists and cancels sent to
that horde instance and not yet finished. `drain_agents` lists each polled agent's `agent_address` and
`last_success_unix_seconds`. A request with `X-Forwarded-For` gets a 404 response. Listeners set that header, so the
view can only be read on the front's address.

## Client authentication and attribution

A client's `Authorization: Bearer <key>` becomes the horde `apikey`. A request without one uses the anonymous key. The
front's `Client-Agent` header is `<application>:<version>:<user agent> via horde-oai-front`. Horde statistics use it to
attribute traffic to the application and distinguish it from direct native calls.

The application value is the first of `X-Title`, `X-OpenRouter-Title`, the host of `HTTP-Referer` and the host of
`Referer` that is present, at most 80 characters. These values use version `0`. Without these headers, the value is
`horde-oai-front` with the Horde version. The user agent is the client's `User-Agent`, or `unknown`.

Control characters and colons are removed from each part. The whole value is at most 256 characters, with the
user agent truncated first.

## Errors the front returns

The [error response contract](../reference/chat_front.md#errors-the-front-returns) lists statuses, codes,
retry headers and stream failures. Use these checks when diagnosing the deployment:

- For `FrontAtCapacity`, inspect `/healthz` and the admission limits in
  [Settings](../reference/chat_front.md#settings).
- For `HordeUnavailable`, check the configured native API roots and their drain state with `/routing`.
  An unknown submit outcome requires the investigation in [Lost submits](#lost-submits).
- For `OperationKeyConflict` during recovery, investigate the front: it must resend the identical body.
- For worker availability errors, inspect the model list and the reported declaration or size gap in
  [No eligible worker](../reference/chat_front.md#no-eligible-worker).

A client retry creates a new request and can create a second charged job. Preserve `x-should-retry: false`
when diagnosing a failure with a client.

## Lost submits

The front sends each client request with a fresh UUID as `horde.operation_key`. The horde claims the key for the
submitting user when it activates the request. A submit that repeats the key with the same body returns the first
request's `{id, kudos}` with status 200. It does not queue or charge anything.

The outcome of a submission is uncertain when its response does not arrive, for example after a read timeout or
connection reset. Recovery also applies
when the Horde returns a 5xx other than a 503 `ChatAdmissionDisabled` or `ChatImageStorageUnavailable`. The front
then sends the identical body with the same key again to the next horde instance. It does this up to
`HORDE_OAI_SUBMIT_RECOVERY_ATTEMPTS` times and only while the request's budget lasts. When every horde instance is out
of the rotation after the failure, the attempt still goes to the horde instances that are not drained. Then:

- A response with an id, 200 for an existing claim or 202 when the lost submit did not reach the Horde, continues the
  request as any submit does. The log shows `Recovered the lost submit of operation <key> on attempt <n>`.
- After a connection failure, a 429 or a 5xx, the front makes the next attempt.
- `OperationKeyConflict` returns 502. See the [error contract](../reference/chat_front.md#errors-the-front-returns).
- Any other horde refusal is passed through with `x-should-retry: false`, `submit_outcome: "unknown"` and the key,
  since the lost submit may still have queued the request.
- With the attempts or the budget spent, the response is 502 `HordeUnavailable` with `submit_outcome: "unknown"` and the
  key. If the request exists, it can be found by the key in `waiting_prompts.operation_key`.

A refused connection on the first submit is not a lost submit. The request did not reach a horde, and the 502 does
not include an `x-should-retry` header.

`horde.oai.submit.recoveries` counts recovered submits, and `horde.oai.client_retry_hints` counts requests that
OpenAI SDKs sent as their own retries (see [Metrics](../reference/chat_front.md#metrics)). Neither metric measures
prevented duplicate requests from clients. A claim match shows that the Horde queued a request whose submit response
was lost. A retry hint shows that a client SDK retried, for any cause.

## Export telemetry

The front exports OpenTelemetry traces, metrics, and its Loguru log records as OpenTelemetry logs. It uses the same
environment variables as the horde's Flask application. Traces have one server span per request, excluding
`/healthz`, and one client span per call to the horde, which the horde's spans continue. With none of the variables
set, the front does not export anything.

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
span beneath it. The metric and log field lists are in the [Chat front reference](../reference/chat_front.md#telemetry).

## Check the front against the OpenAI SDK

After changing the front, run the SDK conformance tests to check its behaviour through an actual client.

The conformance tests run the pinned official `openai` Python SDK against the real front and the real native app,
each served on a loopback port. A fake chat worker completes every job, so the tests do not need an inference
backend. They run under the integration test stack, which provides Postgres, the front's Redis subscription and
object storage for chat images:

```bash
AI_HORDE_REQUIRE_TEST_DEPS=1 bash tests/run_with_test_stack.sh --   .venv/Scripts/python.exe -m pytest tests/integration/test_chat_front_sdk.py -p no:cacheprovider
```

On Linux or macOS the interpreter is `.venv/bin/python`.

The runner works from Git Bash as well as from Linux or macOS. When the Garage bootstrap fails, the runner exits with
an error and does not run the command, so a test run does not silently provision separate databases in place of the
stack.

Verification: every test passes. [Contract tests](../reference/chat_front.md#contract-tests) maps each test to the
contract it covers.

Reversal: the run creates and drops its database schema. The test stack stays up for later runs unless
`run_with_test_stack.sh` is given `--down`.
