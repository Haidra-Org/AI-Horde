---
title: "Chat front reference"
summary: "OpenAI-compatible request and response formats, stream behavior, availability errors and telemetry for the horde.oai service."
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

## API documentation

The front serves its OpenAPI description. `/openapi.json` is the OpenAPI 3.1 document, `/docs` renders it with
Swagger UI and `/redoc` with ReDoc, and `/` redirects to `/docs`. `/healthz` is left out of the description.

### Schema generation

The route handlers read and validate the body by hand, so FastAPI does not derive any schema from them.
`horde/oai/openapi.py` generates the component schemas from `ChatCompletionRequest`, as validated, and from
`ChatCompletionResponse`, `ChatCompletionChunk`, `OpenAIErrorEnvelope`, `ModelList` and `ModelCard`, as serialized.
It also supplies each route's request body, responses by status with their error codes, and the `x-should-retry`
and `Retry-After` headers. The document describes the handlers and does not change how they behave.
The document adjusts the generated schemas where the handlers differ from the models:

- The request schema lists the keys of `DEFAULT_NO_OP_PARAMETERS` that are not request fields, from
  `NO_OP_PARAMETER_SCHEMAS`, each accepted only at its default.
- The schemas of the objects whose unknown keys are dropped by `drop_unknown_nested_keys` (`KEY_DROPPING_MODELS`)
  allow other keys. The top level, `stream_options`, `response_format` and `horde` do not.
- FastAPI's 422 response is removed, since the front does not return it.

### Field descriptions

Each field's description is the docstring of the model field in `horde/oai/schema.py` or `horde/chat_contract.py`,
so those docstrings are public text for API users, and they state where the front differs from OpenAI. In the
document, `plain_description` drops a parenthetical ``(see [`name`][path])`` cross-reference and renders an inline
``[`name`][path]`` one as `` `name` ``. A detail needed only by maintainers goes in a comment above the field. Models
serialized through a wrap `model_serializer` that returns `dict[str, object]` are documented by their fields
(`DocumentedJsonSchema`). The description links to the published documentation site (`DOCS_URL`).

## Request keys

Example: [Front request](chat_wire_examples.md#front-request).

The front validates the body against `ChatCompletionRequest` after screening it in `horde/oai/schema.py`.
A body that is not JSON, or contains `NaN`, `Infinity` or `-Infinity`, returns 400 `BadRequest` before screening.

### Parameter screening

Screening runs in this order:

1. `find_refused_parameter` rejects unknown top-level keys, non-default values in `DEFAULT_NO_OP_PARAMETERS`,
   unsupported roles and unsupported content part types. The error includes the parameter's path.
2. `drop_default_no_ops` removes keys from that table when their values equal the defaults.
3. `drop_unknown_nested_keys` removes unknown keys inside messages, tool definitions and `tool_choice`.

The retained keys inside messages are listed under [Message roles](#message-roles). A `tools` entry retains
`type` and `function`. The function retains `name`, `description`, `parameters` and `strict`, with the
`parameters` schema forwarded unchanged. A `tool_choice` object retains `type` and `function`, and its
function retains `name`.

Other objects and the top level reject unknown keys. For example, an unknown key in `stream_options` or
`response_format` returns 400 at its path. `response_format.json_schema` requires the key `schema`.
The Python field name `schema_` is treated as an unknown key.

| Keys | Handling |
| --- | --- |
| `model`, `models`, `messages`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`, `reasoning_effort`, `n`, `horde` | Validated and forwarded. Null counts as omitted. `reasoning_effort` is one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. `max_tokens` covers the reasoning and the answer together. |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` | Validated against the `horde/chat_contract.py` shapes and forwarded verbatim. A `json_schema` response format is forwarded with its `schema` key and needs one. Dropped at their default: empty or null `tools`, `{"type": "text"}`. A null `tool_choice` or `parallel_tool_calls` is not forwarded. |
| `stream`, `stream_options`, `user`, `metadata` | Applied or accepted by the front and not forwarded, except `stream: true` on a request whose deltas are relayed (see [Relayed deltas](#relayed-deltas)). |
| `logprobs`, `top_logprobs`, `store`, `service_tier` | Dropped at their default (`false`, `0`, `false`, `auto` or `default`). Any other value is 400. |

### Model selection

Send exactly one of `model` and `models`. The OpenAI key `model` selects one model. `models` accepts
1 to `MAX_CHAT_MODEL_COUNT` (5) non-empty, distinct model ids and is forwarded unchanged. Any listed model may
serve the request.

| Invalid selection | Status and code | `param` |
| --- | --- | --- |
| Both keys | 400 `ChatRequestConflict` | `models` |
| Neither key | 400 `BadRequest` | `model` |
| Repeated entry | 400 `BadRequest` | Entry path, such as `models[2]` |
| More than 5 entries | 400 `UnsupportedChatParameter` | `models` |
| Empty entry | 400 `UnsupportedChatParameter` | Entry path |
| Model id containing `:` | 404 `model_not_found` | `model` or the entry path |
| Unknown model | 404 `UnknownChatModel` | `model` or the entry path |

An invalid entry rejects the whole request. For an unknown model in `models`, the native message begins with
the quoted entry path. The front extracts that path with `NATIVE_REQUEST_PATH_PATTERN`.

### Value and contract validation

`ChatCompletionRequest` bounds the sampling values to OpenAI's ranges (the SDK's `OPENAI_SAMPLING_RANGES`): `temperature` 0 to
2, `top_p` 0 to 1, `frequency_penalty` and `presence_penalty` -2 to 2. A value outside its range, `n` or a token limit
below 1, and more than four stop sequences are 400 `UnsupportedChatParameter` with OpenAI's message and `param`. An
empty `messages` is 400 `BadRequest` with OpenAI's message. Such a message holds the parameter itself, so the front
does not prefix it with the path. A token limit from 1 to 15 is refused with the Horde floor (the SDK's `MIN_CHAT_MAX_TOKENS`).

A native payload validation failure reaching the front includes a generic message and does not include an `rc`. The front returns 400
`BadRequest` with the first failing field as `param`, translated from the native text param key where it differs
(`NATIVE_FIELD_PARAMETERS` in `horde/oai/app.py`: `max_length` is `max_tokens`, `stop_sequence` is `stop`).

After validation the front applies the request rules it shares with the native route from `horde/chat_contract.py`,
so the client receives OpenAI's message and `param`: a `tool_choice` or `parallel_tool_calls` without tools
(`ChatRequestConflict`), a tool history breaking OpenAI's rule and a `strict` `json_schema` breaking OpenAI's
structural rules (`UnsupportedChatParameter`), and, under `horde.openai_guards`, a `json_object` request without the
word "json" in its messages (`ChatRequestConflict`, `param` `messages`).

A tool `parameters` or response schema nested
deeper than the SDK's `MAX_CHAT_SCHEMA_NESTING` fails validation as `UnsupportedChatParameter` at its path. The native route
repeats these checks and also checks a forced function that is not defined in the tools, the tool count and tool spec size (the SDK's
`MAX_CHAT_TOOL_COUNT` and `MAX_CHAT_TOOL_SPEC_BYTES`) and the tool call bounds in `MAX_CHAT_TOOL_CALL_*`.

The message text limit includes the function names and arguments of history tool calls.

## Message roles

| Role | Keys | Content |
| --- | --- | --- |
| `system`, `developer` | `role`, `content`, `name` | A string or `text` parts. |
| `user` | `role`, `content`, `name` | A string, or `text` and `image_url` parts. |
| `assistant` | `role`, `content`, `name`, `tool_calls` | A string or `text` parts. Null or omitted only with `tool_calls`, and then forwarded as null. |
| `tool` | `role`, `content`, `tool_call_id` (required) | A string or `text` parts. |

A key outside its role's set (`SUPPORTED_MESSAGE_KEYS`) is dropped, including `name` on a `tool` message.
`tool_calls` on a message other than an assistant message is 400 `UnsupportedChatParameter` at
`messages[<i>].tool_calls`, as OpenAI refuses it.

Inside a message, a tool call keeps `id`, `type` and `function`, its
function keeps `name` and `arguments`, a `text` part keeps `type` and `text`, an `image_url` part keeps `type` and
`image_url`, and the `image_url` object keeps `url` and `detail`. Retained keys are still validated.

This removal covers the
fields added by the OpenAI Python SDK to an accumulated streamed reply (`refusal`, `audio`, `annotations`,
`function_call`, `parsed`, `tool_calls[].index`, `function.parsed_arguments`) when it replays that reply as history.

An unsupported role (such as `function`) or content part type (such as `input_audio`) is still 400
`UnsupportedChatParameter`, with `param` such as `messages[0].role` or `messages[0].content[0].type=input_audio`.

## Response shape

Examples: [Completion](chat_wire_examples.md#completion), [Tool-call completion](chat_wire_examples.md#tool-call-completion),
and the streamed [Content chunk](chat_wire_examples.md#content-chunk),
[Finish chunk](chat_wire_examples.md#finish-chunk) and [Usage chunk](chat_wire_examples.md#usage-chunk).

### Assistant message and adjustments

`choices[i].message` has the shape `{"role": "assistant", "content": ..., "tool_calls": [...]}`.
`content` is always present. It is null only when the worker returns tool calls without text.
`tool_calls` is included only when the worker returns calls. Each call has
`{"id", "type": "function", "function": {"name", "arguments"}}`, as submitted by the worker.
`finish_reason` is `stop`, `length`, `content_filter` or `tool_calls`.

`x_horde.notice` indicates a reply without visible text or tool calls.
`x_horde.context_adjustments` passes through the native status's list unchanged and is omitted when the Horde
did not adjust the request.

| Adjustment reason | Meaning |
| --- | --- |
| `worker_max_length` | `max_tokens` was lowered to the largest value declared by an online worker. |
| `grow` | The context length increased. |
| `truncate_oldest` | Oldest turns were dropped. `dropped_messages` gives the message count. |

See [Context adjustments](chat_requests.md#context-adjustments) for the native contract.

### Reasoning and usage

`reasoning_content` contains the model's reasoning, as returned by DeepSeek and OpenRouter. It is included when
the worker submits reasoning, unless `horde.include_reasoning` is false. Reasoning is separate from `content`.

`usage.completion_tokens_details.reasoning_tokens` is the worker's reported reasoning count, or an estimate
from the reasoning characters. An estimate sets `x_horde.estimated_usage`. The count is returned even when the
reasoning text is hidden. It is omitted when the reply does not have reasoning and the worker did not report a count.

Estimated `usage.completion_tokens` includes reasoning, reply text and tool calls. Each generation's estimated
completion and reasoning counts are capped at the effective completion limit, selected in this order:

1. The lowered value in a `worker_max_length` adjustment.
2. `max_completion_tokens`.
3. `max_tokens`.
4. The native default of 512 (`DEFAULT_CHAT_MAX_TOKENS`).

`effective_completion_limit` in `horde/oai/mapping.py` implements this selection. Reported counts are used
unchanged. All token counts are informational. The Horde charges and rewards on the measured reply, including
the reasoning characters.

### Model and fingerprint

The response's `model` is the first generation's model. With a `models` request, this is the listed model
that served the request. If the generation does not include a model, the response uses the request's `model`
or the first entry of `models`.

`system_fingerprint` is `fp_` followed by the first 10 hex characters of the SHA-256 digest of the first
generation's worker id and model. It is null when the generation does not have a worker id.
`build_system_fingerprint` in `horde/oai/mapping.py` computes it.

Every stream chunk uses the same fingerprint. A relayed stream derives it from the first delta's worker and model,
and uses it for the final chunks too. It stays null when the stream entries do not include a worker.

### Buffered stream events

A stream's headers go out once the first status read has passed the checks in
[No eligible worker](#no-eligible-worker). It then sends, after its keepalive comments, these `chat.completion.chunk`
events:

1. `delta: {"role": "assistant"}`.
2. A reasoning chunk with the whole `delta.reasoning_content`, when the message has it (left out when
   `horde.include_reasoning` is false).
3. A content chunk with the whole `delta.content`, when the reply has text.
4. A tool-call chunk with `delta.tool_calls`, when the reply has calls, each call whole with its 0-based `index`,
   `id`, `type` and `function`. It does not include content, because some clients read tool calls only from such chunks.
5. `delta: {}` with `finish_reason`, `native_finish_reason` and `x_horde`.
6. `choices: []` with `usage`, when `stream_options.include_usage` was true.
7. `data: [DONE]`.

### Relayed deltas

`relays_deltas` in `horde/oai/mapping.py` selects a request with `stream: true`, no `tools` and no `response_format`
other than `text`. The horde verifies tool calls and, under strict conformance, structured output only once the reply
is complete, so relaying text before verification could expose a reply that is later refused.

When the front has a Redis
client (`HORDE_OAI_REDIS_URL`), it forwards `stream: true` for such a request, which makes the horde write the
progress lease at pop and set `stream_progress` on the job (see
[Streaming progress](chat_requests.md#streaming-progress)). Every other streamed request, and every request on a
front without Redis or served by a worker that does not post progress, is delivered whole as above.

#### Redis reads and recovery

One `DeltaStreamReader` task per process reads `horde:chat:stream:<request id>` on `HORDE_OAI_STREAM_REDIS_DB` for
every registered request with one `XREAD BLOCK 250`, from the stream's start, and queues each entry on the request.
A read that takes longer than 5 seconds (`DELTA_READ_TIMEOUT_SECONDS`) is abandoned and made again, which covers a
connection left unresponsive by a network fault. The reader does not send a PING. A failed read is made again after 2 seconds
from the last entry read, and the stream retains its entries, so the reader does not skip any.

A stream that is deleted or expires does not produce entries or an error, and a stream recreated with ids below the
last id read, such as after a failover that lost the key, does not produce those entries. Either way the deltas stop:
the request is delivered whole when the front has not sent anything, and otherwise the final reply completes the
stream through the reconciliation below.

#### Delta fields

The first delta with `seq` 1 pins the generation id (`gen`). The first delta with content to send emits
the role chunk and exposes the stream to the client.

Each delta emits a `delta.reasoning_content` chunk when it includes reasoning, followed by a `delta.content`
chunk when it includes text. With `horde.include_reasoning: false`, reasoning is discarded. A reasoning-only
delta still advances the sequence, so later text is relayed.

Each chunk uses the first delta's `model` and the fingerprint derived from that entry's `worker` and `model`.
If the entry omits the model, the request's `model` or first listed model is used.
Keepalive comments continue between deltas.

#### Pacing

The first exposed delta is sent whole when it arrives. The text and reasoning of each later delta are paced. They are
split into pieces, and the pieces are sent evenly over a window. The window is the estimated interval between the
generation's posts. The estimate is an exponential moving average of the gaps between the posts' stream id times,
with a weight of 0.5 on the newest gap. It is kept between 0.05 seconds and
`HORDE_OAI_DELTA_PACING_MAX_WINDOW_SECONDS`.

A window holds at most `HORDE_OAI_DELTA_PACING_MAX_CHUNKS_PER_SECOND`
pieces per second, and the piece count does not exceed the character count. Within one delta, every reasoning piece
is sent before the first content piece. The piece count is divided between reasoning and content by length, with at
least one piece for each field that has text.

Each cut is at whitespace within half a piece of an even offset. Without such whitespace, the cut is at the next offset
that does not split a visible character. A cut does not separate a character from a following Unicode mark (such as
an accent, vowel sign or keycap), variation selector or skin-tone modifier. A cut does not split a zero-width joiner
sequence or a flag.

A delta that arrives while pieces are pending is merged with them. The merged text is spread over a new window that
starts at the delta's arrival, so pacing adds at most about one window of latency. When the request finishes or the
relay breaks, the pending pieces are sent at once, before the final reply's reconciliation. When the client
disconnects, the pending pieces are dropped. A maximum window of 0 sends each delta as it arrives.

#### Final reply reconciliation

The front reconciles reasoning separately from text. If the final reasoning starts with the relayed reasoning,
the front sends the remaining reasoning before the remaining text. Otherwise it does not send further reasoning.

After the stream is exposed, text reconciliation follows these rules:

| Event | Result |
| --- | --- |
| Final reply equal to the relayed text | The finish chunk, the usage chunk when asked for, and `[DONE]` |
| Final reply starting with the relayed text | The rest as one content chunk, then the finish chunk |
| Final reply not starting with the relayed text | SSE error event `ChatStreamDiverged`, then `[DONE]` |
| An entry of another `gen`, the pinned `gen`'s `ended` marker (also written when the request is cancelled through the native route), or a `seq` out of order | SSE error event `ChatStreamDiverged`, then `[DONE]`. The front cancels the request |
| The request faults | SSE error event `ChatRequestFaulted`, as for a buffered stream |

A restarted attempt starts the reply over, so it is not appended to a partially delivered choice ([ADR
0024](../decisions/0024-stream-chat-deltas-through-a-retained-redis-stream.md)). Before exposure, an `ended`
marker, and a delta read together with its generation's `ended` marker, are dropped. Such a marker or a gap in
the pinned `gen` unpins it, and the next attempt's first delta can pin again, or the request is delivered
whole. The error event is `{"error": {...}}` with `type` `api_error`, `code` `ChatStreamDiverged` and
`metadata.request_id`, followed by `data: [DONE]`.

### Browser access

The front responds to browser preflights and allows any origin on every response (`Access-Control-Allow-Origin: *`),
exposing `Retry-After` and `x-should-retry`, so a web client such as KoboldAI Lite can call it with a bearer key.

### Completion without a generation

The wait treats a native status that is `done`, not `faulted` and without a finished generation as unfinished while
it still reports a job waiting or processing. Otherwise the wait reads it again after `signal_recheck_seconds`, and
when that recheck still shows it, or a `cancelled` signal came for the request, the front cancels the request and
returns 502 `ChatRequestNoGeneration`. The front does not return a completion without a generation.

## Deliberate deviations

| Point | Front | OpenAI |
| --- | --- | --- |
| Token limit floor | `max_tokens` and `max_completion_tokens` from 16, the minimum generation length of the native text API. The front refuses a smaller value and does not raise it | From 1 |
| `param` path into messages | `messages[2].role` | `messages.[2].role`. Tool paths match, such as `tools[0].function.parameters` |
| Error `code` | The front's codes (`UnsupportedChatParameter`, `ChatRequestConflict`, `BadRequest`) | Per failure, such as `decimal_above_max_value` |
| `system_fingerprint` | Hash of the serving worker id and model | The backend configuration |
| Streamed tool calls | Each call whole in one chunk | Arguments in fragments |
| First streamed chunk | `delta` holds only `role` | `delta` also holds `content` |
| Delta granularity | For plain text requests, each worker progress post (about one a second) is paced over the interval until the next post, in up to 20 chunks a second. Other requests get the whole text at once. | One chunk per token or few tokens |
| `json_object` without "json" in the messages | Served, unless `horde.openai_guards` is set | Refused |
| Reasoning text | Returned as `message.reasoning_content` and `delta.reasoning_content`, which the OpenAI SDK keeps in `model_extra`. `horde.include_reasoning: false` hides it. `usage.completion_tokens_details.reasoning_tokens` is the worker's count or an estimate from the reasoning characters | Not returned. `usage.completion_tokens_details.reasoning_tokens` only |

## No eligible worker

### Model availability

The Horde refuses a request at submit when its models are not served by any online chat worker (see
[Known models](chat_requests.md#known-models)). The front then returns 503 `no_eligible_worker` with
`Retry-After: 30` at once. The message is the Horde's, such as "No worker is currently serving the model
'<model>'. Retry later.". The front does not wait for the grace.

The native status of a submitted request can report `is_possible` false without a processing generation. This happens when
the workers serving the model go offline or become busy with other work. The front then waits `fail_fast_grace_seconds`
for a worker to appear, then cancels the request and returns 503 `no_eligible_worker` with `Retry-After: 30`.

### Declaration gaps

When the status includes an `impossible_reason` of kind `declaration_gap` (see
[Impossible reason](chat_requests.md#impossible-reason)), it cancels at once and returns 404
`no_worker_supports_features`, OpenRouter's status for no endpoint supporting the requested features, without
a `Retry-After`. The message lists the missing declarations or the exceeded limit, and adds that guarantees are not
required under `best_effort` when a guarantee is missing. The `type` is `not_found_error`, `param` is null, and
`metadata.request_id` holds the cancelled native request:

```json
--8<-- "docs/reference/chat_wire_examples.md:front-no-worker-supports-features"
```

### Size limit gaps

A gap of only `max_length` or `max_context_length` in `exceeded_limits` is a request over every online worker's
size limits for the request's models. At submit the Horde lowers a `max_tokens` above the largest
declared `max_length` (see [Context adjustments](chat_requests.md#context-adjustments)). A `max_length` gap therefore
arises only when the workers' declarations change after the submit.

The front returns 400 `invalid_request_error` with code
`no_worker_supports_limit` instead, with `param` `max_tokens`, or `horde.max_context_length` when only the context
length is exceeded, and without a `Retry-After`.

Each exceeded limit reads "No worker eligible for this request currently
declares a completion limit that high. The largest any online worker for the model declares now is 1024 tokens." (or
"a context length"), with the value from the model list's `chat` entry.

Under `models` any listed model may serve the
request, so the value is the largest across the listed models' entries and the sentence reads "for the listed models".
when the model list does not hold an entry for any requested model, the sentence states the
limit name in its place. Volunteer workers come and go, so the message describes the online workers at the time and
leaves retrying to the client.

Any other gap, including a size limit beside another cause, keeps the 404.

### Native admission refusals

The Horde also refuses the submit with 403 `NoEligibleWorker` when the request would be refused for kudos while
it cannot be served by any online worker (see [Free output allowance](chat_requests.md#free-output-allowance)). The front
maps it as above: the body's `impossible_reason` gives 404 `no_worker_supports_features` or 400
`no_worker_supports_limit`, and a body without one gives 503 `no_eligible_worker` with `Retry-After: 30` and the
Horde's message. The Horde did not create a request, so these responses do not include a `request_id` and do not
state that the request was cancelled.

The 404 and 400 do not set `x-should-retry: false`. Every `no_eligible_worker`
sets it, since an SDK retries within seconds and the online workers are most likely the same by then. The refusal
comes before any stream header, so a streaming request gets the same
JSON error.

### Fail-fast admission

With `horde.availability_mode: "fail_fast"`, the front checks availability at the first successful status read
when the request does not have a processing or finished generation.

| Condition | Response |
| --- | --- |
| Zero `eligible_worker_threads` | Cancel at once. Return 503 `queue_unavailable` with `Retry-After: 30`. |
| `wait_time` exceeds the remaining request budget | Cancel. Return 503 `queue_unavailable` with `Retry-After` equal to `wait_time`, capped at 600 seconds. |

The budget is `nonstream_timeout_seconds` or `stream_timeout_seconds`.
`QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS` sets the retry cap because the online pool changes minute to minute.
The error's `param` is `horde.availability_mode`. Its message states the eligible worker threads, estimated wait
and remaining budget at that read.

A declaration gap takes precedence over these rules. If an older Horde status does not include
`eligible_worker_threads`, the front does not reject the request based on thread count.

#### Queue position refresh

The Horde refreshes cached queue positions about every 2 seconds. A newly submitted request may therefore have
`queue_position: 0` and `wait_time: 0` even when other requests are ahead.

The wait-time check uses the first status with queue information: a positive `queue_position` or `wait_time`,
or a processing or finished generation. Until then, the front rereads every `signal_recheck_seconds`, for at most
`POSITIONED_READ_WAIT_SECONDS` (2 seconds, `horde/oai/waiter.py`) after the first read.
At that deadline it uses the latest reading. With zero eligible threads, the request does not need a queue position
and is refused on the first read. Later reads use the same grace period as `wait`.

The native `wait_time` counts queued requests ahead with overlapping models that the eligible workers could serve,
then divides that work by the throughput of `eligible_worker_threads`. This approximates the workers' candidate
queues, so a request may start sooner or wait longer. See
[Queue position and wait time](../explanation/request_feasibility_and_queue_pressure.md#queue-position-and-wait-time).

### Stream errors

A streamed request's response opens only after that first-read decision, so a refusal at it (a declaration or limit
gap, `queue_unavailable`, or a status error such as 404) has the same HTTP status, body and `Retry-After` as on a
non-streamed request, and the front cancels the request the same way. An error after the stream's 200 headers, such as
`no_eligible_worker` after the grace, is the SSE error event. Its `metadata.retry_after` includes the interval the
header would.
An unrecognized `impossible_reason` kind is ignored, so the grace applies.

## Kudos cap

A request with `horde.max_kudos` is forwarded with it, and the native route refuses it before creating it when
the kudos estimate exceeds the cap (`ChatKudosCapExceeded`, see [Request body](chat_requests.md#request-body)).
`_kudos_cap_error` in `horde/oai/app.py` returns 400 `invalid_request_error` with code `max_kudos_exceeded`, `param`
`horde.max_kudos`, the native message, and `metadata.rc`, `metadata.kudos_estimate` and `metadata.max_kudos`. The
native error body holds only the message and the `rc`, so both values are read from the message with
`KUDOS_CAP_EXCESS_PATTERN` in `horde/chat_contract.py`, the module that writes it. A message that does not match
leaves them out of `metadata`.

```json
--8<-- "docs/reference/chat_wire_examples.md:front-kudos-cap"
```

## Model card fields

Example: [Model list](chat_wire_examples.md#model-list).

`GET /v1/models` lists each text model with at least one online chat worker. `context_length` and
`top_provider.max_completion_tokens` are the largest declared worker limits. `architecture.input_modalities` adds `image`
for `vision`. `supported_parameters` is `max_tokens`, `temperature`, `top_p` and `stop`, then `seed`,
`frequency_penalty`, `presence_penalty` and `reasoning_effort` for the features with the same values, `tools` and
`parallel_tool_calls` for `tools`, with `tool_choice` added under the `tool_choice_enforced` guarantee, and
`response_format` for `json_object` or `json_schema`. `x_horde` includes the worker count and the queued work.

## Telemetry

### Configuration {#telemetry-configuration}

`configure_telemetry` runs before the application is built. It configures Logfire when
`horde.telemetry.telemetry_enabled` is true and `OTEL_SDK_DISABLED` is not `true`, with the same variables, resource
attributes and sampling as the Flask application, the service name `horde-oai` unless `OTEL_SERVICE_NAME` sets
another, and the views from `horde.oai.metrics.histogram_views`. It bridges Loguru records to OpenTelemetry logs and
instruments httpx, so each call to the horde is a client span whose `traceparent` the horde's Flask instrumentation
continues. The Flask application's `init_telemetry_early` is not reused because it instruments a Flask app.

With `PYROSCOPE_ENABLED=true`, `configure_telemetry` starts Pyroscope through `horde.telemetry.init_pyroscope`, the
function used by the Flask application, with the same `PYROSCOPE_*` variables and the front's service name as the
application name. It also starts when `OTEL_SDK_DISABLED=true`. Span profiles need the SDK.

`instrument_app` adds the server spans (`POST /v1/chat/completions`, `GET /v1/models`) once `create_app` has built
the application. `main` calls it only when telemetry was configured. `/healthz` is excluded, since the listener and
the container poll it every few seconds. The instrumentation does not capture headers or bodies. A streamed request's span lasts
until the stream ends.

### Spans

Besides the server and client spans, the front records the span below. Attribute keys use the `horde.` prefix.

| Name | Attributes | Meaning |
| --- | --- | --- |
| `horde.oai.delta_read` | `entry_count`, `byte_count`, `first_entry_lag_seconds` | A non-empty Redis stream read. See [Delta read attributes](#delta-read-attributes). |

#### Delta read attributes

The `horde.oai.delta_read` span records one non-empty Redis stream read across the registered requests.
An empty read does not produce a span.

| Attribute | Meaning |
| --- | --- |
| `entry_count` | Number of returned entries. |
| `byte_count` | UTF-8 size of the returned field names and values. |
| `first_entry_lag_seconds` | Time from the oldest entry's append to the read's return. The append time is the millisecond part of its stream id, on the Redis server's clock. |

The reader runs as one background task, so this is a root span outside individual request traces.

### Metrics

`create_app` uses the global meter provider unless a `meter` is supplied. Tests supply a separate
`MeterProvider` with an `InMemoryMetricReader`. An unconfigured global provider does not record metrics.

Attribute keys include the `horde.` prefix. Every attribute is bounded: call types, outcomes, wake sources, HTTP
statuses, error codes, outcome classes, refusal reasons, booleans and the model id. The model id comes from a
delivered completion, so it identifies a served model. Client addresses, API keys, prompts, client titles and
request ids are not used as attributes.

| Name | Type | Attributes | Meaning |
| --- | --- | --- | --- |
| `horde.oai.requests.admitted` | counter | `streamed` | Chat requests that took an in-flight slot. |
| `horde.oai.requests.refused` | counter | `status_code`, `code`, `parameter`, `outcome_class` | Refusals during admission or body validation. See [Refusal attributes](#refusal-attributes). |
| `horde.oai.requests.errors` | counter | `status_code`, `code`, `streamed`, `reason`, `stage`, `max_tokens_bucket`, `outcome_class` | Admitted requests ending in an HTTP error. See [Error attributes](#error-attributes). |
| `horde.oai.stream.failures` | counter | `code`, `stage`, `outcome_class` | Streamed requests that ended in an SSE error event after the 200 headers were sent. `stage` is as for `horde.oai.requests.errors`. |
| `horde.oai.fail_fast.rescues` | counter | none | Waits in which one status read did not report an eligible worker or a processing generation, and a later read within `fail_fast_grace_seconds` showed an eligible worker or the request processing. Compared with the `no_eligible_worker` count at the `grace` stage, it shows how many requests the grace kept from a refusal. |
| `horde.oai.inflight` | observable gauge | none | Requests holding an in-flight slot. |
| `horde.oai.inflight.sources` | observable gauge | none | Distinct client addresses holding at least one slot. |
| `horde.oai.inflight.source_max` | observable gauge | none | Maximum slots held by one client address. Compare with `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE`. |
| `horde.oai.request.body_bytes` | histogram, bytes | none | Size of each request body received whole. Oversized bodies are counted as refusals instead. |
| `horde.oai.native.duration` | histogram, seconds | `call_type`, `outcome` | Latency of each native call from taking a call slot to receiving the response. The histogram count is the call count. `call_type` is `submit`, `recovery`, `status`, `cancel` or `models`. `outcome` is `ok`, `refused` (horde status 400 or above), `unreachable` or `abandoned` (the request's budget ended first). |
| `horde.oai.queue_wait` | histogram, seconds | `model` | The horde's `queue_wait_seconds` of each delivered generation: request creation to assignment. |
| `horde.oai.first_content.duration` | histogram, seconds | `streamed`, `relayed` | From the submit acknowledgement, when a stream's headers go out, to the first content sent: the first relayed delta (`relayed` true), the first completion chunk, or the non-streamed body. |
| `horde.oai.request.duration` | histogram, seconds | `streamed`, `status_code`, `code`, `outcome_class` | From an admitted request's arrival to its end. A stream records 200 whatever its outcome. `code` is the error code, `ok` for a delivered completion or `client_closed` for a client that disconnected before delivery. |
| `horde.oai.wakes` | counter | `wake_source`, `found_finished`, `found_done_without_generation` | Each status read of a waiting request, by what caused it: `immediate` (the read after the submit), `signal`, `recheck`, `poll`, `deadline`. And `disconnect`, recorded without a read when the client disconnects. `found_done_without_generation` marks a read showing the request done, not faulted and without a generation. |
| `horde.oai.submit.outcome_unknown` | counter | none | Responses with `submit_outcome: "unknown"`. |
| `horde.oai.submit.recoveries` | counter | `existing_claim` | Lost submits recovered by re-sending. `existing_claim` true is a claim match (the Horde returned 200). |
| `horde.oai.client_retry_hints` | counter | none | Chat requests with `x-stainless-retry-count` above zero. |
| `horde.oai.completions` | counter | `model`, `estimated_usage`, `finish_reason`, `max_length_lowered`, `content`, `reasoning_effort` | Delivered completions. See [Completion attributes](#completion-attributes). |
| `horde.oai.completions.empty` | counter | `model` | Delivered completions whose generation does not hold visible text, the case `x_horde.notice` reports. |
| `horde.oai.request.max_tokens` | histogram, tokens | `source` | Accepted request's completion limit. See [Completion limit source](#completion-limit-source). |
| `horde.oai.reasoning_share` | histogram, ratio | `model`, `finish_reason` | For each delivered completion with reasoning tokens: `usage.completion_tokens_details.reasoning_tokens` divided by `usage.completion_tokens`. Both counts are estimates when the worker did not report them. A share near 1 with `finish_reason` `length` is a reply whose budget went to reasoning. |
| `horde.oai.stream.relayed` | counter | none | Streamed requests that sent at least one relayed delta. |
| `horde.oai.stream.relay_outcomes` | counter | `outcome` | How relayed streams ended: `equal`, `prefix`, `mismatch`, `replaced_attempt`, `ended` or `gap`. See [Relayed deltas](#relayed-deltas). |
| `horde.oai.stream.post_gap` | histogram, seconds | none | Gap between the stream id append times of consecutive accepted worker progress posts in a relayed stream. The pacing window is estimated from these gaps. A negative gap, from a stream recreated with lower ids, is recorded as 0. |
| `horde.oai.stream.pieces_per_post` | histogram | none | Number of sent pieces with part of one accepted post. A post sent whole counts 1. A paced post, or a post merged into the next window, counts more. |
| `horde.oai.stream.pacing_lag` | histogram, seconds | none | Time from a post's stream id append time to the send of the last piece with its text. It is measured on the front's Unix clock, so clock skew between Redis and the front shifts it. It includes the reader's read delay. A negative lag is recorded as 0. |
| `horde.oai.stream.pacing_flushes` | counter | `reason` | Accepted posts with text still pending when pacing stopped: `finished` (sent at once before the final reply), `broken` (sent at once before the error event) or `disconnected` (dropped). |
| `horde.oai.cancels` | counter | `outcome` | Cancel attempts by outcome. See [Cancel outcomes](#cancel-outcomes). |

#### Cancel outcomes

`horde.oai.cancels` counts every cancel attempt, including retries and attempts during shutdown.

| `outcome` | Meaning |
| --- | --- |
| `acknowledged` | The Horde acknowledged the cancel. |
| `failed` | The call failed. |
| `timed_out` | The HTTP call exceeded `HORDE_OAI_CANCEL_TIMEOUT_SECONDS`. |
| `not_started` | A cancel slot did not become free within that timeout, so the cancel was not sent. |
| `already_gone` | The Horde returned 404 because the request no longer exists. |

Attempts ending in `failed`, `timed_out` or `not_started` are retried every
`HORDE_OAI_CANCEL_RETRY_SECONDS` and once more during shutdown. Retries stop 20 minutes after submission
(`horde.oai.app.HORDE_CHAT_REQUEST_EXPIRY_SECONDS`). At most `horde.oai.app.MAX_FAILED_CANCELS` requests with
returned responses remain in the retry queue. The oldest entries are dropped first.

#### Refusal attributes

`horde.oai.requests.refused` records admission and validation refusals: body size (413), body read timeout
(408 `RequestBodyTimeout`), validation (400 or 404), authorization (401) and `FrontAtCapacity` (503).

For `UnsupportedChatParameter`, the `parameter` attribute is the top-level key from the refused path when the key
appears in one of these sets:

- The request schema or default no-op table.
- `UNDEFINED_OPENAI_PARAMETERS`, including `logit_bias` and `functions`.
- `COMMON_NON_OPENAI_SAMPLER_PARAMETERS`, including `top_k`, `min_p` and `rep_pen`.

For example, an unsupported message role produces `parameter=messages`, and an unknown Horde option produces
`parameter=horde`. Other keys produce `parameter=other`, so arbitrary client keys do not become metric labels.
For other error codes, the attribute is empty.

Each refusal also logs the full path. See [Refused parameter log line](#refused-parameter-log-line) to investigate
keys counted as `other`.

#### Error attributes

Admitted requests that got an error status before any header: horde refusals at submit (429
`rate_limit_exceeded` when the horde's 429 does not include an `rc` ), 408 `ChatRequestTimeout`, 404
`no_worker_supports_features`, 400 `no_worker_supports_limit`, 503 `no_eligible_worker`, 503
`queue_unavailable`, 502 `ChatRequestFaulted`, `ChatRequestNoGeneration` and `HordeUnavailable`.

`reason` is `no_eligible_threads` or `wait_exceeds_budget` for `queue_unavailable` and empty for every other code.

`stage` is `submit` for a `no_eligible_worker` refused by the Horde at creation, such as for a model without
an online chat worker. It is `grace` for a `no_eligible_worker` returned when the fail-fast grace ended, and
empty for every other code.

`max_tokens_bucket` is the request's completion limit, as in the finished line's `max_tokens`, reported as
the upper bound of its `BUCKETS_MAX_TOKENS` bucket (`16`, `64`, `128`, `256`, `512`, `768`, `1024`,
`2048`, `4096`, `8192`, `16384` ), or `over` above 16384. A limit of 600 is `768`. It shows refusals by
the limit clients asked for.

#### Completion attributes

`horde.oai.completions` records delivered completions with these attributes:

| Attribute | Meaning |
| --- | --- |
| `model` | Served model. |
| `estimated_usage` | True when a token count was estimated because the worker did not report it. |
| `finish_reason` | First choice's finish reason: `stop`, `length`, `content_filter` or `tool_calls`. |
| `max_length_lowered` | True when `x_horde.context_adjustments` includes `worker_max_length`. |
| `content` | First generation's content category, listed below. |
| `reasoning_effort` | Client's value, or `omitted`. |

A lowered limit with `finish_reason: length` may have cut the reply short.

| `content` value | First generation |
| --- | --- |
| `text` | Visible text. |
| `tool_calls` | Tool calls without visible text. |
| `reasoning_only` | Reasoning without visible text or tool calls. |
| `empty` | Does not include visible text, tool calls or reasoning. |

Whitespace does not count as visible text. Reasoning is read from the native status, so
`horde.include_reasoning: false` does not change the category. A `reasoning_only` completion with
`finish_reason: length` used the output budget before producing a visible reply. The OpenAI API cannot resume
that reasoning.

#### Completion limit source

The completion limit of each request accepted by the Horde at submit, recorded once when the request ends. A
request refused at submit did not run under a limit and is not recorded.

`source` is `client` (the client sent `max_tokens` or `max_completion_tokens` ), `default` (the client sent
neither, so the limit is 512) or `lowered` (the Horde lowered the limit to the largest one declared by an
online worker, the `worker_max_length` context adjustment).

The Horde reports a lowered limit only in a status, so a request that ended before any status read is recorded as `client` or `default`.

#### Histogram buckets

Bucket boundaries: native call latency uses the horde's `BUCKETS_SECONDS`. Queue wait, first content and request
duration use `BUCKETS_REQUEST_LIFECYCLE_SECONDS` (both in `horde/metrics.py`). Body size uses
`horde.oai.metrics.BUCKETS_BODY_BYTES`. Post gap and pacing lag use `horde.oai.metrics.BUCKETS_POST_GAP_SECONDS` (0.1
to 10 seconds), and pieces per post uses `horde.oai.metrics.BUCKETS_PIECES_PER_POST` (1 to 80). The completion limit
uses `horde.oai.metrics.BUCKETS_MAX_TOKENS` (16 to 16384 tokens), and the reasoning share uses
`horde.oai.metrics.BUCKETS_REASONING_SHARE` (0 to 1 in steps of 0.1).

#### Outcome class

Every finished front request has one `outcome_class` on `horde.oai.requests.refused`, `horde.oai.requests.errors`,
`horde.oai.stream.failures` and `horde.oai.request.duration`. `outcome_class_for` in `horde/oai/app.py` derives it
from the request's `code` and, for `no_eligible_worker`, its `stage`, through the table `OUTCOME_CLASS_BY_CODE`.

| Class | Codes |
| --- | --- |
| `served` | `ok` |
| `client_gave_up` | `client_closed` |
| `capacity` | `no_eligible_worker` at stage `grace`, `ChatRequestTimeout`, `queue_unavailable` |
| `service_fault` | `HordeUnavailable`, `HordeUnexpectedResponse`, `ChatRequestFaulted`, `ChatRequestNoGeneration`, `ChatStreamDiverged`, `ChatWorkerFault` |
| `unservable` | `no_eligible_worker` at stage `submit`, `UnknownChatModel`, `model_not_found`, `no_worker_supports_features`, `no_worker_supports_limit`, `PromptExceedsContext` |
| `client_error` | `UnsupportedChatParameter`, `InvalidAPIKey`, `InvalidAuthorizationHeader`, `RequestTooLarge`, `RequestBodyTimeout`, `rate_limit_exceeded`, `max_kudos_exceeded`, `KudosUpfront`, `OperationKeyConflict`, `SourceImageSizeExceeded`, `TooManyExtraSourceImages`, `ChatRequestConflict` |
| `front_capacity` | `FrontAtCapacity` |
| `unclassified` | any other code |

The class of `no_eligible_worker` depends on its `stage`. At `grace`, the Horde accepted the request, but an
eligible worker did not take it within the grace period. This is classed as `capacity`. At `submit`, the
Horde refused the request because it could not be served by any online worker. This is classed as `unservable`. A volunteer worker coming online can make the same request servable.

The front passes the Horde's `rc` through as the code, so the code set is open. A code not in the table is
`unclassified` and is not given a default class, so a new return code stays visible.

The front does not measure settlement lag between delivery and the native terminal state, since it does not observe
the Horde's settlement.

`FrontAtCapacity` is the in-flight cap per client address or in total. It rises with client concurrency, and also when
served requests wait longer, so it has its own class.

The served share of servable requests is `served` divided by the sum of `served`, `capacity` and `service_fault`.

### Refused parameter log line

The front logs one Loguru `INFO` record for each `UnsupportedChatParameter` refusal:

```text
Refused unsupported chat parameter: path=<repr of the request path> label=<horde.parameter label>
```

The path is the refused request path, such as `'top_k'` or `'messages[0].content[1].type'`. It is cut to
`REFUSED_PARAMETER_LOG_LENGTH` (64) characters and written in Python `repr` form, so a client-chosen key cannot make the
line long or split it. `label` is the `horde.parameter` value of the refusal counter. The lines with `label=other`
show the keys counted as `other`. They show whether `COMMON_NON_OPENAI_SAMPLER_PARAMETERS` covers the keys that
clients send.

### Finished-request log line

Every admitted request logs one Loguru `INFO` record when it ends. The message is

```text
Chat request <id> finished: streamed=<bool> submitted_at=<unix> woke_at=<unix|none> woke_by=<source|None> waited=<seconds|none>s status=<status> code=<code> wakes=<count> estimated_usage=<bool|None> done_without_generation_reads=<count> requested_model=<models> served_model=<model|none> retry_after=<seconds|none>
```

A `queue_unavailable` refusal appends
`queue_reason=<reason> wait_time=<seconds|none> eligible_worker_threads=<count|none>`. Every line then ends with
`first_read_wait_time=<seconds|none> first_read_eligible_worker_threads=<count|none> max_tokens=<limit> max_tokens_source=<client|default|lowered>`.

The same values are bound to the record's `extra`, which the OpenTelemetry log bridge exports as attributes. A
request refused at submit does not have an id. Its message starts `Unsubmitted chat request finished:`.
`tests/stress/chat_scale_demo.py` parses the message up to `waited`.

| Field | Meaning |
| --- | --- |
| `request_id` | The horde request id, or null before the submit was acknowledged. |
| `streamed` | Whether the request was streamed. |
| `submitted_at` | Wall-clock time of the submit acknowledgement, Unix seconds. |
| `woke_at` | Wall-clock time of the status read that found the request finished. Null when a read did not find a finished request. |
| `woke_by` | The wake source of that read, or `disconnect`. Null for a timeout, fail-fast or horde error. |
| `waited_seconds` | `woke_at` minus `submitted_at`, or the time until the end when the request was not found finished. |
| `status_code` | The HTTP status sent: 200 for every stream whose headers went out, 499 for a non-streamed client that disconnected. |
| `code` | The error code, `ok` or `client_closed`. |
| `wake_count` | How many status reads the wait made. |
| `estimated_usage` | Whether the delivered completion's usage was estimated. Null when a completion was not delivered. |
| `done_without_generation_reads` | How many status reads showed the request done, not faulted and without a generation. |
| `requested_model` | The models the request accepts, comma-separated. |
| `served_model` | The model of the delivered completion. Null when a completion was not delivered. |
| `retry_after` | The `Retry-After` value in the error response or event, in seconds. Null when it was not included. |
| `queue_reason` | `queue_unavailable` only: `no_eligible_threads` or `wait_exceeds_budget`. |
| `wait_time` | `queue_unavailable` only: the wait estimate from the status read used for admission. Null when the read did not report a value. |
| `eligible_worker_threads` | `queue_unavailable` only: the eligible worker thread count from that status read. Null when the read did not report a value. |
| `first_read_wait_time` | `fail_fast` only: the wait estimate of the status read that took the first-read decision, whether it admitted or refused the request. Null in the `wait` mode, before a decision, or when the read did not report a value. |
| `first_read_eligible_worker_threads` | `fail_fast` only: the eligible worker threads that read reported. Null as for `first_read_wait_time`. |
| `max_tokens` | The request's effective completion limit: the Horde's lowered limit from the completion's status or the last status read, else the client's `max_tokens` or `max_completion_tokens`, else 512. |
| `max_tokens_source` | Where `max_tokens` came from, as the `source` of `horde.oai.request.max_tokens`: `client`, `default` or `lowered`. |

The line does not include the API key, the messages, the client's title or address, or the operation key.

### No-visible-text log line

A delivered completion whose `horde.oai.completions` `content` is `reasoning_only` or `empty` also logs one Loguru
`INFO` record before its finished-request line:

```text
Chat request <id> returned no visible text: content=<reasoning_only|empty> model=<model> max_tokens=<limit> max_tokens_source=<client|default|lowered> reasoning_effort=<effort|omitted> reasoning_tokens=<count|none> finish_reason=<reason>
```

The same values are bound to the record's `extra` as `request_id`, `model`, `max_tokens`,
`max_tokens_source`, `reasoning_effort`, `reasoning_tokens`, `finish_reason` and `content`. `max_tokens`
is the request's effective completion limit, and `max_tokens_source` is its `source` as in
`horde.oai.request.max_tokens`. `reasoning_tokens` is `usage.completion_tokens_details.reasoning_tokens`, an
estimate when the worker did not report it, or null when the completion does not include a reasoning count.
The line does not include the messages, the reply or the reasoning.

## Contract tests

`tests/integration/test_chat_front_sdk.py` exercises the front through the official OpenAI Python SDK:

| Contract | Test |
| --- | --- |
| Completion and usage | `test_a_completion_returns_the_workers_reply_with_its_usage` |
| Stream event order | `test_a_streamed_completion_yields_role_content_finish_and_usage_chunks` |
| Unsupported parameter path | `test_logit_bias_is_refused_with_its_param` |
| Unserved model and SDK retry behavior | `test_a_model_without_an_online_worker_fails_fast_without_an_sdk_retry` |
| Image URL transport | `test_an_image_reaches_a_url_worker_as_a_signed_url` |
| Streamed tool history | `test_a_streamed_tool_call_round_accumulates_in_the_sdk_and_then_answers_the_result` |
| Structured output | `test_a_json_schema_response_format_reaches_the_worker_and_returns_its_document` |

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Entry point and telemetry setup | `horde/oai/__main__.py` | `main`, `configure_telemetry`, `FRONT_SERVICE_NAME` |
| Settings | `horde/oai/settings.py` | `FrontSettings`, `load_settings` |
| Application and routes | `horde/oai/app.py` | `create_app`, `FrontState` |
| OpenAPI description and documentation routes | `horde/oai/openapi.py` | `build_openapi_schema`, `FRONT_DESCRIPTION`, `chat_completions_route_options` |
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
