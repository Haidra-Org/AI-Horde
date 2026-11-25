---
status: accepted
date: 2026-11-23
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Bound free input by a wait-sized allowance

## Context and Problem Statement

ADR 0037 sizes the free output allowance by the request's estimated wait. A text or chat request within it is
admitted without a kudos hold, and above it the requester must have kudos. The allowance applies only to
`max_length`, the tokens to generate. A request with a short output and a very long prompt passes as free, however
long the prompt is. For example, a 50,000-token prompt with 512 tokens to generate does not need kudos.

Processing a prompt occupies a worker before it generates anything, and a long prompt can take longer than the reply.
An anonymous user can therefore queue many long-context requests on a weak worker without kudos. A worker can declare
a smaller `max_context_length` to protect itself, but a worker that serves a large context for funded users is not
protected from free ones.

Prompt length has little effect on what a text request holds or pays. The amount held at admission is computed from
`max_length` and the requested models. The dry-run quote multiplies `max_length` by a factor that grows with
`max_context_length` and stops growing at about 19,600 tokens. The charge at completion uses the same kind of factor,
but it measures the prompt only up to `max_length` tokens. A long prompt with a short output therefore has a small
quote, a small hold and a small charge.

How should free use of input be bounded, and what should a request over that bound hold?

## Decision Drivers

- Ordinary free use, including a default client's full context on an idle fleet, stays free.
- The bound responds to load on the workers that would serve the request, as the output allowance does.
- The bound applies to the input processed by a worker, which can be smaller than the requested context.
- The rule does not set an input-to-output cost ratio.
- The rule is explainable in a refusal message.

## Considered Options

- Leave input ungated.
- A separate free input allowance on the output allowance's wait curve.
- One combined budget, `max_length + input / k`, against a single allowance.

## Decision Outcome

Chosen option: "A separate free input allowance on the output allowance's wait curve".

The input allowance is `HORDE_TEXT_FREE_INPUT_CEILING` (16384 tokens) when the request's estimated wait is at or below
`HORDE_TEXT_FREE_OUTPUT_WAIT_FULL_SECONDS` (60 s). It falls linearly to `HORDE_TEXT_FREE_INPUT_FLOOR` (4096 tokens)
at `HORDE_TEXT_FREE_OUTPUT_WAIT_FLOOR_SECONDS` (300 s) and stays at the floor beyond that. A request with no eligible
worker gets the floor. The wait is the one defined by ADR 0040, and the wait thresholds are the output allowance's.

16384 is the default context of current koboldcpp releases, so an idle fleet serves a default client's full context
free. 4096 keeps a short conversation free on a backed-up queue.

The input measured against the allowance is `min(estimated_prompt_tokens, max_context_length - max_length)`. A worker
cuts the front of a prompt away until the prompt and `max_length` fit the context, so a long prompt sent with a small
context costs only the context. The estimate is the same one used by context sizing, taken after any style template
and any context growth.

A request above the input allowance needs kudos upfront in the same way as a request above the output allowance. The
input allowance determines whether a hold is needed. It does not change the amount held, which is computed from
`max_length` and the requested models as for any text request, or the charge at completion. The anonymous account
cannot hold kudos, so for anonymous use the allowance is a hard bound.

On the native text route, a client that set `allow_downgrade` and sends its prompt under `context_fit: ignore` gets
`max_context_length` lowered to the input allowance plus `max_length`, and the worker cuts the front of the prompt
away. That client already accepts this cut. A downgrade for the output allowance alone lowers the context in the
same way, since a lower `max_length` would otherwise leave more of the context to the prompt. Under `reject` or
`grow`, the client asked the Horde not to cut its prompt, so a request over the input allowance is refused. A chat request is never downgraded, as ADR 0037 records.

`horde.text.free_allowance.decisions` gains `horde.prompt_bucket`, the processed prompt tokens before any downgrade,
and `horde.exceeded`, which lists the exceeded allowances (`none`, `output`, `input`, `both`).

### Consequences

- An idle model serves up to 16384 prompt tokens free. A model with a five-minute backlog serves 4096.
- A long-context free request needs kudos, or under `ignore` with `allow_downgrade` a smaller context.
- A registered user whose balance covers the amount held can send a prompt above the input allowance. Neither the
  hold nor the charge grows much with prompt length, so such a request costs about as much as one with a short
  prompt.
- A dry run of a request whose processed prompt is above the input floor does not use the quote cache, as for a
  request above the output floor.
- The wait counts queued output only, so a queue of long-prompt requests looks shorter than it is until the workers'
  measured speed falls. Both allowances stay generous longer than they should under such a queue.

## Pros and Cons of the Options

### Leave input ungated

- Good, because it changes nothing.
- Bad, because an anonymous user can queue unbounded input on any worker that declares a large context.

### A separate free input allowance on the output allowance's wait curve

- Good, because each figure can be explained separately in a refusal message.
- Good, because it needs no input-to-output ratio.
- Good, because it reuses the output allowance's wait and thresholds, so both allowances respond to the same load.
- Bad, because a request just under both allowances uses more worker time than either figure shows.

### One combined budget

- Good, because it charges input and output against one measure of worker time.
- Bad, because it needs a cost ratio `k` between input and output.
- Bad, because a refusal has to explain a derived figure rather than a token count set by the client.

## Confirmation

`tests/unit/test_kobold_text_wp.py` pins the input allowance at the thresholds and beyond them, the floor without an
eligible worker, the configured input ceiling and floor, a long prompt measured at its context, the refusal message,
a funded long prompt, the downgrade under `ignore`, the refusal under `grow` and the decision attributes.
`docs/reference/chat_requests.md` records the public rule under "Free output and input allowances".

## More Information

[ADR 0037](0037-size-the-free-output-allowance-by-the-estimated-wait.md) defines the output allowance and its wait
curve, which this record extends to input. [ADR 0040](0040-estimate-text-queue-wait-from-the-eligible-workers.md)
defines the wait estimate.

For the current implementation's [free allowance settings](../reference/service_limits.md#free-output-and-image-work),
see the constants reference.
