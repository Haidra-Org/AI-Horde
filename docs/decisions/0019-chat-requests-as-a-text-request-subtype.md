---
status: accepted
date: 2026-10-28
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
`horde/classes/kobold/processing_generation.py`. Chat needs the messages carried to the worker and the structured
reply, usage and finish reason carried back, with the existing queue and accounting reused. Where should a chat
request live?

## Decision Drivers

- Queue position, kudos, upfront kudos, concurrency limits, statistics, countermeasures and moderation must apply to
  chat as they apply to text, without a second implementation.
- Workers that do not declare chat must keep receiving text jobs and must not receive a job without a prompt.
- The pop scan loads every candidate row, and chat payloads can be large.
- The two route families must not be confusable: a chat id on a text route is not found, and the reverse.

## Considered Options

- A `TextWaitingPrompt` with a nullable `messages` column, chat told apart by a discriminator
- A separate polymorphic request class and table for chat
- A chat request translated into a prompt string at submit, without a native chat type

## Decision Outcome

Chosen option: "A `TextWaitingPrompt` with a nullable `messages` column, chat told apart by a discriminator".

`POST /v2/generate/chat/async` creates a `TextWaitingPrompt` whose `messages` column holds the OpenAI array and whose
`prompt` column holds a flattened rendering of the conversation. The flattened prompt feeds the token estimate,
kudos, moderation and the request's record of what was submitted; workers receive the messages. A boolean `is_chat`
column is the discriminator. Because chat-ness reads that column, `messages` and `chat_extras` (tools, tool choice,
response format and the stream hint) are deferred and loaded only for the request a worker is assigned.

Workers opt in to chat on pop ([ADR 0022](0022-declare-chat-capabilities-by-pop-fields.md)). The pop query admits a
chat row only to a worker that declared chat for the request's model, and a text row only to a worker that still
accepts text jobs. The chat status route, `GET /v2/generate/chat/status/<id>`, and the text status and request routes
read the request through `get_text_wp_by_id` with the family they serve, so an id from the other family is not found.

A chat generation's structured reply is written in the same update as its `generation` string
(`TextProcessingGeneration.get_submission_columns`), so the text lifecycle, which reads `generation`, sees a chat
generation finish at the same moment the chat status does. An empty reply is a completed generation.

A chat request is priced as a text request ([ADR 0027](0027-price-chat-generations-by-the-text-output-estimate.md)).
When a streamed chat request is cancelled, each generation still in flight is settled on the output its worker had
posted through the progress route, with a floor of 20 tokens: the requester is charged and the worker is paid for
that output ([ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md)).

### Consequences

- Good: One queue, one price, one set of limits and countermeasures for both families.
- Good: Each added column is nullable or has a constant default, so the migration does not rewrite a table.
- Bad: The schema grows by 34 columns: 12 on `waiting_prompts`, 9 on `workers` and 13 on `processing_gens`
  (`sql_statements/5.1.15.txt`). The migration also adds three indexes: the partial unique index on
  `(user_id, operation_key)` for [ADR 0026](0026-classify-client-retries-and-recover-submits-by-operation-key.md), and
  `worker_id` indexes on `worker_blacklists` and `text_worker_softprompts` for the eligibility query.
- Bad: A chat request carries a prompt column that is a rendering of the messages, and every validation rewrite of
  that prompt has to be mirrored onto the messages.
- Bad: Text-only tooling that reads `prompt` sees a flattened conversation.
- Bad: The text pop query branches on the worker's chat declaration, so a change to text eligibility has to be
  checked against both branches.

## Pros and Cons of the Options

### A `TextWaitingPrompt` with a nullable `messages` column

- Good: Reuses the text queue and accounting without a second implementation.
- Bad: Every reader of `generation`, the pending-generation queries, statistics, moderation projections and cleanup
  have to handle a structured reply beside the string.
- Bad: The flattened prompt is a second representation that must be kept in step with the messages.

### A separate polymorphic request class and table

- Good: Does not need a flattened prompt; the chat shape is the only shape.
- Bad: Every text behaviour that reads `TextWaitingPrompt` needs a second branch or a shared base.

### Translation to a prompt string at submit

- Good: Does not need a native change. A standalone OpenAI-to-Horde proxy works this way.
- Bad: The server owns per-model chat templates and their maintenance, the worker's templating is bypassed, and tools,
  images and reported usage are not available.

## Confirmation

`tests/integration/test_chat.py` pins the structured reply written with the generation and charged as text, the empty
reply, the family filter on the status routes, the chat-only and text-only pop branches, and the cancel event.
`tests/integration/test_chat_migration.py` pins the migration's columns and their defaults on existing rows.
`tests/integration/test_chat_progress.py` pins the settlement of a cancelled streamed generation on its posted output
and on the 20-token floor. `docs/reference/chat_requests.md` records the routes, the schema and the pricing, and
`docs/explanation/chat_requests.md` explains the design.

## More Information

The text path changes made beside this decision are recorded separately:

- [ADR 0037, Size the free output allowance by the request's estimated wait](0037-size-the-free-output-allowance-by-the-estimated-wait.md)
- [ADR 0038, Lease text jobs for a fixed term renewed by progress](0038-lease-text-jobs-for-a-fixed-term-renewed-by-progress.md)
- [ADR 0039, Settle requests after cancellation and repeated faults](0039-settle-requests-after-cancellation-and-repeated-faults.md)
- [ADR 0040, Estimate text queue wait from the request's eligible workers](0040-estimate-text-queue-wait-from-the-eligible-workers.md)
