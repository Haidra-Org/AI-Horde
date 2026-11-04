---
title: "Send text generation requests through the native API"
summary: "Submit a prompt to the native text API, choose models and parameters, poll the status, read or cancel the generations and handle errors."
topics: [generation, requests]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Send text generation requests through the native API

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

A native text request is a raw prompt with KoboldAI-style sampling parameters. The client submits it with
`POST /v2/generate/text/async`, polls `GET /v2/generate/text/status/<id>` until `done` is true, and reads the text
from `generations`. A `DELETE` on the same status path cancels the request. To send OpenAI-shaped chat messages, tools
or images, use the chat front described in
[Send chat completions through the OpenAI-compatible front](send_chat_completions.md).

Before starting, have:

- An HTTP client that can send JSON and set request headers.
- A Horde API key, written in the examples as `HORDE_API_KEY`, or the anonymous key `0000000000`.
- The Horde API base URL, written in the examples as `HORDE_URL`. The public service is `https://aihorde.net/api`.

End state: the client submits a prompt, polls its status until `done` is true, reads the generated text, and cancels
the request when it stops waiting.

## Set the headers

Send two headers with every submit:

| Header | Value |
| --- | --- |
| `apikey` | Your API key, or `0000000000` for anonymous use. The header is required, so an anonymous client sends the anonymous key explicitly. |
| `Client-Agent` | `<client name>:<version>:<contact>`, such as `my-app:1.2.0:dev@example.com`. Without it the request is recorded as `unknown:0:unknown`. |

The queue is ordered by the requester's kudos, so requests from the anonymous key are served after requests from
users with kudos. Every anonymous client uses the same account, so they share one limit on concurrent requests
and can get 429 `TooManyPrompts` when other anonymous requests fill it.

Verification: `GET /v2/find_user` with the key returns 200 and your username:

```bash
curl -s -H "apikey: $HORDE_API_KEY" -H "Client-Agent: my-app:1.2.0:dev@example.com" "$HORDE_URL/v2/find_user" | jq .username
```

With an unknown key, the server returns 401 `InvalidAPIKey`.

## Choose models

List the text models served by at least one online worker:

```bash
curl -s "$HORDE_URL/v2/status/models?type=text" | jq '.[] | {name, count, queued, eta}'
```

| Field | Meaning |
| --- | --- |
| `name` | The model name to send in `models`. Names often start with a backend prefix such as `koboldcpp/`. |
| `count` | Online workers serving the model. |
| `queued` | Tokens waiting to be generated for the model. |
| `jobs` | Jobs waiting for the model. |
| `eta` | Estimated seconds to clear the model's queue. |
| `performance` | The average generation speed for the model. |

Without `type=text` the endpoint lists image models.
The list is cached for 2 seconds.

Send `models` as a list of acceptable model names. A worker serving any of them can pick up the request. With
`models` left out or empty, any text worker can pick it up, whatever model it runs.

Verification: each name in your `models` appears in `.[].name` with `count` at least 1.

## Set the generation parameters

The `params` object holds the generation settings. Every key is optional.

| Key | Default | Range | Effect |
| --- | --- | --- | --- |
| `max_length` | 80 | 16 to 16384 | Tokens to generate. |
| `max_context_length` | 2048 | 80 to 1048576 | Tokens of context the model is given, prompt and output together. |
| `n` | 1 | 1 to 20 | Generations to produce from the prompt. |
| `temperature` | worker default | 0 to 5 | Sampling temperature. |
| `top_p` | worker default | 0.001 to 1 | Nucleus sampling. |
| `top_k` | worker default | 0 to 200 | Top-k sampling. 0 disables it. |
| `top_a`, `tfs`, `typical` | worker default | 0 to 1 | Top-a, tail free and typical sampling. |
| `min_p` | worker default | 0 to 1 | Min-p sampling. |
| `rep_pen` | worker default | 1 to 3 | Repetition penalty. |
| `rep_pen_range` | worker default | 0 to 4096 | How many recent tokens the repetition penalty covers. |
| `rep_pen_slope` | worker default | 0 to 10 | Repetition penalty slope. |
| `smoothing_factor` | worker default | 0 to 10 | Quadratic sampling. |
| `dynatemp_range` | worker default | 0 to 5 | Dynamic temperature range. |
| `dynatemp_exponent` | worker default | 0 to 5 | Dynamic temperature exponent. |
| `sampler_order` | worker default | | A list of sampler ids. When sent, it has to list at least 7 distinct samplers. |
| `stop_sequence` | none | | Strings that end generation. At most 128 distinct strings, 2000 characters in all. The returned text includes the stop sequence. |
| `use_default_badwordsids` | worker default | | `true` applies the default KoboldAI bad word ids. |
| `singleline`, `frmtadsnsp`, `frmtrmblln`, `frmtrmspch`, `frmttriminc` | worker default | | KoboldAI input and output formatting options. |

For a value outside its range, the server returns 400 with an `errors` object keyed by the field. For `max_length`
above `max_context_length`, it returns 400 `TokenOverflow`.

A worker picks up the request only when its declared `max_length` and `max_context_length` are at least the
request's values. A request above every online worker's limits is still queued. The 202 response then includes a
`message` warning that no available worker can fulfill it, and it expires unless a larger worker comes online.

Verification: after the submit, `GET /v2/generate/text/request/<id>` with the same `apikey` returns the values sent
in `params`.

### Fit the prompt to the context

The top-level `context_fit` key sets what happens when the prompt and `max_length` do not fit `max_context_length`.
The prompt length is estimated at 3 characters per token.

| Value | Effect |
| --- | --- |
| `ignore` (default) | The request is sent as it is, and the worker cuts the start of the prompt away. |
| `reject` | The server refuses the request with 400 `PromptExceedsContext`. |
| `grow` | `max_context_length` is raised to the next power of two that fits, up to the largest context declared by an online worker for the requested models. The larger context is charged. A prompt that does not fit at that bound is refused with 400 `PromptExceedsContext`. |

Verification: a [dry run](#check-the-cost) returns the prompt estimate in `resolved.estimated_prompt_tokens`, and
under `grow` it returns the raised context in `resolved.params.max_context_length`.

## Restrict the workers

These top-level keys narrow which workers can pick up the request:

| Key | Default | Effect |
| --- | --- | --- |
| `trusted_workers` | `false` | `true` allows only trusted workers. |
| `validated_backends` | `false` | `true` allows only workers on backends validated by the Horde developers. |
| `slow_workers` | `true` | `false` excludes workers below the minimum speed for the model. The request then always needs the kudos for it upfront (see [Check the cost](#check-the-cost)). |
| `workers` | none | A list of worker ids. Only these workers serve the request. Each id has to exist, or the server returns 404 `WorkerNotFound`. |
| `worker_blacklist` | `false` | `true` turns `workers` into a list of workers that do not serve the request. |
| `softprompt` | none | Only workers that can load this soft prompt serve the request. |

Each restriction shrinks the pool of eligible workers, which can leave a request waiting until it expires.

Verification: after the submit, the [status](#poll-the-status) shows `is_possible` as `true` and `eligible_workers`
at 1 or more.

## Check the cost

Send the request with `"dry_run": true` to get its kudos cost without queueing it:

```bash
curl -s "$HORDE_URL/v2/generate/text/async" -H "apikey: $HORDE_API_KEY" -H "Client-Agent: my-app:1.2.0:dev@example.com" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Once upon a time", "models": ["<model>"], "params": {"max_length": 200}, "dry_run": true}' | jq
```

The response is 200 with `kudos` and `resolved`. `resolved` holds the prompt after any style template, the
`params` and `models` that the request would use, the applied `style`, `estimated_prompt_tokens` and the applied
`context_fit`. A dry run does not queue a request and is not charged.

A request may use a free output allowance without holding kudos. The allowance is 512 to 4096 tokens by default.
It is larger when the eligible workers have a short wait and smaller when they have a long one. A request with
`max_length` above the allowance, or with `slow_workers` set to `false`, needs the requester to have the kudos
for it, which are held when it is queued. Without them, the server refuses the request with 403 `KudosUpfront`,
and the message includes the allowance and the cost. Send `"allow_downgrade": true` to have such a request lowered to the allowance,
with slow workers allowed, instead of refused.

Verification: the dry run returns 200 and a `kudos` value.

## Submit the request

```bash
curl -s "$HORDE_URL/v2/generate/text/async" -H "apikey: $HORDE_API_KEY" -H "Client-Agent: my-app:1.2.0:dev@example.com" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Once upon a time", "models": ["<model>"], "params": {"max_length": 200, "temperature": 0.7}}'
```

The response is 202:

```json
{"id": "<request-id>", "kudos": 12}
```

`kudos` is the expected cost. A `message` is added when no online worker can serve the request as sent.

Verification: the response is 202 with an `id`. `GET /v2/generate/text/request/<id>` with the same `apikey`
returns the submitted parameters with the defaults filled in. Another user's key gets 403, and so does the
anonymous key.

A queued request is stopped only by [cancelling it](#cancel-the-request).

## Poll the status

```bash
curl -s -H "Client-Agent: my-app:1.2.0:dev@example.com" "$HORDE_URL/v2/generate/text/status/<request-id>" | jq
```

Reading the status does not require an API key, only the request id. The status fields:

| Field | Meaning |
| --- | --- |
| `done` | `true` when every generation is finished. |
| `faulted` | `true` when the request failed and will not be completed. |
| `waiting`, `processing`, `finished` | Generations in each state. |
| `restarted` | Generations that timed out or were reported failed by a worker and were queued again. |
| `queue_position`, `wait_time` | Position in the Horde-wide queue and the estimated seconds to finish. |
| `is_possible` | `false` when no online worker can serve the request. |
| `eligible_workers`, `eligible_worker_threads` | Online workers and threads that pass the request's restrictions. They may be busy. |
| `might_stall` | `true` when incoming work keeps taking the eligible workers ahead of this request. |
| `kudos` | Kudos consumed so far. |
| `generations` | The finished generations. |

The status route allows 60 requests a minute for each request id from one address, so poll at most once a second.
A slower interval, such as every 2 to 5 seconds, is enough for most requests.

A request expires 20 minutes after it was created or after a worker last picked up or finished one of its
generations. For an expired request or an unknown id, the status route returns 404 `RequestNotFound`.

Verification: `done` becomes `true` and `generations` holds `n` entries.

## Read the generations

Each entry in `generations` has:

| Field | Meaning |
| --- | --- |
| `text` | The generated text. It does not include the prompt. |
| `seed` | The seed used by the worker. |
| `model` | The model that generated it. |
| `worker_id`, `worker_name` | The worker that generated it. |
| `gen_metadata` | A list of `{"type", "value", "ref"}` entries reported by the worker about the generation, such as censorship. |
| `state` | `ok`, `censored` or `csam`. Prefer `gen_metadata`, which has more detail. |

```bash
curl -s "$HORDE_URL/v2/generate/text/status/<request-id>" | jq -r '.generations[].text'
```

## Receive generations by webhook

Send `"webhook": "https://<your endpoint>"` with the request to receive a POST for each generation as it is
delivered. The URL has to use `https`, or the server returns 400. The body has the fields listed in
[Read the generations](#read-the-generations) (`text`, `seed`, `worker_id`, `worker_name`, `model`, `state`, `gen_metadata`), plus `id` (the generation id),
`request` (the request id) and `kudos` (the generation's cost).

Delivery is best effort. Each delivery is tried up to 3 times with a 6-second limit per try, and a failed delivery
is not reported to the client. Poll the status as well when every generation has to arrive.

Verification: your endpoint receives one POST per generation with `request` equal to the request id.

## Cancel the request

```bash
curl -s -X DELETE -H "Client-Agent: my-app:1.2.0:dev@example.com" "$HORDE_URL/v2/generate/text/status/<request-id>" | jq
```

The response is 200 with the status as it was before the cancel, including the generations already finished.
Generations that are waiting are not handed to workers after the cancel. A cancel cannot be undone. Submit the
request again to restart it.

Verification: a later status read shows `waiting` at 0, or the server returns 404 once the request is removed.

## Use a style

Send `"style": "<style id or name>"` to apply a text style, and `template_fields` for the placeholders declared by
the style. The style can replace the prompt template and parameters. See
[Use the style endpoints](use_style_endpoints.md) for creating styles and the rules that apply to styled requests.

Verification: a dry run with the style returns the style's id and name in `resolved.style` and the prompt after the
template in `resolved.prompt`.

## Errors and retries

Every error is `{"message", "rc"}`, with `rc` one of the
[return codes](https://github.com/Haidra-Org/AI-Horde/blob/main/README_return_codes.md), each listed with its cause.
A payload validation failure is 400 with `message` and an `errors` object keyed by the failing field.

| Status | `rc` | Cause | Retry |
| --- | --- | --- | --- |
| 400 | `BadRequest` and validation errors | A value is out of range or has the wrong type, `context_fit` is unknown, or the webhook is not `https`. | Correct the request. |
| 400 | `MissingPrompt` | `prompt` is empty. | Correct the request. |
| 400 | `TokenOverflow` | `max_length` is above `max_context_length`. | Correct the request. |
| 400 | `PromptExceedsContext` | Under `context_fit` `reject` or `grow`, the prompt does not fit. | Shorten the prompt or change the context. |
| 400 | `MissingFullSamplerOrder`, `TooManyStopSequences`, `ExcessiveStopSequence` | A `sampler_order` or `stop_sequence` breaks its rule. | Correct the request. |
| 400 | `MaintenanceMode` | The Horde is in maintenance mode. | Retry later. |
| 401 | `InvalidAPIKey` | The key is unknown. | Fix the key. |
| 403 | `KudosUpfront` | The request is above the free allowance and the requester lacks the kudos. | Lower `max_length`, send `allow_downgrade`, or earn kudos. |
| 403 | `NoEligibleWorker` | The request would be refused for kudos and no online worker can serve it. The body includes `impossible_reason` when the cause is known. | Change the request or wait for workers. |
| 403 | `TimeoutIP` | Your address is in a timeout. | Wait for the timeout to end. |
| 404 | `WorkerNotFound` | A worker id in `workers` does not exist. | Correct the list. |
| 404 | `RequestNotFound` | The request id does not exist or the request expired. | Do not retry. Submit again if the result is still needed. |
| 429 | `TooManyPrompts` | You hold the maximum number of concurrent requests. | Retry after one of your requests finishes. |
| 429 | | A rate limit was exceeded: 2 submits a second and 90 a minute from one address, 2 submits a second for one personal key, and 60 status reads a minute for one request from one address. | Retry after the `Retry-After` header. |

A refused submit was not queued or charged, so it is safe to correct and send again. A submit that times out on
the client side may still have been queued. Each submit is a separate charged request, so resending after a timeout
can create a duplicate.

## Use the chat front instead

The [OpenAI-compatible chat front](send_chat_completions.md) is the better fit when the client already uses the
OpenAI chat completions API, or when it needs message roles, tools, structured output, images or streamed replies.
It is served only by workers that accept chat jobs. The native text API is served by text workers that accept
prompt jobs, and it gives direct control over the prompt and the KoboldAI sampling parameters.

## Related

- [Use the style endpoints](use_style_endpoints.md): text and image styles and requests sent under them.
- [Request feasibility and queue pressure](../explanation/request_feasibility_and_queue_pressure.md): what
  `is_possible`, `eligible_workers` and `might_stall` are computed from.
- [Send chat completions through the OpenAI-compatible front](send_chat_completions.md): the chat-shaped
  alternative to this API.
