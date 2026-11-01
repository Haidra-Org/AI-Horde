---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Map chat errors to the OpenAI envelope with OpenRouter's status vocabulary

## Context and Problem Statement

The Horde reports errors as a message plus a return code (`README_return_codes.md`). OpenAI clients parse an
envelope with `message`, `type`, `param` and `code`, all four present. OpenRouter adds a `metadata` object and uses
statuses for routing failures that OpenAI does not: 404 when no provider supports the requested features, 503 when
no provider is available, 502 for a model failure. Which shape and which statuses does the chat front use, before and
after the response headers are sent?

## Decision Drivers

- SDKs branch on status and parse the four envelope fields.
- Queue and capacity failures need statuses that OpenAI does not define for them.
- After headers are sent the status is fixed, so the error has to travel in the stream.
- Retry guidance has to separate work that was definitely not submitted from work that was submitted or whose outcome
  is unknown.

## Considered Options

- OpenAI envelope plus `metadata`, OpenRouter's statuses for routing failures, and OpenRouter's mid-stream error event
- OpenAI envelope only, with the Horde's statuses mapped to 400 and 500
- OpenRouter's envelope with a numeric `code`

## Decision Outcome

Chosen option: "OpenAI envelope plus `metadata`, OpenRouter's statuses for routing failures, and OpenRouter's
mid-stream error event", with finish reasons kept inside the OpenAI enum.

Before headers the body is `{"error": {"message", "type", "param", "code", "metadata"}}`
(`OpenAIError` in `horde/oai/schema.py`). The four OpenAI fields are always written, null included. `code` is the
Horde return code when the Horde produced the error, and the front's own code otherwise. `metadata` holds only the
details that are known: `request_id`, `rc` (the Horde return code), `retry_after`, `submit_outcome`,
`operation_key`, `kudos_estimate` and `max_kudos`.

A refusal from the native route keeps its status and return code. The statuses are:

| Status | Cases and `code`                                                                                                   |
| ------ | ------------------------------------------------------------------------------------------------------------------ |
| 400    | Invalid or unsupported input (`BadRequest`, `UnsupportedChatParameter`, `ChatRequestConflict`); a request over every online worker's context limit, or over every completion length declared by the workers online after submission (`no_worker_supports_limit`); a kudos estimate over `horde.max_kudos` (`max_kudos_exceeded`) |
| 401    | An `Authorization` header that is not `Bearer <key>` (`InvalidAuthorizationHeader`); a native authentication refusal |
| 403    | Moderation and other native refusals; a requester who cannot cover the kudos hold (`KudosUpfront`)                |
| 404    | An unknown model (`model_not_found`); a declaration gap among the online workers (`no_worker_supports_features`)   |
| 408    | A non-streamed request still waiting at its deadline (`ChatRequestTimeout`); a body not received within the body-read timeout (`RequestBodyTimeout`) |
| 413    | A body over the byte ceiling (`RequestTooLarge`)                                                                   |
| 429    | A rate limit; the native return code, or `rate_limit_exceeded` when the native answer does not carry one           |
| 502    | A faulted request (`ChatRequestFaulted`), a request done without a generation (`ChatRequestNoGeneration`), a relayed stream that cannot finish consistently (`ChatStreamDiverged`), a Horde that did not answer (`HordeUnavailable`) |
| 503    | A model without an online chat worker at submission, or a request without an eligible worker after the fail-fast grace (`no_eligible_worker`); a `fail_fast` request refused at its first status read (`queue_unavailable`); a full front process or source (`FrontAtCapacity`); native `ChatAdmissionDisabled` and `ChatImageStorageUnavailable` |

Insufficient kudos is the native 403 `KudosUpfront`, passed through with its status like every native refusal. Its
message states the free output allowance the request exceeded.

`Retry-After` is sent where waiting can help: a 503, the 408 for a request that timed out waiting, and a 429 whose
native answer carries one. When a retry
could create a second charged job, the response carries `x-should-retry: false`, which OpenAI SDKs honour by skipping
their automatic retry. Every streamed response carries it, since a job exists once the stream opens. The front
retries its own submit under an operation key, as
[ADR 0026](0026-classify-client-retries-and-recover-submits-by-operation-key.md) describes; a client's retry is a new
request.

After headers, an error is one SSE data event holding the same envelope at the top level, followed by `[DONE]`
(`build_error_events` in `horde/oai/sse.py`). A completion chunk's `finish_reason` is one of `stop`, `length`,
`tool_calls` and `content_filter`; a failure is reported only by the error event.

### Consequences

- Good: An OpenAI SDK parses every error, and a Horde-aware client reads the return code and request id.
- Good: Successful completion chunks stay within the OpenAI finish reason vocabulary.
- Good: Routing failures use statuses that clients written for OpenRouter handle.
- Bad: A failure after headers cannot change the HTTP status, so a client that checks only the status sees 200.
- Bad: `code` holds Horde return codes and the front's codes in two naming styles.

## Pros and Cons of the Options

### OpenAI envelope plus metadata and OpenRouter statuses

- Good: Compatible with both conventions.
- Bad: Two vocabularies to document.

### OpenAI envelope only

- Good: One vocabulary.
- Bad: Capacity failures become 500s that a client cannot tell apart.

### OpenRouter envelope with numeric code

- Good: Matches one population of clients.
- Bad: The OpenAI SDK types `code` as a string.

## Confirmation

`tests/unit/oai/test_errors.py` fixes the envelope, the statuses and codes, `Retry-After` and `x-should-retry`.
`tests/unit/oai/test_lifecycle.py` fixes the capacity, timeout and no-eligible-worker answers, and
`tests/unit/oai/test_chat_front.py` and `tests/unit/oai/test_done_without_generation.py` fix the mid-stream error
event and `[DONE]`. `tests/integration/test_chat_front_sdk.py` checks the shapes against the OpenAI SDK.
`docs/reference/chat_front.md` (Response shape, No eligible worker, Kudos cap) and
`docs/how-to/run_openai_chat_front.md` (Errors the front returns) record the public behaviour.
