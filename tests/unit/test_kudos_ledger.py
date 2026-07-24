# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ledger emission, folding, permanent retention, and applier-lag behaviour.

The kudos ledger records every balance movement as an append-only row and a
single asynchronous applier folds those rows into the materialized balance
  columns (``users.kudos`` and ``users.evaluating_kudos``). A separate stat-event
  archive projects worker display kudos and non-currency counters. These
tests pin the durable contract of that machinery:

- each mutation primitive and inventoried flow emits the expected signed rows,
  grouped under one ``event_id`` per business event;
- the applier claims unapplied rows per account, reproduces the historical
  balance floor on the spendable balance, marks the folded rows applied
  atomically, applies each row exactly once across catch-up re-runs, folds a row
  late (never losing it) when its transaction commits after higher ids, and folds
  escrow and evaluation-promotion delta pairs correctly;
- applied and unapplied rows remain in the permanent recovery archive;
- the applier surfaces its lag as an observable metric.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from horde.classes.base.kudos import (
    KudosLedger,
    KudosStatEvent,
    emit_kudos_ledger_entry,
    emit_kudos_stat_event,
    kudos_event,
)
from horde.classes.base.user import User
from horde.classes.base.worker import WorkerTemplate
from horde.database.kudos_ledger import (
    apply_pending_kudos,
    kudos_applier_lag,
    prune_applied_kudos_ledger,
)
from horde.enums import KudosEntryType, KudosStatRecord, KudosUnit, UserRoleTypes
from horde.flask import db


def _settle(db_session: Any) -> int:
    """Fold every unapplied ledger row in one cycle."""
    return apply_pending_kudos()


def _settle_all(db_session: Any) -> int:
    """Fold to quiescence: repeat until a cycle folds and drains nothing.

    A fold can emit follow-on postings (a trusted user's escrow drains via an
    applier-emitted delta pair folded on a later cycle), so a single fold is not
    enough to observe the settled balances. Returns the total work done.
    """
    total = 0
    for _ in range(10):
        did = apply_pending_kudos()
        total += did
        if did == 0:
            break
    return total


def _applied_count(db_session: Any, *, applied: bool) -> int:
    """Count ledger rows by applied flag with a fresh query (identity-map safe)."""
    return db_session.query(KudosLedger).filter(KudosLedger.applied.is_(applied)).count()


def _make_worker(db_session: Any, owner: User) -> WorkerTemplate:
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:12]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    return worker


def _ledger_rows(db_session: Any, **filters: Any) -> list[KudosLedger]:
    query = db_session.query(KudosLedger)
    for key, value in filters.items():
        query = query.filter(getattr(KudosLedger, key) == value)
    return query.order_by(KudosLedger.id.asc()).all()


def _stat_rows(db_session: Any, **filters: Any) -> list[KudosStatEvent]:
    query = db_session.query(KudosStatEvent)
    for key, value in filters.items():
        query = query.filter(getattr(KudosStatEvent, key) == value)
    return query.order_by(KudosStatEvent.id.asc()).all()


# --------------------------------------------------------------------------- #
# Row emission per entry type                                                 #
# --------------------------------------------------------------------------- #


class TestEmissionPrimitive:
    def test_user_balance_row_is_signed_and_targets_the_user(self, db_session, make_user):
        user = make_user(kudos=1000)
        emit_kudos_ledger_entry(KudosEntryType.ADMIN_ADJUSTMENT, -25, user_id=user.id)
        db_session.commit()

        rows = _ledger_rows(db_session)
        assert len(rows) == 1
        assert rows[0].amount == -25
        assert rows[0].user_id == user.id
        assert rows[0].escrow is False
        assert rows[0].entry_type == KudosEntryType.ADMIN_ADJUSTMENT

    def test_escrow_row_carries_the_escrow_marker(self, db_session, make_user):
        user = make_user(kudos=1000)
        emit_kudos_ledger_entry(KudosEntryType.GENERATION, 50, user_id=user.id, escrow=True)
        db_session.commit()

        row = _ledger_rows(db_session)[0]
        assert row.escrow is True
        assert row.user_id == user.id

    def test_worker_stat_row_keeps_owner_for_audit(self, db_session, make_user):
        owner = make_user(kudos=1000)
        worker = _make_worker(db_session, owner)
        emit_kudos_stat_event(
            KudosEntryType.GENERATION,
            100,
            worker_id=worker.id,
            worker_user_id=owner.id,
            unit=KudosUnit.KUDOS,
            stat_action="generated",
            record=KudosStatRecord.WORKER_KUDOS,
        )
        db_session.commit()

        row = _stat_rows(db_session)[0]
        assert row.worker_id == worker.id
        assert row.worker_user_id == owner.id
        assert row.user_id is None

    def test_all_rows_in_one_event_share_the_event_id(self, db_session, make_user):
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        worker = _make_worker(db_session, owner)
        with kudos_event(job_id=None, wp_type="image"):
            emit_kudos_stat_event(
                KudosEntryType.GENERATION,
                100,
                worker_id=worker.id,
                worker_user_id=owner.id,
                unit=KudosUnit.KUDOS,
                stat_action="generated",
                record=KudosStatRecord.WORKER_KUDOS,
            )
            emit_kudos_ledger_entry(KudosEntryType.GENERATION, 100, user_id=owner.id)
            emit_kudos_ledger_entry(KudosEntryType.GENERATION, -100, user_id=requester.id)
        db_session.commit()

        rows = _ledger_rows(db_session) + _stat_rows(db_session)
        assert len({row.event_id for row in rows}) == 1
        assert all(row.wp_type == "image" for row in rows)

    def test_standalone_emissions_get_distinct_event_ids(self, db_session, make_user):
        user = make_user(kudos=1000)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()

        rows = _ledger_rows(db_session)
        assert len({row.event_id for row in rows}) == 2


class TestGenerationFlowEmission:
    def test_contribution_emits_worker_and_owner_credit_rows(self, db_session, make_user, make_user_role):
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)

        worker.record_contribution(raw_things=1000, kudos=100, things_per_sec=1)
        db_session.commit()

        # The settlement also appends counter postings (worker things/count,
        # owner records); isolate the kudos balance postings this test is about.
        worker_rows = _stat_rows(db_session, worker_id=worker.id, unit="kudos")
        owner_rows = _ledger_rows(db_session, user_id=owner.id)
        assert sum(r.amount for r in worker_rows) == 100
        assert sum(r.amount for r in owner_rows) == 100
        assert all(r.entry_type == KudosEntryType.GENERATION for r in worker_rows + owner_rows)

    def test_untrusted_contribution_splits_owner_credit_into_escrow(self, db_session, make_user, monkeypatch):
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")
        owner = make_user(kudos=1000)
        worker = _make_worker(db_session, owner)

        worker.record_contribution(raw_things=1000, kudos=100, things_per_sec=1)
        db_session.commit()

        owner_balance_rows = [r for r in _ledger_rows(db_session, user_id=owner.id) if not r.escrow]
        owner_escrow_rows = [r for r in _ledger_rows(db_session, user_id=owner.id) if r.escrow]
        assert sum(r.amount for r in owner_balance_rows) == 50
        assert sum(r.amount for r in owner_escrow_rows) == 50


class TestUptimeRewardEmission:
    def test_uptime_crossing_emits_worker_and_owner_rows(self, db_session, make_user, make_user_role):
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.last_check_in = datetime.utcnow() - timedelta(seconds=40)
        worker.uptime = worker.uptime_reward_threshold - 5
        worker.last_reward_uptime = 0
        db_session.flush()

        worker.check_in(ipaddr="10.0.0.2")

        worker_rows = _stat_rows(db_session, worker_id=worker.id)
        owner_rows = _ledger_rows(db_session, user_id=owner.id)
        assert len(worker_rows) >= 1
        assert len(owner_rows) >= 1
        assert all(r.entry_type == KudosEntryType.UPTIME_REWARD for r in worker_rows + owner_rows)


class TestStyleRewardEmission:
    def test_style_credit_emits_a_style_reward_row(self, db_session, make_user):
        owner = make_user(kudos=1000)
        owner.record_style(2, "image")
        db_session.commit()

        # record_style also appends a STAT_RECORD count posting; isolate the kudos
        # style-reward posting this test is about.
        rows = _ledger_rows(db_session, user_id=owner.id)
        assert len(rows) == 1
        assert rows[0].amount == 2
        assert rows[0].entry_type == KudosEntryType.STYLE_REWARD


class TestAwardEmission:
    def test_monthly_kudos_emits_a_recurring_award_row(self, db_session, make_user):
        user = make_user(kudos=1000, monthly_kudos=500)
        user.receive_monthly_kudos(force=True)
        db_session.commit()

        rows = _ledger_rows(db_session, user_id=user.id)
        assert sum(r.amount for r in rows) == 500
        assert all(r.entry_type == KudosEntryType.AWARD for r in rows)


# --------------------------------------------------------------------------- #
# Applier folding                                                             #
# --------------------------------------------------------------------------- #


class TestApplierFolding:
    def test_fold_credits_and_debits_across_accounts(self, db_session, make_user):
        alice = make_user(kudos=100)
        bob = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.TRANSFER, -30, user_id=alice.id)
        emit_kudos_ledger_entry(KudosEntryType.TRANSFER, 30, user_id=bob.id)
        db_session.commit()

        applied = _settle(db_session)

        assert applied == 2
        assert alice.kudos == 70
        assert bob.kudos == 130

    def test_multiple_entries_for_one_account_fold_into_one_update(self, db_session, make_user):
        user = make_user(kudos=100)
        for delta in (10, 20, -5):
            emit_kudos_ledger_entry(KudosEntryType.AWARD, delta, user_id=user.id)
        db_session.commit()

        _settle(db_session)

        assert user.kudos == 125

    def test_worker_balance_is_folded_by_worker(self, db_session, make_user):
        owner = make_user(kudos=1000)
        worker = _make_worker(db_session, owner)
        emit_kudos_stat_event(
            KudosEntryType.GENERATION,
            40,
            worker_id=worker.id,
            worker_user_id=owner.id,
            unit=KudosUnit.KUDOS,
            stat_action="generated",
            record=KudosStatRecord.WORKER_KUDOS,
        )
        db_session.commit()

        _settle(db_session)

        assert worker.kudos == 40


class TestApplierBatchBound:
    def test_fold_stops_at_the_batch_bound_and_resumes_next_cycle(self, db_session, make_user):
        user = make_user(kudos=100)
        for _ in range(5):
            emit_kudos_ledger_entry(KudosEntryType.AWARD, 1, user_id=user.id)
        db_session.commit()

        first = apply_pending_kudos(batch_size=2)
        assert first == 2
        assert user.kudos == 102

        # The folded rows are marked applied, so the next cycle claims the next
        # two unapplied rows.
        second = apply_pending_kudos(batch_size=2)
        assert second == 2
        assert user.kudos == 104

        third = apply_pending_kudos(batch_size=2)
        assert third == 1
        assert user.kudos == 105


class TestApplierCatchUp:
    """One scheduler tick drains a multi-batch backlog via a bounded catch-up loop.

    ``apply_kudos_ledger`` keeps folding while a cycle drains a full batch, up to a
    per-tick cycle bound, so a backlog clears at many batches per tick instead of
    one. Each fold stays its own bounded transaction.
    """

    def test_one_tick_drains_multiple_batches(self, db_session, make_user, monkeypatch):
        import horde.database.kudos_ledger as kudos_ledger_module
        from horde.database.threads import apply_kudos_ledger

        monkeypatch.setattr(kudos_ledger_module, "KUDOS_APPLIER_BATCH_SIZE", 2)
        monkeypatch.setattr(kudos_ledger_module, "KUDOS_APPLIER_MAX_CATCHUP_CYCLES", 10)
        user = make_user(kudos=100)
        for _ in range(5):
            emit_kudos_ledger_entry(KudosEntryType.AWARD, 1, user_id=user.id)
        db_session.commit()

        apply_kudos_ledger()

        db_session.refresh(user)
        assert user.kudos == 105
        assert _applied_count(db_session, applied=False) == 0

    def test_catch_up_stops_at_the_cycle_bound(self, db_session, make_user, monkeypatch):
        import horde.database.kudos_ledger as kudos_ledger_module
        from horde.database.threads import apply_kudos_ledger

        monkeypatch.setattr(kudos_ledger_module, "KUDOS_APPLIER_BATCH_SIZE", 1)
        monkeypatch.setattr(kudos_ledger_module, "KUDOS_APPLIER_MAX_CATCHUP_CYCLES", 2)
        user = make_user(kudos=100)
        for _ in range(5):
            emit_kudos_ledger_entry(KudosEntryType.AWARD, 1, user_id=user.id)
        db_session.commit()

        apply_kudos_ledger()

        db_session.refresh(user)
        # Two single-row cycles fold two rows; the remaining three wait for the
        # next tick rather than draining unbounded within this one.
        assert user.kudos == 102
        assert _applied_count(db_session, applied=False) == 3


class TestAppliedMarking:
    def test_folded_rows_are_marked_applied(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()

        _settle(db_session)

        assert _applied_count(db_session, applied=True) == 2
        assert _applied_count(db_session, applied=False) == 0

    def test_reapply_without_new_rows_is_a_noop(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()
        _settle(db_session)

        applied = _settle(db_session)

        assert applied == 0
        assert user.kudos == 110


class TestUnappliedClaim:
    """Eligibility is per-row applied state, not id order or transaction visibility.

    A row invisible to the applier (its inserting transaction still open) is
    simply not claimed until it commits; it then folds in a later cycle. This
    holds even for an id/txid inversion (a lower id committing after higher ids
    were folded), so no row is lost. A second connection holding an open
    transaction stages the in-progress condition deterministically.
    """

    def test_row_from_an_open_transaction_folds_after_it_commits(self, db_session, make_user):
        user = make_user(kudos=100)
        db_session.commit()
        holder = db.engine.connect()
        holder_txn = holder.begin()
        try:
            # Stage an id/txid inversion: the holder inserts a lower id that stays
            # invisible while its transaction is open, then a higher id is
            # committed and folded first. The lower id must fold late, not be lost.
            holder.execute(
                text(
                    "INSERT INTO kudos_ledger "
                    "(created, event_id, entry_type, user_id, escrow, amount, applied) "
                    "VALUES (now(), gen_random_uuid(), :et, :uid, false, 5, false)",
                ),
                {"et": str(KudosEntryType.AWARD), "uid": user.id},
            )
            emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
            db_session.commit()

            # Only the visible (higher-id) row is claimed; the holder's row is not.
            assert apply_pending_kudos() == 1
            db_session.refresh(user)
            assert user.kudos == 110
        finally:
            holder_txn.commit()
            holder.close()

        # The lower id is now visible and still unapplied: it folds in a later
        # cycle rather than being skipped by the earlier fold.
        assert apply_pending_kudos() == 1
        db_session.refresh(user)
        assert user.kudos == 115


class TestFloorClampReproduction:
    def test_named_user_clamps_to_25(self, db_session, make_user):
        user = make_user(kudos=30)  # named account
        emit_kudos_ledger_entry(KudosEntryType.GENERATION, -1000, user_id=user.id)
        db_session.commit()

        _settle(db_session)

        assert user.kudos == 25

    def test_pseudonymous_user_clamps_to_14(self, db_session, make_user):
        user = make_user(kudos=20, oauth_id=str(uuid.uuid4()))
        emit_kudos_ledger_entry(KudosEntryType.GENERATION, -1000, user_id=user.id)
        db_session.commit()

        _settle(db_session)

        assert user.kudos == 14

    def test_anonymous_user_clamps_to_negative_50(self, db_session, make_user):
        anon = make_user(username="Anonymous", oauth_id="anon", kudos=-40)
        emit_kudos_ledger_entry(KudosEntryType.GENERATION, -100, user_id=anon.id)
        db_session.commit()

        _settle(db_session)

        assert anon.kudos == -50

    def test_escrow_is_not_floor_clamped(self, db_session, make_user):
        user = make_user(kudos=1000)
        user.evaluating_kudos = 0
        db_session.flush()
        emit_kudos_ledger_entry(KudosEntryType.EVALUATION_PROMOTION, -30, user_id=user.id, escrow=True)
        db_session.commit()

        _settle(db_session)

        assert user.evaluating_kudos == -30


# --------------------------------------------------------------------------- #
# Escrow folding and evaluation promotion                                     #
# --------------------------------------------------------------------------- #


class TestEscrowAndPromotion:
    """The applier owns the escrow-to-balance movement for trusted users.

    ``check_for_trust`` only flips the trust flag; the applier drains a trusted
    user's evaluation escrow to their spendable balance by emitting an
    ``EVALUATION_PROMOTION`` delta pair (escrow debit, balance credit) that a
    later cycle folds. The escrow set here directly stands in for escrow that
    earlier cycles already folded into ``evaluating_kudos``.
    """

    def test_trusted_users_residual_escrow_drains_via_an_applier_pair(self, db_session, make_user, monkeypatch):
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
        owner = make_user(kudos=1000, created=datetime.utcnow() - timedelta(days=8))
        owner.evaluating_kudos = 250
        db_session.flush()

        owner.check_for_trust()
        db_session.commit()
        db_session.refresh(owner)
        assert owner.trusted is True
        _settle_all(db_session)

        assert owner.evaluating_kudos == 0
        assert owner.kudos == 1250
        # The drain is an applier-emitted EVALUATION_PROMOTION pair: an escrow
        # debit and a balance credit for the drained amount under one event id.
        promo_rows = _ledger_rows(db_session, entry_type=KudosEntryType.EVALUATION_PROMOTION)
        assert len(promo_rows) == 2
        assert {r.amount for r in promo_rows} == {-250, 250}
        assert len({r.event_id for r in promo_rows}) == 1
        debit = next(r for r in promo_rows if r.amount == -250)
        credit = next(r for r in promo_rows if r.amount == 250)
        assert debit.escrow is True
        assert credit.escrow is False

    def test_untrusted_users_escrow_is_not_drained(self, db_session, make_user):
        owner = make_user(kudos=1000)
        owner.evaluating_kudos = 250
        db_session.flush()
        db_session.commit()

        _settle_all(db_session)

        assert owner.trusted is False
        assert owner.evaluating_kudos == 250
        assert owner.kudos == 1000
        assert _ledger_rows(db_session, entry_type=KudosEntryType.EVALUATION_PROMOTION) == []

    def test_drain_is_idempotent_across_cycles(self, db_session, make_user, monkeypatch):
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
        owner = make_user(kudos=1000, created=datetime.utcnow() - timedelta(days=8))
        owner.evaluating_kudos = 250
        db_session.flush()
        owner.check_for_trust()
        db_session.commit()

        _settle_all(db_session)
        assert owner.evaluating_kudos == 0
        assert owner.kudos == 1250

        # Once escrow reaches zero the scan finds nothing: no further fold work,
        # no further promotion pair, and the balance does not move again.
        assert _settle_all(db_session) == 0
        assert owner.evaluating_kudos == 0
        assert owner.kudos == 1250
        assert len(_ledger_rows(db_session, entry_type=KudosEntryType.EVALUATION_PROMOTION)) == 2

    def test_pending_drain_pair_is_not_re_emitted_while_unapplied(self, db_session, make_user, make_user_role):
        # Until the emitted promotion pair folds, the materialized escrow stays
        # positive, so a naive scan would emit a fresh pair every cycle and
        # over-credit the balance once the backlog folds. Fold nothing
        # (batch_size=0) so the pair stays unapplied across cycles, standing in
        # for a crash or batch bound that delays its fold, and pin that exactly
        # one pair is emitted. The escrow set directly stands in for escrow folded
        # in earlier cycles.
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        owner.evaluating_kudos = 250
        db_session.flush()
        db_session.commit()

        for _ in range(3):
            apply_pending_kudos(batch_size=0)

        # One pair only, and it has not folded, so the escrow is unchanged.
        assert len(_ledger_rows(db_session, entry_type=KudosEntryType.EVALUATION_PROMOTION)) == 2
        db_session.refresh(owner)
        assert owner.evaluating_kudos == 250
        assert owner.kudos == 1000

        # Let the pair fold: escrow drains to zero and the balance is credited
        # exactly once, not once per cycle.
        for _ in range(5):
            if apply_pending_kudos() == 0:
                break
        db_session.refresh(owner)
        assert owner.evaluating_kudos == 0
        assert owner.kudos == 1250
        assert len(_ledger_rows(db_session, entry_type=KudosEntryType.EVALUATION_PROMOTION)) == 2


# --------------------------------------------------------------------------- #
# Crash catch-up (exactly-once)                                               #
# --------------------------------------------------------------------------- #


class TestCatchUp:
    def test_rows_emitted_after_a_fold_are_applied_exactly_once(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()
        _settle(db_session)

        emit_kudos_ledger_entry(KudosEntryType.AWARD, 5, user_id=user.id)
        db_session.commit()
        applied = _settle(db_session)

        assert applied == 1
        assert user.kudos == 115

    def test_a_failed_cycle_leaves_rows_unapplied(self, db_session, make_user, monkeypatch):
        # The balance UPDATEs and the applied-flag flip commit together, so a
        # cycle that raises before committing must leave the row unapplied and the
        # balance untouched; a later clean cycle then folds it exactly once. This
        # is the exactly-once guarantee that replaces the old watermark rewind.
        import horde.database.kudos_ledger as kudos_ledger_module

        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated applier crash mid-cycle")

        monkeypatch.setattr(kudos_ledger_module, "_mark_applied", _boom)
        with pytest.raises(RuntimeError):
            apply_pending_kudos()
        db_session.rollback()

        assert _applied_count(db_session, applied=False) == 1
        db_session.refresh(user)
        assert user.kudos == 100

        monkeypatch.undo()
        assert _settle(db_session) == 1
        db_session.refresh(user)
        assert user.kudos == 110
        assert _applied_count(db_session, applied=True) == 1


# --------------------------------------------------------------------------- #
# Retention pruning                                                           #
# --------------------------------------------------------------------------- #


class TestRetention:
    def test_applied_rows_are_permanent_after_the_old_retention_window(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()
        _settle(db_session)
        row = _ledger_rows(db_session)[0]
        row.created = datetime.utcnow() - timedelta(days=40)
        db_session.commit()

        pruned = prune_applied_kudos_ledger(now=datetime.utcnow(), retention=timedelta(days=30))

        assert pruned == 0
        assert len(_ledger_rows(db_session)) == 1

    def test_unapplied_rows_are_never_pruned(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()
        row = _ledger_rows(db_session)[0]
        row.created = datetime.utcnow() - timedelta(days=40)
        db_session.commit()

        pruned = prune_applied_kudos_ledger(now=datetime.utcnow(), retention=timedelta(days=30))

        assert pruned == 0
        assert len(_ledger_rows(db_session)) == 1

    def test_compatibility_pruner_never_deletes_a_batch(self, db_session, make_user):
        user = make_user(kudos=100)
        for _ in range(5):
            emit_kudos_ledger_entry(KudosEntryType.AWARD, 1, user_id=user.id)
        db_session.commit()
        _settle(db_session)
        for row in _ledger_rows(db_session):
            row.created = datetime.utcnow() - timedelta(days=40)
        db_session.commit()

        pruned = prune_applied_kudos_ledger(now=datetime.utcnow(), retention=timedelta(days=30), batch_size=2)

        assert pruned == 0
        assert len(_ledger_rows(db_session)) == 5

    def test_old_applied_and_unapplied_rows_are_both_kept(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()
        _settle(db_session)  # applied
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)  # unapplied
        db_session.commit()
        for row in _ledger_rows(db_session):
            row.created = datetime.utcnow() - timedelta(days=40)
        db_session.commit()

        pruned = prune_applied_kudos_ledger(now=datetime.utcnow(), retention=timedelta(days=30))

        assert pruned == 0
        assert _applied_count(db_session, applied=False) == 1
        assert _applied_count(db_session, applied=True) == 1


# --------------------------------------------------------------------------- #
# Applier lag metric                                                          #
# --------------------------------------------------------------------------- #


class TestApplierLag:
    def test_lag_is_none_before_the_first_run(self, db_session):
        assert kudos_applier_lag(now=datetime.utcnow()) is None

    def test_lag_reflects_time_since_last_apply(self, db_session, make_user):
        user = make_user(kudos=100)
        emit_kudos_ledger_entry(KudosEntryType.AWARD, 10, user_id=user.id)
        db_session.commit()
        apply_pending_kudos(now=datetime(2026, 1, 1, 0, 0, 0))

        lag = kudos_applier_lag(now=datetime(2026, 1, 1, 0, 0, 30))

        assert lag == pytest.approx(30, abs=1)
