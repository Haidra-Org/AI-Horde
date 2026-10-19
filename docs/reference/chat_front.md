---
title: "Chat front reference"
summary: "The horde.oai package by concern, its request keys, message roles, response, stream and model card shapes, its metrics and labels, and its per-request log line."
topics: [operations, requests]
order: 110
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Chat front reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [operations](../topics.md#operations), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

The chat front is the `horde.oai` package, run as `python -m horde.oai`. It serves the OpenAI chat completions and
model list routes over the horde's native `/v2/generate/chat` routes (see
[Chat requests reference](chat_requests.md)) and does not import anything from the Flask application. Running it is covered in
[Run the OpenAI-compatible chat front](../how-to/run_openai_chat_front.md).

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Entry point and telemetry setup | `horde/oai/__main__.py` | `main`, `configure_telemetry`, `FRONT_SERVICE_NAME` |
| Settings | `horde/oai/settings.py` | `FrontSettings`, `load_settings` |
| Application and routes | `horde/oai/app.py` | `create_app`, `FrontState` |
| Admission caps and gauges' source | `horde/oai/app.py` | `InflightLimiter`, `InflightSlot` |
| Request lifecycle | `horde/oai/app.py` | `_chat_completions`, `_wait_for_completion`, `_stream_completion` |
| Submit and lost-submit recovery | `horde/oai/app.py` | `_submit`, `_recover_submit` |
| Bounded cancels | `horde/oai/app.py` | `_cancel_within_budget`, `_cancel_open_requests` |
| Per-request timing and finished line | `horde/oai/app.py` | `RequestTrace` |
| Error envelope and SSE error | `horde/oai/app.py` | `ChatFrontError` |
| OpenAI request, response and error models | `horde/oai/schema.py` | `ChatCompletionRequest`, `ChatCompletionResponse`, `ErrorMetadata` |
| Refused and no-op parameters | `horde/oai/schema.py` | `find_refused_parameter`, `drop_default_no_ops` |
| Native body, completion, usage estimate | `horde/oai/mapping.py` | `build_native_chat_body`, `build_completion`, `estimate_tokens` |
| Model list entries | `horde/oai/mapping.py` | `build_model_card` |
| Streamed chunks and keepalive | `horde/oai/sse.py` | `build_completion_events`, `build_keepalive_event`, `build_error_events` |
| `Client-Agent` attribution | `horde/oai/attribution.py` | `build_client_agent` |
| Native HTTP client and root rotation | `horde/oai/horde_client.py` | `HordeClient`, `create_http_client` |
| Completion signal subscription and liveness ping | `horde/oai/waiter.py` | `CompletionSignalListener` |
| Per-request wait, poll and deadline | `horde/oai/waiter.py` | `ChatRequestWatcher`, `WaitBudget`, `WakeSource` |
| No eligible worker and declaration gap | `horde/oai/waiter.py`, `horde/oai/app.py` | `NoEligibleWorkerError`, `DeclarationGapError`, `_wait_error` |
| Instruments and bucket boundaries | `horde/oai/metrics.py` | `FrontMetrics`, `histogram_views` |

## Request keys

The front validates a request against `ChatCompletionRequest` after three screening steps in `horde/oai/schema.py`.
A body that is not JSON, or that holds `NaN`, `Infinity` or `-Infinity` (which Python's parser otherwise accepts), is
400 `BadRequest` before screening.
`find_refused_parameter` refuses, by path, any top-level key that is neither a request field nor in
`DEFAULT_NO_OP_PARAMETERS`, any key of that table holding a value other than its default, an unserved role and an
unserved content part type. `drop_default_no_ops` then drops the table's keys that hold their default, and
`drop_unknown_nested_keys` drops the unserved keys inside messages (see [Message roles](#message-roles)), the
`tools` definitions and the `tool_choice` object: a `tools` entry keeps `type` and `function`, and its function keeps
`name`, `description`, `parameters` and `strict`, with the `parameters` schema forwarded unchanged; a `tool_choice`
object keeps `type` and `function`, and its function keeps `name`. Every other object, and the top level, refuses
unknown keys, as OpenAI does: an unknown key inside `stream_options` or `response_format` is 400 at its path.
`response_format.json_schema` reads its schema only from `schema`; `schema_`, the Python field name, is an unknown key.

| Keys | Handling |
| --- | --- |
| `model`, `messages`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`, `n`, `horde` | Validated and forwarded; null counts as omitted. |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` | Validated against the `horde/chat_contract.py` shapes and forwarded verbatim; a `json_schema` response format is forwarded with its `schema` key and needs one. Dropped at their default: empty or null `tools`, `{"type": "text"}`; a null `tool_choice` or `parallel_tool_calls` is not forwarded. |
| `stream`, `stream_options`, `user`, `metadata` | Applied or accepted by the front and not forwarded. |
| `logprobs`, `top_logprobs`, `store`, `service_tier` | Dropped at their default (`false`, `0`, `false`, `auto` or `default`); any other value is 400. |

After validation the front applies the request rules it shares with the native route from `horde/chat_contract.py`,
so the client receives OpenAI's message and `param`: a `tool_choice` or `parallel_tool_calls` without tools
(`ChatRequestConflict`), a tool history breaking OpenAI's rule and a `strict` `json_schema` breaking OpenAI's
structural rules (`UnsupportedChatParameter`), and, under `horde.openai_guards`, a `json_object` request without the
word "json" in its messages (`ChatRequestConflict`, `param` `messages`). A tool `parameters` or response schema nested
deeper than `MAX_CHAT_SCHEMA_NESTING` fails validation as `UnsupportedChatParameter` at its path. The native route
repeats these checks and also checks a forced function the tools do not define and the sizes in `MAX_CHAT_TOOL_*`.
The message text limit counts the function names and arguments of history tool calls with the texts.

## Message roles

| Role | Keys | Content |
| --- | --- | --- |
| `system`, `developer` | `role`, `content`, `name` | A string or `text` parts. |
| `user` | `role`, `content`, `name` | A string, or `text` and `image_url` parts. |
| `assistant` | `role`, `content`, `name`, `tool_calls` | A string or `text` parts; null or omitted only with `tool_calls`, and then forwarded as null. |
| `tool` | `role`, `content`, `tool_call_id` (required) | A string or `text` parts. |

A key outside its role's set (`SUPPORTED_MESSAGE_KEYS`) is dropped and not forwarded, as OpenAI ignores it; a `name`
on a `tool` message is dropped. `tool_calls` on a message other than an assistant message is 400
`UnsupportedChatParameter` at `messages[<i>].tool_calls`, as OpenAI refuses it. The same applies inside a message: a tool call keeps `id`, `type` and `function`, its
function keeps `name` and `arguments`, a `text` part keeps `type` and `text`, an `image_url` part keeps `type` and
`image_url`, and the `image_url` object keeps `url` and `detail`. Served keys keep their validation. This covers the
fields the OpenAI Python SDK adds to an accumulated streamed reply (`refusal`, `audio`, `annotations`,
`function_call`, `parsed`, `tool_calls[].index`, `function.parsed_arguments`) when it replays that reply as history.
An unserved role (such as `function`) or content part type (such as `input_audio`) is still 400
`UnsupportedChatParameter`, with `param` such as `messages[0].role` or `messages[0].content[0].type=input_audio`.

## Response shape

`choices[i].message` is `{"role": "assistant", "content": ..., "tool_calls": [...]}`. `content` is always written and
is null only when the worker returned tool calls without text; `tool_calls` is written only when the worker returned
calls, each `{"id", "type": "function", "function": {"name", "arguments"}}` as the worker sent it. `finish_reason` is
`stop`, `length`, `content_filter` or `tool_calls`. `x_horde.notice` marks a reply with neither visible text nor tool
calls.

A stream sends, after its keepalive comments, these `chat.completion.chunk` events:

1. `delta: {"role": "assistant"}`.
2. A content chunk with the whole `delta.content`, when the reply has text.
3. A tool-call chunk with `delta.tool_calls`, when the reply has calls, each call whole with its 0-based `index`,
   `id`, `type` and `function`. It carries no content, because some clients read tool calls only from such chunks.
4. `delta: {}` with `finish_reason`, `native_finish_reason` and `x_horde`.
5. `choices: []` with `usage`, when `stream_options.include_usage` was true.
6. `data: [DONE]`.

The front answers browser preflights and allows any origin on every response (`Access-Control-Allow-Origin: *`),
exposing `Retry-After` and `x-should-retry`, so a web client such as KoboldAI Lite can call it with a bearer key.

## No eligible worker

While the native status reports `is_possible` false with nothing processing, the front waits `fail_fast_grace_seconds`
for a worker to appear, then cancels the request and returns 503 `no_eligible_worker` with `Retry-After: 30`. When
the status carries an `impossible_reason` of kind `declaration_gap` (see
[Impossible reason](chat_requests.md#impossible-reason)), it cancels at once and returns 404
`no_worker_supports_features`, OpenRouter's status for no endpoint supporting the requested features, with no
`Retry-After`. The message names the missing declarations or the exceeded limit, and adds that guarantees are not
required under `best_effort` when a guarantee is missing. After stream headers the same error is the SSE error event.
An `impossible_reason` of a kind the front does not know is ignored, so the grace applies.

## Model card fields

`GET /v1/models` lists each text model with at least one online chat worker. `context_length` and
`top_provider.max_completion_tokens` are the largest the workers declare; `architecture.input_modalities` adds `image`
for `vision`. `supported_parameters` is `max_tokens`, `temperature`, `top_p` and `stop`, then `seed`,
`frequency_penalty` and `presence_penalty` for the features of the same names, `tools` for `tools`, with
`tool_choice` added under the `tool_choice_enforced` guarantee and `parallel_tool_calls` under
`single_tool_call_enforced`, and `response_format` for `json_object` or `json_schema`. `x_horde` carries the
worker count and the queued work.

## Telemetry configuration

`configure_telemetry` runs before the application is built. It configures Logfire when
`horde.telemetry.telemetry_enabled` is true and `OTEL_SDK_DISABLED` is not `true`, with the same variables, resource
attributes and sampling as the Flask application, the service name `horde-oai` unless `OTEL_SERVICE_NAME` sets
another, and the views from `horde.oai.metrics.histogram_views`. It bridges Loguru records to OpenTelemetry logs. The
Flask application's `init_telemetry_early` is not reused because it instruments a Flask app.

`create_app` takes the meter from the global meter provider unless one is passed as `meter`; tests pass a meter of
a separate `MeterProvider` with an `InMemoryMetricReader`. Unconfigured, the global provider does not record
anything.

## Metrics

Attribute keys carry the `horde.` prefix. Every attribute is bounded: call types, outcomes, wake sources, HTTP
statuses, error codes, booleans and the model id. The model id comes from a delivered completion, so it is a model a
worker served. Client addresses, API keys, prompts, client titles and request ids are never attributes.

| Name | Type | Attributes | Meaning |
| --- | --- | --- | --- |
| `horde.oai.requests.admitted` | counter | `streamed` | Chat requests that took an in-flight slot. |
| `horde.oai.requests.refused` | counter | `status_code`, `code` | Chat requests refused before an in-flight slot: body size (413), body read timeout (408 `RequestBodyTimeout`), validation (400, 404), authorization (401), `FrontAtCapacity` (503). |
| `horde.oai.requests.errors` | counter | `status_code`, `code`, `streamed` | Admitted requests answered with an error status before any header: horde refusals at submit, 408 `ChatRequestTimeout`, 404 `no_worker_supports_features`, 503 `no_eligible_worker`, 502 `ChatRequestFaulted` and `HordeUnavailable`. |
| `horde.oai.stream.failures` | counter | `code` | Streamed requests whose 200 headers were sent and which ended in an SSE error event. |
| `horde.oai.inflight` | observable gauge | none | Requests holding an in-flight slot. |
| `horde.oai.inflight.sources` | observable gauge | none | Distinct client addresses holding at least one slot. |
| `horde.oai.inflight.source_max` | observable gauge | none | The most slots one client address holds; compare with `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE`. |
| `horde.oai.request.body_bytes` | histogram, bytes | none | Size of each request body received whole; oversized bodies are counted as refusals instead. |
| `horde.oai.native.duration` | histogram, seconds | `call_type`, `outcome` | Latency of each native call from taking a call slot to the answer. The histogram count is the call count. `call_type` is `submit`, `recovery`, `status`, `cancel` or `models`; `outcome` is `ok`, `refused` (horde status 400 or above), `unreachable` or `abandoned` (the request's budget ended first). |
| `horde.oai.queue_wait` | histogram, seconds | `model` | The horde's `queue_wait_seconds` of each delivered generation: request creation to assignment. |
| `horde.oai.first_content.duration` | histogram, seconds | `streamed` | From the submit acknowledgement, when a stream's headers go out, to the first content sent: the first completion chunk, or the non-streamed body. |
| `horde.oai.request.duration` | histogram, seconds | `streamed`, `status_code`, `code` | From an admitted request's arrival to its end. A stream records 200 whatever its outcome; `code` is the error code, `ok` for a delivered completion or `client_closed` for a client that disconnected before delivery. |
| `horde.oai.wakes` | counter | `wake_source`, `found_finished` | Each status read of a waiting request, by what caused it: `immediate` (the read after the submit), `signal`, `recheck`, `poll`, `deadline`; and `disconnect`, recorded without a read when the client disconnects. |
| `horde.oai.submit.outcome_unknown` | counter | none | Requests answered with `submit_outcome: "unknown"`. |
| `horde.oai.submit.recoveries` | counter | `existing_claim` | Lost submits recovered by re-sending; `existing_claim` true is a claim match (horde answered 200). |
| `horde.oai.client_retry_hints` | counter | none | Chat requests carrying `x-stainless-retry-count` above zero. |
| `horde.oai.completions` | counter | `model`, `estimated_usage` | Delivered completions; `estimated_usage` true when a count was estimated because the worker did not report it. |
| `horde.oai.completions.empty` | counter | `model` | Delivered completions with a generation holding no visible text, the case `x_horde.notice` reports. |
| `horde.oai.cancels` | counter | `outcome` | Cancels sent to the horde: `acknowledged`, `failed`, or `timed_out` past `HORDE_OAI_CANCEL_TIMEOUT_SECONDS`. A failed or timed-out cancel is sent once more by the shutdown drain and counted again; at most `horde.oai.app.MAX_FAILED_CANCELS` answered requests are held for that, oldest dropped first. |

The front does not measure settlement lag between delivery and the native terminal state, since it does not observe
the horde's settlement.

Bucket boundaries: native call latency uses the horde's `BUCKETS_SECONDS`; queue wait, first content and request
duration use `BUCKETS_REQUEST_LIFECYCLE_SECONDS` (both in `horde/metrics.py`); body size uses
`horde.oai.metrics.BUCKETS_BODY_BYTES`.

## Finished-request log line

Every admitted request logs one Loguru `INFO` record when it ends. The message is

```text
Chat request <id> finished: streamed=<bool> submitted_at=<unix> woke_at=<unix|none> woke_by=<source|None> waited=<seconds|none>s status=<status> code=<code> wakes=<count> estimated_usage=<bool|None>
```

and the same values are bound to the record's `extra`, which the OpenTelemetry log bridge exports as attributes. A
request refused at submit has no id; its message starts `Unsubmitted chat request finished:`.
`tests/stress/chat_scale_demo.py` parses the message up to `waited`.

| Field | Meaning |
| --- | --- |
| `request_id` | The horde request id, or null before the submit was acknowledged. |
| `streamed` | Whether the request was streamed. |
| `submitted_at` | Wall-clock time the submit was acknowledged, Unix seconds. |
| `woke_at` | Wall-clock time of the status read that found the request finished; null when none did. |
| `woke_by` | The wake source of that read, or `disconnect`; null for a timeout, fail-fast or horde error. |
| `waited_seconds` | `woke_at` minus `submitted_at`, or the time until the end when the request was not found finished. |
| `status_code` | The HTTP status sent: 200 for every stream whose headers went out, 499 for a non-streamed client that disconnected. |
| `code` | The error code, `ok` or `client_closed`. |
| `wake_count` | How many status reads the wait made. |
| `estimated_usage` | Whether the delivered completion's usage was estimated; null when none was delivered. |

The line does not carry the API key, the messages, the client's title or address, or the operation key.
