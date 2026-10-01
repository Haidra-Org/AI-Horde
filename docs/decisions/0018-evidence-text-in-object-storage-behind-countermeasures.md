---
status: accepted
date: 2026-09-30
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Keep evidence text in object storage and bound rejection floods with countermeasures

## Context and Problem Statement

[ADR 0015](0015-tiered-moderation-evidence-retention.md) records every rejected prompt and every worker report as a
`prompt_moderation_events` row holding up to three prompt stages of 16,000 characters each, never deleted by age. The
database is kept small by constant cleanup of transient data, and a rejected submission costs the submitter nothing:
no kudos are charged, no waiting request is created, and an anonymous key is accepted. The model-rejection path, which
refuses a prompt that the NSFW-model replacement emptied, deliberately applies no IP timeout so that a probe learns
nothing about the filter, and the negative prompt after `###` is kept verbatim. A script can therefore write rejected
prompts at the generate endpoint's rate limit, 90 a minute per address, each holding over 30 KB of text, and the rows
stay for a year.

Two questions follow. Where should the text live so the database stays light, and what bounds a flood of rejections
in the first place.

## Decision Drivers

- The database holds identity, counts and state; bulk text belongs elsewhere.
- Nothing on the request path waits on an outside service.
- A flood is bounded by countermeasures that act on the traffic, not by storage design.
- Moderators keep enough evidence on an abuser to act, and a flood never erases evidence of other subjects.
- Existing object storage use (the `prompts` bucket, the image buckets) is untouched.
- Every lever is manual and visible: detection alerts, operators decide.

## Considered Options

- Keep text in the row and cap it harder
- Upload text from the request and serve it through the API
- Row as outbox, uploaded by the quorum applier, read through presigned links, bounded by per-subject countermeasures

## Decision Outcome

Chosen option: "Row as outbox, uploaded by the quorum applier, read through presigned links, bounded by per-subject
countermeasures".

**Row as outbox.** Capture inserts the row with its text exactly as before, in state `pending`, plus a SHA-256 digest
of the canonical text object and its character count. The quorum node's applier (`upload_moderation_evidence_text`,
every 5 seconds, under a session-level advisory lock) claims pending rows in batches of 50, puts each as
`evidence/{id}.json` in a dedicated bucket, and then, in one conditional update, nulls the text columns and sets the
state `stored`. A failed put leaves the row pending for the next tick. Rows the retention pass cleared between the
put and the update are not returned by the update, and the applier deletes their objects in the same cycle. The
object key is derived from the event id, so retention can delete an object for any row without a column recording it.

**Presigned reads.** The listing returns, for a stored event, a presigned GET link valid for ten minutes, signed
locally without a network call, with `private, no-store` response headers; a pending event still returns its text
inline. The client fetches the object and reads the digest beside it; on an expired link it refetches the event.

**Retention.** The text window and the ceiling delete the object for every row in their batch in one bulk call,
inside the row-locked transaction, and flag only the rows whose delete succeeded. A missing key counts as deleted. When
the store fails, rows whose text is `stored` wait for the next hourly pass and the rest of the batch is flagged, so an
outage never holds back removal of identity from rows without a stored object. The digest and character count leave with the text.
There is no bucket lifecycle rule, because a rule cannot see the moderation-action exemption.

**Per-subject countermeasures.** One counter per IP subject per hour counts every rejection. Past
`HORDE_MODERATION_SUBJECT_TEXT_CAP_PER_HOUR` (default 5), the subject's further events record the digest, character
count and identity but no text. Past `HORDE_MODEL_REJECTION_TIMEOUT_THRESHOLD` (default 5) model rejections, the
subject's address enters the same escalating timeout the filter path applies on its first rejection; in raid mode
every model rejection does. Moderators are exempt from the timeout as on the filter path, and from the text cap,
since they probe the filter on purpose. The first time either countermeasure applies to a subject in an hour,
moderators get one Discord notice naming the subject by its pseudonym prefix, never by prompt or address. Worker reports are never capped.

**Detection and levers.** The applier samples the capture rate and the number of distinct rejecting subjects over the
last five minutes, the pending backlog and its oldest age, and the upload and write failures. Alerts on those gauges
tell one contained abuser from a distributed flood. The levers are manual: raid mode, the two thresholds, the IP
timeout endpoints, rate limits at the deployment's edge, and maintenance mode.

### Consequences

- Good: Rejection text leaves the database within seconds under normal operation; a row keeps about 1 KB with its
  index entries.
- Good: No request waits on the object store, and an outage delays uploads without losing text.
- Good: A single abuser is contained after five rejections, and the evidence of their first five remains.
- Good: The listing stays one query; signing is local and costs under a millisecond an event.
- Bad: Every rejected prompt is still written to Postgres and its write-ahead log once; the countermeasures, not the
  store, bound a flood.
- Bad: Text sits in the database for as long as the store is unreachable; past the shortest text window retention
  clears it without an upload, which the stalled-upload alert exists to prevent.
- Bad: Rows and objects are two stores; a crash between the put and the state update leaves an object that retention
  deletes by id on schedule but that nothing lists until then.
- Bad: A presigned link is a bearer capability for ten minutes; the signing token is scoped to the evidence bucket
  alone.
- Bad: Attempts past the text cap are recorded without text, so a moderator sees that a subject kept trying but not
  what with.

## Pros and Cons of the Options

### Keep text in the row and cap it harder

- Good: No second store.
- Bad: A row per flood request still carries kilobytes, and the table is the one place the horde keeps light.

### Upload text from the request and serve it through the API

- Good: The database never holds text.
- Bad: The request path blocks on the object store, and the API fans out one object read per listed event.
- Bad: A put failure loses the text or turns a rejection into an error.

## Confirmation

`tests/unit/test_evidence_upload.py` covers the applier cycle, the orphan delete and the store against Garage;
`tests/integration/test_prompt_moderation.py` and `tests/integration/test_moderation_migration.py` cover the states,
the retention deletes and the schema; `tests/integration/test_rejection_countermeasures.py` covers the cap, the
timeout, raid mode, the moderator exemption and the notice.

## More Information

The cap and threshold are env vars read at startup, like the retention windows. The detection instruments are listed in `docs/reference/moderation_operations.md`; alert thresholds and edge
rate limits belong to each deployment. The disclosure in the privacy document states that prompt text in
moderation records is held with the object storage provider.
