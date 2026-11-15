---
title: "Chat front reference"
summary: "Routes, settings, OpenAI-compatible request and response formats, stream behavior, availability errors and telemetry of the horde.oai service."
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

The chat front exposes OpenAI-compatible chat completions and model listings through the Horde's native
`/v2/generate/chat` routes. It runs separately from the Flask application as `python -m horde.oai`.
See [Chat requests reference](chat_requests.md) for the native API behind these routes.
[Run the OpenAI-compatible chat front](../how-to/run_openai_chat_front.md) covers how to run it.

## Routes

| Route | Response |
| --- | --- |
| `POST /v1/chat/completions` | A chat completion, or a server-sent event stream with `"stream": true`. |
| `GET /v1/models` | The model list. See [Model card fields](#model-card-fields). |
| `GET /v1/models/<model name>` | One model list entry. The name may contain slashes. |
| `GET /healthz` | `live`, `ready` (false at `HORDE_OAI_MAX_INFLIGHT` or while stopping), `signal_subscribed` and `inflight`. |
| `GET /routing` | `instances` (each Horde instance's routing state) and `drain_agents`. A request with `X-Forwarded-For` gets 404. |
| `GET /` | A redirect to `/docs`. |
| `GET /docs`, `GET /redoc`, `GET /openapi.json` | The API documentation. |

## API documentation

The front serves an OpenAPI description of its routes. `/openapi.json` is the OpenAPI 3.1 document. `/docs` renders
it with Swagger UI, `/redoc` renders it with ReDoc, and `/` redirects to `/docs`. `/healthz`, `/routing` and `/` are
left out of the description.

### Schema generation

The route handlers read and validate the body by hand, so FastAPI does not derive any schema from them.
`horde/oai/openapi.py` generates the component schemas from `ChatCompletionRequest`, as validated, and from
`ChatCompletionResponse`, `ChatCompletionChunk`, `OpenAIErrorEnvelope`, `ModelList` and `ModelCard`, as serialized.
It also supplies each route's request body, its responses by status with their error codes, and the `x-should-retry`
and `Retry-After` headers. The document only describes the handlers and does not change their behavior. Where the
handlers differ from the models, the document adjusts the generated schemas:

- The request schema lists the keys of `DEFAULT_NO_OP_PARAMETERS` that are not request fields, from
  `NO_OP_PARAMETER_SCHEMAS`, each accepted only at its default.
- The schemas of the objects whose unknown keys are dropped by `drop_unknown_nested_keys` (`KEY_DROPPING_MODELS`)
  allow other keys. The top level, `stream_options`, `response_format` and `horde` do not.
- FastAPI's 422 response is removed, since the front does not return it.

### Field descriptions

Each field's description is the docstring of the model field in `horde/oai/schema.py` or `horde/chat_contract.py`.
Those docstrings are therefore public text for API users, and they state where the front differs from OpenAI. In the
document, `plain_description` drops a parenthetical ``(see [`name`][path])`` cross-reference and renders an inline
``[`name`][path]`` one as `` `name` ``. A detail needed only by maintainers goes in a comment above the field. Models
serialized through a wrap `model_serializer` that returns `dict[str, object]` are documented by their fields
(`DocumentedJsonSchema`). The description links to the published documentation site (`DOCS_URL`).

## Request keys

Example: [Front request](chat_wire_examples.md#front-request).

The front validates the body against `ChatCompletionRequest` after screening it in `horde/oai/schema.py`.
A body that is not JSON, or contains `NaN`, `Infinity` or `-Infinity`, gets 400 `BadRequest` before screening.

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

Unknown keys are refused at the top level and in all other objects. For example, an unknown key in `stream_options`
or `response_format` is refused with 400 at its path. `response_format.json_schema` requires the key `schema`.
The Python field name `schema_` is treated as an unknown key.

| Keys | Handling |
| --- | --- |
| `model`, `models`, `messages`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `stop`, `seed`, `reasoning_effort`, `n`, `horde` | Validated and forwarded. Null counts as omitted. `reasoning_effort` is one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. `max_tokens` covers the reasoning and the answer together. |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` | Validated against the `horde/chat_contract.py` shapes and forwarded verbatim. A `json_schema` response format is forwarded with its `schema` key and needs one. Dropped at their default: empty or null `tools`, `{"type": "text"}`. A null `tool_choice` or `parallel_tool_calls` is not forwarded. |
| `stream`, `stream_options`, `user`, `metadata` | Applied or accepted by the front and not forwarded, except `stream: true` on a request whose deltas are relayed (see [Relayed deltas](#relayed-deltas)). |
| `logprobs`, `top_logprobs`, `store`, `service_tier` | Dropped at their default (`false`, `0`, `false`, `auto` or `default`). Any other value is 400. |

The `horde` object holds worker restrictions, context options, conformance, reasoning visibility, availability and
kudos limits. [Horde options](../how-to/send_chat_completions.md#sampling-defaults-and-the-horde-object) lists them
with their defaults. `horde.dry_run` and `horde.operation_key` are for native callers only
(`INTERNAL_HORDE_OPTION_KEYS` in `horde/chat_contract.py`), and the front refuses both with 400.

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

If any entry is invalid, the whole request is refused. For an unknown model in `models`, the native message begins
with the quoted entry path. The front extracts that path with `NATIVE_REQUEST_PATH_PATTERN`.

### Value and contract validation

`ChatCompletionRequest` bounds the sampling values to OpenAI's ranges (the SDK's `OPENAI_SAMPLING_RANGES`):
`temperature` 0 to 2, `top_p` 0 to 1, `frequency_penalty` and `presence_penalty` -2 to 2. A value outside its range,
an `n` or token limit below 1, and more than four stop sequences get 400 `UnsupportedChatParameter` with OpenAI's
message and `param`. An empty `messages` gets 400 `BadRequest` with OpenAI's message. OpenAI's message already
includes the parameter, so the front does not prefix it with the path. A token limit from 1 to 15 is refused with a
message that states the Horde floor (the SDK's `MIN_CHAT_MAX_TOKENS`).

The front also refuses these values with 400 `UnsupportedChatParameter`: a token limit above 16384 (the SDK's
`MAX_CHAT_MAX_TOKENS`) and an `n` above 1. `max_tokens` and `max_completion_tokens` must be equal when both are sent,
or the request gets 400 `ChatRequestConflict`. `stream_options` without `"stream": true` gets 400 `BadRequest`. A
request without a token limit is forwarded without one, and the Horde generates up to 512 tokens.

When a native payload validation failure reaches the front, it has a generic message and does not include an `rc`.
The front returns 400 `BadRequest` with the first failing field as `param`. Where the native text param key differs,
the front translates it (`NATIVE_FIELD_PARAMETERS` in `horde/oai/app.py`: `max_length` is `max_tokens`,
`stop_sequence` is `stop`).

After validation, the front applies the request rules from `horde/chat_contract.py`, which the native route also uses.
The client then receives OpenAI's message and `param` for these cases: a `tool_choice` or `parallel_tool_calls`
without tools (`ChatRequestConflict`), a tool history that breaks OpenAI's rule and a `strict` `json_schema` that
breaks OpenAI's structural rules (`UnsupportedChatParameter`), and, under `horde.openai_guards`, a `json_object`
request without the word "json" in its messages (`ChatRequestConflict`, `param` `messages`).

A tool `parameters` or response schema nested deeper than the SDK's `MAX_CHAT_SCHEMA_NESTING` fails validation as
`UnsupportedChatParameter` at its path. The native route repeats these checks. It also checks for a forced function
that is not defined in the tools, the tool count and tool spec size (the SDK's `MAX_CHAT_TOOL_COUNT` and
`MAX_CHAT_TOOL_SPEC_BYTES`), and the tool call bounds in `MAX_CHAT_TOOL_CALL_*`.

The message text limit includes the function names and arguments of history tool calls.

## Message roles

| Role | Keys | Content |
| --- | --- | --- |
| `system`, `developer` | `role`, `content`, `name` | A string or `text` parts. |
| `user` | `role`, `content`, `name` | A string, or `text` and `image_url` parts. |
| `assistant` | `role`, `content`, `name`, `tool_calls` | A string or `text` parts. Null or omitted only with `tool_calls`, and then forwarded as null. |
| `tool` | `role`, `content`, `tool_call_id` (required) | A string or `text` parts. |

The front forwards `developer` messages unchanged. The Horde stores them as `system`, so the developer role does not
take precedence over system messages.

A key outside its role's set (`SUPPORTED_MESSAGE_KEYS`) is dropped, including `name` on a `tool` message.
`tool_calls` on a message other than an assistant message is 400 `UnsupportedChatParameter` at
`messages[<i>].tool_calls`, since OpenAI also refuses it.

Inside a message, a tool call keeps `id`, `type` and `function`, and its function keeps `name` and `arguments`. A
`text` part keeps `type` and `text`. An `image_url` part keeps `type` and `image_url`, and the `image_url` object
keeps `url` and `detail`. Retained keys are still validated.

This removal includes the fields that the OpenAI Python SDK adds to an accumulated streamed reply (`refusal`, `audio`,
`annotations`, `function_call`, `parsed`, `tool_calls[].index`, `function.parsed_arguments`) when the SDK sends that
reply back as history.

An unsupported role (such as `function`) or content part type (such as `input_audio`) is still 400
`UnsupportedChatParameter`, with `param` such as `messages[0].role` or `messages[0].content[0].type=input_audio`.

## Image inputs

A `user` message may include `{"type": "image_url", "image_url": {"url": ..., "detail": ...}}` parts. `url` is a
`data:image/...;base64,` URL or an `http(s)` URL, and `detail` is optionally `auto`, `low` or `high`. The front
forwards the parts unchanged.

Before the submit, `_find_image_limit_error` in `horde/oai/app.py` refuses a request that exceeds the native limits
in `horde/chat_contract.py`:

| Limit | Constant | Refusal |
| --- | --- | --- |
| 20 image parts | `MAX_CHAT_IMAGE_COUNT` | 400 `TooManyExtraSourceImages`, `param` `messages` |
| 5 MiB per data URL, decoded | `MAX_CHAT_IMAGE_BYTES` | 400 `SourceImageSizeExceeded`, `param` the path of the part's `url` |
| 25 MiB of data URLs in all, decoded | `MAX_CHAT_IMAGE_TOTAL_BYTES` | 400 `SourceImageSizeExceeded`, `param` the path of the `url` that takes the total over the limit |

The decoded size is estimated from the base64 length. Data URLs do not count toward `HORDE_OAI_MAX_TEXT_BYTES`. The
front does not know the user's trust status. The Horde therefore enforces the smaller 5 MiB total for users who are
neither trusted nor patrons, and checks images behind `http(s)` URLs. The Horde fetches, checks and stores each image,
and charges the extra source image tax per image. It sends the image only to a worker that declares the `vision`
feature and an image transport. A model whose list entry shows `image` in `architecture.input_modalities` has such a
worker online.

Horde image refusals pass through with the Horde's `rc` as `code`, such as `SourceImageUrlRefused` for a URL at a
private address, `SourceImageResolutionExceeded`, or `SourceImageSizeExceeded` for images over the user's total. A
refusal by the image store passes through as 503 `ChatImageStorageUnavailable`.

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
`x_horde.context_adjustments` is the native status's list, passed through unchanged. It is omitted when the Horde
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

Every stream chunk uses the same fingerprint. In a relayed stream, the fingerprint is derived from the first delta's
worker and model and is also used for the final chunks. It stays null when the stream entries do not include a worker.

### Buffered stream events

A stream's headers are sent once the first status read has passed the checks in
[No eligible worker](#no-eligible-worker). After the keepalive comments, the front sends these
`chat.completion.chunk` events:

1. `delta: {"role": "assistant"}`.
2. A reasoning chunk with the whole `delta.reasoning_content`, when the message has it (left out when
   `horde.include_reasoning` is false).
3. A content chunk with the whole `delta.content`, when the reply has text.
4. A tool-call chunk with `delta.tool_calls`, when the reply has calls, each call whole with its 0-based `index`,
   `id`, `type` and `function`. The chunk does not include content, because some clients read tool calls only from
   chunks that do not have content.
5. `delta: {}` with `finish_reason`, `native_finish_reason` and `x_horde`.
6. `choices: []` with `usage`, when `stream_options.include_usage` was true.
7. `data: [DONE]`.

### Relayed deltas

`relays_deltas` in `horde/oai/mapping.py` selects requests that have `stream: true`, do not have `tools`, and do not
have a `response_format` other than `text`. The horde verifies tool calls, and under strict conformance structured
output, only when the reply is complete. Relaying text before verification could therefore expose a reply that is
later refused.

When the front has a Redis client (`HORDE_OAI_REDIS_URL`), it forwards `stream: true` for such a request. The horde
then writes the progress lease at pop and sets `stream_progress` on the job (see
[Streaming progress](chat_requests.md#streaming-progress)). Every other streamed request is delivered whole as above.
This includes every request on a front without Redis and every request served by a worker that does not post
progress.

#### Redis reads and recovery

Each process runs one `DeltaStreamReader` task. It reads `horde:chat:stream:<request id>` on
`HORDE_OAI_STREAM_REDIS_DB` for every registered request in one `XREAD BLOCK 250` call, starting at each stream's
start, and queues each entry on its request. A read that takes longer than 5 seconds (`DELTA_READ_TIMEOUT_SECONDS`)
is abandoned and made again. This covers a connection that a network fault left unresponsive. The reader does not
send a PING. A failed read is made again after 2 seconds, starting from the last entry read. The stream keeps its
entries, so the reader does not skip any.

When a stream is deleted or expires, the reader does not receive entries or an error. When a stream is recreated with
ids below the last id read, for example after a failover that lost the key, the reader does not receive those
entries. In both cases the deltas stop. If the front has not sent anything yet, the request is delivered whole.
Otherwise the final reply completes the stream through the reconciliation below.

#### Delta fields

The first delta with `seq` 1 pins the generation id (`gen`). The role chunk is sent with the first delta that has
content to send, and the stream is then exposed to the client.

For each delta, the front sends a `delta.reasoning_content` chunk when the delta includes reasoning, followed by a
`delta.content` chunk when it includes text. With `horde.include_reasoning: false`, reasoning is discarded. A
reasoning-only delta still advances the sequence, so later text is relayed.

Each chunk uses the first delta's `model` and the fingerprint derived from that entry's `worker` and `model`.
If the entry omits the model, the request's `model` or first listed model is used.
Keepalive comments continue between deltas.

#### Pacing

The front sends the first delta immediately. It divides later text and reasoning into smaller pieces and sends
them at regular intervals to smooth delivery between worker posts. The pacing window estimates the interval between
posts using an exponential moving average of the gaps between their stream ID times,
with a weight of 0.5 on the newest gap. It is kept between 0.05 seconds and
`HORDE_OAI_DELTA_PACING_MAX_WINDOW_SECONDS`.

A window holds at most `HORDE_OAI_DELTA_PACING_MAX_CHUNKS_PER_SECOND` pieces per second, and the piece count does not
exceed the character count. Within one delta, every reasoning piece is sent before the first content piece. The piece
count is divided between reasoning and content by length, with at least one piece for each field that has text.

Each cut is at whitespace within half a piece of an even offset. Without such whitespace, the cut is at the next offset
that does not split a visible character. A cut does not separate a character from a following Unicode mark (such as
an accent, vowel sign or keycap), variation selector or skin-tone modifier. A cut does not split a zero-width joiner
sequence or a flag.

A delta that arrives while pieces are pending is merged with them. The merged text is spread over a new window that
starts at the delta's arrival, so pacing adds at most about one window of latency. When the request finishes or the
relay breaks, the pending pieces are sent at once, before the final reply's reconciliation. When the client
disconnects, the pending pieces are dropped. With a maximum window of 0, each delta is sent as it arrives.

#### Final reply reconciliation

The front reconciles reasoning separately from text. If the final reasoning starts with the relayed reasoning,
the front sends the remaining reasoning before the remaining text. Otherwise it does not send any more reasoning.

After the stream is exposed, text reconciliation follows these rules:

| Event | Result |
| --- | --- |
| Final reply equal to the relayed text | The finish chunk, the usage chunk when asked for, and `[DONE]` |
| Final reply starting with the relayed text | The rest as one content chunk, then the finish chunk |
| Final reply not starting with the relayed text | SSE error event `ChatStreamDiverged`, then `[DONE]` |
| An entry of another `gen`, the pinned `gen`'s `ended` marker (also written when the request is cancelled through the native route), or a `seq` out of order | SSE error event `ChatStreamDiverged`, then `[DONE]`. The front cancels the request |
| The request faults | SSE error event `ChatRequestFaulted`, as for a buffered stream |

A restarted attempt starts the reply over, so it is not appended to a partially delivered choice ([ADR
0024](../decisions/0024-stream-chat-deltas-through-a-retained-redis-stream.md)). Before the stream is exposed, the
front drops an `ended` marker, and also a delta read together with its generation's `ended` marker. Such a marker, or
a gap in the pinned `gen`, unpins the generation. The first delta of the next attempt can then pin again. Otherwise
the request is delivered whole. The error event is `{"error": {...}}` with `type` `api_error`, `code`
`ChatStreamDiverged` and `metadata.request_id`, followed by `data: [DONE]`.

### Browser access

The front responds to browser preflight requests and allows any origin on every response
(`Access-Control-Allow-Origin: *`). It exposes the `Retry-After` and `x-should-retry` headers. A web client such as
KoboldAI Lite can therefore call the front with a bearer key.

### Completion without a generation

A native status can be `done` and not `faulted` without a finished generation. While such a status still reports a
job waiting or processing, the front keeps waiting. Otherwise the front reads the status again after
`signal_recheck_seconds`. If the recheck still shows the request done without a generation, or a `cancelled` signal
arrived for the request, the front cancels the request and returns 502 `ChatRequestNoGeneration`. The front does not
return a completion without a generation.

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

## Errors the front returns

Every error is an OpenAI envelope, `{"error": {"message", "type", "param", "code", "metadata"}}`, with `param` and
`code` written as null when they do not apply. `metadata` holds what is known: `request_id`, the horde's `rc`,
`retry_after`, and `submit_outcome: "unknown"` with the request's `operation_key` when a submit was lost and recovery
did not settle it.

A Horde error keeps the Horde's status, and its `rc` becomes `code`. A horde 429 keeps `Retry-After` and the
`X-RateLimit-*` headers. It has code `rate_limit_exceeded` when the Horde does not send an `rc`, such as for its
per-route request limit. A horde 503 at submit without `Retry-After` (such as `ChatAdmissionDisabled` or
`ChatImageStorageUnavailable`) gets `Retry-After: 30`.

During the wait, a status read that gets 429, 500, 502, 503 or 504 is made again at the next wake. Any other horde
error, such as 401 or 403, ends the wait, and the front cancels the request before returning the error. A 404 means
the horde does not hold the request any more.

Once the horde holds a request, or may hold one, every error includes `x-should-retry: false`, so OpenAI SDKs do not
retry into a second charged job. Errors before the submit do not include the header and are safe to retry. A 503
`no_eligible_worker` always includes it.

| Status | `code` | `x-should-retry` | Cause |
| --- | --- | --- | --- |
| 400 | `UnsupportedChatParameter` | | A key, role, content part, value or size that is not supported by the front. `param` holds its path. See [Request keys](#request-keys). |
| 400 | `ChatRequestConflict` | | `max_tokens` and `max_completion_tokens` differ, `model` and `models` are both sent, `only` and `ignore` are both set, or `tool_choice` or `parallel_tool_calls` is sent without tools. |
| 400 | `BadRequest` | | The body is not a JSON object, or a value has the wrong type. |
| 400 | `TooManyExtraSourceImages` | | More than 20 image parts. See [Image inputs](#image-inputs). |
| 400 | `SourceImageSizeExceeded` | | An image data URL decodes to more than 5 MiB, or the data URLs to more than 25 MiB in all. `param` is the path of the part's `url`. |
| 400 | `max_kudos_exceeded` | | The kudos estimate exceeds `horde.max_kudos`. See [Kudos cap](#kudos-cap). |
| 400 | `no_worker_supports_limit` | `false` after the submit | The request is over the size limits of every online worker. See [Size limit gaps](#size-limit-gaps). |
| 401 | `InvalidAuthorizationHeader` | | `Authorization` is present but not `Bearer <key>`. Omit it to use the anonymous key. |
| 404 | `model_not_found` | | The model id contains `:`. The front does not serve model variants. |
| 404 | `UnknownChatModel` | | The horde does not know the model. |
| 404 | `no_worker_supports_features` | `false` after the submit | No online worker declares the requested features or guarantees. See [Declaration gaps](#declaration-gaps). |
| 408 | `ChatRequestTimeout` | `false` | The request was still waiting after `HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS` and one final status read. It is cancelled. An outcome reached by the deadline is returned instead. `Retry-After: 30`. |
| 408 | `RequestBodyTimeout` | | The body took longer than `HORDE_OAI_BODY_READ_TIMEOUT_SECONDS` to arrive. The request was not submitted. |
| 413 | `RequestTooLarge` | | The body exceeds `HORDE_OAI_MAX_BODY_BYTES`. |
| 502 | `ChatRequestFaulted` | `false` | The horde reports the request as faulted, or it expired on the horde. When the fault followed a refused worker reply, the message states the refusal and `metadata.rc` includes its return code. |
| 502 | `ChatRequestNoGeneration` | `false` | The horde reported the request done without a generation and not faulted, after a short recheck or a `cancelled` signal, for example after a maintenance cancel. It is cancelled. |
| 502 | `HordeUnavailable` | | Every usable horde instance in `HORDE_OAI_HORDE_URLS` and `HORDE_OAI_FALLBACK_HORDE_URLS` refused the connection, or every horde instance is drained. |
| 502 | `HordeUnavailable` | `false` | The submit was sent and its outcome stayed unknown after recovery. See [Lost submits](../how-to/run_openai_chat_front.md#lost-submits). |
| 502 | `OperationKeyConflict` | `false` | A recovery attempt was refused because the operation key holds a different request. The front re-sends the identical body, so this is a front defect. `metadata` holds the `rc` and `operation_key`. |
| 503 | `FrontAtCapacity` | | The process or the client's address is at its maximum of open requests. `Retry-After: 5`. |
| 503 | `no_eligible_worker` | `false` | No online chat worker serves the model at submit, or no online worker could serve the request for `HORDE_OAI_FAIL_FAST_GRACE_SECONDS`, after which it is cancelled. `Retry-After: 30`. See [No eligible worker](#no-eligible-worker). |
| 503 | `queue_unavailable` | `false` | A `horde.availability_mode: "fail_fast"` request whose first status read showed zero eligible worker threads, or an estimated wait over the time left in its budget. It is cancelled. `Retry-After` is the estimated wait, at most 600, or 30 with zero threads. See [Fail-fast admission](#fail-fast-admission). |

A streamed request's headers go out after the first status read. A refusal at that read (a declaration or limit gap,
`queue_unavailable`, or a status error) is therefore returned as the HTTP error in the table, as on a non-streamed
request. In a stream, an error after the headers arrives as one `data:` event with the same envelope and no choice, then
`data: [DONE]`. A relayed stream whose final reply does not match the relayed text ends with the `ChatStreamDiverged`
event. A streamed request has a separate budget, `HORDE_OAI_STREAM_TIMEOUT_SECONDS`. See
[Stream errors](#stream-errors).

A client retry after a lost response is a new request and can create a second charged job. The front does not
deduplicate requests.

## No eligible worker

### Model availability

The Horde refuses a request at submit when its models are not served by any online chat worker (see
[Known models](chat_requests.md#known-models)). The front then returns 503 `no_eligible_worker` with
`Retry-After: 30` at once. The message is the Horde's, with ` Retry later.` added, such as
`No worker is currently serving the model 'llama-3-8b'. Retry later.` The front does not wait for the grace.

The native status of a submitted request can report `is_possible` false without a processing generation. This
happens when the workers serving the model go offline or become busy with other work. The front then waits
`fail_fast_grace_seconds` for a worker to appear, then cancels the request and returns 503 `no_eligible_worker` with
`Retry-After: 30`.

### Declaration gaps

When the status includes an `impossible_reason` of kind `declaration_gap` (see
[Impossible reason](chat_requests.md#impossible-reason)), the front cancels the request at once. It returns 404
`no_worker_supports_features` without a `Retry-After`. OpenRouter uses this status when the requested
features are not supported by any endpoint. The message lists the missing declarations or the exceeded limit. When a
guarantee is missing, the message also says that guarantees are not required under `best_effort`. The `type` is
`not_found_error`, `param` is null, and `metadata.request_id` holds the cancelled native request:

```json
--8<-- "docs/reference/chat_wire_examples.md:front-no-worker-supports-features"
```

### Size limit gaps

A gap of only `max_length` or `max_context_length` in `exceeded_limits` means that the request is over the size
limits of every online worker for the request's models. At submit the Horde lowers a `max_tokens` above the largest
declared `max_length` (see [Context adjustments](chat_requests.md#context-adjustments)). A `max_length` gap therefore
arises only when the workers' declarations change after the submit.

For these gaps, the front returns 400 `invalid_request_error` with code `no_worker_supports_limit` instead, without a
`Retry-After`. The `param` is `max_tokens`, or `horde.max_context_length` when only the context length is exceeded.

The message has one sentence for each exceeded limit, such as
`No worker eligible for this request currently declares a completion limit that high; the largest any online worker for the model declares now is 1024 tokens.`
For the context length, the sentence says "a context length" instead. The value comes from the model list's `chat`
entry.

Under `models` any listed model may serve the request, so the value is the largest across the listed models' entries,
and the sentence reads "for the listed models". When the model list does not have an entry for any requested model,
the sentence gives the limit's name in place of the value. Volunteer workers come and go, so the message describes the
workers online at that time, and the client decides whether to retry.

Any other gap, including a size limit beside another cause, keeps the 404.

### Native admission refusals

The Horde also refuses the submit with 403 `NoEligibleWorker` when the request would be refused for kudos and it
cannot be served by any online worker (see [Free output allowance](chat_requests.md#free-output-allowance)). The
front maps this refusal as above. When the body includes an `impossible_reason`, the front returns 404
`no_worker_supports_features` or 400 `no_worker_supports_limit`. A body without one gets 503 `no_eligible_worker`
with `Retry-After: 30` and the Horde's message. The Horde did not create a request, so these responses do not include
a `request_id` and do not state that the request was cancelled.

The 404 and 400 do not set `x-should-retry: false`. Every `no_eligible_worker` response sets it, since an SDK
retries within seconds and the online workers are most likely the same by then. The refusal comes before any stream
header, so a streaming request gets the same JSON error.

### Fail-fast admission

With `horde.availability_mode: "fail_fast"`, the front checks availability at the first successful status read
when the request does not have a processing or finished generation.

| Condition | Response |
| --- | --- |
| Zero `eligible_worker_threads` | Cancel at once. Return 503 `queue_unavailable` with `Retry-After: 30`. |
| `wait_time` exceeds the remaining request budget | Cancel. Return 503 `queue_unavailable` with `Retry-After` equal to `wait_time`, capped at 600 seconds. |

The budget is `nonstream_timeout_seconds` or `stream_timeout_seconds`.
`QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS` sets the retry cap, because the online pool changes from minute to
minute. The error's `param` is `horde.availability_mode`. Its message gives the eligible worker threads, the
estimated wait and the remaining budget at that read.

A declaration gap takes precedence over these rules. If an older Horde status does not include
`eligible_worker_threads`, the front does not reject the request based on thread count.

#### Queue position refresh

The Horde refreshes cached queue positions about every 2 seconds. A newly submitted request may therefore have
`queue_position: 0` and `wait_time: 0` even when other requests are ahead.

The wait-time check uses the first status with queue information: a positive `queue_position` or `wait_time`,
or a processing or finished generation. Until then, the front reads the status again every `signal_recheck_seconds`,
for at most `POSITIONED_READ_WAIT_SECONDS` (2 seconds, `horde/oai/waiter.py`) after the first read.
At that deadline it uses the latest read. With zero eligible threads, the request does not need a queue position
and is refused on the first read. Later reads use the same grace period as the `wait` mode.

The native `wait_time` counts queued requests ahead with overlapping models that the eligible workers could serve,
then divides that work by the throughput of `eligible_worker_threads`. This is an approximation of the workers'
candidate queues, so a request may start sooner or wait longer. See
[Queue position and wait time](../explanation/request_feasibility_and_queue_pressure.md#queue-position-and-wait-time).

### Stream errors

A streamed request's response opens only after the first-read decision. A refusal at that point (a declaration or
limit gap, `queue_unavailable`, or a status error such as 404) therefore has the same HTTP status, body and
`Retry-After` as on a non-streamed request. The front also cancels the request in the same way. An error after the
stream's 200 headers, such as `no_eligible_worker` after the grace, is sent as the SSE error event. Its
`metadata.retry_after` holds the interval that the `Retry-After` header would have held.
An unrecognized `impossible_reason` kind is ignored, so the grace applies.

## Kudos cap

The front forwards `horde.max_kudos` with the request. When the kudos estimate exceeds the cap, the native route
refuses the request before creating it (`ChatKudosCapExceeded`, see [Request body](chat_requests.md#request-body)).
`_kudos_cap_error` in `horde/oai/app.py` returns 400 `invalid_request_error` with code `max_kudos_exceeded`, `param`
`horde.max_kudos`, the native message, and `metadata.rc`, `metadata.kudos_estimate` and `metadata.max_kudos`. The
native error body holds only the message and the `rc`. The front therefore reads both values from the message with
`KUDOS_CAP_EXCESS_PATTERN` in `horde/chat_contract.py`, which is the module that writes the message. If the message
does not match, both values are left out of `metadata`.

```json
--8<-- "docs/reference/chat_wire_examples.md:front-kudos-cap"
```

## Model card fields

Example: [Model list](chat_wire_examples.md#model-list).

`GET /v1/models` lists each text model with at least one online chat worker, from the Horde's
`GET /v2/status/models?type=text&chat=true`. Each process caches the list for five seconds
(`MODEL_LIST_CACHE_SECONDS`). `build_model_card` in `horde/oai/mapping.py` builds each entry from the model's `chat`
object in that list:

| Field | Value |
| --- | --- |
| `id`, `object`, `owned_by` | The model name, `model`, `aihorde`. |
| `created` | The Horde's `chat.created`, or `0` when the Horde does not record one. |
| `context_length`, `top_provider.context_length` | `chat.max_context_length`, the largest value declared by an online chat worker. |
| `top_provider.max_completion_tokens` | `chat.max_length`, the largest value declared by an online chat worker. |
| `architecture` | Text in and out. With the `vision` feature, `input_modalities` adds `image` and `modality` is `text+image->text`. |
| `supported_parameters` | Always `max_tokens`, `temperature`, `top_p` and `stop`. Then `seed`, `frequency_penalty`, `presence_penalty` and `reasoning_effort` for each declared feature with that name. `tools` and `parallel_tool_calls` for the `tools` feature, and with them `tool_choice` under the `tool_choice_enforced` guarantee. `response_format` for `json_object` or `json_schema`. |
| `x_horde.workers` | `chat.workers`, the number of online chat workers serving the model. |
| `x_horde.queued` | The model's queue, which chat requests share with text requests. Absent when the Horde does not report one. |

The limits and supported parameters describe the workers online when the list was built. Availability can change
before a request is assigned, and a combination of individually supported options may not match any one worker.

## Settings

`FrontSettings` in `horde/oai/settings.py` reads every setting from a `HORDE_OAI_*` environment variable at start, and
from a `.env` file in the working directory. When a variable is set but invalid, the front does not start, and the
validation error identifies the variable. The telemetry variables are listed in
[Export telemetry](../how-to/run_openai_chat_front.md#export-telemetry).

### Connections and listening

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_HORDE_URLS` | `http://127.0.0.1:7001/api` | Comma-separated API root URLs of the primary Horde instances, used round-robin. A Horde instance that refuses the connection is skipped for that call. |
| `HORDE_OAI_FALLBACK_HORDE_URLS` | empty | Comma-separated API root URLs of the fallback Horde instances, used round-robin only when every primary Horde instance is unusable. A URL cannot be in both lists. |
| `HORDE_OAI_DRAIN_AGENT_PORT` | unset | TCP port of the drain agent on the host of each Horde instance. The front sends the Horde instance's port and a newline, and reads `ready` or `drain`, as HAProxy's `agent-check` does. The front does not send new calls to a drained Horde instance. When it is unset, the front does not poll and every Horde instance counts as ready. |
| `HORDE_OAI_DRAIN_AGENT_POLL_SECONDS` | `1` | How often the drain agent of every Horde instance is polled, concurrently. One query may take half of this (`DRAIN_AGENT_TIMEOUT_FRACTION`). |
| `HORDE_OAI_LISTEN` | `0.0.0.0` | Address to bind. |
| `HORDE_OAI_PORT` | `7002` | Port to bind. |
| `HORDE_OAI_REDIS_URL` | unset | URL of the Horde's primary Redis, such as `redis://redis:6379/7`. Pub/sub ignores the logical database, so only the host matters. When it is unset, every request relies on the fallback poll alone, and streams are not relayed. |
| `HORDE_OAI_STREAM_REDIS_DB` | `7` | The logical database for chat content deltas on the host of `HORDE_OAI_REDIS_URL`. This is the Horde's chat signal database. The front reads it on a separate client, whatever database the URL selects. Unused without `HORDE_OAI_REDIS_URL`. |
| `HORDE_OAI_REDIS_LIVENESS_SECONDS` | `30` | How long the signal subscription may stay idle before the front sends a PING, and how long the PONG may take. A missing PONG sets `signal_subscribed: false` on `/healthz` and the front subscribes again, so an unresponsive connection is detected even when the subscription is idle. The front also turns on TCP keepalive for this connection. |
| `HORDE_OAI_PROXY_PASSKEY` | unset | The front operator's service account proxy passkey. When set, the front sends it as `Proxy-Authorization` with the client's address as `Proxied-For`, so IP timeouts and suspicion apply to the client. |
| `HORDE_OAI_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Addresses uvicorn trusts for `X-Forwarded-For`. Set it to the listener's addresses. The resulting client address is the per-source admission key and the `Proxied-For` value. |

### Admission and request limits

See [front response budgets and caps](service_limits.md#chat-front-deadlines-and-admission) for their purpose and
interaction with native request expiry, and [connection and cancellation controls](operational_constants.md#front-connections-and-cancellation)
for the separate pools and retry budgets.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_MAX_INFLIGHT` | `1000` | Open requests per process before 503 `FrontAtCapacity`. A request holds its slot until it ends, including its cancel. When the Horde stalls and requests wait to their deadline, the cap is reached once requests arrive faster than this value divided by the request budget (1000 over 300 seconds is 3.3 per second). The cap detects a stall and does not limit throughput. The value also sizes the cancel pool: one cancel slot per 20 open requests, at least 16 (`cancel_max_connections`). |
| `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE` | `5` | Open requests per client address per process before 503 `FrontAtCapacity`. Checked before the global cap, and before any of the body is read. Users behind one egress address share it. |
| `HORDE_OAI_MAX_BODY_BYTES` | `37748736` | Largest request body, counted while it is received and before JSON is parsed. A larger body gets 413. The default is the native route's 36 MiB (`MAX_CHAT_BODY_BYTES`), room for 25 MiB of images as data URLs plus the text limit. |
| `HORDE_OAI_MAX_MESSAGE_COUNT` | `1024` | Most messages per request, the native route's limit. More gets 400. |
| `HORDE_OAI_MAX_TEXT_BYTES` | `1048576` | Most UTF-8 bytes of message text per request, including the function names and arguments of history tool calls. This is the native route's limit. More gets 400. |
| `HORDE_OAI_BODY_READ_TIMEOUT_SECONDS` | `30` | Time allowed to receive the body. A slower body gets 408 `RequestBodyTimeout`. |

### Request budgets and cancellation

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS` | `300` | Total budget of a non-streamed request, from before the submit. |
| `HORDE_OAI_STREAM_TIMEOUT_SECONDS` | `600` | Total budget of a streamed request, from before the submit. |
| `HORDE_OAI_FINAL_STATUS_TIMEOUT_SECONDS` | `2` | Allowance for the one status read made at the deadline. |
| `HORDE_OAI_CANCEL_TIMEOUT_SECONDS` | `10` | Budget of one cancel's HTTP call. The wait for a free cancel slot is limited to the same length. A cancel that does not get a slot in time is not sent and is retried. |
| `HORDE_OAI_CANCEL_RETRY_SECONDS` | `30` | How often cancels that failed, timed out or did not start are sent again, until the front's fixed cutoff 20 minutes after registering the submitted request. This cutoff does not track native request refreshes; see [cancellation controls](operational_constants.md#front-connections-and-cancellation). |
| `HORDE_OAI_SHUTDOWN_DRAIN_SECONDS` | `30` | How long uvicorn waits for open requests on a clean stop before cancelling their handlers. |
| `HORDE_OAI_SHUTDOWN_CANCEL_SECONDS` | `15` | Process-wide limit for cancelling every request still open at stop. |

### Streaming and status polling

The [polling and stream-read constants](operational_constants.md#polling-signals-and-stream-reads) identify the
fixed stretching, jitter and batching values used with these settings.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_HEARTBEAT_SECONDS` | `5` | Interval of the keepalive comment on a streamed response. |
| `HORDE_OAI_DELTA_PACING_MAX_WINDOW_SECONDS` | `2` | Longest window over which a relayed stream spreads the text of one worker progress post. See [Pacing](#pacing). `0` sends each post as one chunk when it arrives. |
| `HORDE_OAI_DELTA_PACING_MAX_CHUNKS_PER_SECOND` | `20` | Maximum number of content chunks per second of window in a paced stream. |
| `HORDE_OAI_FALLBACK_POLL_SECONDS` | `15` | Base status poll interval when no signal arrives. It stretches to half the Horde's `wait_time`, at most 60 seconds, and is shortened at random by up to a fifth. |
| `HORDE_OAI_SIGNAL_RECHECK_SECONDS` | `0.5` | Delay of the one extra status read after a terminal signal, when the first read did not show the outcome. |
| `HORDE_OAI_FAIL_FAST_GRACE_SECONDS` | `20` | How long the Horde may report a lack of eligible workers, without a processing generation, before the request is cancelled and gets 503 `no_eligible_worker`. |
| `HORDE_OAI_SUBMIT_RECOVERY_ATTEMPTS` | `2` | How many times a submit with a lost response is sent again with the same operation key, within the request's budget. `0` returns 502 at once. See [Lost submits](../how-to/run_openai_chat_front.md#lost-submits). |

### Connection pool

| Variable | Default | Meaning |
| --- | --- | --- |
| `HORDE_OAI_NATIVE_MAX_CONNECTIONS` | `200` | Simultaneous submits, status reads and model lists to the Horde, and the size of their pool. 32 of these (`SUBMIT_MAX_CONCURRENT_CALLS`) are reserved for submits, so status reads cannot hold back a new request's submit. The value must be above 32. Cancels use a separate pool sized from `HORDE_OAI_MAX_INFLIGHT`. |

## Telemetry

### Configuration {#telemetry-configuration}

`configure_telemetry` runs before the application is built. It configures Logfire when
`horde.telemetry.telemetry_enabled` is true and `OTEL_SDK_DISABLED` is not `true`. Logfire then uses the same
variables, resource attributes and sampling as the Flask application, the service name `horde-oai` unless
`OTEL_SERVICE_NAME` sets another, and the views from `horde.oai.metrics.histogram_views`. `configure_telemetry` also
bridges Loguru records to OpenTelemetry logs and instruments httpx, so each call to the horde is a client span. The
horde's Flask instrumentation continues the trace from that span's `traceparent`. The Flask application's
`init_telemetry_early` is not reused because it instruments a Flask app.

With `PYROSCOPE_ENABLED=true`, `configure_telemetry` starts Pyroscope through `horde.telemetry.init_pyroscope`, which
the Flask application also uses. Pyroscope gets the same `PYROSCOPE_*` variables, with the front's service name as
the application name. Pyroscope also starts when `OTEL_SDK_DISABLED=true`, but span profiles need the SDK.

`instrument_app` adds the server spans (`POST /v1/chat/completions`, `GET /v1/models`) once `create_app` has built
the application. `main` calls it only when telemetry was configured. `/healthz` is excluded, since the listener and
the container poll it every few seconds. The instrumentation does not capture headers or bodies. A streamed request's
span lasts until the stream ends.

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

Attribute keys include the `horde.` prefix. Every attribute has a bounded set of values: call types, outcomes, wake
sources, HTTP statuses, error codes, outcome classes, refusal reasons, booleans and the model id. The model id comes
from a delivered completion, so it identifies a served model. Client addresses, API keys, prompts, client titles and
request ids are not used as attributes.

| Name | Type | Attributes | Meaning |
| --- | --- | --- | --- |
| `horde.oai.requests.admitted` | counter | `streamed` | Chat requests that took an in-flight slot. |
| `horde.oai.requests.refused` | counter | `status_code`, `code`, `parameter`, `outcome_class` | Refusals during admission or body validation. See [Refusal attributes](#refusal-attributes). |
| `horde.oai.requests.errors` | counter | `status_code`, `code`, `streamed`, `reason`, `stage`, `max_tokens_bucket`, `outcome_class` | Admitted requests ending in an HTTP error. See [Error attributes](#error-attributes). |
| `horde.oai.stream.failures` | counter | `code`, `stage`, `outcome_class` | Streamed requests that ended in an SSE error event after the 200 headers were sent. `stage` is as for `horde.oai.requests.errors`. |
| `horde.oai.fail_fast.rescues` | counter | none | Waits in which one status read did not report an eligible worker or a processing generation, and a later read within `fail_fast_grace_seconds` showed an eligible worker or the request processing. Comparing it with the `no_eligible_worker` count at the `grace` stage shows how many requests the grace saved from a refusal. |
| `horde.oai.inflight` | observable gauge | none | Requests holding an in-flight slot. |
| `horde.oai.inflight.sources` | observable gauge | none | Distinct client addresses holding at least one slot. |
| `horde.oai.inflight.source_max` | observable gauge | none | Maximum slots held by one client address. Compare with `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE`. |
| `horde.oai.request.body_bytes` | histogram, bytes | none | Size of each request body received whole. Oversized bodies are counted as refusals instead. |
| `horde.oai.native.duration` | histogram, seconds | `call_type`, `outcome` | Latency of each native call from taking a call slot to receiving the response. The histogram count is the call count. `call_type` is `submit`, `recovery`, `status`, `cancel` or `models`. `outcome` is `ok`, `refused` (horde status 400 or above), `unreachable` or `abandoned` (the request's budget ended first). |
| `horde.oai.queue_wait` | histogram, seconds | `model` | The horde's `queue_wait_seconds` of each delivered generation: request creation to assignment. |
| `horde.oai.first_content.duration` | histogram, seconds | `streamed`, `relayed` | From the submit acknowledgement, when a stream's headers go out, to the first content sent: the first relayed delta (`relayed` true), the first completion chunk, or the non-streamed body. |
| `horde.oai.request.duration` | histogram, seconds | `streamed`, `status_code`, `code`, `outcome_class` | From an admitted request's arrival to its end. A stream records 200 whatever its outcome. `code` is the error code, `ok` for a delivered completion or `client_closed` for a client that disconnected before delivery. |
| `horde.oai.wakes` | counter | `wake_source`, `found_finished`, `found_done_without_generation` | Each status read of a waiting request, by what caused it: `immediate` (the read after the submit), `signal`, `recheck`, `poll`, `deadline`. `disconnect` is recorded without a read when the client disconnects. `found_done_without_generation` marks a read showing the request done, not faulted and without a generation. |
| `horde.oai.submit.outcome_unknown` | counter | none | Responses with `submit_outcome: "unknown"`. |
| `horde.oai.submit.recoveries` | counter | `existing_claim` | Lost submits recovered by re-sending. `existing_claim` is true when the Horde returned an existing claim (200) and false when it returned 202. First-try submits are not counted. |
| `horde.oai.client_retry_hints` | counter | none | Chat requests with `x-stainless-retry-count` above zero, the header sent by OpenAI SDKs on their own retries. |
| `horde.oai.completions` | counter | `model`, `estimated_usage`, `finish_reason`, `max_length_lowered`, `content`, `reasoning_effort` | Delivered completions. See [Completion attributes](#completion-attributes). |
| `horde.oai.completions.empty` | counter | `model` | Delivered completions whose generation does not include visible text. `x_horde.notice` reports the same case. |
| `horde.oai.request.max_tokens` | histogram, tokens | `source` | Accepted request's completion limit. See [Completion limit source](#completion-limit-source). |
| `horde.oai.reasoning_share` | histogram, ratio | `model`, `finish_reason` | For each delivered completion with reasoning tokens: `usage.completion_tokens_details.reasoning_tokens` divided by `usage.completion_tokens`. Both counts are estimates when the worker did not report them. A share near 1 with `finish_reason` `length` means a reply that spent its budget on reasoning. |
| `horde.oai.stream.relayed` | counter | none | Streamed requests that sent at least one relayed delta. |
| `horde.oai.stream.relay_outcomes` | counter | `outcome` | How relayed streams ended: `equal`, `prefix`, `mismatch`, `replaced_attempt`, `ended` or `gap`. See [Relayed deltas](#relayed-deltas). |
| `horde.oai.stream.post_gap` | histogram, seconds | none | Gap between the stream id append times of consecutive accepted worker progress posts in a relayed stream. The pacing window is estimated from these gaps. A negative gap, from a stream recreated with lower ids, is recorded as 0. |
| `horde.oai.stream.pieces_per_post` | histogram | none | Number of pieces sent for one accepted post. A post sent whole counts 1. A paced post, or a post merged into the next window, counts more. |
| `horde.oai.stream.pacing_lag` | histogram, seconds | none | Time from a post's stream id append time to the sending of the last piece of its text. It is measured on the front's Unix clock, so clock skew between Redis and the front shifts it. It includes the reader's read delay. A negative lag is recorded as 0. |
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
`HORDE_OAI_CANCEL_RETRY_SECONDS` and once more during shutdown. Retries stop 20 minutes after the front registers the
submitted request
(`horde.oai.app.HORDE_CHAT_REQUEST_EXPIRY_SECONDS`). At most `horde.oai.app.MAX_FAILED_CANCELS` requests whose
responses were already returned stay in the retry queue. The oldest entries are dropped first.

That cutoff uses the front's registration timestamp, while [native request expiry](service_limits.md#request-lifetime-and-expiry) can
be refreshed by assignment and settlement. Dropping a retry therefore does not establish that the request is gone.

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

`horde.oai.requests.errors` counts admitted requests that got an error status before any header was sent. These
include horde refusals at submit (429 `rate_limit_exceeded` when the horde's 429 does not include an `rc`), 408
`ChatRequestTimeout`, 404 `no_worker_supports_features`, 400 `no_worker_supports_limit`, 503 `no_eligible_worker`,
503 `queue_unavailable`, 502 `ChatRequestFaulted`, `ChatRequestNoGeneration` and `HordeUnavailable`.

`reason` is `no_eligible_threads` or `wait_exceeds_budget` for `queue_unavailable` and empty for every other code.

`stage` is `submit` for a `no_eligible_worker` refusal by the Horde at creation, such as for a model without
an online chat worker. It is `grace` for a `no_eligible_worker` returned when the fail-fast grace ended, and
empty for every other code.

`max_tokens_bucket` is the request's completion limit, as in the finished line's `max_tokens`, reported as
the upper bound of its `BUCKETS_MAX_TOKENS` bucket (`16`, `64`, `128`, `256`, `512`, `768`, `1024`,
`2048`, `4096`, `8192`, `16384`), or `over` above 16384. A limit of 600 is `768`. It shows refusals grouped by the
limit requested by clients.

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
`finish_reason: length` used up the output budget before producing a visible reply. The OpenAI API cannot resume
that reasoning.

#### Completion limit source

`horde.oai.request.max_tokens` records the completion limit of each request accepted by the Horde at submit. It is
recorded once, when the request ends. A request refused at submit did not run under a limit and is not recorded.

`source` is `client` (the client sent `max_tokens` or `max_completion_tokens`), `default` (the client sent
neither, so the limit is 512) or `lowered` (the Horde lowered the limit to the largest one declared by an
online worker, the `worker_max_length` context adjustment).

The Horde reports a lowered limit only in a status, so a request that ended before any status read is recorded as
`client` or `default`.

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
eligible worker did not take it within the grace period. This is classed as `capacity`. At `submit`, the Horde
refused the request because it could not be served by any online worker. This is classed as `unservable`. The same
request can become servable when a volunteer worker comes online.

The front passes the Horde's `rc` through as the code, so the code set is open. A code not in the table is
`unclassified` and is not given a default class, so a new return code stays visible.

The front does not measure settlement lag between delivery and the native terminal state, since the Horde's
settlement is not visible to the front.

`FrontAtCapacity` means that the in-flight cap per client address, or the total in-flight cap, was reached. Its count
rises with client concurrency and also when served requests wait longer, so it has its own class.

The served share of servable requests is `served` divided by the sum of `served`, `capacity` and `service_fault`.

### Refused parameter log line

The front logs one Loguru `INFO` record for each `UnsupportedChatParameter` refusal:

```text
Refused unsupported chat parameter: path=<repr of the request path> label=<horde.parameter label>
```

The path is the refused request path, such as `'top_k'` or `'messages[0].content[1].type'`. It is cut to
`REFUSED_PARAMETER_LOG_LENGTH` (64) characters and written in Python `repr` form, so a client-chosen key cannot make
the line long or split it. `label` is the `horde.parameter` value of the refusal counter. The lines with
`label=other` show the keys counted as `other`. From them, an operator can check whether
`COMMON_NON_OPENAI_SAMPLER_PARAMETERS` covers the keys sent by clients.

### Finished-request log line

The front logs one Loguru `INFO` record for every admitted request when the request ends. The message format is:

```text
Chat request <id> finished: streamed=<bool> submitted_at=<unix> woke_at=<unix|none> woke_by=<source|None> waited=<seconds|none>s status=<status> code=<code> wakes=<count> estimated_usage=<bool|None> done_without_generation_reads=<count> requested_model=<models> served_model=<model|none> retry_after=<seconds|none>
```

For a `queue_unavailable` refusal, the line also includes
`queue_reason=<reason> wait_time=<seconds|none> eligible_worker_threads=<count|none>`. Every line then ends with
`first_read_wait_time=<seconds|none> first_read_eligible_worker_threads=<count|none> max_tokens=<limit> max_tokens_source=<client|default|lowered>`.

The same values are bound to the record's `extra`, which the OpenTelemetry log bridge exports as attributes. A
request refused at submit does not have an id. Its message starts with `Unsubmitted chat request finished:`.
`tests/stress/chat_scale_demo.py` parses the message up to `waited`.

| Field | Meaning |
| --- | --- |
| `request_id` | The horde request id, or null before the submit was acknowledged. |
| `streamed` | Whether the request was streamed. |
| `submitted_at` | Wall-clock time of the submit acknowledgement, Unix seconds. |
| `woke_at` | Wall-clock time of the status read that showed the request finished. Null when a read did not show a finished request. |
| `woke_by` | The wake source of that read, or `disconnect`. Null for a timeout, fail-fast or horde error. |
| `waited_seconds` | `woke_at` minus `submitted_at`, or the time until the end when a read did not show the request finished. |
| `status_code` | The HTTP status sent: 200 for every stream whose headers went out, 499 for a non-streamed client that disconnected. |
| `code` | The error code, `ok` or `client_closed`. |
| `wake_count` | Number of status reads during the wait. |
| `estimated_usage` | Whether the delivered completion's usage was estimated. Null when a completion was not delivered. |
| `done_without_generation_reads` | How many status reads showed the request done, not faulted and without a generation. |
| `requested_model` | The models the request accepts, comma-separated. |
| `served_model` | The model of the delivered completion. Null when a completion was not delivered. |
| `retry_after` | The `Retry-After` value in the error response or event, in seconds. Null when it was not included. |
| `queue_reason` | `queue_unavailable` only: `no_eligible_threads` or `wait_exceeds_budget`. |
| `wait_time` | `queue_unavailable` only: the wait estimate from the status read used for admission. Null when the read did not report a value. |
| `eligible_worker_threads` | `queue_unavailable` only: the eligible worker thread count from that status read. Null when the read did not report a value. |
| `first_read_wait_time` | `fail_fast` only: the wait estimate of the status read used for the first-read decision, whether the request was admitted or refused. Null in the `wait` mode, before a decision, or when the read did not report a value. |
| `first_read_eligible_worker_threads` | `fail_fast` only: the eligible worker threads that read reported. Null as for `first_read_wait_time`. |
| `max_tokens` | The request's effective completion limit: the Horde's lowered limit from the completion's status or the last status read, else the client's `max_tokens` or `max_completion_tokens`, else 512. |
| `max_tokens_source` | Where `max_tokens` came from, as the `source` of `horde.oai.request.max_tokens`: `client`, `default` or `lowered`. |

The line does not include the API key, the messages, the client's title or address, or the operation key.

### No-visible-text log line

For a delivered completion whose `horde.oai.completions` `content` is `reasoning_only` or `empty`, the front also
logs one Loguru `INFO` record before the finished-request line:

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

`tests/integration/test_chat_front_sdk.py` tests the front through the official OpenAI Python SDK. [Check the front
against the OpenAI SDK](../how-to/run_openai_chat_front.md#check-the-front-against-the-openai-sdk) gives the command.

| Contract | Test |
| --- | --- |
| Model list | `test_the_model_list_includes_the_fake_workers_model` |
| Completion and usage | `test_a_completion_returns_the_workers_reply_with_its_usage` |
| Stream event order | `test_a_streamed_completion_yields_role_content_finish_and_usage_chunks` |
| Unsupported parameter path | `test_logit_bias_is_refused_with_its_param` |
| Unserved model and SDK retry behavior | `test_a_model_without_an_online_worker_fails_fast_without_an_sdk_retry` |
| Image URL transport | `test_an_image_reaches_a_url_worker_as_a_signed_url` |
| Tool call round | `test_a_tool_call_round_returns_the_call_and_then_answers_the_result` |
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
| Image limits before the submit | `horde/oai/app.py` | `_find_image_limit_error` |
| Native body, completion, usage estimate | `horde/oai/mapping.py` | `build_native_chat_body`, `build_completion`, `estimate_tokens` |
| Model list entries | `horde/oai/mapping.py` | `build_model_card` |
| Streamed chunks and keepalive | `horde/oai/sse.py` | `build_completion_events`, `build_delta_events`, `build_keepalive_event`, `build_error_events` |
| Delta relay and reconciliation | `horde/oai/app.py`, `horde/oai/mapping.py` | `DeltaRelay`, `_relay_deltas`, `relays_deltas` |
| `Client-Agent` attribution | `horde/oai/attribution.py` | `build_client_agent` |
| Native HTTP client and instance rotation | `horde/oai/horde_client.py` | `HordeClient`, `create_http_client`, `cancel_max_connections` |
| Completion signal subscription and liveness ping | `horde/oai/waiter.py` | `CompletionSignalListener` |
| Delta stream reader | `horde/oai/waiter.py` | `DeltaStreamReader`, `DeltaSubscription`, `DeltaEntry` |
| Per-request wait, poll and deadline | `horde/oai/waiter.py` | `ChatRequestWatcher`, `WaitBudget`, `WakeSource` |
| No eligible worker and declaration gap | `horde/oai/waiter.py`, `horde/oai/app.py` | `NoEligibleWorkerError`, `DeclarationGapError`, `_wait_error` |
| Instruments and bucket boundaries | `horde/oai/metrics.py` | `FrontMetrics`, `histogram_views` |
