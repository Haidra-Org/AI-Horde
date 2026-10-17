---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Declare chat capabilities by pop fields alone

## Context and Problem Statement

A chat job needs facts about the worker that a text job does not: whether it accepts chat jobs, whether the loaded
model has a projector, whether its template supports tools, its real context and output limits, and how it accepts
images. The Horde gates image features by parsing the bridge agent string against a version table in
`horde/bridge_reference.py`, `BRIDGE_CAPABILITIES`. How should chat capability reach the scheduler?

## Decision Drivers

- A capability is a property of the running model and backend; a release version does not determine it.
- Workers should opt in explicitly and be able to change what they declare without a release.
- Any scribe, not only koboldcpp, must be able to participate.
- The scheduler must never send a job a worker cannot run.

## Considered Options

- Pop fields: `chat_jobs`, `chat_features`, `chat_limits`, `image_transports`
- Bridge agent version parsing, as for image capabilities
- Server-side probes that verify a worker before it is eligible

## Decision Outcome

Chosen option: "Pop fields: `chat_jobs`, `chat_features`, `chat_limits`, `image_transports`".

Every chat capability is declared on each pop and stored on the worker row. `chat_features` is a list of strings
from a vocabulary the Horde publishes; an absent feature means unsupported and an unknown string is ignored. Changed
semantics get a new feature name. The bridge agent string is never parsed for chat. Eligibility in the pop query and
in `horde/classes/kobold/worker.py`, `TextWorker.can_generate`, reads the declared features against the features a
request needs.

### Consequences

- Good: A worker operator controls eligibility from configuration; a new backend joins by declaring.
- Good: The scheduler reasons about facts the worker asserted, without a table to maintain per release.
- Bad: Declarations are claims; a misconfigured worker can declare what it cannot do. Probes remain a later option.
- Bad: The draft's bridge gating for chat images is removed and replaced.

## Pros and Cons of the Options

### Pop fields

- Good: Explicit, per worker, per pop.
- Bad: Trusts the worker.

### Bridge agent version parsing

- Good: Already exists for images.
- Bad: Cannot express whether a projector is loaded or a template supports tools; couples eligibility to releases.

### Server-side probes

- Good: Produces evidence of a capability.
- Bad: Costs worker time before any job; unbuilt.

## More Information

Ruling R21 of the review. The worker contract that carries these fields is sketched in the review document under
"Worker contract v2".

First-release scope: explicit chat opt-in, the current served model, actual chat limits and implemented optional sampling
features. Opt-in includes a tested baseline text/history/defaults contract. Missing declarations disable support;
model changes update the declaration, while an in-flight assignment retains its model/limit snapshot. Add vision,
tools, logprobs or streaming declarations only alongside working payload, submit and response contracts. An online
capability union is discovery metadata; it does not prove that one worker supports every advertised combination.
