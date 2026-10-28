---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Declare chat capabilities by pop fields alone

## Context and Problem Statement

A chat job needs facts about the worker that a text job does not: whether it accepts chat jobs, which model it serves
them with, whether the loaded model has a projector, whether its template supports tools, its real context and output
limits, and how it accepts images. The Horde gates image features by parsing the bridge agent string against a version
table in `horde/bridge_reference.py`, `BRIDGE_CAPABILITIES`. How should chat capability reach the scheduler?

## Decision Drivers

- A capability is a property of the running model and backend; a release version does not determine it.
- Workers should opt in explicitly and be able to change what they declare without a release.
- Any worker implementation, not only koboldcpp, must be able to participate.
- The scheduler must not send a job a worker cannot run.

## Considered Options

- Pop fields: `chat_jobs`, `prompt_jobs`, `model_id`, `chat_features`, `chat_limits`, `image_transports`
- Bridge agent version parsing, as for image capabilities
- Server-side probes that verify a worker before it is eligible

## Decision Outcome

Chosen option: "Pop fields: `chat_jobs`, `prompt_jobs`, `model_id`, `chat_features`, `chat_limits`,
`image_transports`".

A worker declares its chat capability on every pop, and `TextWorker.check_in` stores the declaration on the worker row.
The declaration is replaced at each pop, so a worker that stops declaring chat stops receiving chat jobs at its next
pop.

- `chat_jobs` opts in to chat jobs. It requires `model_id`, the model the worker serves chat jobs with, which must be
  one of the worker's `models`, and `chat_limits` with `max_context_length` and `max_length`. `chat_limits` may add
  `tools_max_temperature`, the highest temperature the backend keeps for a request with tools.
- `prompt_jobs` defaults to true. A worker that sets it to false with `chat_jobs` is chat-only: it receives chat jobs
  and does not receive text jobs, and its text `max_length` and `max_context_length` do not bound anything. Setting it
  to false without `chat_jobs` is refused, since the worker could not receive any job.
- `chat_features` lists the optional features the worker implements and the guarantees its backend keeps
  ([ADR 0036](0036-route-chat-requests-by-declared-guarantees-under-a-conformance-mode.md)), from a vocabulary the SDK
  publishes. An absent feature means unsupported, and an unknown string is dropped. Changed semantics get a new name.
- `image_transports` lists how the worker accepts a request's images: `url`, `base64` or both.

An incomplete declaration is refused with 400 `ChatDeclarationInvalid`. The pop query in
`horde/database/text_functions.py` compares the chat model, the chat limits, the tool temperature limit and image
support with the request. `TextWorker.chat_refusal_reason` in `horde/classes/kobold/worker.py`, reached through
`TextWorker.can_generate_with_eligibility_inputs`, also compares the declared features with the request's stored
`chat_required_features`. The bridge agent string does not decide chat capability. It still decides
`validated_backends` eligibility and the reward cut for unvalidated backends, as for every text job.

A generation keeps the model and chat limits the worker declared when it was assigned (`assigned_model`,
`assigned_max_context_length`, `assigned_max_length`), so a later change of declaration does not change a job in
flight.

### Consequences

- Good: A worker operator controls eligibility from configuration, and a new backend joins by declaring.
- Good: The scheduler reasons about facts the worker asserted, without a table to maintain per release.
- Good: A chat-only worker can serve chat without advertising text limits it does not serve at.
- Bad: Declarations are claims, so a misconfigured worker can declare what it cannot do.
- Bad: The union of declarations online is discovery metadata. It does not show that one worker supports every
  advertised combination.

## Pros and Cons of the Options

### Pop fields

- Good: Explicit, per worker, per pop.
- Bad: Trusts the worker.

### Bridge agent version parsing

- Good: The mechanism exists for images.
- Bad: Cannot express whether a projector is loaded or a template supports tools, and couples eligibility to releases.

### Server-side probes

- Good: Produces evidence of a capability.
- Bad: Costs worker time before any job.

## Confirmation

`tests/integration/test_chat.py` pins the pop behaviour: a worker without the chat declaration does not pop a chat
job, an invalid declaration is refused, `prompt_jobs` false without `chat_jobs` is refused, a chat-only worker pops
chat jobs and not text jobs without text limits, a request needing an undeclared feature waits for a worker that
declares it, each pop is judged by its own declaration, and the assigned model and limits survive a later change of
declaration. `tests/unit/test_text_worker_eligibility.py` pins the chat refusal reasons and the separation of chat
limits from text limits. `docs/reference/chat_requests.md` records the declaration fields and eligibility, and
`docs/how-to/serve_chat_jobs.md` shows a worker how to declare them.
