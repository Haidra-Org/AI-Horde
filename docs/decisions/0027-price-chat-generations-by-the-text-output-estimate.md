---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Price chat generations by the text output estimate, with the basis isolated

## Context and Problem Statement

Text generations are settled at submit in `horde/classes/kobold/processing_generation.py`,
`TextProcessingGeneration.get_gen_kudos`. The token count it prices is `get_things_count`: the returned characters
divided by 4, rounded up, at least 20 and at most the requested `max_length`. The requester is charged and the worker
rewarded from that one figure. `TextWaitingPrompt.calculate_kudos` prices a quote or upfront check on the requested
`max_length`, because the output does not exist at that point. OpenAI clients routinely request 4096 or more tokens and receive short
replies. With contract v2 the worker also reports prompt and completion token counts. On what basis are chat
generations priced?

## Decision Drivers

- The established text pricing is a product position users and workers understand.
- Reported token counts are claims from volunteers until audited.
- Chat preserves main's native text pricing, cancellation and worker-reward dynamics.
- Reported counts are stored so the alternative can be evaluated; storing them does not switch the policy.

## Considered Options

- The text output estimate, as text, with the charge basis isolated in one function and reported counts stored
- Reported completion tokens, bounded by the requested length
- A blend: reserve on the requested length, settle on reported tokens

## Decision Outcome

Chosen option: "The text output estimate, as text, with the charge basis isolated in one function and reported
counts stored".

Chat generations charge and reward exactly as text does: the text kudos formula over ceil(output characters / 4),
floored at 20 and capped at the requested `max_length`. One function per request type,
`TextProcessingGeneration.get_charge_basis`, returns the basis, `output_estimate`, and that value is stored on every
chat generation as `charge_basis`. The reported prompt and completion counts are stored and shown in the response. They are
inert: they do not change the charge or the reward.

Preserve main's activation tax and dispatched-work settlement. Undispatched choices do not incur a generation charge;
cancelling dispatched work retains the native cancellation charge/reward path and its exceptions. Worker faults
retain the separate abort/reissue semantics. Disconnects, missing output and future partial streams do not introduce
refunds. Repeated/late submit, cancellation and cleanup must not settle the same generation twice.

### Consequences

- Good: The pricing policy does not change at cutover; calculations stay behind a small implementation boundary.
- Good: A short reply to a large `max_tokens` is charged for the short reply; the requested length only caps the
  charge.
- Bad: The estimate counts characters, so it diverges from the worker's token count for non-English text and
  code; the 20-token floor charges a near-empty reply as 20 tokens.
- Bad: A later policy change also requires quotes, reservations, cancellation, reporting trust and worker-incentive
  review. It is not a one-function operational change.

## Pros and Cons of the Options

### Text output estimate

- Good: Established; does not need trust in worker reports.
- Bad: An approximation of the tokens actually generated.

### Reported tokens

- Good: Fair per reply; what OpenAI-shaped clients expect.
- Bad: Trusts worker reports; changes worker incentives.

### Reserve then settle

- Good: Both properties.
- Bad: The settlement step adds accounting machinery.

## More Information

This record and [ADR 0028](0028-price-chat-generations-on-reported-completion-tokens.md) are mutually exclusive.
Accepting one rejects the other. Ruling R18 of the review chose this record as the hedge while the position is
considered. ADR 0028 remains the open alternative; the hedge stands until the project owner closes the fork. Characterize cancellation against a pinned main/deployment revision, including activation
tax, reservation release and dispatched-generation cleanup, before claiming parity.
