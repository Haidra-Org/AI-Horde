---
title: "Send chat completions through the OpenAI-compatible front"
summary: "Point an existing OpenAI client at the Horde: base URL, key and attribution, model choice, accepted and refused keys, the horde object, images, tools, structured output, streams, errors and limits."
topics: [chat, requests]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Send chat completions through the OpenAI-compatible front

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [chat](../topics.md#chat), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

Preconditions: an OpenAI chat completions client in any language that lets you set the base URL and the bearer token;
the base URL of a Horde chat front, written here as `OAI_BASE_URL` (for the public horde,
`https://oai.aihorde.net/v1`); and a Horde API key, written as `HORDE_API_KEY`, or the anonymous key `0000000000`.

End state: the client sends `POST /v1/chat/completions` to the Horde, streamed or not, with tools, images and
structured output where a served model supports them, and it handles the Horde's refusals and retry headers without
creating duplicate charged jobs.

The front refuses with a 400 any top-level key it does not serve, so a reply never silently differs from what was
asked. Unknown keys inside messages, `tools` definitions and a `tool_choice` object are dropped, as OpenAI does. The
reasons are in the [chat requests explanation](../explanation/chat_requests.md) and
[ADR 0029](../decisions/0029-route-unsupported-parameters-by-declared-support-refusing-by-default.md). The
[tutorial](../tutorial/first_chat_completion.md) walks the same features with the Python SDK from scratch.

## Configure base URL, key and attribution

Set the client's base URL to `OAI_BASE_URL` and its API key to the Horde API key; the client sends it as
`Authorization: Bearer <key>`. With the Python SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="<OAI_BASE_URL>", api_key="<HORDE_API_KEY>")
```

A request without `Authorization` uses the anonymous key, and so does `Bearer 0000000000`. Anonymous requests are
served after requests from users with kudos ([ADR 0035](../decisions/0035-keep-anonymous-access-on-the-chat-front.md)).
An `Authorization` header that is not `Bearer <key>` is 401 `InvalidAuthorizationHeader`.

The Horde attributes traffic by `Client-Agent`, which the front composes from your headers as
`<application>:<version>:<user agent> via horde-oai-front`. The application is your `X-Title` header, else
`X-OpenRouter-Title`, else the host of `HTTP-Referer`, else the host of `Referer`; the user agent is your
`User-Agent`. Send `X-Title` with your application's name so Horde statistics count your traffic under it:

```python
client = OpenAI(base_url="<OAI_BASE_URL>", api_key="<HORDE_API_KEY>", default_headers={"X-Title": "<your-app>"})
```

Verification: `GET /v1/models` with the key returns 200:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $HORDE_API_KEY" "$OAI_BASE_URL/models"
```

prints `200`. A malformed header prints `401`.

## Choose a model from the model card

`GET /v1/models` lists each model at least one online chat worker serves; a model with no worker online is absent.
Model ids are the Horde's model names and may contain slashes; an id containing `:` is 404 `model_not_found`, since
the front does not serve model variants.

```bash
curl -s "$OAI_BASE_URL/models" | jq '.data[] | {id, context_length, max: .top_provider.max_completion_tokens, supported_parameters, x_horde}'
```

| Field | Meaning for a request |
| --- | --- |
| `x_horde.workers` | Online chat workers serving the model. |
| `x_horde.queued` | The model's queued work, shared with text requests. |
| `context_length` | The largest context any of those workers declares. |
| `top_provider.max_completion_tokens` | The largest `max_tokens` any of those workers declares. |
| `supported_parameters` | `max_tokens`, `temperature`, `top_p`, `stop`; then `seed`, `frequency_penalty`, `presence_penalty`, `tools`, `response_format` when a worker declares the feature; `tool_choice` and `parallel_tool_calls` only when a worker serving tools also declares the guarantee that enforces them (see [Conformance](#conformance)). |
| `architecture.input_modalities` | Includes `image` when a worker accepts images. |

The fields are the maxima and union across the model's workers, and they are availability hints: a request at the
largest `max_tokens` fits only the workers that declare it, and the workers online can change before the request is
picked up. A request that no online worker can serve ends with 503 `no_eligible_worker`, or at once with 404
`no_worker_supports_features` when the model's online workers lack a declaration it needs (see
[Errors and retries](#errors-and-retries)).

Verification: the model you configured appears in `.data[].id`, with `x_horde.workers` at least 1 and every optional
parameter your client sends listed in `supported_parameters`.

## Request keys the front accepts

| Keys | Handling |
| --- | --- |
| `model`, `messages` | Required; `messages` holds at least one message. |
| `max_tokens`, `max_completion_tokens` | 16 to 4096; equal when both are sent. Default 512 when neither is. |
| `temperature`, `top_p`, `stop` | Forwarded; `stop` is a string or up to four strings. |
| `seed`, `frequency_penalty`, `presence_penalty` | Forwarded; a worker has to declare the feature for `seed` (any value) or a non-zero penalty. Penalties range -2 to 2. |
| `n` | Only `1`. |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format` | Forwarded unchanged to a worker that declares the feature; see [Tools](#tools-and-tool_choice) and [Structured output](#structured-output). |
| `stream`, `stream_options` | Applied by the front; `stream_options` needs `"stream": true`. |
| `user`, `metadata` | Accepted and unused. |
| `horde` | The Horde extension object, below. |

Message roles are `system`, `developer`, `user`, `assistant` and `tool`, with these keys:

| Role | Keys |
| --- | --- |
| `system`, `developer`, `user` | `role`, `content`, `name` |
| `assistant` | `role`, `content`, `name`, `tool_calls` |
| `tool` | `role`, `content`, `tool_call_id` |

A `developer` message is sent to the worker as `system`. Content is a string or an array of `text` parts; a `user`
message may also carry `image_url` parts.

Any other key on a message, and any unknown key inside a tool call, its `function` object or a content part, is
dropped and not forwarded, as OpenAI ignores it. A `name` on a `tool` message is dropped. `tool_calls` on a message
other than an assistant message is refused, as OpenAI refuses it.

Stock clients send some keys at their defaults. The front accepts these and drops them before the Horde sees them:
empty or null `tools`, null `tool_choice`, null `parallel_tool_calls`, `response_format: {"type": "text"}`,
`logprobs: false`, `top_logprobs: 0`, `store: false`, and `service_tier` `"auto"` or `"default"`.

Verification: a request carrying only `model`, `messages` and the keys above returns 200 with a `chat.completion`
body.

## Keys the front refuses

Any other key, and any of the dropped keys at another value, is 400 `UnsupportedChatParameter`, with `param` holding
the key's path. Examples: `logit_bias`, `logprobs: true`, `functions`, `function_call`, `audio`, `modalities`,
`prediction`, `web_search_options`, `models`, `reasoning_effort`, and sampler extras such as `rep_pen`, `top_k` and
`min_p`. An unknown key inside `stream_options` or `response_format` is refused at its path, as OpenAI refuses it.
`tool_calls` on a message other than an assistant message is refused at `messages[<i>].tool_calls`, and a role other
than the five above at `messages[<i>].role`. A refused content part is reported with its type, such as
`messages[0].content[0].type=input_audio`.

Other 400 codes:

| `code` | Cause |
| --- | --- |
| `ChatRequestConflict` | `max_tokens` and `max_completion_tokens` differ; `horde.only` and `horde.ignore` are both set; a `tool_choice` (`none` included) or `parallel_tool_calls` without tools, as OpenAI refuses them; a forced function the tools do not define; under `horde.openai_guards`, a `json_object` request without the word "json" in its messages. |
| `BadRequest` | The body is not a JSON object, a value has the wrong type or range, `messages` is empty, or a `json_schema` response format has no `schema`. |

Each refusal happens before the request is queued, so it is not charged and is safe to correct and resend.

Verification:

```bash
curl -s "$OAI_BASE_URL/chat/completions" -H "Authorization: Bearer $HORDE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "<model>", "messages": [{"role": "user", "content": "Hi"}], "logit_bias": {"50256": -100}}' | jq .error
```

returns `"code": "UnsupportedChatParameter"` and `"param": "logit_bias"`. The Python SDK raises `BadRequestError`
with `param == "logit_bias"`.

## Sampling defaults and the horde object

The worker receives every sampling value, so the worker's defaults never apply. A key you omit is sent as the neutral
value: `temperature` 1.0, `top_p` 1.0, `frequency_penalty` 0, `presence_penalty` 0, and no repetition penalty,
`top_k` or `min_p` filtering. `max_tokens` defaults to 512. Send `temperature` and `top_p` explicitly when the model's
recommended settings differ.

The `horde` object carries the Horde options OpenAI does not have a key for:

| Key | Default | Effect |
| --- | --- | --- |
| `only` | none | A non-empty list of worker ids; only these workers serve the request. |
| `ignore` | none | A non-empty list of worker ids; these workers never serve it. Cannot be combined with `only`. |
| `trusted_workers` | `false` | `true` restricts the request to trusted workers. |
| `validated_backends` | `false` | `true` restricts the request to workers on validated backends. |
| `slow_workers` | `true` | `false` excludes workers the Horde marks slow. |
| `max_context_length` | none | An explicit context length; a worker serves the request only when its declared chat context is at least this. |
| `context_fit` | `grow` | `grow` raises the context to fit the messages, up to the largest chat context an online worker for the model declares. `reject` refuses messages that do not fit the context. |
| `conformance` | `strict` | `strict` sends the request only to workers that follow every part of the OpenAI contract it relies on. `best_effort` also accepts workers that may deviate; see [Conformance](#conformance). |
| `openai_guards` | `false` | `true` turns on the request refusals OpenAI applies and the Horde relaxes on purpose: a `json_object` response format whose messages never mention "json" is 400 `ChatRequestConflict`. |

Any other key in `horde`, including `dry_run` and `operation_key`, is 400 `UnsupportedChatParameter`. Restrictions
narrow the eligible workers, so a combination no online worker satisfies ends with `no_eligible_worker`.

With the Python SDK, send the object as `extra_body={"horde": {...}}`; other clients add it to the JSON body.

Verification: `x_horde.worker_id` of the response is one of the `only` ids, and `x_horde.context_adjustments` lists
`{"from", "to", "reason": "grow"}` when `grow` raised the context, null otherwise.

## Images

A `user` message may carry `{"type": "image_url", "image_url": {"url": ..., "detail": ...}}` parts. `url` is a
`data:image/...;base64,` URL or a public `http(s)` URL; `detail` is optional (`auto`, `low`, `high`) and passed to the
worker. Send images only to a model whose `architecture.input_modalities` includes `image`.

| Bound | Refusal |
| --- | --- |
| At most 5 images per request | 400 `TooManyExtraSourceImages` |
| More than 1 only for trusted users and patrons | 400 `MoreThanMinExtraSourceImage` |
| At most 5 MiB per image, decoded or fetched | 400 `SourceImageSizeExceeded`, `param` at the part's `url` |
| At most 3072 by 3072 pixels | 400 `SourceImageResolutionExceeded` |
| A URL at a private, loopback or reserved address | 400 `SourceImageUrlRefused` |
| A URL answering other than 200 | 400 `SourceImageUrlInvalid` |
| Not readable as an image | 400 `SourceImageUnreadable` |

The Horde fetches a URL once at submit, with 2-second timeouts and at most 3 redirects, and stores each image for
the worker; each image adds 5 kudos to the request's cost. Image parts in any other role are refused with their path,
such as `messages[1].content[0].type=image_url`.

Verification: the reply describes the image, and `x_horde.kudos` is higher than for the same request without it.

## Tools and tool_choice

`tools` holds up to 128 functions, `{"type": "function", "function": {"name", "description", "parameters",
"strict"}}`, each `name` 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `_` and `-`. Other keys on an entry or its
`function` are dropped; the `parameters` schema is forwarded unchanged. `tool_choice` is `none`, `auto`,
`required`, or `{"type": "function", "function": {"name": "<name>"}}` to force one function; other keys in that
object are dropped. `parallel_tool_calls` is a boolean forwarded to the worker with the tools. `tool_choice` and
`parallel_tool_calls` without `tools` are 400 `ChatRequestConflict`, as under OpenAI. A `parameters` schema may nest
at most 10 levels below its root. A model's card lists `tools` when a worker serves them.

A tool round:

1. Send the request with `tools`. A reply with calls has `finish_reason` `tool_calls` (or `stop` under a forced
   `tool_choice`), `message.content` null when there is no text, and `message.tool_calls`, each `{"id", "type":
   "function", "function": {"name", "arguments"}}` with `arguments` a JSON string.
2. Run each call.
3. Send the history again with the assistant message replayed as `{"role": "assistant", "content": null,
   "tool_calls": [...]}`, then one `{"role": "tool", "tool_call_id": "<id>", "content": "<result>"}` per call.

The history follows OpenAI's rule: the `tool` messages come right after the assistant message and answer each of its
call ids exactly once, in any order. A message arriving before every call is answered, a second answer to one id, or
a `tool` message with no open calls is 400 `UnsupportedChatParameter` with `param` `messages[<i>].role`; an id the
preceding assistant message did not issue has `param` `messages[<i>].tool_call_id`. The messages are OpenAI's. With
the Python SDK, appending the returned `message` object to the history replays it correctly, including the message
`get_final_completion()` returns from a stream: the front drops the fields the SDK adds to it (`refusal`, `parsed`,
`tool_calls[].index`, `function.parsed_arguments` and similar).

Verification: the first reply's `message.tool_calls[0].function.name` is the function asked for, and the follow-up
reply has `finish_reason` `stop` with text and no `tool_calls`.

## Conformance

By default (`"conformance": "strict"`) a request goes only to workers that declare the guarantees it relies on: an
enforced `tool_choice` for `required` or a forced function, at most one call under `parallel_tool_calls: false`, schema
adherence for a `json_schema` format with `strict: true`, and refusal rather than trimming of a prompt that does not
fit. A request that needs a guarantee no online worker serving the model declares ends at once with 404
`no_worker_supports_features`, and the message names the guarantee.

To accept workers that may deviate, for example a backend that renders the same prompt for every `tool_choice`, send:

```json
{"horde": {"conformance": "best_effort"}}
```

Under `best_effort` the reply may not follow those parts of the contract. Features and limits still apply in both
modes: a worker that clamps the temperature when tools are present does not receive a tools request above its limit,
and a tools request above the limit of every online worker ends at once with 404 `no_worker_supports_features`.
`tool_choice: "none"` does not need a guarantee, since the worker does not receive tool definitions under it.

Verification: a request with a forced `tool_choice` that waited under `strict` on a model whose card does not list
`tool_choice` is picked up under `best_effort`.

## Structured output

`response_format` takes `{"type": "json_object"}` or `{"type": "json_schema", "json_schema": {"name", "schema",
"description", "strict"}}`, with `schema` required. A model's card lists `response_format` when one of its workers
serves either type; it does not say which, so a `json_schema` request can lack an eligible worker on a model whose
workers serve only `json_object`.

With `"strict": true` the schema follows OpenAI's structural rules: every object schema, nested ones included, sets
`"additionalProperties": false` and lists all its properties in `required`. A schema breaking them is 400
`UnsupportedChatParameter` with `param` `response_format` and OpenAI's message. A schema may nest at most 10 levels
below its root, strict or not.

A request may offer `tools` and set a `response_format` together; the format applies only to the final text answer,
as under OpenAI. Only a worker declaring `tools_with_response_format` serves such a request,
under either conformance mode. When none of the model's online workers declares it, the request ends at once with
404 `no_worker_supports_features`.

OpenAI refuses a `json_object` request whose messages never mention JSON; the Horde serves it. To get OpenAI's
refusal, send `{"horde": {"openai_guards": true}}`.

The compact JSON of `tools`, `tool_choice` and `response_format` together may be at most 256 KiB.

Verification: `json.loads(choices[0].message.content)` succeeds, and for `json_schema` the document has the schema's
required keys.

## Streaming

With `"stream": true` the response is server-sent events in this order:

1. Keepalive comment lines while the request waits, such as `: horde queue_position=7 wait_time=42`, at an interval
   the deployment sets (5 seconds by default).
2. A role chunk, `delta: {"role": "assistant"}`.
3. A content chunk with the whole `delta.content`, when the reply has text.
4. A tool-call chunk with `delta.tool_calls`, when the reply has calls: every call whole, each with its 0-based
   `index`, `id`, `type` and `function`, and no content in the same chunk.
5. A finish chunk, `delta: {}`, with `finish_reason`, `native_finish_reason` and `x_horde`.
6. A usage chunk with `choices: []` and `usage`, when `stream_options.include_usage` is true.
7. `data: [DONE]`.

The worker generates the whole reply before the Horde reports it, so content arrives in one chunk. Every chunk
carries the same `id` and the served `model`. An SDK that accumulates tool-call deltas by `index` rebuilds the calls from the single tool-call chunk; in the Python SDK, `client.chat.completions.stream(...)` with
`get_final_completion()` returns the assembled message.

A stream's headers go out once the Horde holds the request, so its status is 200 whatever the outcome. A failure after
that is one `data: {"error": {...}}` event, with the same envelope as an error response and no `choices`, followed by
`data: [DONE]`. A hand-written parser checks each `data:` payload for a top-level `error`.

Verification: with `stream_options.include_usage`, a served request yields four chunks for a text reply (role,
content, finish, usage) and the finish chunk carries `x_horde.request_id`.

## Errors and retries

Every error is `{"error": {"message", "type", "param", "code", "metadata"}}`, with `param` and `code` null when they
do not apply and `metadata` carrying the Horde's `request_id` and return code where known
([ADR 0034](../decisions/0034-map-chat-errors-to-the-openai-envelope-with-openrouter-status-vocabulary.md)). A refusal
from the Horde keeps its status and has its return code (see `README_return_codes.md` at the repository root) as
`code`; a Horde 429 keeps `Retry-After` and the `X-RateLimit-*` headers.

The front's own codes:

| Status | `code` | `x-should-retry` | Cause |
| --- | --- | --- | --- |
| 400 | `UnsupportedChatParameter`, `ChatRequestConflict`, `BadRequest` | | See [Keys the front refuses](#keys-the-front-refuses). |
| 401 | `InvalidAuthorizationHeader` | | `Authorization` is not `Bearer <key>`. |
| 404 | `model_not_found` | | The model id contains `:`. |
| 404 | `no_worker_supports_features` | `false` | Online workers serve the model, but none declares a capability or guarantee the request needs, or the request exceeds a limit each declares, such as the temperature limit with tools. Returned on the first status read, without the grace period; the request is cancelled. |
| 408 | `RequestBodyTimeout` | | The body arrived too slowly; the request was not submitted. |
| 408 | `ChatRequestTimeout` | `false` | The request was still waiting at the front's deadline and was cancelled. |
| 413 | `RequestTooLarge` | | The body exceeds the front's byte limit. |
| 502 | `ChatRequestFaulted` | `false` | The Horde reports the request faulted or expired. |
| 502 | `HordeUnavailable` | | No Horde root accepted the connection; the request was not submitted. |
| 502 | `HordeUnavailable` | `false` | The submit was sent and no answer came; `metadata.submit_outcome` is `"unknown"`. |
| 503 | `FrontAtCapacity` | | The front or your address holds its maximum of open requests. `Retry-After: 5`. |
| 503 | `no_eligible_worker` | `false` | No online worker could serve the request for the front's grace period; it is cancelled. `Retry-After: 30`. |

Retry rules:

- An error without `x-should-retry` came before the Horde held the request. The request was not queued or charged,
  and a retry after `Retry-After` is safe.
- An error with `x-should-retry: false` came after the Horde held the request, or may have. OpenAI SDKs read the
  header and do not retry. A retry is a new request and can create a second charged job, since the front does not
  deduplicate client requests.
- `no_eligible_worker` means the request as sent has no worker online: check the model card for the model's workers,
  `supported_parameters` and `max_completion_tokens`, and change the request or wait for workers; the same request
  fails the same way.
- `no_worker_supports_features` names what is missing. Drop or change the feature, lower the temperature, or send
  `horde.conformance: "best_effort"` when the missing names are guarantees; the same request fails the same way
  until a worker declaring them comes online.
- A stream always carries `x-should-retry: false`.

Verification: a request for a model the Horde knows but no worker serves returns 503 with `"code":
"no_eligible_worker"` and `x-should-retry: false` after the grace period:

```bash
curl -s -i "$OAI_BASE_URL/chat/completions" -H "Authorization: Bearer $HORDE_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "<an unserved model>", "messages": [{"role": "user", "content": "Hi"}]}' | grep -i -E "^HTTP|x-should-retry|retry-after|no_eligible"
```

## Limits

| Limit | Value | Refusal |
| --- | --- | --- |
| Request body | 36 MiB by default; the deployment sets it | 413 `RequestTooLarge` |
| Messages per request | 1024 | 400 `UnsupportedChatParameter` |
| Message text | 1 MiB of UTF-8 across all text and the function names and arguments of replayed tool calls, image data URLs excluded | 400 `UnsupportedChatParameter` |
| Images | 5 per request (1 for untrusted users), 5 MiB and 3072 by 3072 pixels each | See [Images](#images) |
| Tools, tool choice and response format | 256 KiB of compact JSON together; 128 tools; schemas nested at most 10 levels | 400 `UnsupportedChatParameter` |
| Tool calls replayed in one assistant message | 128 | 400 |
| `max_tokens` | 16 to 4096 | 400 `UnsupportedChatParameter` |
| `max_tokens` against workers | At most a worker's declared length; the card's `max_completion_tokens` is the largest | 503 `no_eligible_worker` after the grace period |
| Non-streamed and streamed wait | 300 and 600 seconds by default; the deployment sets them | 408 `ChatRequestTimeout`, or an in-stream error |

`max_tokens` is the limit clients most often exceed: a client default above every online worker's declared length
makes the request ineligible. Omitting `max_tokens` sends 512, which is also ineligible on a model whose workers all
declare less, so send an explicit value at or below the card's `max_completion_tokens`.

Verification: a request at the card's `max_completion_tokens` is served; one above it ends with `no_eligible_worker`.

## Reversal

A client request does not change Horde state beyond the request itself. Point the client's base URL back at its
previous provider to stop using the Horde. A request the client abandons by closing the connection is cancelled on
the Horde.

## Related

- [Chat front reference](../reference/chat_front.md): request keys, message roles, response, stream and model card
  shapes.
- [Chat requests reference](../reference/chat_requests.md): the native contract behind the front, including worker
  eligibility and pricing.
- [Add chat completions to a web frontend](add_chat_to_a_frontend.md): the same contract from a browser client.
