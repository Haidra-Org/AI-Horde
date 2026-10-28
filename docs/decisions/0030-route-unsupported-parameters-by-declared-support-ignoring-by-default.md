---
status: rejected
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Route by declared parameter support and drop unsupported sampling values by default

## Context and Problem Statement

[ADR 0029](0029-route-unsupported-parameters-by-declared-support-refusing-by-default.md) covers this question and
records the decision. This record describes one option it rejected.

## Considered Options

- Drop a non-default sampling value that no eligible worker supports, list it in an extension field of the response,
  and refuse it only when the client sets a request option asking for refusal

## Decision Outcome

Rejected in favour of ADR 0029. A dropped value changes the answer, and the report reaches only clients that read
the extension field. The service refuses a value outside its contract with 400 `UnsupportedChatParameter`, and a value
that no online worker serving the model declares with 404 `no_worker_supports_features`. The request option and the
reporting field of this option do not exist.

## More Information

ADR 0029 records the arguments for this option under its Pros and Cons of the Options.
