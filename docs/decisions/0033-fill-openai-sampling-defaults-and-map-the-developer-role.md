---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Fill OpenAI sampling defaults server-side and map the developer role to system

## Context and Problem Statement

OpenAI semantics for an omitted sampling field are temperature 1.0, top_p 1.0, penalties 0 and no repetition
penalty. A chat job that omits a field reaches koboldcpp's KoboldAI endpoint defaults, which include a repetition
penalty and a different temperature. Some OpenAI-compatible clients send instruction messages with role `developer`,
which the draft refuses. What does an omission mean, and what does `developer` mean?

## Decision Drivers

- Omission must mean the same thing on every worker.
- A template or worker should not have to know a role the model predates.
- Clients whose SDK defaults changed must not fail.

## Considered Options

- The Horde fills the OpenAI defaults into the worker payload and rewrites `developer` to `system`
- Pass omissions and roles through verbatim and let each worker decide
- Refuse `developer` and document the KoboldAI defaults

## Decision Outcome

Chosen option: "The Horde fills the OpenAI defaults into the worker payload and rewrites `developer` to `system`".

For chat jobs, `horde/classes/kobold/waiting_prompt.py`, `TextWaitingPrompt.prepare_job_payload`, writes every
sampling field, with the OpenAI default where the client omitted it. The chat route accepts `developer` and stores it
as `system` before any worker sees it.

### Consequences

- Good: Explicit parameter defaults across scribes; clients that send `developer` can use older templates.
- Bad: Rewriting `developer` to `system` does not preserve distinct role precedence. Document this limitation and
  do not advertise a developer-role capability while all requests are normalized this way.
- Bad: A worker's defaults are never used for chat jobs, even where an operator tuned them.

## Pros and Cons of the Options

### Fill defaults and rewrite the role

- Good: One meaning everywhere.
- Bad: Overrides operator defaults.

### Pass through verbatim

- Good: Respects the worker.
- Bad: The same request answers differently per worker; `developer` breaks templates that lack it.

### Refuse and document

- Good: Does not need a mapping.
- Bad: Fails real clients for no gain.

## More Information

First-release scope: tested text/history behaviour and neutral defaults, with non-default optional samplers routed by declared
support. Defaults do not guarantee identical answers across workers. Keep a typed message boundary so role-aware
backends can later preserve original roles under an explicit extension contract.

Rulings R6 and R23 of the review.
