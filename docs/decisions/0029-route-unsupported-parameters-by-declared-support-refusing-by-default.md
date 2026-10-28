---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Route by declared parameter support and refuse unsupported values

## Context and Problem Statement

Workers differ in which sampling and capability fields they act on. A client that sends `frequency_penalty` or
`tools` to a worker that ignores them receives a reply that differs from what it asked for, and the reply does not
show it. Stock OpenAI clients also send some fields at their default value on every request. What happens to a
request field that the service or the eligible workers do not serve?

## Decision Drivers

- A field that changes the answer must not be discarded without the client knowing.
- Capability fields (`tools`, `response_format`, image parts) change the answer whatever their value.
- Stock clients send some fields at their default value unprompted.
- Most OpenAI-compatible clients were written against services that ignore unknown sampling fields.
- A refusal caused by the online workers should tell the client whether waiting can help.

## Considered Options

- Route by declared support, treat a default value as omitted, and refuse any other unsupported value
- Route by declared support, drop unsupported sampling values and report them
- Refuse every unsupported field, including default values

## Decision Outcome

Chosen option: "Route by declared support, treat a default value as omitted, and refuse any other unsupported value".

The front screens a request before validation (`horde/oai/schema.py`). A key, value, role, content part type or
`horde` option outside the service contract is refused with 400 and the return code `UnsupportedChatParameter`, and
the message states the parameter. Values outside the contract include `n` other than 1, a token limit outside 16 to
16384, more than four stop sequences and a sampling value outside OpenAI's range.

A closed table of keys, `DEFAULT_NO_OP_PARAMETERS`, is accepted and dropped when the key holds its documented
default:

| Key               | Dropped when it is              |
| ----------------- | ------------------------------- |
| `tools`           | null or an empty list           |
| `response_format` | `{"type": "text"}`              |
| `logprobs`        | false or null                   |
| `top_logprobs`    | the integer 0 or null           |
| `store`           | false                           |
| `service_tier`    | `auto` or `default`             |

`tools` and `response_format` holding any other value are validated and forwarded. Any other value of the remaining
keys is refused with `UnsupportedChatParameter`, as is a top-level key that is neither in the table nor a field of
the request model.

Unknown keys are refused at the top level and inside every object except messages and their nested objects, the
`tools` definitions and the `tool_choice` object, which drop them. Inside those objects a dropped key is one outside
the role's set, or outside the served fields of a tool call, its function object, a content part, an image part's
`image_url` object, a `tools` entry, its `function` object, the `tool_choice` object or its `function` object. A
dropped key is not forwarded. A function's `parameters` schema is client data and is forwarded unchanged. Served
keys are validated. An unserved role, content part type or tool type is refused, and so is `tool_calls` on a
message other than an assistant message. The `horde` extension object, `stream_options` and `response_format`
refuse unknown keys.

The split follows OpenAI. OpenAI's chat completions API returns a normal completion for a `tool` message carrying
`name`, for a message carrying an unknown key, for `tool_call_id` on a `user` message, and for an unknown key inside
a content part, a tool call of an assistant history message, a `tools` entry, its `function` object or the
`tool_choice` object. It refuses an unknown top-level parameter, an unknown key inside `response_format`,
`response_format.json_schema` or `stream_options`, and `tool_calls` on a non-assistant message. The OpenAI Python
SDK replays its accumulated streamed reply with fields it adds (`refusal`, `audio`, `annotations`, `function_call`,
`parsed`, `tool_calls[].index`, `function.parsed_arguments`), so refusing those would break the SDK's tool loop.

A served field that needs a worker to act on it becomes a requirement stored on the request
(`required_chat_features` in `horde/classes/kobold/chat_request.py`): `seed` when set, `reasoning_effort` when set,
each penalty whose value is not 0, `vision` for an image part, `tools`, `json_object` or `json_schema` by the response
format, and `tools_with_response_format` when both are forwarded. A penalty at 0 is not a requirement, so a request
that sends the default is served by any chat worker. Only workers that declare every requirement are eligible, as
[ADR 0022](0022-declare-chat-capabilities-by-pop-fields.md) describes. When the eligible workers do not cover the
request, the front answers by the cause (`horde/oai/app.py`, `horde/oai/waiter.py`):

| Cause                                                                                           | Answer                                                                 |
| ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| The online workers serving the model all lack a declaration the request needs, or all apply a `tools_max_temperature` it exceeds | 404 `no_worker_supports_features` at the status read that reports it; the answer does not carry `Retry-After` |
| The request exceeds the `max_length` or `max_context_length` every online worker serving the model declares | 400 `no_worker_supports_limit`, with `param` set to the token limit or context length key |
| No worker is eligible, nothing is processing, the status does not report a declaration gap, and the fail-fast grace has passed | 503 `no_eligible_worker` with `Retry-After` |

A declaration gap does not close when a worker restarts, so the 404 is returned at once and does not carry a retry hint.
A size limit gap is a property of what the request asks for at the time, so it is answered as an invalid request.
The grace before the 503 (20 seconds by default) covers a worker restart or a model load.

### Consequences

- Good: A reply either follows every field the client sent or is refused with the parameter stated.
- Good: A client that sends only the listed defaults is served by any chat worker.
- Bad: A client that always sends a field outside the contract receives a 400 until it stops sending it.
- Bad: A non-default penalty or a seed narrows the eligible workers to those that declare it.

## Pros and Cons of the Options

### Refuse unsupported values and treat defaults as omitted

- Good: The client learns of every field that would change the answer.
- Good: Default values that stock clients send cost nothing.
- Bad: A client cannot ask the service to drop a field; it has to remove the field itself.

### Drop unsupported sampling values and report them

- Good: Matches the services most OpenAI-compatible clients were written against, and refuses fewer requests.
- Bad: The changed answer is reported only in an extension field listing the dropped keys, which most clients do not
  read.
- Bad: Needs a request option to restore refusals and a response field for the report.

### Refuse every unsupported field, including defaults

- Good: The simplest rule.
- Bad: Refuses stock clients that send default values unprompted.

## Confirmation

`tests/unit/oai/test_request_screening.py` fixes the refused keys, values, roles and content parts and the nested
keys that are dropped. `tests/unit/oai/test_admission_and_table.py` fixes the default no-op table.
`tests/unit/test_chat_request.py` (`TestUnsupportedParameters`, `TestRequiredFeatures`) fixes the native refusals and
the requirements derived from a request. `tests/unit/oai/test_errors.py` and `tests/unit/oai/test_lifecycle.py` fix
the 404, 400 and 503 answers to a worker gap. `docs/reference/chat_front.md` (Request keys, No eligible worker) and
`docs/reference/chat_requests.md` (Refused keys and values, Impossible reason) record the public behaviour.

## More Information

[ADR 0030](0030-route-unsupported-parameters-by-declared-support-ignoring-by-default.md) records the rejected option
of dropping unsupported sampling values by default.
[ADR 0036](0036-route-chat-requests-by-declared-guarantees-under-a-conformance-mode.md) adds guarantees and limits to
the same declaration.
