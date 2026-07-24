# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of request settlement and contribution accounting.

Covers the kudos movements that settle a completed job: the requester is debited
for usage, the contributing worker and its owner are credited, and the +2
style-owner credit is applied on styled generations. While a worker's owner is
untrusted, half of every contribution credit is held in an evaluation escrow
(``evaluating_kudos``) rather than the spendable balance; once that escrow
crosses the trust threshold it is flushed into the balance and the owner becomes
trusted. A censored generation debits the requester nothing.

The image pop/submit HTTP lifecycle depends on S3-compatible object storage, so
settlement is characterized against the real ``record_usage`` and
``record_contribution`` primitives on a persisted ORM graph rather than driven
end to end over HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.base.user import User, UserRole
from horde.classes.base.worker import WorkerTemplate
from horde.enums import UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole


def _make_worker(db_session: Session, owner: User) -> WorkerTemplate:
    """Create a persisted generic worker owned by the given user."""
    worker = WorkerTemplate(name=f"worker_{uuid.uuid4().hex[:12]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    return worker


class TestRequesterDebit:
    """Usage debits the requester by the charged amount."""

    def test_usage_debits_requester_balance(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """Recorded usage lowers the requester's balance by the charged kudos."""
        user = make_user(kudos=1000)
        user.record_usage(raw_things=10, kudos=50, usage_type="image")
        settle_kudos()
        assert user.kudos == 950

    def test_horde_tax_debit_uses_the_same_path(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """The up-front horde tax debits through the same usage path as ordinary usage."""
        user = make_user(kudos=1000)
        user.record_usage(raw_things=0, kudos=1, usage_type="image")
        settle_kudos()
        assert user.kudos == 999

    def test_usage_debit_respects_the_floor(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """A usage debit cannot push the requester below the user-class floor."""
        user = make_user(kudos=30)
        user.record_usage(raw_things=0, kudos=1000, usage_type="image")
        settle_kudos()
        assert user.kudos == 25


class TestCensoredDebit:
    """A censored generation charges the requester nothing."""

    def test_censored_generation_debits_zero(self) -> None:
        """A censored generation resolves to a zero requester debit."""
        # adjust_user_kudos reads only ``self.censored``; a SimpleNamespace is a
        # deliberate duck-typed stand-in for the ORM ``self``, so pyrefly's
        # self-type check is suppressed here and below.
        censored = SimpleNamespace(censored=True)
        assert ProcessingGeneration.adjust_user_kudos(censored, 100) == 0  # type: ignore

    def test_uncensored_generation_debits_full_amount(self) -> None:
        """An uncensored generation debits the full charged amount."""
        uncensored = SimpleNamespace(censored=False)
        assert ProcessingGeneration.adjust_user_kudos(uncensored, 100) == 100  # type: ignore


class TestStyleOwnerCredit:
    """A styled generation credits the style owner a fixed +2."""

    def test_style_credit_adds_two_kudos(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """Recording a style credits the owner two kudos."""
        owner = make_user(kudos=1000)
        owner.record_style(2, "image")
        settle_kudos()
        assert owner.kudos == 1002


class TestWorkerContributionCredit:
    """A contribution credits the worker and its owner."""

    def test_trusted_owner_receives_full_credit_on_balance(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A trusted owner receives the whole contribution credit on the spendable balance."""
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)

        worker.record_contribution(raw_things=1000, kudos=100, things_per_sec=1)
        db_session.commit()
        settle_kudos()

        assert worker.kudos == 100
        assert owner.kudos == 1100
        assert owner.evaluating_kudos == 0

    def test_worker_credit_scales_with_bridge_multiplier(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        monkeypatch: pytest.MonkeyPatch,
        settle_kudos: Callable[[], int],
    ) -> None:
        """The bridge multiplier scales both the worker and owner contribution credit."""
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        worker = _make_worker(db_session, owner)
        monkeypatch.setattr(type(worker), "get_bridge_kudos_multiplier", lambda self: 2)

        worker.record_contribution(raw_things=1000, kudos=100, things_per_sec=1)
        db_session.commit()
        settle_kudos()

        assert worker.kudos == 200
        assert owner.kudos == 1200


class TestUntrustedEscrowSplit:
    """While untrusted, half of a contribution credit is held in escrow."""

    def test_half_of_credit_goes_to_escrow(
        self,
        db_session: Session,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
        settle_kudos: Callable[[], int],
    ) -> None:
        """An untrusted owner's contribution credit splits evenly between balance and escrow."""
        # A fresh account is not yet trusted, so half its contribution credit is
        # withheld for evaluation and half reaches the spendable balance. The
        # escrow stays below the trust threshold, so no promotion occurs.
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")
        owner = make_user(kudos=1000)
        worker = _make_worker(db_session, owner)

        worker.record_contribution(raw_things=1000, kudos=100, things_per_sec=1)
        db_session.commit()
        settle_kudos()

        assert owner.kudos == 1050
        assert owner.evaluating_kudos == 50
        # The worker itself is always credited the full contribution.
        assert worker.kudos == 100

    def test_anonymous_owner_is_not_escrowed(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """An anonymous owner receives the whole contribution credit on the balance."""
        anon = make_user(username="Anonymous", oauth_id="anon", kudos=1000)
        worker = _make_worker(db_session, anon)

        worker.record_contribution(raw_things=1000, kudos=100, things_per_sec=1)
        db_session.commit()
        settle_kudos()

        assert anon.kudos == 1100
        assert anon.evaluating_kudos == 0


class TestTrustPromotion:
    """Crossing the trust threshold flushes escrow into the balance."""

    def test_escrow_flush_credits_balance_and_grants_trust(
        self,
        db_session: Session,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Crossing the threshold empties escrow into the balance and records a trusted role."""
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
        owner = make_user(kudos=1000, created=datetime.utcnow() - timedelta(days=8))
        owner.evaluating_kudos = 250
        db_session.flush()

        owner.check_for_trust()
        settle_kudos()

        assert owner.evaluating_kudos == 0
        assert owner.kudos == 1250
        # The owner is now trusted: a committed TRUSTED role records the promotion.
        db_session.refresh(owner)
        assert owner.trusted is True
        role = db_session.query(UserRole).filter_by(user_id=owner.id, user_role=UserRoleTypes.TRUSTED).first()
        assert role is not None and role.value is True

    def test_escrow_below_threshold_is_not_promoted(
        self,
        db_session: Session,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Escrow below the threshold stays escrowed and grants no trust."""
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
        owner = make_user(kudos=1000, created=datetime.utcnow() - timedelta(days=8))
        owner.evaluating_kudos = 50
        db_session.flush()

        owner.check_for_trust()

        assert owner.evaluating_kudos == 50
        assert owner.kudos == 1000
        assert owner.trusted is False

    def test_young_account_is_not_promoted(
        self,
        db_session: Session,
        make_user: MakeUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An account younger than a week is not promoted even with sufficient escrow."""
        # Promotion requires the account to have existed for at least a week.
        monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100")
        owner = make_user(kudos=1000, created=datetime.utcnow() - timedelta(days=2))
        owner.evaluating_kudos = 250
        db_session.flush()

        owner.check_for_trust()

        assert owner.evaluating_kudos == 250
        assert owner.trusted is False
