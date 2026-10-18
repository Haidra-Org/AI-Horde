---
title: "Chat requests reference"
summary: "Chat routes and their text mapping, known models, operation keys, chat declaration on pop, chat job payload, structured submit, pricing, chat model list, admission flag and completion signal."
topics: [requests, workers]
order: 100
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Chat requests reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

A chat request is a text request whose worker input is an OpenAI `messages` array instead of a prompt. It lives in
the text queue and shares the text worker pool: queue position, kudos, upfront kudos, concurrency limits, statistics
and countermeasures treat it as a text request. Only text workers that declare chat jobs on pop receive it, and
those workers keep receiving text jobs.

## Code map

| Concept                          | File                                            | Symbol                                                |
| -------------------------------- | ----------------------------------------------- | ----------------------------------------------------- |
| Body validation and mapping      | `horde/classes/kobold/chat_request.py`          | `parse_chat_request`, `build_text_params`             |
| Worker targeting                 | `horde/classes/kobold/chat_request.py`          | `build_worker_targeting`                              |
| `horde` object and size limits   | `horde/chat_contract.py`                        | `ChatHordeOptions`, `MAX_CHAT_MESSAGE_COUNT`          |
| Flattened estimate               | `horde/classes/kobold/chat_request.py`          | `flatten_messages`                                    |
| Feature vocabulary               | `horde/classes/kobold/chat_request.py`          | `ChatFeature`, `required_chat_features`               |
| Tool and format shapes           | `horde/chat_contract.py`                        | `ChatTool`, `ChatToolCall`, `ChatResponseFormat`      |
| Stored tools and format          | `horde/classes/kobold/chat_request.py`          | `build_chat_extras`                                   |
| Charged reply characters         | `horde/classes/kobold/chat_request.py`          | `chat_reply_characters`, `build_stored_reply`         |
| Sampling defaults                | `horde/classes/kobold/chat_request.py`          | `CHAT_SAMPLING_DEFAULTS`, `build_chat_job_payload`    |
| Submit route                     | `horde/apis/v2/kobold.py`                       | `ChatAsyncGenerate`                                   |
| Body size bound                  | `horde/apis/v2/kobold.py`                       | `read_chat_request_body`                              |
| Known model check                | `horde/apis/v2/kobold.py`                       | `ChatAsyncGenerate.ensure_model_known`                |
| Operation key claim              | `horde/apis/v2/kobold.py`                       | `ChatAsyncGenerate.activate_waiting_prompt`           |
| Operation fingerprint            | `horde/classes/kobold/chat_request.py`          | `build_operation_fingerprint`                         |
| Claim in the activation          | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt._activate`                         |
| Claim lookup and worker queries  | `horde/database/text_functions.py`              | `find_chat_operation_claim`, `is_chat_model_served`, `get_chat_model_offers` |
| Chat model list                  | `horde/apis/v2/base.py`                         | `Models.add_chat_offers`                              |
| Admission flag                   | `horde/config.py`                               | `Config.chat_admission_enabled`                       |
| Status and cancel route          | `horde/apis/v2/kobold.py`                       | `ChatAsyncStatus`                                     |
| Discriminator and messages       | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt.is_chat`, `TextWaitingPrompt.messages` |
| Stored features and extras       | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt.chat_required_features`, `TextWaitingPrompt.chat_extras` |
| Pop payload                      | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt.prepare_job_payload`               |
| Assignment snapshot              | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt.get_generation_assignment`         |
| Chat declaration on pop          | `horde/apis/v2/kobold.py`                       | `TextJobPop.validate_chat_declaration`                |
| Stored declaration               | `horde/classes/kobold/worker.py`                | `TextWorker.check_in`                                 |
| Worker eligibility               | `horde/classes/kobold/worker.py`                | `TextWorker.chat_refusal_reason`                      |
| Queue filter                     | `horde/database/text_functions.py`              | `get_sorted_text_wp_filtered_to_worker`               |
| Structured submit                | `horde/apis/v2/kobold.py`                       | `TextJobSubmit.prepare_chat_submission`               |
| Stored reply and charge basis    | `horde/classes/kobold/processing_generation.py` | `TextProcessingGeneration.get_submission_columns`     |
| Completion signal                | `horde/chat_signal.py`                          | `publish_chat_event`                                  |
| Expiry and fault signals         | `horde/database/threads.py`                     | `collect_chat_faults`, `publish_chat_faults`          |
| Signal database                  | `horde/redis_ctrl.py`                           | `chat_signal_db`                                      |
| Image fetch, normalise, encode   | `horde/image.py`                                | `fetch_public_image`, `load_chat_image`, `encode_chat_image` |
| Image storage at submit          | `horde/apis/v2/kobold.py`                       | `ChatAsyncGenerate.load_chat_images`, `ChatAsyncGenerate.upload_source_images` |
| Image references and transport   | `horde/classes/kobold/chat_request.py`          | `replace_image_urls`, `select_image_transport`        |
| Image parts at pop               | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt.build_worker_messages`             |
| Image cleanup                    | `horde/database/threads.py`                     | `collect_expired_chat_image_keys`                     |
| Image delete retry               | `horde/classes/kobold/waiting_prompt.py`        | `delete_stored_chat_images`, `retry_chat_image_deletes` |
| Schema migration                 | `sql_statements/5.1.15.txt`                     |                                                       |

## Routes

| Method and path                            | Behavior                                                                   |
| ------------------------------------------ | -------------------------------------------------------------------------- |
| `POST /v2/generate/chat/async`             | Queue a chat request. `202` with `{id, kudos, message, warnings}`, as text; `200` with `{id, kudos}` for a repeated operation key |
| `GET /v2/generate/chat/status/<id>`        | Full status, with each finished generation and its structured reply        |
| `DELETE /v2/generate/chat/status/<id>`     | Cancel, as text                                                            |

The text and chat route families are distinct on the wire. Every read filters on `is_chat`: a chat request's id
returns `404 RequestNotFound` on the text status and request routes, and a text request's id does the same on the
chat routes. The chat routes do not include a stored-parameters route.

The submit route runs the text submit path unchanged after mapping, so rate limits, concurrency, shared keys,
countermeasures, moderation, upfront kudos and context sizing all apply. The behaviour below does not change the text
route.

The route handles a submission in this order:

1. A body whose `Content-Length` is above `MAX_CHAT_BODY_BYTES` (36 MiB, `horde/chat_contract.py`) returns
   `413 RequestTooLarge` before it is parsed. A chunked body, which does not declare a length, is read only up to
   one byte past the limit and refused the same way.
2. The body is parsed and validated (below).
3. A body with `horde.operation_key` and without `horde.dry_run` looks up the key (see [Operation key](#operation-key)).
4. The admission flag is checked. A repeated operation key is answered before this check: with admission off it
   still returns `200` with the existing request, since it does not create anything.
5. The checks every generate route shares run: maintenance mode, authentication, the concurrency limit,
   countermeasures and moderation.
6. The model is checked (see [Known models](#known-models)).
7. The context is sized (`context_fit`) and the params are validated, then the request is created and activated as a
   text request is. A text request sizes its context before step 5; a chat request sizes it after the model check,
   so an unknown model returns `404` rather than `PromptExceedsContext`. The `apikey` and `Client-Agent` headers are
read as on text requests. The text submit response carries no `is_possible`, so neither does the chat one: when no
online worker can serve the request it carries the usual warning `message`, and the status route reports
`is_possible`.

### Known models

A model is known when the text model reference lists it (`model_reference.is_known_text_model`) or an online worker,
one that checked in within the last 300 seconds, declared `chat_jobs` with it as its `model_id`. The reference is in
memory; the worker table is queried only for a model the reference does not list. A model unknown to both returns
`404 UnknownChatModel` after authentication and the concurrency limit, before context sizing, any request row and the
horde tax, dry runs included. A known model that
no online worker can serve is accepted, waits, and reports `is_possible` false, as a text request does.

### Operation key

`horde.operation_key` is a UUID a native caller, normally the OpenAI-compatible front, sends to identify one
submission (ADR 0026). It is claimed for the submitting user, the owner of the shared key when one is used:

- The route looks up the user's key before validation, so a repeated submission is answered before the concurrency
  limit and any availability or model check. With the same fingerprint it returns `200` with the first request's
  `{id, kudos}` and does not queue or charge anything, even after the request's workers went offline. With a
  different fingerprint it returns `400 OperationKeyConflict`.
- An unclaimed key is written to the request in the activation transaction, before the first commit that also
  carries the activation and the horde tax. The unique index on `(user_id, operation_key)` lets only one of two
  concurrent submissions commit. The other rolls back, its inactive row and upfront kudos hold are deleted, and it
  returns `200` with the winning request, or `400 OperationKeyConflict` when the two requests differ.
- The same key from another user is a separate claim.
- A dry run with a key quotes without looking up or claiming it.
- The claim is held only by the request row and lasts as long as the row.

The fingerprint is the SHA-256 hex digest of canonical JSON (sorted keys, no insignificant whitespace, UTF-8) over the
messages as stored (`developer` rewritten to `system`), the mapped text params, the model list, every `horde`
option with defaults filled in except `operation_key` and `dry_run`, and the stored `chat_extras` only when the request
has any. `max_tokens` and `max_completion_tokens` with the
same value, or an option sent at its default and one left out, give the same fingerprint. `stream`,
`stream_options`, `user` and `metadata` are not in it.

The activation path commits in several places. `WaitingPrompt.__init__` commits the inactive row and
`prepare_job_payload` commits again; the upfront kudos hold is committed before activation; inside
`WaitingPrompt._activate`, `record_usage` commits the activation and the tax through `WaitingPrompt.refresh`, and a
final commit follows the style reward. The claim is set at the start of `TextWaitingPrompt._activate`, so the
first of the two activation commits writes it. It is kept on the instance as a plain attribute, so a deadlock retry, which rolls
back and runs `_activate` again, writes it again.

### Admission flag

`HORDE_CHAT_ADMISSION_ENABLED` (default `true`) gates `POST /v2/generate/chat/async` only. When it is `false` that
route returns `503 ChatAdmissionDisabled`, dry runs included, while the status and cancel routes and chat handling in
text pop and submit keep serving requests already queued. A repeated [operation key](#operation-key) still returns
`200` with its existing request, since it does not create anything, so a caller recovering an ambiguous submit
learns the id of a request that is queued and charged.

## Request body

| Key                                      | Mapping                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------- |
| `model`                                  | Required. The only model the request accepts                                     |
| `messages`                               | Required, at least one. Roles `system`, `developer`, `user`, `assistant`, `tool`; see [Tools](#tools) for the tool keys |
| `max_tokens`, `max_completion_tokens`    | `max_length`, 16 to 4096, default `512`. Both may be sent only with equal values |
| `temperature`, `top_p`                   | The text params of the same name                                                 |
| `frequency_penalty`, `presence_penalty`  | -2 to 2; forwarded to the worker, which has to declare the feature for a non-zero value |
| `seed`                                   | Forwarded to the worker, which has to declare the `seed` feature                 |
| `stop`                                   | `stop_sequence`; a single string becomes a one-element list                      |
| `n`                                      | Only `1`                                                                         |
| `stream`, `stream_options`, `user`, `metadata` | Accepted, not applied                                                      |
| `tools`, `tool_choice`, `parallel_tool_calls` | Stored and forwarded to the worker; see [Tools](#tools)                     |
| `response_format`                        | Stored and forwarded to the worker; see [Tools](#tools)                          |
| `horde`                                  | Extension object, below                                                          |

A message's `content` is a string or an array of parts whose `type` is `text`, or `image_url` on a `user` message (see
[Images](#images)). `system`, `developer` and `user` messages may carry `name`. A `developer` message is stored and
sent to workers as `system`, which loses its precedence over a system message; the Horde does not advertise a
developer-role feature while that holds. The default `max_length` is `512` because OpenAI clients usually omit a token limit and
the text default of 80 is too short for a reply. The mapped params are validated against the text params model, so
every range a text request enforces applies, and an out-of-range value returns the same `400` validation error and is
not clamped.

The `horde` object carries the request options that OpenAI does not have a key for. It is validated by `ChatHordeOptions` in
`horde/chat_contract.py`, the model the OpenAI-compatible front validates it with, so the two accept the same keys
and values:

| Key                                             | Type                  | Default | Effect                                                         |
| ----------------------------------------------- | --------------------- | ------- | -------------------------------------------------------------- |
| `only`                                          | non-empty array of worker ids | none | Only these workers serve the request                           |
| `ignore`                                        | non-empty array of worker ids | none | These workers never serve it; cannot be combined with `only`   |
| `trusted_workers`, `validated_backends`         | boolean               | `false` | As on text requests                                            |
| `slow_workers`                                  | boolean               | `true`  | As on text requests                                            |
| `max_context_length`                            | positive integer      | none    | An explicit context length                                     |
| `context_fit`                                   | `grow` or `reject`    | `grow`  | With `grow`, the context grows only up to the largest chat context an online chat worker serving the model declares |
| `dry_run`                                       | boolean               | `false` | Return the kudos cost without queueing                         |
| `operation_key`                                 | UUID                  | none    | Claims the submission for its user; see [Operation key](#operation-key) |

Styles, template fields, webhooks, soft prompts, extra source images, proxied accounts, downgrading and batching
control are not available on chat requests.

### Images

A `user` message may carry image parts, `{"type": "image_url", "image_url": {"url": ..., "detail": ...}}`, where `url`
is a `data:image/...;base64,` URL or an `http(s)` URL and the optional `detail` (`auto`, `low`, `high`) is passed to the
worker unchanged. An image part in any other role returns `400 UnsupportedChatParameter`.

The caps are those of extra source images on image requests, shared with the front from `horde/chat_contract.py`:

| Cap                                                  | Refusal                                    |
| ---------------------------------------------------- | ------------------------------------------ |
| At most 5 images per request (`MAX_CHAT_IMAGE_COUNT`) | `400 TooManyExtraSourceImages`            |
| More than 1 only for trusted users and patrons (`MAX_CHAT_IMAGE_COUNT_UNTRUSTED`) | `400 MoreThanMinExtraSourceImage` |
| At most 5 MiB decoded or fetched per image (`MAX_CHAT_IMAGE_BYTES`) | `400 SourceImageSizeExceeded` |
| At most 3072*3072 pixels, read from the image header before decoding | `400 SourceImageResolutionExceeded` |
| Readable as an image                                 | `400 SourceImageUnreadable`                |

`MAX_CHAT_BODY_BYTES` is sized for a request at every cap: 5 data URLs of 5 MiB, which base64 grows by 4/3 to about
33.3 MiB, plus the 1 MiB text allowance, with room for JSON structure, rounded to 36 MiB.

An `http(s)` URL is fetched by `fetch_public_image` in `horde/image.py`, through `send_public_request` (see
[Outbound requests reference](outbound_requests.md)). Each hop resolves its host once, refuses the
hop when any resolved address is private, loopback, link-local, multicast, reserved or unspecified (an IPv4-mapped IPv6
address is checked as IPv4), and connects to the checked address itself, sending the URL's host as the `Host` header
and, over https, as the TLS server name the certificate is verified against. The fetch does not resolve the host again
between the check and the connection, so a name that answers differently the second time cannot redirect the fetch. Each
redirect is checked the same way, at most 3 are followed, and connecting and each read time out after 2 seconds. A
refused URL returns `400 SourceImageUrlRefused`; a non-200 answer returns `400 SourceImageUrlInvalid`.

After every other check passes, each image is normalised (RGB, or RGBA with transparency; longest side at most 2048)
and encoded as JPEG at quality 90, with transparency flattened onto white. JPEG is decoded by every chat worker's
image loader: koboldcpp reads images with llama.cpp's multimodal loader, which decodes PNG and JPEG and does not
decode WebP. A dry run counts its images and refuses too many, but does not fetch or store them.
The request row is written with each image part's `url` replaced by `horde-r2://<request id>_chat_<n>.jpg`, `n`
counting the request's images from 0; the images are then stored under those keys in the source image bucket, before
activation. The row never holds image bytes or the submitted URL. Each image adds the extra source image tax
(`EXTRA_SOURCE_IMAGE_HORDE_TAX`, 5 kudos) to the activation tax, and the dry run quote includes it. A store failure
deletes the row and returns `503 ChatImageStorageUnavailable`; the request is not charged.

Only a worker that declares the `vision` feature and at least one image transport receives a request with images. At
pop each reference is replaced by what the worker declared:

| Declared `image_transports` | The part's `url`                                                                       |
| --------------------------- | -------------------------------------------------------------------------------------- |
| contains `url`              | A presigned download URL, signed locally, valid for the job's TTL and at least 600 seconds (`MIN_CHAT_IMAGE_URL_SECONDS`) |
| `base64` alone              | `data:image/jpeg;base64,` with the stored bytes, read from the store                   |

Both are built after the commits that assign the generation, which release the queue query's row lock, so the store
is not read under the lock. The generation records the transport in `processing_gens.image_transport`. A worker that
cannot fetch a signed URL faults the job.

Images are deleted after the request row: `TextWaitingPrompt.delete` deletes the keys its messages reference, and the
cleanup thread bulk deletes the keys of the expired chat rows it removes. A failed activation deletes the images the
submission stored. A key the store does not delete is added to the Redis set `horde:chat:image_delete_retry` on the
primary (`CHAT_IMAGE_DELETE_RETRY_KEY`), and each cleanup tick retries up to 1000 of them, removing each key that
deletes. The set does not have a TTL, since a key that expired out of it would leave its image stored and
unreferenced; it is bounded at 100,000 keys (`MAX_CHAT_IMAGE_DELETE_RETRY_KEYS`), past which keys are logged instead.

The row is written before any image is stored, so every stored image is referenced by a row until that row is
deleted, and the row's deletion either deletes the image or queues its key. Every stored image is accounted for by a
row or the retry set, so the Horde does not list the bucket.

Input images on a chat request are not screened by the server or by a chat worker. Image workers screen the images
they generate with a scanner and report a job as `worker_csam`; chat workers do not run a scanner, so the
chat vision path is not screened for CSAM on any side
([recorded events](moderation_operations.md#recorded-events)).

### Tools

The tool and response format shapes are validated by the models in `horde/chat_contract.py` the front validates with.

| Key                   | Value                                                                              |
| --------------------- | ---------------------------------------------------------------------------------- |
| `tools`               | At most `MAX_CHAT_TOOL_COUNT` (128) `{type: "function", function: {name, description?, parameters?, strict?}}`; `name` 1 to 64 characters of `[A-Za-z0-9_-]`. An empty list or null is the same as none |
| `tool_choice`         | `none`, `auto`, `required` or `{type: "function", function: {name}}`. Null, or `none` without tools, is the same as none |
| `parallel_tool_calls` | Boolean; stored with tools, dropped without them                                   |
| `response_format`     | `{type: "text"}`, `{type: "json_object"}` or `{type: "json_schema", json_schema: {name, schema, description?, strict?}}`; `schema` is required. `text` or null is the same as none |

The compact JSON of `tools`, `tool_choice` and `response_format` together may be at most `MAX_CHAT_TOOL_SPEC_BYTES`
(256 KiB); more returns `400 UnsupportedChatParameter`. A `tool_choice` other than `none` without a non-empty `tools`,
or one forcing a function `tools` does not define, returns `400 ChatRequestConflict`.

An `assistant` message may carry `tool_calls`, 1 to `MAX_CHAT_TOOL_CALL_COUNT` (128) `{id, type: "function",
function: {name, arguments}}`, and then a null or absent `content`. A `tool` message carries `role`, `content` (a
string or text parts) and `tool_call_id` (1 to 256 characters), which has to be the `id` of a tool call in an earlier
assistant message of the same request; otherwise `400 UnsupportedChatParameter` names `messages[i].tool_call_id`. Both
are stored and sent to the worker unchanged, with an assistant message's `content` stored as null when it was absent.

The values a request applies are stored in `waiting_prompts.chat_extras` (`build_chat_extras`), each in the shape the
client sent and only when it changes the answer:

```json
{
  "tools": [{"type": "function", "function": {"name": "lookup_weather", "parameters": {"type": "object"}}}],
  "tool_choice": "auto",
  "parallel_tool_calls": false,
  "response_format": {"type": "json_schema", "json_schema": {"name": "colour", "schema": {"type": "object"}}}
}
```

`tool_choice` `none` beside tools is stored, since a worker given the tools without it would default to `auto`. The
column is NULL when the request applies none of them.

### Refused keys and values

Any key outside the lists above returns `400 UnsupportedChatParameter`, and the message names the key. This covers
`logprobs`, `top_logprobs`, `logit_bias`, `modalities`, `audio`, `prediction` and `reasoning_effort`, as well as:

- a message key outside its role's keys: `role`, `content` and `name`, plus `tool_calls` on an `assistant` message,
  and `role`, `content` and `tool_call_id` on a `tool` message (for example `messages[0].tool_calls` on a `user`
  message, or `messages[0].name` on a `tool` message);
- a role other than `system`, `developer`, `user`, `assistant` and `tool` (for example `messages[0].role=function`);
- a content part whose type is not `text` or, on a `user` message, `image_url` (for example
  `messages[0].content[1].type=input_audio`, or `messages[0].content[0].type=image_url` on an `assistant` message);
- an unknown key in `horde` (for example `horde.models`);
- an `n` other than 1 (`n=2`);
- more than `MAX_CHAT_MESSAGE_COUNT` (1024) messages, or message text over `MAX_CHAT_TEXT_BYTES` (1 MiB of UTF-8
  across every string content and text part), the limits the front shares from `horde/chat_contract.py`.

A body over `MAX_CHAT_BODY_BYTES` returns `413 RequestTooLarge` before any of these checks.

Differing `max_tokens` and `max_completion_tokens`, or both `horde.only` and `horde.ignore`, return
`400 ChatRequestConflict`. A value of the wrong type, a `context_fit` of `ignore`, an `operation_key` that is not a
UUID, a missing `model` or an empty `messages` returns `400 BadRequest`.

### Context adjustments

When `context_fit` `grow` raises the context length to fit the messages, the request stores the change and the status
route returns it as `context_adjustments`: `[{"from": 2048, "to": 4096, "reason": "grow"}]`, where `from` is the
requested `max_context_length` or the default. It is `null` when sizing did not change the context. The list is
stored in a separate nullable JSONB column: the requested length is not kept anywhere else once growth overwrites
it, so the adjustment cannot be recomputed at status time.

## Flattened prompt

The request's `prompt` column holds the messages flattened into one string: each message as `role: text`, with the
parts of a message and the messages themselves joined by newlines and each image part rendered as `[image]`
(`CHAT_IMAGE_PLACEHOLDER`). An assistant message's `tool_calls` follow its text as compact JSON, a `tool` message is
`tool: <text>`, and the compact JSON of the stored `tools` and of a `json_schema` response format follow the messages,
since the worker renders both into its prompt. It is an estimate only. The character-based
token estimate, `context_fit` sizing, kudos and moderation read it; no worker receives it. `submitted_prompt` holds
the flattened messages as submitted.

Every rewrite validation makes to the flattened prompt is mirrored onto the messages. The replacement filter applied
for flagged users and NSFW models is applied to each message text, a text the filter empties becomes an empty string,
and the prompt is flattened again from the result. `messages` therefore always holds what workers receive. Tool call
arguments, the tool definitions and the response format are JSON documents and are not rewritten.

## Chat declaration on pop

`POST /v2/generate/text/pop` takes five further fields. The worker row stores them at every pop, including a pop
inside the 30 second window in which the other check-in fields are not rewritten, and the pop is matched against
them. A pop without `chat_jobs` therefore withdraws the whole declaration and does not receive a chat job.

| Field           | Type                                   | Required        | Meaning                                                            |
| --------------- | -------------------------------------- | --------------- | ------------------------------------------------------------------ |
| `chat_jobs`     | boolean                                | no, `false`     | The worker accepts chat jobs; stored as `accepts_chat_jobs`         |
| `chat_features` | array of strings                       | no              | The optional features below that the worker implements; unknown names are ignored, absent means none |
| `chat_limits`   | `{max_context_length, max_length}`     | with `chat_jobs` | The limits chat jobs run under; the text pop's limits bound only text jobs |
| `model_id`      | string                                 | with `chat_jobs` | The model the worker serves chat jobs with; exactly one of `models` |
| `image_transports` | array of `url`, `base64`            | no              | How the worker receives images; absent or empty means none; unknown names are ignored |

A pop that sets `chat_jobs` without `chat_limits` or `model_id`, or with a `model_id` that is not one of `models`,
returns `400 ChatDeclarationInvalid`. The native route treats `model_id` as an opaque name and does not reserve any
character in it.

The feature vocabulary (`ChatFeature`) grows by adding names; a name never changes meaning:

| Feature             | A request needs it when                       |
| ------------------- | --------------------------------------------- |
| `seed`              | it sends `seed`, whatever its value            |
| `presence_penalty`  | it sends a non-zero `presence_penalty`         |
| `frequency_penalty` | it sends a non-zero `frequency_penalty`        |
| `vision`            | a message carries an image part; the worker also has to declare an image transport |
| `tools`             | it stores `tools` with a `tool_choice` other than `none`, or any message is a `tool` message or carries `tool_calls` |
| `json_object`       | its `response_format` type is `json_object`    |
| `json_schema`       | its `response_format` type is `json_schema`    |

A field at its documented default needs nothing. `required_chat_features` is the only place these are derived: it runs
once when the request is created and stores the sorted names in `waiting_prompts.chat_required_features`, which
`TextWorker.chat_refusal_reason` compares with the worker's `chat_features`, so eligibility never loads a candidate's
messages or extras.

### Eligibility

A chat request goes to a worker only when every rule holds. The queue query applies the cheap ones; `can_generate`
applies the rest and repeats them for worker availability:

1. The worker declared `chat_jobs` on its latest pop and its `model_id` is the request's model.
2. Every feature in the request's `chat_required_features` is in the worker's `chat_features`, and a request with
   images finds an image transport in its `image_transports`. The queue query withholds requests with images from a worker without both,
   testing `messages` for an image part by JSONB containment on chat rows only, and computes the same test for the
   returned page so `can_generate` does not load candidates' messages.
3. The request's `max_context_length` and `max_length` fit the worker's `chat_limits`, never its text limits.
4. The usual restrictions hold: `only` and `ignore`, trusted, validated, slow workers.

A request no online worker can serve is accepted and waits, with `is_possible` false on the status route. A worker
that predates chat never sees a chat request, and a text request goes to a chat worker under its text limits.

When a chat request is assigned, the generation records the worker's `model_id`, chat context and chat length as
`assigned_model`, `assigned_max_context_length` and `assigned_max_length`, so a later change to the declaration does
not redefine work already handed out.

### Chat job pop response

A text job's pop response does not carry a `chat` key. A chat job's response has the same top-level fields plus
`chat: true`, and its payload is:

| Field                                                                  | Value                                                         |
| ---------------------------------------------------------------------- | ------------------------------------------------------------- |
| `messages`                                                             | The stored array; roles `system`, `user`, `assistant`, `tool`; `tool_calls` and `tool_call_id` as stored; image parts as in [Images](#images) |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`       | Each only when stored in `chat_extras`, exactly as stored     |
| `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `rep_pen`, `top_k`, `min_p` | Always present; the client's value or `1.0`, `1.0`, `0`, `0`, `1.0`, `0`, `0` |
| `stop_sequence`, `seed`                                                | Only when the client sent them                                |
| `max_length`, `max_context_length`                                     | The validated budgets                                         |
| `n`                                                                    | `1`                                                           |

There is no `prompt`. The worker applies its own chat template, tokenises the rendered input, and fails a job that
does not fit rather than truncating it. Every sampling field is present, so a worker's own defaults never apply to a
chat job.

## Structured submit

`POST /v2/generate/text/submit` keeps `generation` for text jobs. A chat job sends these instead:

| Field                  | Type                                      | Required | Meaning                                                    |
| ---------------------- | ----------------------------------------- | -------- | ---------------------------------------------------------- |
| `message`              | `{role: "assistant", content, tool_calls?}` | yes    | The reply; an empty `content` is a completed reply. `tool_calls` holds 1 to 128 calls as minted by the backend; `content` may be null only with them |
| `finish_reason`        | `stop`, `length`, `content_filter` or `tool_calls` | yes | Why the worker stopped; `tool_calls` needs a non-empty `message.tool_calls`. A forced `tool_choice` may finish with `stop` and calls |
| `native_finish_reason` | string, at most 32 characters             | no       | The backend's reason                                       |
| `prompt_tokens`, `completion_tokens` | integer, 0 to 10,000,000    | no       | Omit a count the backend did not report; stored as null     |
| `model_id`             | string                                    | yes      | Has to be the generation's `assigned_model`                 |
| `state`                | `ok`, `censored`, `csam`, `faulted`       | no       | As for text jobs                                           |

A body carrying `message`, `model_id` or `finish_reason` is parsed as a chat submit, which does not require
`generation`; any other body is parsed exactly as before, so a text submit without `generation` is still refused by the
parser. A chat submit without a valid `message` or `finish_reason`, or with `generation` as well as `message`, returns
`400 ChatSubmitInvalid`, as does a text job's submit carrying `message`. A `model_id` other than the assigned model
returns `400 ChatModelMismatch`. A faulted chat job submits `{id, state: "faulted", model_id}` with neither `generation`
nor `message`; it is handled as any faulted text submission.

The submit goes through the text path's compare-and-set: one update claims the pending generation and writes
`generation` (the message content), `message`, `finish_reason`, `native_finish_reason`, the counts and `charge_basis`
together, so a submit that loses to a cancel or a duplicate does not write any of them, and every predicate that reads
`generation` treats chat and text alike. `message` is stored with `tool_calls` when present and NUL replaced in every
string (`build_stored_reply`); `generation` holds the content, or an empty string when it is null. An empty `content`
completes the generation, is charged and rewarded, and counts towards the per-model empty-generation counter unless
the reply carries tool calls, which are a complete reply.

### Pricing

A chat generation is charged and rewarded exactly as a text generation with the same reply, through the text kudos
formula, and stores `charge_basis` `output_estimate`. The reply's characters are the content plus each tool call's
function name and arguments (`chat_reply_characters`, read by `TextProcessingGeneration.get_things_count`). The
formula's token count is those characters divided by 4, rounded up, at least 20 and at most the requested `max_length`, so a short reply to a large `max_tokens` is not
charged the full request. Reported token counts are stored and shown on the status route and never change the charge.
The basis is computed in one place, `TextProcessingGeneration.get_charge_basis`. The status route returns, per
generation, `message` (with `tool_calls` when the worker submitted them), `finish_reason` as submitted,
`native_finish_reason`, `prompt_tokens`, `completion_tokens`, `charge_basis` and `queue_wait_seconds`, the seconds from
the request's creation to the generation's assignment. The text status route returns none of them.

## Chat model list

`GET /v2/status/models?type=text&chat=true` lists only the text models that at least one online chat worker serves,
each with the usual fields plus a `chat` object. Without `chat=true` the entries do not carry `chat` and the
response is the plain list. `chat=true` with another `type` returns `400`.

| `chat` field         | Value                                                                                  |
| -------------------- | -------------------------------------------------------------------------------------- |
| `workers`            | Online workers, not in maintenance, that serve chat jobs with the model                |
| `max_context_length` | The largest `chat_limits.max_context_length` among them                                |
| `max_length`         | The largest `chat_limits.max_length` among them                                        |
| `features`           | The sorted union of their `chat_features`                                              |
| `created`            | Unix seconds of the earliest `workers.created` among them; `null` when none records it |

The entry's `queued` and `jobs` are the model's queue, which chat requests share with text requests. The entries come
from the same `models_cache` the plain list reads, filtered as usual; the chat object comes from one query over the
online chat workers. Both responses go through the route's two-second cache, keyed by the query string. `created`
changes when the earliest of those workers goes offline; the Horde does not keep a first-seen time per model.

## Completion signal

Chat requests publish to Redis channel `horde:chat:<request id>`, on the host the cache uses. The publishing client
uses logical database `chat_signal_db` (7); Redis delivers a pub/sub message to subscribers on any logical database,
so a subscriber only needs the host. Each message is `{"id": "<request id>", "event": "<event>", "generations": <n>}`,
where `generations` counts the generations with a stored result. A message never carries text.

| Event        | Published when                                                                    |
| ------------ | --------------------------------------------------------------------------------- |
| `generation` | A submit stored a generation, after its commit                                     |
| `done`       | After `generation`, when the submit completed the request                          |
| `cancelled`  | The cancel route claimed the cancellation, or maintenance mode aborted the request |
| `faulted`    | The cleanup thread faulted the request, or claimed its expiry                      |

An expired request's row is deleted in the same cleanup pass that claims its expiry, as a text request's is, so the
status route then returns `404`. The `faulted` event for an expiry is published after that deletion commits, so a
client woken by it reads that settled state. A request faulted by repeated worker faults keeps its row until it
expires, and its status reports `faulted`.

Publishing is best effort. A Redis failure is logged and never fails the submit, cancel or cleanup that published,
and a subscriber that misses a message reaches the result through the status route. A client subscribes after it has
the request id, so it checks the status once after subscribing to cover an event published in between.

## Schema

`sql_statements/5.1.15.txt` adds `waiting_prompts.is_chat` (boolean, not null, default false) and
`waiting_prompts.messages` (JSONB, deferred in the ORM); `workers.accepts_chat_jobs` (boolean, not null, default
false), `chat_features` (text array), `chat_max_context_length`, `chat_max_length`, `chat_model_id` and
`image_transports` (text array); and `processing_gens.message` (JSONB), `finish_reason` and `native_finish_reason`
(varchar 32), `prompt_tokens`, `completion_tokens`, `charge_basis` (varchar 16), `assigned_model`,
`assigned_max_context_length`, `assigned_max_length` and `image_transport` (varchar 16); and
`waiting_prompts.operation_key` (UUID), `operation_fingerprint` (char 64),
`context_adjustments` (JSONB), `chat_extras` (JSONB, deferred in the ORM) and `chat_required_features` (text array). Every column is nullable or has a constant default, so no table is rewritten. The
unique partial index `ix_waiting_prompts_user_operation_key` on `(user_id, operation_key) WHERE operation_key IS NOT
NULL` is built with `CREATE UNIQUE INDEX CONCURRENTLY`, which cannot run inside a transaction block; a failed
build leaves an invalid index. `db.create_all()` does not add columns to existing tables.

## Related

- [Prompt provenance reference](prompt_provenance.md)
- [Style contract reference](style_contract.md), for `context_fit`
