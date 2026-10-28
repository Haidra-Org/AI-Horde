---
status: rejected
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Stream chat deltas through progress rows in Postgres

## Context and Problem Statement

A streaming chat worker posts batches of generated text while it generates, and the front forwards them to the client
in order. This record evaluates the primary database as the store for those batches.
[ADR 0024](0024-stream-chat-deltas-through-a-retained-redis-stream.md) is the accepted decision on this question.

## Decision Drivers

- Postgres holds every other fact about a request, and the cleanup thread prunes request state.
- A Redis failover must not lose output in flight.

## Considered Options

- Progress rows in Postgres, one per batch, keyed by generation and sequence
- A Redis stream per request
- Pub/sub only

## Decision Outcome

Rejected. The evaluated option has the progress route insert each batch as a row with a unique index on generation
id and sequence. An identical repeat returns the accepted sequence without a new row, changed content is refused, and
a gap is refused with the expected sequence. The front reads the rows through a bounded native route, since it does
not hold a database connection. Retention and row and byte limits cover the active stream and a cleanup grace.

### Consequences

- Good: One store, durable for audit, and a Redis failover does not lose output.
- Bad: One insert per batch per stream in flight on the primary database, on the connection pool pops and submits
  use. At one batch per second per stream this is the dominant write load under concurrency.
- Bad: The front gains native HTTP read traffic for deltas.

## Pros and Cons of the Options

### Postgres progress rows

- Good: Durable and queryable.
- Bad: Write amplification on the primary database.

### Redis stream per request

- Good: Cheap and ordered, with native trimming and expiry.
- Bad: Output in flight can be lost on failover.

### Pub/sub only

- Good: Does not need storage.
- Bad: A lost message is lost output.
