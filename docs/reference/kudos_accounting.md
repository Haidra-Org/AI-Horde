---
title: "Kudos accounting reference"
summary: "The mutation and consumption contract: accounting events, projection targets, reservations, the lock order, the read models, and event retention."
topics: [kudos, accounting]
order: 70
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Kudos accounting reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [accounting](../topics.md#accounting), [kudos](../topics.md#kudos)
<!-- END GENERATED: topics -->

The maintainer contract for kudos mutation and consumption: the authoritative events, the materialized targets, the
producers and consumers, the locks, and the tests that hold each rule. For design rationale, read [Kudos accounting,
projection, and concurrency](../explanation/kudos_accounting.md). For deployment and incident procedures, follow
[Kudos ledger operations](../how-to/kudos_ledger_operations.md).

## Source of truth by mode

| Concern | `shadow` mode | `ledger` mode |
| --- | --- | --- |
| Accepted currency movement | Append `kudos_ledger` row with `applied = true` | Append `kudos_ledger` row with `applied = false` |
| Accepted display/counter movement | Append `kudos_stat_events` row with `applied = true` | Append `kudos_stat_events` row with `applied = false` |
| Materialized mutation owner | `kudos_legacy_projection` applies the historical inline change | `apply_pending_kudos` folds events asynchronously |
| Spend authorization | Payer lock plus `available_kudos` and reservations | Payer lock plus `available_kudos` and reservations |
| Ordinary balance reads | Materialized column, current after the transaction | Materialized column, eventually consistent |
| Audit/replay role | Forward audit; opening balance predates it | Authoritative post-cutover movement history |
| Default | Yes, for new/migrated installations | No; operator must explicitly cut over |

The existing balance at first deployment is the opening position. The ledger contains deltas from deployment onward,
not a fabricated history of older movements.

## Vocabulary

| Term | Exact meaning |
| --- | --- |
| Currency | A delta to a user's spendable `users.kudos` or evaluation `users.evaluating_kudos` balance. |
| Display kudos | A worker/team attribution total. It describes earned work but is not spendable account currency. |
| Counter | A value denominated in kudos, things, or count and projected into stats, records, or aggregates. |
| Business event | One logical mutation such as a settlement or transfer, correlated by one `event_id`. |
| Posting | One signed row affecting exactly one target balance or statistic. Direction comes from the amount's sign. |
| Projection | A denormalized current value maintained from postings for inexpensive reads. |
| Hold/reservation | A temporary claim against one payer's available kudos. It is neither a debit nor a credit. |
| Applied | The posting was included in a committed projection transaction, or was projected inline in shadow mode. |
| Available balance | Conservative spend capacity after floor, active reservations, and unreserved queued debits. |
| Effective balance | Materialized balance plus all committed unapplied currency deltas, clamped to the account floor. |

## Data model

### Accounting tables

| Model/table | Purpose | Ownership and lifetime | Important constraints |
| --- | --- | --- | --- |
| `KudosLedger` / `kudos_ledger` | Currency postings, range-partitioned by `created` | Required `users.id` foreign key with `ON DELETE RESTRICT`; authoritative history must not be orphaned | One user target; spendable vs escrow selected by `escrow`; `NUMERIC(20,2)` amount; NaN rejected; partial unapplied index |
| `KudosStatEvent` / `kudos_stat_events` | Display and counter postings, range-partitioned by `created` | User, worker, and team IDs are immutable audit references, intentionally not ownership foreign keys because workers/teams may be hard-deleted | Exactly one of `user_id` and `worker_id`; typed unit; NaN rejected; invalid events are quarantined outside the drainable queue |
| `KudosReservation` / `kudos_reservations` | Payer holds for upfront work and transfers | User ownership foreign key with `ON DELETE CASCADE` | Unique `business_id`; positive original amount; non-negative remaining amount; active-user partial index |
| `KudosBalanceSnapshot` / `kudos_balance_snapshots` | User-currency reconciliation baseline | User ownership foreign key with `ON DELETE CASCADE` | One row per snapshot/user; records balance, escrow, and visible applied totals |
| `KudosSnapshotInvalidation` / `kudos_snapshot_invalidations` | Interval of snapshot creation times whose baselines a checkpoint rebuild made unusable | Operational state | One row per recheckpoint that deleted checkpoints. Reconciliation refuses a snapshot created inside any interval |
| `KudosLedgerControl` / `kudos_ledger_control` | Single-row mutation-mode control | Installation state | ID 1; `shadow` or `ledger`; non-null change time |
| `KudosLedgerApplierState` / `kudos_ledger_applier_state` | Projector heartbeat | Operational state only | ID 1; not a watermark and not used for exactly-once folding |
| `KudosStatRollup` / `kudos_stat_rollups` | Statistic events of retired partitions, summed per UTC day and attribution key | Authoritative history. User, worker and team IDs are audit references without foreign keys | Unique over day, attribution columns and `source_range`, with NULLs not distinct |
| `KudosStatQuarantine` / `kudos_stat_quarantine` | Quarantined statistic events moved unchanged out of retired partitions | Audit references without foreign keys. Rows do not expire | `id` is the original event ID, not a sequence |
| `KudosLedgerCheckpoint` / `kudos_ledger_checkpoints` | Per-user cumulative applied balance and escrow totals through a retired partition's upper bound | User foreign key with `ON DELETE CASCADE`. Authoritative history | Unique `(boundary, user_id)`. Excludes `RECONCILIATION` postings |
| `KudosLedgerDailyStatement` / `kudos_ledger_daily` | Per-user daily sums of retired currency postings by balance and entry type | User foreign key with `ON DELETE CASCADE`. Authoritative history | Unique `(user_id, escrow, entry_type, day, source_range)` |
| `KudosIdempotencyKey` / `kudos_idempotency_keys` | Retry keys of transfers and repairs, with parties, amount and outcome | Deleted after the key lifetime. The party columns are not foreign keys | `event_id` primary key. `created` index for expiry |
| `KudosRetentionUnit` / `kudos_retention_catalog` | Retention state of each retired partition or legacy sub-range | Operational state | Unique `(dataset, partition_name, unit_lower, unit_upper)` with NULLs not distinct |

The mapped accounting models use SQLAlchemy 2 `Mapped`/`mapped_column` attributes. Runtime dialect operations are
confined to typed helpers: advisory locks and isolation in `horde/database/kudos_db.py`, counter upserts in
`horde/database/kudos_counters.py`, and legacy mode behavior in `horde/database/kudos_legacy_projection.py`. Raw DDL
belongs in the versioned migration file, not request code. The SQLAlchemy mypy plugin checks the accounting model
surface in CI.

### `KudosLedger` fields

| Field | Contract |
| --- | --- |
| `id` | Database identity used only for bounded claim order; it is not a transaction watermark. |
| `created` | Audit timestamp, queue-age source and partition key. The primary key is (`id`, `created`). |
| `event_id` | UUID shared by related postings. A deterministic UUID is produced when an idempotency key is supplied. |
| `entry_type` | Typed business classification from `KudosEntryType`; never encodes debit/credit direction. |
| `user_id` | Required currency owner. |
| `escrow` | `false` targets spendable balance; `true` targets evaluation escrow. |
| `amount` | Signed `NUMERIC(20,2)` delta. Positive credits and negative debits are separate postings. |
| `applied` | Per-row work-queue state changed in the same transaction as its projection. |
| `job_id`, `wp_type` | Optional correlation dimensions inherited from the active `kudos_event`. |
| `detail` | Typed-key audit metadata. The applier only interprets documented keys. |

### `KudosStatEvent` fields

`event_id`, `entry_type`, `job_id`, `wp_type`, `amount`, `detail`, and `applied` have the same correlation/audit
meaning as their currency counterparts. The remaining fields define a counter projection:

| Field | Contract |
| --- | --- |
| `user_id` | User counter target; mutually exclusive with `worker_id`. |
| `worker_id` | Worker counter target; mutually exclusive with `user_id`. |
| `worker_user_id` | Worker owner captured for audit; not itself a projection target. |
| `team_id` | Team attribution captured when the event is emitted. |
| `unit` | `kudos`, `things`, or `count`; consumers must not combine unlike units. |
| `stat_action` | Action/bucket dimension such as `generated`, `usage`, `contributions`, or `fulfilments`. |
| `record` | Typed projector discriminator or user-record dimension. |
| `quarantined`, `quarantine_reason`, `quarantined_at` | Durable poison-event isolation. Every claimed statistic row sharing a malformed event's `event_id` is quarantined together and retained for review. |

### Stable enums and audit keys

| Enum | Values/role |
| --- | --- |
| `KudosLedgerMode` | `shadow`, `ledger` |
| `KudosUnit` | `kudos`, `things`, `count` |
| `KudosStatRecord` | `user_kudos`, `worker_kudos`, `last_active` |
| `KudosStatEventQuarantineReason` | Closed reason codes for deterministic statistic-event isolation |
| `KudosAggregate` | `contributions`, `fulfilments` |
| `KudosAuditDetail` | `reason`, `reservation_id`, `snapshot_id`, `touch_last_active`, `style_author_id`, `style_surcharge`, `style_type` |

The three `style_*` keys appear together on a waiting-prompt activation debit whose request runs under someone else's
style. They contain the author's user id, the part of the debit that is the style surcharge (a decimal string), and the
style type (`text` or `image`), which is the user-record dimension the author's style count is kept under. They are written and
read through `StyleReward.audit_detail` and `StyleReward.from_audit_detail`.

Do not add an untyped string discriminator when one of these axes describes it. Add or extend the enum and teach the
projector and tests about the new value together.

## Entry-type catalogue

An entry type answers “why did this posting exist?” The sign and target answer “what did it do?”

| `KudosEntryType` | Expected use |
| --- | --- |
| `GENERATION` | Image/text/interrogation settlement: requester debit, worker-owner spendable/escrow credit, and worker display event as applicable |
| `UPTIME_REWARD` | Periodic worker display credit and owner spendable/escrow credit |
| `EVALUATION_PROMOTION` | Paired escrow debit and spendable credit after trust promotion, plus the user's matching `accumulated` statistic movement |
| `TRANSFER` | Paired source debit and destination credit for a user gift |
| `ADMIN_ADJUSTMENT` | Signed administrator adjustment |
| `AWARD` | Monthly, rating/aesthetic, and other application award credits |
| `STYLE_REWARD` | Style-owner currency reward for the part of the style surcharge the requester's activation debit collected. The style object's counters remain separate |
| `STAT_RECORD` | User record counts or thing totals |
| `STAT_CONTRIBUTION` | Worker/team contribution and fulfilment aggregates |
| `STAT_ACTIVITY` | Asynchronous user `last_active` touch |
| `FLOOR_ADJUSTMENT` | Explicit currency created when an account-floor rule forgives a debit |
| `RECONCILIATION` | Deterministic compensating currency posting emitted by an approved repair |

## Mutation contract

All new or modified kudos code must obey these rules.

1. **Use the public producer surface.** User currency enters through `User.modify_kudos` or
   `User.modify_evaluating_kudos`. Derived events enter through `emit_kudos_stat_event`. Do not assign a materialized
   accounting column in a business path. Narrow exceptions are listed under [excluded kudos-like state](#excluded-kudos-like-state).
2. **Group a logical event.** Wrap a settlement, transfer, activation tax, or other multi-posting mutation in
   `kudos_event`. Include `job_id` and `wp_type` where available.
3. **Make external retries explicit.** If an API mutation can be retried independently of locked business state,
   accept a stable idempotency key, derive the event ID from it, and reject reuse with different parameters. A random
   event UUID provides correlation only.
4. **Commit business state and postings together.** Do not commit part of an event, perform network work, and append
   the rest later. Emission helpers flush by default so the caller owns the transaction boundary.
5. **One currency row, one user balance.** Worker/team IDs and counter units never belong in `KudosLedger`. Emit
   separate debit and credit rows. Do not encode a transfer as an opaque net amount.
6. **Reserve before authorizing eventual spend.** Under `acquire_payer_lock`, call `reserve_kudos` with a stable
   `business_id`. Attach that reservation ID to matching debit metadata. Never use `effective_kudos` to authorize a
   spend.
7. **Do not fund from pending credits.** `available_kudos` deliberately ignores them. Changing that rule requires a
   proof covering credit rollback, event ordering, and projector failure.
8. **Preserve mode ownership.** Business code emits events in both modes and delegates temporary inline behavior to
   `kudos_legacy_projection`. Do not add mode branches to endpoints or settlement methods.
9. **Record non-linear compatibility behavior.** If a rule floors, caps, forgives, or otherwise changes the requested
   signed delta, emit the difference as its own typed posting so replay matches the projection.
10. **Capture historical dimensions at emission.** Team membership and similar attribution must be stamped on the
    event. The projector must not infer past ownership from mutable current relationships.
11. **Use decimal accounting values.** Convert through `Decimal(str(value))` and round/quantize at the existing
    two-decimal boundary. Never fold currency through binary-float accumulation.
12. **Keep the archive immutable.** Do not update old amounts, retarget postings, delete applied history, or toggle
    `applied` as a repair. Emit a compensating posting. Only the retention job removes applied history, as described
    under [event retention](#event-retention).
13. **Add producer, fold, retry, and recovery tests.** At minimum pin the emitted rows and materialized result. A new
    spend path also needs concurrent overspend coverage. A new retryable path needs replay/conflict coverage.

Direct materialized assignments are allowed only for initial data/bootstrap before an accounting movement exists,
or inside the shadow/projector/reconciliation implementation described here. A test fixture that needs an opening
balance should assign it directly and commit rather than creating misleading ledger history.

## Producer inventory

This is the current inventory of paths that create user currency or kudos-derived projection events.

| Business event | Entry types and targets | Primary code |
| --- | --- | --- |
| Waiting-prompt activation tax | Requester `GENERATION` debit; user usage/record/activity stats; upfront reservation consumption | `WaitingPrompt._activate`, `WaitingPrompt.record_usage`, `User.record_usage` |
| Image/text generation settlement or cancellation settlement | Requester `GENERATION` debit; owner spendable/escrow credit; worker kudos/contribution/fulfilment; user records; team aggregates | `ProcessingGeneration.record`, `WorkerTemplate.record_contribution`, `User.record_usage`, `User.record_contributions` |
| Interrogation form settlement/cancellation | Same currency pattern for requester/worker owner plus interrogation worker counters | `InterrogationForms.record`, `InterrogationWorker.record_contribution` |
| Worker uptime interval | Owner `UPTIME_REWARD` spendable/escrow credit and worker display/stat credit | `WorkerTemplate.record_uptime`, `User.record_uptime` |
| Trust threshold crossing | `EVALUATION_PROMOTION` escrow debit and spendable credit, plus the user `accumulated` statistic event | Projector promotion/drain helpers; shadow compatibility projector |
| User transfer | Paired `TRANSFER` source debit/destination credit, transfer log, payer reservation | `transfer_kudos` and username/API-key wrappers |
| Monthly/recurring grant | `AWARD` user credit | `User.modify_monthly_kudos`, `User.receive_monthly_kudos` |
| Rating/aesthetic reward | `AWARD` user credit | Stable API rating/aesthetic endpoints |
| KoboldAI or other application award | `AWARD` user credit | Kobold and base API award endpoints |
| Style-owner reward | `STYLE_REWARD` user credit and user style record, for the part of the surcharge the activation debit collected | `User.record_style`, called by `_credit_style_authors` in ledger mode and `project_style_reward` in shadow mode |
| Administrator adjustment | `ADMIN_ADJUSTMENT` signed user delta | User administration API through `User.modify_kudos` |
| Minimum-balance forgiveness | Already-applied `FLOOR_ADJUSTMENT` user credit | User projection helper in shadow mode; `_apply_user_deltas` in ledger mode |
| Reconciliation repair | Deterministic `RECONCILIATION` user spendable/escrow delta and its idempotency key. Refused with `ValueError` when the snapshot is older than the key lifetime, and with `StaleCheckpointsError` while a currency retention unit is blocked with `late rows` or `online drift since export` or when the snapshot was created inside a `kudos_snapshot_invalidations` interval | `reconcile_balances(..., apply_repairs=True)` |

`User.modify_kudos` also emits the matching per-action user-kudos statistics event. `WorkerTemplate.modify_kudos`
emits worker display/stat events, not currency. Producer wrappers should retain that distinction.

Two interrogation contracts in that table correct behaviour the inline implementation had:

- An interrogation uptime crossing grants an untrusted owner exactly one reward, routed wholly to the spendable
  balance by the alchemist bypass and minting no escrow, where the inline path credited both the balance and escrow
  for the same crossing (pinned by
  `tests/unit/test_kudos_invariants.py::test_interrogation_uptime_grants_untrusted_owner_exactly_one_reward`).
- Cancelling an interrogation form a worker is still processing settles it like a submission, crediting the worker
  and owner and debiting the requester the form kudos plus burn, where the inline path moved the form to `CANCELLED`
  ahead of the settle check and so settled nothing (pinned by
  `tests/unit/test_kudos_invariants.py::test_cancelling_in_flight_interrogation_form_settles_like_a_submit`).

## Projection target inventory

| Materialized target | Event source | Projector rule |
| --- | --- | --- |
| `users.kudos` | Non-escrow currency postings | Sum per user, apply in user-ID order, clamp to `get_min_kudos`, record floor difference |
| `users.evaluating_kudos` | Escrow currency postings | Sum per user; promotion is evaluated after the fold |
| `users.last_active` | `STAT_ACTIVITY`/touch detail | Keep the latest event timestamp |
| `user_stats.value` | `user_kudos` stat record | Atomic insert-or-increment by `(user_id, action)` |
| `user_records.value` | `STAT_RECORD` | Atomic insert-or-increment by `(user_id, record_type, record)` |
| `workers.kudos` | `worker_kudos` stat record | Sum per worker |
| `worker_stats.value` | `worker_kudos` stat record | Atomic insert-or-increment by `(worker_id, action)` |
| `workers.contributions` | `STAT_CONTRIBUTION` / `contributions` | Sum thing deltas per worker |
| `workers.fulfilments` | `STAT_CONTRIBUTION` / `fulfilments` | Sum count deltas per worker |
| `teams.kudos` | Team-stamped worker kudos events | Sum per captured team ID |
| `teams.contributions` | Team-stamped contribution events | Sum per captured team ID |
| `teams.fulfilments` | Team-stamped fulfilment events | Sum per captured team ID |
| Reservation remainder/release time | Currency debit metadata or completed transfer event | Consume request holds by debit; release transfer hold only after every event posting is applied |
| Trusted user role and worker pause state | Projected escrow crossing threshold | Promote eligible mature user, unpause workers, then emit the escrow drain pair |
| Style author credit | Activation debits carrying the `style_*` detail keys | After the user fold, attribute each requester's floor difference to their surcharges in id order, each absorbing up to its whole amount. Emit the author's `STYLE_REWARD` posting and style record for any remainder, folded by a later cycle |

A surcharge the floor forgave is not credited to the style's author, since the requester paid none of it and crediting
it would create currency. The floor difference is computed per user per batch, so a requester's credits in the same
batch reduce what the floor forgives. In shadow mode the same attribution runs inline during activation from the floor
adjustment `User.modify_kudos` returns. In ledger mode that return is zero and only the applier credits, so a request
never credits its author twice.

Currency rows and statistic rows are each claimed in a bounded batch. A single event may cross a batch boundary.
Any event-wide side effect must query for remaining unapplied rows rather than assuming a batch contains the
whole event.

## Consumer and read-model inventory

Choosing the correct read is part of the mutation contract. A blanket replacement of every `.kudos` access is wrong
because account currency, queue priority, worker attribution, job price, and shared-key quota have different meaning.

### User currency consumers

| Consumer | Current read | Consistency and rule |
| --- | --- | --- |
| Upfront image/text request admission | `reserve_kudos` -> `available_kudos` | Spend-safe and conservative; includes floor, holds, and queued debits; ignores queued credits |
| Interrogation admission and interrogation-worker check | `reserve_kudos` / `available_kudos` | Spend-safe; retryable form reactivation reuses its business ID |
| User transfer | `reserve_kudos` -> `available_kudos` | Payer-serialized; recipient is not locked; optional API idempotency key protects replay for the [key lifetime](#idempotency-key-lifetime) |
| Admin adjustment “new balance” response | `effective_kudos` | Includes the just-committed/unapplied delta; not an authorization value |
| Queue priority for image/text | `user.kudos` copied to `waiting_prompt.extra_priority` at activation | Snapshot of a potentially lagging projection; queue ordering thereafter uses `extra_priority` |
| Queue priority for interrogation | `user.kudos` copied to `interrogations.extra_priority` at construction | Snapshot of a potentially lagging projection |
| Stable worker's secondary upfront eligibility check | materialized `waiting_prompt.user.kudos` minus floor | Eventual legacy recheck; initial admission hold prevents overspend, but lag may transiently alter scheduling eligibility |
| User details, login/welcome, status and ordinary API display | materialized `user.kudos`/`evaluating_kudos` | Eventually consistent in ledger mode |
| Award endpoints returning `new_kudos` | generally materialized `user.kudos` | May return the pre-projection value unless the endpoint explicitly uses `effective_kudos` |
| User listing/sorting and inactive-account heuristics | materialized `User.kudos` | Eventually consistent; intended for administrative/read-model behavior, not spend admission |
| Trust promotion | materialized `evaluating_kudos` inside the serialized projector | Evaluated after fold; final qualifying event promotes without another request |
| Snapshot/reconciliation | materialized user columns plus applied ledger totals | Repeatable-read baseline; repair only through compensating postings |

The two legacy materialized reads in worker scheduling are documented rather than hidden. If they are changed, the
maintainer must decide the intended policy: `available_kudos` is conservative spend capacity. `effective_kudos`
includes queued income. The materialized value is a cheap, lagging priority/display projection. They are not
interchangeable.

### Derived-stat consumers

| Projection | Consumers |
| --- | --- |
| `workers.kudos`, `worker_stats` | Worker detail/API reward totals and per-action breakdowns |
| `workers.contributions`, `workers.fulfilments` | Worker details, top-worker queries, global totals, performance/leaderboard views |
| `teams.kudos`, `teams.contributions`, `teams.fulfilments` | Team detail/API aggregates and rankings |
| `user_stats` | User per-action kudos breakdown returned by user detail paths |
| `user_records` | User usage/contribution/request/fulfilment records, contributor queries, statistics endpoints |
| `users.last_active` | Account activity and stale-account lifecycle logic |
| Applier heartbeat and queue/reservation ages | Quorum-node metrics, operator status command, cutover gates. Node-local `/api/v2/status/heartbeat` excludes these shared-database signals |

All of these can lag in ledger mode. Consumers that need transaction-local proof of an accepted event should inspect
the event/result they just created, not force the projector or assume a refreshed aggregate.

### Excluded kudos-like state

The following kudos-adjacent state is not a user currency projection and must not be routed through
`KudosLedger` without a separate design decision:

| State | Meaning and mutation rule |
| --- | --- |
| `UserSharedKey.kudos` and `utilized` | Per-key quota/budget, including `-1` for unlimited; consumed inline with the request, because the clamp at zero is an enforcement read in the request path and a separate endpoint sets the budget absolutely |
| `WaitingPrompt.kudos`, `consumed_kudos`, generation/form kudos | Estimated price, accumulated job cost, or reward returned for one job; kept inline because they are per-prompt rows rather than shared aggregates and the same event already emits a currency posting |
| `WorkerTemplate.kudos` | Worker-attributed display total; projected from `KudosStatEvent` |
| `Team.kudos` | Team-attributed display total; projected from worker stat events stamped with `team_id` |
| `Style.kudos`, `Style.contributions`, `Style.fulfilments` | Style-level popularity/contribution counters; still maintained by the style subsystem |
| `KudosTransferLog` | Auxiliary transfer history and policy/audit record; does not replace the paired currency postings |
| Test-user bootstrap balance | Absolute fixture/local-login opening state; direct assignment avoids inventing an accounting movement |
| `workers.last_reward_uptime` | An absolute cursor set to the worker's current uptime at each reward crossing, which a signed-delta fold cannot express |
| `workers.aborted_jobs` | Resets to zero on hourly rollover and on tripping the drop threshold, so its value depends on event ordering and timing rather than a sum, and the threshold decision is read in the same path that increments it |

## Reservation lifecycle

| Operation | Stable business ID | Created/reactivated | Consumed/released |
| --- | --- | --- | --- |
| Image/text upfront admission | `upfront:<waiting-prompt-id>` | Before activation is accepted | Debit projection consumes it; prompt deletion/cancel paths and the expired-prompt cleanup release unused remainder |
| Interrogation upfront admission | `interrogation:<form-id>` | When a worker claims a waiting form | Debit projection consumes it; cancellation/failure paths and the expired-interrogation cleanup release it; a retry reactivates the row |
| Transfer | `transfer:<source-user-id>:<event-id>` | Before paired postings commit | Released only after no unapplied posting remains for the event |

`business_id` uniqueness supplies retry stability. Reactivation may not change payer. Reservation mutation uses row
locks only after payer-scoped admission serialization. Release functions are idempotent and return zero when there
is not an active hold. `available_kudos` reads the balance, active holds, and queued debits in one statement so all terms come
from a single database snapshot. A fold or release committing mid-read cannot hide a debit from every term.

## Concurrency and lock reference

| Primitive | Scope | Acquired by | Purpose |
| --- | --- | --- | --- |
| Applier advisory transaction lock | Installation | Projector and mode transition | At most one database projector; serialize final drain with projection |
| Payer advisory transaction lock | User ID | Reservations/transfers/admission | Prevent concurrent promises from overspending one payer without locking recipients |
| Reconciliation advisory transaction lock | Installation | Repair mode | Prevent concurrent compensation emitters |
| Mode-gate advisory transaction lock (shared) | Mutation transaction | `get_kudos_ledger_mode` | Pin mode until the writer commits |
| Mode-gate advisory transaction lock (exclusive) | Installation | `set_kudos_ledger_mode` | Wait for every old-mode writer before ownership changes; fair queueing keeps new pins from starving a waiting transition |
| Event-row `FOR UPDATE SKIP LOCKED` | Bounded batch | Projector; worker deletion for that worker | Claim exact unapplied work without a watermark; serialize worker deletion before child-counter writes |
| Projection-target `FOR KEY SHARE` | Claimed users/workers | Projector | Prevent target deletion between validation and foreign-key child insertion without blocking ordinary non-key updates |
| Reservation-row `FOR UPDATE` | Business ID | Consume/release | Serialize hold depletion/release |
| Repeatable-read transaction | Snapshot/reconciliation command | Reconciliation helpers | Observe balances and applied totals from one consistent database snapshot |
| Retention advisory session lock | Installation | `run_retention_tick` | At most one retention tick at a time. It is held on a dedicated autocommit connection, so a long export does not keep a transaction open |

Required accounting lock order is applier lock before exclusive mode-gate lock. Projection targets are visited in
stable ID order. Code that needs another accounting lock must document where it fits before it is merged. The legacy
`USE_SQLITE` runtime mode short-circuits PostgreSQL advisory locks and therefore cannot validate these concurrency
properties.

## Failure and recovery invariants

- A projector cycle commits both target updates and exact applied flags, or neither.
- Deterministically malformed statistic events are quarantined by business-event ID. Unrelated events in the batch continue, while infrastructure failures still roll back the whole cycle.
- A row is selected by `applied = false`, never by `id > watermark`.
- A stopped projector is a lag incident, not lost work. Restore it and drain in ledger mode.
- A transfer hold remains active until both sides of the event are materialized, even when a batch splits the event.
- A trusted user's positive escrow with no in-flight promotion pair is self-healed by a later projector cycle.
- Minimum-balance flooring creates an explicit `FLOOR_ADJUSTMENT` so snapshot replay equals projection.
- A snapshot records both opening columns and the applied totals visible at that instant.
- Reconciliation excludes prior `RECONCILIATION` entries from its movement baseline and emits one deterministic repair
  event per snapshot/user.
- Ledger-to-shadow transition owns the applier lock, waits for ledger-mode writers, drains the final tail, and changes
  mode in one transaction.
- Applied currency and statistic history stays online for the retention window. After the window, the retention job
  removes a partition only once its replacement is committed. See [event retention](#event-retention).

Never recover by deleting ledger/stat rows, editing old amounts or targets, changing `applied`, or directly assigning a
production balance. Preserve evidence and emit compensation. See the [operations guide](../how-to/kudos_ledger_operations.md)
for the executable procedure.

## Event retention

`kudos_ledger` and `kudos_stat_events` keep exact events online for at least 30 days. After that window, statistic
events are replaced by daily rollups. Currency postings are replaced by per-user checkpoints, daily statements and a
verified archive object. The partition is then dropped. The reasons are recorded in
[ADR 42](../decisions/0042-roll-up-statistics-and-archive-currency-after-the-online-window.md). The procedures for
converting, enabling, unblocking and restoring are in
[kudos ledger operations](../how-to/kudos_ledger_operations.md#converting-the-event-tables-to-partitions).

### Partition layout

Both event tables are partitioned by `RANGE (created)` and have the primary key (`id`, `created`). `created` is naive
UTC.

| Partition | Bounds | Origin |
| --- | --- | --- |
| `<table>_p_legacy` | `MINVALUE` to the swap timestamp | On a converted database, the table as it was before `sql_statements/5.1.16.txt`. On a fresh database, an empty partition up to the UTC Monday 00:00 on or before the first bootstrap |
| `<table>_p_<YYYYMMDD>` | From the date in the name for one partition width | On a converted database, the eight weeks from the swap timestamp are created by `sql_statements/5.1.16.txt`. Later partitions are created ahead of time by `ensure_kudos_event_partitions` |

`sql_statements/5.1.16.txt` creates eight weekly partitions because the release before it does not create
partitions. After a rollback to that release, inserts keep finding a partition until eight weeks after the swap
timestamp. Each of these partitions has the storage parameters of `KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS`.

`ensure_kudos_event_partitions` creates partitions until one covers the current time plus twice the partition width.
It runs in `init_db` at startup with the default width, and on every retention tick with `KUDOS_PARTITION_WIDTH_DAYS`.
New partitions continue from the highest existing upper bound, so with the default width every bound is a UTC Monday
00:00. A new partition inherits every partitioned index and, on `kudos_ledger`, the `users` foreign key.

Each new partition is created as a standalone table with
`LIKE <table> INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES`, the storage parameters of
`KudosPartitioning.AUTOVACUUM_STORAGE_PARAMETERS` and a validated `<partition>_bounds` CHECK on its range. It is then
attached with `ALTER TABLE ... ATTACH PARTITION`, and the CHECK is dropped. The attach skips its validation scan
because of the CHECK, and attaches each copied index to the matching partitioned index.

| Lock | Mode | Waits behind |
| --- | --- | --- |
| Parent, from the second bounds listing to the end of the transaction | `SHARE UPDATE EXCLUSIVE` | Another partition creation, `VACUUM`, `ANALYZE`, a concurrent detach |
| `users`, during a `kudos_ledger` attach | `SHARE ROW EXCLUSIVE` | Any open transaction that wrote to `users` |

Reads and inserts through the parent do not wait for these locks. Writes to `users` queue behind the `users` request
while it waits. Every wait ends after `KudosPartitioning.CREATE_LOCK_TIMEOUT` (3 seconds). On a timeout, the partition
is not created, its standalone table is rolled back, and the call logs the warning
`Deferred partition <partition> of <table> to the next call`, or `Deferred the partitions of <table> to the next call`
when the parent lock timed out, and returns without raising. The partitions it created
before the timeout are kept, and the next retention tick retries the rest. A range whose partition name already
belongs to a table outside the partitioning, such as a detached partition, is skipped with a warning.
`kudos_stat_events_p_legacy` also has `ix_kudos_stat_events_legacy_created`. That index belongs to the legacy partition
alone and is not attached to the parent. The retention job reads the legacy partition by `created` sub-range through
it.

### Retention units

A retention unit is one partition, except for the legacy partition, which is split into one unit per partition width
of `created`. Legacy unit boundaries sit on the same grid as the weekly partitions. The first legacy unit starts at
`MINVALUE`, and an empty legacy partition is a single unit. Each unit is one `kudos_retention_catalog` row with
`dataset` (`currency` for `kudos_ledger`, `statistics` for `kudos_stat_events`), `partition_name`, `unit_lower` (NULL
for `MINVALUE`), `unit_upper`, `state`, `object_key`, `error` and these bookkeeping columns:

| Column | Meaning |
| --- | --- |
| `row_count`, `max_id` | Row count and highest `id` of the unit's range, with no filter. Recorded in the transaction that commits the replacement: the checkpoint for currency, the rollup for statistics. NULL before that |
| `first_failed_at` | When the current run of failed statistics dumps began. NULL after a successful dump |
| `consecutive_dump_failures` | Failed statistics dump attempts since the last success |
| `last_dump_attempt_at` | When the statistics dump was last attempted, by the tick's reference time. Spaces the retries |
| `detached_at` | The tick's reference time when the partition was detached, set on every unit before the recount that follows the detach. With `KUDOS_RETENTION_DROP_GRACE_DAYS` above 0 the drop is due once the grace has passed since it. A unit that recount blocks keeps it, so `retention reattach` finds the partition. NULL while the partition is attached. A `dropped` unit keeps the value it had |
| `state_before_detach` | The unit's state before that detach. It stays set while the unit is blocked with `reattached by operator`, and `retention unblock` restores it. NULL otherwise |

A currency unit in `checkpointed`, `exported`, `verified`, `detached` or `dropped` records the checkpoint boundary at
`unit_upper`. The checkpoint chain is read from these catalog rows, because an empty unit does not write any
`kudos_ledger_checkpoints` rows.

A unit's `source_range` is `<partition_name>/<lower>_<upper>`. Each bound is formatted as `%Y%m%dT%H%M%S`, and a
`MINVALUE` lower bound is written as `min`. Rollups, quarantine rows and daily statements store the `source_range` of
the unit they came from. A retried step deletes the rows with its own `source_range` before inserting them again. A UTC
day can span two units, so readers sum across `source_range`.

A unit is eligible when its upper bound is older than the dataset's window. A tick creates the catalog rows of a
weekly partition, in state `pending`, once its upper bound is older than the window, and the rows of the legacy
partition the first time it sees it attached, because the legacy sub-ranges age one by one and the partition's own
upper bound, the swap time, is the bound of its last sub-range only. Units are processed in order of upper bound,
oldest first.

### Retention states

The states are `KudosRetentionState` values. Each arrow is one transition, and a tick makes at most one transition.

| Dataset | Path |
| --- | --- |
| `statistics` | `pending` -> `rolled_up` -> `dumped` -> `dropped`, or `rolled_up` -> `dump_failed` (retried) -> `dumped`, or `dump_failed` -> `dump_waived` (operator) -> `dropped`, or without an archive `rolled_up` -> `dump_waived` -> `dropped`. A dump over the statistics budget makes the unit `blocked` |
| `currency` | `pending` -> `checkpointed` -> `exported` -> `verified` -> `dropped` |

With `KUDOS_RETENTION_DROP_GRACE_DAYS` above 0, the last arrow of both paths is two transitions: the drop-ready state
-> `detached` -> `dropped`. `retention reattach` moves a `detached` unit to `blocked` with `reattached by operator`,
and `retention unblock` then returns it to its drop-ready state.

| State | Dataset | Meaning |
| --- | --- | --- |
| `pending` | Both | Catalogued. A replacement is not committed yet |
| `rolled_up` | `statistics` | Applied, non-quarantined rows are summed into `kudos_stat_rollups`, and quarantined rows are copied to `kudos_stat_quarantine`. The rollup totals matched the unit's totals in the same transaction |
| `dumped` | `statistics` | A compressed copy of the unit is in the statistics tier of the archive. The dump read the object back in full and it matched its manifest: digest, stored bytes, row count, amount sum and columns |
| `dump_failed` | `statistics` | The last dump attempt failed, either the upload or its read-back against the manifest. The dump is retried once `DUMP_RETRY_SPACING` has passed since the attempt. The unit is not dropped in this state |
| `dump_waived` | `statistics` | The unit is dropped without a dump, because an operator accepted it or no archive is configured (`error` is then `archive not configured`). The rollup is the only replacement |
| `checkpointed` | `currency` | Per-user checkpoints at `unit_upper` and the unit's daily statements are committed in one repeatable-read transaction |
| `exported` | `currency` | The unit's rows and manifest are in the currency tier of the archive |
| `verified` | `currency` | The object was read back and matched its manifest, the online rows and the row count recorded at the checkpoint |
| `detached` | Both | The partition is detached concurrently and kept as a standalone table, for the drop grace period or until the drop that follows on the same tick. The unit is drop-ready while every unit of the partition that is not `dropped` is `detached`, and `state_before_detach` holds its previous state. Its rows are no longer read through the parent table |
| `dropped` | Both | The partition is detached concurrently and dropped |
| `blocked` | Both | The unit is stopped until an operator resets it. The reason is in `error`. A unit blocked while its partition is detached has `detached_at` set, and `retention unblock` refuses it until `retention reattach` attaches the partition |

Any state except `dropped` can become `blocked`. `retention unblock` returns a unit blocked with `reattached by operator` to `state_before_detach`.
Otherwise it returns a blocked currency unit whose checkpoint
is committed (`row_count` is set) to `checkpointed`, so export and verification run again against that checkpoint.
Every other blocked unit returns to `pending`, and its steps run again from the start. Each step replaces what an
earlier attempt wrote. `retention unblock --waive-dump` moves a `dump_failed` statistics unit, or one blocked for
`dump retries exhausted`, `statistics tier would exceed budget` or `archive object missing`, to `dump_waived`. It
refuses a unit of a detached partition until the partition is reattached.

`retention unblock --recheckpoint` returns a blocked currency unit and every later currency unit to `pending`, and
clears their `error`, `row_count`, `max_id` and `object_key`. In the same transaction it deletes the
`kudos_ledger_checkpoints` rows at the boundary and the `kudos_ledger_daily` rows of the `source_range` of each reset
unit with a recorded `row_count`. The latest checkpoint is then the last valid one, and reconciliation reads every
later posting from `kudos_ledger`. The next ticks recompute each checkpoint, daily
statement and recorded row count in upper-bound order, and each export replaces the object at its key. A blocked
currency unit stops the currency dataset, so every later unit is still online. The command refuses when a later
currency unit is `dropped`, because its rows can no longer be recounted, and when the unit's own partition is
detached. Use it when a unit blocks with `late rows`
or `online drift since export` because of rows committed after its checkpoint.

`retention reattach <unit-id>` attaches a `detached` partition back to its parent with
`ALTER TABLE <parent> ATTACH PARTITION <partition> FOR VALUES FROM (<lower>) TO (<upper>)`. The bounds are the
partition's recorded range: the lowest `unit_lower` (`MINVALUE` for the legacy partition) to the highest
`unit_upper`. Each `detached` unit becomes `blocked` with `reattached by operator` and keeps
`state_before_detach`, so the partition stays attached until an operator unblocks its units. A unit that the drop
recount blocked after the detach keeps its own reason and loses `state_before_detach`. A partition whose units are
all blocked that way, with no `detached` unit, is accepted too. The command clears
`detached_at` and commits with the attach. It refuses when no unit of the partition has `detached_at` set, or its
table no longer exists. `retention unblock` on a unit with `state_before_detach` set returns it to that state and clears the column,
so the partition is drop-ready again and the next tick detaches it again with a new grace period.
`retention unblock --recheckpoint` also accepts such a currency unit and clears the column. `retention unblock` refuses
a unit of a detached partition, and `retention unblock --recheckpoint` refuses when a later currency unit is detached.
Reattach those first.

All four commands take the retention lock and exit with an error while a tick holds it.

Units are selected by these rules:

- A tick selects the next unit of each dataset. When a step fails or is deferred, the tick runs the other dataset's
  unit.
- A blocked currency unit stops the currency dataset, because every later checkpoint chains from it. Blocked
  statistics units are skipped.
- A `dump_failed` unit retries only the dump, because its rollup is committed. It is selected again once
  `DUMP_RETRY_SPACING` (15 minutes) has passed since `last_dump_attempt_at`, and skipped until then, so later
  statistics units proceed. Statistics units do not chain. When a retry fails and the first failure is older than
  `KUDOS_RETENTION_DUMP_RETRY_DAYS`, the unit becomes `blocked` with `dump retries exhausted`. That attempt also
  counts toward `consecutive_dump_failures` and `horde.kudos.retention.dump_failures`.
- Without an archive, statistics units go from `rolled_up` to `dump_waived` with `error` set to
  `archive not configured`, and are dropped. Currency units stay `checkpointed` and are skipped, so later units are
  still checkpointed. A currency partition is not dropped without an archive.
- A partition is dropped only when each of its units is `dumped`, `dump_waived` or `verified`, or each of them is
  `detached`. A `blocked` unit of a detached partition is still selected, so it stops the currency dataset. The drop
  marks all of them `dropped`. An interrupted concurrent detach is finished with `DETACH PARTITION ... FINALIZE`.
- With `KUDOS_RETENTION_HOLD_DROPS` set to `true`, drop-ready units are skipped, so their partitions stay attached
  and the detach of a grace period is held too. Every other step runs. Once every eligible unit is drop-ready, ticks
  are `idle`, and each tick that holds at least one partition logs `Kudos retention holds <n> partitions`.
- With `KUDOS_RETENTION_DROP_GRACE_DAYS` above 0, the drop of a partition that was never kept detached recounts,
  detaches, recounts again and then marks its units `detached` with `detached_at` set to the tick's reference time.
  It does not drop the table. A `detached` unit is skipped until its reference time minus the grace reaches
  `detached_at`, so later units proceed. The drop then recounts the rows once more, because the partition may have
  been reattached and written in between, and drops the table. A `detached` partition whose table no longer exists
  is marked `dropped` when its drop falls due.
- The grace is measured with the tick's reference time, including an `--as-of` rehearsal time. A partition detached
  by a rehearsal tick is dropped only by a tick whose reference time is at least the grace later, and a tick at the
  real time does not drop it before the real clock reaches that point.
- Before the detach, and again after it, the drop recounts each unit's rows and highest `id` and compares them with
  `row_count` and `max_id`. A difference, or a unit with no recorded count, blocks that unit with `late rows` and
  nothing is dropped. Every unit is marked `detached` with `detached_at` before the recount that follows the detach,
  also without a grace period. A difference found then leaves the partition detached, the blocked unit keeps
  `detached_at`, its siblings stay `detached`, and the partition is not drop-ready. Run `retention reattach` before
  unblocking.
- The drop connection sets `lock_timeout` to `DROP_LOCK_TIMEOUT`. The connection is discarded afterwards instead of
  returning to the pool, because the setting lasts for the database session. When another session holds a lock on the partition
  or its parent in a transaction older than `KUDOS_RETENTION_DROP_DEFER_MINUTES`, or the timeout expires, the tick
  outcome is `deferred` with the pid and transaction age, and the drop is tried again on the next tick. The drop also
  checks that the connection holding the retention lock is alive, and fails without dropping when it is not.
- A tick whose reference time is later than the real clock is `refused` unless `confirm_live_data_loss` is set
  (`retention step --confirm-live-data-loss`).
- When the archive is configured, each tick aborts multipart uploads under the deployment's prefix that were started
  more than `STALE_UPLOAD_AGE` ago.
- A step that raises an error does not make a transition. Examples are an unreachable archive during export or a
  rollup whose totals differ from the unit's totals. The tick outcome is `failed`, and the step runs again on the
  next tick.

### Blocked reasons

The `error` of a blocked unit is one of the `RetentionBlockReason` values.

| `error` | Dataset and state | Condition |
| --- | --- | --- |
| `unapplied rows` | Both, at `pending` | The unit has a row with `applied = false`. For statistics, only rows with `quarantined = false` count |
| `checkpoint chain gap` | `currency`, at `pending` | No catalog unit with a recorded boundary ends at `unit_lower`, or one with a recorded boundary ends after `unit_upper`. Only a unit that starts at `MINVALUE` can start the chain without a previous boundary |
| `boundary already checkpointed` | `currency`, at `pending` | A unit of another partition already records a boundary at `unit_upper`. Recomputing it would replace that checkpoint with this unit's rows alone |
| `currency tier would exceed budget` | `currency`, at `checkpointed` | The stored currency tier, minus an object already stored at the unit's own key, plus the estimated export size is larger than `KUDOS_ARCHIVE_BUDGET_BYTES`. The estimate is the unit's share of the partition heap size times `ARCHIVE_SIZE_RATIO` |
| `statistics tier would exceed budget` | `statistics`, at `rolled_up` or `dump_failed` | The same test against the statistics tier and `KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES`. Nothing is uploaded, and later statistics units proceed |
| `archive mismatch` | `currency`, at `exported` | The object read back differs from its manifest in digest, size, row count, amount sum or per-user totals |
| `online drift since export` | `currency`, at `exported` | The manifest's row count differs from `row_count`, or the row count, amount sum or per-user totals of the online rows differ from the archived object |
| `late rows` | Both, at the drop | A unit's row count or highest `id` differs from `row_count` and `max_id`, or the unit has no recorded count |
| `dump retries exhausted` | `statistics`, at `dump_failed` | A dump retry failed more than `KUDOS_RETENTION_DUMP_RETRY_DAYS` after the first failure |
| `archive object missing` | `currency`, at `exported`, `verified` or `detached`, or at the drop. `statistics`, at `detached` with a `dumped` state before the detach, or at the drop | The tick's archive listing does not hold the unit's `object_key`, or the drop found a currency unit or a dumped statistics unit whose `object_key` or object is missing. A unit of a detached partition is reattached first. `retention unblock` then returns a currency unit to `checkpointed`, so it is exported and verified again, and a statistics unit to `pending`, so it is rolled up and dumped again. `retention unblock --waive-dump` drops a statistics unit without the dump |
| `reattached by operator` | Both, at `detached` | `retention reattach` attached the unit's partition again. `retention unblock` returns the unit to `state_before_detach` |

### Reconciliation after retention

`_applied_totals_subquery` sums the latest checkpoint and the applied postings at or above its boundary. Before the
first checkpoint, it sums every applied posting. Snapshots store cumulative applied totals, so a snapshot taken before
a checkpoint stays comparable after it. A boundary without per-user rows occurs only when no contributing posting is
older than it, so the latest boundary with rows, or none, still gives the right tail.

`create_balance_snapshot` holds the retention lock, so a snapshot never crosses a tick or an operator recovery, and it
raises `StaleCheckpointsError` while `stale_checkpoint_units` lists a unit. `retention unblock --recheckpoint` records,
in the transaction that deletes the stale checkpoints, one `kudos_snapshot_invalidations` row from the earliest reset
unit's catalog `created` time to the reset. A snapshot created inside such an interval stored applied totals from
checkpoints that omitted late rows, so `reconcile_balances` refuses it with `StaleCheckpointsError` in read-only and
repair modes alike. The snapshot's rows are kept as evidence. The interval is wider than the bad window. On a legacy
sub-range unit it starts at the migration, so one legacy recheckpoint invalidates every snapshot taken since the
cutover. Snapshots outside every interval stay usable, and a fresh snapshot after the recovery is the baseline for
later checks.

### Idempotency key lifetime

The key lifetime is the shorter of the two online windows (`KudosRetentionPolicy.key_lifetime`), which is 30 days by
default. `transfer_kudos` and repair look up `kudos_idempotency_keys` instead of `kudos_ledger`, and they write the key
row in the same transaction as their postings. Every tick deletes up to `EXPIRE_KEYS_BATCH_SIZE` keys older than the
lifetime, whether or not retention is enabled.

- A transfer `Idempotency-Key` is honored for the online retention window (30 days by default). A retry after that
  creates a new transfer.
- `reconcile_balances(..., apply_repairs=True)` refuses a snapshot older than the key lifetime with a `ValueError`
  whose message says to take a new snapshot. Without the refusal, a repair repeated after its key expired could post
  a second `RECONCILIATION`.
- `reconcile_balances(..., apply_repairs=True)` raises `StaleCheckpointsError`, a `ValueError`, while
  `stale_checkpoint_units` lists a currency unit blocked with `late rows` or `online drift since export`. That unit
  holds rows its checkpoint and every later checkpoint omit, so a repair would compensate for drift that does not
  exist. A read-only run still reports drift and logs a warning. The `reconcile` command lists the units in
  `stale_checkpoint_units` and sets `drift_reliable` to `false`. `retention unblock --recheckpoint` deletes the stale
  checkpoints, and repairs are accepted again.
- `sql_statements/5.1.16.txt` backfills the keys of `TRANSFER` and `RECONCILIATION` events created in the last
  `key_lifetime_days` days, a psql variable that defaults to 30. When either online window is above 30, the file
  is run with `-v key_lifetime_days=<days>` set to the shorter window, so every key inside the lifetime exists. The
  file prints the window it used.

### Retention configuration

`KUDOS_RETENTION_POLICY` is read from the environment at import. An invalid value stops startup with a `ValueError`
that includes the variable.

| Variable | Default | Constraint | Effect |
| --- | --- | --- | --- |
| `KUDOS_RETENTION_ENABLED` | `false` | `true`, `1`, `yes`, `false`, `0` or `no` | Turns partition removal on. Partitions are created and keys expire either way |
| `KUDOS_RETENTION_CURRENCY_DAYS` | 30 | At least 30 | Online window of `kudos_ledger`, counted from a partition's upper bound |
| `KUDOS_RETENTION_STATISTICS_DAYS` | 30 | At least 30 | Online window of `kudos_stat_events`, counted from a partition's upper bound |
| `KUDOS_PARTITION_WIDTH_DAYS` | 7 | 1 to 31 | Width of new partitions and of legacy units. The width adds to the effective retention |
| `KUDOS_ARCHIVE_BUDGET_BYTES` | Unset, no limit | Positive whole number. Required when the archive is configured | Byte budget of the currency tier (`KudosRetentionPolicy.currency_budget_bytes`) |
| `KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES` | Unset, no limit | Positive whole number | Byte budget of the statistics tier (`KudosRetentionPolicy.statistics_budget_bytes`) |
| `KUDOS_RETENTION_DUMP_RETRY_DAYS` | 7 | Positive whole number | How long failed statistics dumps are retried, from the first failure, before the unit blocks |
| `KUDOS_RETENTION_DROP_DEFER_MINUTES` | 5 | Positive whole number | Transaction age of another session's lock on the partition or its parent that defers a drop |
| `KUDOS_RETENTION_HOLD_DROPS` | `false` | `true`, `1`, `yes`, `false`, `0` or `no` | Keeps drop-ready partitions attached. Every other step runs |
| `KUDOS_RETENTION_DROP_GRACE_DAYS` | 7 | Whole number, 0 or more | How long a detached partition is kept before it is dropped. 0 drops it right after the detach and has to be set explicitly, so an environment that lost the variable keeps a reattach window |
| `KUDOS_ARCHIVE_ACCOUNT` | Unset | Required for the archive | S3 endpoint URL |
| `KUDOS_ARCHIVE_BUCKET` | Unset | Required for the archive | Archive bucket |
| `KUDOS_ARCHIVE_AWS_ACCESS_KEY_ID` | Unset | Required for the archive | Access key ID of credentials scoped to the archive bucket |
| `KUDOS_ARCHIVE_AWS_SECRET_ACCESS_KEY` | Unset | Required for the archive | Secret of those credentials |
| `KUDOS_ARCHIVE_REGION` | `auto` | | Signing region |
| `KUDOS_ARCHIVE_PREFIX` | Empty | Letters, digits, `-`, `_`, `/` and `.` | Key prefix under which this deployment writes and lists its objects, so deployments can share a bucket. A trailing slash is added |

The archive is configured only when the four required `KUDOS_ARCHIVE_*` variables are all set. When only some are set,
`KudosArchive.from_environment` logs one warning that lists the unset variables and returns `None`.

### Archive objects

The archive is one S3-compatible bucket. Each unit is one compressed CSV object with a header row, produced by `COPY`
of the unit's rows in `id` order, and a JSON manifest stored beside it. Objects are written once.

| Item | Layout |
| --- | --- |
| Object key | `<prefix><tier>/<source_range>.csv.<ext>`, with each `/` in `source_range` replaced by `__`. `prefix` is `KUDOS_ARCHIVE_PREFIX`. `tier` is `currency` or `statistics`. The key has no dataset segment because the tier already names the dataset, and the manifest records it |
| Manifest key | The object key followed by `.manifest.json` |
| Codec | zstd (`zst`) for new objects, at `ZSTD_COMPRESSION_LEVEL`. gzip (`gz`) objects stay readable. A reader selects the codec by the key's extension |

| Manifest field | Contents |
| --- | --- |
| `schema_version` | Manifest format version. New manifests are version 2. A version 1 manifest does not record column types, and restore refuses it. Verification and audit read both versions |
| `dataset`, `partition_name` | Where the rows came from |
| `unit_lower`, `unit_upper` | The unit's bounds as `%Y%m%dT%H%M%S`. `unit_lower` is null for `MINVALUE` |
| `row_count` | CSV data rows |
| `compressed_sha256`, `compressed_bytes` | Digest and size of the stored bytes |
| `columns` | The CSV header |
| `column_types` | Version 2: each column's `name` and PostgreSQL `type` in CSV column order, as `format_type` reports them with modifiers, such as `numeric(20,2)`. The export reads them from a temporary view over its query in the export transaction, and aborts the upload when the CSV header differs from them. Absent in version 1 |
| `codec` | `zstd` or `gzip` |
| `amount_sum` | Sum of `amount`, as a decimal string |
| `per_user_totals` | Currency only: user ID to `[balance_sum, escrow_sum]` as decimal strings. Null for statistics |

Each tier has its own budget, and a tick does not evict anything. The statistics dump and the currency export each
admit an upload only when the tier's stored bytes, minus an object already stored at the unit's own key, plus the
estimate fit the tier's budget. Otherwise the unit blocks with `statistics tier would exceed budget` or
`currency tier would exceed budget`. The estimate is an early admission check. The upload also counts the actual
compressed bytes, including the compressor's final flush, against the same allowance, and an overrun aborts the
multipart upload and blocks the unit with the same reason without replacing an object already at the key. Budgets
count completed data objects and exclude manifests and incomplete multipart uploads. Every writer to a tier holds the
retention lock, including `archive evict`.

`archive evict --tier statistics --budget-bytes <n>` runs `evict_statistics_dumps`, which deletes the oldest
statistics objects and their manifests, ordered by unit upper bound, until the statistics tier fits `<n>`. The dump of
a statistics unit that is not `dropped` is kept, because its raw rows are still online. Objects that are not recorded by any
unit are deleted like dropped dumps. Currency objects are never deleted. The deletes go out one batch at a time. When one
fails, `ArchiveEvictionError` holds the keys deleted before it, and the command prints them. The command sets the
`object_key` of each evicted unit to NULL, also after a failed batch, so an evicted dump is not reported as lost.

With an archive configured, every tick lists both tiers after its step, while it still holds the retention lock. A
unit whose `object_key` is not in the listing was removed outside the horde, or by an eviction that stopped before it
updated the catalog. What the tick does depends on the unit:

| Unit | Change | Log line |
| --- | --- | --- |
| `currency` in `exported`, `verified` or `detached` | `blocked` with `archive object missing`, `object_key` NULL, because a drop would remove the only copy of its postings | `Kudos retention blocked <dataset> unit <source_range>: archive object missing: ...` |
| `statistics` in `dumped` | `dump_failed`, `object_key` NULL, `error` `archive object missing: <key>`. The dump retry window starts at that tick, and the next tick dumps the rows again | `Kudos retention dump failed for <source_range>: the archive does not hold <key>, so the unit is dumped again` |
| `statistics` in `detached`, with `dumped` before the detach | `blocked` with `archive object missing`, `object_key` NULL | `Kudos retention blocked <dataset> unit <source_range>: archive object missing: ...` |
| `statistics` in `dropped` | None. The unit keeps its `object_key`, so `archive audit` reports the lost object on every run | None |
| Any other | `object_key` NULL | `Kudos retention cleared object key <key> of <dataset> unit <id> (<source_range>, <state>)` |

Before the detach and again immediately before the drop, retention checks every unit of the partition against the
archive. For a currency unit, the object exists, its manifest is readable, the stored size equals the manifest's
`compressed_bytes`, and the manifest's `row_count` equals the unit's recorded count. For a statistics unit in
`dumped`, or `detached` with `dumped` before the detach, the object exists. A missing key or object blocks the unit
with `archive object missing`, a currency mismatch blocks it with `archive mismatch`, and an unreachable store or
unreadable manifest fails the tick so that a later tick retries. The check compares metadata only. The digest was
verified at the `verified` step or by the dump's read-back, and `archive audit --read-back` re-reads objects. The
post-step listing does not gate the drop.
The tier sizes from the same listing are the `horde.kudos.archive.tier_bytes` values.

### Retention metrics and log lines

`kudos_retention_tick` runs on the quorum node every 60 seconds and records these metrics after each tick.

| Metric | Type and unit | Attributes | Meaning |
| --- | --- | --- | --- |
| `horde.kudos.retention.steps` | Counter, `1` | `horde.kudos.dataset`, `horde.kudos.retention.step`, `horde.kudos.retention.outcome` | One per tick. The step is the `RetentionStep` that ran, or `none`. The outcome is a `RetentionTickOutcome`: `skipped_lock`, `idle`, `stepped`, `blocked`, `failed`, `refused` or `deferred`. Each tick adds 0 to the `blocked` and `failed` series of both datasets, so those series exist before the first event |
| `horde.kudos.retention.dump_failures` | Counter, `{failure}` | `horde.kudos.dataset` | One per failed statistics dump attempt, including the one that blocks the unit. Each tick adds 0 for both datasets |
| `horde.kudos.retention.oldest_eligible_age` | Gauge, `s` | `horde.kudos.dataset` | How long the oldest unit still online has been past its window, by sub-range for the legacy partition. 0 when every unit is inside its window |
| `horde.kudos.retention.blocked_units` | Gauge, `{unit}` | `horde.kudos.block_reason` | Catalog units in `blocked`, one series per `RetentionBlockReason` value, 0 when no unit has that reason |
| `horde.kudos.retention.partition_coverage` | Gauge, `1` | None | 1 when each event table has a partition containing the real current time and one containing it plus one partition width, else 0 |
| `horde.kudos.retention.held_partitions` | Gauge, `{partition}` | None | `held_partitions` from `retention_health`. 0 when `KUDOS_RETENTION_HOLD_DROPS` is not `true` |
| `horde.kudos.retention.pending_units` | Gauge, `{unit}` | `horde.kudos.dataset` | `pending_units_by_dataset` from `retention_health`. The units of the legacy partition are counted until the whole partition is dropped, so the value stays high for a whole catch-up |
| `horde.kudos.retention.restored_partitions` | Gauge, `{partition}` | None | The number of `restored_partitions` from `retention_health`. Above 0, the tick does not make a transition in the dataset of each such partition |
| `horde.kudos.archive.tier_bytes` | Gauge, `By` | `horde.kudos.archive.tier` | Stored bytes of each tier, excluding manifests, from the tick's listing. Not recorded without an archive, and kept at the last value when the listing fails |
| `horde.kudos.archive.budget_bytes` | Gauge, `By` | `horde.kudos.archive.tier` | One series per tier that has a budget: `currency` from `KUDOS_ARCHIVE_BUDGET_BYTES`, `statistics` from `KUDOS_ARCHIVE_STATISTICS_BUDGET_BYTES`. Not recorded without an archive |

Two warnings are under log contract. Log-based alerting matches their prefixes, and
`tests/unit/test_kudos_retention.py::test_log_contract_prefixes_start_the_outcome_warnings` tests them.

| Prefix | Constant | Logged when | Full line |
| --- | --- | --- | --- |
| `Kudos retention blocked` | `RETENTION_BLOCKED_LOG_PREFIX` | A unit becomes `blocked`, or a tick does not make a transition in a dataset because of a restored partition | `Kudos retention blocked <dataset> unit <source_range>: <error>`, optionally followed by `: <detail>`, or `Kudos retention blocked <dataset> dataset: an attached partition has dropped catalog units: <partitions>` |
| `Kudos retention dump failed` | `RETENTION_DUMP_FAILED_LOG_PREFIX` | A statistics dump attempt fails and the unit stays `dump_failed`, the dump step waives a unit because no archive is configured, or the tick finds the dump of a `dumped` unit missing | `Kudos retention dump failed for <source_range>: <cause> (<n> consecutive failures since <first failure>)`, `...: archive not configured`, or `...: the archive does not hold <key>, so the unit is dumped again` |

The dump-failed warning is logged on every failed attempt. The attempt that exhausts the retry window logs the blocked
warning instead, with `dump retries exhausted` and the same detail. Without an archive, the dump-failed warning is
logged once per statistics unit, with the cause `archive not configured`, and `horde.kudos.retention.dump_failures`
does not count it. Other retention log lines are not under contract.

`retention_health` returns these fields. `retention status` prints them with the archive state and the units.

| Field | Meaning |
| --- | --- |
| `oldest_eligible_age_seconds` | Per dataset, how long the oldest unit still online has been past its window, by sub-range for the legacy partition, or null |
| `blocked_units` | Catalog units in `blocked` |
| `blocked_units_by_reason` | Catalog units in `blocked` per `RetentionBlockReason` value, with every reason present |
| `pending_units` | Catalog units neither `dropped` nor `blocked`, including currency units waiting for an archive and `detached` units |
| `pending_units_by_dataset` | `pending_units` per dataset, with both datasets present |
| `last_step_age_seconds` | Seconds since the most recent catalog update, or null for an empty catalog |
| `partition_coverage_ok` | Whether each event table has a partition containing the real current time and one containing it plus one partition width |
| `hold_drops` | Whether `KUDOS_RETENTION_HOLD_DROPS` is `true` |
| `held_partitions` | Partitions whose units, other than `dropped` ones, are all drop-ready, while `hold_drops` is true. 0 otherwise |
| `drop_grace_days` | The drop grace of the running policy in days, from `KUDOS_RETENTION_DROP_GRACE_DAYS` or its default |
| `detached_partitions` | One entry per partition with `detached` units, oldest detach first: `dataset`, `partition_name`, `detached_at` and `drop_due_at` (`detached_at` plus the grace) |
| `restored_partitions` | One entry per attached partition with `dropped` catalog units, as `dataset` and `partition_name`. Such a partition was restored under its original name. The dataset does not make a transition until the partition is detached |

### Retention commands

These are subcommands of `tools/kudos_ledger_admin.py`. Each prints JSON.

| Command | Effect |
| --- | --- |
| `retention status` | Prints the health fields, `archive_configured`, `archive_tier_bytes`, and every unit not yet dropped with its catalog columns and `source_range`. Read-only |
| `retention step [--dataset <dataset>] [--as-of <timestamp>] [--confirm-live-data-loss]` | Runs one tick as the scheduler does. With `--dataset currency` or `--dataset statistics`, only that dataset's units are stepped. `--as-of` is a rehearsal control: every eligibility, key-expiry and partition-creation decision is taken as of that UTC time, which must be no earlier than the current time and no later than the current time plus the longer window, the drop grace and four partition widths, and retention must be enabled. With a drop grace, a partition detached at one `--as-of` is dropped only by a later tick whose `--as-of` is at least the grace past it. A time later than now is refused without `--confirm-live-data-loss`, because against a live database it removes current data. Use that flag on a restored copy only. It can drop a partition |
| `retention unblock <unit-id> [--waive-dump \| --recheckpoint]` | Returns a blocked unit to `state_before_detach` when it is set (a unit blocked by `retention reattach`), else to `checkpointed` (currency unit with a committed checkpoint) or `pending`, and clears `error`. With `--waive-dump`, moves a `dump_failed` statistics unit, or one blocked for `dump retries exhausted`, `statistics tier would exceed budget` or `archive object missing`, to `dump_waived`. With `--recheckpoint`, returns a blocked currency unit and every later currency unit to `pending` so their checkpoints are recomputed, and refuses when a later currency unit is `dropped` or `detached`. Refuses a unit of a `detached` partition. Exits with an error for any other unit, or while a tick holds the retention lock |
| `retention reattach <unit-id>` | Attaches the `detached` partition of the unit back to its parent over its recorded range and blocks its units with `reattached by operator`, keeping `state_before_detach`. `retention unblock` on each unit returns it to that state. Refuses when the partition is not `detached` or its table no longer exists. Exits with an error while a tick holds the retention lock |
| `statement <user-id> --from <YYYY-MM-DD> --to <YYYY-MM-DD>` | Prints the user's lines from `kudos_ledger_daily` for those UTC days, summed across `source_range`. Days still online do not have lines yet |
| `archive restore <key> --into <scratch-table> [--append]` | Copies an archive object into a new scratch table, `table` or `schema.table`, created from the manifest's `column_types` alone, without reference to the live event tables. A version 1 manifest does not record column types and is refused. An existing table is refused, because a repeated restore would hold every row twice and each restore verifies only its own rows. With `--append` the rows are added to an existing table, and the output has `"appended": true` and a note. Requires the archive configuration |
| `archive evict --tier statistics --budget-bytes <n>` | Deletes the oldest statistics objects of `dropped` units and objects without a unit until the statistics tier fits `<n>`, sets the `object_key` of each evicted unit to NULL, and prints the deleted keys and `tier_bytes`, the stored bytes left. Exits 1 with an `error` when the dumps of units not yet dropped keep the tier above `<n>`. On a failed delete it prints the keys deleted before it with the error and exits 1 |
| `archive audit [--tier currency\|statistics] [--since-key <key>] [--read-back]` | Checks every object of the tier, both tiers by default, against its manifest without downloading it: the manifest is readable, its `compressed_bytes` equals the size in the listing, and its `unit_upper` and `codec` equal the ones the key encodes. This finds a deleted, replaced or truncated object, a lost manifest and a manifest that belongs to another object. Damage that keeps an object's size is found only with `--read-back`, which downloads every object and compares its digest, stored bytes, row count, amount sum, columns and per-user totals with the manifest, so it reads the whole archive. Each object was read back in full once when it was written, at the currency `verified` step or by the statistics dump. Prints one JSON line per object (`"kind": "object"`, `matches_manifest`, `problems`), one per catalog unit whose object is missing (`"kind": "missing_object"`), then a summary line with `objects`, `problems`, `last_key`, `read_back`, `noncurrent_bytes` and `delete_markers`. The last two map each audited tier to the summed size of the noncurrent object versions under its prefix, manifests included, and to its count of delete markers. They show storage a versioned bucket still holds for evicted or replaced objects, which the `horde.kudos.archive.tier_bytes` gauge and `retention status` do not count. A tier's value is `null` when the store did not list its versions, which differs from `0`. Neither figure counts as a problem. A unit is missing its object when its `object_key` is not listed, or when it is a currency unit in `exported`, `verified`, `detached` or `dropped` without an `object_key`. Exits 1 on any problem, including an unreadable manifest. `--since-key` audits only objects whose key sorts after `<key>`, to resume from a summary's `last_key`. It reads the catalog and changes nothing |

### Privacy disclosure

`AccountingRetentionDisclosure.from_policy` derives the two windows, and whether currency is archived, from the same
policy and `KUDOS_ARCHIVE_*` variables that the retention job reads. The privacy policy renders them under "Retention
of Your Personal Data".

## Observability contract

`kudos_applier_health` reports:

| Field | Meaning |
| --- | --- |
| `pending_rows` | Combined unapplied currency and drainable, non-quarantined statistic event count |
| `oldest_pending_seconds` | Age of the oldest drainable unapplied event across both queues |
| `ledger_pending_rows` | Drainable unapplied currency posting count |
| `stat_pending_rows` | Drainable unapplied statistic event count |
| `oldest_ledger_pending_seconds` | Age of the oldest drainable currency posting |
| `oldest_stat_pending_seconds` | Age of the oldest drainable statistic event |
| `quarantined_rows` | Total statistic rows retained outside the drainable queue; permanent evidence, not unresolved-incident state |
| `oldest_quarantined_seconds` | Age of the oldest quarantined statistic row |
| `newest_quarantined_seconds` | Age of the most recently quarantined statistic row; operationally useful for identifying a new incident |
| `heartbeat_seconds` | Time since the projector last completed a cycle; may grow even when no rows are pending |
| `active_reservations` | Count of holds with positive remaining amount and no release time |
| `oldest_reservation_seconds` | Age of the oldest active hold |

### Projector cadence and health sampling

The background task runs every three seconds and keeps folding within a tick while full batches drain, up to a bounded
number of catch-up cycles, so a backlog clears at many batches per tick while each fold stays a small transaction.
Health is sampled once per tick, before the fold loop, and exported as current-value gauges. The combined queue and
oldest-age gauges have currency/stat splits, while the quarantine gauges distinguish permanent evidence from the age
of the newest incident. Phase histograms, cycles-per-tick and configured batch/catch-up gauges explain capacity limits.
### Alerting and node health

These metrics are recorded on the quorum node only. The per-node
`/api/v2/status/heartbeat` endpoint deliberately excludes them, because a load balancer health check fed a
shared-database signal would remove every node from rotation at once. Operators should alert on queue age and
reservation age from the quorum telemetry, not only row count: a steady queue can be healthy under load, while one
old row or hold can indicate a poisoned path.

## Code map

| Responsibility | File | Symbols |
| --- | --- | --- |
| Models, event context, mode control and event emission | `horde/classes/base/kudos.py` | `KudosLedger`, `KudosStatEvent`, `kudos_event`, `set_kudos_ledger_mode`, `emit_kudos_ledger_entry`, `emit_kudos_stat_event` |
| Bounded projection, trust promotion, floor recording and health | `horde/database/kudos_ledger.py` | `apply_pending_kudos`, `kudos_applier_health` |
| Available/effective balances and hold lifecycle | `horde/database/kudos_reservations.py` | `available_kudos`, `effective_kudos`, `reserve_kudos`, `consume_reservation`, `release_reservation` |
| Repeatable-read snapshots, drift and compensating repairs | `horde/database/kudos_reconciliation.py` | `create_balance_snapshot`, `reconcile_balances` |
| Advisory locks and transaction isolation | `horde/database/kudos_db.py` | `acquire_applier_lock`, `acquire_payer_lock`, `acquire_mode_pin`, `acquire_mode_gate_exclusive`, `begin_repeatable_read` |
| Atomic counter upserts by database dialect | `horde/database/kudos_counters.py` | `increment_counter`, `increment_counters` |
| Temporary shadow-mode inline projection and cutover deletion boundary | `horde/database/kudos_legacy_projection.py` | `project_user_balance`, `project_user_escrow`, `project_trust_promotion`, `project_style_reward` |
| User currency and statistic producers | `horde/classes/base/user.py` | `User.modify_kudos`, `User.record_usage`, `User.record_contributions`, `User.record_uptime`, `User.record_style` |
| Worker display/stat producers and contribution orchestration | `horde/classes/base/worker.py` | `WorkerTemplate.modify_kudos`, `WorkerTemplate.record_contribution`, `WorkerTemplate.record_uptime` |
| Generation settlement event boundary and reservation cleanup | `horde/classes/base/processing_generation.py` | `ProcessingGeneration.record` |
| Activation tax, usage debit and queue-priority snapshot | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt._activate`, `WaitingPrompt.record_usage` |
| Interrogation reservation, settlement and cleanup | `horde/classes/stable/interrogation.py` | `InterrogationForms.record` |
| Transfer admission, paired postings and idempotency | `horde/database/functions.py` | `transfer_kudos` |
| Periodic projection and health metrics | `horde/database/threads.py` | `apply_kudos_ledger` |
| Accounting discriminators and metadata keys | `horde/enums.py` | `KudosEntryType`, `KudosLedgerMode`, `KudosAuditDetail` |
| Initial schema and counter uniqueness | `sql_statements/5.1.0.txt` | SQL migration |
| Quarantine columns and concurrent drainable-queue index replacement | `sql_statements/5.1.9.txt` | SQL migration. Run with psql autocommit |
| Retention policy, tick, steps, catalog, health, statements, restore, archive audit and disclosure | `horde/database/kudos_retention.py` | `KudosRetentionPolicy`, `KUDOS_RETENTION_POLICY`, `run_retention_tick`, `select_next_unit`, `STEP_TABLE`, `RetentionBlockReason`, `retention_health`, `unblock_unit`, `reattach_partition`, `daily_statement`, `restore_archive_unit`, `audit_archive_metadata`, `audit_archive_contents`, `units_without_archive_objects`, `AccountingRetentionDisclosure` |
| Archive client, manifest, codecs, operator eviction, object audit and version usage | `horde/kudos_archive.py` | `KudosArchive`, `ArchiveManifest`, `ArchiveColumn`, `ArchiveObjectAudit`, `ArchiveVersionUsage`, `ArchiveTier`, `ZstdCodec`, `GzipCodec`, `KudosArchiveError`, `ArchiveEvictionError` |
| Partition bootstrap and retention models | `horde/classes/base/kudos.py` | `ensure_kudos_event_partitions`, `KudosPartitioning`, `KudosStatRollup`, `KudosStatQuarantine`, `KudosLedgerCheckpoint`, `KudosLedgerDailyStatement`, `KudosIdempotencyKey`, `KudosRetentionUnit` |
| Retention lock | `horde/database/kudos_db.py` | `try_acquire_retention_lock`, `release_retention_lock` |
| Checkpoint plus online tail and repair key lifetime | `horde/database/kudos_reconciliation.py` | `_applied_totals_subquery`, `_refuse_snapshot_older_than_key_lifetime` |
| Periodic retention tick, metrics and log contract lines | `horde/database/threads.py` | `kudos_retention_tick` |
| Retention tables, key backfill and partition conversion | `sql_statements/5.1.16.txt` | SQL migration. Run with psql and `ON_ERROR_STOP=1` |
| Status, drain, snapshot, reconciliation, repair, promotion-statistic backfill, mode, retention, statement and archive restore, evict and audit commands | `tools/kudos_ledger_admin.py` | `main` |

See [Kudos ledger operations](../how-to/kudos_ledger_operations.md) for cutover, rollback and recovery rehearsals.

## Regression-test map

| Contract | Primary coverage |
| --- | --- |
| Emission shape, event grouping, currency/stat separation | `tests/unit/test_kudos_ledger.py`, `test_kudos_balance.py`, `test_kudos_safety.py` |
| Existing generation, cancellation, uptime, monthly, interrogation semantics | `test_kudos_settlement.py`, `test_kudos_image_cancel.py`, `test_kudos_uptime.py`, `test_kudos_monthly.py`, `test_kudos_interrogation.py` |
| Counter and team fold equivalence | `test_kudos_counters.py`, `test_kudos_counter_fold.py` |
| Concurrent projector exclusion and spend reservation | `test_kudos_safety.py` on PostgreSQL-capable paths |
| Transfer replay and parameter conflict | `test_kudos_safety.py`, integration kudos endpoint tests |
| Trust promotion on final qualifying posting | `test_kudos_safety.py`, `test_kudos_ledger.py` |
| Atomic ledger-to-shadow final drain | `test_kudos_safety.py` |
| Snapshot drift, idempotent compensation, floor replay | `test_kudos_safety.py` |
| Style author credited only the collected surcharge | `test_kudos_ledger.py` (`TestStyleRewardAttribution`), integration `test_text_style_application.py` (`TestTextStyleAuthorCredit`, `TestTextStyleAuthorCreditInShadowMode`) |
| Remaining activation deadlock retry/failed-session telemetry | `test_wp_activate_deadlock.py` |
| Image/text/interrogation upfront admission behavior | Integration request-activation tests and `test_kudos_interrogation.py` |
| Migration idempotency against mapped schema | `test_kudos_safety.py` |
| Partition conversion, index attachment, sequence continuity, key backfill and ORM parity | Integration `test_kudos_retention_migration.py` |
| Retention policy, unit selection, state transitions, blocked reasons, rollups, checkpoints, export, drop and log contract | `test_kudos_retention.py` |
| Archive codecs, manifests, object keys, verification, operator eviction and restore | `test_kudos_archive.py` (object-store tests use the `object_storage` marker) |
| Key expiry for transfers, checkpoint plus tail parity, drift after a checkpoint, repair refusal | `test_kudos_safety.py` |
| Legacy partitions retired with reconciliation, idempotency and projection intact | Integration `test_kudos_retention_end_to_end.py` |

The unit suite provisions PostgreSQL through testcontainers, so the advisory-lock and concurrent-projector tests
exercise the real primitives. The legacy `USE_SQLITE` runtime mode short-circuits those locks and is not covered by
the suite. Before cutover or a recovery rehearsal, run the safety and integration suites as required by the
operations guide.

## Review checklist for a new kudos change

- Is the value currency, a display attribution, a counter, a price, or a quota?
- What is the business event boundary, and which postings share its `event_id`?
- Is the operation externally retryable? If so, what stable key and parameter-conflict rule make it idempotent?
- Can it authorize spend? If so, where is the payer lock and stable reservation?
- Which projection owns the read, what lag can its consumer tolerate, and should the consumer use materialized,
  available, or effective balance?
- Does the producer capture mutable attribution such as team membership at event time?
- Does the change introduce another lock? Where does it fit in the documented order?
- Can a batch split the event, and does any event-wide side effect handle that case?
- Is every non-linear adjustment represented by a posting?
- Can the change be rolled back through the supported mode transition without running old code against new holds?
- Do tests cover emission, fold, concurrency/retry behavior, and recovery, not merely the final number?
- Have this reference, the explanation, and the operations guide been updated when the contract changed?
