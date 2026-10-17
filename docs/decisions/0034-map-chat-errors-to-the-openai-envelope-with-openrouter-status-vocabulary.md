---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Map chat errors to the OpenAI envelope with OpenRouter's status vocabulary

## Context and Problem Statement

The Horde reports errors as a message plus a return code (`README_return_codes.md`). OpenAI clients parse an
envelope with `message`, `type`, `param` and `code`, all four present. OpenRouter adds a `metadata` object and uses a
status vocabulary for routing failures that OpenAI does not have: 402 for exhausted credit, 503 for no provider
meeting the requirements, 502 for a model failure. Which shape and which statuses does the chat front use, before and
after the response headers are sent?

## Decision Drivers

- SDKs branch on status and parse the four envelope fields.
- Queue and capacity failures need statuses OpenAI never emits.
- After headers are sent the status is fixed; the error must travel in the stream.
- Retry guidance distinguishes definitely unsubmitted work from submitted or unknown outcomes.

## Considered Options

- OpenAI envelope plus `metadata`; OpenRouter's status vocabulary; OpenRouter's mid-stream error event
- OpenAI envelope only, with the Horde's statuses mapped to 400 and 500
- OpenRouter's envelope with numeric `code`

## Decision Outcome

Chosen option: "OpenAI envelope plus `metadata`; OpenRouter's status vocabulary; OpenRouter's mid-stream error
event", amended below to keep finish reasons inside the OpenAI success enum.

Before headers: `{"error": {"message", "type", "param", "code"}}` with `code` the Horde return code and a `metadata`
object carrying the request id and details. Statuses: 400 invalid, 401 auth, 402 insufficient kudos, 403 moderation
or forbidden, 404 unknown model, 408 non-streamed timeout, 429 rate limit, 502 worker or generation failure, 503 no
eligible worker; `Retry-After` where a retry can help and `x-should-retry: false` where it would duplicate work.
After headers: one SSE data event with a top-level `error` envelope, then terminate (`[DONE]` when writable).
Do not emit a choice with `finish_reason: error` or fabricate success. Set retry-suppression headers when opening a
submitted stream; their delivery is not guaranteed on disconnect.

### Consequences

- Good: Successful completion chunks stay within the OpenAI finish-reason vocabulary.
- Bad: Post-header failure cannot change HTTP status; test stream exceptions in named SDK/UI versions and count
  delivery failures separately from HTTP 200s. Compatibility with every client is not assumed.

## Pros and Cons of the Options

### OpenAI envelope plus metadata and OpenRouter statuses

- Good: Superset of both conventions.
- Bad: Two vocabularies to document.

### OpenAI envelope only

- Good: One vocabulary.
- Bad: Capacity failures become indistinguishable 500s.

### OpenRouter envelope with numeric code

- Good: Matches one popular client population.
- Bad: The OpenAI SDK types `code` as a string.

## More Information

The front also maps ingress byte-limit refusal to 413. Unsupported service input is 400; temporary absence of workers
satisfying supported constraints is 503, raised by the front after the native route reports `is_possible` false. The
native submit path does not change for this. Operation keys protect internal native retries and do
not protect independent client retries; follow ADR 0026's amended guarantee.

Rulings R8 and R9 of the review.
