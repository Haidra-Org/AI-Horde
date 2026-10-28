---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Run the chat front as a separate ASGI process woken by a Redis signal

## Context and Problem Statement

An OpenAI chat call holds one HTTP connection open until the completion exists. A Horde request can wait minutes
for a worker. The Flask application runs under waitress with 45 threads per instance by default, shared with worker
pops and submits (`horde/argparser.py`). Where does the waiting happen, and how does the waiter learn that a request
finished?

## Decision Drivers

- Thousands of open waits must not consume request threads of the Horde instances.
- The status route is limited to 60 reads per minute per address, method and path (`horde/apis/v2/kobold.py`,
  `horde/apis/limiter_api.py`, `get_request_path`).
- The outcome must not depend on a notification arriving; it lives in Postgres.
- The front must not need a database connection or the Flask stack.

## Considered Options

- A separate ASGI process that submits over HTTP, holds one Redis pattern subscription per process and polls as a
  fallback
- The same, polling the status route only
- Handlers inside the Flask application that wait synchronously

## Decision Outcome

Chosen option: "A separate ASGI process that submits over HTTP, holds one Redis pattern subscription per process and
polls as a fallback".

The front is the `horde/oai` package, run as `python -m horde.oai` under uvicorn with one event loop. It submits to
`POST /v2/generate/chat/async` and holds one pattern subscription to `horde:chat:*` (`horde/oai/waiter.py`). The
native side publishes on `horde:chat:<request id>` (`horde/chat_signal.py`) when a generation lands, when the request
is done, and when it is cancelled or faulted. A message carries the event and a generation count, and does not carry
generated text. On each wake the front reads the status once. Publishing is best effort, and a poll stretched by the
wait estimate covers a lost message. The signal channel uses logical database 7 on the Redis host the cache treats as
primary; the native cache in `horde/horde_redis.py` fans writes out to every configured server, and the chat signal
and stream keys are kept only on that host.

Outside `horde.oai`, the front imports only `horde.chat_contract`, `horde.consts`, `horde.metrics` and
`horde.telemetry`, and it does not hold a database connection. Its native calls go through one HTTP client with a connection limit (`HTTP_MAX_CONNECTIONS`) and a
separate bound on concurrent submits (`SUBMIT_MAX_CONCURRENT_CALLS`) in `horde/oai/horde_client.py`. When the front
holds a proxy passkey it forwards the client's address in `Proxied-For`, so native limits and countermeasures apply to
the client's address. Chat submits have their own per-address limits,
`HORDE_CHAT_REQUEST_2SEC_LIMIT_PER_IP` and `HORDE_CHAT_REQUEST_90MIN_LIMIT_PER_IP`
(`get_chat_request_2sec_limit_per_ip` and `get_chat_request_90min_limit_per_ip`), which default to the text submit
limits and are counted apart from them.

Waits, native calls, response writes and shutdown are bounded. The front cancels a request on client disconnect, on
its deadline and at shutdown.

### Consequences

- Good: A wait costs the front a coroutine and does not cost the Horde anything until the status read.
- Good: A notification ends a wait without waiting for the next poll.
- Bad: Polling continues while subscribed. Status load is the initial and final reads, the fallback polls and a
  bounded number of rechecks after a signal.
- Bad: A process to deploy, drain and monitor, and a request is pinned to the process that accepted it.
- Bad: Every front process receives every chat event, so the channel carries only per-generation and terminal events.
- Bad: A front process that exits without its shutdown drain leaves its requests to run and be charged, with no
  client to deliver them to.

## Pros and Cons of the Options

### Separate ASGI process with signal and fallback poll

- Good: Scales waiting without occupying a native thread for each open connection.
- Bad: Status, submit and cancel calls still use the native thread pool and need an aggregate budget.
- Bad: Adds Redis pub/sub to the deployment's dependencies.

### Separate ASGI process polling only

- Good: Does not depend on Redis.
- Bad: Status load scales with open waits divided by the poll interval, and a finished request waits for the next
  poll.

### Waiting inside Flask

- Good: Does not add a process to deploy.
- Bad: Each wait pins a waitress thread shared with pops and submits; a few hundred waits starve the workers.

## Confirmation

`tests/integration/test_chat.py` pins the signal published on completion, on cancel and after an expired request's
row is gone. `tests/unit/oai/test_lifecycle.py` pins the subscription's liveness pings and resubscription and the
bounded shutdown that cancels open requests. `tests/unit/oai/test_poll_cadence.py` pins the stretched fallback poll.
`tests/unit/test_chat_rate_limit.py` pins the chat submit limits apart from the text limits.
`docs/reference/chat_requests.md` records the completion signal and the rate limits, `docs/reference/chat_front.md`
the front, and `docs/how-to/run_openai_chat_front.md` its deployment.
