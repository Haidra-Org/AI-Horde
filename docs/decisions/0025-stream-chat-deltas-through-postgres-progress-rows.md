---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Stream chat deltas through progress rows in Postgres

## Context and Problem Statement

The same question as [ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md): where do a worker's
delta batches go. This record proposes the primary database.

## Decision Drivers

- Postgres already holds every other fact about a request, and the cleanup thread already prunes request state.
- A Redis failover must not lose in-flight output.
- The progress route is already a database write for the cancel check and the job TTL refresh.

## Considered Options

- Progress rows in Postgres, one per batch, keyed by generation and sequence
- A Redis stream per request
- Pub/sub only

## Decision Outcome

Chosen option: "Progress rows in Postgres, one per batch, keyed by generation and sequence".

The progress route inserts batches with a unique index on generation id and sequence. Identical repeats return the
accepted sequence without a new row; changed content is rejected. Reject gaps with the expected sequence. Entries
include stable choice and attempt identity. The front reads through a bounded native route; the design does not
introduce a direct database connection or per-batch broadcast signal traffic.
Retention and row/byte limits cover the bounded active stream lifetime and cleanup grace.

### Consequences

- Good: One store; survives a Redis failover; durable for audit.
- Bad: One insert per batch per in-flight stream on the primary database, on the same connection pool as pops and
  submits; at one batch per second per stream this is the dominant write load under concurrency.
- Bad: The front gains native HTTP read traffic for deltas. It retains the no-database-connection boundary of
  [ADR 0023](0023-run-the-chat-front-as-a-separate-asgi-process-woken-by-redis.md).

## Pros and Cons of the Options

### Postgres progress rows

- Good: Durable and queryable.
- Bad: Write amplification on the primary database.

### Redis stream per request

- Good: Cheap and ordered.
- Bad: In-flight output lost on failover.

### Pub/sub only

- Good: Does not need storage.
- Bad: Lossy.

## More Information

This record and [ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md) are mutually exclusive.
Accepting one rejects the other. The review recommends ADR 0024; the measurement that decides is the write rate the
primary database absorbs at the expected number of concurrent streams.

This is deferred until after the buffered-SSE text release. Replay, attempt-fencing and partial-output rules are the same as ADR 0024:
the front does not replay the whole result after exposure or splice a replacement attempt, and native cancellation
settlement is unchanged.
A unique index alone is not sufficient acknowledgment/replay semantics.
