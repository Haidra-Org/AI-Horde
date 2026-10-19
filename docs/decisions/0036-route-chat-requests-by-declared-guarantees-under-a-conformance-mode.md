---
status: proposed
date: 2026-10-19
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Route chat requests by declared guarantees under a conformance mode

## Context and Problem Statement

A chat backend can accept every OpenAI chat completions field and still deviate from the contract on purpose.
koboldcpp caps the temperature at 1.0 when tools are present, its Jinja tool strategy renders the same prompt for
every `tool_choice` and ignores `parallel_tool_calls`, a JSON Schema it cannot compile to a grammar falls back to
unconstrained text, and a prompt over the context length is trimmed, which the contract refuses. Each is a
defensible choice for a local user who can see the backend. Over the Horde the requester sees only the reply. ADR 0022
declares capabilities (the worker accepts an input) but does not address fidelity (the reply follows the contract).
How does the Horde route a request that depends on fidelity, and how does a client ask for less than full fidelity?

## Decision Drivers

- A plain OpenAI client, unaware of the Horde, must get OpenAI behaviour or a refusal and must not get a silently
  altered request.
- Backend maintainers will not change every deliberate behaviour, so the Horde must catalogue the deviations.
- Every routing decision must be derivable from the request on the server; the default path must not need an added
  request field.
- Worker reports are claims. Routing may trust a claim; cost and reward may not.
- The vocabulary is shared by the server and workers, so it belongs in the SDK beside the image sampler contracts.

## Considered Options

- Declared guarantees and limits, strict conformance by default, best-effort as a request-side opt-in
- Declared guarantees and limits, best-effort by default, strict as a request-side opt-in
- Server-side rewriting of requests to what the backend does (clamp, filter) without declarations
- Refuse every request that touches a known deviation on every worker

## Decision Outcome

Chosen option: "Declared guarantees and limits, strict conformance by default, best-effort as a request-side
opt-in".

Three declaration kinds, defined once in `horde_sdk.generation_parameters.text.chat_policy`:

- A capability (`ChatFeature`) states the backend acts on an input: `seed`, the penalties, `vision`, `tools`,
  `json_object`, `json_schema`. Unchanged from ADR 0022.
- A guarantee (`ChatGuarantee`) states the backend follows one part of the contract exactly:
  `tool_choice_enforced`, `single_tool_call_enforced`, `structured_output_enforced`, `prompt_overflow_refused`.
  Capabilities and guarantees share the `chat_features` pop list.
- A limit (`ChatWorkerLimits`) bounds the values the backend applies as sent. `tools_max_temperature` joins
  `max_context_length` and `max_length` in `chat_limits`; absent means the temperature is applied as sent.

Each request carries a conformance mode in the `horde` body object, `strict` by default. The server derives the
request's traits once, stores the required guarantees beside the required capabilities, and compares both with the
worker's declaration in eligibility. Limits apply under both modes, because a clamped value changes the request.
Under `best_effort` the request does not require a guarantee and replies pass through. When the online chat workers
serving the model all lack a declaration the request needs, or all apply a limit it exceeds, the chat status reports
the gap as `impossible_reason` and the chat front refuses the request at once with 404
`no_worker_supports_features`, without the grace it gives a model without an online worker.

The server narrows a job before dispatch where the contract allows: tool definitions are never forwarded under
`tool_choice: none`, and a named `tool_choice` forwards only that function under `required`. On submit the server
verifies what the payload can prove: a call under `none`, a missing or wrong call under a forced choice, and
duplicate call ids are refused; more than one call under `parallel_tool_calls: false` is reduced to the first; a
`strict` `json_schema` reply is validated against the schema. These checks do not trust anything the worker says.

Work the server cannot verify, such as a backend's hidden tool-selection pass or a trimmed prompt, is never
rewarded. Any allowance for tool work, if added, is a request-derived estimate, uniform across backends, placed with the
pricing basis of ADR 0027.

### Consequences

- Good: A plain client gets honoured requests or a refusal with a return code; a Horde-aware client can opt into
  best effort.
- Good: A backend joins by declaring what it enforces; a strategy that enforces less declares less, without a change to
  its local behaviour.
- Good: Routing, narrowing and verification are three separate layers; a false declaration costs the worker refused
  submissions, and the requester does not receive a wrong reply.
- Bad: Under strict a request needing a guarantee no online worker declares is refused, so coverage depends on
  workers adopting the declarations.
- Bad: Submit verification adds a JSON Schema validator dependency and a validation pass per structured reply.

## Pros and Cons of the Options

### Strict by default, best-effort opt-in

- Good: The clients that cannot opt in are the ones that need strict behaviour.
- Bad: Early coverage is thinner until workers declare guarantees.

### Best-effort by default, strict opt-in

- Good: Every request finds a worker.
- Bad: A plain OpenAI client receives silently altered behaviour and cannot tell.

### Server-side rewriting without declarations

- Good: Does not need a worker change.
- Bad: The server would clamp or drop values on the client's behalf, the deviation the decision exists to prevent,
  and it would have to know every backend's behaviour.

### Refuse every known deviation everywhere

- Good: Simplest rule.
- Bad: Punishes backends that do enforce the contract, and does not leave a path for best-effort clients.

## More Information

Does not supersede a record; extends ADR 0022 (declaration mechanism) and relies on ADR 0027 (pricing basis) and ADR 0029
(strict request schema). The `horde` body object is the one ADR 0020 defines.
