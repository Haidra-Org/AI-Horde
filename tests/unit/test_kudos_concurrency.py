# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Multi-threaded kudos accounting invariants exercised against real Postgres.

These tests run genuine worker threads, each with its own Flask app context (and
therefore its own scoped session and database connection), against a payer or a
control row that every thread contends on at once. Each thread arms a
conservative per-session ``lock_timeout``/``statement_timeout`` so a lock-ordering
regression surfaces as a failed statement rather than a hung suite.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from sqlalchemy import func, text

from horde.classes.base.kudos import (
    KudosLedger,
    KudosLedgerControl,
    KudosReservation,
    KudosStatEvent,
    emit_kudos_ledger_entry,
    get_kudos_ledger_mode,
    kudos_event,
    set_kudos_ledger_mode,
)
from horde.classes.base.user import User
from horde.database.kudos_ledger import apply_pending_kudos
from horde.database.kudos_reservations import consume_reservation, release_reservation, reserve_kudos
from horde.enums import KudosAuditDetail, KudosEntryType, KudosLedgerMode
from horde.flask import db

# A lock-ordering or convoy bug must fail a statement inside a few seconds rather
# than block a worker thread until the join timeout. Values are milliseconds.
_LOCK_TIMEOUT_MS = "5000"
_STATEMENT_TIMEOUT_MS = "8000"
_JOIN_TIMEOUT_SECONDS = 60.0
_DRAIN_CYCLE_CAP = 200


@pytest.fixture
def concurrent_app(_pg_dsn: str, _pg_schema: str) -> Iterator[object]:
    """A Flask app whose pool is wide enough for many worker threads at once.

    The shared unit-test app deliberately runs a five-connection pool to surface
    contention quickly. These tests instead need every contending thread to hold
    its own connection simultaneously, so they use a dedicated app and engine and
    dispose it afterwards; disposing also discards the per-session timeouts the
    threads set, so nothing leaks back to the shared engine.
    """
    import horde.flask as horde_flask
    from horde.flask import create_app

    saved_instance = horde_flask._app_instance
    app = create_app(
        config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": _pg_dsn,
            "SQLALCHEMY_ENGINE_OPTIONS": {
                "pool_size": 20,
                "max_overflow": 0,
                "connect_args": {"options": f"-c search_path={_pg_schema}"},
            },
        },
    )
    with app.app_context():
        db.create_all()
        db.session.add(KudosLedgerControl(id=1, mode=str(KudosLedgerMode.LEDGER)))
        db.session.commit()
        db.session.remove()
    try:
        yield app
    finally:
        with app.app_context():
            db.session.rollback()
            for table in reversed(db.metadata.sorted_tables):
                db.session.execute(table.delete())
            db.session.commit()
            db.session.remove()
            db.engine.dispose()
        horde_flask._app_instance = saved_instance


def _new_user(balance: Decimal) -> User:
    suffix = uuid.uuid4().hex[:12]
    user = User(
        username=f"conc_{suffix}",
        oauth_id=f"oauth_{suffix}",
        api_key=f"key_{suffix}",
        kudos=float(balance),
    )
    db.session.add(user)
    db.session.flush()
    return user


def _run_in_app_context(app: object, body: Callable[[], None], errors: list[BaseException]) -> None:
    """Run ``body`` in its own app context with the deadlock tripwire armed.

    Committing the SET statements pins the timeouts at session scope so every
    subsequent transaction on this thread's connection inherits them. Any
    exception (including a tripped timeout) is captured for the coordinator to
    assert on rather than being lost on the worker thread.
    """
    with app.app_context():  # type: ignore[attr-defined]
        try:
            db.session.execute(text(f"SET SESSION lock_timeout = '{_LOCK_TIMEOUT_MS}'"))
            db.session.execute(text(f"SET SESSION statement_timeout = '{_STATEMENT_TIMEOUT_MS}'"))
            db.session.commit()
            body()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the coordinating thread
            errors.append(exc)
            db.session.rollback()
        finally:
            db.session.remove()


def _drain_to_quiescence() -> None:
    for _ in range(_DRAIN_CYCLE_CAP):
        if apply_pending_kudos() == 0:
            return
    raise AssertionError("Kudos applier did not reach quiescence within the cycle cap")


def test_hot_payer_reservations_never_double_spend_and_fold_exactly_once(concurrent_app: object) -> None:
    """Concurrent reservations against one payer never double-spend and every posting folds exactly once.

    Eight worker threads race a bounded reserve-then-settle mix against a single
    funded payer while a ninth thread runs the projector continuously. After the
    workers finish and the ledger is drained to quiescence, the payer's balance
    must equal the arithmetic of exactly the debits the workers recorded, every
    ledger posting must be applied exactly once (no unapplied rows, one distinct
    event id per row, no compensating floor posting), and no hold may remain
    active.
    """
    app = concurrent_app
    worker_count = 8
    ops_per_worker = 25
    amounts = [Decimal("10"), Decimal("15"), Decimal("20"), Decimal("25")]
    initial_balance = Decimal("3000")

    with app.app_context():  # type: ignore[attr-defined]
        payer_id = _new_user(initial_balance).id
        db.session.commit()
        db.session.remove()

    errors: list[BaseException] = []
    results: dict[int, tuple[Decimal, Decimal, int]] = {}
    ready = threading.Barrier(worker_count)
    stop_applier = threading.Event()

    def applier_body() -> None:
        cycles = 0
        while not stop_applier.is_set() and cycles < 1_000_000:
            apply_pending_kudos()
            cycles += 1

    def make_worker(index: int) -> Callable[[], None]:
        def run() -> None:
            payer = db.session.get(User, payer_id)
            consumed = Decimal("0")
            released = Decimal("0")
            rejected = 0
            ready.wait(timeout=_JOIN_TIMEOUT_SECONDS)
            for op in range(ops_per_worker):
                amount = amounts[(index + op) % len(amounts)]
                business_id = f"hot:{index}:{op}"
                reservation = reserve_kudos(payer, amount, business_id=business_id)
                if reservation is None:
                    db.session.rollback()
                    rejected += 1
                    continue
                db.session.commit()
                if (index + op) % 2 == 0:
                    with kudos_event():
                        emit_kudos_ledger_entry(
                            KudosEntryType.GENERATION,
                            -amount,
                            user_id=payer_id,
                            detail={KudosAuditDetail.RESERVATION_ID: business_id},
                        )
                        consume_reservation(business_id, amount)
                    db.session.commit()
                    consumed += amount
                else:
                    release_reservation(business_id)
                    db.session.commit()
                    released += amount
            results[index] = (consumed, released, rejected)

        return run

    applier_thread = threading.Thread(target=_run_in_app_context, args=(app, applier_body, errors))
    worker_threads = [threading.Thread(target=_run_in_app_context, args=(app, make_worker(index), errors)) for index in range(worker_count)]
    applier_thread.start()
    for thread in worker_threads:
        thread.start()
    for thread in worker_threads:
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    stop_applier.set()
    applier_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not any(thread.is_alive() for thread in worker_threads)
    assert not applier_thread.is_alive()
    assert errors == []
    assert len(results) == worker_count

    with app.app_context():  # type: ignore[attr-defined]
        _drain_to_quiescence()

        unapplied = db.session.query(KudosLedger).filter(KudosLedger.applied.is_(False)).count()
        assert unapplied == 0

        row_count = db.session.query(KudosLedger).count()
        distinct_events = db.session.query(func.count(func.distinct(KudosLedger.event_id))).scalar()
        assert row_count == distinct_events

        floor_rows = db.session.query(KudosLedger).filter(KudosLedger.entry_type == KudosEntryType.FLOOR_ADJUSTMENT).count()
        assert floor_rows == 0

        active_holds = (
            db.session.query(KudosReservation).filter(KudosReservation.released_at.is_(None), KudosReservation.remaining_amount > 0).count()
        )
        assert active_holds == 0

        consumed_total = sum((consumed for consumed, _released, _rejected in results.values()), Decimal("0"))
        assert consumed_total > 0

        payer = db.session.get(User, payer_id)
        assert Decimal(str(payer.kudos)) == initial_balance - consumed_total
        db.session.remove()


# A healthy pin handoff is a couple of database round trips; this timeout only
# unwedges a writer whose peer is queued behind an already-admitted transition.
_HANDOFF_TIMEOUT_SECONDS = 2.0


def test_mode_transition_is_not_starved_by_gapless_writer_pins(concurrent_app: object) -> None:
    """A mode transition acquires its exclusive gate despite mode pins that never leave a gap.

    Two writer threads hand the transaction-scoped mode pin to each other in
    strict alternation: a writer commits only on its turn, and it passes the
    turn only after re-acquiring a fresh pin, so at every instant at least one
    pin is provably held. A gate with fair queueing admits a waiting transition
    as soon as the pins held when it queued drain, because later pin requests
    queue behind the exclusive waiter. A gate that lets new shared holders
    bypass a queued exclusive waiter starves the transition indefinitely, which
    the coordinator's statement timeout converts into a deterministic failure
    rather than a hung suite.
    """
    app = concurrent_app
    errors: list[BaseException] = []
    stop_writers = threading.Event()
    ready = threading.Semaphore(0)
    turns = [threading.Semaphore(0), threading.Semaphore(0)]

    def make_writer(index: int) -> Callable[[], None]:
        def run() -> None:
            get_kudos_ledger_mode()
            ready.release()
            while not stop_writers.is_set():
                turns[index].acquire(timeout=_HANDOFF_TIMEOUT_SECONDS)
                if stop_writers.is_set():
                    break
                db.session.commit()
                get_kudos_ledger_mode()
                turns[1 - index].release()
            db.session.commit()

        return run

    writer_threads = [threading.Thread(target=_run_in_app_context, args=(app, make_writer(index), errors)) for index in range(2)]
    for thread in writer_threads:
        thread.start()

    try:
        with app.app_context():  # type: ignore[attr-defined]
            assert ready.acquire(timeout=_JOIN_TIMEOUT_SECONDS)
            assert ready.acquire(timeout=_JOIN_TIMEOUT_SECONDS)
            turns[0].release()
            db.session.execute(text(f"SET SESSION lock_timeout = '{_LOCK_TIMEOUT_MS}'"))
            db.session.execute(text(f"SET SESSION statement_timeout = '{_STATEMENT_TIMEOUT_MS}'"))
            db.session.commit()
            set_kudos_ledger_mode(KudosLedgerMode.SHADOW)
            assert get_kudos_ledger_mode() == KudosLedgerMode.SHADOW
            db.session.commit()
            db.session.remove()
    finally:
        stop_writers.set()
        for turn in turns:
            turn.release()
        for thread in writer_threads:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not any(thread.is_alive() for thread in writer_threads)
    assert errors == []


def test_ledger_mode_transitions_preserve_every_concurrent_credit(concurrent_app: object) -> None:
    """Mode transitions wait out in-flight writers so every credit lands once and no writer fails.

    Six writer threads continuously credit their own distinct users through the
    real balance-mutation entry point while the coordinator flips ownership
    ledger -> shadow -> ledger -> shadow with the writers still running. The
    exclusive mode gate a transition takes must manifest to a concurrent
    writer as waiting, never as an error or a lost/duplicated credit. After the
    writers stop and the final mode is drained, each user's balance must reflect
    exactly the amount its writer recorded, no ledger or stat event may remain
    unapplied, and the control row must hold the last mode requested.
    """
    app = concurrent_app
    writer_count = 6
    initial_balance = Decimal("1000")
    credit = Decimal("1.00")
    write_cap = 100_000
    writes_between_transitions = 100

    with app.app_context():  # type: ignore[attr-defined]
        user_ids = [_new_user(initial_balance).id for _ in range(writer_count)]
        db.session.commit()
        db.session.remove()

    errors: list[BaseException] = []
    emitted: dict[int, Decimal] = {}
    progress = threading.Semaphore(0)
    stop_writers = threading.Event()

    def make_writer(user_id: int) -> Callable[[], None]:
        def run() -> None:
            user = db.session.get(User, user_id)
            total = Decimal("0")
            for _ in range(write_cap):
                if stop_writers.is_set():
                    break
                # expire_on_commit is off for this app, so force a fresh balance
                # read before an inline (shadow) mutation computes from it;
                # otherwise a transition-time fold committed since the last write
                # would be overwritten from a stale in-memory balance.
                db.session.expire(user)
                user.modify_kudos(credit, "accumulated", entry_type=KudosEntryType.AWARD)
                total += credit
                progress.release()
            emitted[user_id] = total

        return run

    writer_threads = [threading.Thread(target=_run_in_app_context, args=(app, make_writer(user_id), errors)) for user_id in user_ids]
    for thread in writer_threads:
        thread.start()

    transitions = [KudosLedgerMode.SHADOW, KudosLedgerMode.LEDGER, KudosLedgerMode.SHADOW]
    # Writers must be stopped and joined even when a transition fails, or they
    # keep writing through fixture teardown and poison the rest of the suite.
    try:
        with app.app_context():  # type: ignore[attr-defined]
            db.session.execute(text(f"SET SESSION lock_timeout = '{_LOCK_TIMEOUT_MS}'"))
            db.session.execute(text(f"SET SESSION statement_timeout = '{_STATEMENT_TIMEOUT_MS}'"))
            db.session.commit()
            for mode in transitions:
                for _ in range(writes_between_transitions):
                    assert progress.acquire(timeout=_JOIN_TIMEOUT_SECONDS)
                set_kudos_ledger_mode(mode)
            for _ in range(writes_between_transitions):
                assert progress.acquire(timeout=_JOIN_TIMEOUT_SECONDS)
            db.session.remove()
    finally:
        stop_writers.set()
        for thread in writer_threads:
            thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not any(thread.is_alive() for thread in writer_threads)
    assert errors == []
    assert len(emitted) == writer_count

    with app.app_context():  # type: ignore[attr-defined]
        _drain_to_quiescence()

        assert get_kudos_ledger_mode() == KudosLedgerMode.SHADOW
        db.session.commit()

        assert db.session.query(KudosLedger).filter(KudosLedger.applied.is_(False)).count() == 0
        assert db.session.query(KudosStatEvent).filter(KudosStatEvent.applied.is_(False)).count() == 0

        for user_id in user_ids:
            user = db.session.get(User, user_id)
            assert Decimal(str(user.kudos)) == initial_balance + emitted[user_id]
        db.session.remove()
