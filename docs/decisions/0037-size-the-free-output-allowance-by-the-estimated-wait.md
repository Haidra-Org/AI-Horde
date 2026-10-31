---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Size the free output allowance by the request's estimated wait

## Context and Problem Statement

A text request from a requester who does not have kudos (the anonymous account, or a registered account with an
empty balance) is served without a kudos hold up to a number of output tokens called the free output allowance. Above it the request
is refused with `KudosUpfront`, or lowered to the allowance when the client allows a downgrade. The allowance decides
what a casual user can ask for without earning kudos first, and how much free work the fleet accepts when it is busy.

The allowance was computed from horde-wide totals: 512 tokens plus 5 per active text thread, minus 0.9 per queued text
request, clamped between 256 and 512. Current instruction and reasoning models routinely need more than 512 tokens for
one answer, and a horde-wide figure says nothing about the workers that would actually serve the request. A queue of
twenty requests on one slow model lowered the allowance for a request to an idle model, and an idle fleet never
offered more than 512.

How much output should a request without kudos be allowed, and what should the figure respond to?

## Decision Drivers

- The allowance should be generous when the workers that can serve the request are idle and fall as they become busy.
- A capable worker that clears its queue quickly should keep its users at the full allowance even while jobs wait.
- The figure must be cheap to compute on the request creation path.
- The same rule must apply to plain text requests and chat requests, which share the text queue (ADR 0019).
- The rule must be explainable in one sentence in a refusal message.

## Considered Options

- Horde-wide queue count, clamped to a small range (the previous rule).
- Competing jobs per eligible thread, falling linearly from a ceiling to a floor.
- Estimated wait on the request's eligible workers, falling linearly from a ceiling to a floor.

## Decision Outcome

Chosen option: "Estimated wait on the request's eligible workers".

The allowance is `HORDE_TEXT_FREE_OUTPUT_CEILING` (4096 tokens) when the request's estimated wait is at or below
`HORDE_TEXT_FREE_OUTPUT_WAIT_FULL_SECONDS` (60 s). It falls linearly to `HORDE_TEXT_FREE_OUTPUT_FLOOR` (512 tokens)
at `HORDE_TEXT_FREE_OUTPUT_WAIT_FLOOR_SECONDS` (300 s) and stays at the floor beyond that. A request with no eligible
worker gets the floor.

The estimated wait is the one ADR 0040 defines: the queued output ahead of the request that its eligible workers could
take, divided by the combined measured speed of those workers. It is the same figure the status route reports as
`wait_time`, without the request's own output.

The allowance is measured once, when the request is created, from the workers eligible at the requested `max_length`.
A request over the allowance is refused with `KudosUpfront` and a message stating the allowance in tokens. On the
native text route a client that set `allow_downgrade` gets the request lowered to the allowance instead, with slow
workers allowed. A chat request is never downgraded, since an OpenAI client cannot see that its `max_tokens` changed.
A dry run over the allowance is refused the same way, so a quote never describes a request that would be refused.
A request that disallows slow workers always holds its kudos, whatever the allowance.

The ceiling, the floor and both wait thresholds are deployment settings in `horde/config.py`.

The allowance's effect is exported from every non-dry-run text and chat request creation.
`horde.text.free_allowance.decisions` counts each decision by requester (`anonymous`, `registered`, `shared_key`),
outcome (`free`, `funded`, `refused`, `downgraded`), requested and allowance size bucket (`<=512`, `513-1024`,
`1025-2048`, `2049-4096`, `>4096`) and whether the request is a chat request.
`horde.text.free_allowance.estimated_wait_seconds` records the wait the allowance was sized by, and
`horde.text.queue_wait_seconds` records each assignment's time from request creation to pop, so the ceiling and floor
can be judged against the waits requesters see.

### Consequences

- An idle model serves up to 4096 tokens to any user. A model with a five-minute backlog serves 512.
- Fast workers raise the allowance for their models by shortening the wait, which rewards capacity where it is.
- The allowance for one model is not affected by load on another.
- Requests up to 16384 tokens are accepted on the text routes; everything above the allowance needs kudos, and a
  worker still bounds what it serves through its advertised `max_length`.
- Clients cannot know the allowance before sending; the refusal states it, and a dry run returns the same refusal.

## Pros and Cons of the Options

### Horde-wide queue count, clamped to a small range

- Good, because it is one subtraction on cached totals.
- Bad, because a backlog on one model lowers the allowance for every model.
- Bad, because the clamp caps the allowance at 512 however idle the fleet is.

### Competing jobs per eligible thread

- Good, because it looks only at the workers that could serve the request.
- Bad, because a count of jobs ignores how fast those workers clear them; two queued jobs on a fast worker and two on
  a slow one give the same allowance.
- Bad, because with most models served by one or two workers, a slope that reaches the floor at a few jobs per thread
  keeps nearly every popular model at the floor.

### Estimated wait on the request's eligible workers

- Good, because it measures what the user experiences: time until their request starts.
- Good, because it reuses the status estimate, so the allowance and `wait_time` agree.
- Bad, because the estimate depends on measured worker speed, which a new worker lacks until it completes jobs; such a
  worker is counted at an assumed 10 tokens per second.

## Confirmation

`tests/unit/test_kobold_text_wp.py` pins the curve at the thresholds, between them and beyond them, the floor without an
eligible worker, the configured thresholds, the upfront decision, each allowance outcome counted once with its
attributes and the queue wait recorded at assignment. `tests/unit/test_text_worker_eligibility.py` pins
the competing work and the eligible speed the estimate reads. `tests/integration/test_chat.py` pins that a quote cached
for a funded requester does not answer an anonymous dry run above the allowance on the text route; chat dry runs are
not answered from the quote cache, since no chat context fit is `ignore`.
`docs/reference/chat_requests.md` records the public rule under "Free output allowance" and
`docs/explanation/kudos_accounting.md` explains it.

## More Information

ADR 0040 defines the wait estimate this rule reads. ADR 0035 records that anonymous access to the chat front stays and
depends on this allowance.
