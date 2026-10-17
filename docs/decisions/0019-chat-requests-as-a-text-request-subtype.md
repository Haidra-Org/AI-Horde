---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Carry chat requests as a text request subtype

## Context and Problem Statement

An OpenAI chat request is a `messages` array with sampling fields. The native text request is a prompt string with
the same sampling fields, queued in `waiting_prompts`, priced by `max_length`, matched to workers by
`horde/database/text_functions.py`, `get_sorted_text_wp_filtered_to_worker`, and settled by
`horde/classes/kobold/processing_generation.py`. Chat needs the messages carried to the worker, usage and finish
reason carried back, with the existing queue and accounting reused. Where should a chat request live?

## Decision Drivers

- Queue position, kudos, upfront kudos, concurrency limits, statistics, countermeasures and moderation must apply to
  chat exactly as they apply to text, without a second implementation.
- Workers that predate chat must keep receiving text jobs and must never receive a job without a prompt.
- The pop scan loads every candidate row; chat payloads can be large.
- The two route families must not be confusable: a chat id on a text route is not found, and the reverse.

## Considered Options

- A `TextWaitingPrompt` with a nullable `messages` column, chat told apart by a discriminator
- A separate polymorphic request class and table for chat
- A chat request translated into a prompt string at submit, without a native chat type

## Decision Outcome

Chosen option: "A `TextWaitingPrompt` with a nullable `messages` column, chat told apart by a discriminator".

The chat routes under `/v2/generate/chat` create a `TextWaitingPrompt` whose `messages` column holds the OpenAI
array and whose `prompt` column holds a flattened estimate used only by the token estimate, kudos and moderation. A
boolean `is_chat` column is the discriminator, so `messages` and the sibling column for tools and response format can
be deferred and are loaded only for the request a worker is assigned. Workers opt in on pop; the queue filter admits a
chat request only to a worker that opted in. The status and request routes of each family filter on the
discriminator, so an id from the other family is not found.

### Consequences

- Good: One queue, one price, one set of limits and countermeasures; the text path is undisturbed.
- Good: The schema change is three nullable columns and a boolean.
- Bad: A chat request carries a prompt column that is an estimate, and every validation rewrite of that prompt has
  to be mirrored onto the messages.
- Bad: Text-only tooling that reads `prompt` sees a flattened conversation.

## Pros and Cons of the Options

### A `TextWaitingPrompt` with a nullable `messages` column

- Good: Reuses the text queue and accounting without a second implementation.
- Bad: String-based completion predicates, pending-generation queries, statistics, moderation projections and
  cleanup require an explicit audit; a structured message alone does not satisfy the existing lifecycle.
- Bad: The estimate prompt is a second representation that must be kept in step.

### A separate polymorphic request class and table

- Good: Does not need an estimate prompt; the chat shape is the only shape.
- Bad: Every text behaviour that reads `TextWaitingPrompt` needs a second branch or a shared base.

### Translation to a prompt string at submit

- Good: Does not need a native change; this is how the pilot proxy works.
- Bad: The server owns per-model chat templates and their maintenance; tools, images and usage are not available;
  the worker's templating is bypassed.

## More Information

The draft implementation follows the chosen option. The discriminator column is the amendment the draft needs
before `messages` can be deferred, since the chat-ness property reads the column.

First-release scope: one text-only choice and one requested model. Persist the structured message and its compatible string
generation in the same completion transition. Empty string is a completed generation; tool-only messages need
an explicit successful structured-output representation. Add tools/response-format storage with that feature; it is
not a prerequisite for text chat. Native text cancellation economics remain unchanged.
