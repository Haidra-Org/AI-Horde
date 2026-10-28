---
status: accepted
date: 2026-10-28
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
`max_length`, because the output does not exist at that point. OpenAI clients routinely request 4096 or more tokens
and receive short replies. A chat worker may also report prompt, completion and reasoning token counts on submit. On
what basis are chat generations priced?

## Decision Drivers

- The text pricing is a policy users and workers understand, and chat and text should not diverge without reason.
- Reported token counts are claims from volunteer workers that the Horde cannot check.
- A reply should not be charged for output that was requested and not generated.
- Reported counts should be kept, so the generated output and the reported usage can be compared.

## Considered Options

- The text output estimate, with the charge basis recorded per generation and reported counts stored
- Reported completion tokens, bounded above by the requested length and below by a floor
- A blend: reserve on the requested length, settle on reported tokens

## Decision Outcome

Chosen option: "The text output estimate, with the charge basis recorded per generation and reported counts stored".

A chat generation is charged and rewarded as a text generation with the same reply: the text kudos formula over
ceil(output characters / 4), floored at 20 and capped at the requested `max_length`. The reply's characters are its
content plus each tool call's function name and arguments (`chat_reply_characters`), so tool calls are charged as
output. `TextProcessingGeneration.get_charge_basis` returns the basis, `output_estimate`, for every text generation,
and a submitted chat generation stores it in the `charge_basis` column. The reported prompt, completion and reasoning
token counts are optional, stored, and shown on the status route and in the OpenAI `usage` object. They do not change
the charge or the reward.

The activation tax and upfront kudos follow the text path. A choice that is not dispatched does not incur a
generation charge. Worker faults keep the text abort and reissue rules.

When a streamed chat request is cancelled, including by the front when its client disconnects, each generation still
in flight is settled on the output its worker had posted through the progress route, priced by the same estimate with
the floor of 20 tokens: the requester is charged and the worker is paid for that output
([ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md)).
`TextProcessingGeneration.settle_cancelled` locks and reloads the generation first, so a submit or fault that lands
during the cancel settles it once. The settlement records the priced character count in `settled_character_count` and
leaves `charge_basis` null. A generation of a cancelled request that does not stream stays in flight and is settled
when its worker submits, as for text.

### Consequences

- Good: Chat and text share one pricing policy, so a worker earns the same for the same reply on either route.
- Good: A short reply to a large `max_tokens` is charged for the short reply; the requested length only caps the
  charge.
- Good: The stored basis and reported counts let the estimate be compared with reported usage per generation.
- Bad: The estimate counts characters, so it diverges from the worker's token count for non-English text and code.
- Bad: The 20-token floor charges a near-empty reply, and a cancelled streamed generation without posted output, as
  20 tokens.
- Bad: Quotes and the upfront kudos check use the requested length, so a large `max_tokens` is quoted and checked
  for more than a short reply is charged.
- Bad: Changing the basis would also mean reviewing quotes, reservations, cancellation settlement, report trust and
  worker incentives together.

## Pros and Cons of the Options

### Text output estimate

- Good: Established, measured by the Horde, and does not depend on worker reports.
- Bad: An approximation of the tokens generated.

### Reported completion tokens

- Good: Charges each reply for the tokens its worker reports, which OpenAI-shaped clients expect.
- Good: Agent loops that send large `max_tokens` by default pay for what they used.
- Bad: Gives workers an incentive to over-report, so reports would need auditing and a bound.
- Bad: Needs a floor so an empty or tiny reply still pays the worker's prompt processing.
- Bad: Counts are optional, so a generation without a report still needs the estimate.
- Bad: Upfront kudos would still reserve on the requested length, so the requester's experience at submit is the same.

### Reserve then settle

- Good: Reserves safely and charges per reply.
- Bad: The settlement step adds accounting machinery, and it still trusts the reported count.

## Confirmation

`tests/integration/test_chat.py` pins a structured reply charged as text with `charge_basis` `output_estimate`, a
text generation's status without `charge_basis`, unreported token counts as null, the empty reply, a tool-call reply
charged by its arguments, and reasoning stored and shown without being charged.
`tests/integration/test_chat_progress.py` pins the settlement of a cancelled streamed generation on its posted output
and on the 20-token floor, and that a cancel of a request that does not stream leaves its generation pending and
unpaid. `docs/reference/chat_requests.md` records the pricing, and `docs/explanation/chat_requests.md` explains it.

## More Information

[ADR 0028](0028-price-chat-generations-on-reported-completion-tokens.md) records pricing on reported completion
tokens as a rejected alternative.
