---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Classify client retries at the front and recover ambiguous submits by operation key

## Context and Problem Statement

The official OpenAI SDK retries 408, 409, 429, every 5xx, timeouts and connection errors up to twice, and does not
send an idempotency key. A front that answers a server-side timeout with 408, or a lost submit with 502, turns one
client call into up to three charged jobs. The SDK honours an `x-should-retry` response header. How does the front
avoid duplicate work, and what happens when the front does not learn whether a submit reached the Horde?

## Decision Drivers

- Internal recovery of a submission the Horde may have accepted must not create another job.
- A client retry without a stable caller key cannot be shown to be the same logical request.
- A retry that is safe, because the request was not submitted, must stay allowed.
- The design cannot require a client change.
- A header reduces retries only when the client receives it.

## Considered Options

- Retry classification with `x-should-retry: false` plus a front-generated operation key claimed at activation
- Retry classification alone
- Caller-provided idempotency keys only
- Deduplication of client retries by a hash of the request content

## Decision Outcome

Chosen option: "Retry classification with `x-should-retry: false` plus a front-generated operation key claimed at
activation".

The front distinguishes three states: not submitted, submitted with an id, and outcome unknown. Once the Horde holds a
request, or may hold one, every response carries `x-should-retry: false`, including a successful stream, a timeout
after submission and a transport failure after the request was sent. A response sent before submission, such as a
refused connection or the 408 for a slow request body, stays retriable.

The front generates a UUID for each client HTTP request and sends it as `horde.operation_key`. When a submit's answer
is lost, the front sends the same body and key again, up to `submit_recovery_attempts` times (default 2) within the
request's budget.

The native chat route claims the key for the submitting user, or for the owner of the shared key when one is used:

- Before validation and admission, `find_existing_claim` looks the key up. A repeat with the same fingerprint returns
  200 with the first request's `id` and `kudos` and does not queue or charge anything. A repeat with a different
  fingerprint returns 400 `OperationKeyConflict`.
- An unclaimed key is written in `TextWaitingPrompt._activate` (`horde/classes/kobold/waiting_prompt.py`) before the
  first commit, which also carries the activation and the horde tax. The partial unique index on
  `(user_id, operation_key)` lets one of two concurrent submissions commit. The other rolls back, its inactive row and
  upfront kudos hold are deleted, and it returns the winning request or the conflict.
- The claim is held by the request row and lasts as long as the row. The same key from another user is a separate
  claim, and a dry run quotes without claiming.

The fingerprint is a SHA-256 digest of the stored messages, the mapped parameters, the sorted model list, the stored
`chat_extras` and the `horde` options. It leaves out the options that do not change the work
(`OPERATION_FINGERPRINT_EXCLUDED_HORDE_KEYS` in `horde/classes/kobold/chat_request.py`: `availability_mode`,
`dry_run`, `include_reasoning`, `max_kudos` and `operation_key`).

`horde.operation_key` is accepted from any native caller. The front refuses it in a public request body and always
supplies its own. The chat routes do not read an `Idempotency-Key` header.

### Consequences

- Good: Recovering an ambiguous submit does not activate or tax a second request, and clients do not have to change.
- Good: A native caller, such as a proxy or bot, can recover its own ambiguous submits with a stable key.
- Bad: Each client HTTP request gets a new key, so a client retry after a lost response, a front crash or a retry
  routed to another front process can still create charged work. The front does not promise exactly-once delivery.
- Bad: The header reduces retries only for clients that receive and honour it.
- Bad: Two columns, `operation_key` and `operation_fingerprint`, and a unique index on `waiting_prompts`.
- Bad: The claim ends with the request row, so recovery after the row is deleted creates a new request. The front's
  recovery runs within one client call.

## Pros and Cons of the Options

### Classification plus operation key

- Good: Gives header-reading clients useful guidance and makes internal ambiguous submits recoverable.
- Bad: Cannot recognise a fresh client retry without a stable logical-request key.
- Bad: The claim has to be atomic with activation and tax in an activation path that commits in more than one place.

### Classification alone

- Good: Small.
- Bad: Does not help with a lost submit answer or a client that ignores the header.

### Caller keys only

- Good: A standard shape with caller-controlled scope.
- Bad: The official SDK does not send one, so most clients would not be protected.

### Deduplication by request content

- Good: Catches a client retry without a key.
- Bad: Two identical requests from one user are legitimate, for example a regeneration, and would be merged.

## Confirmation

`tests/integration/test_chat.py` pins the operation key: a repeat returns the first request, also while admission is
off and after its model went offline; a changed request is refused; reordered models are the same operation; two users
make two requests; a dry run does not claim; and two concurrent submits with one key make one activation, one tax and
one job. `tests/unit/oai/test_submit_recovery.py` pins the front's recovery, its bounds, a fresh key per client request
and the retry-hint metric. `tests/unit/oai/test_errors.py` and `tests/unit/oai/test_chat_front.py` pin the
`x-should-retry` header. `docs/reference/chat_requests.md` records the operation key and its fingerprint.
