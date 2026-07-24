# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Concurrency, reservation, cutover, and recovery guards for kudos."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from horde.classes.base.kudos import (
    KudosBalanceSnapshot,
    KudosLedger,
    KudosReservation,
    KudosStatEvent,
    emit_kudos_ledger_entry,
    get_kudos_ledger_mode,
    set_kudos_ledger_mode,
)
from horde.classes.base.user import KudosTransferLog
from horde.classes.base.worker import WorkerTemplate
from horde.database.functions import transfer_kudos
from horde.database.kudos_ledger import apply_pending_kudos
from horde.database.kudos_reconciliation import create_balance_snapshot, reconcile_balances
from horde.database.kudos_reservations import available_kudos, effective_kudos, release_reservation, reserve_kudos
from horde.enums import KudosAuditDetail, KudosEntryType, KudosLedgerMode, UserRoleTypes
from horde.flask import db


def test_accounting_schema_separates_currency_from_stats_and_declares_ownership() -> None:
    """Pin target/unit separation and intentional foreign-key semantics."""
    assert set(KudosLedger.__table__.columns.keys()) == {
        "id",
        "created",
        "event_id",
        "entry_type",
        "user_id",
        "escrow",
        "amount",
        "applied",
        "job_id",
        "wp_type",
        "detail",
    }
    ledger_foreign_keys = {key.target_fullname: key.ondelete for key in KudosLedger.__table__.foreign_keys}
    assert ledger_foreign_keys == {"users.id": "RESTRICT"}
    assert {key.target_fullname: key.ondelete for key in KudosReservation.__table__.foreign_keys} == {
        "users.id": "CASCADE",
    }
    assert {key.target_fullname: key.ondelete for key in KudosBalanceSnapshot.__table__.foreign_keys} == {
        "users.id": "CASCADE",
    }
    assert not KudosStatEvent.__table__.foreign_keys


def test_cutover_branching_stays_out_of_business_models() -> None:
    """Keep the future legacy-removal diff mechanical and reviewable."""
    repository_root = Path(__file__).parents[2]
    business_modules = (
        "horde/classes/base/user.py",
        "horde/classes/base/worker.py",
        "horde/classes/base/team.py",
        "horde/classes/base/waiting_prompt.py",
        "horde/classes/base/processing_generation.py",
        "horde/classes/stable/interrogation.py",
        "horde/classes/stable/interrogation_worker.py",
    )
    for relative_path in business_modules:
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "kudos_projection_is_async" not in source, relative_path


def test_kudos_schema_migration_is_idempotent_on_the_mapped_schema(db_session) -> None:
    """Exercise the deploy SQL against the exact PostgreSQL schema under test."""
    repository_root = Path(__file__).parents[2]
    migration = (repository_root / "sql_statements/5.1.0.txt").read_text(encoding="utf-8")
    db_session.execute(text(migration))
    db_session.commit()


def _settle_all() -> int:
    total = 0
    for _ in range(10):
        folded = apply_pending_kudos()
        total += folded
        if folded == 0:
            break
    return total


def test_database_lock_rejects_a_second_concurrent_applier(app, db_session, make_user, monkeypatch):
    import horde.database.kudos_ledger as ledger_module

    user = make_user(kudos=100)
    user.modify_kudos(10, "awarded", entry_type=KudosEntryType.AWARD)
    entered = threading.Event()
    release = threading.Event()
    original = ledger_module._apply_user_deltas

    def hold_first(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger_module, "_apply_user_deltas", hold_first)

    def apply_in_thread() -> int:
        with app.app_context():
            try:
                return apply_pending_kudos()
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(apply_in_thread)
        assert entered.wait(timeout=5)
        second = executor.submit(apply_in_thread)
        assert second.result(timeout=5) == 0
        release.set()
        # One currency posting and its independent action-stat event are folded.
        assert first.result(timeout=5) == 2

    db_session.refresh(user)
    assert user.kudos == 110


def test_two_transfers_cannot_spend_the_same_unprojected_balance(db_session, make_user, fake_redis):
    source = make_user(kudos=100)
    first_dest = make_user(kudos=25)
    second_dest = make_user(kudos=25)
    db_session.commit()

    assert transfer_kudos(source, first_dest, 60)[0] == 60
    rejected = transfer_kudos(source, second_dest, 60)

    assert rejected[0] == 0
    assert rejected[2] == "KudosTransferNotEnough"
    assert db_session.query(KudosReservation).filter(KudosReservation.released_at.is_(None)).count() == 1
    _settle_all()
    db_session.refresh(source)
    db_session.refresh(first_dest)
    assert source.kudos == 40
    assert first_dest.kudos == 85
    assert db_session.query(KudosReservation).filter(KudosReservation.released_at.is_(None)).count() == 0


def test_transfer_cannot_ignore_an_ordinary_queued_debit(db_session, make_user, fake_redis):
    source = make_user(kudos=100)
    destination = make_user(kudos=25)
    source.modify_kudos(-60, "accumulated", entry_type=KudosEntryType.GENERATION)

    rejected = transfer_kudos(source, destination, 60)

    assert rejected[0] == 0
    assert rejected[2] == "KudosTransferNotEnough"


def test_effective_balance_reports_committed_unprojected_movements(db_session, make_user):
    user = make_user(kudos=100)
    user.modify_kudos(25, "awarded", entry_type=KudosEntryType.AWARD)

    assert effective_kudos(user) == 125


def test_settlement_activity_does_not_dirty_user_rows_before_projection(db_session, make_user):
    user = make_user(kudos=100)
    db_session.commit()
    original_last_active = user.last_active

    user.record_usage(raw_things=1_000_000, kudos=1, usage_type="image")

    assert user.last_active == original_last_active
    _settle_all()
    assert user.last_active > original_last_active


def test_transfer_idempotency_key_replays_the_result_not_the_money(db_session, make_user, fake_redis):
    source = make_user(kudos=100)
    destination = make_user(kudos=25)
    db_session.commit()

    assert transfer_kudos(source, destination, 20, idempotency_key="client-request-1")[0] == 20
    assert transfer_kudos(source, destination, 20, idempotency_key="client-request-1")[0] == 20

    assert db_session.query(KudosTransferLog).count() == 1
    assert db_session.query(KudosLedger).count() == 2
    _settle_all()
    db_session.refresh(source)
    db_session.refresh(destination)
    assert source.kudos == 80
    assert destination.kudos == 45


def test_transfer_idempotency_key_rejects_changed_parameters(db_session, make_user, fake_redis):
    source = make_user(kudos=100)
    first_destination = make_user(kudos=25)
    second_destination = make_user(kudos=25)
    db_session.commit()

    assert transfer_kudos(source, first_destination, 20, idempotency_key="client-request-1")[0] == 20
    conflict = transfer_kudos(source, second_destination, 20, idempotency_key="client-request-1")

    assert conflict[0] == 0
    assert conflict[2] == "IdempotencyKeyConflict"


def test_final_escrow_crossing_promotes_without_another_request(db_session, make_user, monkeypatch):
    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
    owner = make_user(kudos=1000, evaluating_kudos=90, created=datetime.utcnow() - timedelta(days=8))
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(worker)
    db_session.commit()

    worker.record_contribution(raw_things=1000, kudos=40, things_per_sec=1)
    db_session.commit()
    _settle_all()

    db_session.refresh(owner)
    assert owner.trusted is True
    assert owner.evaluating_kudos == 0
    assert owner.kudos == 1130


def test_shadow_mode_projects_inline_and_never_replays(db_session, make_user):
    set_kudos_ledger_mode(KudosLedgerMode.SHADOW)
    user = make_user(kudos=100)
    user.modify_kudos(10, "awarded", entry_type=KudosEntryType.AWARD)

    assert user.kudos == 110


def test_shadow_transition_atomically_drains_unsettled_ledger_work(db_session, make_user):
    user = make_user(kudos=100)
    user.modify_kudos(10, "awarded", entry_type=KudosEntryType.AWARD)

    set_kudos_ledger_mode(KudosLedgerMode.SHADOW)

    assert get_kudos_ledger_mode() == KudosLedgerMode.SHADOW
    assert user.kudos == 110
    assert db_session.query(KudosLedger).filter(KudosLedger.applied.is_(False)).count() == 0
    row = db_session.query(KudosLedger).one()
    assert row.applied is True
    assert apply_pending_kudos() == 0
    assert user.kudos == 110


def test_snapshot_reconciliation_repairs_only_with_compensating_posting(db_session, make_user):
    user = make_user(kudos=100)
    db_session.commit()
    snapshot_id = create_balance_snapshot()
    user.modify_kudos(10, "awarded", entry_type=KudosEntryType.AWARD)
    _settle_all()
    user.kudos = 999
    db_session.commit()

    drifts = reconcile_balances(snapshot_id)
    assert len(drifts) == 1
    assert drifts[0].expected_balance == 110
    assert user.kudos == 999

    reconcile_balances(snapshot_id, apply_repairs=True)
    reconcile_balances(snapshot_id, apply_repairs=True)
    assert db_session.query(KudosLedger).filter(KudosLedger.entry_type == KudosEntryType.RECONCILIATION).count() == 1
    _settle_all()
    db_session.refresh(user)
    assert user.kudos == 110
    assert reconcile_balances(snapshot_id) == []


def test_snapshot_replay_accounts_for_flooring_across_separate_batches(db_session, make_user):
    user = make_user(kudos=30)
    db_session.commit()
    snapshot_id = create_balance_snapshot()
    user.modify_kudos(-100, "accumulated", entry_type=KudosEntryType.GENERATION)
    _settle_all()
    user.modify_kudos(20, "awarded", entry_type=KudosEntryType.AWARD)
    _settle_all()

    assert user.kudos == 45
    assert reconcile_balances(snapshot_id) == []


def test_released_hold_exposes_its_unapplied_debit_to_admission(db_session, make_user, fake_redis):
    """A tagged debit counts against admission once its hold has been released.

    An upfront hold is released inline at prompt completion while the debit it
    guarded is still unprojected. During that window the debit must be counted
    against the spendable balance; otherwise admission re-spends money that has
    already left the balance in all but the applier's own bookkeeping.
    """
    user = make_user(kudos=100)  # Named floor 25 leaves 75 nominally spendable.
    hold = reserve_kudos(user, 60, business_id="upfront:job-1")
    assert hold is not None
    emit_kudos_ledger_entry(
        KudosEntryType.GENERATION,
        -60,
        user_id=user.id,
        detail={KudosAuditDetail.RESERVATION_ID: "upfront:job-1"},
    )
    release_reservation("upfront:job-1")
    db_session.commit()

    # 100 balance - 25 floor - 60 owed by the still-unapplied debit.
    assert available_kudos(user) == 15
    # A fresh spend that would only fit under the pre-debit balance is rejected.
    assert reserve_kudos(user, 60, business_id="upfront:job-2") is None


def test_ledger_mode_read_is_memoized_within_a_transaction(db_session, assert_query_count):
    """Repeated mode reads in one transaction query the control row once.

    The first read pins the mode under a key-share lock held until commit, so
    later reads in the same transaction return the pinned value without re-querying
    the single global control row a settlement reads many times.
    """
    with assert_query_count() as queries:
        first = get_kudos_ledger_mode()
        second = get_kudos_ledger_mode()

    control_reads = [statement for statement in queries.statements if "kudos_ledger_control" in statement.lower()]
    assert first == second
    assert len(control_reads) == 1


def test_ledger_mode_is_reread_in_a_new_transaction(db_session, assert_query_count):
    """A commit ends the pin, so the next transaction re-reads the control row."""
    get_kudos_ledger_mode()
    db_session.commit()

    with assert_query_count() as queries:
        get_kudos_ledger_mode()

    control_reads = [statement for statement in queries.statements if "kudos_ledger_control" in statement.lower()]
    assert len(control_reads) == 1


def test_shadow_mode_applier_does_not_drain_trusted_escrow(db_session, make_user, make_user_role):
    """In shadow mode the applier leaves a trusted user's escrow to the inline path.

    Shadow mode's inline legacy projection owns promotion and the escrow drain, so
    the applier must not move a trusted user's evaluation escrow while shadowing.
    """
    set_kudos_ledger_mode(KudosLedgerMode.SHADOW)
    owner = make_user(kudos=1000)
    make_user_role(owner, UserRoleTypes.TRUSTED)
    owner.evaluating_kudos = 250
    db_session.flush()
    db_session.commit()

    apply_pending_kudos()

    db_session.refresh(owner)
    assert owner.evaluating_kudos == 250
    promotion_rows = db_session.query(KudosLedger).filter(KudosLedger.entry_type == KudosEntryType.EVALUATION_PROMOTION)
    assert promotion_rows.count() == 0


def test_expired_prompt_cleanup_releases_its_upfront_hold(db_session, make_user):
    """Pruning an expired prompt releases its upfront admission hold.

    The expiry sweep prunes prompts with a bulk delete that never runs
    ``WaitingPrompt.delete()``, so it must release the ``upfront:<wp-id>`` hold
    itself; otherwise a request that expires unserved permanently reduces the
    payer's available kudos.
    """
    from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
    from horde.database.threads import check_waiting_prompts

    requester = make_user(kudos=1000)
    waiting_prompt = ImageWaitingPrompt(
        worker_ids=[],
        models=["stable_diffusion"],
        prompt="a test robot",
        user_id=requester.id,
        params={"width": 512, "height": 512, "steps": 8, "sampler_name": "k_euler_a"},
    )
    waiting_prompt.expiry = datetime.utcnow() - timedelta(hours=1)
    hold = reserve_kudos(requester, 30, business_id=f"upfront:{waiting_prompt.id}")
    assert hold is not None
    db_session.commit()

    check_waiting_prompts()

    active_holds = (
        db_session.query(KudosReservation).filter(KudosReservation.released_at.is_(None), KudosReservation.remaining_amount > 0).count()
    )
    assert active_holds == 0


def test_expired_interrogation_cleanup_releases_form_holds(db_session, make_user):
    """Pruning an expired interrogation releases its forms' admission holds.

    Deleting the interrogation cascades to its forms, so the sweep must release the
    ``interrogation:<form-id>`` holds before the cascade removes the rows that name
    them; otherwise a form still processing at expiry leaks its hold.
    """
    from horde.classes.stable.interrogation import Interrogation, InterrogationForms
    from horde.classes.stable.interrogation_worker import InterrogationWorker
    from horde.database.threads import check_interrogations
    from horde.enums import State

    requester = make_user(kudos=1000)
    owner = make_user(kudos=1000)
    interrogation = Interrogation(user_id=requester.id)
    interrogation.expiry = datetime.utcnow() - timedelta(hours=1)
    worker = InterrogationWorker(name=f"iw_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    form = InterrogationForms(
        i_id=interrogation.id,
        name="caption",
        kudos=3,
        state=State.PROCESSING,
        worker_id=worker.id,
        initiated=datetime.utcnow(),
    )
    db_session.add(form)
    db_session.flush()
    hold = reserve_kudos(requester, 4, business_id=f"interrogation:{form.id}")
    assert hold is not None
    db_session.commit()

    check_interrogations()

    active_holds = (
        db_session.query(KudosReservation).filter(KudosReservation.released_at.is_(None), KudosReservation.remaining_amount > 0).count()
    )
    assert active_holds == 0
