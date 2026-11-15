---
title: "Moderation operations reference"
summary: "Moderation evidence, retention and object storage, with API fields for event review, promotion queues and worker suspicion history."
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

The Horde records a moderation event when it rejects a prompt or receives a worker's report on a job. These events
remain available for review after the request expires or the account is deleted. An account wipe preserves the
events, their notes and worker report records. Successful prompt replacement does not create an event when the
request proceeds to generation. Moderators can list recorded events and attach review notes.

Worker suspicion reports have a separate history that remains available after a suspicion reset or worker deletion.
The moderator API lists this history along with the current promotion and paused-worker review queues.

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
| Rejection countermeasures   | `horde/countermeasures.py`, `horde/apis/v2/base.py` | `CounterMeasures.count_model_rejection`, `CounterMeasures.claim_model_rejection_notice`, `GenerateTemplate._handle_prompt_rejection` |
| Countermeasure policy       | `horde/database/prompt_moderation.py`       | `MODEL_REJECTION_TIMEOUT_THRESHOLD`, `load_positive_setting` |
| Detection gauges            | `horde/metrics.py`, `horde/database/prompt_moderation.py` | `moderation_*` instruments, `pending_text_health`, `recent_rejection_activity` |
| Worker suspicion history    | `horde/classes/base/worker.py`              | `WorkerSuspicionEvent`, written by `WorkerTemplate.report_suspicion` |
| Promotion criteria          | `horde/classes/base/user.py`                | `User.promotion_status`, `User.promotion_queue_criteria`, `PromotionStatus` |
| Review queues, history      | `horde/database/moderation.py`              | `get_moderation_overview`, `get_worker_suspicion_events` |

The schema is `sql_statements/5.1.12.txt`. Tests build the tables from the models, so
`test_orm_models_build_the_same_moderation_tables_as_the_migration` in
`tests/integration/test_moderation_migration.py` checks that the models and the migration produce the same columns,
defaults, constraints and indexes.

## Recorded events

An event holds the account, any proxied account, the submitting address and its pseudonym, the requested model names,
whichever request, job and worker identifiers exist, the reason, the outcome, and the known prompt stages. For an
origin that is not an address, the event holds the origin text in place of the address and pseudonym.

| Reason             | Outcome    | Raised by                                                  |
| ------------------ | ---------- | ---------------------------------------------------------- |
| `filter_rejection` | `rejected` | The prompt filter flagged the prompt and the replacement filter was off, emptied the prompt, or refused it because its positive part was longer than 7,000 characters. The replacement filter is on when the request sets `replacement_filter` or the account is in education mode |
| `model_rejection`  | `rejected` | The NSFW-model replacement emptied the prompt, for an NSFW model or for a flagged account on any model |
| `worker_csam`      | `censored` | An image worker reported the job                            |

A `worker_csam` event records that a worker reported the job. It does not establish a finding about the account.
Workers do not scan the input images of a chat request. A `worker_csam` event therefore cannot come from the chat
vision path, and those images are not screened.

### Request origins

The address is the IP subject (`CounterMeasures.ip_subject`) derived from the origin of the rejected submission or the
waiting request. The same function normalizes worker report records, the `ipaddr` filter and worker IP blocks:

| Origin                                            | Recorded as                         |
| ------------------------------------------------- | ----------------------------------- |
| IPv4 address                                      | The address                         |
| IPv4-mapped IPv6 address (`::ffff:a.b.c.d`)       | The IPv4 address                    |
| IPv6 address, or IPv6 network of /64 or narrower  | Its /64 network, since IPv6 clients rotate addresses inside it |
| Blank                                             | `null`                              |
| Anything else, such as free text in `Proxied-For` or a wider network | `null`, without a pseudonym. The text is kept as `origin_text` (a blank value is not kept) |

A value without an IP subject is not used as a countermeasure key and is not blocked. Blocking a worker with such an
origin does not set a timeout and logs a warning. Capture keeps the origin text in `origin_text` on the event and the
worker report record, clipped to 255 characters (`MAX_ORIGIN_TEXT_CHARACTERS`). `origin_text` is `null` when the
origin has an IP subject.

Capture also stores `ip_subject_key`, the address pseudonym. It is an HMAC-SHA256 over a moderation label and the
subject, keyed with the deployment's `secret_key`. Equal subjects have equal pseudonyms. The pseudonym outlives the
address, so an address filter still matches an event after its address is removed.

The IPv4 space is small enough to enumerate, so anyone who knows a published secret can reverse a pseudonym to its
address. When `secret_key` is unset, or equals the `.env_template` placeholder or the fallback of `hash_api_key`,
startup logs one warning and events are captured without a pseudonym.

`models` lists the requested models. It does not identify the model used by a worker.

### Prompt stages and storage state

| Stage               | Meaning                                              | Rejection | `worker_csam`                     |
| ------------------- | ---------------------------------------------------- | --------- | --------------------------------- |
| `submitted_prompt`  | The original submission, before styles or moderation | Recorded  | Recorded when the request still held it |
| `moderation_prompt` | The text evaluated by moderation, after styles       | Recorded  | `null`                            |
| `effective_prompt`  | The prompt received by the worker                    | `null`    | Recorded                          |

A stage that is unknown or does not exist is `null`. It is not copied from another stage. A rejected submission does
not have a waiting-request ID, since a row was not created. A worker report cannot recover the styled pre-moderation
prompt, so `moderation_prompt` stays `null`. A rejection does not have an effective prompt. The rejection happens
either before any replacement runs or after a replacement empties the prompt. A replacement that produces text lets the
request through without an event. The text refused by a rejection is `moderation_prompt`.

Each stage is clipped to 16,000 characters, with a `text_truncated` flag. The limit applies only to evidence. The
original submission on the waiting request keeps its full length. `job_id` is unique, so a repeated worker report for
one job resolves to the existing event and does not create a second row. A rejected attempt does not have a request
ID, so each attempt is a separate row.

A quorum node is the Horde API replica selected to run this shared maintenance task.
The stages are held in the row only until that node uploads them (see [Evidence store](#evidence-store)).
`text_state` shows where the text is: `pending` (in the row, awaiting upload), `stored` (in the evidence bucket at
`evidence/<id>.json`, with the row's stage columns null) or `none` (the text is not stored anywhere, because no stage
existed or retention cleared it). `text_sha256` is the SHA-256 of the canonical object (the three clipped stages as
compact JSON with sorted keys), and `text_chars` is the sum of the clipped stage lengths. Both are recorded whenever a
stage existed, and both are removed with the text.

Captured values change only through scheduled retention of text, address and identity (see [Retention](#retention)).
A moderator can attach notes to an event, including an anonymized one. A note is a separate row that is not deleted,
and it exempts its event from retention (see [Moderation action](#moderation-action)).

### Problem-job alerts

Worker reports also write `user_problem_jobs`, and the problem-job Discord alerts count that table. The account alerts
count reports over the past hour or day. The IP alerts count reports over the past hour or day for one IP subject. A
report whose origin does not have an IP subject is counted for its account only, since records without an address
cannot be told apart by IP. [Worker report retention](#worker-report-retention) covers how long the records are kept.

An alert identifies its subject: the account alias, or the IP subject, shown as the first 12 hex characters of its
pseudonym with the latest account. The alert also lists the count over the window, the threshold, the mute period, the
latest job, worker and models, the LoRA names, and the event ID of the evidence just captured. It does not include
prompt text or an address. A Discord message outlives the evidence retention window and cannot be redacted. The sender logs non-success HTTP responses and connection failures. Inspect those logs when an alert is
missing; prompt length does not contribute to the alert payload.

If `HORDE_MODERATION_FRONTPAGE_URL` is set to an absolute HTTP URL, the alert links to
`<url>/admin/review?tab=prompts&event_id=<id>`. Otherwise it says to open the event in the frontpage Prompts tab. When
the evidence write failed, the alert says so in place of the event ID and does not have a review line. Under a
placeholder secret a pseudonym is not computed, so the alert does not identify the IP subject.

## Moderator API

All paths below are under `/api/v2/operations/moderation`.

### Conventions

Every endpoint requires the moderator `apikey` header and responds with `Cache-Control: private, no-store`. Every
returned timestamp is ISO 8601 in UTC with an explicit `+00:00` offset. Submitted times must include an explicit
timezone. Responses are built from explicit field lists, since these tables hold prompt text and a future column must
not appear in the API by accident.

Rate limits count per API key and method (`get_request_api_key_per_method`). They do not count per address, since
moderators share office addresses and VPN exits. Each endpoint allows 120 reads (`GET`) and 30 writes (`POST`) a
minute. Each limit is scoped to its endpoint and not to the concrete path, so notes on every event share one budget. A
request without the header counts against the anonymous key's bucket for that endpoint and is refused for the missing
header, so keyless traffic cannot spend a moderator's allowance.

Every response lists `Retry-After`,
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` in `Access-Control-Expose-Headers`, so a browser
client can read how long to wait after a `429`.

### Events and notes

`GET /prompts` lists events with filters and cursor pagination. `POST /prompts/<id>/notes` adds a moderator note.

#### Event filters

| Filter | Handling |
| --- | --- |
| `event_id` | Selects one event, including an ID from an alert's review link. A value below 1 returns `400 InvalidModerationEventID`. |
| `user_id`, `proxied_account`, `worker_id` | Selects events for the account, proxied account or reporting worker. |
| `reason` | Repeatable. Selects any listed reason. An unrecognized reason returns `400 InvalidModerationReason`. |
| `since`, `until` | Inclusive start and exclusive end. Both require a timezone, and `since` must be earlier than `until`. Invalid bounds return `400 InvalidModerationTimeRange`. |
| `ipaddr` | An address or IPv6 network of /64 or narrower, normalized to its IP subject. IPv6 addresses select their /64, and IPv4-mapped addresses select their IPv4 address. A blank value does not filter. A value that is not a single subject returns `400 InvalidModerationAddressFilter`. |
| `ip_subject_key` | An exact pseudonym from the listing, as 64 lowercase hex characters. Any other format returns `400 InvalidModerationAddressFilter`. |

The `ipaddr` filter matches by pseudonym until anonymization, so an event still matches after address retention
removes its address. An event without a pseudonym matches by its address instead.

#### Event response

Each event includes identity, reason and outcome, the correlation fields except the internal `job_id`, and notes
ordered oldest first. `ip_subject_key` is the pseudonym shared by the events of one subject. It is not an address.
Anonymized events have `null` for `user_id` and `ip_subject_key`.

Text fields are `text_state`, the three prompt stages, `text_url`, `text_sha256`, `text_chars` and `text_truncated`.
The stages are inline only while `pending`. While `stored`, `text_url` is a presigned object link valid for
10 minutes, signed without a network call. It is `null` in other states and whenever an evidence store is not
configured. The store returns `403` for an expired link. Refetch the event by `event_id` for a fresh link.

Retention fields are `text_redacted`, `ipaddr_redacted`, `anonymized` and
[`hold_source`](#moderation-action). Each retention flag is set after its step runs, even if a value did not need
removing. The page's `retention` object reports the active policy: `text_days`, `ipaddr_days` and `ceiling_days`.
A window configured as `none` is returned as `null`.

#### Pagination

`limit` accepts 1 to 100, default 50. `before_id` is an exclusive cursor in descending ID order.
The response includes `next_cursor`, or `null` at the end. Invalid pagination returns
`400 InvalidOperationsLimit` or `400 InvalidOperationsCursor`.

#### Notes

`POST /prompts/<id>/notes` accepts `{note}` with 1 to 2,000 characters, nonblank and without NUL.
Invalid text returns `400 InvalidModerationNote`. The response includes the note, reviewer attribution and
creation time. Notes on anonymized events are stored normally. A missing event returns `404 ModerationEventNotFound`.

### Promotion and paused-worker review

`GET` on the base path returns `promotion_threshold` (the kudos trust threshold, `null` when one is not configured),
`suspicion_threshold` (`User.SUSPICION_THRESHOLD`), `worker_suspicion_threshold` (`WorkerTemplate.suspicion_threshold`),
`promotion_blocked_users`, `promotion_eligible_users`, `paused_workers`, and `paused_workers_total`. `limit` 1-500
(default 100) applies independently to each queue. Each promotion row includes the account's kudos, age and worker
totals, and its current suspicion reasons (`id`, `name`, `description`, `count`).

The automatic promotion criteria are: not yet trusted, above the kudos trust threshold, at least
`User.PROMOTION_MIN_ACCOUNT_AGE` (seven days) old, and not anonymous. An account that meets every criterion but has
reached `User.SUSPICION_THRESHOLD` suspicions is blocked. An account below that count is eligible. Without a configured
trust threshold both promotion lists are empty. Each queue is ordered by evaluating kudos (highest first) and then
account ID. [Promotion queue queries](#promotion-queue-queries) covers how the criteria are kept in step with automatic
promotion.

Paused rows include the worker and owner context, models, and current suspicion reasons in the same shape. Filters
are repeatable `worker_type` (`image`, `text`, `interrogation`), repeatable `suspicion_id`, and `online` (check-in
within five minutes). Sort by `last_check_in` (default), `suspicion`, `name`, or `owner`. `paused_workers_total`
counts all matches before the limit. An unknown `suspicion_id` returns `400 InvalidSuspicionID`.

### Suspicion history listing

`GET /worker_suspicion_events` filters by worker UUID (`worker_id`) or owning account (`user_id`). It returns `id`,
`created`, `worker_id`, `worker_name`, `user_id`, `suspicion_id`, `reason` and `detail`, newest first, with `limit`
1-500 (default 100) and the same `before_id` cursor as `/prompts`. A `worker_id` that is not a UUID returns
`400 InvalidWorkerID`.

## Worker suspicion history

`worker_suspicions` is the resettable current score. Its rows cascade away with the worker, and a moderator reset
clears them. `worker_suspicion_events` is the history. `report_suspicion` appends one row per newly recorded
suspicion, with the worker's id and name, the owning account, the suspicion reason and the formatted diagnostic. The
name column is limited to `MAX_WORKER_NAME_CHARACTERS` (100), the name length of the workers table. The history table
does not have foreign keys, so deleting the worker or resetting its suspicion leaves the rows in place, and `User.wipe`
does not delete them. `report_suspicion` does not add another row for a non-accumulating reason that is already
recorded on the worker.

## Retention

The [retention policy constants](service_limits.md#trust-moderation-and-retained-history) collect windows,
exemptions and capture bounds. [Evidence-processing controls](operational_constants.md#moderation-processing-and-evidence-storage)
explain the batch and storage budgets that can delay a backlog beyond those windows.

Retention removes prompt text, addresses and identifying fields on separate schedules measured from capture.
The event row remains for aggregate reporting. These schedules and the final anonymization deadline apply to events
that are not exempt under [Moderation action](#moderation-action):

| Variable                                  | Default | Removes at that age                                   | `none`                  |
| ----------------------------------------- | ------- | ----------------------------------------------------- | ----------------------- |
| `HORDE_MODERATION_TEXT_RETENTION_DAYS`    | `none`  | The three prompt stages, the stored object, `text_sha256` and `text_chars`. Sets `text_redacted`, and sets `text_state` to `none` | Kept until the ceiling  |
| `HORDE_MODERATION_IPADDR_RETENTION_DAYS`  | 30      | `ipaddr` (the pseudonym stays). Sets `ipaddr_redacted` | Kept until the ceiling  |
| `HORDE_MODERATION_EVIDENCE_CEILING_DAYS`  | 365     | Identity. Sets all three flags (below)                | Refused                 |

The ceiling anonymizes the event. It clears `user_id`, `proxied_account`, `ipaddr`, `ip_subject_key`, `request_id`,
`job_id`, the prompt stages, the stored object, `text_sha256` and `text_chars`. It keeps `reason`, `outcome`,
`models`, `created`, `text_truncated`, the notes and `worker_id`. `worker_id` identifies the reporting worker and not
the subject. Counts by reason, outcome, model and worker therefore stay derivable from the rows.

### Moderation action

Every retention step skips an event under moderation action, without a cap, for as long as the condition holds.
`_under_moderation_action` is the single definition. An event is under moderation action when any of these holds:

| Condition | Source |
| --------- | ------ |
| The event has at least one note | `prompt_moderation_notes.event_id` |
| The event's account is flagged | `user_roles` row `FLAGGED` with `value` true |
| The event's account is suspicious: not trusted, and at least `User.SUSPICION_THRESHOLD` (5) suspicions | `user_roles` row `TRUSTED` and the count of `user_suspicions`, with the same rule as `User.is_suspicious` |

Worker report records use the account conditions only (`_account_restricted`). The records exist to identify accounts
attempting to generate illegal content and to enforce the restrictions that follow. Those restrictions cannot be
enforced once the record is gone. The ceiling bounds only unactioned events. See
[ADR 0015](../decisions/0015-tiered-moderation-evidence-retention.md).

The exemption is evaluated at each pass. Once an event does not have a note and its account is neither flagged nor
suspicious, the next pass applies every window that has already elapsed. Values already removed stay removed if the
account is flagged later.

The event listing reports the exemption in `hold_source`: `note` when the event has a note, `account_status` when its
account is flagged or suspicious, `note` when both hold, and `null` when neither does. The listing query derives it
from the predicates combined by `_under_moderation_action`, so it matches the result of the next retention pass at the
time of the read. It is not stored in a column. A hold cannot be released by hand. It ends only when its condition
no longer applies.

### Retention configuration

The variables are read once at startup into `MODERATION_RETENTION_POLICY`. The retention pass and the privacy
document both read that policy when they run. A window is a positive whole number of days or `none` (any case). The
ceiling is a positive whole number of days, at most 365, and cannot be disabled. A window longer than the ceiling,
or any malformed value, raises `ValueError` at import and stops startup.

### Worker report retention

Worker report records (`user_problem_jobs`) follow the same address window and ceiling. Past the address window their
`ipaddr` and `origin_text` are nulled. Past the ceiling the record is deleted, unless the account is flagged or
suspicious. The records do not have a foreign key to the reporting worker, so deleting a worker, including through an
account wipe, keeps the reports it made. Their alerts count only the last hour or day, so neither step changes an
alert.

### Retention loop

The quorum node runs the retention tick every minute (`apply_moderation_retention`). A tick
repeats passes while any step changes a full batch, up to 20 passes (`RETENTION_MAX_CATCHUP_CYCLES`). Each step can
therefore drain 20,000 rows a minute, and a backlog of millions clears in hours. A pass anonymizes, then removes text,
then removes addresses. Each step changes at most 1,000 events (`CLEANUP_BATCH_SIZE`), oldest first, in its own
transaction.

The two steps that clear text lock their batch and delete the objects of every row in it with one bulk call. A missing
key counts as deleted, so a row that is still `pending` or already `none` does not cost anything. Only the rows whose
delete succeeded are flagged. When the store fails or a store is not configured, `stored` rows stay unflagged for the
next pass and every other row proceeds.

A store outage therefore does not block the ceiling, or the text step for rows
without a stored object. Without a configured store, the warning that `stored` rows are waiting is logged at most once
an hour. A `pending` row flagged during an outage is safe. If an upload stores the object of a cleared row, the
upload's conditional update skips the row and the applier deletes the object.

Two steps then handle worker report records the same way. One deletes at most 1,000 records past the ceiling. The
other nulls the address of at most 1,000 records past the address window. When `HORDE_WORKER_SUSPICION_RETENTION_DAYS`
is set, a last step deletes at most 1,000 worker suspicion history rows past that window, oldest first, unless the
owning account is flagged or suspicious. [Retention queries](#retention-queries) lists the index behind each step.

### Worker suspicion retention

Worker suspicion history (`worker_suspicion_events`) is worker diagnostics, so it does not have a text or address
window, and the ceiling does not apply. It is kept indefinitely unless `HORDE_WORKER_SUSPICION_RETENTION_DAYS` is a
positive whole number of days, at most 36,500 (`MAX_WORKER_SUSPICION_RETENTION_DAYS`). With such a value, rows older
than the window are deleted, except rows whose owning account is flagged or suspicious. The bound keeps the pass's
cutoff inside the date range. A longer window would be no different from leaving the variable unset. `none`, a value
above the bound, or any malformed value raises `ValueError` at import and stops startup. When the variable is unset,
the table grows with suspicion volume.

### Account deletion and privacy disclosure

Wiping an account (`User.wipe`) does not change events, notes or worker report records. They keep their `user_id`, so
the account's records stay listed and filterable, and retention applies to them as to any other account's records.

The privacy document (`/api/v2/documents/privacy`) renders the same evidence windows in its "Moderation records"
section. It links the source code at `HORDE_REPOSITORY` (`horde.vars.horde_repository`, default
`https://github.com/Haidra-Org/AI-Horde`). It also states what the worker suspicion history records and its retention
period, taken from the same policy (`RetentionPolicy.worker_suspicion_days`).

### Retention effects

`EVENT_COLUMN_RETENTION` in `horde/database/prompt_moderation.py` is the source of truth for which columns each step
clears. The steps build their updates from it, and startup fails when an event column does not have a fate.
`test_every_event_column_has_a_retention_fate` in `tests/unit/test_moderation_retention_policy.py` checks its
coverage, and `test_anonymization_removes_identity_and_keeps_the_record_and_its_notes` checks its effect. Each entry
below applies only to rows that are not under [moderation action](#moderation-action).

| Setting or event | Columns and tables changed | Readers affected |
| ---------------- | -------------------------- | ---------------- |
| Text window | `submitted_prompt`, `moderation_prompt`, `effective_prompt`, `text_sha256` and `text_chars` nulled. `evidence/<id>.json` deleted. `text_state` set to `none`. `text_redacted` set | The listing returns null stages and a null `text_url`. Filters do not use the text. |
| Address window | `ipaddr` and `origin_text` nulled. `ipaddr_redacted` set. `ip_subject_key` kept. `user_problem_jobs.ipaddr` and `user_problem_jobs.origin_text` nulled | The listing returns a null `ipaddr` and `origin_text`, and the same `ip_subject_key`. The `ipaddr` filter, including another address in the same IPv6 /64, still matches an event with a pseudonym. An event without one stops matching it. Problem-job alerts count the last hour or day, so they are unaffected. |
| Ceiling | Text and address columns, `text_sha256`, `text_chars`, `ip_subject_key`, `user_id`, `proxied_account`, `request_id` and `job_id` nulled. `evidence/<id>.json` deleted. `text_state` set to `none`. All three flags set. Notes kept. `user_problem_jobs` rows past the ceiling deleted | The `user_id`, `proxied_account`, `ipaddr` and `ip_subject_key` filters stop matching the event. The `worker_id`, `since` and `until` filters still match it. A later report of the same job does not match `job_id` and records a new event. Notes stay listed, and a later note is stored. |
| `User.wipe` | None | The account's events, notes and worker report records stay, with `user_id` intact. Reports made by its deleted workers stay, with `worker_id` intact. |
| Worker suspicion window | `worker_suspicion_events` rows older than the window deleted, unless the owning account is flagged or suspicious | The rows leave `/worker_suspicion_events`. When the window is unset, rows are not deleted. The privacy document states the window, or that the history is kept indefinitely. |
| Any evidence window setting | None | The privacy document and the listing's `retention` report the same values. A `none` window is `null` in `retention` and "kept with the record" in the document. |

## Evidence store

Prompt text moves to object storage after capture so a slow upload does not delay the request. The database row
retains identity, state and counts. Capture first inserts the text in a row with state `pending`. The quorum node's
applier (`upload_moderation_evidence_text`, every 5 seconds under a session-level advisory lock) claims up to 50
pending rows a cycle and puts each as `evidence/<id>.json` (`ContentType: application/json`). It then nulls the stage
columns and sets `stored` in one conditional update. That update also records `text_sha256` and `text_chars` for a row
captured before those columns existed. Up to 10 cycles run per tick while batches fill.

A failed put ends the cycle and the tick. That row and the remaining rows stay pending for the next tick. The update
does not return rows cleared by retention between the put and the update. The applier deletes their objects in the
same cycle, so an object does not outlive its row's text. Before that delete the applier re-reads the rows, and a row
that another applier run has recorded as `stored` keeps its object.

The object key is a function of the event id (`evidence_object_key`), the only key builder, so objects outside the
`evidence/` prefix cannot be addressed.

### Store configuration

| Variable | Meaning |
| -------- | ------- |
| `R2_EVIDENCE_ACCOUNT` | S3 endpoint URL of the evidence store. Required for uploads. |
| `R2_EVIDENCE_BUCKET` | Bucket name. Required for uploads. It does not have a default, so it cannot alias an image bucket. |
| `EVIDENCE_AWS_ACCESS_KEY_ID`, `EVIDENCE_AWS_SECRET_ACCESS_KEY` | A token scoped to that bucket alone. The presigned link embeds the access key id. |
| `R2_EVIDENCE_REGION` | SigV4 region, default `auto` (R2). Garage needs its own region name. |

Unless the endpoint, bucket, access key id and secret are all set, the client is `None` and startup logs a warning
listing the unset variables. Capture continues, the applier only records gauges, and rows stay `pending` with inline
text. The listing does not return a `text_url`.

Unsetting the store after uploads keeps `stored` rows past their windows, because their objects cannot be deleted.
Each retention step logs a warning with the retained count.

The client uses 3 s connect and 10 s read timeouts with two attempts. The bucket needs a CORS rule allowing `GET`
from the frontpage origin with empty `AllowedHeaders`. A presigned `GET` does not send custom headers, so it does not
require a preflight. The image buckets, and the `prompts` bucket written by `upload_prompt` on filter rejections, are
separate. The evidence store does not use or change them.

## Countermeasures and levers

A rejected submission is not charged, so storage design alone cannot bound a flood. Countermeasures limit repeated
rejections.

### Per-subject limits

`CounterMeasures.count_model_rejection` counts model rejections per IP subject per hour in the countermeasure Redis.
The key is `model_rejections:<subject>`, so it does not collide with the bare-address suspicion keys. Without that
Redis, or while it fails, the count is 0 and only raid mode times out addresses. Filter rejections are not counted,
since the filter path times out the address on its first rejection. `_handle_prompt_rejection` records the evidence of
every rejection. For a model rejection it then applies these rules:

| Model rejections in the hour | Effect | Setting |
| ---------------------------- | ------ | ------- |
| Above `MODEL_REJECTION_TIMEOUT_THRESHOLD` | The address enters the escalating IP timeout that the filter path applies on its first rejection (`CounterMeasures.report_suspicion`) | `HORDE_MODEL_REJECTION_TIMEOUT_THRESHOLD`, default 5 |
| Any, in raid mode | Every model rejection times out the address | `HordeSettings.raid` via the admin endpoint |

The filter path keeps its own timeout and account suspicion. Moderators are exempt from the timeout on both paths,
since they probe the filter on purpose, and their model rejections are not counted.

The model path times out after a threshold, and not at once, because its replacement is silent by design. Someone
probing the filter does not learn anything from a rewritten prompt. The rejection counted here is the one case where
the prompt was emptied, which is already visible to the client. Every event keeps its text until retention removes it.
The first model-path timeout of a subject in an hour (`claim_model_rejection_notice`) sends moderators one Discord
notice with the pseudonym prefix, the count, the latest event and its review link. `horde.moderation.countermeasures`
counts it with `horde.action` `timeout`.

The threshold is a positive whole number read at startup. A malformed value stops startup.

### Detection metrics

The applier samples the events table every tick:

| Instrument | Meaning |
| ---------- | ------- |
| `horde.moderation.evidence.captured` (`horde.reason`, `horde.text_state`) | Events recorded |
| `horde.moderation.evidence.write_failures` | Rows that failed to insert |
| `horde.moderation.rejections_per_minute` | Events created over the last 5 minutes, per minute |
| `horde.moderation.rejecting_subjects` | Distinct `ip_subject_key` values over the same window. One or a few means the per-subject countermeasures are containing the rejections. Many means a distributed flood |
| `horde.moderation.evidence.pending_rows`, `horde.moderation.evidence.oldest_pending_seconds` | Upload backlog and its age |
| `horde.moderation.evidence.uploads`, `horde.moderation.evidence.upload_failures` | Applier outcomes |
| `horde.moderation.countermeasures` (`horde.action`) | Subjects first timed out for model rejections in the hour. `horde.action` is always `timeout` |
| `horde.moderation.retention.rows` (`horde.step`) | Rows each retention step changed or deleted: `anonymized`, `text_redacted`, `ipaddr_redacted`, `problem_jobs_deleted`, `problem_job_ipaddr_redacted`, `worker_suspicion_deleted` |
| `horde.moderation.retention.cycles` | Retention passes run. One tick runs between 1 and 20 |
| `horde.moderation.retention.saturation` | Retention ticks that used every catch-up pass with a full final batch. A sustained count means rows are falling behind their windows |
| `horde.moderation.observation_timestamp` | Unix time of the quorum node's latest sample. Recording rules select the live process by it, since a former quorum node keeps exporting its last gauge values |

Operators alert on these gauges in their own deployment. The per-subject countermeasures contain a sustained rate
from one or a few subjects. The same rate from many subjects is a distributed flood, and needs an edge rate limit or
raid mode. The retention tick records the `horde.moderation.retention.*` counters. Saturation for hours means rows are
falling behind their windows.

### Manual controls

Operators can enable raid mode, change the rejection threshold, or use the IP timeout and block endpoints under
`/operations/ipaddr`. Edge rate limits are part of deployment configuration. Maintenance mode is also available.

## Implementation notes

### Event listing indexes

Each subject filter uses its own index, newest `id` first: `(user_id, id)`, `(proxied_account, id)`, `(worker_id, id)`
and `(ip_subject_key, id)`. The last three are partial on a non-null value. A partial `(ipaddr, id)` index covers
events without a pseudonym, for the `ipaddr` fallback. That index stays small while the deployment has a private
secret. The `reason` filter uses `(reason, id)`, so a page of a rare reason does not walk the events of the common ones.

### Promotion queue queries

The promotion criteria have two forms side by side in `horde/classes/base/user.py`. `User.promotion_status` is the
Python decision used by `check_for_trust` to promote. `User.promotion_queue_criteria` is the SQL filter for one queue.
`test_promotion_queues_match_check_for_trust` checks that each queue holds exactly the accounts that `check_for_trust`
would promote now, or would promote once their suspicions were cleared, including the threshold and suspicion
boundaries. A change to either form must be made to both.

Each queue is one query, limited to `limit` rows. The query walks `ix_users_evaluating_kudos_rank`
(`users (evaluating_kudos DESC, id) WHERE evaluating_kudos > 0`) from the threshold upward and checks trust, age, the
anonymous account and the suspicion count per row. Its cost therefore depends on the number of accounts above the
threshold. It does not depend on the size of `users`. The blocked queue is usually short, so its query normally scans
that whole range. Roles, suspicions and workers are loaded only for the returned rows. A negative threshold does not
imply the index predicate, so the query cannot use the index.

### Retention queries

Each event step uses a partial index on `(created, id)` that excludes the events the step has already finished. A pass
therefore does not rescan finished rows, and a repeated pass does not change them. The worker report steps use the
`created` index of `user_problem_jobs` for the ceiling and `ix_user_problem_jobs_ipaddr_pending`
(`WHERE ipaddr IS NOT NULL OR origin_text IS NOT NULL`) for the address window. The worker suspicion step uses
`ix_worker_suspicion_events_created`.

Each step repeats its full condition, including the moderation-action exemption, in the `UPDATE` or `DELETE` itself
as well as in the subquery that selects the batch.

## Sharp edges

- **Every rejected prompt is still written once.** The row holds the text until the applier moves it, so a flood
  costs Postgres and its write-ahead log one insert per request. The countermeasures bound the flood. The store only
  keeps the table light.
- **A store outage keeps text in the database.** Rows stay `pending` with their text until the applier recovers. Past
  the shortest text window, retention clears them without an upload. The stalled-upload alert exists to catch this.
- **Presigned links are bearer capabilities** for 10 minutes and embed the signing access key id. The token is
  scoped to the evidence bucket, and the API response and the object response both include `private, no-store`.
- **The model-path timeout needs the countermeasure Redis.** Without it, or while it fails, the count is 0. Outside
  raid mode the model path then does not time out an address and does not send a notice. The existing IP timeouts
  have the same dependency.
- **Evidence can be lost.** The write uses a separate transaction on a second pooled connection, so rolling back a
  rejected submission cannot erase it. A pool or database failure logs only the exception type and does not log the
  bound prompt text. The rejection still stands. Monitor those failure logs and pool capacity.
- **The rejection path takes a second pooled connection.** Rejections spike during an abuse flood, when the pool is
  already contended.
- **No foreign keys.** A rejected submission does not have a waiting row, and evidence must outlive request expiry and
  account deletion. Only notes reference an event, and events are not deleted.
- **Retention is eventual and does not delete by age.** A backlog larger than 20 batches a minute leaves text,
  addresses or identity past their windows until the tick catches up. `horde.moderation.retention.saturation` counts
  ticks that used every catch-up pass, and the maintenance loop logs failures. Anonymized events stay, so the row count
  grows with rejection and problem-job volume. Events and worker report records under moderation action are not
  redacted or anonymized. Redaction and anonymization change live rows only and leave backups alone.
- **Rows under moderation action are rescanned on every pass.** They do not get a retention flag, so they stay in the
  partial indexes ([Retention queries](#retention-queries)), and each pass reads them again before the exemption
  filters them out. That is why each step is limited to 1,000 rows, which keeps the rescan cheap at up to 20 passes a
  minute.
- **Changing a window changes the disclosure.** The privacy document renders the configured values. A shorter window
  applies to existing events on the next pass. A longer one cannot restore removed values.
- **The pseudonym needs a private secret.** Without one, events do not include a pseudonym, and the `ipaddr` filter
  stops matching them once their address is removed. Setting a private `secret_key` later does not add pseudonyms to
  earlier events. The same secret salts stored API keys, so it must not be rotated.
- **Privileged accounts are recorded.** Moderator rejections and administrator problem jobs produce evidence, though
  moderators are exempt from the IP blocking and the administrator from the problem-job alerts.
- **Worker history is committed with the owner's suspicion.** Unlike prompt evidence, the suspicion event is added to
  the request's session, and `User.report_suspicion` commits it right after adding the owner's suspicion. When the owner
  is anonymous or already has a non-accumulating reason, `User.report_suspicion` returns without committing, and
  the event is committed or rolled back with the rest of the request's session.

`tests/integration/test_prompt_moderation.py` and `tests/integration/test_moderation_operations.py` cover these
contracts.

## Related

- [Prompt provenance reference](prompt_provenance.md)
