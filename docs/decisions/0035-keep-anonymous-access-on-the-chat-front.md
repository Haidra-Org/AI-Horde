---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Keep anonymous access on the chat front

## Context and Problem Statement

The Horde serves requests made with its anonymous key. An OpenAI client that does not send an `Authorization` header
could be served as anonymous or refused. Anonymous access lets any client use the front without registering,
including automated agent loops and scrapers. Does the chat front require a registered key?

## Decision Drivers

- Anonymous use is the service's core proposition for the average user.
- Abuse must be bounded without removing the feature.
- Every request reaches the Horde from the front's address, so a per-address bound at the Horde must apply to the end
  user.

## Considered Options

- Keep anonymous access, bounded by the native limits and per-source caps on the front
- Require a registered key on the front
- Anonymous access for non-streamed requests only

## Decision Outcome

Chosen option: "Keep anonymous access, bounded by the native limits and per-source caps on the front".

A missing `Authorization` header is the anonymous key. A header that is present but is not `Bearer <key>` is refused
with 401 `InvalidAuthorizationHeader`.

The anonymous account does not have kudos to hold, so an anonymous chat request is served free up to the free output
allowance. The allowance is sized by the request's estimated wait, from 4096 tokens when the eligible workers are
idle down to 512, as
[ADR 0037](0037-size-the-free-output-allowance-by-the-estimated-wait.md) (Size the free output allowance by the
request's estimated wait) records. A request above the allowance is refused with 403 `KudosUpfront`, and the message
states the allowance. Chat requests are submitted with `allow_downgrade` false, so a request is refused and is not
lowered to the allowance. A requester with kudos to hold may ask for up to 16384 tokens (`MAX_LENGTH_LIMIT` in
`horde/classes/kobold/request_fit.py`, and the front's token limit range).

The front bounds what one process and one source can hold open (`horde/oai/settings.py`):

| Bound                                                  | Default    | Answer                    |
| ------------------------------------------------------ | ---------- | ------------------------- |
| Open requests per process (`max_inflight`)             | 1000       | 503 `FrontAtCapacity`     |
| Open requests per end-user address per process (`max_inflight_per_source`) | 5 | 503 `FrontAtCapacity` |
| Request body size, counted before parsing (`max_body_bytes`) | 36 MiB | 413 `RequestTooLarge`  |
| Request body read time (`body_read_timeout_seconds`)   | 30 seconds | 408 `RequestBodyTimeout`  |

With a proxy passkey configured, each Horde call carries the end-user address as `Proxied-For`, so the Horde's
per-address rate limits on the chat route (`HORDE_CHAT_REQUEST_2SEC_LIMIT_PER_IP`,
`HORDE_CHAT_REQUEST_90MIN_LIMIT_PER_IP`) and its suspicion apply to the user and not to the front. The front does not
otherwise distinguish anonymous requests.

### Consequences

- Good: A user can try the service from any OpenAI client without registering.
- Good: An anonymous request costs at most the free output allowance, which shrinks as the queue grows.
- Bad: Anonymous agent loops consume worker capacity up to the allowance and the caps.
- Bad: The per-source cap is per process, so it multiplies with the number of front processes, and users behind one
  egress address share it.

## Pros and Cons of the Options

### Keep anonymous access

- Good: Keeps the service usable without registration.
- Bad: Anyone can send requests, so the bounds carry the abuse control.

### Require a key

- Good: Every request is attributed to an account.
- Bad: Changes what the service is for its casual users.

### Anonymous for non-streamed requests only

- Good: Streams, the long-held connections, would need a key.
- Bad: The line is arbitrary, and it pushes anonymous users to the slower experience.

## Confirmation

`tests/unit/oai/test_chat_front.py` fixes the anonymous key for a missing header.
`tests/unit/oai/test_lifecycle.py` and `tests/unit/oai/test_admission_and_table.py` fix the per-source and global
caps. `tests/integration/test_chat.py` fixes a free anonymous chat request within the allowance and the
`KudosUpfront` refusal above it, and `tests/unit/test_kobold_text_wp.py` fixes the allowance. `tests/unit/oai/test_errors.py`
fixes the 401, 408 and 413 answers. `docs/reference/chat_requests.md` (Rate limits, Free output allowance) and
`docs/how-to/run_openai_chat_front.md` record the public behaviour.
