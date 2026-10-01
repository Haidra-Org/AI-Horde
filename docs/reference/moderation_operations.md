---
title: "Moderation operations reference"
summary: "What the horde retains when a prompt is rejected or a worker reports a job, how retention removes text, address and identity on schedule, and the moderator API that lists it and attaches notes."
topics: [requests, moderation, operations]
order: 65
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Moderation operations reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [moderation](../topics.md#moderation), [operations](../topics.md#operations), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

A rejected prompt or a worker's report on a job writes an event that outlasts the request it came from, so a moderator
can review it after the request expired or the account was deleted. Wiping the account keeps its events, their notes
and its worker report records. A replacement that succeeded and then generated is never recorded. Moderators list events and
attach notes to them.

## Code map

| Concept                     | File                                        | Symbol                               |
| --------------------------- | ------------------------------------------- | ------------------------------------ |
| Evidence row, notes         | `horde/classes/base/prompt_moderation.py`   | `PromptModerationEvent`, `PromptModerationNote` |
| Capture                     | `horde/database/prompt_moderation.py`       | `record_prompt_evidence`             |
| Rejection capture hook      | `horde/apis/v2/base.py`                     | `GenerateTemplate._record_prompt_rejection` |
| Worker report capture       | `horde/classes/base/user.py`                | `User.record_problem_job`, `UserProblemJobs` |
| Alert review link           | `horde/discord.py`                          | `moderation_event_url`               |
| IP subject and pseudonym    | `horde/countermeasures.py`                  | `CounterMeasures.ip_subject`, `CounterMeasures.parse_ip_subject`, `CounterMeasures.ip_subject_key`, `IP_SUBJECT_KEY_SECRET` |
| Listing and notes           | `horde/database/prompt_moderation.py`       | `get_prompt_events`, `add_prompt_note` |
| Retention policy            | `horde/database/prompt_moderation.py`       | `RetentionPolicy`, `load_retention_policy`, `MODERATION_RETENTION_POLICY` |
| Column retention map        | `horde/database/prompt_moderation.py`       | `EVENT_COLUMN_RETENTION`, `RetentionFate`, `REMOVAL_FLAGS` |
| Retention pass              | `horde/database/prompt_moderation.py`, `horde/database/threads.py` | `apply_evidence_retention`, `apply_moderation_retention` |
| Moderation-action exemption | `horde/database/prompt_moderation.py`       | `_under_moderation_action`, `_account_restricted` |
| Privacy disclosure          | `horde/apis/v2/base.py`, `horde/templates/privacy_policy.html` | `DocsPrivacy`                        |
| Endpoints and limits        | `horde/apis/v2/moderation.py`               | `Operations*` resources, `_moderator_limits` |
| Rate limit key              | `horde/apis/limiter_api.py`                 | `get_request_api_key_per_method`     |
| Evidence store              | `horde/r2.py`                               | `build_evidence_client`, `evidence_client`, `evidence_object_key`, `put_evidence_text`, `delete_evidence_texts`, `evidence_text_url` |
| Text upload applier         | `horde/database/prompt_moderation.py`, `horde/database/threads.py` | `upload_pending_text`, `claim_pending_text`, `mark_text_stored`, `upload_moderation_evidence_text` |
| Rejection countermeasures   | `horde/countermeasures.py`, `horde/apis/v2/base.py` | `CounterMeasures.count_rejection`, `CounterMeasures.claim_rejection_notice`, `GenerateTemplate._handle_prompt_rejection` |
| Countermeasure policy       | `horde/database/prompt_moderation.py`       | `MODEL_REJECTION_TIMEOUT_THRESHOLD`, `SUBJECT_TEXT_CAP_PER_HOUR`, `load_positive_setting` |
| Detection gauges            | `horde/metrics.py`, `horde/database/prompt_moderation.py` | `moderation_*` instruments, `pending_text_health`, `recent_rejection_activity` |

The schema is `sql_statements/5.1.12.txt`. Tests build the tables from the models, so
`test_orm_models_build_the_same_moderation_tables_as_the_migration` in
`tests/integration/test_moderation_migration.py` requires both to produce the same columns, defaults, constraints and
indexes.

## Recorded events

An event holds the account, any proxied account, the submitting address and its pseudonym (or, for an origin that is
not an address, its text), the requested model names, whichever request, job and worker identifiers exist, the reason,
the outcome, and the known prompt stages.

| Reason             | Outcome    | Raised by                                                  |
| ------------------ | ---------- | ---------------------------------------------------------- |
| `filter_rejection` | `rejected` | The prompt filter rejected the prompt, or the replacement filter emptied it or was asked to replace more than 7000 characters |
| `model_rejection`  | `rejected` | The NSFW-model replacement emptied the prompt, for an NSFW model or for a flagged account on any model |
| `worker_csam`      | `censored` | An image worker reported the job                            |

A `worker_csam` event records that a worker reported the job. It carries no finding about the account.

The address is the IP subject (`CounterMeasures.ip_subject`) of the request origin the rejection or the waiting
request saw. The same function normalizes worker report records, the `ipaddr` filter and worker IP blocks:

| Origin                                            | Recorded as                         |
| ------------------------------------------------- | ----------------------------------- |
| IPv4 address                                      | The address                         |
| IPv4-mapped IPv6 address (`::ffff:a.b.c.d`)       | The IPv4 address                    |
| IPv6 address, or IPv6 network of /64 or narrower  | Its /64 network, since IPv6 clients rotate addresses inside it |
| Blank                                             | `null`                              |
| Anything else, such as free text in `Proxied-For` or a wider network | `null`, with no pseudonym; the text is kept as `origin_text` (blank is nothing) |

A value with no subject is never keyed or blocked: blocking a worker whose recorded origin has no subject sets
no timeout and logs a warning. Capture keeps its text in `origin_text` on the event and the worker report record,
clipped to 255 characters (`MAX_ORIGIN_TEXT_CHARACTERS`); `origin_text` is `null` when the origin had a subject.

Capture also stores `ip_subject_key`, the address pseudonym: HMAC-SHA256 under the deployment's `secret_key`, over a
moderation label and the subject. It is equal for equal subjects and outlives the address, so address filters keep
matching an event after its address is removed. The IPv4 space is small enough to enumerate, so a pseudonym under a
published secret reverses to its address: when `secret_key` is unset or equals the `.env_template` placeholder or
`hash_api_key`'s fallback, startup logs one warning and events are captured without a pseudonym. `models` lists the
models the request asked for, not the model a worker ran.

| Stage               | Meaning                                              | Rejection | `worker_csam`                     |
| ------------------- | ---------------------------------------------------- | --------- | --------------------------------- |
| `submitted_prompt`  | The original submission, before styles or moderation | Recorded  | Recorded when the request kept it |
| `moderation_prompt` | The text moderation evaluated, after styles          | Recorded  | `null`                            |
| `effective_prompt`  | The prompt a worker received                         | `null`    | Recorded                          |

A stage that is unknown or does not exist is `null`, never a copy of another stage. A rejected submission has no
waiting-request ID, since no row was created. A worker report cannot recover the styled pre-moderation prompt, so
`moderation_prompt` stays `null`. A rejection never has an effective prompt: it happens either before any replacement
ran or after a replacement emptied the prompt, and a replacement that produces text lets the request through without an
event. The text a rejection refused is `moderation_prompt`.

Each stage is clipped to 16,000 characters behind a `text_truncated` flag. The limit applies to evidence alone; the
original submission on the waiting request keeps its full length. `job_id` is unique, so a repeated worker report for
one job resolves to the existing event rather than a second row. A rejected attempt has no request ID, so each attempt
is a separate row.

The stages are held in the row only until the quorum node uploads them (see [Evidence store](#evidence-store)).
`text_state` says where the text is: `pending` (in the row, awaiting upload), `stored` (in the evidence bucket at
`evidence/<id>.json`, the row's stage columns null) or `none` (no text anywhere: no stage existed, the subject was
past its text cap, or retention cleared it). `text_sha256` is the SHA-256 of the canonical object (the three clipped
stages as compact JSON with sorted keys) and `text_chars` the sum of the clipped stage lengths; both are recorded
whenever a stage existed, including past the cap, and leave with the text.

Captured values never change; retention removes text, address and identity on schedule (see [Retention](#retention)).
A moderator can attach notes to an event, including an anonymized one; a note is a separate row that is never
deleted, and it exempts its event from retention (see [Moderation action](#moderation-action)).

Worker reports write `user_problem_jobs`, and the problem-job Discord alerts count that table: the account alerts
over the past hour or day, and the IP alerts over the past hour or day for one IP subject. Each alert carries the
event ID and, when configured, the review link (see [Problem-job alerts](#problem-job-alerts)). A report whose origin
has no IP subject is counted for its account only, since records without an address cannot be told apart by IP.
Those records follow the evidence address window and ceiling (see [Retention](#retention)).

### Problem-job alerts

An alert identifies its subject (the account alias, or the IP subject by the first 12 hex characters of its pseudonym
with the latest account), the count over the window, the threshold, the mute period, the latest job, worker and
models, the LoRA names, and the event ID of the evidence just captured. It carries no prompt text and no address: a
Discord message outlives the evidence retention window and cannot be redacted, and Discord drops content over 2,000
characters, which lost long-prompt alerts while their mute key was set. If `HORDE_MODERATION_FRONTPAGE_URL` is set
to an absolute HTTP URL, the alert links to `<url>/admin/review?tab=prompts&event_id=<id>`; otherwise it says to
open the event in the frontpage Prompts tab. When the evidence write failed, the alert says so in place of the event
ID and has no review line. Under a placeholder secret there is no pseudonym and the IP subject is unnamed.

## Moderator API

All paths below are under `/api/v2/operations/moderation`.

### Conventions

Every endpoint requires the moderator `apikey` header and responds with `Cache-Control: private, no-store`. Every
timestamp returned is ISO 8601 in UTC with an explicit `+00:00` offset; times sent must carry an explicit timezone.
Responses are built from explicit field lists, since these tables hold prompt text and a future column must not appear
in the API by accident.

Rate limits count per API key and method (`get_request_api_key_per_method`), not per address, since moderators share
office addresses and VPN exits: 120 reads (`GET`) and 30 writes (`POST`) a minute on each endpoint. Each limit is
scoped to its endpoint rather than the concrete path, so notes on every event share one budget. A request without the
header counts against the anonymous key's bucket for that endpoint and is refused by the missing header, so keyless
traffic cannot spend a moderator's allowance. Every response lists `Retry-After`, `X-RateLimit-Limit`,
`X-RateLimit-Remaining` and `X-RateLimit-Reset` in `Access-Control-Expose-Headers`, so a browser client can read how
long to wait after a `429`.

### Events and notes

- `GET /prompts` filters by `event_id`, `user_id`, `proxied_account`, `ipaddr`, `ip_subject_key`, `worker_id`,
  `since` (inclusive) and `until` (exclusive). `event_id` selects that one event; the `event_id` in an alert's review
  link is meant for it, and a value below 1 returns `400 InvalidModerationEventID`. `ipaddr` takes an address or an IPv6 network of
  /64 or narrower and selects its IP subject, so an IPv6 address selects its /64 and an IPv4-mapped address its IPv4
  address. It matches events by pseudonym, so an event whose address retention removed still matches until it is
  anonymized; an event without a pseudonym matches by its address. A blank `ipaddr` filters nothing, and a value that
  is not one subject returns `400 InvalidModerationAddressFilter`. `ip_subject_key` takes a pseudonym as the listing
  returns it (64 lowercase hex characters) and matches events carrying exactly that pseudonym; any other value returns
  `400 InvalidModerationAddressFilter`. The page returns event identity, reason/outcome, `text_state`, the three prompt stages
  (inline only while `pending`), `text_url` (a presigned link to the object while `stored`, valid for 10 minutes
  and signed without a network call; `null` otherwise, and always `null` when no evidence store is configured),
  `text_sha256`, `text_chars`, `text_truncated`, correlation fields except the internal `job_id`, `ip_subject_key` (the pseudonym, equal for events
  of one subject, never an address), the retention flags `text_redacted`, `ipaddr_redacted` and `anonymized`, and
  notes ordered oldest first. A retention flag is set once its step ran on the event, whether or not there was a value
  to remove. `user_id` and `ip_subject_key` are `null` on an anonymized event. A client that reveals text after the link expired
  gets `403` from the store and refetches the event by `event_id` for a fresh link. The page also carries
  `retention`: `text_days`, `ipaddr_days` (each `null` when that window is `none`) and `ceiling_days`, the policy in
  force. It takes `limit` 1-100 (default 50) and an exclusive descending `before_id` cursor, and returns
  `next_cursor` or `null`. An out-of-range page
  returns `400 InvalidOperationsLimit` or `400 InvalidOperationsCursor`; a bound without a timezone, or `since` not
  earlier than `until`, returns `400 InvalidModerationTimeRange`.
- `POST /prompts/<id>/notes` accepts `{note}`: 1-2,000 characters, nonblank, without NUL; anything else returns
  `400 InvalidModerationNote`. It returns the note with reviewer attribution and creation time. A note on an
  anonymized event is stored like any other; an event ID that does not exist returns `404 ModerationEventNotFound`.

Each subject filter reads its own index, newest `id` first: `(user_id, id)`, `(proxied_account, id)`, `(worker_id, id)`
and `(ip_subject_key, id)`, the last three partial on a non-null value. Events without a pseudonym keep a partial
`(ipaddr, id)` index for the `ipaddr` fallback, which stays small while the deployment has a private secret.

## Retention

Retention never deletes an event by age. Each part of an event not under moderation action decays on its own schedule,
counted from capture, under a hard ceiling that applies to every reason:

| Variable                                  | Default | Removes at that age                                   | `none`                  |
| ----------------------------------------- | ------- | ----------------------------------------------------- | ----------------------- |
| `HORDE_MODERATION_TEXT_RETENTION_DAYS`    | `none`  | The three prompt stages, the stored object, `text_sha256` and `text_chars`; sets `text_redacted` and `text_state` `none` | Kept until the ceiling  |
| `HORDE_MODERATION_IPADDR_RETENTION_DAYS`  | 30      | `ipaddr` (the pseudonym stays); sets `ipaddr_redacted` | Kept until the ceiling  |
| `HORDE_MODERATION_EVIDENCE_CEILING_DAYS`  | 365     | Identity; sets all three flags (below)                | Refused                 |

The ceiling anonymizes the event: it clears `user_id`, `proxied_account`, `ipaddr`, `ip_subject_key`, `request_id`,
`job_id`, the prompt stages, the stored object, `text_sha256` and `text_chars`. It keeps `reason`, `outcome`, `models`, `created`, `text_truncated`, the notes and
`worker_id`, which identifies the reporting worker rather than the subject, so counts by reason, outcome, model and
worker stay derivable from the rows.

### Moderation action

Every retention step skips an event under moderation action, with no cap, for as long as the condition holds.
`_under_moderation_action` is the single definition. An event is under moderation action when any of these holds:

| Condition | Source |
| --------- | ------ |
| The event has at least one note | `prompt_moderation_notes.event_id` |
| The event's account is flagged | `user_roles` row `FLAGGED` with `value` true |
| The event's account is suspicious: not trusted, and at least `User.SUSPICION_THRESHOLD` (5) suspicions | `user_roles` row `TRUSTED`, count of `user_suspicions`, as `User.is_suspicious` judges it |

Worker report records use the account conditions only (`_account_restricted`). The records exist to identify accounts
attempting to generate illegal content and to enforce the restrictions that follow, which cannot be enforced once the
record is gone; the ceiling bounds only unactioned events. See
[ADR 0015](../decisions/0015-tiered-moderation-evidence-retention.md).

The exemption is evaluated at each pass. Once an event has no note and its account is neither flagged nor suspicious,
the next pass applies every window already elapsed. Values already removed stay removed if the account is flagged
later.

The variables are read once at startup into `MODERATION_RETENTION_POLICY`; the retention pass and the privacy
document both read that policy when they run. A window is a positive whole number of days or `none` (any case); the
ceiling is a positive whole number of days, at most 365, and cannot be disabled. A window longer than the ceiling,
or any malformed value, raises `ValueError` at import and stops startup.

Worker report records (`user_problem_jobs`) follow the same address window and ceiling: past the address window their
`ipaddr` and `origin_text` are nulled, and past the ceiling the record is deleted, unless the account is flagged or
suspicious. They have no foreign key to the reporting worker, so deleting a worker, including through an account
wipe, keeps the reports it made. Their
alerts count the last hour or day only, so
neither step changes an alert.

The primary node's maintenance loop runs the retention tick every minute (`apply_moderation_retention`). A tick
repeats passes while any step changes a full batch, up to 20 passes (`RETENTION_MAX_CATCHUP_CYCLES`), so each step can
drain 20,000 rows a minute and a backlog of millions clears in hours. A pass anonymizes, then removes text, then removes
addresses; each step changes at most 1,000 events (`CLEANUP_BATCH_SIZE`), oldest first, in its own transaction.
The two steps that clear text lock their batch, delete the objects of every row in it with one bulk call (a missing
key counts as deleted, so a row still `pending` or already `none` costs nothing), and flag only the rows whose delete
succeeded. When the store fails or no store is configured, `stored` rows stay unflagged for the next pass and every
other row proceeds, so a store outage never blocks the ceiling or the text step for rows without a stored object.
Without a configured store, the warning that `stored` rows wait is logged at most once an hour. A
`pending` row flagged during an outage is safe: an upload that stored its object finds the row cleared and deletes the
object itself.
Each step reads a partial index on `(created, id)` that excludes events it already finished, so a pass does not
rescan finished rows, and a repeated pass changes nothing. Two steps then treat worker report records the same way:
one deletes at most 1,000 past the ceiling through their `created` index, and one nulls the address of at most 1,000
past the address window through `ix_user_problem_jobs_ipaddr_pending`
(`WHERE ipaddr IS NOT NULL OR origin_text IS NOT NULL`).

Wiping an account (`User.wipe`) touches no event, note or worker report record. They keep their `user_id`, so the
account's records stay listed and filterable, and retention treats them as any other account's.

The privacy document (`/api/v2/documents/privacy`) renders the same windows in its "Moderation records" section, and
links the source code at `HORDE_REPOSITORY` (`horde.vars.horde_repository`, default
`https://github.com/Haidra-Org/AI-Horde`).

### Retention effects

`EVENT_COLUMN_RETENTION` in `horde/database/prompt_moderation.py` is the source of truth for which columns each step
clears: the steps build their updates from it, and startup fails when an event column has no fate.
`test_every_event_column_has_a_retention_fate` in `tests/unit/test_moderation_retention_policy.py` locks its coverage,
and `test_anonymization_removes_identity_and_keeps_the_record_and_its_notes` its effect. Every row below applies
only to rows not under [moderation action](#moderation-action).

| Setting or event | Columns and tables changed | Readers affected |
| ---------------- | -------------------------- | ---------------- |
| Text window | `submitted_prompt`, `moderation_prompt`, `effective_prompt`, `text_sha256` and `text_chars` nulled; `evidence/<id>.json` deleted; `text_state` set `none`; `text_redacted` set | The listing returns null stages and a null `text_url`. No filter reads text. |
| Address window | `ipaddr` and `origin_text` nulled; `ipaddr_redacted` set; `ip_subject_key` kept. `user_problem_jobs.ipaddr` and `user_problem_jobs.origin_text` nulled | The listing returns a null `ipaddr` and `origin_text`, and the same `ip_subject_key`. The `ipaddr` filter, including another address in the same IPv6 /64, still finds an event with a pseudonym; an event without one no longer matches it. Problem-job alerts, which count the last hour or day, are unaffected. |
| Ceiling | Text and address columns, `text_sha256`, `text_chars`, `ip_subject_key`, `user_id`, `proxied_account`, `request_id` and `job_id` nulled; `evidence/<id>.json` deleted; `text_state` set `none`; all three flags set; notes kept. `user_problem_jobs` rows past the ceiling deleted | The `user_id`, `proxied_account`, `ipaddr` and `ip_subject_key` filters no longer find the event; `worker_id`, `since` and `until` do. A later report of the same job no longer matches `job_id` and records a new event. Notes stay listed, and a later note is stored. |
| `User.wipe` | None | The account's events, notes and worker report records stay, with `user_id` intact. Reports its deleted workers made stay, with `worker_id` intact. |
| Any window setting | None | The privacy document and the listing's `retention` report the same values. A `none` window is `null` in `retention` and "kept with the record" in the document. |

## Evidence store

Prompt text is bulk data, so it lives in object storage and the row keeps identity, state and counts. Nothing on the
request path talks to the store: capture inserts the row with its text in state `pending`, and the quorum node's
applier (`upload_moderation_evidence_text`, every 5 seconds under a session-level advisory lock) claims up to 50
pending rows a cycle, puts each as `evidence/<id>.json` (`ContentType: application/json`), and then nulls the stage
columns and sets `stored` in one conditional update, which also records `text_sha256` and `text_chars` for a row
captured before those columns existed. Up to 10 cycles run per tick while batches fill. A failed put ends the cycle
and the tick; it and the rows after it stay pending for the next tick. Rows that retention cleared between the put
and the update are not returned by it, and the applier deletes their objects in the same cycle, so no object outlives
its row's text. Before that delete it re-reads the rows, and one that another applier run has recorded as `stored`
keeps its object. The object key is a function of the event id (`evidence_object_key`), the only key builder, so nothing
outside the `evidence/` prefix can be addressed.

| Variable | Meaning |
| -------- | ------- |
| `R2_EVIDENCE_ACCOUNT` | S3 endpoint URL of the evidence store. Required for uploads. |
| `R2_EVIDENCE_BUCKET` | Bucket name. Required for uploads; it has no default, so it can never alias an image bucket. |
| `EVIDENCE_AWS_ACCESS_KEY_ID`, `EVIDENCE_AWS_SECRET_ACCESS_KEY` | A token scoped to that bucket alone. The presigned link embeds the access key id. |
| `R2_EVIDENCE_REGION` | SigV4 region, default `auto` (R2); Garage needs its own region name. |

Unless all four of the endpoint, bucket, access key id and secret are set, the client is `None`: startup logs one
warning naming the unset variables, capture is unchanged, the applier only records its gauges, rows stay `pending`
with their text inline, and the listing returns no `text_url`. Unsetting the store after uploads keeps `stored` rows
past their windows, since their objects cannot be deleted; each retention step that meets them logs a warning with
the count kept. The
client uses 3 s connect and 10 s read timeouts with two attempts, and the bucket needs a CORS rule allowing `GET`
from the frontpage origin (`AllowedHeaders` empty; a presigned `GET` sends no custom header, so there is no
preflight). The `prompts` bucket that `upload_prompt` writes on filter rejections and the image buckets are separate
and unchanged.

## Countermeasures and levers

A rejected submission costs the submitter nothing, so storage design cannot bound a flood; the countermeasures do.

**Per subject.** `CounterMeasures.count_rejection` counts every rejection per IP subject per hour in the countermeasure
Redis (key `rejections:<subject>`, so it never collides with the bare-address suspicion keys; without that Redis, or
while it fails, the count is 0 and nothing below applies). `_handle_prompt_rejection` runs once per rejected request:

| Count in the hour | Effect | Setting |
| ----------------- | ------ | ------- |
| Above `SUBJECT_TEXT_CAP_PER_HOUR` | The event records digest, character count and identity but no text (`text_state` `none`) | `HORDE_MODERATION_SUBJECT_TEXT_CAP_PER_HOUR`, default 5 |
| Above `MODEL_REJECTION_TIMEOUT_THRESHOLD`, model rejections only | The address enters the escalating IP timeout the filter path applies on its first rejection (`CounterMeasures.report_suspicion`) | `HORDE_MODEL_REJECTION_TIMEOUT_THRESHOLD`, default 5 |
| Any, in raid mode | Every model rejection times out the address | `HordeSettings.raid` via the admin endpoint |

The filter path keeps its own timeout and account suspicion. Moderators are exempt from the timeout on both paths and
from the text cap, since they probe the filter on purpose.
The model path times out after a threshold rather than at once because its replacement is silent by design (a probe
learns nothing from a rewritten prompt), and the rejection that this counts is the one case where the prompt was
emptied, which the client already sees. Worker reports are never capped. The first time either countermeasure
applies to a subject in an hour (`claim_rejection_notice`), moderators get one Discord notice with the pseudonym
prefix, the count and the latest reason, and `horde.moderation.countermeasures` counts it by `horde.action`.
Both thresholds are positive whole numbers read at startup; a malformed value stops startup.

**Detection.** The applier samples, every tick, from the events table:

| Instrument | Meaning |
| ---------- | ------- |
| `horde.moderation.evidence.captured` (`horde.reason`, `horde.text_state`) | Events recorded |
| `horde.moderation.evidence.write_failures` | Rows that failed to insert |
| `horde.moderation.rejections_per_minute` | Events created over the last 5 minutes, per minute |
| `horde.moderation.rejecting_subjects` | Distinct `ip_subject_key` values over the same window; one or a few means the per-subject countermeasures are containing it, many means a distributed flood |
| `horde.moderation.evidence.pending_rows`, `horde.moderation.evidence.oldest_pending_seconds` | Upload backlog and its age |
| `horde.moderation.evidence.uploads`, `horde.moderation.evidence.upload_failures` | Applier outcomes |
| `horde.moderation.countermeasures` (`horde.action`) | Subjects first capped or timed out |
| `horde.moderation.retention.rows` (`horde.step`) | Rows each retention step changed or deleted: `anonymized`, `text_redacted`, `ipaddr_redacted`, `problem_jobs_deleted`, `problem_job_ipaddr_redacted` |
| `horde.moderation.retention.cycles` | Retention passes run; one tick runs between 1 and 20 |
| `horde.moderation.retention.saturation` | Retention ticks that used every catch-up pass with a full final batch; sustained, rows are falling behind their windows |
| `horde.moderation.observation_timestamp` | Unix time of the quorum node's latest sample; recording rules select the live process by it, since a former quorum node keeps exporting its last gauge values |

Operators alert on these gauges in their own deployment. A sustained rate from one or a few subjects is contained
by the per-subject countermeasures; the same rate from many subjects is a distributed flood, which only an edge
rate limit or raid mode answers. The retention tick, not the applier, records the `horde.moderation.retention.*`
counters; a tick that saturates for hours means rows are falling behind their windows.

**Levers, all manual.** Raid mode (timeout on every model rejection); the two thresholds; the IP timeout and block
endpoints under `/operations/ipaddr`; rate limits at the deployment's edge, which belong to the operator's own configuration; maintenance mode.

## Sharp edges

- **Every rejected prompt is still written once.** The row carries the text until the applier moves it, so a flood
  costs Postgres and its write-ahead log one insert per request; the countermeasures bound the flood, the store
  only keeps the table light.
- **A store outage keeps text in the database.** Rows stay `pending` with their text until the applier recovers. Past
  the shortest text window retention clears them without an upload, which the stalled-upload alert exists to catch.
- **Presigned links are bearer capabilities** for 10 minutes and embed the signing access key id; the token is
  scoped to the evidence bucket, and the API response and the object response both carry `private, no-store`.
- **The countermeasures need the countermeasure Redis.** Without it, or while it fails, the count is 0: no cap, no model-path timeout,
  no notice, as with the existing IP timeouts.

- **Evidence can be lost.** The write uses a separate transaction on a second pooled connection, so rolling back a
  rejected submission cannot erase it. A pool or database failure logs the exception type alone, never the bound
  prompt text, and preserves the rejection. Monitor those failure logs and pool capacity.
- **The rejection path takes a second pooled connection.** Rejections spike during an abuse flood, when the pool is
  already contended.
- **No foreign keys.** A rejected submission has no waiting row, and evidence must outlive request expiry and account
  deletion. Only notes reference an event, and nothing deletes an event.
- **Retention is eventual and never deletes by age.** A backlog larger than 20 batches a minute leaves text,
  addresses or identity past their windows until the tick catches up; `horde.moderation.retention.saturation` counts
  ticks that used every catch-up pass, and the maintenance loop logs failures.
  Anonymized events stay, so row count grows with rejection and problem-job volume.
  Events and worker report records under moderation action are never redacted or anonymized. Redaction and anonymization
  change live rows only and leave backups alone.
- **Rows under moderation action are rescanned on every pass.** They never get a retention flag, so the partial
  indexes keep them, and each pass reads them again before the exemption filters them out. That is why each step is
  bounded to 1,000 rows, which keeps the rescan cheap at up to 20 passes a minute.
- **Changing a window changes the disclosure.** The privacy document renders the configured values; a shorter window
  applies to existing events on the next pass, a longer one cannot restore removed values.
- **The pseudonym needs a private secret.** Without one, events carry no pseudonym and the `ipaddr` filter stops
  finding them once their address is removed. Setting a private `secret_key` later does not add pseudonyms to earlier
  events. The same secret salts stored API keys, so it never rotates.
- **Privileged accounts are recorded.** Moderator rejections and administrator problem jobs produce evidence, though
  moderators are exempt from the IP blocking and the administrator from the problem-job alerts.

`tests/integration/test_prompt_moderation.py` covers these contracts.

## Related

- [Prompt provenance reference](prompt_provenance.md)
