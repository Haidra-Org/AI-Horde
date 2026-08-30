# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The kudos counter fold: emission stamping, applier fold, and request-path purity.

Phase 1 moved the kudos *balance* columns onto the append-only ledger. This suite
pins the phase-2 extension that folds the per-action stats (``user_stats``,
``worker_stats``), the per-user records (``user_records``), and the worker
aggregate counters (``workers.contributions``, ``workers.fulfilments``) the
same way:

- every mutating primitive labels its posting (a kudos posting carries the stats
  bucket in ``stat_action``; a counter posting is a ``STAT_RECORD`` or
  ``STAT_CONTRIBUTION`` row with ``unit``/``stat_action``/``record`` dimensions);
- the applier reconstructs each counter by grouping the claimed batch on its
  dimension, reproducing the historical round-then-sum semantics, and folds
  balances and counters from one claimed batch in one transaction;
- the request transaction appends postings only: it writes no ``user_stats``,
  ``worker_stats``, ``user_records``, or worker-counter rows inline for the folded
  events, so the applier is their single writer.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from horde.classes.base.kudos import KudosLedger, KudosStatEvent, emit_kudos_ledger_entry, emit_kudos_stat_event
from horde.classes.base.team import Team
from horde.classes.base.user import User, UserRecords, UserStats
from horde.classes.base.worker import WorkerStats, WorkerTemplate
from horde.database.kudos_ledger import apply_pending_kudos, kudos_applier_health
from horde.enums import (
    KudosEntryType,
    KudosStatEventQuarantineReason,
    KudosStatRecord,
    KudosUnit,
    UserRecordTypes,
    UserRoleTypes,
)


def _make_worker(db_session: Any, owner: User) -> WorkerTemplate:
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:12]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    return worker


def _make_team(db_session: Any, owner: User) -> Team:
    team = Team(name=f"team_{uuid.uuid4().hex[:12]}", owner_id=owner.id)
    db_session.add(team)
    db_session.flush()
    return team


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


def _user_stat(db_session: Any, user_id: int, action: str) -> float | None:
    row = db_session.query(UserStats).filter_by(user_id=user_id, action=action).first()
    return None if row is None else row.value


def _worker_stat(db_session: Any, worker_id: Any, action: str) -> float | None:
    row = db_session.query(WorkerStats).filter_by(worker_id=worker_id, action=action).first()
    return None if row is None else row.value


def _user_record(db_session: Any, user_id: int, record_type: UserRecordTypes, record: str) -> float | None:
    row = db_session.query(UserRecords).filter_by(user_id=user_id, record_type=record_type, record=record).first()
    return None if row is None else row.value


class TestEmissionStamping:
    """Every mutating primitive labels its posting with the fold dimension.

    A kudos posting carries its ``user_stats``/``worker_stats`` bucket in
    ``stat_action`` and denominates ``amount`` in ``kudos``. A ``user_records``
    movement is a ``STAT_RECORD`` posting whose ``stat_action`` is the record type
    and whose ``record`` is the record dimension. A worker aggregate movement is a
    ``STAT_CONTRIBUTION`` posting discriminated by ``stat_action``.
    """

    def test_user_modify_kudos_stamps_the_action_bucket(self, db_session, make_user):
        user = make_user(kudos=1000)
        user.modify_kudos(10, "accumulated")
        db_session.commit()

        row = _stat_rows(db_session, user_id=user.id, record="user_kudos")[0]
        assert row.stat_action == "accumulated"
        assert row.unit == "kudos"

    def test_worker_modify_kudos_stamps_the_action_bucket(self, db_session, make_user):
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.modify_kudos(100, "generated")
        db_session.commit()

        row = _stat_rows(db_session, worker_id=worker.id, record="worker_kudos")[0]
        assert row.stat_action == "generated"
        assert row.unit == "kudos"

    def test_escrow_posting_is_not_stamped_with_a_stats_bucket(self, db_session, make_user):
        # The evaluation escrow half of an untrusted movement never fed user_stats;
        # it must not emit a stat event, so the fold cannot double the bucket.
        user = make_user(kudos=1000)
        user.modify_evaluating_kudos(50, KudosEntryType.GENERATION)
        db_session.commit()

        row = _ledger_rows(db_session, user_id=user.id)[0]
        assert row.escrow is True
        assert _stat_rows(db_session, user_id=user.id) == []

    def test_record_usage_emits_stat_record_postings(self, db_session, make_user):
        user = make_user(kudos=1000)
        user.record_usage(raw_things=2_000_000, kudos=10, usage_type="image")
        db_session.commit()

        record_rows = _stat_rows(db_session, entry_type=KudosEntryType.STAT_RECORD)
        by_action = {(r.stat_action, r.record): r for r in record_rows}
        request_row = by_action[(UserRecordTypes.REQUEST.name, "image")]
        usage_row = by_action[(UserRecordTypes.USAGE.name, "image")]
        assert request_row.amount == 1
        assert request_row.unit == "count"
        assert usage_row.amount == 2.0
        assert usage_row.unit == "things"

    def test_worker_contribution_emits_stat_contribution_postings(self, db_session, make_user, make_user_role):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        db_session.commit()

        contribution_rows = _stat_rows(db_session, entry_type=KudosEntryType.STAT_CONTRIBUTION)
        by_action = {r.stat_action: r for r in contribution_rows}
        assert by_action["contributions"].amount == 2.0
        assert by_action["contributions"].unit == "things"
        assert by_action["contributions"].worker_id == worker.id
        assert by_action["fulfilments"].amount == 1
        assert by_action["fulfilments"].unit == "count"


class TestPerDimensionFold:
    """The applier reconstructs each counter by grouping the batch on its dimension."""

    def test_user_stats_group_by_action(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        user.modify_kudos(10, "accumulated")
        user.modify_kudos(5, "accumulated")
        user.modify_kudos(500, "recurring")
        settle_kudos()

        assert _user_stat(db_session, user.id, "accumulated") == 15
        assert _user_stat(db_session, user.id, "recurring") == 500

    def test_worker_stats_group_by_action(self, db_session, make_user, settle_kudos):
        worker = _make_worker(db_session, make_user(kudos=0))
        worker.modify_kudos(100, "generated")
        worker.modify_kudos(40, "uptime")
        settle_kudos()

        assert _worker_stat(db_session, worker.id, "generated") == 100
        assert _worker_stat(db_session, worker.id, "uptime") == 40

    def test_user_records_group_by_type_and_record(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="text")
        settle_kudos()

        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "image") == 2
        assert _user_record(db_session, user.id, UserRecordTypes.USAGE, "image") == 2.0
        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "text") == 1

    def test_worker_counters_group_by_worker(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.record_contribution(raw_things=1_000_000, kudos=50, things_per_sec=1)
        worker.record_contribution(raw_things=1_000_000, kudos=50, things_per_sec=1)
        settle_kudos()

        assert worker.contributions == 2.0
        assert worker.fulfilments == 2

    def test_round_then_sum_reproduces_the_per_increment_total(self, db_session, make_user, settle_kudos):
        # thing_divisors["image"] is 1_000_000; three raw amounts that each scale to
        # a two-decimal usage value fold to their rounded sum.
        user = make_user(kudos=1000)
        for raw in (1_333_333, 1_333_333, 1_333_334):
            user.record_usage(raw_things=raw, kudos=1, usage_type="image")
        settle_kudos()

        # round(1.33) * 2 + round(1.33) == 3.99 by round-then-sum at fold.
        assert _user_record(db_session, user.id, UserRecordTypes.USAGE, "image") == 3.99


class TestDeletedWorkerFold:
    """Deleting a worker cannot poison the global statistics projector."""

    def test_worker_delete_preserves_pending_team_attribution(self, db_session: Any, make_user: Any) -> None:
        owner = make_user(kudos=0)
        team = _make_team(db_session, owner)
        worker = _make_worker(db_session, owner)
        worker_id = worker.id
        worker.modify_kudos(100, "generated", team_id=team.id)
        db_session.commit()

        event = _stat_rows(db_session, worker_id=worker_id)[0]
        assert event.applied is False

        worker.delete()

        db_session.expire_all()
        assert db_session.query(WorkerTemplate).filter_by(id=worker_id).first() is None
        assert db_session.query(KudosStatEvent).filter_by(id=event.id).one().applied is False
        assert apply_pending_kudos() == 1
        db_session.expire_all()
        assert db_session.query(KudosStatEvent).filter_by(id=event.id).one().applied is True
        assert db_session.query(Team).filter_by(id=team.id).one().kudos == 100
        assert _worker_stat(db_session, worker_id, "generated") is None

    def test_applier_skips_historical_orphan_worker_stat_event(self, db_session: Any, make_user: Any) -> None:
        worker = _make_worker(db_session, make_user(kudos=0))
        worker_id = worker.id
        worker.modify_kudos(100, "generated")
        db_session.commit()

        event_id = _stat_rows(db_session, worker_id=worker_id)[0].id
        # Simulate an orphan left by an older application version or an
        # out-of-band delete that bypassed Worker.delete().
        db_session.query(WorkerTemplate).filter_by(id=worker_id).delete(synchronize_session=False)
        db_session.commit()

        assert apply_pending_kudos() == 1
        db_session.expire_all()
        assert db_session.query(KudosStatEvent).filter_by(id=event_id).one().applied is True
        assert _worker_stat(db_session, worker_id, "generated") is None


class TestPoisonEventIsolation:
    """Deterministic malformed events are retained without blocking valid work."""

    @pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
    def test_emitters_reject_non_finite_amounts(self, db_session: Any, make_user: Any, amount: Decimal) -> None:
        user = make_user(kudos=0)

        with pytest.raises(ValueError, match="must be finite"):
            emit_kudos_ledger_entry(KudosEntryType.ADMIN_ADJUSTMENT, amount, user_id=user.id)
        with pytest.raises(ValueError, match="must be finite"):
            emit_kudos_stat_event(
                KudosEntryType.AWARD,
                amount,
                user_id=user.id,
                unit=KudosUnit.KUDOS,
                stat_action="awarded",
                record=KudosStatRecord.USER_KUDOS,
            )

    def test_unsupported_projection_is_quarantined_while_valid_event_folds(self, db_session: Any, make_user: Any) -> None:
        user = make_user(kudos=1000)
        user.modify_kudos(25, "awarded", entry_type=KudosEntryType.AWARD)
        malformed_event_id = uuid.uuid4()
        malformed = KudosStatEvent(
            event_id=malformed_event_id,
            entry_type=KudosEntryType.AWARD,
            user_id=user.id,
            amount=1,
            unit=KudosUnit.COUNT,
            stat_action="future",
            record="future",
            applied=False,
        )
        valid_peer = KudosStatEvent(
            event_id=malformed_event_id,
            entry_type=KudosEntryType.STAT_RECORD,
            user_id=user.id,
            amount=1,
            unit=KudosUnit.COUNT,
            stat_action=UserRecordTypes.REQUEST.name,
            record="image",
            applied=False,
        )
        db_session.add_all([malformed, valid_peer])
        db_session.commit()

        assert apply_pending_kudos() == 4
        db_session.expire_all()
        assert user.kudos == 1025
        assert _user_stat(db_session, user.id, "awarded") == 25
        quarantined = db_session.get(KudosStatEvent, malformed.id)
        assert quarantined is not None
        assert quarantined.applied is False
        assert quarantined.quarantined is True
        assert quarantined.quarantine_reason == KudosStatEventQuarantineReason.UNKNOWN_PROJECTION
        assert quarantined.quarantined_at is not None
        quarantined_peer = db_session.get(KudosStatEvent, valid_peer.id)
        assert quarantined_peer is not None
        assert quarantined_peer.quarantined is True
        assert quarantined_peer.quarantine_reason == KudosStatEventQuarantineReason.UNKNOWN_PROJECTION
        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "image") is None
        assert apply_pending_kudos() == 0

    def test_missing_user_projection_is_quarantined(self, db_session: Any, make_user: Any) -> None:
        user = make_user(kudos=0)
        event = KudosStatEvent(
            event_id=uuid.uuid4(),
            entry_type=KudosEntryType.STAT_RECORD,
            user_id=user.id,
            amount=1,
            unit=KudosUnit.COUNT,
            stat_action=UserRecordTypes.REQUEST.name,
            record="image",
            applied=False,
        )
        db_session.add(event)
        db_session.commit()
        event_id = event.id
        db_session.query(User).filter_by(id=user.id).delete(synchronize_session=False)
        db_session.commit()

        assert apply_pending_kudos() == 1
        db_session.expire_all()
        quarantined = db_session.get(KudosStatEvent, event_id)
        assert quarantined is not None
        assert quarantined.quarantined is True
        assert quarantined.quarantine_reason == KudosStatEventQuarantineReason.MISSING_USER

        health = kudos_applier_health()
        assert health["pending_rows"] == 0
        assert health["oldest_pending_seconds"] is None
        assert health["quarantined_rows"] == 1
        assert health["oldest_quarantined_seconds"] is not None

    @pytest.mark.parametrize(
        ("stat_action", "record", "message"),
        [
            ("x" * 21, "worker_kudos", "action exceeds 20"),
            ("generated", "x" * 31, "record exceeds 30"),
        ],
    )
    def test_emitter_rejects_dimensions_the_projection_tables_cannot_store(
        self,
        db_session: Any,
        make_user: Any,
        stat_action: str,
        record: str,
        message: str,
    ) -> None:
        worker = _make_worker(db_session, make_user(kudos=0))
        with pytest.raises(ValueError, match=message):
            emit_kudos_stat_event(
                KudosEntryType.GENERATION,
                1,
                worker_id=worker.id,
                unit=KudosUnit.KUDOS,
                stat_action=stat_action,
                record=record,
            )

    def test_failed_transaction_does_not_report_folded_rows(
        self,
        db_session: Any,
        make_user: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import horde.database.kudos_ledger as ledger_module

        user = make_user(kudos=1000)
        user.modify_kudos(25, "awarded", entry_type=KudosEntryType.AWARD)
        db_session.commit()
        calls: list[tuple[int, dict[str, str] | None]] = []

        class FoldedRecorder:
            def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
                calls.append((amount, attributes))

        def fail_projection(_deltas: Any) -> None:
            raise RuntimeError("projection failed")

        monkeypatch.setattr(ledger_module, "kudos_applier_folded", FoldedRecorder())
        monkeypatch.setattr(ledger_module, "_apply_user_stats_deltas", fail_projection)

        with pytest.raises(RuntimeError, match="projection failed"):
            apply_pending_kudos()
        assert calls == []
        db_session.rollback()


class TestSingleCycleAtomicity:
    """One claimed batch folds balances and every counter in one transaction."""

    def test_one_cycle_folds_balances_and_counters(self, db_session, make_user):
        user = make_user(kudos=1000)
        worker = _make_worker(db_session, user)
        user.modify_kudos(50, "accumulated")
        worker.modify_kudos(30, "generated")
        db_session.commit()

        folded = apply_pending_kudos()

        assert folded == 3
        assert user.kudos == 1050
        assert worker.kudos == 30
        assert _user_stat(db_session, user.id, "accumulated") == 50
        assert _worker_stat(db_session, worker.id, "generated") == 30


class TestRequestPathPurity:
    """In-scope request transactions append postings only, writing no counter rows.

    Until the applier folds, the ``user_stats``/``worker_stats``/``user_records``
    tables and the worker counter columns carry nothing for the event: the request
    path no longer touches them, so the applier is their single writer.
    """

    def test_record_usage_writes_no_user_counter_rows_inline(self, db_session, make_user):
        user = make_user(kudos=1000)
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        db_session.commit()

        assert db_session.query(UserStats).filter_by(user_id=user.id).count() == 0
        assert db_session.query(UserRecords).filter_by(user_id=user.id).count() == 0

    def test_worker_contribution_writes_no_worker_rows_inline(self, db_session, make_user, make_user_role):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)  # no team: isolates the worker/owner writes
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        db_session.commit()

        assert worker.contributions == 0
        assert worker.fulfilments == 0
        assert db_session.query(WorkerStats).filter_by(worker_id=worker.id).count() == 0
        assert db_session.query(UserRecords).filter_by(user_id=owner.id).count() == 0

    def test_counters_appear_only_after_the_fold(self, db_session, make_user, settle_kudos):
        user = make_user(kudos=1000)
        user.record_usage(raw_things=1_000_000, kudos=10, usage_type="image")
        db_session.commit()
        assert db_session.query(UserRecords).filter_by(user_id=user.id).count() == 0

        settle_kudos()

        assert _user_record(db_session, user.id, UserRecordTypes.REQUEST, "image") == 1
        assert _user_stat(db_session, user.id, "accumulated") == -10


class TestTeamAttributionStamping:
    """The image-submit settlement stamps team_id on the worker's own postings.

    The team aggregate is derived, not separately posted: when the inline gate
    fires (the worker has a team and its wtype is image), the settlement stamps the
    worker's kudos-credit, contribution, and fulfilment postings with the team id.
    Off the gate (no team, or a non-image worker) the postings carry no team id, so
    the fold attributes nothing to a team.
    """

    def _worker_postings(self, db_session: Any, worker: WorkerTemplate) -> list[KudosStatEvent]:
        return _stat_rows(db_session, worker_id=worker.id)

    def test_image_worker_with_team_stamps_all_worker_postings(self, db_session, make_user, make_user_role):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        team = _make_team(db_session, owner)
        worker = _make_worker(db_session, owner)
        worker.set_team(team)
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        db_session.commit()

        postings = self._worker_postings(db_session, worker)
        # The worker's own postings (generated credit, contribution things, fulfilment
        # count) all carry the team id; nothing else is emitted for the team.
        assert len(postings) == 3
        assert all(p.team_id == team.id for p in postings)

    def test_worker_without_a_team_stamps_no_team_id(self, db_session, make_user, make_user_role):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        db_session.commit()

        postings = self._worker_postings(db_session, worker)
        assert postings  # the worker postings still exist
        assert all(p.team_id is None for p in postings)

    def test_non_image_worker_with_a_team_stamps_no_team_id(self, db_session, make_user, make_user_role):
        # The inline gate is wtype == "image"; a text worker on a team feeds no team
        # aggregate. wtype is a class attribute, so an instance override exercises the
        # gate without a distinct worker subclass.
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        team = _make_team(db_session, owner)
        worker = _make_worker(db_session, owner)
        worker.set_team(team)
        worker.wtype = "text"
        worker.record_contribution(raw_things=2, kudos=100, things_per_sec=1)
        db_session.commit()

        postings = self._worker_postings(db_session, worker)
        assert postings
        assert all(p.team_id is None for p in postings)


class TestTeamAggregateFold:
    """The fold derives all three team columns from the stamped postings."""

    def test_three_columns_fold_from_the_worker_postings(self, db_session, make_user, make_user_role, settle_kudos):
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        team = _make_team(db_session, owner)
        worker = _make_worker(db_session, owner)
        worker.set_team(team)
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        worker.record_contribution(raw_things=1_000_000, kudos=50, things_per_sec=1)
        settle_kudos()

        assert team.contributions == 3.0
        assert team.fulfilments == 2
        assert team.kudos == 150

    def test_team_attribution_is_fixed_at_event_time_not_fold_time(self, db_session, make_user, make_user_role, settle_kudos):
        # team_id is stamped in the emitting transaction, so a worker that changes
        # teams between the event and the fold cannot misattribute: the original team
        # keeps the contribution and the new team gets nothing.
        owner = make_user(kudos=0)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        team_a = _make_team(db_session, owner)
        team_b = _make_team(db_session, owner)
        worker = _make_worker(db_session, owner)
        worker.set_team(team_a)
        worker.record_contribution(raw_things=2_000_000, kudos=100, things_per_sec=1)
        db_session.flush()
        worker.set_team(team_b)

        settle_kudos()

        assert team_a.contributions == 2.0
        assert team_a.fulfilments == 1
        assert team_a.kudos == 100
        assert team_b.contributions == 0
        assert team_b.fulfilments == 0
        assert team_b.kudos == 0
