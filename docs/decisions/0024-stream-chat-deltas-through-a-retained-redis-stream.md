---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Stream chat deltas through a retained Redis stream per request

## Context and Problem Statement

OpenAI clients expect tokens as they are generated. The Horde's submit is one-shot: a worker reports a finished reply.
A worker such as koboldcpp can read its local token stream and post batches while it generates. Where do those batches
go, so that the front can forward them in order, detect a lost batch, and avoid splicing a reissued job into a reply
the client already holds part of?

## Decision Drivers

- Ordering and gap detection per generation, with the worker's sequence number as the authority.
- Retention long enough for the front to catch up after a stall, and short enough that idle streams expire.
- The completion signal channel, which every front process receives, must not carry per-token traffic
  ([ADR 0023](0023-run-the-chat-front-as-a-separate-asgi-process-woken-by-redis.md)).
- The terminal outcome stays in Postgres regardless of the stream.
- A progress post should not add a Postgres write to the hot path.

## Considered Options

- One Redis stream per request, appended by a native progress route, read by the front with `XREAD`
- Progress rows in Postgres appended by the native progress route
- Pub/sub only, without retention

## Decision Outcome

Chosen option: "One Redis stream per request, appended by a native progress route, read by the front with `XREAD`".

A request sent with `stream: true` stores the stream hint, `stream_progress`, in `chat_extras`, and its job payload
carries it. The front sends `stream: true` only for a request whose text it can relay as it arrives: one without
tools and without a structured response format, on a front configured with Redis. At pop the Horde writes a lease,
`horde:chat:lease:<generation id>`, holding the request, worker, model, owner, a digest of the owner's key and the byte
cap. The worker posts each batch to `POST /v2/generate/text/progress` with the generation id and a sequence number
starting at 1, and still submits the whole reply to the submit route.

The route authenticates the post from the lease alone and does not read Postgres. One Lua script
(`horde/chat_progress.lua`) checks and appends the batch atomically. A repeat of the last accepted sequence with the
same content is acknowledged without a second entry. A sequence that is not the next one, or a repeat with changed
content, is refused with 409 `ChatProgressOutOfSequence` and the expected sequence. A generation is capped at 1000
entries and 16 bytes per token of its `max_length`. An accepted batch is appended to `horde:chat:stream:<request id>`
as an entry carrying the generation id, the sequence, the content, any reasoning content, and the worker and model from
the lease. The stream lives on the chat signal database, expires 900 seconds after its last append and is trimmed to
about 4000 entries. When a generation faults, its stream gets an `ended` marker and its lease is deleted. A progress
post can also renew the job lease, which is the only case in which it writes Postgres
([ADR 0038](0038-lease-text-jobs-for-a-fixed-term-renewed-by-progress.md)).

The front reads every registered request's stream with one `XREAD` per process. The first entry with sequence 1 pins
its generation, and the first chunk sent to the client exposes the stream. After exposure, an entry from another
generation, the pinned generation's `ended` marker, or a sequence out of order ends the client's stream with the SSE
error `ChatStreamDiverged`, and the front cancels the request. The final reply completes the stream when it starts
with the relayed text, and ends it with the same error when it does not. Before exposure, the front drops such entries
and can deliver the reply whole.

Each progress acknowledgement carries the request's cancellation state. When a streamed request is cancelled, the
cancel route settles each generation still in flight on the output its worker had posted, read from the stream, with
a floor of 20 tokens: the requester is charged and the worker is paid for that output. It then flags each lease as
cancelled and appends the `ended` marker. The worker's next post is acknowledged with `cancelled: true`, and the worker
stops generating and does not submit. The front cancels the request when its client disconnects, so a disconnect
during a streamed reply is settled the same way.

### Consequences

- Good: Ordered and retained, with trimming and expiry handled by Redis; a stalled reader catches up.
- Good: The signal channel stays small, and progress posts do not load Postgres.
- Good: A worker is paid for streamed output the client received before cancelling, and the requester pays for it.
- Bad: Redis holds state, and a failover can lose deltas. Before exposure the front delivers the reply whole; after
  exposure a lost stream ends with the final reply's reconciliation or an error, and the front does not replay the
  whole reply.
- Bad: A cancelled streamed generation is charged for posted output, which differs from a text request, where a
  generation still in flight at the cancel is settled when its worker submits.
- Bad: A requester whose client disconnects before any output is posted is still charged the 20-token floor for each
  generation in flight.

## Pros and Cons of the Options

### Redis stream per request

- Good: Ordered append, blocking reads, trimming and expiry are native operations.
- Bad: Redis is the Horde's cache, and a stream is state.

### Postgres progress rows

- Good: One store, and the cleanup thread already prunes request state.
- Good: Survives a Redis failover and is durable for audit.
- Bad: One insert per batch per stream in flight on the primary database, on the connection pool pops and submits use.
- Bad: The front gains native HTTP read traffic for deltas, since it does not hold a database connection.

### Pub/sub only

- Good: Does not store state.
- Bad: A lost message is lost output, and tool-call arguments cannot tolerate it.

## Confirmation

`tests/integration/test_chat_progress.py` pins the stream hint and lease at pop, the refusal of a post without a lease,
in-sequence appends without a signal, the gap refusal with the expected sequence, the end of a stream on fault, the
cancel reaching the worker through the next acknowledgement, and the settlement of a cancelled streamed generation on
its posted output and on the 20-token floor. `tests/unit/test_chat_progress.py` pins the lease contents and the fault
marker. `tests/unit/oai/test_delta_relay.py` pins the front's relay, the error and cancel after a lost attempt, and the
reconciliation with the final reply. `docs/reference/chat_requests.md` records the progress route and the pricing of a
cancelled streamed generation, and `docs/reference/chat_front.md` the relay.

## More Information

[ADR 0025](0025-stream-chat-deltas-through-postgres-progress-rows.md) records the Postgres progress rows option as a
rejected alternative.
[ADR 0038, Lease text jobs for a fixed term renewed by progress](0038-lease-text-jobs-for-a-fixed-term-renewed-by-progress.md)
records the job lease and its renewal by progress posts.
