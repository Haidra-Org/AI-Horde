---
title: "Serve chat jobs from a text worker"
summary: "Declare chat on text pop, recognise a chat job, render and fit it, serve tools, structured output and images, and submit the structured reply, with koboldcpp's embedded worker as the worked example."
topics: [chat, workers]
order: 62
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Serve chat jobs from a text worker

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [chat](../topics.md#chat), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

A chat job is a text job whose payload carries an OpenAI `messages` array in place of a prompt. A text worker opts in
by adding a chat declaration to `POST /api/v2/generate/text/pop`, renders each chat job with the chat template of the
model it has loaded, and submits a structured assistant message to `POST /api/v2/generate/text/submit`. Every field
named here is specified in the [chat requests reference](../reference/chat_requests.md); this page is the procedure.

Preconditions: a text worker that pops `/api/v2/generate/text/pop` and submits `/api/v2/generate/text/submit` with a
registered API key; a backend that can render a conversation with the loaded model's chat template and count the
rendered tokens; and a horde at `<HORDE_URL>` that serves the `/api/v2/generate/chat` routes.

End state: the worker's model appears in `GET <HORDE_URL>/api/v2/status/models?type=text&chat=true` with the
features and limits the worker declared, the worker completes chat jobs with structured replies, faults the ones it
cannot serve, and keeps receiving text jobs.

For why chat travels this way, see [Chat requests on the Horde](../explanation/chat_requests.md). To send chat
requests as a client, see [Send chat completions through the OpenAI-compatible front](send_chat_completions.md).
To run this end to end with koboldcpp for the first time, follow [Serve your first chat job with koboldcpp](../tutorial/first_chat_worker.md).

## 1. Declare chat on pop

Add five fields to every pop body, beside the usual `name`, `models`, `max_length` and `max_context_length`
([Chat declaration on pop](../reference/chat_requests.md#chat-declaration-on-pop)):

```json
{
  "chat_jobs": true,
  "chat_features": ["seed", "presence_penalty"],
  "chat_limits": {"max_context_length": 4096, "max_length": 512},
  "model_id": "<model-name>",
  "image_transports": []
}
```

- `chat_jobs: true` opts in. The horde stores the declaration from every pop, so a pop without `chat_jobs` withdraws
  it, and the worker receives no chat job from that pop.
- `chat_features` lists the optional features the backend honours. Declare a name only when the backend does what it
  promises; a declaration is a claim the horde does not test ([ADR 0022](../decisions/0022-declare-chat-capabilities-by-pop-fields.md)).
  A request that needs a feature waits for a worker that declares it, so an undeclared feature costs only the
  requests that need it. Unknown names are ignored.

  | Feature             | The worker promises                                                                 |
  | ------------------- | ----------------------------------------------------------------------------------- |
  | `seed`              | The backend applies `seed` to sampling                                              |
  | `presence_penalty`  | The backend applies OpenAI's presence penalty                                       |
  | `frequency_penalty` | The backend applies OpenAI's frequency penalty, as a separate control               |
  | `vision`            | The loaded model reads image parts; an image transport is declared as well          |
  | `tools`             | The template renders tools and tool turns; the worker submits the model's calls     |
  | `json_object`       | The backend constrains the reply to one JSON object                                 |
  | `json_schema`       | The backend constrains the reply to the client's JSON Schema                        |
  | `tools_with_response_format` | The backend serves tools and a response format in one request, applying the format only to the final text answer |

  The same list carries guarantees: a guarantee states that the backend follows one part of the OpenAI contract
  exactly, where it could otherwise deviate on purpose ([ADR 0036](../decisions/0036-route-chat-requests-by-declared-guarantees-under-a-conformance-mode.md)).
  A request is `strict` unless the client opts into `best_effort`, and a strict request that needs a guarantee waits
  for a worker that declares it. Declare a guarantee only when the backend implements the behaviour; operator
  configuration can withhold a guarantee and does not add one.

  | Guarantee                    | The worker promises                                                              |
  | ---------------------------- | -------------------------------------------------------------------------------- |
  | `tool_choice_enforced`       | Under `required` or a named function the reply calls a tool (that function), never prose alone; under `none` it calls none |
  | `single_tool_call_enforced`  | Under `parallel_tool_calls: false` the reply carries at most one call            |
  | `structured_output_enforced` | A `json_schema` reply satisfies the schema, or the job is faulted; the backend does not fall back to free text |
  | `prompt_overflow_refused`    | A prompt that does not fit `chat_limits.max_context_length` faults the job and is not trimmed |

  Every strict request needs `prompt_overflow_refused`, so a worker that does not declare it serves only
  `best_effort` requests.

- `chat_limits` bounds chat jobs only. The pop's `max_context_length` and `max_length` keep bounding text jobs. Set
  `chat_limits.max_context_length` to the context the backend really has for one conversation, rendered template and
  reply together, and `chat_limits.max_length` to the most tokens it generates for one reply. A request that
  exceeds either limit is not sent to the worker. Set the optional `chat_limits.tools_max_temperature` when the
  backend lowers the temperature while tools are present: a request that forwards tools at a higher temperature is
  then not sent to the worker, under either conformance mode, since a clamped value changes the request. Leave it out
  when the backend applies every temperature as sent.
- `model_id` is the model the worker serves chat jobs with, and it has to be one of `models`. The native route treats
  it as an opaque name. The OpenAI-compatible front reserves `:` for model variants and answers 404 for any model id
  that contains one, so a model name with a colon can be served natively and cannot be reached through the front
  ([ADR 0020](../decisions/0020-select-chat-service-by-request-body-and-model-variants.md)). Choose a name without a
  colon.
- `image_transports` lists how the worker accepts images: `url`, `base64` or both. Leave it out, or send it empty,
  when the worker takes no images (see [step 5](#5-serve-images)).
- `prompt_jobs` is optional and defaults to `true`. Send `"prompt_jobs": false` with `chat_jobs: true` to serve chat
  jobs only: the worker then does not receive text jobs, does not make a text request possible, and may leave out
  `max_length` and `max_context_length`.

Verification: the pop returns 200. A pop that sets `chat_jobs` without `chat_limits` or `model_id`, with a negative
`chat_limits.tools_max_temperature`, or with a `model_id` outside `models`, or that sets `prompt_jobs: false` without
`chat_jobs`, returns `400 ChatDeclarationInvalid` and its message says which field is wrong. Within about ten seconds
(the model cache refresh) the model list shows the declaration:

```bash
curl -s "<HORDE_URL>/api/v2/status/models?type=text&chat=true" | jq '.[] | select(.name == "<model-name>") | .chat'
```

The `chat` object reports `workers`, the largest `max_context_length` and `max_length` among the online chat workers
serving the model, and the sorted union of their `features`
([Chat model list](../reference/chat_requests.md#chat-model-list)). Covered by
`tests/integration/test_statistics.py`, `test_text_models_carry_the_chat_declarations_only_when_asked_for`, and
`tests/integration/test_chat.py`, `test_an_invalid_chat_declaration_is_refused_on_pop`.

Reversal: pop without `chat_jobs`. The worker stops receiving chat jobs from that pop on. A chat job already assigned
keeps the model and limits recorded at assignment, so the worker still submits it under the old `model_id`
(`test_the_assigned_model_and_limits_survive_a_later_change_of_declaration`).

## 2. Recognise a chat job

A chat job's pop response carries `chat: true` at the top level. A text job's response does not carry a `chat` key
and is unchanged, so branch on that key ([Chat job pop response](../reference/chat_requests.md#chat-job-pop-response)).

A chat job's payload differs from a text payload in these ways:

- `messages` holds the conversation. Roles are `system`, `user`, `assistant` and `tool`; a client's `developer`
  message arrives as `system` ([ADR 0033](../decisions/0033-fill-openai-sampling-defaults-and-map-the-developer-role.md)).
  An assistant message may carry `tool_calls`, a tool message carries `tool_call_id`, and a user message may carry
  `image_url` parts.
- There is no `prompt`. The horde stores a flattened copy of the conversation for its estimates and does not send it
  to workers ([Flattened prompt](../reference/chat_requests.md#flattened-prompt)).
- `temperature`, `top_p`, `frequency_penalty`, `presence_penalty`, `rep_pen`, `top_k` and `min_p` are always present,
  with OpenAI's defaults where the client left them out. Apply them as sent and ignore the backend's local defaults,
  so one request means the same thing on every worker.
- `stop_sequence` and `seed` are present only when the client sent them.
- `max_length` and `max_context_length` are validated budgets within the worker's `chat_limits`. Generate at most
  `max_length` tokens and render within `max_context_length`.
- `tools`, `tool_choice`, `parallel_tool_calls` and `response_format` are present only when the request applies them
  (see [step 4](#4-serve-tools-and-structured-output)).

Verification: log the pop's `chat` key and the payload keys. A chat payload has `messages` and no `prompt`. Covered by
`test_a_chat_request_completes_with_its_structured_reply_and_signals`, which checks the whole payload, and
`test_a_developer_message_reaches_the_worker_as_a_system_message`.

## 3. Render and fit the conversation

Render `messages` with the chat template of the loaded model, tokenise the result, and check that the prompt tokens
plus `max_length` fit `max_context_length`. When they do not, fault the job (see [step 7](#7-fault-a-job)). Never
drop older messages to make it fit: the client sent that conversation and would receive a reply to a different one
without knowing.

The horde sizes the context from a character estimate of the flattened conversation, so its sizing is approximate.
Only the worker knows how many tokens its template and tokenizer produce, which is why the fit check belongs to the
worker ([Chat job pop response](../reference/chat_requests.md#chat-job-pop-response)).

The fit check must not run side effects. A backend's request transform may do more than render. koboldcpp's chat
transform runs a short in-process generation to choose a tool when `tool_choice` is `auto` or `required`. Running that
transform to estimate the prompt would generate once more for every chat job with tools, outside the job's budget.
koboldcpp's `horde_chat_overflows` therefore removes `tools` and `tool_choice` from a copy of the payload before
rendering it, and adds the token count of the tools' JSON instead, which bounds what the template renders of them.

Verification: send a conversation longer than `chat_limits.max_context_length` allows. The worker submits
`{"id": "<job-id>", "state": "faulted", "model_id": "<model-name>"}` and does not generate.

## 4. Serve tools and structured output

`tools`, `tool_choice`, `parallel_tool_calls` and `response_format` arrive as the client sent them, and only when they
change the answer ([Tools](../reference/chat_requests.md#tools)), with two narrowings: under `tool_choice: none` no
tool key arrives, and under a named `tool_choice` only that function arrives, with `tool_choice: "required"`. Pass
them to the backend unchanged.

- Declare `tools` only when the template renders tool definitions, assistant tool calls and tool results, and the
  worker returns the model's calls as `tool_calls`. A request needs `tools` when it offers tools under a `tool_choice`
  other than `none`, and also when its history alone contains a tool call or a tool message, since the template has
  to render those turns.
- Declare `json_object` only when the backend constrains the reply to one JSON object. A grammar for JSON in general
  is not enough: koboldcpp's shared JSON grammar is rooted at an array for tool calls, so for `json_object` it roots the
  grammar at an object.
- Declare `json_schema` only when the backend constrains the reply to the schema in
  `response_format.json_schema.schema`.
- Declare `tools_with_response_format` only when the backend serves a request carrying both tool definitions and a
  `response_format`: the model may still call a tool, and the format constrains only the final text answer, as
  under OpenAI. A request that forwards tools beside a response format needs it under either conformance mode. A
  backend whose output grammar for the format forbids the tool-call syntax, or whose tool grammar replaces the format,
  does not declare it.

Submit tool calls with the `id`, `type` and `function` the backend minted, and `content` null when the model only
called tools (see [step 6](#6-submit-the-reply)).

Verification: a request with tools waits while only workers without `tools` are online, its status reports
`is_possible: false`, and the first pop declaring `tools` receives it with the tools in the payload. Covered by
`test_a_tools_request_waits_for_a_worker_declaring_tools_and_is_not_possible_without_one`,
`test_a_tool_history_alone_needs_a_worker_declaring_tools` and
`test_a_json_schema_request_goes_only_to_a_worker_declaring_json_schema`.

## 5. Serve images

Declare `vision` only while the loaded model reads images, and declare at least one image transport with it. A
request with images goes only to a worker that declares both ([Images](../reference/chat_requests.md#images)).

At pop, each `image_url` part's `url` is replaced according to the declared transports:

- With `url` declared, it is a signed object storage URL, valid for the job's TTL and at least 600 seconds. Fetch it
  before generating, bound the bytes read, and take the media type from the bytes. The horde stores every image as
  JPEG, and an object store may answer with a generic `Content-Type`, so the header is not a reliable media type.
  koboldcpp's `horde_chat_fetch_images` reads at most 16 MiB, recognises PNG, JPEG and WebP signatures, and rewrites
  the part as a data URL for its local chat endpoint.
- With `base64` alone, it is a `data:image/jpeg;base64,` URL with the bytes inline.

The horde prefers `url` when a worker declares both ([ADR 0032](../decisions/0032-send-chat-images-to-workers-as-signed-urls.md)).
A fetch that fails is a faulted job (see [step 7](#7-fault-a-job)).

Verification: a request with an image part reaches the worker as a signed `http(s)` URL when it declares `url`, and
never reaches a worker without `vision` and a transport. Covered by `tests/integration/test_chat_images.py`,
`test_a_url_worker_receives_a_signed_url_valid_for_at_least_the_job_ttl`,
`test_a_base64_worker_receives_a_jpeg_data_url_built_after_the_queue_lock_is_released` and
`test_a_worker_without_vision_or_a_transport_never_pops_an_image_request`.

## 6. Submit the reply

Send `message`, `finish_reason` and `model_id` in place of `generation`
([Structured submit](../reference/chat_requests.md#structured-submit)):

```json
{
  "id": "<job-id>",
  "message": {"role": "assistant", "content": "Blue."},
  "finish_reason": "stop",
  "native_finish_reason": "eos_token",
  "prompt_tokens": 21,
  "completion_tokens": 3,
  "model_id": "<model-name>",
  "state": "ok"
}
```

- `message.content` is the reply text. An empty string is a completed reply. It may be null only when
  `message.tool_calls` carries the model's calls.
- `finish_reason` is one of `stop`, `length`, `content_filter` and `tool_calls`. Map the backend's reason onto them:
  `length` when generation reached `max_length`, `tool_calls` when the reply carries calls, `content_filter` when the
  backend withheld or cut the reply on a content rule, and `stop` otherwise. `tool_calls` needs a non-empty
  `message.tool_calls`; a forced `tool_choice` may finish with `stop` and calls. Report `tool_calls` as the backend
  does: the horde stores `stop` for a reply to a named `tool_choice`, which is what OpenAI returns.
- `native_finish_reason` is the backend's reason, at most 32 characters. It is optional and shown to the client
  beside `finish_reason`.
- `prompt_tokens` and `completion_tokens` are sent only when the backend reported them. Leave a count out rather
  than sending zero; the horde stores a missing count as null and shows it to the client, and the counts never change
  the charge ([Pricing](../reference/chat_requests.md#pricing)).
- `model_id` is the model the job was assigned, the `model_id` the worker declared when it popped the job.
- `state` is `ok`, `censored`, `csam` or `faulted`, as for text jobs.

The horde refuses a submit it cannot store as a reply:

- `400 ChatSubmitInvalid` for a submit without a valid `message` or `finish_reason`, with both `generation` and
  `message`, with `tool_calls` as the reason and no calls, or with a `native_finish_reason` over 32 characters. The
  client receives the reply as an OpenAI choice, which needs a message and a finish reason, and a `tool_calls` reason
  tells the client to look for calls.
- `400 ChatSubmitInvalid` for `message` on a text job's submit, whose reply is `generation`.
- `400 ChatSubmitInvalid` for a call to a function the request's `tools` do not define, or two calls with the same
  `id`, under either conformance mode. The client dispatches a call by its name and answers it by its id. A request
  with a named `tool_choice` reaches the worker with one tool, but the check uses every tool the client offered.
- `400 ChatModelMismatch` for a `model_id` other than the assigned model. The generation records the model it was
  assigned to, and the client is told which model answered.
- `400` for a token count outside 0 to 10,000,000.

A `strict` request's reply also has to honour the request
([Reply verification](../reference/chat_requests.md#reply-verification)). The horde checks what the reply proves,
whatever the worker declared, so a backend that declares a guarantee it does not keep has its submits refused:

- `400 ChatSubmitNonconformant` for a call under `tool_choice: none` (the worker received no tools then), for no call
  under `tool_choice: "required"`, which is also what a named choice arrives as, or for a call to another function
  than the named one.
- `400 ChatSubmitNonconformant` for content that is not a JSON object under `response_format` `json_object` or a
  `json_schema` without `strict`, or content that does not match a `strict` `json_schema`. A reply that only calls
  tools, with empty or null content, is not checked against the format.
- More than one call under `parallel_tool_calls: false` is not refused: the horde keeps the first and the client
  receives one call. Submit the calls as the backend produced them.

A `best_effort` request's reply is checked only for the function names and ids above. Each refusal is definitive:
fault the job ([step 7](#7-fault-a-job)), do not resubmit the same reply, and withdraw a guarantee the backend
keeps failing.

Verification: the submit returns `{"reward": <kudos>}`, and the requester's `GET
<HORDE_URL>/api/v2/generate/chat/status/<request-id>` shows `done: true` and the submitted `message`,
`finish_reason`, token counts and `charge_basis: "output_estimate"`. Covered by
`test_a_chat_request_completes_with_its_structured_reply_and_signals`,
`test_a_tool_call_reply_is_popped_submitted_shown_and_charged_by_its_arguments`,
`test_a_submit_naming_another_model_than_the_assigned_one_is_refused`,
`test_a_chat_submit_without_a_valid_structured_reply_is_refused`,
`test_a_strict_request_refuses_a_reply_its_tool_choice_or_response_format_rules_out`,
`test_a_best_effort_request_shows_that_reply_as_submitted`,
`test_a_reply_no_client_could_use_is_refused_under_every_conformance_mode` and
`test_a_strict_single_call_request_shows_only_the_first_of_several_calls`.

Reversal: none. An accepted submit is settled; a second submit for the same job returns `400 DuplicateGen`.

## 7. Fault a job

Submit `{"id": "<job-id>", "state": "faulted", "model_id": "<model-name>"}`, with neither `generation` nor `message`,
whenever the worker cannot serve a job it popped: the conversation does not fit, an image cannot be fetched, the
template cannot render it, or the backend fails. `model_id` marks the body as a chat submit, which does not require
`generation`.

A faulted submit is handled as a faulted text submit. The reward is 0 and the generation is not charged. The request
gets the job back for another worker, and its status counts the attempt under `restarted`. The worker's aborted job
count rises, and a worker that aborts more than 20 jobs in an hour is put into maintenance, so a worker that faults
every chat job should withdraw its chat declaration instead.

A job the requester cancels while the worker holds it is settled at the cancellation, as a text job is: the worker is
rewarded and the requester charged for dispatched work
([ADR 0027](../decisions/0027-price-chat-generations-by-the-text-output-estimate.md)). A submit for a cancelled job,
or for one whose TTL ran out, returns `400 AbortedGen`.

Verification: the faulted submit returns `{"reward": 0}`, and the next pop by any eligible worker receives a new job
for the same request. Covered by `test_a_faulted_chat_submit_needs_neither_generation_nor_message`.

## 8. Keep serving text jobs

A worker that declares chat jobs keeps receiving text jobs, under its text limits, with an unchanged response. One
pop loop serves both: branch on `chat`, send chat jobs to the backend's chat endpoint and text jobs to its completion
endpoint, and submit each in the shape of its kind.

Verification: a text request for the worker's model pops with no `chat` key and is submitted with `generation`.
Covered by `test_a_chat_worker_still_pops_text_jobs_with_an_unchanged_response`.

## Worked example: koboldcpp's embedded worker

koboldcpp's embedded Horde worker, `run_horde_worker` in `koboldcpp.py`, implements every step above. It is enabled
by these flags (help text as printed by `koboldcpp --help`):

- `--hordechat`: Lets your AI Horde worker accept chat jobs, which are rendered with the loaded chat template.
- `--hordemodelname [name]`: Sets your AI Horde display model name.
- `--hordegenlen [amount]`: Sets the maximum number of tokens your worker will generate from an AI horde job.
- `--hordemaxctx [amount]`: Sets the maximum context length your worker will accept from an AI Horde job. If 0, matches
  main context limit.
- `--hordekey [apikey]`: Sets your AI Horde API key.
- `--hordeworkername [name]`: Sets your AI Horde worker name.
- `--hordeurl [url]`: Sets the AI Horde cluster your worker pops jobs from and submits them to.

The worker starts only when both `--hordekey` and `--hordeworkername` are set. `--hordeurl` takes the horde root,
such as `https://aihorde.net`, without `/api`.

With `--hordechat`, each pop declares:

- `chat_jobs: true`;
- `chat_features` `seed`, `presence_penalty`, `tools`, `json_object` and `json_schema`, plus `vision` when a
  multimodal projector is loaded. `frequency_penalty` is not declared, since koboldcpp treats it as an alias of the
  presence penalty. `tools_with_response_format` is not declared: koboldcpp's generic tool path replaces the response
  format grammar with its tool grammar, so a request carrying both is served by other workers;
- `image_transports` `url` and `base64` when `vision` is declared, and none otherwise;
- `chat_limits.max_context_length` as the smaller of the loaded context size and `--hordemaxctx`, and
  `chat_limits.max_length` as `--hordegenlen` bounded by the room that context leaves for a reply;
- `model_id` as the model name it registers, which koboldcpp prefixes with `koboldcpp/`.

A chat job is fetched for images, checked for fit by `horde_chat_overflows`, and generated through koboldcpp's local
`/v1/chat/completions`; a text job still goes to `/api/v1/generate`. The reply is submitted with `finish_reason`
`tool_calls` when the backend returned calls, `length` when it reported `length`, and `stop` otherwise, and with each
token count only when the backend reported it. A fetch failure, an overflow, a render failure or a failed generation
is submitted as `faulted`.

### Start koboldcpp as a chat worker

```bash
koboldcpp --model <model.gguf> --contextsize 4096 --jinja \
  --hordechat --hordeurl <HORDE_URL> --hordekey <api-key> \
  --hordeworkername <worker-name> --hordemodelname <model-name> \
  --hordemaxctx 4096 --hordegenlen 512
```

`--jinja` formats the conversation with the model's Jinja chat template, including tool calls. Add `--mmproj
<projector.gguf>` for a vision model.

Verification: the log prints `Embedded Horde Worker '<worker-name>' is started.`, and the chat model list shows the
declaration:

```bash
curl -s "<HORDE_URL>/api/v2/status/models?type=text&chat=true" \
  | jq '.[] | select(.name == "koboldcpp/<model-name>") | .chat'
```

returns `workers` of at least 1, `max_context_length` 4096, `max_length` 512, and `features`
`["json_object", "json_schema", "presence_penalty", "seed", "tools"]`, with `vision` added when a projector is loaded.

The embedded worker does not pop while koboldcpp's local API served a request in the last 20 seconds, so a local
client in use delays Horde jobs.

Reversal: stop koboldcpp, or restart it without `--hordechat` to serve text jobs only. A worker counts as online for
300 seconds after its last pop, so the declaration leaves the model list within that time.

### Check bodies against the server's proof

The Horde's integration tests drive a minimal conforming worker, `tests/integration/_fake_chat_worker.py`, through
`tests/integration/test_chat.py`: the declaration, the popped payload with tools and tool history, the structured
submit with `tool_calls` and a null `content`, the faulted submit and the text-job coexistence are each asserted
there. A worker author can compare a new worker's pop and submit bodies with the ones that worker sends, and run those
tests against a local horde to see the server's refusals (`ChatDeclarationInvalid`, `ChatSubmitInvalid`,
`ChatSubmitNonconformant`, `ChatModelMismatch`) with their exact messages.

## Related

- [Chat requests reference](../reference/chat_requests.md): every pop, payload and submit field, eligibility and
  pricing.
- [Chat requests on the Horde](../explanation/chat_requests.md): why chat is a text request subtype and what the
  design costs.
- [Run the OpenAI-compatible chat front](run_openai_chat_front.md): the process clients reach these workers through.
- [Request and job lifecycle](../haidra-assets/docs/workers.md): the lifecycle chat jobs follow as text jobs.
