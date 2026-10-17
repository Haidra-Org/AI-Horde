---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Prefer the worker that last served a conversation's prefix

## Context and Problem Statement

Each turn of a chat resends the whole conversation. On the Horde each turn lands on whichever worker pops it, so a
worker's prompt cache and context shift never apply across turns, and prompt processing dominates latency and worker
time for long conversations. Should the scheduler prefer the worker that served the previous turn?

## Decision Drivers

- The benefit is real only when the same prefix returns to the same worker within its cache lifetime.
- Affinity must not relax a hard constraint; it is a preference among eligible workers.
- The key must be scoped so one user's conversation never steers another user's request.
- OpenRouter's sticky routing already defines a key shape clients send: `session_id`, `x-session-id`,
  `prompt_cache_key`.

## Considered Options

- A soft preference keyed on a hash of the opening system or developer message and the first user message, scoped by
  user and model, overridable by `session_id` or `prompt_cache_key`, expiring after ten idle minutes
- No affinity
- A hard pin to the previous worker while it is online

## Decision Outcome

Chosen option: "A soft preference keyed on a hash of the opening system or developer message and the first user
message, scoped by user and model, overridable by `session_id` or `prompt_cache_key`, expiring after ten idle
minutes".

The request stores the key. Assignment records the worker that served it. The pop ordering prefers that worker when
it is eligible and the latency penalty of waiting for it is within a bound; otherwise the request goes to the next
eligible worker. The gain is measured before the preference is widened.

### Consequences

- Good: Prompt-cache reuse on koboldcpp for the common multi-turn case; less worker time per turn.
- Bad: A column, a Redis key and a pop ordering change; a measurement obligation before relying on it.

## Pros and Cons of the Options

### Soft preference

- Good: Bounded cost, measurable benefit.
- Bad: Adds a scheduling input.

### No affinity

- Good: Does not need any implementation.
- Bad: Long conversations pay full prompt processing every turn.

### Hard pin

- Good: Maximum cache reuse.
- Bad: A single worker becomes a queue for one user; violates fair assignment.

## More Information

Disposition: deferred until after the first text round trip, including the request column, public options and Redis keys. Measure benefit before adding
scheduling state. Anonymous callers sharing the native anonymous user need an explicit affinity-isolation policy;
an affinity design must also have a bounded latency penalty.

Ruling R12 of the review. The design author's reply asked for the scoping and the bounded penalty recorded here.
