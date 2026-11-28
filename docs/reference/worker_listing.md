---
title: "Worker listing reference"
summary: "What the full worker list, the paged list of registered workers and an owner's worker list return to each caller, how a cursor walk behaves while workers change, and the list metrics."
topics: [workers]
order: 75
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Worker listing reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

Three endpoints list workers:

- `GET /v2/workers` returns every active worker in one JSON array. A worker is active when it checked in within the
  last 5 minutes.
- `GET /v2/workers/page` lists every registered worker, active or inactive, one bounded page at a time, with filters, a
  choice of order and a cursor.
- `GET /v2/workers/mine` pages through the caller's workers, online or offline, in the owner view.

The full list reads a [snapshot](#snapshot) that is rebuilt every 2 seconds. The two paged lists read a
[read model](#read-model) that background jobs keep current, so a page request does not build any row. The steps for
showing these lists in a front end are in [List workers in a front end](../how-to/list_workers_in_a_frontend.md).
[ADR 0047](../decisions/0047-paginate-the-worker-list-with-a-keyset-cursor.md) records why the paged lists are separate
endpoints, why they read a read model and why they page with a cursor.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Cursor, filters, orders | `horde/worker_pages.py` | `WorkerPageCursor`, `WorkerPageFilters`, `WorkerListSort`, `DEFAULT_WORKER_LIST_SORT` |
| Page size and argument limits | `horde/worker_pages.py` | `WorkerPageLimits` |
| Views, row store, active worker IDs | `horde/worker_rows.py` | `WorkerListView`, `read_worker_rows`, `read_active_worker_ids`, `store_worker_rows` |
| Order and filter table | `horde/classes/base/worker.py` | `WorkerListEntry` |
| Keyset query of the paged lists | `horde/database/functions.py` | `page_registered_workers`, `RegisteredWorkerPage` |
| Refresh jobs | `horde/database/threads.py` | `store_worker_list`, `refresh_worker_list_entries`, `refresh_worker_rows_rolling` |
| Details row of one worker | `horde/classes/base/worker.py` | `WorkerTemplate.get_details` |
| Snapshot read and fallback | `horde/database/functions.py`, `horde/horde_redis.py` | `read_worker_list_snapshot`, `LOCAL_CACHE_MAX_LIFETIME` |
| Full list | `horde/apis/v2/base.py` | `Workers` |
| Paged lists | `horde/apis/v2/base.py` | `WorkerPageResource`, `WorkersPage`, `WorkersMine` |
| View choice, cache headers, rate limits | `horde/apis/v2/base.py` | `_resolve_worker_list_view`, `PUBLIC_WORKER_PAGE_CACHE_CONTROL`, `WORKER_PAGE_FLEET_RATE_LIMIT` |
| Response model | `horde/apis/models/v2.py` | `response_model_worker_page` |
| Return codes | `horde/exceptions.py`, `README_return_codes.md` | `InvalidWorkerListLimit`, `InvalidWorkerListCursor`, `InvalidWorkerListFilter`, `WorkerListUnavailable` |
| Metrics | `horde/metrics.py`, `horde/apis/v2/base.py` | `worker_list_reads`, `worker_list_rows`, `worker_list_snapshot_misses`, `_record_worker_list_read` |

`tests/unit/test_worker_pages.py` covers the cursor and the request parts that do not touch the database.
`tests/integration/test_worker_list_entries.py` covers the refresh of `worker_list_entries` and the page query: walks,
orders, filters and statements. `tests/integration/test_worker_rows.py` covers the row store.
`tests/integration/test_worker_pages.py` covers the endpoints, with workers that register by popping for jobs. The
sections below give the test for each contract.

## Views

A view is the set of fields in each row. Every row is built by `WorkerTemplate.get_details`.

| View | Who gets it | Rows contain |
| --- | --- | --- |
| public | anyone without a moderator's key, on the full list and the paged list | the fields open to every caller, with `owner` and `messages` only for owners who set `public_workers` |
| moderator | a moderator's key, on the full list and the paged list | every field, including `ipaddr`, `contact`, `suspicious` and `paused` |
| owner | the owner's key, on `GET /v2/workers/mine` | the public fields with `owner`, `messages`, `ipaddr` and `contact`, without `suspicious` and `paused` |

Each entry of a paged list has the same fields as the response of `GET /v2/workers/{id}` for that worker, with the
same key (`test_a_page_row_is_the_stored_row_and_the_single_worker_details`).

## Cache headers

| Response | `Cache-Control` |
| --- | --- |
| Public page of `GET /v2/workers/page` | `public, max-age=15` |
| Moderator page of `GET /v2/workers/page` | `private, no-store` |
| Every page of `GET /v2/workers/mine` | `private, no-store` |
| Public response of `GET /v2/workers` | none |
| Moderator response of `GET /v2/workers` | `private, no-store` |

A public page may be kept by shared caches for 15 seconds, because its rows and order are already seconds to a minute
old when they are read. A shared cache in front of the horde stores a page only when it is configured to cache API
responses, so the header does not bound the load on the server. The fleet-wide rate limit does. Moderator and owner
responses contain worker IP addresses and contacts, so no cache may store them.
`test_an_anonymous_page_may_be_cached_by_shared_caches`,
`test_a_moderator_finds_a_private_owners_workers_with_addresses_and_no_caching` and
`test_only_moderator_rows_of_the_full_list_are_kept_out_of_shared_caches` cover the headers.

## Snapshot

The full list does not query the database for rows. The quorum node's `store_worker_list` job runs every 2 seconds and
stores two lists of the active workers in Redis for 300 seconds. The interval and the lifetime are also listed with the
other background jobs in [Operational
constants](operational_constants.md#queue-scans-availability-and-worker-measurements). The two lists are:

| Redis key | View |
| --- | --- |
| `worker_cache` | public |
| `worker_cache_privileged` | moderator |

A request reads its node's local Redis first, then the shared Redis. An entry copied into local Redis is kept for at
most 5 seconds. When the list is missing from both, `read_worker_list_snapshot` builds it from the database, logs a
warning and counts a snapshot miss. The node keeps the rebuilt list in its local Redis for `LOCAL_CACHE_MAX_LIFETIME` (5
seconds), so a burst of reads causes only one rebuild. The recovered snapshot is served within 5 seconds after the job
restarts. `test_a_missing_snapshot_is_rebuilt_once_and_kept_even_when_a_worker_has_a_message` and
`test_a_rebuilt_list_expires_with_the_local_cache_so_the_recovered_snapshot_takes_over` cover the fallback.

## Read model

The paged lists read two stores, both written by jobs on the quorum node.

**`worker_list_entries`** holds one narrow row per registered worker of the image, text and interrogation types: its
type, owner, team, lower-cased name, uptime, completed jobs and kudos. Each order has an index on its column and the
worker ID. `refresh_worker_list_entries` copies the changed values from `workers` every 60 seconds
(`WORKER_LIST_ENTRIES_REFRESH_INTERVAL_SECONDS`) and deletes the entries of deleted workers. The page query reads the
order, the list membership, `total` and the type, owner, team and name filters from this table.

**The row store** is one Redis hash per view (`worker_rows:public`, `worker_rows:moderator`, `worker_rows:owner`).
Each hash holds the finished JSON row of every registered worker. A write site is a code path that changes a worker's
details and marks the worker dirty. Three refreshes write the row store:

| Refresh | What it rewrites | How often |
| --- | --- | --- |
| Active | every active worker | every 2 seconds, in `store_worker_list` |
| Dirty | workers marked by a write site after a change to their details, such as a message, a team change or an owner's setting | the next run of `store_worker_list`, up to 500 workers a run |
| Rolling | every registered worker in ID order | 500 workers every 30 seconds |

The same job keeps the IDs of the active workers in Redis, and the `online` filter reads them. The model filter reads
each worker's advertised models from `worker_models`, and the public owner filter reads `users.public_workers`. The
page query checks both with `EXISTS` subqueries, so both filters are applied in the same statement.

### Freshness

| Part of a page | Age |
| --- | --- |
| Rows of active workers | a few seconds |
| `online` | a few seconds |
| Order, membership, `total`, and the `type`, `owner_id`, `team_id` and `name_contains` filters | up to 60 seconds |
| A row whose change was not marked by a write site | up to one rolling pass over every registered worker |

A new worker appears in the paged lists after the next refresh of `worker_list_entries`. A deleted worker can stay in
the order and `total` for up to 60 seconds. Deleting a worker removes its stored rows, so pages leave it out.

## Full list

`GET /v2/workers` lists only the active workers. Its API description points to `GET /v2/workers/page` for every
registered worker. It takes `type` or `name`. `name` matches one worker by exact name, ignoring case. When both are
set, only `name` is applied. The response is a JSON array of rows. A moderator's key gets the moderator view. Any other
key gets the public view, including a key that does not match an account. `test_the_full_list_returns_a_bare_array`
covers the shape.

## Paged listings

`GET /v2/workers/page` lists every registered worker of the image, text and interrogation types, active or inactive. A
moderator's key gets the moderator view, and any other valid key or no key gets the public view.

| Argument | Default | Meaning |
| --- | --- | --- |
| `limit` | 25 | Rows per page, from 1 to 100. |
| `cursor` | none | The `next_cursor` of the previous page. Omit it for the first page. |
| `sort` | `uptime` | The page order. See [Orders](#orders). |
| `type` | none | `image`, `text` or `interrogation`. |
| `model` | none | Only workers that advertise this exact model name. |
| `team_id` | none | Only workers in the team with this ID. |
| `owner_id` | none | Only workers owned by this account, among the workers whose owner is visible to the caller. |
| `name_contains` | none | Only workers whose name contains this text, ignoring case. At most 100 characters. |
| `online` | none | `true` for active workers, `false` for the others. |

The filters combine freely. A blank `model`, `team_id` or `name_contains` does not narrow the list. The response is:

```json
{"workers": [...], "next_cursor": "eyJ2IjoxLC...", "total": 412}
```

`total` is the number of registered workers that pass the filters when the page is read, across all pages.
`next_cursor` is null on the last page. With `online=true`, a walk returns the rows of the full list for the same view
(`test_the_online_filter_returns_the_rows_of_the_full_list`).

The response body is built from the stored rows without parsing them. The rows are joined into `workers`, and
`next_cursor` and `total` follow. A worker returned by the query without a stored row is left out of the page, and
`total` still counts it (`test_a_worker_without_a_stored_row_is_left_out_and_still_counted`).

The paged list refuses a key that does not match an account with a 401, while the full list accepts it. A moderator who
mistypes a key gets an error on the paged list. On the full list, the same moderator gets the public view and may take
it for the whole list (`test_a_mistyped_key_is_refused_rather_than_read_as_the_public_view`).

### Orders

| `sort` | Order |
| --- | --- |
| `uptime` | longest uptime first (the default) |
| `requests_fulfilled` | most jobs completed first |
| `kudos_rewards` | most kudos earned first |
| `name` | name ascending, ignoring case |

Uptime is the default, so workers with a long service record come first and names chosen to sort first alphabetically do
not fill the first page. Rows with the same value are ordered by ascending worker ID, so no two rows tie and the
cursor has an exact position. The list does not offer an order by suspicion.

### Filters and privacy

A public caller who filters by `owner_id` gets only the workers of owners who made their workers public. For an owner
who did not, the page is empty and `total` is 0, so the count does not show how many workers the account runs
(`test_the_public_cannot_find_a_private_owners_workers_by_owner`). An owner who uses their API key on this endpoint
also gets the public view. `GET /v2/workers/mine` lists their workers in the owner view.

`model` matches whole model names, because model names nest and a partial match would list workers that cannot serve
the model. An interrogation worker does not advertise models, so it does not match a `model` filter. `name_contains`
matches any part of a worker name after both are lower-cased. `%` and `_` in the search text match only themselves.

### Walking the list

Pass each `next_cursor` back with the same `sort` and filters until it is null. The cursor holds the sort, the last
row's sort value and its worker ID. It also holds a walk key, a short hash of the filters, the caller's view and, on the
owner's list, the account. A cursor is refused with `InvalidWorkerListCursor` under another sort, other filters, another
view or another account. To change the filters, request the first page again without a cursor. A position in one
filtered list does not correspond to a position in another, so a walk continued under new filters would skip workers
without any error (`test_a_cursor_reused_with_other_filters_is_refused`,
`test_a_cursor_reused_in_another_view_or_list_is_refused`).

Page boundaries are best effort, because a worker's sort value can change during a walk:

- A worker whose value changes so that it moves past the cursor before its page is read is skipped by the walk.
- A worker whose value changes so that it moves from a page already read to a later position is listed twice.
- A worker registered, deleted or renamed during a walk can be listed, left out or listed twice, depending on where it
  lands relative to the cursor.
- Workers joining or leaving do not shift the rest of the walk, because each page starts after the last row's position
  in the order.
- A walk always ends, because each page starts strictly after the previous one.

Uptime is added at each check-in, and jobs and kudos are added as a worker works, so these values change mostly for
online workers. A client that needs an exact copy of the list has to reconcile walks itself.

## Owner's view

`GET /v2/workers/mine` requires the `apikey` header and lists the workers owned by that key's account, online or not, in
the owner view. It takes the same arguments as the paged list except `owner_id`, and runs the same query restricted to
the account. A key that does not match an account is refused with 401 `InvalidAPIKey`. A moderator's key lists only the
workers of the moderator's account. A worker that changes owner moves to its new owner's list after the next refresh
of `worker_list_entries`. `TestOwnersListingTheirOwnWorkers` covers these rules.

## Query cost

A page runs one SQL statement on `worker_list_entries`. It selects the IDs and sort values of at most `limit + 1`
workers after the cursor and counts `total` in a scalar subquery. When no rows follow the cursor, the page runs a second
statement to count `total`, because the first statement does not return a row to hold the count. This is rare. An
issued cursor is past the end only when every worker after it was deleted or left the filters since the previous page.
A request with an `apikey` adds the statements that look up the account. A page request does not load any worker
(`test_an_anonymous_page_issues_one_statement_on_worker_list_entries`,
`test_the_moderator_statement_count_does_not_grow_with_suspicion_rows_or_interrogation_workers`,
`test_the_statement_count_does_not_grow_with_the_number_of_owned_workers`).

A page reads Redis once with `HMGET` for its rows, and once more with `SMEMBERS` when `online` is set. The rate limits
add their own counter updates in the limiter's storage.

## Availability

The paged lists return 503 `WorkerListUnavailable` with `Retry-After: 10` when:

- Redis is not connected, or a read of the row store or of the active worker IDs fails.
- The query returned worker IDs but the row store does not hold a row for any of them. The row hash is then missing as
  a whole. This happens when Redis loses its data, or when the jobs stop for an hour and the row hashes expire.

A page with some rows missing is served without them, because a single row can be missing for a short time while the
rest are stored. `TestAvailability` covers both sides.

## Rate limits

| Limit | Applies to | Value |
| --- | --- | --- |
| Per client IP address | each of `GET /v2/workers/page` and `GET /v2/workers/mine` | 90 requests a minute |
| Fleet-wide | both endpoints together, from every client IP address | 3,000 requests a minute (`WORKER_PAGE_FLEET_RATE_LIMIT`) |

The fleet-wide limit is a [flask-limiter](https://flask-limiter.readthedocs.io/en/stable/) shared limit (`Limiter.shared_limit`)
with one key for every caller, so total page traffic has a ceiling however many client IP addresses send it. A request
over either limit gets the limiter's 429 (`test_page_and_mine_requests_share_one_fleet_limit`).

## Return codes

| Status | `rc` | Cause |
| --- | --- | --- |
| 400 | `InvalidWorkerListLimit` | `limit` is outside 1 to 100. |
| 400 | `InvalidWorkerListCursor` | The cursor is malformed, longer than 1,024 characters, of an unknown version, issued for another sort, filters, view or account, or holds a sort value outside the range of a 64-bit integer. |
| 400 | `InvalidWorkerListFilter` | `team_id` is not a UUID, `owner_id` is negative, or `name_contains` is longer than 100 characters. |
| 400 | (reqparse) | `sort`, `type` or `online` is not one of the listed values, or `apikey` is missing on the owner's list. |
| 401 | `InvalidAPIKey` | The `apikey` header does not match any account. |
| 429 | (limiter) | A [rate limit](#rate-limits) was reached. |
| 503 | `WorkerListUnavailable` | The page could not be read from the read model. See [Availability](#availability). |

`TestRefusedRequests` covers the 400 return codes.

## Metrics

| Instrument | Kind | Labels | Use |
| --- | --- | --- | --- |
| `horde.workers.list.reads` | counter | `horde.workers.list.endpoint` (`full`, `page`, `mine`), `.view` (`public`, `moderator`, `owner`), `.sort` (an order above, or `none` on the full list), `.filters` (the filter names in use, comma separated, or `none`), `.continued` (`true` when the read followed a cursor) | How many requests read each list, and how many walks go past the first page. |
| `horde.workers.list.rows` | histogram | `horde.workers.list.endpoint` | Rows per response. The full list's rows grow with the number of active workers, and the paged lists' rows stay at or under the limit. |
| `horde.workers.list.snapshot_misses` | counter | `horde.workers.list.view` | Full-list reads that rebuilt the list from the database. Any steady rate means the snapshot job on the quorum node has stopped. |

Every label value comes from a fixed set. Filter values, such as a model name, a name search or an owner ID, are not
used as labels. `test_reads_are_counted_by_endpoint_and_walk_position_with_bounded_labels` and
`test_reads_are_counted_under_their_own_endpoint_and_view` cover the labels. The row store's metrics,
`horde.worker_rows.written` and `horde.worker_rows.rolling_pass_duration`, show how many rows each refresh writes and
how long a rolling pass takes. `horde.worker_rows.written` counts the rows by the `horde.worker_rows.tier` attribute
(`active`, `dirty`, `rolling`). The attribute is not named `horde.tier`, because a deployment can promote the
`horde.tier` resource attribute to a metric label, and a data point attribute of the same name would be merged into
that label
(`test_written_rows_are_counted_under_an_attribute_apart_from_the_promoted_resource_labels`).

## Sharp edges

- **Pages are stale.** See [Freshness](#freshness). A public page can also be served by a shared cache for 15 seconds.
- **`total` can change during a walk.** It is counted again for each page, and it counts workers whose row is missing.
- **A walk is not exact.** Workers whose sort values change during the walk can be skipped or listed twice, as described
  under [Walking the list](#walking-the-list).
- **The name search lower-cases with Python's `str.lower()`.** Characters with a different case-folded form, such as
  `ß`, do not match their expansions, so `STRASSE` does not match `Straße`.
- **An owner's key on the paged list gets the public view**, as on the full list. Owners who keep their workers private
  see the owner view only on `GET /v2/workers/mine`.
- **Shared keys are refused.** `find_user_by_api_key` does not accept shared keys, so a shared key gets a 401 on both
  paged lists and the public view on the full list.

## Related

- [List workers in a front end](../how-to/list_workers_in_a_frontend.md)
- [ADR 0047: list every registered worker in pages with a keyset
  cursor](../decisions/0047-paginate-the-worker-list-with-a-keyset-cursor.md)
- [Moderation operations reference](moderation_operations.md), for suspicion history and the paused-worker queue
