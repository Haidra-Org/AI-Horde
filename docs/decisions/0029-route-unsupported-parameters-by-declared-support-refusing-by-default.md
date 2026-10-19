---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Route by declared parameter support and refuse unsupported values by default

## Context and Problem Statement

Workers differ in which sampling and capability fields they honour. A client that sends `frequency_penalty` or
`tools` to a worker that ignores them receives a reply that silently differs from what it asked for. What happens
to a request field no eligible worker supports?

## Decision Drivers

- A behaviour-changing field must not be silently discarded.
- Capability fields (`tools`, `response_format`, image parts) can never be dropped; they change the answer.
- Stock clients send sampling fields at their default values unprompted; refusing those would break everything.
- OpenRouter-aware clients expect a lenient mode to exist.

## Considered Options

- Route by declared support; a default-valued field counts as omitted; a non-default unsupported sampling field is
  refused unless the client opts into dropping it
- Route by declared support; drop unsupported sampling fields by default and report them
- Refuse every unsupported field, including defaults

## Decision Outcome

Chosen option: "Route by declared support; a default-valued field counts as omitted; a non-default unsupported
sampling field is refused unless the client opts into dropping it".

Capability fields are hard requirements: the request is routed only to workers declaring them, and when none is
online the answer is 503 with `Retry-After`. Sampling fields at their documented default are omitted for routing.
A field/value outside the implemented service contract is a 400 that lists the field. A supported non-default value
without an eligible worker at the time is 503, decided by the front from the native `is_possible` answer after
submission, so a temporary worker outage is not described as invalid input. The native submit path does not
change for this.

The first release uses strict behaviour only. A future opt-in `horde.require_parameters: false` mode may drop unsupported sampling
fields with explicit reporting; it is not required for the first release. Capability fields are never dropped. Accept only listed
default no-ops such as empty tools or false logprobs; this is not permission to ignore arbitrary unknown fields.

### Consequences

- Good: The default never hides a changed answer; any later lenient mode must be explicit.
- Bad: Some clients will meet a 400 on a field they always send, and must either stop sending it or opt in.

## Pros and Cons of the Options

### Refuse by default, opt into dropping

- Good: Honest by default.
- Bad: Friction for clients that spray fields.

### Drop by default, report

- Good: Matches OpenRouter; fewer 400s.
- Bad: The default hides a changed answer in a field most clients never read.

### Refuse everything

- Good: Simplest rule.
- Bad: Breaks every client that sends defaults.

## More Information

This record and [ADR 0030](0030-route-unsupported-parameters-by-declared-support-ignoring-by-default.md) are
mutually exclusive. Accepting one rejects the other. Ruling R5 of the review chose this record.

### Amendment: unknown keys inside messages, tool definitions and tool choice

Messages and their nested objects, the `tools` definitions and the `tool_choice` object drop unknown keys; every
other object, and the top level, refuses them. In the OpenAI chat front, a dropped key is a key outside the role's
set, or a key outside the served fields of a tool call, its function object, a content part, an image part's
`image_url` object, a `tools` entry, its `function` object, the `tool_choice` object or its `function` object. The
dropped key is never forwarded. A function's `parameters` schema is client data and is forwarded unchanged. Served
keys keep their validation. An unserved role, content part type or tool type is still refused, and so is
`tool_calls` on a message other than an assistant message. The `horde` extension object, `stream_options` and
`response_format` keep the strict rule.

The split mirrors OpenAI. Probing OpenAI's chat completions API showed that it returns a normal completion for a
`tool` message carrying `name`, for a message carrying an arbitrary unknown key, for `tool_call_id` on a `user`
message, and for an unknown key inside a content part, inside a tool call of an assistant history message, on a
`tools` entry, on its `function` object and inside the `tool_choice` object. It refuses an unknown top-level
parameter with "Unrecognized request argument supplied: X", an unknown key inside `response_format`,
`response_format.json_schema` or `stream_options` with "Unknown parameter: 'response_format.bogus'" and that path as
`param`, and `tool_calls` on a non-assistant message with "Invalid parameter: only messages with role 'assistant' can
have a function call". The front's refusals mirror these. The OpenAI Python SDK also replays its accumulated streamed
reply with fields it adds (`refusal`, `audio`, `annotations`, `function_call`, `parsed`, `tool_calls[].index`,
`function.parsed_arguments`), so refusing them would break the SDK's tool loop.
