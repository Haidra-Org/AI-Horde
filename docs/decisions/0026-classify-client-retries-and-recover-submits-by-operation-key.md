---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Classify client retries at the front and recover ambiguous submits by operation key

## Context and Problem Statement

The official OpenAI SDK retries 408, 409, 429, every 5xx, timeouts and connection errors up to twice, and does not send an
idempotency key. A front that answers a server-side timeout with 408, or a lost submit with 502, turns one client
call into up to three charged jobs. The SDK honours an `x-should-retry` response header. How does the front avoid
duplicate work, and what happens when the front never learns whether a submit reached the Horde?

## Decision Drivers

- Internal recovery of a possibly accepted native submission must not create another job.
- Client retries without stable caller identity cannot be guaranteed to represent the same logical request.
- A retry that is safe (the request was not submitted) must stay allowed.
- The design cannot require a client change.
- A header is mitigation; it cannot help when the client never receives the response.

## Considered Options

- Retry classification with `x-should-retry: false` plus a front-generated operation key claimed at activation
- Retry classification alone
- Caller-provided idempotency keys only

## Decision Outcome

Chosen option: "Retry classification with `x-should-retry: false` plus a front-generated operation key claimed at
activation".

The front distinguishes three states: not submitted, submitted with an id, and outcome unknown. It sends
`x-should-retry: false` on every response after a job exists or may exist (the non-streamed timeout, a transport
failure after the request was sent), and leaves refused connections retriable. The front generates a UUID per client
request and sends it as the operation key. The native chat route keeps a unique index on user id and key, claimed in
the same transaction as activation in `horde/classes/base/waiting_prompt.py`, `WaitingPrompt.activate`; a collision
returns the existing request id and creation metadata after verifying the same normalized effective request.
Keep a fingerprint and reject reuse with changed content/options. Lookup existing claims before repeating availability
checks or side effects. Retention covers the request lifetime and the maximum internal recovery horizon; preserve a
minimal claim tombstone if ordinary cleanup would remove it too early.

Each incoming client HTTP request gets a separate operation key. This cannot deduplicate a client retry after a lost
response, a front crash, or a retry routed to another process. The header reduces retries only for clients that
receive and honor it. Stable caller idempotency can be added separately with explicit scope, fingerprint, retention
and replay semantics. Never use prompt contents or a retry-count header as identity.

### Consequences

- Good: Native submission recovery avoids duplicate activation/tax without requiring client changes.
- Good: The front can recover an ambiguous submit, as can the user.
- Bad: Independent client retries can still create charged work; the front does not promise exactly-once client delivery.
- Bad: After the retained claim expires the guarantee ends; internal recovery must not outlive that window.
- Bad: An added column and unique index on `waiting_prompts`.

## Pros and Cons of the Options

### Classification plus operation key

- Good: Gives header-reading clients useful guidance and makes internal ambiguous submits recoverable.
- Bad: Cannot recognize a fresh client retry without a stable logical-request key.
- Bad: Transactional work at activation, which commits in more than one place.

### Classification alone

- Good: Small.
- Bad: Does not help with a lost response or a client that ignores the header.

### Caller keys only

- Good: Standard shape.
- Bad: The official SDK does not send one.

## More Information

Rulings R8 and R22 of the review. The design's D8 asked for the operation key; the review added the classification.

The claim, reservation, activation and tax must be atomic despite the commits in the activation path. Test concurrent
duplicates and a lost acknowledgment. Metrics distinguish internal recovery matches from client retry hints; their
intersection is not a measurement of prevented client duplicates. Post-submission SSE headers also suppress suggested
retries without claiming that a disconnected client receives the hint.
