---
status: accepted
date: 2026-10-28
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
reasonable choice for a local user who can see the backend. Over the Horde the requester sees only the reply.
[ADR 0022](0022-declare-chat-capabilities-by-pop-fields.md) declares capabilities (the worker accepts an input) and
does not address fidelity (the reply follows the contract). How does the Horde route a request that depends on
fidelity, and how does a client ask for less than full fidelity?

## Decision Drivers

- A plain OpenAI client, unaware of the Horde, must get OpenAI behaviour or a refusal, and must not get an altered
  request without knowing it.
- Backends keep their deliberate behaviours, so the Horde has to record which ones a worker avoids.
- Every routing decision must be derivable from the request on the server, and the default path must not need an
  added request field.
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

Three declaration kinds are defined once in `horde_sdk.generation_parameters.text.chat_policy`:

- A capability (`ChatFeature`) states that the backend acts on an input: `seed`, `presence_penalty`,
  `frequency_penalty`, `reasoning_effort`, `vision`, `tools`, `json_object`, `json_schema` and
  `tools_with_response_format`, declared as ADR 0022 describes. A request needs its capabilities under both modes.
- A guarantee (`ChatGuarantee`) states that the backend follows one part of the contract exactly:
  `tool_choice_enforced`, `single_tool_call_enforced`, `structured_output_enforced` and `prompt_overflow_refused`.
  Capabilities and guarantees share the `chat_features` pop list.
- A limit (`ChatWorkerLimits`) bounds the values the backend applies as sent: `max_context_length`, `max_length` and
  `tools_max_temperature`, in the `chat_limits` pop field. When `tools_max_temperature` is absent the temperature is
  applied as sent.

`reasoning_effort` accepts OpenAI's values `none`, `minimal`, `low`, `medium`, `high` and `xhigh`, and is forwarded
unchanged. The server does not set a default. A request that sets it, `none` included, needs a worker that declares the
`reasoning_effort` capability, since a backend that ignores the key reasons anyway.

Each request carries a conformance mode, `horde.conformance`, which is `strict` by default. The server derives the
request's requirements once at submit (`required_chat_features` in `horde/classes/kobold/chat_request.py`), stores
the guarantees beside the capabilities, and compares both with the worker's declaration in eligibility. A `strict`
request requires:

- `tool_choice_enforced` when it forwards tools under `tool_choice: required` or a choice that specifies a function;
- `structured_output_enforced` when its `json_schema` format is `strict`;
- `prompt_overflow_refused` always.

A `best_effort` request does not require a guarantee. Limits apply under both modes, because a clamped value changes
the request.

`single_tool_call_enforced` is part of the vocabulary so a worker can declare it, and a request does not require it. Under
`strict`, when `parallel_tool_calls` is false, the server keeps only the first call of a reply, so the client
receives at most one call from any worker.

When the online chat workers serving the model all lack a declaration the request needs, or all apply a
`tools_max_temperature` it exceeds, the chat status reports the gap as `impossible_reason`, and the chat front refuses
the request with 404 `no_worker_supports_features` at the status read that reports it. A request over the
`max_length` or `max_context_length` that every online worker serving the model declares is refused with 400
`no_worker_supports_limit`.

Before dispatch the server narrows the job where the contract allows (`build_worker_chat_extras`): tool definitions
are not forwarded under `tool_choice: none`, and a `tool_choice` that specifies a function forwards only that
function, under `required`.

On submit the server checks what the payload can prove (`verify_chat_reply`) and does not trust anything the worker
reports:

| Check                                                                                  | Mode            | Outcome                                  |
| -------------------------------------------------------------------------------------- | --------------- | ---------------------------------------- |
| A call to a function the request did not offer                                         | Both            | Refused, `ChatSubmitInvalid`             |
| Two calls sharing an id                                                                | Both            | Refused, `ChatSubmitInvalid`             |
| `reasoning_content` over the SDK's `MAX_CHAT_TEXT_BYTES`                               | Both            | Refused, `ChatSubmitInvalid`             |
| More than one call under `parallel_tool_calls: false`                                  | `strict`        | Reduced to the first call                |
| A call under `tool_choice: none`, or a missing or wrong call under a forced choice     | `strict`        | Refused, `ChatSubmitNonconformant`       |
| Reply content that does not satisfy a `strict` `json_schema`, or is not a JSON object under `json_object` or a non-strict `json_schema` | `strict` | Refused, `ChatSubmitNonconformant` |

Under `best_effort` a reply that passes the checks for both modes is delivered as submitted. A refused submit costs
the worker a faulted job.

Work the server cannot verify, such as a backend's hidden tool-selection pass or a trimmed prompt, is not rewarded.
Pricing follows [ADR 0027](0027-price-chat-generations-by-the-text-output-estimate.md).

### Consequences

- Good: A plain client gets its request honoured or a refusal with a return code, and a Horde-aware client can opt
  into best effort.
- Good: A backend joins by declaring what it enforces; a strategy that enforces less declares less and keeps its
  local behaviour.
- Good: A false declaration costs the worker refused submits, and the requester does not receive a nonconformant
  reply under `strict`.
- Bad: Under `strict` every request requires `prompt_overflow_refused`, so a chat worker that does not declare it
  serves only `best_effort` requests.
- Bad: Under `strict` a request needing a guarantee that no online worker declares is refused, so coverage depends on
  workers adopting the declarations.
- Bad: Submit verification adds a JSON Schema validator dependency (`jsonschema`) and a validation pass per
  structured reply.

## Pros and Cons of the Options

### Strict by default, best-effort opt-in

- Good: The clients that cannot opt in are the ones that need strict behaviour.
- Bad: Coverage depends on how many workers declare guarantees.

### Best-effort by default, strict opt-in

- Good: Every request finds a worker.
- Bad: A plain OpenAI client receives altered behaviour and cannot tell.

### Server-side rewriting without declarations

- Good: Does not need a worker change.
- Bad: The server would clamp or drop values on the client's behalf without telling it, and would have to know every
  backend's behaviour.

### Refuse every known deviation everywhere

- Good: The simplest rule.
- Bad: Refuses requests that backends enforcing the contract could serve, and a client that would accept best effort
  cannot ask for it.

## Confirmation

`tests/unit/test_chat_request.py` (`TestSharedHordeOptions`, `TestReasoningEffort`, `TestRequiredGuarantees`,
`TestRequiredFeatures`, `TestWorkerChatExtras`, `TestToolsTemperature`, `TestReplyVerification`) fixes the
conformance default, the derived requirements, narrowing and the submit checks.
`tests/unit/test_text_worker_eligibility.py` fixes eligibility against the declarations. `tests/unit/oai/test_errors.py`
fixes the 404 and 400 answers to a gap, and `tests/integration/test_chat.py` covers both modes end to end.
`docs/reference/chat_requests.md` (Chat declaration on pop, Guarantees, Tool temperature limit, Eligibility, Reply
verification, Reasoning) and `docs/how-to/serve_chat_jobs.md` record the public behaviour.

## More Information

This record extends ADR 0022 with guarantees and limits. It relies on
[ADR 0029](0029-route-unsupported-parameters-by-declared-support-refusing-by-default.md) for the strict request schema
and on ADR 0027 for pricing. The `horde` body object is the one
[ADR 0020](0020-select-chat-service-by-request-body-and-model-variants.md) defines.
