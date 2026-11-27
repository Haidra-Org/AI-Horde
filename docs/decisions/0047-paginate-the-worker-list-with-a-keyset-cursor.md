---
status: proposed
date: 2026-11-27
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# List every registered worker in pages with a keyset cursor

`GET /v2/workers/page` lists every registered worker, active or inactive, in bounded pages with a keyset cursor.
`GET /v2/workers/mine` pages through the caller's own workers the same way. Both read a read model that background jobs
build off the request path: a narrow indexed table for the order, the filters and the total, and the finished JSON
row of every worker in Redis. `GET /v2/workers` keeps returning the active workers in one bare array, and its API
description points to `GET /v2/workers/page` for the rest.

## Context and Problem Statement

`GET /v2/workers` returns only the active workers, which are the workers that checked in within the last 5 minutes. A
worker that stops checking in drops out of every list. It can still be read one at a time through
`GET /v2/workers/{id}` or `GET /v2/workers/name/{name}`, and only by a caller who already has its ID or name.

The active list is small. Usually a few dozen workers are active at once, so the full list stays cheap to build and
send. The registered workers include every worker that has not been deleted, active or not, and can outnumber the
active ones many times over. They are not listed by any endpoint.

Callers want different things from the registered workers. People using a front end want to browse them and find
workers by model, team or part of a name. An owner wants every worker they run, including offline workers and the
fields shown only to owners. An operator wants to see how many requests read each list.

A worker's details row is built in Python by `WorkerTemplate.get_details`, which reads the owner, the owner's roles,
the team, the messages and the advertised models. Building rows for a page of 100 workers on each request costs far
more than reading them.

## Decision Drivers

- Existing clients of `GET /v2/workers` keep working without a change.
- Every registered worker can be reached by walking a list, without knowing its ID or name first.
- The cost of a page request does not grow with the number of registered workers or with the page size, and a page
  does not load or build any worker on the request path.
- Workers joining or leaving between two pages do not shift the rest of a walk.
- A filter does not reveal anything hidden from the caller's view, including through a count.
- The moderator and owner views stay out of every shared cache.
- Total page traffic has a ceiling, however many client IP addresses send it.
- Operators can see how many requests read each list, with metric labels drawn from fixed sets.

## Considered Options

- Page a read model built off the request path, with a keyset cursor, on a sibling endpoint
- Page the `workers` table on each request and load the page's workers through the ORM
- Page the cached active-worker snapshot with a keyset cursor
- Add inactive workers to `GET /v2/workers`
- Add `limit` and `offset` to `GET /v2/workers`

## Decision Outcome

Chosen option: "Page a read model built off the request path, with a keyset cursor, on a sibling endpoint".

**A sibling endpoint.** `GET /v2/workers` keeps its bare array of active workers, its filters and its privilege rules.
Its API description states that it lists only active workers and that `GET /v2/workers/page` lists every registered
worker. The paged list returns `{workers, next_cursor, total}`. The moderation listings already use `next_cursor` for
their cursor field. Each entry in `workers` has the same fields as the response of `GET /v2/workers/{id}` for that
worker. As on that route, the fields depend on the caller's key.

**A read model for order and filters.** The table `worker_list_entries` holds one narrow row per registered worker:
its type, owner, team, lower-cased name, uptime, completed jobs and kudos. Each order has an index on its column and
the worker ID. A job on the quorum node copies changed values from `workers` into the table every 60 seconds and
deletes the entries of deleted workers. The model filter reads `worker_models`, the owner filter reads
`users.public_workers`, and the online filter reads the set of active worker IDs that the worker list job keeps in
Redis.

**Stored rows.** For every registered worker, the row store in Redis holds the finished JSON bytes of its details row
in each view (public, moderator and owner), marshalled as `GET /v2/workers/{id}` marshals it. Three refreshes keep the
rows current. The worker list job rewrites every active worker every 2 seconds. A write site is a code path that
changes a worker's details, such as posting a message or changing a team, and it marks the worker dirty. The same job
rewrites the dirty workers. A rolling pass rewrites every registered worker in ID order, which bounds how stale a row
can be when a write site did not mark it.

**One statement and one read per page.** A page request runs one SQL statement. It selects at most `limit + 1` worker
IDs after the cursor from the index of the sort column and counts the entries that pass the filters in a scalar
subquery, so the response can report whether another page follows and how many workers match. When every worker after
a cursor was deleted or left the filters since the previous page, the statement does not return a row to hold the
count, so a second statement counts `total`. The handler then
reads the stored rows of those IDs with one `HMGET` and joins the bytes into the response body without parsing them.
An ID without a stored row is left out of the page. When the query returned IDs but the row store does not hold a row
for any of them, or Redis cannot be reached, the request is refused with a 503 and a `Retry-After`.

**A keyset cursor.** The cursor is base64url JSON holding a format version, the sort, the last row's sort value and its
worker ID. Every order breaks ties by ascending worker ID, so the position is exact. A page starts at the first row
strictly after the cursor.

**A cursor belongs to one walk.** The cursor also holds a walk key, which is a short hash of the filters, the caller's
view and, on the owner's list, the account. A request whose filters, view or account give another key is refused with
`InvalidWorkerListCursor`. A position in one filtered list does not correspond to a position in another. Without the
key, a walk continued under new filters would skip the matching workers before the cursor, and `total` would count
workers that the walk never returns, without any error. The key is a hash, so the search text does not appear in the
cursor.

**Orders.** `uptime` is the default and descends, so workers with a long service record come first. Names chosen to sort
first alphabetically, such as runs of `A`, do not crowd the first page. `requests_fulfilled` and `kudos_rewards` also
descend. `name` ascends by the lower-cased name. The list does not offer a suspicion order, because ranking suspicion
is a job for moderation tools.

**Page boundaries are best effort.** The cursor holds a sort value, and a worker's sort value can change during a walk.

- A worker whose value changes so that it moves past the cursor before its page is read is skipped by the walk.
- A worker whose value changes so that it moves from a page already read to a later position is listed twice.
- A worker registered, deleted or renamed during a walk can be listed, left out or listed twice, depending on where it
  lands relative to the cursor.
- Workers joining or leaving do not shift the rest of the walk, because each page starts after the last row's position
  in the order.
- A walk always ends, because each page starts strictly after the previous one.

**Filters by view.** `type`, `model`, `team_id`, `owner_id`, `name_contains` and `online` combine freely. A public
`owner_id` filter matches only the workers of owners who made their workers public, and `total` is 0 for any other
owner. `model` matches whole names, because model names nest. `name_contains` matches any part of a name, ignoring
case, and treats `%` and `_` as plain characters.

**An owner's list.** `GET /v2/workers/mine` runs the same query restricted to the caller's account and returns the
owner rows, which add the workers' IP addresses, the owner's contact and the messages.

**Refusals.** A limit outside 1 to 100, a malformed cursor, a cursor issued for another sort or walk and a malformed
filter are refused with a 400 and a return code for clients to branch on. An `apikey` header that does not match any
account is refused with a 401, so a moderator with a mistyped key does not mistake the public view for the whole list.
The full list ignores such a key.

**Rate limits.** Each paged list allows 90 requests a minute per client IP address. Both lists also count against one
limit of 3,000 requests a minute shared by every client IP address, so requests from many addresses cannot push the
page traffic past that.

**Caching.** Public pages are sent with `Cache-Control: public, max-age=15`, because their rows and order are already
seconds to a minute old. A shared cache in front of the horde stores a page only when it is configured to cache API
responses, so the header does not bound the load on the server. The fleet-wide rate limit does. Moderator responses on both lists, and every response of the owner's list, are sent with
`Cache-Control: private, no-store`, because they contain worker IP addresses and contacts. The full list keeps reading
the active worker snapshot. When that snapshot is missing, the API node that serves the request rebuilds the list from
the database and keeps it in local Redis for `LOCAL_CACHE_MAX_LIFETIME` (5 seconds).

**Observability.** `horde.workers.list.reads` counts reads by endpoint (`full`, `page` or `mine`), view, sort, filter
names and whether the read followed a cursor. `horde.workers.list.rows` records rows per response.
`horde.workers.list.snapshot_misses` counts full-list reads that rebuilt the active list from the database.

### Consequences

- Good: Every registered worker can be listed, browsed and searched.
- Good: The full list's rows, shape, filters and status codes do not change for existing clients.
- Good: A page costs one SQL statement and one Redis read, whatever its size, and the request path does not build any
  row. A page after a cursor that has no rows left costs one more statement for the count.
- Good: Workers joining or leaving do not shift the rest of a walk, and a walk always ends.
- Good: The paged list and the owner's list share one query, so their filters and orders behave the same.
- Bad: Pages are stale. The rows of active workers are seconds old. The order, the membership of the list and the
  type, owner, team and name filters come from `worker_list_entries`, which is up to 60 seconds old. A row whose change
  was not marked by any write site stays stale until the rolling pass reaches it, which takes one pass over every
  registered worker.
- Bad: The row store keeps three rows of JSON per registered worker in Redis, and the memory grows with the number of
  registered workers.
- Bad: `total` counts the filtered entries again on every page, so a page's cost includes a count over every entry
  that passes the filters.
- Bad: A worker whose sort value changes during a walk can be skipped or listed twice. A client that needs an exact
  copy of the list has to reconcile walks itself.
- Bad: `GET /v2/workers` and `GET /v2/workers/page` list different sets of workers, and clients can confuse the two.
- Neutral: A worker without a stored row is left out of its page while `total` counts it.

### Confirmation

`tests/unit/test_worker_pages.py` checks the cursor against malformed input and against cursors issued for another
sort or walk, and it checks the walk key and the filter names used as metric labels.
`tests/integration/test_worker_list_entries.py` checks the refresh of `worker_list_entries`, the walks, orders, filters
and statement count of the page query.
`tests/integration/test_worker_rows.py` checks that each stored row equals the single-worker route and that each
refresh rewrites the rows it is responsible for. `tests/integration/test_worker_pages.py` drives both endpoints after
running the production jobs. It checks that a page body holds the stored bytes verbatim, that an anonymous page runs
one statement and does not load any worker, the views and cache headers, the 503 and its `Retry-After`, the shared
rate limit, the refusals and the metric labels. The locust `MetaBrowser` walks the list under load, and `MisuseUser`
probes the refusals.

## Pros and Cons of the Options

### Page a read model built off the request path, with a keyset cursor, on a sibling endpoint

- Good: The list includes inactive workers, which are not in any snapshot.
- Good: Existing clients of `GET /v2/workers` are untouched.
- Good: A page reads an index and Redis and does not build anything, so its cost does not depend on how the rows are
  built.
- Bad: Background jobs, write sites and Redis memory have to keep the read model current, and a page is only as fresh
  as they are.

### Page the `workers` table on each request and load the page's workers through the ORM

- Good: Every page is current.
- Bad: Each page loads up to 100 workers with their relationships and builds their rows in Python on the request path.
- Bad: The sort columns of `workers` are not indexed, and every check-in updates `uptime`, so an index there would add
  write cost to the busiest update path. Each page therefore scans the table and sorts the matching rows, and the
  cost grows with the number of registered workers.

### Page the cached active-worker snapshot with a keyset cursor

- Good: A page reads Redis and not the database.
- Bad: The snapshot holds only active workers, so the list still does not include inactive workers.
- Bad: The active list usually fits in one page, so paging it saves little.

### Add inactive workers to `GET /v2/workers`

- Good: One endpoint.
- Bad: Clients that treat the list as the active workers break.
- Bad: The response grows with every registered worker, without a bound.

### Add `limit` and `offset` to `GET /v2/workers`

- Good: No new endpoint, and the response stays an array.
- Bad: The list still holds only active workers.
- Bad: An offset counts rows, so every worker that joins or leaves ahead of it shifts the rest of the walk.
- Bad: An array does not have room for a next link or a total, so clients have to guess when to stop.

## More Information

The behaviour, the limits, the return codes and the metrics are in [the worker listing
reference](../reference/worker_listing.md).
