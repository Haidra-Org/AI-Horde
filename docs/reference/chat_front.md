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
| Bounded cancels | `horde/oai/app.py` | `_cancel_within_budget`, `_retry_failed_cancels`, `_cancel_open_requests` |
| Per-request timing and finished line | `horde/oai/app.py` | `RequestTrace` |
| Error envelope and SSE error | `horde/oai/app.py` | `ChatFrontError` |
| OpenAI request, response and error models | `horde/oai/schema.py` | `ChatCompletionRequest`, `ChatCompletionResponse`, `ErrorMetadata` |
| Refused and no-op parameters | `horde/oai/schema.py` | `find_refused_parameter`, `drop_default_no_ops` |
| Native body, completion, usage estimate | `horde/oai/mapping.py` | `build_native_chat_body`, `build_completion`, `estimate_tokens` |
| Model list entries | `horde/oai/mapping.py` | `build_model_card` |
| Streamed chunks and keepalive | `horde/oai/sse.py` | `build_completion_events`, `build_delta_events`, `build_keepalive_event`, `build_error_events` |
| Delta relay and reconciliation | `horde/oai/app.py`, `horde/oai/mapping.py` | `DeltaRelay`, `_relay_deltas`, `relays_deltas` |
| `Client-Agent` attribution | `horde/oai/attribution.py` | `build_client_agent` |
| Native HTTP client and root rotation | `horde/oai/horde_client.py` | `HordeClient`, `create_http_client`, `cancel_max_connections` |
| Completion signal subscription and liveness ping | `horde/oai/waiter.py` | `CompletionSignalListener` |
| Delta stream reader | `horde/oai/waiter.py` | `DeltaStreamReader`, `DeltaSubscription`, `DeltaEntry` |
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
| `model`, `models`, `messages`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`, `reasoning_effort`, `n`, `horde` | Validated and forwarded; null counts as omitted. `reasoning_effort` is one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` | Validated against the `horde/chat_contract.py` shapes and forwarded verbatim; a `json_schema` response format is forwarded with its `schema` key and needs one. Dropped at their default: empty or null `tools`, `{"type": "text"}`; a null `tool_choice` or `parallel_tool_calls` is not forwarded. |
| `stream`, `stream_options`, `user`, `metadata` | Applied or accepted by the front and not forwarded, except `stream: true` on a request whose deltas are relayed (see [Relayed deltas](#relayed-deltas)). |
| `logprobs`, `top_logprobs`, `store`, `service_tier` | Dropped at their default (`false`, `0`, `false`, `auto` or `default`); any other value is 400. |

A request sends exactly one of `model`, the OpenAI key, and `models`, a list of 1 to `MAX_CHAT_MODEL_COUNT` (5)
acceptable models, each non-empty and listed once, forwarded unchanged. Whichever listed model an online worker runs
serves the request. Both keys are 400 `ChatRequestConflict` with `param` `models`; neither is 400 `BadRequest` with
`param` `model`; a repeated entry is 400 `BadRequest` at its path, such as `models[2]`. More than 5 entries is 400
`UnsupportedChatParameter` with `param` `models`, and an empty entry is the same at its path, as `tools` over its cap is
refused as a value outside the service contract. A model id containing `:` is
404 `model_not_found` with `param` `model`, or the entry's path such as `models[1]`, and refuses the whole request.
The Horde refuses a model it does not know with 404 `UnknownChatModel`; for an entry of `models` its message starts
with the entry's quoted path, which the front returns as `param` (`NATIVE_REQUEST_PATH_PATTERN`).

`ChatCompletionRequest` bounds the sampling values to OpenAI's ranges (the SDK's `OPENAI_SAMPLING_RANGES`): `temperature` 0 to
2, `top_p` 0 to 1, `frequency_penalty` and `presence_penalty` -2 to 2. A value outside its range, `n` or a token limit
below 1, and more than four stop sequences are 400 `UnsupportedChatParameter` with OpenAI's message and `param`; an
empty `messages` is 400 `BadRequest` with OpenAI's message. Such a message holds the parameter itself, so the front
does not prefix it with the path. A token limit from 1 to 15 is refused with the Horde floor (the SDK's `MIN_CHAT_MAX_TOKENS`).

A native payload validation failure reaching the front carries a generic message and does not carry an `rc`; the front returns 400
`BadRequest` with the first failing field as `param`, translated from the native text param key where it differs
(`NATIVE_FIELD_PARAMETERS` in `horde/oai/app.py`: `max_length` is `max_tokens`, `stop_sequence` is `stop`).

After validation the front applies the request rules it shares with the native route from `horde/chat_contract.py`,
so the client receives OpenAI's message and `param`: a `tool_choice` or `parallel_tool_calls` without tools
(`ChatRequestConflict`), a tool history breaking OpenAI's rule and a `strict` `json_schema` breaking OpenAI's
structural rules (`UnsupportedChatParameter`), and, under `horde.openai_guards`, a `json_object` request without the
word "json" in its messages (`ChatRequestConflict`, `param` `messages`). A tool `parameters` or response schema nested
deeper than the SDK's `MAX_CHAT_SCHEMA_NESTING` fails validation as `UnsupportedChatParameter` at its path. The native route
repeats these checks and also checks a forced function the tools do not define, the tool count and tool spec size (the SDK's
`MAX_CHAT_TOOL_COUNT` and `MAX_CHAT_TOOL_SPEC_BYTES`) and the tool call bounds in `MAX_CHAT_TOOL_CALL_*`.
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
calls. `x_horde.context_adjustments` is the native status's list, passed through unchanged: a `grow` entry when the
context grew and a `truncate_oldest` entry with `dropped_messages` when the oldest turns were dropped (see
[Context adjustments](chat_requests.md#context-adjustments)); it is left out when the Horde changed neither. `reasoning_content`, the model's reasoning as DeepSeek and OpenRouter return it, is written only when the
request sets `horde.include_reasoning` and the worker submitted reasoning; it is never part of `content`.
`usage.completion_tokens_details.reasoning_tokens`, OpenAI's shape, is the reasoning count the worker reported,
written whether or not the reasoning is returned and left out when no worker reported one. Like the other reported
counts it is informational: the Horde charges and rewards on the reply it measures. The response `model` is the first
generation's model, the one that served the request, which under `models` is whichever listed model a worker ran; the
request's `model`, or its first listed model, stands in only when the generation does not carry a model. `system_fingerprint` is `fp_` and
the first 10 hex characters of the SHA-256 of the first generation's worker
id and model (`build_system_fingerprint` in `horde/oai/mapping.py`), null when the generation has no worker id; every
chunk of a stream carries the same value. A relayed stream computes it from the worker and model of its first delta's
stream entry and the final chunks carry that value; it is null throughout when the entries do not carry a worker.

A stream's headers go out once the first status read has passed the checks in
[No eligible worker](#no-eligible-worker). It then sends, after its keepalive comments, these `chat.completion.chunk`
events:

1. `delta: {"role": "assistant"}`.
2. A reasoning chunk with the whole `delta.reasoning_content`, when the message carries it (only under
   `horde.include_reasoning`).
3. A content chunk with the whole `delta.content`, when the reply has text.
4. A tool-call chunk with `delta.tool_calls`, when the reply has calls, each call whole with its 0-based `index`,
   `id`, `type` and `function`. It does not carry content, because some clients read tool calls only from such chunks.
5. `delta: {}` with `finish_reason`, `native_finish_reason` and `x_horde`.
6. `choices: []` with `usage`, when `stream_options.include_usage` was true.
7. `data: [DONE]`.

### Relayed deltas

`relays_deltas` in `horde/oai/mapping.py` selects a request with `stream: true`, no `tools` and no `response_format`
other than `text`. The horde verifies tool calls and, under strict conformance, structured output only once the reply
is complete, so text relayed before that check could belong to a reply it then refuses. When the front has a Redis
client (`HORDE_OAI_REDIS_URL`), it forwards `stream: true` for such a request, which makes the horde write the
progress lease at pop and set `stream_progress` on the job (see
[Streaming progress](chat_requests.md#streaming-progress)). Every other streamed request, and every request on a
front without Redis or served by a worker that does not post progress, is delivered whole as above.

One `DeltaStreamReader` task per process reads `horde:chat:stream:<request id>` on `HORDE_OAI_STREAM_REDIS_DB` for
every registered request with one `XREAD BLOCK 250`, from the stream's start, and queues each entry on the request.
A read that takes longer than 5 seconds (`DELTA_READ_TIMEOUT_SECONDS`) is abandoned and made again, which covers a
connection a network fault black-holes; the reader does not send a PING. A failed read is made again after 2 seconds
from the last entry read, and the stream retains its entries, so the reader does not skip any.

A stream that is deleted or expires does not produce entries or an error, and a stream recreated with ids below the
last id read, such as after a failover that lost the key, does not produce those entries. Either way the deltas stop:
the request is delivered whole when the front has not sent anything, and otherwise the final reply completes the
stream through the reconciliation below.

The first delta with `seq` 1 pins its `gen`, and the first delta that sends anything sends the role chunk and exposes
the stream. A delta then sends a chunk with `delta.reasoning_content` when it carries reasoning and the request sets
`horde.include_reasoning`, and a chunk with `delta.content` when it carries text; each chunk carries the `model` the
first delta's entry carries and the `system_fingerprint` of that entry's `worker` and `model` (the request's model, or
its first listed model, when the entry omits it). Without `horde.include_reasoning` the reasoning is dropped, and a delta carrying only
reasoning advances the sequence without sending anything, so the text after a reasoning block is still relayed.
Keepalive comments continue between deltas. Reasoning is not part of the reconciliation below; the final reply sends
the part of its reasoning the deltas did not cover, before the rest of the text, when it starts with the relayed
reasoning, and does not send it otherwise. After exposure:

| Event | Result |
| --- | --- |
| Final reply equal to the relayed text | The finish chunk, the usage chunk when asked for, and `[DONE]` |
| Final reply starting with the relayed text | The rest as one content chunk, then the finish chunk |
| Final reply not starting with the relayed text | SSE error event `ChatStreamDiverged`, then `[DONE]` |
| An entry of another `gen`, the pinned `gen`'s `ended` marker (also written when the request is cancelled through the native route), or a `seq` out of order | SSE error event `ChatStreamDiverged`, then `[DONE]`; the front cancels the request |
| The request faults | SSE error event `ChatRequestFaulted`, as for a buffered stream |

A restarted attempt starts the reply over, so it is never spliced into a choice the client holds part of
([ADR 0024](../decisions/0024-stream-chat-deltas-through-a-retained-redis-stream.md)). Before exposure, an `ended`
marker, and a delta read together with its generation's `ended` marker, are dropped; such a marker or a gap in the
pinned `gen` unpins it, and the next attempt's first delta can pin again, or the request is delivered whole. The error event is `{"error": {...}}` with `type` `api_error`, `code` `ChatStreamDiverged` and
`metadata.request_id`, followed by `data: [DONE]`.

The front answers browser preflights and allows any origin on every response (`Access-Control-Allow-Origin: *`),
exposing `Retry-After` and `x-should-retry`, so a web client such as KoboldAI Lite can call it with a bearer key.

The wait treats a native status that is `done`, not `faulted` and without a finished generation as unfinished while
it still reports a job waiting or processing. Otherwise the wait reads it again after `signal_recheck_seconds`, and
when that recheck still shows it, or a `cancelled` signal came for the request, the front cancels the request and
returns 502 `ChatRequestNoGeneration`. The front does not return a completion without a generation.

## Deliberate deviations

| Point | Front | OpenAI |
| --- | --- | --- |
| Token limit floor | `max_tokens` and `max_completion_tokens` from 16, the minimum generation length of the native text API; the front refuses a smaller value and does not raise it | From 1 |
| `param` path into messages | `messages[2].role` | `messages.[2].role`; tool paths match, such as `tools[0].function.parameters` |
| Error `code` | The front's codes (`UnsupportedChatParameter`, `ChatRequestConflict`, `BadRequest`) | Per failure, such as `decimal_above_max_value` |
| `system_fingerprint` | Hash of the serving worker id and model | The backend configuration |
| Streamed tool calls | Each call whole in one chunk | Arguments in fragments |
| First streamed chunk | `delta` holds only `role` | `delta` also holds `content` |
| Delta granularity | One chunk per worker progress post, about one a second, for plain text requests only; others whole | One chunk per token or few tokens |
| `json_object` without "json" in the messages | Served, unless `horde.openai_guards` is set | Refused |
| Reasoning text | Hidden; `horde.include_reasoning` returns it as `message.reasoning_content` and `delta.reasoning_content` | Not returned; `usage.completion_tokens_details.reasoning_tokens` only |

## No eligible worker

While the native status reports `is_possible` false with nothing processing, the front waits `fail_fast_grace_seconds`
for a worker to appear, then cancels the request and returns 503 `no_eligible_worker` with `Retry-After: 30`. When
the status carries an `impossible_reason` of kind `declaration_gap` (see
[Impossible reason](chat_requests.md#impossible-reason)), it cancels at once and returns 404
`no_worker_supports_features`, OpenRouter's status for no endpoint supporting the requested features, without
a `Retry-After`. The message lists the missing declarations or the exceeded limit, and adds that guarantees are not
required under `best_effort` when a guarantee is missing.

A gap of only `max_length` or `max_context_length` in `exceeded_limits` is a request over the size limits every
online worker serving the request's models declares. The front returns 400 `invalid_request_error` with code
`no_worker_supports_limit` instead, with `param` `max_tokens`, or `horde.max_context_length` when only the context
length is exceeded, and without a `Retry-After`. Each exceeded limit reads "No worker eligible for this request currently
declares a completion limit that high; the largest any online worker for the model declares now is 1024 tokens." (or
"a context length"), with the value from the model list's `chat` entry. Under `models` any listed model may serve the
request, so the value is the largest across the listed models' entries and the sentence reads "for the listed models";
when the model list does not hold an entry for any requested model, the sentence states the
limit name in its place. Volunteer workers come and go, so the message describes the online workers at the time and
leaves retrying to the client. Any other gap, including a size limit beside another cause, keeps the 404.

A request with `horde.availability_mode: "fail_fast"` is also decided at its first successful status read, with
nothing processing or finished. Zero `eligible_worker_threads` cancels it at once and returns 503
`queue_unavailable` with `Retry-After: 30`; a `wait_time` over the time left in the budget
(`nonstream_timeout_seconds` or `stream_timeout_seconds`) does the same with `Retry-After` set to the `wait_time`, capped at 600 s
(`QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS`), since the pool changes minute to minute.
`param` is `horde.availability_mode`, and the message states the eligible worker threads, the estimated wait and the
budget left at that read. A declaration gap on the same read keeps its answer, and a status without
`eligible_worker_threads`, from a Horde that does not report it, does not refuse on threads. A request just submitted
is absent from the Horde's cached queue positions, which refresh about every 2 s, so its first status can read
`queue_position` 0 and `wait_time` 0 whatever is queued ahead. Under `fail_fast` the wait rule therefore decides on
the first status that carries queue information (a `queue_position` or `wait_time` above zero, or a generation
processing or finished); until one arrives the front reads again every `signal_recheck_seconds`, for at most
`POSITIONED_READ_WAIT_SECONDS` (2 s, `horde/oai/waiter.py`) after the first read, and then the latest reading decides.
With zero threads the request does not need a position and is refused on the first read. Later reads follow the grace as under `wait`. The native `wait_time` counts the queued requests ahead that the request's eligible workers could
take, by model overlap, and divides that work by the throughput of its `eligible_worker_threads`. It approximates the
workers' candidate queues, so a request may still wait longer or start sooner than estimated (see [Queue position and wait time](../explanation/request_feasibility_and_queue_pressure.md#queue-position-and-wait-time)).

A streamed request's response opens only after that first-read decision, so a refusal at it (a declaration or limit
gap, `queue_unavailable`, or a status error such as 404) has the same HTTP status, body and `Retry-After` as on a
non-streamed request, and the front cancels the request the same way. An error after the stream's 200 headers, such as
`no_eligible_worker` after the grace, is the SSE error event; its `metadata.retry_after` carries the interval the
header would.
An `impossible_reason` of a kind the front does not know is ignored, so the grace applies.

## Kudos cap

A request carrying `horde.max_kudos` is forwarded with it, and the native route refuses it before creating it when
the kudos estimate exceeds the cap (`ChatKudosCapExceeded`, see [Request body](chat_requests.md#request-body)).
`_kudos_cap_error` in `horde/oai/app.py` returns 400 `invalid_request_error` with code `max_kudos_exceeded`, `param`
`horde.max_kudos`, the native message, and `metadata.kudos_estimate` and `metadata.max_kudos`. The native error body
holds only the message and the `rc`, so both values are read from the message with `KUDOS_CAP_EXCESS_PATTERN` in
`horde/chat_contract.py`, the module that writes it; a message that does not match leaves them out of `metadata`.

## Model card fields

`GET /v1/models` lists each text model with at least one online chat worker. `context_length` and
`top_provider.max_completion_tokens` are the largest the workers declare; `architecture.input_modalities` adds `image`
for `vision`. `supported_parameters` is `max_tokens`, `temperature`, `top_p` and `stop`, then `seed`,
`frequency_penalty`, `presence_penalty` and `reasoning_effort` for the features with the same values, `tools` and
`parallel_tool_calls` for `tools`, with `tool_choice` added under the `tool_choice_enforced` guarantee, and
`response_format` for `json_object` or `json_schema`. `x_horde` carries the worker count and the queued work.

## Telemetry configuration

`configure_telemetry` runs before the application is built. It configures Logfire when
`horde.telemetry.telemetry_enabled` is true and `OTEL_SDK_DISABLED` is not `true`, with the same variables, resource
attributes and sampling as the Flask application, the service name `horde-oai` unless `OTEL_SERVICE_NAME` sets
another, and the views from `horde.oai.metrics.histogram_views`. It bridges Loguru records to OpenTelemetry logs and
instruments httpx, so each call to the horde is a client span whose `traceparent` the horde's Flask instrumentation
continues. The Flask application's `init_telemetry_early` is not reused because it instruments a Flask app.

With `PYROSCOPE_ENABLED=true`, `configure_telemetry` starts Pyroscope through `horde.telemetry.init_pyroscope`, the
function the Flask application uses, with the same `PYROSCOPE_*` variables and the front's service name as the
application name. It starts also when `OTEL_SDK_DISABLED=true`; span profiles need the SDK.

`instrument_app` adds the server spans (`POST /v1/chat/completions`, `GET /v1/models`) once `create_app` has built
the application; `main` calls it only when telemetry was configured. `/healthz` is excluded, since the listener and
the container poll it every few seconds. The instrumentation does not capture headers or bodies. A streamed request's span lasts
until the stream ends.

## Spans

Besides the server and client spans, the front records these spans. Attribute keys carry the `horde.` prefix.

| Name | Attributes | Meaning |
| --- | --- | --- |
| `horde.oai.delta_read` | `entry_count`, `byte_count`, `first_entry_lag_seconds` | One delta stream read that returned entries, across every registered request's stream; an empty read does not record a span. `entry_count` is the entries returned, `byte_count` the UTF-8 size of their field names and values, and `first_entry_lag_seconds` the time from the oldest returned entry's append, the millisecond part of its stream id on the Redis server's clock, to the read's return. The reader is one background task, so the span is a root span and not part of a request's trace. |

`create_app` takes the meter from the global meter provider unless one is passed as `meter`; tests pass a meter of
a separate `MeterProvider` with an `InMemoryMetricReader`. Unconfigured, the global provider does not record
anything.

## Metrics

Attribute keys carry the `horde.` prefix. Every attribute is bounded: call types, outcomes, wake sources, HTTP
statuses, error codes, refusal reasons, booleans and the model id. The model id comes from a delivered completion, so it is a model a
worker served. Client addresses, API keys, prompts, client titles and request ids are never attributes.

| Name | Type | Attributes | Meaning |
| --- | --- | --- | --- |
| `horde.oai.requests.admitted` | counter | `streamed` | Chat requests that took an in-flight slot. |
| `horde.oai.requests.refused` | counter | `status_code`, `code` | Chat requests refused before an in-flight slot: body size (413), body read timeout (408 `RequestBodyTimeout`), validation (400, 404), authorization (401), `FrontAtCapacity` (503). |
| `horde.oai.requests.errors` | counter | `status_code`, `code`, `streamed`, `reason` | Admitted requests answered with an error status before any header: horde refusals at submit (429 `rate_limit_exceeded` when the horde's 429 does not carry an `rc`), 408 `ChatRequestTimeout`, 404 `no_worker_supports_features`, 400 `no_worker_supports_limit`, 503 `no_eligible_worker`, 503 `queue_unavailable`, 502 `ChatRequestFaulted`, `ChatRequestNoGeneration` and `HordeUnavailable`. `reason` is `no_eligible_threads` or `wait_exceeds_budget` for `queue_unavailable` and empty for every other code. |
| `horde.oai.stream.failures` | counter | `code` | Streamed requests whose 200 headers were sent and which ended in an SSE error event. |
| `horde.oai.inflight` | observable gauge | none | Requests holding an in-flight slot. |
| `horde.oai.inflight.sources` | observable gauge | none | Distinct client addresses holding at least one slot. |
| `horde.oai.inflight.source_max` | observable gauge | none | The most slots one client address holds; compare with `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE`. |
| `horde.oai.request.body_bytes` | histogram, bytes | none | Size of each request body received whole; oversized bodies are counted as refusals instead. |
| `horde.oai.native.duration` | histogram, seconds | `call_type`, `outcome` | Latency of each native call from taking a call slot to the answer. The histogram count is the call count. `call_type` is `submit`, `recovery`, `status`, `cancel` or `models`; `outcome` is `ok`, `refused` (horde status 400 or above), `unreachable` or `abandoned` (the request's budget ended first). |
| `horde.oai.queue_wait` | histogram, seconds | `model` | The horde's `queue_wait_seconds` of each delivered generation: request creation to assignment. |
| `horde.oai.first_content.duration` | histogram, seconds | `streamed`, `relayed` | From the submit acknowledgement, when a stream's headers go out, to the first content sent: the first relayed delta (`relayed` true), the first completion chunk, or the non-streamed body. |
| `horde.oai.request.duration` | histogram, seconds | `streamed`, `status_code`, `code` | From an admitted request's arrival to its end. A stream records 200 whatever its outcome; `code` is the error code, `ok` for a delivered completion or `client_closed` for a client that disconnected before delivery. |
| `horde.oai.wakes` | counter | `wake_source`, `found_finished`, `found_done_without_generation` | Each status read of a waiting request, by what caused it: `immediate` (the read after the submit), `signal`, `recheck`, `poll`, `deadline`; and `disconnect`, recorded without a read when the client disconnects. `found_done_without_generation` marks a read showing the request done, not faulted and without a generation. |
| `horde.oai.submit.outcome_unknown` | counter | none | Requests answered with `submit_outcome: "unknown"`. |
| `horde.oai.submit.recoveries` | counter | `existing_claim` | Lost submits recovered by re-sending; `existing_claim` true is a claim match (horde answered 200). |
| `horde.oai.client_retry_hints` | counter | none | Chat requests carrying `x-stainless-retry-count` above zero. |
| `horde.oai.completions` | counter | `model`, `estimated_usage` | Delivered completions; `estimated_usage` true when a count was estimated because the worker did not report it. |
| `horde.oai.completions.empty` | counter | `model` | Delivered completions whose generation does not hold visible text, the case `x_horde.notice` reports. |
| `horde.oai.stream.relayed` | counter | none | Streamed requests that sent at least one relayed delta. |
| `horde.oai.stream.relay_outcomes` | counter | `outcome` | How relayed streams ended: `equal`, `prefix`, `mismatch`, `replaced_attempt`, `ended` or `gap`; see [Relayed deltas](#relayed-deltas). |
| `horde.oai.cancels` | counter | `outcome` | Cancels sent to the horde: `acknowledged`, `failed`, `timed_out` when the HTTP call took longer than `HORDE_OAI_CANCEL_TIMEOUT_SECONDS`, `not_started` when no cancel slot became free within that time and the cancel was not sent, or `already_gone` when the horde answered 404 because the request no longer exists. A cancel that ended `failed`, `timed_out` or `not_started` is sent again every `HORDE_OAI_CANCEL_RETRY_SECONDS` and once more by the shutdown drain, each attempt counted; a request is dropped from the retries 20 minutes after its submit (`horde.oai.app.HORDE_CHAT_REQUEST_EXPIRY_SECONDS`), and at most `horde.oai.app.MAX_FAILED_CANCELS` answered requests are held, oldest dropped first. |

The front does not measure settlement lag between delivery and the native terminal state, since it does not observe
the horde's settlement.

Bucket boundaries: native call latency uses the horde's `BUCKETS_SECONDS`; queue wait, first content and request
duration use `BUCKETS_REQUEST_LIFECYCLE_SECONDS` (both in `horde/metrics.py`); body size uses
`horde.oai.metrics.BUCKETS_BODY_BYTES`.

## Finished-request log line

Every admitted request logs one Loguru `INFO` record when it ends. The message is

```text
Chat request <id> finished: streamed=<bool> submitted_at=<unix> woke_at=<unix|none> woke_by=<source|None> waited=<seconds|none>s status=<status> code=<code> wakes=<count> estimated_usage=<bool|None> done_without_generation_reads=<count> requested_model=<models> served_model=<model|none> retry_after=<seconds|none>
```

A `queue_unavailable` refusal appends
`queue_reason=<reason> wait_time=<seconds|none> eligible_worker_threads=<count|none>`. Every line then ends with
`first_read_wait_time=<seconds|none> first_read_eligible_worker_threads=<count|none>`.

The same values are bound to the record's `extra`, which the OpenTelemetry log bridge exports as attributes. A
request refused at submit does not have an id; its message starts `Unsubmitted chat request finished:`.
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
| `done_without_generation_reads` | How many status reads showed the request done, not faulted and without a generation. |
| `requested_model` | The models the request accepts, comma-separated. |
| `served_model` | The model of the delivered completion; null when none was delivered. |
| `retry_after` | The `Retry-After` the error response or event carried, in seconds; null when it carried none. |
| `queue_reason` | `queue_unavailable` only: `no_eligible_threads` or `wait_exceeds_budget`. |
| `wait_time` | `queue_unavailable` only: the wait estimate the deciding status read reported; null when it had none. |
| `eligible_worker_threads` | `queue_unavailable` only: the eligible worker threads that read reported; null when it had none. |
| `first_read_wait_time` | `fail_fast` only: the wait estimate of the status read that took the first-read decision, whether it admitted or refused the request; null in the `wait` mode, before a decision, or when the read had none. |
| `first_read_eligible_worker_threads` | `fail_fast` only: the eligible worker threads that read reported; null as for `first_read_wait_time`. |

The line does not carry the API key, the messages, the client's title or address, or the operation key.
