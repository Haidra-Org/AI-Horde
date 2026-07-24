<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Kudos accounting reference

This page is the maintainer contract for kudos mutation and consumption. It inventories the authoritative events,
materialized targets, producers, consumers, locks, and tests. For design rationale, read [Kudos accounting,
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
| Audit/replay role | Permanent forward audit; opening balance predates it | Authoritative post-cutover movement archive |
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
| `KudosLedger` / `kudos_ledger` | Permanent currency postings | Required `users.id` foreign key with `ON DELETE RESTRICT`; authoritative history must not be orphaned | One user target; spendable vs escrow selected by `escrow`; `NUMERIC(20,2)` amount; NaN rejected; partial unapplied index |
| `KudosStatEvent` / `kudos_stat_events` | Permanent display and counter postings | User, worker, and team IDs are immutable audit references, intentionally not ownership foreign keys because workers/teams may be hard-deleted | Exactly one of `user_id` and `worker_id`; typed unit; NaN rejected; partial unapplied index |
| `KudosReservation` / `kudos_reservations` | Payer holds for upfront work and transfers | User ownership foreign key with `ON DELETE CASCADE` | Unique `business_id`; positive original amount; non-negative remaining amount; active-user partial index |
| `KudosBalanceSnapshot` / `kudos_balance_snapshots` | User-currency reconciliation baseline | User ownership foreign key with `ON DELETE CASCADE` | One row per snapshot/user; records balance, escrow, and visible applied totals |
| `KudosLedgerControl` / `kudos_ledger_control` | Single-row mutation-mode control | Installation state | ID 1; `shadow` or `ledger`; non-null change time |
| `KudosLedgerApplierState` / `kudos_ledger_applier_state` | Projector heartbeat | Operational state only | ID 1; not a watermark and not used for exactly-once folding |

The mapped accounting models use SQLAlchemy 2 `Mapped`/`mapped_column` attributes. Runtime dialect operations are
confined to typed helpers: advisory locks and isolation in `horde/database/kudos_db.py`, counter upserts in
`horde/database/kudos_counters.py`, and legacy mode behavior in `horde/database/kudos_legacy_projection.py`. Raw DDL
belongs in the versioned migration file, not request code. The SQLAlchemy mypy plugin checks the accounting model
surface in CI.

### `KudosLedger` fields

| Field | Contract |
| --- | --- |
| `id` | Database identity used only for bounded claim order; it is not a transaction watermark. |
| `created` | Audit timestamp and queue-age source. |
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

### Stable enums and audit keys

| Enum | Values/role |
| --- | --- |
| `KudosLedgerMode` | `shadow`, `ledger` |
| `KudosUnit` | `kudos`, `things`, `count` |
| `KudosStatRecord` | `user_kudos`, `worker_kudos`, `last_active` |
| `KudosAggregate` | `contributions`, `fulfilments` |
| `KudosAuditDetail` | `reason`, `reservation_id`, `snapshot_id`, `touch_last_active` |

Do not add an untyped string discriminator when one of these axes describes it. Add or extend the enum and teach the
projector and tests about the new value together.

## Entry-type catalogue

An entry type answers “why did this posting exist?” The sign and target answer “what did it do?”

| `KudosEntryType` | Expected use |
| --- | --- |
| `GENERATION` | Image/text/interrogation settlement: requester debit, worker-owner spendable/escrow credit, and worker display event as applicable |
| `UPTIME_REWARD` | Periodic worker display credit and owner spendable/escrow credit |
| `EVALUATION_PROMOTION` | Paired escrow debit and spendable credit after trust promotion |
| `TRANSFER` | Paired source debit and destination credit for a user gift |
| `ADMIN_ADJUSTMENT` | Signed administrator adjustment |
| `AWARD` | Monthly, rating/aesthetic, and other application award credits |
| `STYLE_REWARD` | Style-owner currency reward; style object's own counters remain separate |
| `STAT_RECORD` | User record counts or thing totals |
| `STAT_CONTRIBUTION` | Worker/team contribution and fulfilment aggregates |
| `STAT_ACTIVITY` | Asynchronous user `last_active` touch |
| `FLOOR_ADJUSTMENT` | Explicit currency created when an account-floor rule forgives a debit |
| `RECONCILIATION` | Deterministic compensating currency posting emitted by an approved repair |

## Mutation contract

All new or modified kudos code must obey these rules.

1. **Use the public producer surface.** User currency enters through `User.modify_kudos` or
   `User.modify_evaluating_kudos`; derived events enter through `emit_kudos_stat_event`. Do not assign a materialized
   accounting column in a business path. Narrow exceptions are listed under [excluded kudos-like state](#excluded-kudos-like-state).
2. **Group a logical event.** Wrap a settlement, transfer, activation tax, or other multi-posting mutation in
   `kudos_event`. Include `job_id` and `wp_type` where available.
3. **Make external retries explicit.** If an API mutation can be retried independently of locked business state,
   accept a stable idempotency key, derive the event ID from it, and reject reuse with different parameters. A random
   event UUID provides correlation only.
4. **Commit business state and postings together.** Do not commit part of an event, perform network work, and append
   the rest later. Emission helpers flush by default so the caller owns the transaction boundary.
5. **One currency row, one user balance.** Worker/team IDs and counter units never belong in `KudosLedger`. Emit
   separate debit and credit rows; do not encode a transfer as an opaque net amount.
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
    `applied` as a repair. Emit a compensating posting.
13. **Add producer, fold, retry, and recovery tests.** At minimum pin the emitted rows and materialized result. A new
    spend path also needs concurrent overspend coverage; a new retryable path needs replay/conflict coverage.

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
| Trust threshold crossing | `EVALUATION_PROMOTION` escrow debit and spendable credit | Projector promotion/drain helpers; shadow compatibility projector |
| User transfer | Paired `TRANSFER` source debit/destination credit, transfer log, payer reservation | `transfer_kudos` and username/API-key wrappers |
| Monthly/recurring grant | `AWARD` user credit | `User.modify_monthly_kudos`, `User.receive_monthly_kudos` |
| Rating/aesthetic reward | `AWARD` user credit | Stable API rating/aesthetic endpoints |
| KoboldAI or other application award | `AWARD` user credit | Kobold and base API award endpoints |
| Style-owner reward | `STYLE_REWARD` user credit and user style record | `User.record_style` |
| Administrator adjustment | `ADMIN_ADJUSTMENT` signed user delta | User administration API through `User.modify_kudos` |
| Minimum-balance forgiveness | Already-applied `FLOOR_ADJUSTMENT` user credit | User projection helper in shadow mode; `_apply_user_deltas` in ledger mode |
| Reconciliation repair | Deterministic `RECONCILIATION` user spendable/escrow delta | `reconcile_balances(..., apply_repairs=True)` |

`User.modify_kudos` also emits the matching per-action user-kudos statistics event. `WorkerTemplate.modify_kudos`
emits worker display/stat events, not currency. Producer wrappers should retain that distinction.

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

Currency rows and statistic rows are each claimed in a bounded batch. A single event may cross a batch boundary;
therefore any event-wide side effect must query for remaining unapplied rows rather than assuming a batch contains the
whole event.

## Consumer and read-model inventory

Choosing the correct read is part of the mutation contract. A blanket replacement of every `.kudos` access is wrong
because account currency, queue priority, worker attribution, job price, and shared-key quota have different meaning.

### User currency consumers

| Consumer | Current read | Consistency and rule |
| --- | --- | --- |
| Upfront image/text request admission | `reserve_kudos` -> `available_kudos` | Spend-safe and conservative; includes floor, holds, and queued debits; ignores queued credits |
| Interrogation admission and interrogation-worker check | `reserve_kudos` / `available_kudos` | Spend-safe; retryable form reactivation reuses its business ID |
| User transfer | `reserve_kudos` -> `available_kudos` | Payer-serialized; recipient is not locked; optional API idempotency key protects replay |
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
maintainer must decide the intended policy: `available_kudos` is conservative spend capacity; `effective_kudos`
includes queued income; the materialized value is a cheap, lagging priority/display projection. They are not
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
| Applier heartbeat and queue/reservation ages | Metrics, `/api/v2/status/heartbeat`, operator status command, cutover gates |

All of these can lag in ledger mode. Consumers that need transaction-local proof of an accepted event should inspect
the event/result they just created, not force the projector or assume a refreshed aggregate.

### Excluded kudos-like state

The following fields use the word “kudos” but are not user currency projections and must not be routed through
`KudosLedger` without a separate design decision:

| State | Meaning and mutation rule |
| --- | --- |
| `UserSharedKey.kudos` and `utilized` | Per-key quota/budget, including `-1` for unlimited; consumed inline with the request |
| `WaitingPrompt.kudos`, `consumed_kudos`, generation/form kudos | Estimated price, accumulated job cost, or reward returned for one job |
| `WorkerTemplate.kudos` | Worker-attributed display total; projected from `KudosStatEvent` |
| `Team.kudos` | Team-attributed display total; projected from worker stat events stamped with `team_id` |
| `Style.kudos`, `Style.contributions`, `Style.fulfilments` | Style-level popularity/contribution counters; still maintained by the style subsystem |
| `KudosTransferLog` | Auxiliary transfer history and policy/audit record; does not replace the paired currency postings |
| Test-user bootstrap balance | Absolute fixture/local-login opening state; direct assignment avoids inventing an accounting movement |

## Reservation lifecycle

| Operation | Stable business ID | Created/reactivated | Consumed/released |
| --- | --- | --- | --- |
| Image/text upfront admission | `upfront:<waiting-prompt-id>` | Before activation is accepted | Debit projection consumes it; prompt deletion/cancel paths and the expired-prompt cleanup release unused remainder |
| Interrogation upfront admission | `interrogation:<form-id>` | When a worker claims a waiting form | Debit projection consumes it; cancellation/failure paths and the expired-interrogation cleanup release it; a retry reactivates the row |
| Transfer | `transfer:<source-user-id>:<event-id>` | Before paired postings commit | Released only after no unapplied posting remains for the event |

`business_id` uniqueness supplies retry stability. Reactivation may not change payer. Reservation mutation uses row
locks only after payer-scoped admission serialization; release functions are idempotent and return zero when no active
hold remains. `available_kudos` reads the balance, active holds, and queued debits in one statement so all terms come
from a single database snapshot; a fold or release committing mid-read cannot hide a debit from every term.

## Concurrency and lock reference

| Primitive | Scope | Acquired by | Purpose |
| --- | --- | --- | --- |
| Applier advisory transaction lock | Installation | Projector and mode transition | At most one database projector; serialize final drain with projection |
| Payer advisory transaction lock | User ID | Reservations/transfers/admission | Prevent concurrent promises from overspending one payer without locking recipients |
| Reconciliation advisory transaction lock | Installation | Repair mode | Prevent concurrent compensation emitters |
| Control-row key-share lock | Mutation transaction | `get_kudos_ledger_mode` | Pin mode until the writer commits |
| Control-row exclusive lock | Installation | `set_kudos_ledger_mode` | Wait for every old-mode writer before ownership changes |
| Event-row `FOR UPDATE SKIP LOCKED` | Bounded batch | Projector | Claim exact unapplied work without a watermark |
| Reservation-row `FOR UPDATE` | Business ID | Consume/release | Serialize hold depletion/release |
| Repeatable-read transaction | Snapshot/reconciliation command | Reconciliation helpers | Observe balances and applied totals from one consistent database snapshot |

Required accounting lock order is applier lock before exclusive control-row lock. Projection targets are visited in
stable ID order. Code that needs another accounting lock must document where it fits before it is merged. The legacy
`USE_SQLITE` runtime mode short-circuits PostgreSQL advisory locks and therefore cannot validate these concurrency
properties.

## Failure and recovery invariants

- A projector cycle commits both target updates and exact applied flags, or neither.
- A row is selected by `applied = false`, never by `id > watermark`.
- A stopped projector is a lag incident, not lost work; restore it and drain in ledger mode.
- A transfer hold remains active until both sides of the event are materialized, even when a batch splits the event.
- A trusted user's positive escrow with no in-flight promotion pair is self-healed by a later projector cycle.
- Minimum-balance flooring creates an explicit `FLOOR_ADJUSTMENT` so snapshot replay equals projection.
- A snapshot records both opening columns and the applied totals visible at that instant.
- Reconciliation excludes prior `RECONCILIATION` entries from its movement baseline and emits one deterministic repair
  event per snapshot/user.
- Ledger-to-shadow transition owns the applier lock, waits for ledger-mode writers, drains the final tail, and changes
  mode in one transaction.
- Applied currency and statistic history is permanent. `prune_applied_kudos_ledger` is intentionally a no-op.

Never recover by deleting ledger/stat rows, editing old amounts or targets, changing `applied`, or directly assigning a
production balance. Preserve evidence and emit compensation. See the [operations guide](../how-to/kudos_ledger_operations.md)
for the executable procedure.

## Observability contract

`kudos_applier_health` reports:

| Field | Meaning |
| --- | --- |
| `pending_rows` | Combined unapplied currency and statistic event count |
| `oldest_pending_seconds` | Age of the oldest unapplied event across both queues |
| `heartbeat_seconds` | Time since the projector last completed a cycle; may grow even when no rows are pending |
| `active_reservations` | Count of holds with positive remaining amount and no release time |
| `oldest_reservation_seconds` | Age of the oldest active hold |

The background task runs every three seconds and keeps folding within a tick while full batches drain, up to a bounded
number of catch-up cycles, so a backlog clears at many batches per tick while each fold stays a small transaction. The
heartbeat endpoint reports `DEGRADED` when the oldest pending event exceeds 30 seconds. Operators should alert on queue age and reservation age, not only row count: a steady queue can be
healthy under load, while one old row or hold can indicate a poisoned path.

## Code map

| File | Responsibility |
| --- | --- |
| `horde/classes/base/kudos.py` | Typed models, event context, mode control, currency/stat emission primitives |
| `horde/database/kudos_ledger.py` | Bounded claim/fold transaction, trust promotion, floor recording, health |
| `horde/database/kudos_reservations.py` | Available/effective balance calculations and hold lifecycle |
| `horde/database/kudos_reconciliation.py` | Repeatable-read snapshots, drift calculation, compensating repair emission |
| `horde/database/kudos_db.py` | PostgreSQL advisory-lock and isolation primitives |
| `horde/database/kudos_counters.py` | Dialect-specific typed atomic counter upsert |
| `horde/database/kudos_legacy_projection.py` | Temporary shadow-mode inline projection; the intended cutover deletion boundary |
| `horde/classes/base/user.py` | User currency, usage, contribution, uptime, style, and trust producer surface |
| `horde/classes/base/worker.py` | Worker display/stat producer surface and uptime/contribution orchestration |
| `horde/classes/base/processing_generation.py` | Generation settlement event boundary and reservation cleanup |
| `horde/classes/base/waiting_prompt.py` | Activation tax, requester usage debit, queue-priority snapshot |
| `horde/classes/stable/interrogation.py` | Interrogation reservation, settlement event, and cleanup |
| `horde/database/functions.py` | Transfer admission, paired postings, idempotency validation |
| `horde/database/threads.py` | Periodic projector invocation and health metric recording |
| `horde/enums.py` | Stable accounting discriminators and metadata keys |
| `sql_statements/5.1.0.txt` | Idempotent production schema migration and counter uniqueness preparation |
| `tools/kudos_ledger_admin.py` | Status, drain, snapshot, reconcile/repair, and mode commands |
| `docs/how-to/kudos_ledger_operations.md` | Cutover, rollback, disaster-recovery, and rehearsal how-to |

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
| Remaining activation deadlock retry/failed-session telemetry | `test_wp_activate_deadlock.py` |
| Image/text/interrogation upfront admission behavior | Integration request-activation tests and `test_kudos_interrogation.py` |
| Migration idempotency against mapped schema | `test_kudos_safety.py` |

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
- Do tests cover emission, fold, concurrency/retry behavior, and recovery—not merely the final number?
- Have this reference, the explanation, and the operations guide been updated when the contract changed?
