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
| Declaration vocabulary           | `horde_sdk.generation_parameters.text.chat_policy` | `ChatFeature`, `ChatGuarantee`, `ChatConformance`, `required_chat_guarantees` |
| Request requirements             | `horde/classes/kobold/chat_request.py`          | `build_chat_request_traits`, `required_chat_features`, `chat_tools_temperature` |
| Client-side requirements (SDK)   | `horde_sdk.generation_parameters.text.chat_policy` | `chat_request_requirements`, `chat_service_gap`; `TestRequirementParity` in `tests/unit/test_chat_request.py` keeps both derivations equal |
| Request limits and defaults (SDK) | `horde_sdk.generation_parameters.text.chat_limits` | `MAX_CHAT_TOOL_COUNT`, `MAX_CHAT_TOOL_SPEC_BYTES`, `MAX_CHAT_TEXT_BYTES`, `MAX_CHAT_SCHEMA_NESTING`, `OPENAI_SAMPLING_RANGES`, `CHAT_SAMPLING_DEFAULTS` |
| Worker builders and fault reasons (SDK) | `horde_sdk.worker.chat_declaration`       | `ChatFaultReason`, `ChatFinishReason`, `ChatImageTransport` |
| Dispatch narrowing               | `horde/classes/kobold/chat_request.py`          | `build_worker_chat_extras`                            |
| Tool and format shapes           | `horde/chat_contract.py`                        | `ChatTool`, `ChatToolCall`, `ChatResponseFormat`      |
| Stored tools and format          | `horde/classes/kobold/chat_request.py`          | `build_chat_extras`                                   |
| Charged reply characters         | `horde/classes/kobold/chat_request.py`          | `chat_reply_characters`, `build_stored_reply`         |
| Sampling defaults in the payload | `horde/classes/kobold/chat_request.py`          | `build_chat_job_payload`                              |
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
| Stream hint                      | `horde/classes/kobold/chat_request.py`          | `build_stored_chat_extras`, `CHAT_STREAM_PROGRESS_KEY`, `streams_progress` |
| Stream lease at pop and fault    | `horde/classes/kobold/processing_generation.py` | `TextProcessingGeneration._write_stream_lease`, `TextProcessingGeneration.end_progress_after_fault` |
| Progress route                   | `horde/apis/v2/kobold.py`                       | `TextJobProgress`                                     |
| Lease, append script and stream  | `horde/chat_progress.py`                        | `write_stream_lease`, `append_progress`, `end_progress_stream`, `APPEND_PROGRESS_SCRIPT` |
| Structured submit                | `horde/apis/v2/kobold.py`                       | `TextJobSubmit.prepare_chat_submission`               |
| Reply verification               | `horde/classes/kobold/chat_request.py`          | `verify_chat_reply`                                   |
| Stored reply and charge basis    | `horde/classes/kobold/processing_generation.py` | `TextProcessingGeneration.get_submission_columns`     |
| Completion signal                | `horde/chat_signal.py`                          | `publish_chat_event`                                  |
| Expiry and fault signals         | `horde/database/threads.py`                     | `collect_chat_faults`, `publish_chat_faults`          |
| Fault on repeated faulted jobs   | `horde/classes/base/processing_generation.py`, `horde/classes/base/waiting_prompt.py`, `horde/database/threads.py` | `ProcessingGeneration.fault_and_restart`, `WaitingPrompt.mark_faulted_after_repeated_faults`, `FAULTED_GENERATIONS_LIMIT`, `check_waiting_prompts` |
| Declaration gap on the status    | `horde/database/functions.py`                   | `find_chat_declaration_gap`, `get_worker_availability_for_request` |
| Signal database                  | `horde/redis_ctrl.py`                           | `chat_signal_db`                                      |
| Image fetch, normalise, encode   | `horde/image.py`                                | `fetch_public_image`, `load_chat_image`, `encode_chat_image` |
| Image storage at submit          | `horde/apis/v2/kobold.py`                       | `ChatAsyncGenerate.load_chat_images`, `ChatAsyncGenerate.upload_source_images` |
| Image references and transport   | `horde/classes/kobold/chat_request.py`          | `replace_image_urls`, `select_chat_transport`         |
| Messages offload                 | `horde/classes/kobold/chat_request.py`, `horde/apis/v2/kobold.py`, `horde/r2.py` | `build_chat_messages_offload`, `ChatAsyncGenerate.prepare_messages_offload`, `upload_chat_messages` |
| Messages URL at pop              | `horde/classes/kobold/waiting_prompt.py`        | `TextWaitingPrompt.get_pop_payload`, `CHAT_MESSAGES_URL_SECONDS` |
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
   so an unknown model returns `404` even when its messages exceed the context. Moderation's rewrite is mirrored onto the
   messages before sizing, so `truncate_oldest` trims the screened and rewritten messages. The `apikey` and `Client-Agent` headers are
read as on text requests. The text submit response does not carry `is_possible`, and the chat one does not either:
when no online worker can serve the request it carries the usual warning `message`, and the status route reports
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

### Rate limits

`POST /v2/generate/chat/async` holds one ordinary address to `HORDE_CHAT_REQUEST_2SEC_LIMIT_PER_IP` and
`HORDE_CHAT_REQUEST_90MIN_LIMIT_PER_IP`, in flask-limiter notation such as `5/second` and `300/minute`. The address is
the forwarded client address when the request carries a known proxy passkey. Clients behind one egress address, such
as the users of one chat front, share both budgets. Unset, each follows the text submit limit, `2/second` and
`90/minute`. A whitelisted service address gets the text route's whitelisted limits, and the per-key limit is the
text route's. Each route counts its requests separately, so chat submits do not spend the text budget. An exceeded limit
returns `429`.

## Request body

| Key                                      | Mapping                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------- |
| `model`                                  | Required. The only model the request accepts                                     |
| `messages`                               | Required, at least one. Roles `system`, `developer`, `user`, `assistant`, `tool`; see [Tools](#tools) for the tool keys |
| `max_tokens`, `max_completion_tokens`    | `max_length`, 16 to 4096, default `512`. Both may be sent only with equal values |
| `temperature`, `top_p`                   | The text params with the same keys                                                |
| `frequency_penalty`, `presence_penalty`  | -2 to 2; forwarded to the worker, which has to declare the feature for a non-zero value |
| `seed`                                   | Forwarded to the worker, which has to declare the `seed` feature                 |
| `stop`                                   | `stop_sequence`; a single string becomes a one-element list                      |
| `n`                                      | Only `1`                                                                         |
| `stream`                                 | `true` stores the stream hint; see [Streaming progress](#streaming-progress)     |
| `stream_options`, `user`, `metadata`     | Accepted and not applied                                                         |
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
| `context_fit`                                   | `grow`, `reject` or `truncate_oldest` | `grow`  | With `grow`, the context grows only up to the largest chat context an online chat worker serving the model declares. With `truncate_oldest`, the context grows as under `grow`; when the messages do not fit even at that bound, whole turns are dropped from the oldest forward until they do. See [Context adjustments](#context-adjustments) |
| `conformance`                                   | `strict` or `best_effort` | `strict` | With `strict`, only workers declaring every guarantee the request needs serve it; `best_effort` drops that requirement. See [Guarantees](#guarantees) |
| `openai_guards`                                 | boolean               | `false` | Turns on the request refusals OpenAI applies and the Horde relaxes on purpose: a `json_object` response format without the word "json" (any case) in some message text returns `400 ChatRequestConflict` with OpenAI's message. Acts on admission only; not stored |
| `include_reasoning`                             | boolean               | `false` | Read by the OpenAI front, which returns the model's reasoning only when it is set. The native route accepts it and does not store it; it is not in the operation fingerprint, and the native status shows a submitted `reasoning_content` either way |
| `availability_mode`                             | `wait` or `fail_fast` | `wait`  | Read by the OpenAI front. With `wait` the request queues for the front's budget; with `fail_fast` the front refuses it at the first status read when the status reports zero `eligible_worker_threads` and nothing processing, or a `wait_time` over the time left in the budget. The native route accepts it and does not store it; it is not in the operation fingerprint |
| `max_kudos`                                     | positive integer      | none    | A cap on the request's kudos estimate, the quote a dry run returns. When the estimate exceeds it, the route deletes the inactive row before any image upload or kudos hold and returns `400 ChatKudosCapExceeded`, whose message states the estimate at current pricing and the cap; a dry run is refused the same way. The estimate assumes the full `max_length`, while the charge is the output estimate ([ADR 0027](../decisions/0027-price-chat-generations-by-the-text-output-estimate.md)), so the check is conservative. Not stored; not in the operation fingerprint |
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

### Messages offload

The submit route can store a large request's whole `messages` array as one object, so a worker fetches one
self-contained JSON body and the pop response does not carry a large payload. It is off unless enabled:

| Setting                               | Default                  | Effect                                                                      |
| ------------------------------------- | ------------------------ | --------------------------------------------------------------------------- |
| `HORDE_CHAT_MESSAGES_OFFLOAD_ENABLED` | `false`                  | Offload requests over the threshold at submit                               |
| `HORDE_CHAT_MESSAGES_OFFLOAD_BYTES`   | `262144`                 | The largest messages size, in bytes, that stays inline                      |
| `HORDE_CHAT_MESSAGES_OFFLOAD_GZIP`    | `false`                  | Store the body gzipped, with `Content-Encoding: gzip`, when that is smaller |
| `R2_CHAT_MESSAGES_BUCKET`             | `R2_SOURCE_IMAGE_BUCKET` | The bucket the messages object is stored in                                 |

The size compared is the compact JSON of the messages as a `base64` worker receives them, each image inlined as its
stored JPEG in a `data:image/jpeg;base64,` URL. 256 KiB keeps text-only requests inline and offloads any request
carrying a stored image over about 192 KiB. A dry run is never offloaded.

The object is the plain JSON messages array in that form, keyed `<request id>_chat_messages.json` in the
`R2_CHAT_MESSAGES_BUCKET` bucket. That bucket defaults to the source image bucket, so the object sits beside the
images unless `R2_CHAT_MESSAGES_BUCKET` sets another bucket. The key suffix tells a messages object from an
image when the object is signed or deleted, so a queued delete retry reaches the right bucket. The object is stored
with `Content-Type: application/json` and `Cache-Control: no-transform`. With
`HORDE_CHAT_MESSAGES_OFFLOAD_GZIP` on, the whole body is gzipped and kept only when smaller, and the object carries
`Content-Encoding: gzip`; the key is the same either way. Compression is a transport encoding of the whole body,
and the JSON fields are plain. Object storage that honours `Cache-Control: no-transform`, Cloudflare R2 and
Garage included, serves the gzip-stored object as stored, with `Content-Encoding: gzip`, whatever `Accept-Encoding`
the client sends.
The row is written with `chat_messages_offloaded` set, then the images and the messages object are stored before
activation; a store failure deletes the row and the objects already stored and returns `503
ChatImageStorageUnavailable`. The images are stored as without the offload, so every other transport keeps working.

At assignment `select_chat_transport` picks `messages_url` for an offloaded request when the worker declared it, and
otherwise the transports above; the generation records it in `processing_gens.image_transport`, which is NULL for a
request sent inline without images. A `messages_url` job's payload carries `messages_url` in place of `messages`: a
presigned download URL, signed locally after the queue lock is released, valid for 120 seconds
(`CHAT_MESSAGES_URL_SECONDS`). The worker fetches right after the pop, so the URL does not cover the job's TTL as an
image URL does, and a short lifetime limits replay of a leaked URL. A worker that did not declare `messages_url`
receives an offloaded request inline, as if it had not been offloaded.

The response to the URL is the JSON body either plain or with `Content-Encoding: gzip`, depending on the setting the
request was stored under. The fetcher has to honour `Content-Encoding` on the response and cannot assume either
form; Python's `urllib`, which koboldcpp's worker uses, does not decode it automatically.

The body is bounded by the request limits. Its non-image part is the stored messages, whose text is at most 1 MiB
(`MAX_CHAT_TEXT_BYTES`) and whose JSON is no larger than the same part of the submitted body. Each of at most 5 images
is a JPEG of at most 2048 pixels a side at quality 90; random noise, the worst case for JPEG, encodes to about 3.6
MiB, or 4.8 MiB as base64, so 5 images stay under 25 MiB. The whole body therefore stays under the 36 MiB
`MAX_CHAT_BODY_BYTES`, which is the fetch cap to set on a worker for the decoded body.

A fetch that fails is a faulted job, as for an image URL. `ChatFaultReason` does not include a value for a failed
input fetch, so the fault is sent without `fault_reason`.

The object is deleted with the request's images: `TextWaitingPrompt.delete`, the expiry cleanup in
`collect_expired_chat_image_keys`, a failed activation and the delete retry set all include its key
(`list_stored_chat_object_keys`). The Horde does not set an object store lifecycle rule for `*_chat_messages.json`
objects; a chat request's TTL is 20 minutes.

### Tools

The tool and response format shapes are validated by the models in `horde/chat_contract.py` the front validates with.

| Key                   | Value                                                                              |
| --------------------- | ---------------------------------------------------------------------------------- |
| `tools`               | At most `MAX_CHAT_TOOL_COUNT` (128) `{type: "function", function: {name, description?, parameters?, strict?}}`; `name` 1 to 64 characters of `[A-Za-z0-9_-]`. An empty list or null is the same as none |
| `tool_choice`         | `none`, `auto`, `required` or `{type: "function", function: {name}}`. Only allowed with a non-empty `tools`; null is the same as none |
| `parallel_tool_calls` | Boolean. Only allowed with a non-empty `tools`; null is the same as none           |
| `response_format`     | `{type: "text"}`, `{type: "json_object"}` or `{type: "json_schema", json_schema: {name, schema, description?, strict?}}`; `schema` is required. `text` or null is the same as none |

The compact JSON of `tools`, `tool_choice` and `response_format` together may be at most `MAX_CHAT_TOOL_SPEC_BYTES`
(256 KiB); more, or more than 128 tools, returns `400 UnsupportedChatParameter`. A tool `parameters` schema and a
`json_schema` schema may nest at most `MAX_CHAT_SCHEMA_NESTING` (10) levels below their root, OpenAI's limit for
strict schemas, whether `strict` is set or not; deeper returns `400 UnsupportedChatParameter`. A `tool_choice` (any
value, `none` included) or a `parallel_tool_calls` without a non-empty `tools`, or a `tool_choice` forcing a function
`tools` does not define, returns `400 ChatRequestConflict`; the first two carry OpenAI's message, for example
"Invalid value for 'tool_choice': 'tool_choice' is only allowed when 'tools' are specified.".

A `json_schema` with `strict` `true` follows OpenAI's structural rules: every object schema, at the root and in every
subschema (`properties`, array `items`, `$defs`, `anyOf` and the rest), sets `additionalProperties: false` and lists
each of its properties in `required`. A schema breaking them returns `400 UnsupportedChatParameter` with OpenAI's
message, such as "Invalid schema for response_format 'colour': In context=('properties', 'shade'),
'additionalProperties' is required to be supplied and to be false.". Other keywords, such as `minimum`, are accepted,
as OpenAI accepts them. A schema without `strict` `true` is not checked structurally.
A tool definition with `function.strict` `true` follows the same rules for its `parameters`, refused with `param`
`tools[i].function.parameters` and a message such as "Invalid schema for function 'get_weather': In context=(),
'additionalProperties' is required to be supplied and to be false."; a tool without it is not checked structurally.

An `assistant` message may carry `tool_calls`, 1 to `MAX_CHAT_TOOL_CALL_COUNT` (128) `{id, type: "function",
function: {name, arguments}}`, and then a null or absent `content`. A `tool` message carries `role`, `content` (a
string or text parts) and `tool_call_id` (1 to 256 characters). The history follows OpenAI's rule
(`check_tool_history` in `horde/chat_contract.py`, shared with the front): an assistant message with `tool_calls` is
followed at once by `tool` messages answering each of its ids exactly once, in any order among themselves, and the
ids within one assistant message are unique. An id may recur in a later round. Each break returns
`400 UnsupportedChatParameter`, the message prefixed with the request path:

| History                                                         | Path                         | Message (OpenAI's where OpenAI has one) |
| --------------------------------------------------------------- | ---------------------------- | --------------------------------------- |
| A message other than `tool` while calls are unanswered          | `messages[i].role`           | "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. The following tool_call_ids did not have response messages: ..." |
| A `tool` message with no open calls, or a second answer to an id | `messages[i].role`           | "Invalid parameter: messages with role 'tool' must be a response to a preceding message with 'tool_calls'." |
| A `tool` message answering an id the preceding assistant message did not issue | `messages[i].tool_call_id` | "Invalid parameter: 'tool_call_id' of '...' not found in 'tool_calls' of previous message." |
| An id repeated within one assistant message                     | `messages[i].tool_calls`     | The repeated id                          |
| The history ends with calls unanswered                          | `messages`                   | As for the first row                     |

Both are stored and sent to the worker unchanged, with an assistant message's `content` stored as null when it was
absent.

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

`tool_choice` `none` beside tools is stored, since the request's guarantees, the dispatch narrowing below and the
operation fingerprint depend on it. The column is NULL when the request does not apply any of them. The stored values are
the client's; only the worker's copy is narrowed (see [Chat job pop response](#chat-job-pop-response)).

### Refused keys and values

Any key outside the lists above returns `400 UnsupportedChatParameter`, and the message holds the key. This covers
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
  across every string content and text part and every history tool call's function name and arguments), the limits
  the front shares from `horde/chat_contract.py`.

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

Under `truncate_oldest` the context first grows as under `grow`. When the messages do not fit at the growth bound, the
request runs under the larger of that bound and the requested length, and its oldest turns are dropped one at a time
until the token estimate of the remaining messages fits (`truncate_oldest_messages`, `horde/classes/kobold/chat_request.py`).
A turn is a `user` message with the `assistant` message right after it, or an `assistant` message carrying
`tool_calls` with the `tool` messages answering it; any other message is a turn by itself. Every `system` and
`developer` message is kept wherever it sits, as are the latest `user` message and everything after it. A request that
still does not fit returns `400 PromptExceedsContext`, as under `grow`. A trimmed request records both changes:

```json
[
  {"from": 2048, "to": 8192, "reason": "grow"},
  {"from": 21000, "to": 7900, "reason": "truncate_oldest", "dropped_messages": 10}
]
```

On a `truncate_oldest` entry, `from` and `to` are the token estimates of the submitted and the kept messages, and
`dropped_messages` is how many messages were dropped; a `grow` entry does not carry it. The grow
entry is absent when the requested context was already at or above the bound. The `horde.text.context_fit` counter records a
trimmed request with the outcome `truncated` and the mode `truncate_oldest`. A dry run is trimmed the same way, so its
quote and `estimated_prompt_tokens` describe the kept messages; its `resolved.context_fit` reports `grow`, the mode
the text sizing ran under.

## Flattened prompt

The request's `prompt` column holds the messages flattened into one string: each message as `role: text`, with the
parts of a message and the messages themselves joined by newlines and each image part rendered as `[image]`
(`CHAT_IMAGE_PLACEHOLDER`). An assistant message's `tool_calls` follow its text as compact JSON, a `tool` message is
`tool: <text>`, and the compact JSON of the stored `tools` follows the messages, since the worker renders the
definitions into its prompt. A response format schema is not part of it: the reference worker compiles the schema into
a grammar and never renders it into the prompt. It is an estimate only. The character-based
token estimate, `context_fit` sizing, kudos and moderation read it; workers do not receive it. `submitted_prompt` holds
the flattened messages as submitted.

Every rewrite validation makes to the flattened prompt is mirrored onto the messages. The replacement filter applied
for flagged users and NSFW models is applied to each message text, a text the filter empties becomes an empty string,
and the prompt is flattened again from the result. `messages` therefore always holds what workers receive. Under
`truncate_oldest`, `messages` and `prompt` hold only the kept turns, and images in dropped turns are not fetched,
stored or charged; `submitted_prompt` holds the full flattened submission, which moderation screened. Tool call
arguments, the tool definitions and the response format are JSON documents and are not rewritten.

## Chat declaration on pop

`POST /v2/generate/text/pop` takes six further fields. The worker row stores them at every pop, including a pop
inside the 30 second window in which the other check-in fields are not rewritten, and the pop is matched against
them. A pop without `chat_jobs` therefore withdraws the whole declaration and does not receive a chat job.

| Field           | Type                                   | Required        | Meaning                                                            |
| --------------- | -------------------------------------- | --------------- | ------------------------------------------------------------------ |
| `chat_jobs`     | boolean                                | no, `false`     | The worker accepts chat jobs; stored as `accepts_chat_jobs`         |
| `prompt_jobs`   | boolean                                | no, `true`      | The worker accepts text jobs; `false` needs `chat_jobs` and makes the worker chat-only; stored as `accepts_prompt_jobs` |
| `chat_features` | array of strings                       | no              | The optional features and the guarantees below that the worker declares, in one list; unknown values are ignored, absent means none |
| `chat_limits`   | `{max_context_length, max_length, tools_max_temperature?}` | with `chat_jobs` | The limits chat jobs run under; the text pop's limits bound only text jobs. `tools_max_temperature` is stored as `workers.chat_tools_max_temperature` |
| `model_id`      | string                                 | with `chat_jobs` | The model the worker serves chat jobs with; exactly one of `models` |
| `image_transports` | array of `url`, `base64`, `messages_url` | no         | How the worker receives images and offloaded messages; absent or empty means none; unknown values are ignored. Only `url` and `base64` let it receive images; `messages_url` is declared beside them ([Messages offload](#messages-offload)) |

A pop that sets `chat_jobs` without `chat_limits` or `model_id`, with a negative `chat_limits.tools_max_temperature`,
or with a `model_id` that is not one of `models`, or sets `prompt_jobs` to `false` without `chat_jobs`, returns
`400 ChatDeclarationInvalid`. The native route treats `model_id` as an opaque string and does not reserve any
character in it.

The feature vocabulary (`ChatFeature`) grows by adding values; a value never changes meaning:

| Feature             | A request needs it when                       |
| ------------------- | --------------------------------------------- |
| `seed`              | it sends `seed`, whatever its value            |
| `presence_penalty`  | it sends a non-zero `presence_penalty`         |
| `frequency_penalty` | it sends a non-zero `frequency_penalty`        |
| `vision`            | a message carries an image part; the worker also has to declare an image transport |
| `tools`             | it stores `tools` with a `tool_choice` other than `none`, or any message is a `tool` message or carries `tool_calls` |
| `json_object`       | its `response_format` type is `json_object`    |
| `json_schema`       | its `response_format` type is `json_schema`    |
| `tools_with_response_format` | it forwards tool definitions (a `tool_choice` other than `none`) and stores a `response_format`; the backend applies the format to the final text answer and not to tool calls, as OpenAI does |

A field at its documented default does not need a feature. Features are capabilities, so a request needs them under both
conformance modes.

### Guarantees

A guarantee (`ChatGuarantee`) states that the backend follows one part of the OpenAI contract exactly (ADR 0036).
Guarantees share the `chat_features` list and namespace with the features above:

| Guarantee                    | A `strict` request needs it when                                              |
| ---------------------------- | ----------------------------------------------------------------------------- |
| `tool_choice_enforced`       | it forwards tools under `tool_choice` `required` or a function choice          |
| `single_tool_call_enforced`  | it forwards tools with `parallel_tool_calls` `false`                          |
| `structured_output_enforced` | its `response_format` is `json_schema` with `strict` `true`                   |
| `prompt_overflow_refused`    | always                                                                        |

Tools are not forwarded under `tool_choice` `none`, so `none` does not need a tool guarantee. A `best_effort`
request does not need a guarantee. The SDK's `required_chat_guarantees` defines these rules.

`required_chat_features` is the only place a request's features and guarantees are derived: it runs once when the
request is created and stores the sorted values in `waiting_prompts.chat_required_features`, which
`TextWorker.chat_refusal_reason` compares with the worker's `chat_features`, so eligibility never loads a candidate's
messages or extras. The conformance mode is stored in `waiting_prompts.chat_conformance`.

### Tool temperature limit

`chat_limits.tools_max_temperature` is the highest temperature the backend applies as sent when tools are present.
`waiting_prompts.chat_tools_temperature` holds the temperature the job carries when the request forwards tools, NULL
otherwise, and is written once when the request is created. A worker whose limit is below that value is refused with `tools_max_temperature`,
under both conformance modes, since a clamped value changes the request. A worker without the limit applies every
temperature as sent. The SDK's `violated_chat_limit` defines the rule; the server applies it to the stored value.

### Eligibility

A chat request goes to a worker only when every rule holds. The queue query applies the cheap ones; `can_generate`
applies the rest and repeats them for worker availability:

1. The worker declared `chat_jobs` on its latest pop and its `model_id` is the request's model.
2. Every feature and guarantee in the request's `chat_required_features` is in the worker's `chat_features`, and a request with
   images finds `url` or `base64` in its `image_transports`. The queue query withholds requests with images from a worker without both,
   testing `messages` for an image part by JSONB containment on chat rows only, and computes the same test for the
   returned page so `can_generate` does not load candidates' messages.
3. The request's `max_context_length` and `max_length` fit the worker's `chat_limits` (its text limits do not
   apply), and its `chat_tools_temperature` does not exceed the worker's `tools_max_temperature`. The queue query
   applies both.
4. The usual restrictions hold: `only` and `ignore`, trusted, validated, slow workers.

A request no online worker can serve is accepted and waits, with `is_possible` false on the status route. A worker
that predates chat never sees a chat request, and a text request goes to a chat worker under its text limits.

### Impossible reason

When no worker is eligible and nothing is processing, the chat status route also reports `impossible_reason` if the
cause is the workers' declarations: every online chat worker serving the model that passes the queue query's rules
was refused for a missing capability or guarantee (rule 2) or for a chat limit the request exceeds (rule 3):
`max_context_length`, `max_length` or `tools_max_temperature`. The availability pass leaves the size limits out of
the query so that a worker below them reaches `can_generate` and its refusal counts; the queue query and the check at
creation still apply them in SQL.

```json
{"kind": "declaration_gap", "missing_declarations": ["tool_choice_enforced"], "exceeded_limits": []}
```

`missing_declarations` lists the values the request needs that a refused worker does not declare, with
`image_transports` for a worker that declares `vision` without an image transport. `exceeded_limits` lists the
limits the request exceeds, such as `max_length` or `tools_max_temperature`. The field is `null` otherwise, including when no online
worker serves the model or one is refused for any other reason, since those can change while the request waits.
It is computed from the refusal reasons the availability pass already collects, only when that pass finds no
eligible worker, and is cached with the availability for 60 seconds. Workers come and go, so the reason describes the
online workers at the time of the poll. The OpenAI front answers a gap of only `max_length` or
`max_context_length` with a 400 and every other gap with a 404 (see
[No eligible worker](chat_front.md#no-eligible-worker)).

When a chat request is assigned, the generation records the worker's `model_id`, chat context and chat length as
`assigned_model`, `assigned_max_context_length` and `assigned_max_length`, so a later change to the declaration does
not redefine work already handed out.

### Chat job pop response

A text job's pop response does not carry a `chat` key. A chat job's response has the same top-level fields plus
`chat: true`, and its payload is:

| Field                                                                  | Value                                                         |
| ---------------------------------------------------------------------- | ------------------------------------------------------------- |
| `messages`                                                             | The stored array; roles `system`, `user`, `assistant`, `tool`; `tool_calls` and `tool_call_id` as stored; image parts as in [Images](#images). Absent when `messages_url` is sent |
| `messages_url`                                                         | Only for a generation assigned the `messages_url` transport: a signed URL, valid for 120 seconds, to the array as in [Messages offload](#messages-offload) |
| `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`       | Each only when stored in `chat_extras`, narrowed as below     |
| `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `rep_pen`, `top_k`, `min_p` | Always present; the client's value or `1.0`, `1.0`, `0`, `0`, `1.0`, `0`, `0` |
| `stop_sequence`, `seed`                                                | Only when the client sent them                                |
| `max_length`, `max_context_length`                                     | The validated budgets                                         |
| `n`                                                                    | `1`                                                           |

The tool keys are narrowed for the worker (`build_worker_chat_extras`): under `tool_choice` `none` the pop does not send
`tools`, `tool_choice` or `parallel_tool_calls`; under a `tool_choice` that specifies a function only that function is sent in `tools`,
with `tool_choice` `required`. `auto` and `required` are sent as stored.

The payload does not carry a `prompt`. The worker applies its chat template, tokenises the rendered input, and
fails a job that does not fit; it does not truncate the input. Every sampling field is present, so a worker's
defaults never apply to a chat job.

## Structured submit

`POST /v2/generate/text/submit` keeps `generation` for text jobs. A chat job sends these instead:

| Field                  | Type                                      | Required | Meaning                                                    |
| ---------------------- | ----------------------------------------- | -------- | ---------------------------------------------------------- |
| `message`              | `{role: "assistant", content, tool_calls?, reasoning_content?}` | yes    | The reply; an empty `content` is a completed reply. `tool_calls` holds 1 to 128 calls as minted by the backend; `content` may be null only with them. `reasoning_content` is the model's reasoning, never part of `content`; see [Reasoning](#reasoning) |
| `finish_reason`        | `stop`, `length`, `content_filter` or `tool_calls` | yes | Why the worker stopped; `tool_calls` needs a non-empty `message.tool_calls`. A forced `tool_choice` may finish with `stop` and calls |
| `native_finish_reason` | string, at most 32 characters             | no       | The backend's reason                                       |
| `prompt_tokens`, `completion_tokens` | integer, 0 to 10,000,000    | no       | Omit a count the backend did not report; stored as null     |
| `reasoning_tokens`     | integer, 0 to 10,000,000                  | no       | How many of `completion_tokens` were reasoning. Omit it when the backend did not report it; stored as null |
| `model_id`             | string                                    | yes      | Has to be the generation's `assigned_model`                 |
| `state`                | `ok`, `censored`, `csam`, `faulted`       | no       | As for text jobs                                           |
| `fault_reason`         | string                                    | no       | With `state` `faulted` only: why the worker faulted the job; see [Worker fault reasons](#worker-fault-reasons) |

A body carrying `message`, `model_id`, `finish_reason` or `fault_reason` is parsed as a chat submit, which does not require
`generation`; any other body is parsed as a text submit, so the parser refuses a text submit without `generation`.
A chat submit without a valid `message` or `finish_reason`, or with `generation` as well as `message`, returns
`400 ChatSubmitInvalid`, as does a text job's submit carrying `message`. A `model_id` other than the assigned model
returns `400 ChatModelMismatch`. A faulted chat job submits `{id, state: "faulted", model_id}` without `generation`
or `message`, and optionally `fault_reason`; it is handled as any faulted text submission.

A request whose faulted jobs exceed `FAULTED_GENERATIONS_LIMIT` (2) is faulted on the submit that crosses it, for
text and image requests alike, and a chat request publishes its `faulted` signal at once. Without this, a request
that fails on every attempt is handed out again after each fault until the stale-generation thread faults it, and
each fault counts toward the worker's aborted-job threshold. The thread applies the same limit to requests whose
jobs timed out.

A request is `done` when it is `faulted`, or when no job is waiting or processing and either all requested results
are stored or the remaining jobs were cancelled. Cancellation retains partial results and lets in-flight jobs finish,
but faults after cancellation do not restart jobs. A cancelled request can therefore be `done` without a generation;
the front reports `ChatRequestNoGeneration` when it does not have a reply to deliver. The cancel route also tells a worker
streaming progress for the request to stop (see [Streaming progress](#streaming-progress)). For a streamed
request it settles each in-flight generation on the content its worker posted (see [Pricing](#pricing)); the
generation is faulted as cancelled, does not count toward the worker's aborted-job threshold and is not restarted.

Faulted generations do not count as results. A job's fault and its restart are committed together, as are a pop's
taken job and its first generation. A worker-reported image fault that is not restarted and leaves nothing waiting
or processing with results missing faults the request in the same commit; image timeouts retain their retry policy.
The status reads completion, cancellation, the fault flag and fault details in one statement before loading the
generations. That statement adds one query to every status read and every completion check of a waiting request,
image `/check` included.

The submit goes through the text path's compare-and-set: one update claims the pending generation and writes
`generation` (the message content), `message`, `finish_reason`, `native_finish_reason`, the counts and `charge_basis`
together, so a submit that loses to a cancel or a duplicate does not write any of them, and every predicate that reads
`generation` treats chat and text alike. `message` is stored with `tool_calls` when present and NUL replaced in every
string (`build_stored_reply`); `generation` holds the content, or an empty string when it is null. An empty `content`
completes the generation, is charged and rewarded, and counts towards the per-model empty-generation counter unless
the reply carries tool calls, which are a complete reply.

### Reasoning

A reasoning model, such as Qwen3, emits a `<think>` block before the answer. The worker submits it as
`message.reasoning_content` and never inside `content`, an unclosed block cut off at `max_length` included. The Horde
stores it in `message` beside `content`, with NUL replaced, only when it was submitted, and the status shows it there.
It is not the reply: `generation` holds the content alone, the charge and reward are computed on the content and tool
calls (`chat_reply_characters`), and the conformance rules do not read it. Its UTF-8 length is capped at the SDK's
`MAX_CHAT_TEXT_BYTES` (1 MiB), the most message text a request may carry, since the kudos cost does not bound what is
stored as reasoning; a longer one returns `400 ChatSubmitInvalid`. `reasoning_tokens` is stored in a separate column and shown on
the status. Like the other reported counts it never changes the charge or reward, since the Horde pays only for the
output it measures.

### Reply verification

Before the compare-and-set, `verify_chat_reply` checks the reply against the request's stored `chat_extras` (the
client's values, not the narrowed copy the worker received) under its `chat_conformance`. It is the third layer of
ADR 0036, after eligibility and dispatch narrowing, and does not trust any worker declaration. A refused submit leaves the
generation pending, the koboldcpp worker faults the job on any `400` return code, and the request faults after
`FAULTED_GENERATIONS_LIMIT` faulted jobs, so the checks cover only what the payload proves and what OpenAI never
returns.

| Rule                                                                                | `strict`                         | `best_effort`      |
| ----------------------------------------------------------------------------------- | -------------------------------- | ------------------ |
| A call to a function not among the request's `tools`, or any call when it has none   | `400 ChatSubmitInvalid`          | same               |
| Two calls with the same `id`                                                        | `400 ChatSubmitInvalid`          | same               |
| A call under `tool_choice` `none`                                                   | `400 ChatSubmitNonconformant`    | passes             |
| No call under `tool_choice` `required`                                              | `400 ChatSubmitNonconformant`    | passes             |
| No call, or a call to another function, under a function `tool_choice`              | `400 ChatSubmitNonconformant`    | passes             |
| More than one call under `parallel_tool_calls` `false`                              | the first call is kept           | all calls kept     |
| `json_object`, or `json_schema` without `strict`: content that is not a JSON object  | `400 ChatSubmitNonconformant`    | passes             |
| `json_schema` with `strict` `true`: content that does not match the schema           | `400 ChatSubmitNonconformant`    | passes             |

The single-call reduction runs first, so a call it drops is not checked, and the finish reason is kept as submitted.
Under either mode, a `tool_calls` finish of a reply with calls to a request whose `tool_choice` specifies a function is stored as
`stop` (`chat_reply_finish_reason`), since OpenAI finishes such a choice with `stop` and the worker received the
choice as `required`.
The submit's `horde.job_submit.done` log record then carries `dropped_tool_calls`. A reply whose `content` is empty or
null and that carries tool calls is exempt from the response format. JSON is parsed by Python's `json` with `NaN` and
`Infinity` refused. A strict schema is validated with `jsonschema`, by the draft its `$schema` declares and Draft
2020-12 otherwise; references resolve only inside the schema. A schema the library cannot compile, or whose
references do not resolve, refuses the reply with `ChatSubmitNonconformant` and a message saying the schema cannot
be compiled, since the reply cannot be shown to match it. The request's schema is not checked when it is created.

### Refusal record and worker counters

A refusal from the submit route (`ChatSubmitInvalid`, `ChatSubmitNonconformant` or `ChatModelMismatch`) of a reply to
a pending generation is stored before the `400` is sent, in its own commit (`TextJobSubmit.record_chat_refusal`,
`TextWaitingPrompt.record_chat_refusal`). The request's `chat_fault_reason` takes the message, cut to
`MAX_CHAT_FAULT_REASON_LENGTH` (1000) characters, and `chat_fault_rc` the return code; a later refusal overwrites
both, and they are not cleared. The generation's `chat_refusal_rc` takes the return code. A failed write is logged and
the refusal is still sent.

Once the request is faulted, the chat status reports `fault_reason` and `fault_rc` from those columns; both are null
while it is not faulted, or when the Horde did not refuse a reply to it and no worker sent a known
[fault reason](#worker-fault-reasons). The `faulted` signal carries them too (see
[Completion signal](#completion-signal)).

When the worker's fault submit for a generation arrives, `TextProcessingGeneration.log_worker_abort` reads the stored
`chat_refusal_rc`, so a worker cannot claim the exemption itself:

| Stored `chat_refusal_rc`        | Worker counter                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------ |
| `ChatSubmitNonconformant`       | `nonconformant_replies` is incremented; `aborted_jobs` and `uncompleted_jobs` are not             |
| any other, or none              | `aborted_jobs` and `uncompleted_jobs` are incremented, as for any dropped job (`log_aborted_job`) |

A nonconformant reply is a well-formed reply that broke a `strict` request's contract: the worker generated and the
model failed the contract, so it does not count toward the hourly aborted-job threshold that puts a worker into
maintenance. The job still does not earn kudos. The same rule applies when the stale-generation thread aborts a generation
whose reply was refused as nonconformant. Worker details (`GET /v2/workers`, `/v2/workers/{id}`,
`/v2/workers/name/{name}`) and the moderation worker report expose `nonconformant_replies`.

### Worker fault reasons

A faulted chat submit may carry `fault_reason`, one of the SDK's `ChatFaultReason` values
(`horde_sdk.worker.chat_declaration`). The horde stores the message for the value from
`CHAT_WORKER_FAULT_MESSAGES` in `horde/classes/kobold/chat_request.py` and does not store text from the worker:

| `fault_reason`        | When the worker sends it                                                | Stored `chat_fault_reason`                                                             |
| --------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `prompt_overflow`     | The rendered messages do not fit the worker's context                   | The worker could not fit the prompt into its context.                                  |
| `unbuildable_grammar` | The backend cannot build the grammar for `tool_choice` or `response_format` | The worker could not build the grammar the request's tool choice or response format needs. |
| `tool_violation`      | The model's reply broke the request's tool controls and was not submitted | The model's reply broke the request's tool controls.                                   |
| `reply_refused`       | The horde refused the worker's reply with a `400`                       | The AI Horde refused the worker's reply.                                               |
| `generation_error`    | The backend failed while generating                                     | The worker's backend failed while generating.                                          |

`TextJobSubmit.record_chat_worker_fault` writes the message to `chat_fault_reason` and `ChatWorkerFault` to
`chat_fault_rc` before the fault is applied, in its own commit (`TextWaitingPrompt.record_chat_worker_fault`), so the
status and the `faulted` signal of a fault that crosses `FAULTED_GENERATIONS_LIMIT` carry it. The route does not write
anything when the generation is not pending, or when the horde refused a reply of the same generation: that refusal's message
is more specific, and is what a worker's `reply_refused` follows. A missing or unknown value is not stored and the
fault is accepted as one without a reason, so a worker sending a reason this horde does not know is unaffected. The value is ignored
on every submit other than a chat job's fault. A failed write is logged and the fault is still applied. The fault
counts toward the worker's counters as the table in [Refusal record and worker counters](#refusal-record-and-worker-counters)
states; a reason does not exempt it.

### Pricing

A chat generation is charged and rewarded exactly as a text generation with the same reply, through the text kudos
formula, and stores `charge_basis` `output_estimate`. The reply's characters are the content plus each tool call's
function name and arguments (`chat_reply_characters`, read by `TextProcessingGeneration.get_things_count`). The
formula's token count is those characters divided by 4, rounded up, at least 20 and at most the requested `max_length`, so a short reply to a large `max_tokens` is not
charged the full request. Reported token counts are stored and shown on the status route and never change the charge.
The basis is computed in one place, `TextProcessingGeneration.get_charge_basis`. The status route returns, per
generation, `message` (with `tool_calls` and `reasoning_content` when the worker submitted them), `finish_reason` as
submitted, `native_finish_reason`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `charge_basis` and
`queue_wait_seconds`, the seconds from
the request's creation to the generation's assignment. The text status route does not return any of them.

A streamed generation still in flight when the client cancels is settled at the cancel on the content its worker
posted, under the same output estimate: `read_posted_progress` (`horde/chat_progress.py`) joins the generation's
`content` entries from the request's stream in `seq` order, reasoning aside, and
`TextProcessingGeneration.settle_cancelled` prices those characters, pays the worker, charges the requester with
the usual extra burn and faults the generation as cancelled. The posted content is measured by the Horde, so it is
a charge input where a worker-reported count is not. A cancel before any post pays the floor of 20 tokens. When
the stream does not hold the generation's entries, the lease's accepted `bytes` stand in for the characters;
they count a multibyte character more than once and include reasoning, so the rare fallback leans toward the
worker. A Redis failure settles on the floor. The settlement records the character count it priced in the
generation's `settled_character_count`, which is null on every other generation and is not shown on the status
route. The posted content is not stored as the generation's reply, so the
status does not list a generation for it, and `charge_basis` and `finish_reason` stay null on the cancelled
generation. The Horde does not measure partial work on a request that does not stream: its worker submits after the
cancel and is paid for the reply as usual.

## Chat model list

`GET /v2/status/models?type=text&chat=true` lists only the text models that at least one online chat worker serves,
each with the usual fields plus a `chat` object. Without `chat=true` the entries do not carry `chat` and the
response is the plain list. `chat=true` with another `type` returns `400`.

The text `count` leaves out the threads of workers that set `prompt_jobs: false`, since they do not serve text requests.
A model served only by such workers is still listed, with `count` 0, and `chat.workers` reports its chat workers.

| `chat` field         | Value                                                                                  |
| -------------------- | -------------------------------------------------------------------------------------- |
| `workers`            | Online workers, not in maintenance, that serve chat jobs with the model                |
| `max_context_length` | The largest `chat_limits.max_context_length` among them                                |
| `max_length`         | The largest `chat_limits.max_length` among them                                        |
| `features`           | The sorted union of their `chat_features`, guarantees included                         |
| `created`            | Unix seconds of the earliest `workers.created` among them; `null` when none records it |

The entry's `queued` and `jobs` are the model's queue, which chat requests share with text requests. The entries come
from the same `models_cache` the plain list reads, filtered as usual; the chat object comes from one query over the
online chat workers. Both responses go through the route's two-second cache, keyed by the query string. `created`
changes when the earliest of those workers goes offline; the Horde does not keep a first-seen time per model.

## Streaming progress

A request sent with `stream: true` is stored with the stream hint, `stream_progress: true` in `chat_extras`, and its
job payload carries the same key. The hint does not change the reply and is not in the operation fingerprint. A worker posts
the text it generated so far to `POST /v2/generate/text/progress` only for a job whose payload carries the hint, and
still submits the whole reply to the submit route.

| Field           | Value                                                                                     |
| --------------- | ----------------------------------------------------------------------------------------- |
| `apikey` header | The worker owner's API key, as on submit                                                  |
| `id`            | The generation id from the pop                                                            |
| `seq`           | 1 for the first post of the generation, then one more for each                            |
| `delta.content` | The text since the previous post; empty only when `delta.reasoning_content` is set        |
| `delta.reasoning_content` | Optional: the reasoning since the previous post, never part of `delta.content`  |

A delta whose `content` and `reasoning_content` are both empty or absent returns `400 BadRequest`. Reasoning counts
toward the byte cap with the content. An accepted post returns `200` with `{"accepted_seq": <seq>, "cancelled":
<bool>}`; `cancelled` is `true` once the request was cancelled. A post that
repeats the last accepted `seq` with the same content and reasoning returns the same body and does not append an entry, so a worker retries a post whose answer it lost by sending it
again unchanged.

| Status | Return code                 | Meaning                                                                      |
| ------ | --------------------------- | ---------------------------------------------------------------------------- |
| `400`  | `BadRequest`                | The delta carries neither content nor reasoning                              |
| `404`  | `ChatProgressNotStreaming`  | The generation has no lease: not streamed, already submitted or faulted, expired or unknown. Stop posting; still submit |
| `403`  | `WrongCredentials`          | The API key is not the key of the worker's owner                             |
| `409`  | `ChatProgressOutOfSequence` | `seq` is not the next one, or repeats the last one with other content. The body adds `expected_seq` |
| `413`  | `ChatProgressCapReached`    | The generation reached its entry or byte cap. Stop posting; still submit     |
| `503`  | `ChatProgressUnavailable`   | Redis could not be reached                                                   |

When a generation carrying the hint is assigned, the pop writes the lease `horde:chat:lease:<generation id>` on the
chat signal database: a hash of `request`, `worker`, `model`, `owner` (the owner's user id), `key` (a SHA-256 of the owner's
stored key hash), `max_bytes`, `fake` and, once the request is cancelled, `cancelled` (`1`). It expires `CHAT_PROGRESS_LEASE_GRACE_SECONDS` (90) after the job's TTL,
which covers the stale-generation thread's 60 second tick. The route reads only the lease and does not read Postgres; a key reset
during the job therefore takes effect for posts only once the lease ends. A fake generation's posts are answered as a
real one's and not appended.

One Lua script checks and appends a post atomically. The lease hash also holds the last accepted `seq`, the bytes
accepted so far and the last content and reasoning. An accepted delta is appended to the stream
`horde:chat:stream:<request id>` with fields `gen` (the generation id), `seq`, `content`, `reasoning_content` when the
delta carries reasoning, and the lease's `worker` and `model`, from which the front
computes the reply's `system_fingerprint` at the first delta. A lease without a `model` field gives entries without
one. Caps, in `horde/chat_progress.py`:

| Constant                            | Value                     | Reason                                                                 |
| ----------------------------------- | ------------------------- | ---------------------------------------------------------------------- |
| `MAX_CHAT_PROGRESS_ENTRIES`         | 1000 per generation       | One post per second covers past the front's 600 second stream budget   |
| `CHAT_PROGRESS_BYTES_PER_TOKEN`     | 16 bytes per `max_length` token | A token decodes to a few characters of at most four UTF-8 bytes  |
| `CHAT_PROGRESS_STREAM_TTL_SECONDS`  | 900, reset on each append | The front's stream budget plus 300 seconds for a stalled reader        |
| `CHAT_PROGRESS_STREAM_MAX_ENTRIES`  | 4000, approximate trim    | Room for every attempt a request gets before its faults fault it       |

A successful submit deletes the lease and leaves the stream to expire, so a reader reconciles what it relayed with
the final reply. When a generation with a lease faults, on a faulted submit or in the stale-generation thread, the
stream gets an entry with `gen` and `ended: 1` and the lease is deleted. The route never publishes on the
completion signal channel.

When the cancel route claims a streamed request's cancellation, it sets `cancelled` on the lease of each generation
still in flight and appends that generation's `gen` and `ended: 1` entry, in one transaction that writes only leases
that still exist. The leases are kept, so the worker's next post is still accepted, and its `200` carries `cancelled:
true`; the delta it carries was generated before the worker read the flag and is harmless after the ended marker.
The worker then stops generating and does not submit. Before flagging the leases, the route settles each of those
generations on the content the stream holds for it (see [Pricing](#pricing)), so a submit that arrives after the
cancel, from a worker that did not post again, returns `400 AbortedGen`. Maintenance mode's abort does not flag the leases, since it
lets in-flight jobs settle. A fake generation's lease is flagged without an `ended` entry.

Writing, flagging and deleting the lease and the `ended` entry are best effort: a Redis failure is logged and never
fails the pop, submit, fault or cancel.

## Completion signal

Chat requests publish to Redis channel `horde:chat:<request id>`, on the host the cache uses. The publishing client
uses logical database `chat_signal_db` (7); Redis delivers a pub/sub message to subscribers on any logical database,
so a subscriber only needs the host. Each message is `{"id": "<request id>", "event": "<event>", "generations": <n>}`,
where `generations` counts the generations with a stored result. A `faulted` message adds `fault_reason` and
`fault_rc` when a reply to the request was refused or a worker sent a known fault reason, so a client that finds the expired row gone still learns why. A
message never carries generated text.

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
false), `accepts_prompt_jobs` (boolean, not null, default true), `chat_features` (text array), `chat_max_context_length`, `chat_max_length`, `chat_model_id` and
`image_transports` (text array); and `processing_gens.message` (JSONB), `finish_reason` and `native_finish_reason`
(varchar 32), `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `charge_basis` (varchar 16), `assigned_model`,
`assigned_max_context_length`, `assigned_max_length` and `image_transport` (varchar 16); and
`waiting_prompts.operation_key` (UUID), `operation_fingerprint` (char 64),
`context_adjustments` (JSONB), `chat_extras` (JSONB, deferred in the ORM), `chat_required_features` (text array),
`chat_conformance` (varchar, not null, default `strict`), `chat_tools_temperature` (double precision) and
`chat_messages_offloaded` (boolean, not null, default false); and
`workers.chat_tools_max_temperature` (double precision); and `waiting_prompts.chat_fault_reason` (varchar 1000) and
`chat_fault_rc` (varchar 32), `processing_gens.chat_refusal_rc` (varchar 32) and `settled_character_count`
(integer), and `workers.nonconformant_replies`
(integer, not null, default 0). Every column is nullable or has a constant default, so the migration does not rewrite a table. The
unique partial index `ix_waiting_prompts_user_operation_key` on `(user_id, operation_key) WHERE operation_key IS NOT
NULL` is built with `CREATE UNIQUE INDEX CONCURRENTLY`, which cannot run inside a transaction block; a failed
build leaves an invalid index. `db.create_all()` does not add columns to existing tables.

## Related

- [Prompt provenance reference](prompt_provenance.md)
- [Style contract reference](style_contract.md), for `context_fit`
