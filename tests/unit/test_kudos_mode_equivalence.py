# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Both kudos accounting modes move balances and counters identically.

The kudos subsystem can materialize a movement two ways: shadow mode mutates the
balance and counter rows inline as the business event runs, while ledger mode
records the movement as an append-only posting that the applier folds into those
same rows afterwards. Both must be observationally identical: for one fixed
workload, the per-account balance movements and every derived counter total must
come out exactly the same regardless of which mode owns the writes.

This module runs one scripted workload against two independent, freshly created
sets of participants (once per mode) and compares the resulting per-account
deltas position by position. The workload uses the same production emission entry
points both modes rely on, so each mode exercises its real inline or folded
branch rather than a synthetic write.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from horde.classes.base.kudos import KudosLedger, KudosReservation, KudosStatEvent, set_kudos_ledger_mode
from horde.classes.base.user import User, UserRecords, UserStats
from horde.classes.base.worker import WorkerStats, WorkerTemplate
from horde.database.functions import transfer_kudos
from horde.database.kudos_ledger import apply_pending_kudos
from horde.database.kudos_reservations import release_reservation, reserve_kudos
from horde.enums import KudosEntryType, KudosLedgerMode
from horde.flask import db

# Fixed, mode-independent workload magnitudes. Every amount is an integer so the
# folded and inline results land on the same integer balance/counter columns with
# no rounding ambiguity to reason about.
_SETTLEMENT_KUDOS = 40
_UPTIME_KUDOS = 10
_ADMISSION_DEBIT = 60
_FLOOR_DEBIT = 100
_TRANSFER_AMOUNT = 100
_RELEASE_HOLD = 40
# Two megapixelsteps: raw_things / thing_divisor["image"] resolves to an exact 2.
_RAW_THINGS = 2_000_000

_OWNER_START = 100
_REQUESTER_START = 1000
_GIVER_START = 500
_RECEIVER_START = 50
# 30 is below the 25 floor after the 100 debit, so the debit is partly forgiven.
_FLOOR_USER_START = 30


@dataclass(frozen=True)
class _Participants:
    """One run's accounts, labelled so two runs compare position by position."""

    owner: User
    requester: User
    giver: User
    receiver: User
    floor_user: User
    worker: WorkerTemplate

    def users_by_label(self) -> dict[str, User]:
        return {
            "owner": self.owner,
            "requester": self.requester,
            "giver": self.giver,
            "receiver": self.receiver,
            "floor_user": self.floor_user,
        }

    def workers_by_label(self) -> dict[str, WorkerTemplate]:
        return {"worker": self.worker}


def _make_participants(make_user: Callable[..., User]) -> _Participants:
    owner = make_user(kudos=_OWNER_START)
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.session.add(worker)
    db.session.flush()
    participants = _Participants(
        owner=owner,
        requester=make_user(kudos=_REQUESTER_START),
        giver=make_user(kudos=_GIVER_START),
        receiver=make_user(kudos=_RECEIVER_START),
        floor_user=make_user(kudos=_FLOOR_USER_START),
        worker=worker,
    )
    db.session.commit()
    return participants


def _run_workload(participants: _Participants, mid_settle: Callable[[], None]) -> None:
    """Drive one fixed sequence of kudos movements through production entry points.

    Covers a worker settlement credit to an owner (spendable and, while the owner
    is untrusted, escrow), an uptime escrow credit, an admission-style generation
    debit guarded by a consumed hold, a debit forgiven at the balance floor, a
    user-to-user transfer, and a hold that is released rather than consumed.
    ``mid_settle`` folds the ledger mid-sequence so, in ledger mode, projection
    interleaves with emission instead of running only at the end.
    """
    owner = participants.owner
    worker = participants.worker
    requester = participants.requester

    # Settlement: worker balance/stat credit, plus the untrusted owner's split
    # spendable and escrow credit and their contribution/fulfilment records.
    worker.record_contribution(raw_things=_RAW_THINGS, kudos=_SETTLEMENT_KUDOS, things_per_sec=1)
    db.session.commit()

    # Uptime reward on an untrusted owner routes entirely to the escrow balance.
    owner.record_uptime(kudos=_UPTIME_KUDOS)

    # Admission-style generation debit guarded by an upfront hold that its own
    # projected debit consumes.
    admission_hold = f"upfront:{requester.id}"
    assert reserve_kudos(requester, _ADMISSION_DEBIT, business_id=admission_hold) is not None
    requester.record_usage(
        raw_things=_RAW_THINGS,
        kudos=_ADMISSION_DEBIT,
        usage_type="image",
        reservation_id=admission_hold,
    )

    # A debit that overshoots the account floor: the part below the floor is
    # forgiven and recorded as created supply.
    participants.floor_user.modify_kudos(-_FLOOR_DEBIT, "accumulated", entry_type=KudosEntryType.GENERATION)

    mid_settle()

    # User-to-user transfer: source debit and destination credit under one hold.
    result = transfer_kudos(participants.giver, participants.receiver, _TRANSFER_AMOUNT)
    assert result[0] == _TRANSFER_AMOUNT

    # A hold that is released with no debit ever drawn against it.
    release_hold = f"release:{requester.id}"
    assert reserve_kudos(requester, _RELEASE_HOLD, business_id=release_hold) is not None
    release_reservation(release_hold)
    db.session.commit()


def _fold_to_quiescence() -> None:
    """Fold pending kudos work until a cycle folds nothing."""
    for _ in range(50):
        if apply_pending_kudos() == 0:
            return
    raise AssertionError("Kudos applier did not reach quiescence")


def _snapshot(participants: _Participants) -> dict[str, dict[object, object]]:
    """Read the balance and counter rows the workload touches, keyed by label."""
    user_balance: dict[object, object] = {}
    user_stats: dict[object, object] = {}
    user_records: dict[object, object] = {}
    worker_balance: dict[object, object] = {}
    worker_stats: dict[object, object] = {}

    for label, user in participants.users_by_label().items():
        kudos, evaluating = db.session.query(User.kudos, User.evaluating_kudos).filter(User.id == user.id).one()
        user_balance[label] = (int(kudos), int(evaluating))
        for action, value in db.session.query(UserStats.action, UserStats.value).filter(UserStats.user_id == user.id):
            user_stats[(label, action)] = int(value)
        for record_type, record, value in db.session.query(
            UserRecords.record_type,
            UserRecords.record,
            UserRecords.value,
        ).filter(UserRecords.user_id == user.id):
            user_records[(label, record_type.name, record)] = float(value)

    for label, worker in participants.workers_by_label().items():
        kudos, contributions, fulfilments = (
            db.session.query(WorkerTemplate.kudos, WorkerTemplate.contributions, WorkerTemplate.fulfilments)
            .filter(WorkerTemplate.id == worker.id)
            .one()
        )
        worker_balance[label] = (int(kudos), int(contributions), int(fulfilments))
        for action, value in db.session.query(WorkerStats.action, WorkerStats.value).filter(WorkerStats.worker_id == worker.id):
            worker_stats[(label, action)] = int(value)

    return {
        "user_balance": user_balance,
        "user_stats": user_stats,
        "user_records": user_records,
        "worker_balance": worker_balance,
        "worker_stats": worker_stats,
    }


def _deltas(initial: dict[str, dict[object, object]], final: dict[str, dict[object, object]]) -> dict[object, object]:
    """Reduce a before/after pair to per-key movements comparable across runs."""
    deltas: dict[object, object] = {}
    for label in final["user_balance"]:
        initial_kudos, initial_evaluating = initial["user_balance"][label]  # type: ignore[misc]
        final_kudos, final_evaluating = final["user_balance"][label]  # type: ignore[misc]
        deltas[("user_kudos", label)] = final_kudos - initial_kudos
        deltas[("user_evaluating", label)] = final_evaluating - initial_evaluating
    for label in final["worker_balance"]:
        initial_worker = initial["worker_balance"][label]  # type: ignore[misc]
        final_worker = final["worker_balance"][label]  # type: ignore[misc]
        deltas[("worker_kudos", label)] = final_worker[0] - initial_worker[0]
        deltas[("worker_contributions", label)] = final_worker[1] - initial_worker[1]
        deltas[("worker_fulfilments", label)] = final_worker[2] - initial_worker[2]
    for section in ("user_stats", "user_records", "worker_stats"):
        for key in set(final[section]) | set(initial[section]):
            deltas[(section, key)] = final[section].get(key, 0) - initial[section].get(key, 0)
    return deltas


def test_shadow_and_ledger_modes_produce_identical_balance_and_counter_movements(db_session, make_user, fake_redis) -> None:
    """One workload yields the same account deltas whether writes are inline or folded.

    Shadow mode materializes each movement inline; ledger mode records postings the
    applier folds afterwards. Run against two independent participant sets, the
    spendable and escrow balance movements plus every touched user stat, user
    record, worker aggregate, and worker stat must match position by position.
    Once the ledger run reaches quiescence there must be no unapplied postings or
    stat events, and neither mode may leave a hold open for any participant.
    """
    set_kudos_ledger_mode(KudosLedgerMode.SHADOW)
    shadow = _make_participants(make_user)
    shadow_before = _snapshot(shadow)
    _run_workload(shadow, mid_settle=lambda: None)
    shadow_after = _snapshot(shadow)
    shadow_deltas = _deltas(shadow_before, shadow_after)

    set_kudos_ledger_mode(KudosLedgerMode.LEDGER)
    ledger = _make_participants(make_user)
    ledger_before = _snapshot(ledger)
    _run_workload(ledger, mid_settle=_fold_to_quiescence)
    _fold_to_quiescence()
    ledger_after = _snapshot(ledger)
    ledger_deltas = _deltas(ledger_before, ledger_after)

    assert ledger_deltas == shadow_deltas

    # The workload must have actually moved money and counters, or an all-zero
    # equality above would prove nothing.
    assert shadow_deltas[("user_kudos", "owner")] == _SETTLEMENT_KUDOS // 2
    assert shadow_deltas[("user_evaluating", "owner")] == _SETTLEMENT_KUDOS // 2 + _UPTIME_KUDOS
    assert shadow_deltas[("worker_kudos", "worker")] == _SETTLEMENT_KUDOS
    assert shadow_deltas[("user_kudos", "receiver")] == _TRANSFER_AMOUNT
    # 30 balance minus a 100 debit is floored back up to the 25 minimum.
    assert shadow_deltas[("user_kudos", "floor_user")] == 25 - _FLOOR_USER_START

    assert db.session.query(KudosLedger).filter(KudosLedger.applied.is_(False)).count() == 0
    assert db.session.query(KudosStatEvent).filter(KudosStatEvent.applied.is_(False)).count() == 0
    # Both modes must retire every hold the workload took: inline consumption
    # when the movement materializes inline, fold-time consumption otherwise.
    participant_ids = [user.id for run in (shadow, ledger) for user in run.users_by_label().values()]
    open_reservations = (
        db.session.query(KudosReservation)
        .filter(
            KudosReservation.user_id.in_(participant_ids),
            KudosReservation.released_at.is_(None),
            KudosReservation.remaining_amount > 0,
        )
        .count()
    )
    assert open_reservations == 0
