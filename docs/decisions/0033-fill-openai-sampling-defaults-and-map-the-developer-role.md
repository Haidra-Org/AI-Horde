---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Fill OpenAI sampling defaults server-side and map the developer role to system

## Context and Problem Statement

OpenAI's defaults for an omitted sampling field are temperature 1.0, top_p 1.0 and penalties 0, and OpenAI does not
apply a repetition penalty. A chat job that omits a field reaches the worker's KoboldAI defaults, which on koboldcpp
include a repetition penalty and a different temperature. Some OpenAI-compatible clients send instruction messages
with the role `developer`, which many chat templates do not know. What does an omitted field mean, and what does
`developer` mean?

## Decision Drivers

- An omitted field must mean the same thing on every worker.
- A template or worker should not have to know a role the model predates.
- Clients that send `developer` must be served.

## Considered Options

- The Horde fills the OpenAI defaults into the worker payload and rewrites `developer` to `system`
- Pass omissions and roles through unchanged and let each worker decide
- Refuse `developer` and document the KoboldAI defaults

## Decision Outcome

Chosen option: "The Horde fills the OpenAI defaults into the worker payload and rewrites `developer` to `system`".

`build_chat_job_payload` in `horde/classes/kobold/chat_request.py`, called from
`TextWaitingPrompt.prepare_job_payload` for chat requests, writes every key of the SDK's `CHAT_SAMPLING_DEFAULTS`
into the job payload, with the client's value where it sent one:

| Field               | Default |
| ------------------- | ------- |
| `temperature`       | 1.0     |
| `top_p`             | 1.0     |
| `frequency_penalty` | 0.0     |
| `presence_penalty`  | 0.0     |
| `rep_pen`           | 1.0     |
| `top_k`             | 0       |
| `min_p`             | 0.0     |

The first four are OpenAI's defaults. OpenAI does not have a key for `rep_pen`, `top_k` or `min_p`, so they are set
to their neutral values. `stop_sequence`, `seed` and `reasoning_effort` are written only when the client sent them. KoboldAI samplers
outside the table, such as `top_a`, `typical`, `tfs` and `rep_pen_range`, are not written, so the worker's own
defaults apply to them. The table applies to chat jobs only; native text jobs do not use it.

The front forwards a `developer` message to the native chat route unchanged. The native route stores it as `system`
(`serialize_messages`), so a worker only meets roles every chat template knows. Since every request is normalised
this way, the chat features do not include one for preserving the `developer` role.

### Consequences

- Good: An omitted field in the table has the same value on every worker.
- Good: Clients that send `developer` are served by templates that predate the role.
- Bad: Rewriting `developer` to `system` loses any precedence a model gives the two roles.
- Bad: An operator's tuned defaults for the samplers in the table do not apply to chat jobs.
- Bad: Samplers outside the table take each worker's defaults, so the same request can still sample differently on
  different workers.

## Pros and Cons of the Options

### Fill defaults and rewrite the role

- Good: One meaning for an omitted field across workers.
- Bad: Overrides operator defaults for the samplers in the table.

### Pass through unchanged

- Good: Respects the worker's configuration.
- Bad: The same request answers differently per worker, and `developer` breaks templates that lack it.

### Refuse and document

- Good: Does not need a mapping.
- Bad: Refuses clients that send `developer`, and an omitted field still means different things per worker.

## Confirmation

`tests/unit/test_chat_request.py` (`TestChatJobPayload`, `TestMessages`) fixes the payload defaults and the stored
role. `tests/unit/oai/test_chat_front.py` fixes the front's forwarding of `developer`.
`docs/reference/chat_requests.md` (Request body, Chat job pop response) and `docs/reference/chat_front.md` (Message
roles) record the public behaviour.
