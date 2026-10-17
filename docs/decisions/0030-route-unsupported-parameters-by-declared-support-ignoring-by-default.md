---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Route by declared parameter support and drop unsupported sampling values by default

## Context and Problem Statement

The same question as
[ADR 0029](0029-route-unsupported-parameters-by-declared-support-refusing-by-default.md). This record proposes
OpenRouter's default: unsupported sampling fields are dropped and the client may opt into strictness.

## Decision Drivers

- The largest population of OpenAI-compatible clients was written against services that ignore unknown sampling
  fields.
- A 400 on a field the client always sends is a support burden without a fix on the client side.
- Capability fields remain hard requirements either way.

## Considered Options

- Drop unsupported sampling fields by default; report them; `require_parameters: true` refuses instead
- Refuse by default; opt into dropping
- Refuse everything

## Decision Outcome

Chosen option: "Drop unsupported sampling fields by default; report them; `require_parameters: true` refuses
instead".

Capability fields are hard requirements as in ADR 0029. A non-default sampling field no eligible worker supports is
dropped, listed in `x_horde.ignored_parameters`, and the request proceeds. A client that sets
`horde.require_parameters` to true is refused with a 400 that lists the field.

### Consequences

- Good: Fewer refusals; the lenient behaviour clients expect.
- Bad: The changed answer is reported in an extension field most clients never read, which the compatibility ethos
  rejects.

## Pros and Cons of the Options

### Drop by default

- Good: Compatible with the widest set of clients.
- Bad: Silent to the user.

### Refuse by default

- Good: Honest.
- Bad: Friction.

### Refuse everything

- Good: Simple.
- Bad: Unusable.

## More Information

Disposition: not selected. Strict implemented behaviour plus a small tested set of default no-ops is the first
release's contract. Measure concrete client incompatibilities before adding lenient dropping or its settings/reporting surface.

This record and [ADR 0029](0029-route-unsupported-parameters-by-declared-support-refusing-by-default.md) are
mutually exclusive. Accepting one rejects the other. The review chose ADR 0029; this record stays as the alternative
if friction proves too high in practice.
