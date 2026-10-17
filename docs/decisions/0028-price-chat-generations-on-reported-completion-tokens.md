---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Price chat generations on reported completion tokens

## Context and Problem Statement

The same question as [ADR 0027](0027-price-chat-generations-by-the-text-output-estimate.md): the basis on which chat
generations are charged and rewarded. This record proposes the worker-reported completion count.

## Decision Drivers

- A reply should cost what it used.
- Agent loops send large `max_tokens` by default and would be priced out under capacity pricing.
- Workers under contract v2 report counts on every submit.

## Considered Options

- Reported completion tokens, bounded above by the requested length and below by a floor
- The text output estimate (characters divided by 4, floored and capped at the requested length)
- Reserve on requested length, settle on reported tokens

## Decision Outcome

Chosen option: "Reported completion tokens, bounded above by the requested length and below by a floor".

The charge basis function returns the smaller of the requested length and the reported completion count, with a
floor so an empty or tiny reply still pays the worker's prompt processing. Prompt tokens continue to be priced
through the context multiplier. Reports are bounded, and a worker whose reports disagree with the estimate beyond a
threshold is flagged for review.

### Consequences

- Good: Fair per reply; matches client expectations.
- Bad: An incentive for workers to over-report; auditing is required before the switch.
- Bad: Upfront kudos still reserve on the requested length, so the user experience at submit is unchanged.

## Pros and Cons of the Options

### Reported tokens

- Good: Accurate.
- Bad: Trust and audit work.

### Text output estimate

- Good: Simple and established; does not need trust in reports.
- Bad: Approximates tokens from characters.

### Reserve then settle

- Good: Fair and safe.
- Bad: Adds machinery.

## More Information

Disposition: not implemented; the fork stays open as a hedge. Retaining reported usage does not switch the
policy. Accepting this record is a separate decision.

This record and [ADR 0027](0027-price-chat-generations-by-the-text-output-estimate.md) are mutually exclusive. Accepting one
rejects the other. The data ADR 0027 stores is what this record needs to be evaluated.
