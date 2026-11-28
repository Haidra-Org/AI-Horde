---
title: "List workers in a front end"
summary: "Browse every registered worker one page at a time, load later pages, search and filter them, list an owner's workers, and handle the refusals by return code."
topics: [workers]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# List workers in a front end

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

A front end shows every registered worker, active or inactive, from `GET /api/v2/workers/page`, and loads later pages
with a cursor. It searches and filters the same endpoint, lists an owner's workers from `GET /api/v2/workers/mine`, and
recovers from each refusal by its return code. `GET /api/v2/workers` lists only the workers that checked in within the
last 5 minutes.

You need:

- Network access to the `/api/v2` endpoints of a horde deployment.
- For [step 4](#4-list-an-owners-workers), the API key of an account that runs workers.

Steps 1 and 2 are the core of the procedure. Steps 3 and 4 are independent options that build on the first page
request. Step 5 applies to every request.

The [worker listing reference](../reference/worker_listing.md) holds the full contract: every argument, the fields in
each view, the walk rules, the freshness of each part of a page and the metrics. The commands below use these shell
variables:

```bash
HORDE=<HORDE_URL>/api/v2
APIKEY=<your-api-key>
CLIENT_AGENT="<client name>:<version>:<contact>"
```

## 1. Load the first screen

Request one page without a cursor. The default order lists the workers with the longest uptime first.

```bash
curl -s "$HORDE/workers/page?limit=25" -H "Client-Agent: $CLIENT_AGENT" | jq '{total, next_cursor, shown: (.workers | length)}'
```

The body has `workers`, `next_cursor` and `total`. `shown` is at most 25. `total` is the number of registered workers
that pass the filters, across all pages. Each entry in `workers` has the same fields as the response of
`GET /api/v2/workers/{id}` for that worker, so the code that renders one worker also renders an entry here. Which fields
appear depends on the API key, as on that route (see [views](../reference/worker_listing.md#views)). The `online`
field of each entry shows whether the worker is active.

The rows and their order are read from copies that the horde refreshes in the background, so a page can be seconds to
a minute old. The [freshness table](../reference/worker_listing.md#freshness) gives the age of each part. A public
page has `Cache-Control: public, max-age=15`, so a browser or a shared cache can reuse it for 15 seconds.

## 2. Load the next pages

Pass the previous page's `next_cursor` back as `cursor`, with the same `sort`, until `next_cursor` is null. A front end
that loads workers as the user scrolls requests the next page when the user asks for it. A cursor is not tied to a
session, so a client may stop following it at any point.

```bash
CURSOR=$(curl -s "$HORDE/workers/page?limit=25" -H "Client-Agent: $CLIENT_AGENT" | jq -r '.next_cursor')
curl -s "$HORDE/workers/page?limit=25&cursor=$CURSOR" -H "Client-Agent: $CLIENT_AGENT" | jq '{next_cursor, shown: (.workers | length)}'
```

Check a walk against these rules, which are described in full under
[Walking the list](../reference/worker_listing.md#walking-the-list):

- The walk ends with `next_cursor` set to null.
- `total` can change between pages, because it is counted again for each page.
- A page can hold fewer rows than `limit` before the last page, because a worker without a stored row is left out.
  Only a few rows are left out this way. When many are missing, the horde returns 503 instead of a near-empty page.
- A worker whose uptime, jobs or kudos change during the walk can be skipped or appear twice. These values change
  mostly for online workers.

If the rows are kept in one list on screen, drop a row whose worker ID is already shown.

## 3. Search and filter

Add filters to the first page request. They combine freely:

- `name_contains` matches any part of a worker name, ignoring case.
- `model` matches a whole model name.
- `type` is `image`, `text` or `interrogation`.
- `team_id` is a team's ID.
- `online` is `true` for active workers and `false` for the others.

```bash
curl -s "$HORDE/workers/page?name_contains=<part of a name>&type=image" -H "Client-Agent: $CLIENT_AGENT" | jq '[.workers[].name]'
curl -s "$HORDE/workers/page?model=<model name>&online=true" -H "Client-Agent: $CLIENT_AGENT" | jq '.total'
```

Every listed name contains the search text, and `total` counts only the matching workers. A blank filter value does
not narrow the list. When the user changes a filter, request the first page again without a cursor. The server refuses
a cursor sent with filters other than those of the page that returned it. A worker that was just registered or renamed
appears in search results within about a minute.

## 4. List an owner's workers

The owner's list includes the workers' IP addresses and the owner's contact. The response has
`Cache-Control: private, no-store`, and a front end must not store it in a shared cache.

Send the owner's key to `GET /api/v2/workers/mine`. The list includes private workers and workers that are offline. Pass
`online=false` to show only the workers that have stopped checking in.

```bash
curl -s "$HORDE/workers/mine?online=false" -H "apikey: $APIKEY" -H "Client-Agent: $CLIENT_AGENT" | jq '[.workers[] | {name, online}]'
```

Every row has `"online": false`, and every `owner` is the account of the key. A worker that changes owner moves to its
new owner's list within about a minute.

## 5. Handle refusals

Branch on the `rc` field of the error body, not on the message text.

| Status and `rc` | What the front end does |
| --- | --- |
| 400 `InvalidWorkerListCursor` | Request the first page again without a cursor. The cursor was altered, or was sent with another sort, other filters or another API key than the page that returned it. |
| 400 `InvalidWorkerListLimit` | Request a `limit` from 1 to 100. |
| 400 `InvalidWorkerListFilter` | Show the filter as invalid. `team_id` must be a UUID and `name_contains` at most 100 characters. |
| 401 `InvalidAPIKey` | Ask the user to check the API key. The paged lists refuse a key that does not match an account. |
| 429 | Wait and retry. Each paged list allows 90 requests a minute per client IP address, and both together allow 3,000 requests a minute from all addresses. |
| 503 `WorkerListUnavailable` | Wait for the seconds in the `Retry-After` header, then repeat the same request. The horde could not read the page, or the stored rows of many of its workers were missing, as they are for a few minutes while the horde rebuilds them. Keep the rows already shown. The horde logs each refusal as one warning and counts it in `horde.workers.list.unavailable`, so retries after `Retry-After` do not flood its logs. |

To test the handling, request a page with `limit=0` and with `cursor=abc`. The front end shows the limit error for the
first request and starts again from the first page for the second.

```bash
curl -s "$HORDE/workers/page?limit=0" -H "Client-Agent: $CLIENT_AGENT" | jq '.rc'
curl -s "$HORDE/workers/page?cursor=abc" -H "Client-Agent: $CLIENT_AGENT" | jq '.rc'
```

The first command prints `"InvalidWorkerListLimit"` and the second prints `"InvalidWorkerListCursor"`.
