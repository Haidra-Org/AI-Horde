---
status: rejected
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Price chat generations on reported completion tokens

## Context and Problem Statement

A chat generation is charged to the requester and rewarded to the worker from one token count. A chat worker may
report prompt and completion token counts on submit. This record evaluates the reported completion count as that
basis. [ADR 0027](0027-price-chat-generations-by-the-text-output-estimate.md) is the accepted decision on this
question.

## Decision Drivers

- A reply should cost the tokens it used.
- Agent loops send large `max_tokens` by default, and pricing on the requested length would price them out.

## Considered Options

- Reported completion tokens, bounded above by the requested length and below by a floor
- The text output estimate: characters divided by 4, floored and capped at the requested length
- Reserve on the requested length, settle on reported tokens

## Decision Outcome

Rejected. The evaluated option charges the smaller of the requested length and the reported completion count, with a
floor so an empty or tiny reply still pays the worker's prompt processing. Prompt tokens stay priced through the
context multiplier of the text formula. Reports are bounded, and a worker whose reports disagree with the output
estimate beyond a threshold is flagged for review.

### Consequences

- Good: Each reply is charged for the tokens its worker reports, which OpenAI-shaped clients expect.
- Bad: Workers gain an incentive to over-report, so reports need auditing.
- Bad: Reported counts are optional on submit, so a generation without a report still needs another basis.
- Bad: Upfront kudos still reserve on the requested length, so the requester's experience at submit is unchanged.

## Pros and Cons of the Options

### Reported completion tokens

- Good: Matches the worker's tokenizer, including for non-English text and code.
- Bad: Requires trust in, and auditing of, worker reports.

### Text output estimate

- Good: Simple and established, and measured by the Horde.
- Bad: Approximates tokens from characters.

### Reserve then settle

- Good: Reserves safely and charges per reply.
- Bad: Adds accounting machinery and still trusts the reported count.
