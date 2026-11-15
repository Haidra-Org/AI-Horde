---
title: "Operational constants"
summary: "Process budgets, scheduling and cache intervals, projection batches, retention controls and storage timeouts, with implementation references and rationale."
topics: [operations, requests, workers, accounting, moderation]
order: 6
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Operational constants

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [accounting](../topics.md#accounting), [moderation](../topics.md#moderation), [operations](../topics.md#operations), [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

These values bound server work, cache staleness, background transactions and recovery. They help operators interpret
lag and contributors understand the effects of changing a number. Defaults can be overridden only where a setting
is identified. For client and worker deadlines, payload limits, admission and retention policy, see
[Service limits and behavioral constants](service_limits.md).

An **unrecorded** rationale means the reviewed source does not explain the exact numerical choice. Timing intervals
are scheduling inputs, not measured service guarantees: execution time, contention and failures can extend them.
The tables name defining files and symbols, including functions that contain inline values.

## Process capacity and connections

### Native API and Redis

| Property | Value and scope | Definition and consumers | Purpose and rationale |
| --- | --- | --- | --- |
| Native listener | Port 7001; 45 Waitress threads; connection limit 1,024 | `horde/argparser.py`: `arg_parser` options `--port`, `--waitress_threads`, `--waitress_connection_limit`; `server.py` | Bounds native HTTP capacity. Exact defaults are unrecorded; waiting chat connections run in a separate process to preserve worker request threads |
| PostgreSQL connection pool | 15 persistent connections plus 5 overflow; 30 s pool wait; pre-ping off | `horde/flask.py`: `create_app`, `SQLALCHEMY_ENGINE_OPTIONS`; `SQLALCHEMY_POOL_SIZE`, `SQLALCHEMY_MAX_OVERFLOW`, `SQLALCHEMY_POOL_TIMEOUT`, `SQLALCHEMY_POOL_PRE_PING` | Bounds connections per replica and surfaces pool exhaustion rather than allowing unbounded overflow into PostgreSQL |
| Front listener | Port 7002 | `horde/oai/settings.py`: `FrontSettings.port`; `horde/oai/__main__.py` | Separate process and listener immediately after the native port |
| Redis socket budgets | Read 5 s, connect 3 s by default | `horde/redis_ctrl.py`: `redis_socket_timeout_seconds`, `redis_socket_connect_timeout_seconds`, `_redis_client`; `REDIS_SOCKET_TIMEOUT`, `REDIS_SOCKET_CONNECT_TIMEOUT` | Prevents stalled Redis sockets holding native threads, database connections and locks indefinitely |
| Redis databases | 0 main state, 1 limiter, 2 address checks, 3 cache, 4 address suspicion, 5 timeouts, 6 local cache, 7 chat streams | `horde/redis_ctrl.py`: database variables, `get_local_horde_db`, `get_chat_signal_db`; `FrontSettings.stream_redis_db` defaults to 7 | Separates stores. Pub/sub is host-wide; stream keys require the same logical database on writer and reader |
| Local cache lifetimes | Expiring writes use the shorter of their TTL and 5 s locally; writes without expiry use 10 s locally | `horde/horde_redis.py`: `LOCAL_CACHE_MAX_LIFETIME`, `LOCAL_CACHE_SET_LIFETIME`, `HordeRedis.horde_r_setex`, `HordeRedis.horde_r_set` | Bounds local staleness. Eligibility declaration-generation reads bypass this cache so a changed declaration is visible promptly |

### Front connections and cancellation

`horde/oai/settings.py`, `FrontSettings`, accepts `HORDE_OAI_` environment overrides. Connection implementation
constants are in `horde/oai/horde_client.py`; cancellation tracking is in `horde/oai/app.py`.

| Setting or symbol | Default/value | Purpose and interactions |
| --- | --- | --- |
| `native_max_connections`, `HTTP_MAX_CONNECTIONS` | 200 | Main native HTTP pool; must exceed the submit concurrency allowance so status/model calls retain capacity |
| `SUBMIT_MAX_CONCURRENT_CALLS` | 32 | Separate submit semaphore prevents slow submissions occupying the whole pool |
| `CANCEL_MIN_CONNECTIONS`, `INFLIGHT_REQUESTS_PER_CANCEL_CONNECTION` | `max(16, max_inflight // 20)` connections | `cancel_max_connections` sizes a separate cancel pool, so cancellation can proceed while the main pool is busy |
| `HTTP_CONNECT_TIMEOUT_SECONDS`, `HTTP_READ_TIMEOUT_SECONDS` | 5 s, 30 s | Native-call transport bounds, further constrained by caller budgets |
| `UNHEALTHY_URL_BACKOFF_SECONDS` | 3 s | Temporarily avoids a failed Horde instance before trying it again |
| `drain_agent_poll_seconds`, `DRAIN_AGENT_TIMEOUT_FRACTION` | 1 s; half the polling interval | Bounds drain-agent calls so polls do not overlap |
| `cancel_timeout_seconds` | 10 s | Separate bounds for obtaining a cancel slot and making the cancel HTTP call; they can add together |
| `cancel_retry_seconds` | 30 s | Retries failed cancellation while the front runs, because abandoned work may still be assigned and charged |
| `MAX_FAILED_CANCELS` | 10,000 tracked failed cancels/process | Bounds retry tracking memory; exact cap rationale is unrecorded |
| `HORDE_CHAT_REQUEST_EXPIRY_SECONDS` | 1,200 s | Age since `FrontState.open_request` registered the submitted request, after which the front drops a failed-cancel retry. This duplicates the ordinary native lifetime; native refreshes can extend request existence |
| `shutdown_drain_seconds`, `shutdown_cancel_seconds` | 30 s, 15 s | Bounds graceful response drain and then process-wide cancellation; the latter is not a fresh budget per request |

The front's [response deadlines and open-request caps](service_limits.md#chat-front-deadlines-and-admission) are
separate from these connection and cancellation budgets. A failed or dropped cancel is not proof of native expiry.
See [front deployment](../how-to/run_openai_chat_front.md) and
[retry recovery](../decisions/0026-classify-client-retries-and-recover-submits-by-operation-key.md).

## Polling, signals and stream reads

Definitions are in `horde/oai/settings.py`, `FrontSettings`, and `horde/oai/waiter.py` unless otherwise specified.

| Property | Default/value | Purpose and rationale |
| --- | --- | --- |
| Fallback status poll | `fallback_poll_seconds=15`; stretches toward `wait_time / 2`, maximum 60 s; jitter fraction 0.2 | `MAX_STRETCHED_POLL_SECONDS`, `WAIT_TIME_POLL_DIVISOR`, `POLL_JITTER_FRACTION`: reduces native status load during long waits and avoids synchronized polls |
| Signal recheck | `signal_recheck_seconds=0.5` | One short follow-up read can see a completion not yet visible to the first read after a terminal signal |
| Redis liveness | `redis_liveness_seconds=30` | Idle subscription PING/PONG checks expose a black-holed connection rather than reporting it healthy indefinitely |
| Listener reads/reconnects | `LISTENER_READ_TIMEOUT_SECONDS=1`, `LISTENER_RECONNECT_DELAY_SECONDS=2` | Bounds listener responsiveness and retry rate; exact values unrecorded |
| Delta stream read | `DELTA_READ_BLOCK_MILLISECONDS=250`, `DELTA_READ_TIMEOUT_SECONDS=5`, `DELTA_READ_COUNT=100` | Bounds blocking and batches while multiplexing request streams through one process reader |
| Front model-list cache | 5 s | `horde/oai/app.py`: `MODEL_LIST_CACHE_SECONDS`; reduces repeated native model-list calls; exact duration unrecorded |
| Capacity retry hint | 5 s | `horde/oai/app.py`: `INFLIGHT_RETRY_AFTER_SECONDS`; guidance after front capacity refusal |
| Worker/native outage retry hint | 30 s | `NO_ELIGIBLE_WORKER_RETRY_AFTER_SECONDS`, `HORDE_UNAVAILABLE_RETRY_AFTER_SECONDS`; waiting may help, without promising recovery |
| Queue retry-hint cap | 600 s | `QUEUE_UNAVAILABLE_RETRY_AFTER_CAP_SECONDS`; bounds the queue-derived hint |

Signals are best effort and do not replace fallback polling. Stream retention and byte/entry limits are in
[retained progress](service_limits.md#rate-limits-and-retained-progress); those limits must still accommodate front
response budgets and a stalled reader.

## Queue scans, availability and worker measurements

| Property | Value and scope | Definition and consumers | Purpose and rationale |
| --- | --- | --- | --- |
| Image pop scan pages | 10, 50, 200 rows; final size repeats | `horde/database/functions.py`: `POP_SCAN_PAGE_SIZES`, `pop_scan_page_size`, `read_pop_scan_page` | Cheap near-head matches, larger deep scans; page size does not limit total scan depth |
| Text pop scan pages | 3, 10, 50, 200 rows; final size repeats | Same file: `TEXT_POP_SCAN_PAGE_SIZES` | Text rows include prompts, so smaller initial pages reduce duplicated work across concurrent pops |
| Pop claim batch | 8 eligible candidates/claim statement | Same file: `POP_CLAIM_BATCH_SIZE`, `claim_first_waiting_prompt` | Claims the first unlocked candidate in one statement instead of retrying each contested row |
| Request availability cache | 60 s | Same file: `_cache_worker_availability`, `get_worker_availability_for_request` | Avoids eligibility queries per status poll. Text declaration-generation changes invalidate it before TTL; changed request inputs explicitly discard it |
| Queue snapshots | Refresh interval 1 s; stored TTL 5 s | `horde/database/__init__.py`: `start_background_threads`; `horde/database/threads.py`: `store_prioritized_wp_queue` | Shares queue state across replicas without rebuilding it for every poll |
| Queue aging | +50 priority every 10 s, in chunks of 250 rows | `horde/database/threads.py`: `increment_extra_priority`; registration in `start_background_threads` | Raises waiting priority. Chunked stable-order locks avoid a transaction per request and contention with deletion; exact increment unrecorded |
| Worker list/model snapshots | Worker list every 2 s, model list every 10 s; TTLs 300 s and 600 s | `store_worker_list`, `store_available_models` in `horde/database/threads.py`; `start_background_threads` | Refresh and expiry are distinct. Worker list is refreshed frequently because declarations change often; exact model interval/TTLs unrecorded |
| Materialized worker speed | Latest 20 samples; refresh every 30 s; search window 180 s; updates of 100 workers/transaction | `horde/database/threads.py`: `WORKER_SPEED_SAMPLE_LIMIT`, `WORKER_SPEED_SAMPLE_WINDOW`, `WORKER_SPEED_REFRESH_CHUNK`, `refresh_worker_speeds` | The overlapping search window tolerates delayed ticks and clock skew. Small updates bound locks on rows shared with check-in and projection |
| Quorum claim | 5 s | `horde/database/threads.py`: `QUORUM_CLAIM_TTL`, `get_quorum`; `horde/database/classes.py`: `Quorum` | Short-lived ownership claim for maintenance; database advisory locks independently protect accounting and upload writers |

Worker speed feeds both [lease capacity](service_limits.md#text-and-chat-leases) and wait estimates. A cached estimate
is an observation of eligibility and speed, not reserved worker capacity.

### Assignment pressure and shadow forecasts

`horde/request_scheduling.py` defines the values here. Its `calculate_assignment_pressure`,
`calculate_scheduling_forecast`, `_enqueue_scheduling_event` and `_extend_state_expiry` consume them.

| Symbol | Value | Purpose and rationale |
| --- | --- | --- |
| `ASSIGNMENT_PRESSURE_WINDOW` | 5 minutes | Limits the arrival/return evidence used to judge sustained pressure; exact duration unrecorded |
| `DISPATCH_OBSERVATION_RETENTION` | 10 minutes | Retains dispatch/return observations beyond the observation window; exact margin unrecorded |
| `ASSIGNMENT_PRESSURE_MINIMUM_SECONDS` | 120 s | Minimum evidence duration for sustained-rate pressure; exact choice unrecorded |
| `P90_MINIMUM_MARGIN_SECONDS`, `P90_RELATIVE_MARGIN` | 60 s, 0.5 | Provisional p90 adds `max(60 seconds, 0.5 * p50)`; it is a candidate upper estimate rather than an empirically established percentile |
| `SCHEDULING_EVENT_QUEUE_SIZE` | 2,048 | Bounded local delivery queue keeps Redis away from request paths; overflow drops telemetry, not generation work |
| `FORECAST_EXPIRY_GRACE` | 10 minutes | Keeps outcome-pairing state beyond request expiry; refresh extends the expiry horizon |

[Forecast promotion criteria](request_scheduling.md#forecast-validation) require four 24-hour series with at least
10,000 pairs each, 80% within the p50 tolerance, a 90th percentile of absolute p50 error at most 120 s, and p90 coverage
85% through 95%.
[Stall validation](request_scheduling.md#stall-prediction-validation) requires 10,000 predictions and 500 observed
stalls per generation type, precision at least 60%, recall at least 80%, false-positive rate at most 10%, sustained
for 24 hours. These are documented monitoring criteria. This checkout does not supply monitoring-rule definitions
that establish their enforcement, and the exact tuning rationale is unrecorded. They are not runtime API limits.

## Accounting projection and maintenance

| Property | Default/value | Definition and consumers | Purpose and rationale |
| --- | --- | --- | --- |
| Projector batch | 1,000 rows, configurable by `KUDOS_APPLIER_BATCH_SIZE`, minimum 1 | `horde/database/kudos_ledger.py`: `KUDOS_APPLIER_BATCH_SIZE` | Bounded transactions limit locks while folding postings |
| Projector catch-up | At most 10 cycles per tick; 3-second background interval | Same file: `KUDOS_APPLIER_MAX_CATCHUP_CYCLES`; `horde/database/threads.py`: `apply_kudos_ledger`; `start_background_threads` | Drains full batches faster without unbounded ticks; interval aims for a few seconds of projection lag |
| Retention-lock retry spacing | 250 ms | `horde/database/kudos_db.py`: `RETENTION_LOCK_RETRY_INTERVAL` | Polls for the retention lock during bounded waits; exact spacing unrecorded |
| Periodic task failure retry | 10 s | `horde/threads.py`: `PrimaryTimedFunction.run` | A failed callback retries without waiting its full normal interval; exact choice unrecorded |

Database advisory locks, rather than a cache TTL, serialize the projector, payer admission, reconciliation, retention
and mode changes. Lock keys identify namespaces; their numeric encodings are not tunable throughput limits. See
[accounting concurrency](kudos_accounting.md#concurrency-and-lock-reference).

## Accounting retention and archives

Policy settings are parsed by `horde/database/kudos_retention.py`, `KudosRetentionPolicy`; the
[full configuration table](kudos_accounting.md#retention-configuration) owns accepted values and archive setup.

| Property | Default/value | Definition and consumers | Purpose and rationale |
| --- | --- | --- | --- |
| Retention removal enabled | `KUDOS_RETENTION_ENABLED=false` | `KudosRetentionPolicy`, `kudos_retention_tick` | Partition removal is opt-in; partition creation and key expiry still run |
| Online windows | 30 days each; minimum 30; configurable upward | `DEFAULT_WINDOW_DAYS`, `MINIMUM_WINDOW_DAYS`; `KUDOS_RETENTION_CURRENCY_DAYS`, `KUDOS_RETENTION_STATISTICS_DAYS` | Keeps recent exact events for investigation; partition age determines eligibility |
| New partition width | 7 days, configurable 1 through 31 | `DEFAULT_PARTITION_WIDTH_DAYS`, `MINIMUM_PARTITION_WIDTH_DAYS`, `MAXIMUM_PARTITION_WIDTH_DAYS`; `KUDOS_PARTITION_WIDTH_DAYS` | Width adds to effective retention, so it stays small relative to the window |
| Retention cadence | 60 s; at most one catalog transition/tick | `horde/database/__init__.py`: `start_background_threads`; `horde/database/threads.py`: `kudos_retention_tick` | Bounds maintenance work and keeps exports separate from settlement |
| Key expiry batch | 5,000/tick | `horde/database/kudos_retention.py`: `EXPIRE_KEYS_BATCH_SIZE` | Bounds deletion transaction size; exact batch count unrecorded |
| Drop grace/hold | 7 days by default, configurable to 0; `KUDOS_RETENTION_HOLD_DROPS=false` | `DEFAULT_DROP_GRACE_DAYS`, `KudosRetentionPolicy` | Keeps detached data reattachable even if a rendered environment loses the grace variable; immediate drop requires explicit zero |
| Drop contention | Defer for another session's partition/parent lock held by a transaction at least 5 minutes old; drop connection lock timeout 30 s | `DEFAULT_DROP_DEFER_MINUTES`, `DROP_LOCK_TIMEOUT`; `KUDOS_RETENTION_DROP_DEFER_MINUTES` | Avoids a long lock queue hanging maintenance or interrupting consequential work |
| Statistics dump retries | Default retry window 7 days; spacing 15 minutes | `DEFAULT_DUMP_RETRY_DAYS`, `DUMP_RETRY_SPACING`; `KUDOS_RETENTION_DUMP_RETRY_DAYS` | Allows store recovery while later units progress; failed dumps eventually block their unit under the implemented policy |
| Archive catalog reconciliation | Hourly; lock wait at most 10 s | `horde/database/threads.py`: `KUDOS_ARCHIVE_RECONCILIATION_INTERVAL`, `reconcile_kudos_archive_catalog`; `horde/database/kudos_retention.py`: `RECONCILIATION_LOCK_WAIT` | Skips long-held locks instead of competing with a lengthy retention step; the next hourly pass can recover |
| Stale multipart cleanup | Runs daily; uploads older than 1 day | `KUDOS_ARCHIVE_STALE_UPLOAD_INTERVAL`; `horde/database/kudos_retention.py`: `STALE_UPLOAD_AGE`, `abort_stale_archive_uploads` | Cleans abandoned uploads well beyond expected export duration |
| Housekeeping wake | 60 s | `horde/database/threads.py`: `KUDOS_ARCHIVE_JOB_WAKE_INTERVAL`, `_kudos_archive_job_due` | Checks separate jobs for due work without coupling them to a slow retention tick |
| Partition creation lock wait | 3 s | `horde/classes/base/kudos.py`: `KudosPartitioning.CREATE_LOCK_TIMEOUT`; `ensure_kudos_event_partitions` | Fails a partition-creation wait promptly rather than holding subsequent writers behind it |
| Partition vacuum/analyze triggers | Currency 100,000/200,000 rows; statistics 250,000/500,000; both scale factors 0 | `horde/classes/base/kudos.py`: `KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS`; `ensure_kudos_event_partitions` | Projection updates every event once, so fixed dead-row thresholds replace percentage triggers. Each partition needs its own settings; parent storage parameters are not inherited |
| Archive transfer | 8 MiB multipart parts; 1 MiB read chunks; zstd level 12 | `horde/kudos_archive.py`: `ARCHIVE_PART_BYTES`, `ARCHIVE_READ_CHUNK_BYTES`, `ZSTD_COMPRESSION_LEVEL` | Bounds streaming memory and compresses historical rows; exact tuning unrecorded |
| Archive network budgets | Connect 5 s; reads 60 s; configured `max_attempts=3` for data and metadata clients | Same file: `ARCHIVE_CONNECT_TIMEOUT_SECONDS`, `ARCHIVE_READ_TIMEOUT_SECONDS`, `ARCHIVE_MAX_ATTEMPTS`, `ARCHIVE_METADATA_READ_TIMEOUT_SECONDS`, `ARCHIVE_METADATA_MAX_ATTEMPTS` | Bounds object-store stalls. In botocore client configuration this means 3 retries after the initial attempt, up to 4 calls; exact budgets unrecorded |
| Archive size estimate | 0.2 of partition heap bytes | `horde/database/kudos_retention.py`: `ARCHIVE_SIZE_RATIO` | Preflight estimate for archive space; actual compressed size still has to fit the configured budget |

Currency removal requires a verified archive replacement. Statistics use rollups and retained quarantine evidence;
the implemented retry/blocking rules are detailed in [retention states](kudos_accounting.md#retention-states).
Archive budgets are deployment choices, not fixed capacities. No automatic eviction of currency is authorized by a
byte budget. The [retention procedure](../how-to/kudos_ledger_operations.md) explains holds, grace and restoration.

## Moderation processing and evidence storage

The [service retention policy](service_limits.md#trust-moderation-and-retained-history) sets what must leave or remain.
The following controls determine how fast a backlog is processed. Definitions are in
`horde/database/prompt_moderation.py` unless another module is named.

| Property | Value | Symbols, consumers and rationale |
| --- | --- | --- |
| Evidence upload | Batches of 50, at most 10 cycles/tick; 5-second interval | `EVIDENCE_UPLOAD_BATCH_SIZE`, `EVIDENCE_UPLOAD_MAX_CATCHUP_CYCLES`, `upload_pending_text`; `start_background_threads`: bounded uploads outside request transactions; stops on store failure |
| Retention cleanup | 1,000 rows/step/pass, at most 20 passes/tick; 60-second interval | `CLEANUP_BATCH_SIZE`, `RETENTION_MAX_CATCHUP_CYCLES`, `apply_moderation_retention` in `horde/database/threads.py`: short transactions with catch-up capacity; saturation can delay removal beyond the nominal window |
| Rejection activity gauges | Trailing 5 minutes | `REJECTION_ACTIVITY_WINDOW`: smooths bursts while exposing floods promptly |
| Worker suspicion retention | Default indefinite; configured positive days at most 36,500; flagged or suspicious owners exempt | `MAX_WORKER_SUSPICION_RETENTION_DAYS`, `HORDE_WORKER_SUSPICION_RETENTION_DAYS`: separate from prompt evidence; bounds prevent date/timedelta overflow |
| Evidence network budgets | Connect 3 s, read 10 s, configured `max_attempts=2`, pool 8 | `horde/r2.py`: `EVIDENCE_CONNECT_TIMEOUT_SECONDS`, `EVIDENCE_READ_TIMEOUT_SECONDS`, `EVIDENCE_MAX_ATTEMPTS`, `EVIDENCE_MAX_POOL_CONNECTIONS`; botocore client configuration allows 2 retries after the initial attempt, up to 3 calls. Bounds an unavailable store's cost per upload cycle |
| Bulk evidence deletion | 1,000 keys/call | `horde/r2.py`: `EVIDENCE_DELETE_BATCH_SIZE`; matches the S3 bulk-delete limit |
| Chat image deletion retry | At most 100,000 queued keys; retry batch 1,000 | `horde/classes/kobold/waiting_prompt.py`: `MAX_CHAT_IMAGE_DELETE_RETRY_KEYS`, `CHAT_IMAGE_DELETE_RETRY_BATCH`; bounds Redis retry state and S3 deletion work |

## Other asynchronous delivery and model metadata

| Property | Value and scope | Definition and consumers | Purpose and rationale |
| --- | --- | --- | --- |
| Webhook queue/senders | 256 waiting deliveries; 4 threads/process | `horde/classes/base/processing_generation.py`: `WEBHOOK_QUEUE_MAXSIZE`, `WEBHOOK_SENDER_THREADS`, `enqueue_webhook` | Keeps slow subscribers off submit paths and limits accumulated work; queue overflow drops delivery, not the generation |
| Interrogation result cache | Image-output forms 90 minutes; other forms 5 days, when the source is not R2-stored | `horde/classes/stable/interrogation.py`: `InterrogationForms.deliver` | Image-output cache expires before the recorded 120-minute object lifetime; exact non-image duration rationale unrecorded |
| Model-reference publication | 3,600 s between fleet publications; poll every 60 s | `horde/model_reference.py`: `FLEET_PUBLISH_INTERVAL_SECONDS`; `horde/database/threads.py`: `publish_model_reference_snapshot`; `start_background_threads` | One elected publisher refreshes the shared image-reference snapshot |
| Compiled statistics snapshots | Retain 7 days, prune hourly, always keep the latest snapshot | `horde/database/threads.py`: `COMPILED_STATS_RETENTION`, `prune_compiled_stats`; `start_background_threads` | Read paths use the latest snapshot, so older compiled snapshots can be removed. This leaves raw generation statistics untouched |
| Snapshot staleness | Warning after 10,800 s, warnings at most hourly | `horde/model_reference.py`: `STALE_SNAPSHOT_SECONDS`, `STALE_WARNING_INTERVAL_SECONDS` | Three publication intervals expose a stale snapshot without log flooding |
| Snapshot publication lock/retries | 180 s lock, at most 10 transaction retries | `horde/model_reference_snapshot.py`: `DEFAULT_PUBLISH_LOCK_SECONDS`, `MAX_TRANSACTION_RETRIES` | Bounds concurrent publication and contention recovery; exact values unrecorded |
| Replica reference refresh | Every 5 s from Redis; text-reference refresh hourly | `horde/flask.py`: `create_app`, `PrimaryTimedFunction` registrations | Replicas consume the published image snapshot without independently polling the upstream service |

## Verification map

| Mechanism | Tests |
| --- | --- |
| Queue scan reachability, pages and claims | `tests/unit/test_pop_queue_scan.py`, `tests/unit/test_job_pop_priority_users.py` |
| Cached eligibility and speed | `tests/unit/test_text_worker_eligibility.py`, `tests/unit/test_worker_speed_materialization.py` |
| Pressure, forecast storage and failure isolation | `tests/unit/test_request_scheduling.py` |
| Front budgets and poll cadence | `tests/unit/oai/test_settings.py`, `tests/unit/oai/test_lifecycle.py`, `tests/unit/oai/test_poll_cadence.py` |
| Native routing and cancellation capacity | `tests/unit/oai/test_instance_routing.py`, `tests/unit/oai/test_submit_recovery.py` |
| Projector batches and concurrency | `tests/unit/test_kudos_ledger.py`, `tests/unit/test_kudos_concurrency.py` |
| Retention, archive budgets and recovery | `tests/unit/test_kudos_retention.py`, `tests/unit/test_kudos_archive.py`, `tests/integration/test_kudos_retention_end_to_end.py` |
| Moderation upload and retention | `tests/unit/test_evidence_upload.py`, `tests/unit/test_moderation_retention_policy.py`, `tests/integration/test_prompt_moderation.py` |
| Webhook bounds and delivery | `tests/unit/test_generation_webhook.py` |
| Fleet snapshot and loader | `tests/unit/test_model_reference_snapshot.py`, `tests/unit/test_model_reference_loader.py`, `tests/integration/test_model_reference_snapshot_redis.py` |

Some infrastructure values have no dedicated boundary assertion in these suites. These references identify the
relevant verification paths without claiming every numeric default is independently tested.
