---
status: rejected
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Prefer the worker that last served a conversation's prefix

## Context and Problem Statement

Each turn of a chat resends the whole conversation. Each turn is assigned to whichever eligible worker pops it, so a
worker's prompt cache helps a later turn only when that turn happens to reach the same worker. For long
conversations prompt processing dominates latency and worker time. Should the scheduler prefer the worker that
served the previous turn?

## Considered Options

- A soft preference for the previous worker, keyed on a hash of the opening system or developer message and the
  first user message, scoped by user and model, and expiring after ten idle minutes
- No affinity
- A hard pin to the previous worker while it is online

## Decision Outcome

Chosen option: "No affinity". Chat turns are assigned like any other request, and the scheduler does not prefer a previous worker. OpenAI clients may send
`prompt_cache_key` for the same purpose; it is not a field of the request model, so the front refuses it with 400
`UnsupportedChatParameter` (`tests/unit/oai/test_request_screening.py`).

The soft preference is not built because its cost is certain and its benefit is unmeasured. It needs a request
column for the key, scheduling state recording which worker served each key, and a change to pop ordering. The gain
depends on the same prefix returning to the same worker within that worker's cache lifetime, which the Horde does not
observe.

The soft preference would be reconsidered given:

- a measurement of the prompt processing time a repeated prefix saves on a chat worker, large enough to justify the
  scheduling state;
- a bound on how long a request may wait for its preferred worker before it goes to the next eligible one;
- a scope that separates requesters sharing the anonymous account, since scoping by user alone lets one anonymous
  requester's conversation steer another's.

### Consequences

- Good: Pop ordering and request state stay as they are for chat requests.
- Bad: Long conversations pay full prompt processing on every turn.

## Pros and Cons of the Options

### Soft preference

- Good: Prompt cache reuse on the common multi-turn case, at a bounded latency cost.
- Bad: Adds a scheduling input and state that must be measured to be justified.

### No affinity

- Good: Pop ordering has one rule for every text request.
- Bad: Long conversations pay full prompt processing every turn.

### Hard pin

- Good: The most cache reuse.
- Bad: A single worker becomes a queue for one user, which breaks fair assignment.
