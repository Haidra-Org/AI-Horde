---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Run the chat front as a separate ASGI process woken by a Redis signal

## Context and Problem Statement

An OpenAI chat call holds one HTTP connection open until the completion exists. A Horde request can wait minutes
for a worker. The Flask application runs under waitress with 45 threads per instance shared with worker pops and
submits (`horde/argparser.py`). Where does the waiting happen, and how does the waiter learn that a request finished?

## Decision Drivers

- Thousands of open waits must not consume request threads of the Horde instances.
- The public status route is limited to 60 reads per minute per address and path (`horde/apis/limiter_api.py`).
- The outcome must never depend on a notification arriving; it lives in Postgres.
- The front must not need a database connection or the Flask stack.

## Considered Options

- A separate ASGI process that submits over HTTP, subscribes to a Redis channel per request and polls as a fallback
- The same, polling the status route only
- Handlers inside the Flask application that wait synchronously

## Decision Outcome

Chosen option: "A separate ASGI process that submits over HTTP, subscribes to a Redis channel per request and polls
as a fallback".

The front is a separate process, proposed as the `horde/oai` package, running under uvicorn with one event loop. It
submits to the native chat route, holds one pattern subscription to `horde:chat:*` on the primary Redis, and fetches
the status once on each wake. The native side publishes an event on each generation and on every terminal outcome;
publishing is best effort. A stretched poll covers a lost message. The front does not hold a database connection.

### Consequences

- Good: A wait costs the front a coroutine and does not cost the Horde anything until the status read.
- Good: Notifications avoid waiting until the next ordinary poll after completion.
- Bad: Polling remains active while subscribed; status load includes initial/final reads, fallback polls and bounded
  signal-settle retries. It is not a fixed two reads per request.
- Bad: A new process to deploy, drain and monitor; a request is pinned to the process that accepted it.
- Bad: Every front process receives every chat event; the signal channel must carry only terminal and per-generation
  events and must not carry tokens.

## Pros and Cons of the Options

### Separate ASGI process with signal and fallback poll

- Good: Scales waiting without occupying a native thread for each open connection.
- Bad: Status, submit and cancel HTTP calls still consume the native thread pool and need an aggregate budget.
- Bad: One more moving part, and Redis pub/sub is a new dependency pattern for the repository.

### Separate ASGI process polling only

- Good: Does not depend on Redis.
- Bad: Status load scales with open waits divided by the poll interval. The limiter includes the concrete request
  path, so it is not one shared 60/minute budget across all requests; the passkey preserves attribution.

### Waiting inside Flask

- Good: Does not add a process to deploy.
- Bad: Each wait pins a waitress thread shared with pops and submits; a few hundred waits starve the workers.

## More Information

The implementation follows the chosen option. The native cache in `horde/horde_redis.py` fans writes out to
every configured server; chat signal and stream keys live only on the primary.

Correctness requirements: terminal status comes from committed database state or an explicitly fresh cache; publishing
after commit alone does not invalidate caches. Waits, native-call acquisition, response writes and shutdown have
finite budgets, including streamed requests. Native cancellation is best effort and preserves text accounting.
Crashes may leave charged work uncollected; durable delivery recovery is outside the first release's scope.
