# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of interrogation (alchemy) form settlement.

When an interrogation form is delivered, the worker and its owner are credited
the form's kudos (the owner's share following the usual trust routing) and the
requesting user is debited the form's kudos plus a fixed burn. The burn is one
kudos, raised to two when the request opted into slow workers.

The interrogation pop/submit HTTP lifecycle depends on S3-compatible object
storage, so settlement is characterized against the real
``InterrogationForms.record`` on a persisted ORM graph rather than driven end to
end over HTTP.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from horde.classes.base.kudos import KudosReservation
from horde.classes.base.user import User
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.enums import State, UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole


@pytest.fixture(autouse=True)
def _trust_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")


def _build_processing_form(
    db_session: Session,
    requester: User,
    owner: User,
    *,
    kudos: float = 3,
    slow_workers: bool = False,
) -> InterrogationForms:
    """Create a persisted, still-processing interrogation form for the given requester and owner."""
    interrogation = Interrogation(user_id=requester.id, slow_workers=slow_workers)
    worker = InterrogationWorker(name=f"iw_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    # The legacy declarative InterrogationForms model has an untyped implicit
    # constructor, so each keyword is suppressed for pyrefly's benefit only.
    form = InterrogationForms(
        i_id=interrogation.id,
        name="caption",
        kudos=kudos,
        state=State.PROCESSING,
        worker_id=worker.id,
        initiated=datetime.utcnow(),
    )
    db_session.add(form)
    db_session.flush()
    return form


class TestInterrogationSettlement:
    """Delivering a form credits the worker and owner and debits the requester with a burn."""

    def test_trusted_owner_settlement(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A trusted owner is credited on the balance while the requester pays kudos plus burn."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        form = _build_processing_form(db_session, requester, owner, kudos=3)

        form.record(form.kudos)
        settle_kudos()

        assert form.worker.kudos == 3
        assert owner.kudos == 1003
        # The requester pays the form kudos plus the one-kudos burn.
        assert requester.kudos == 1000 - (3 + 1)

    def test_untrusted_owner_share_is_escrowed(self, db_session: Session, make_user: MakeUser, settle_kudos: Callable[[], int]) -> None:
        """An untrusted owner's share is split between the balance and evaluation escrow."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        form = _build_processing_form(db_session, requester, owner, kudos=4)

        form.record(form.kudos)
        settle_kudos()

        assert form.worker.kudos == 4
        # Half the owner's credit is withheld for evaluation while untrusted.
        assert owner.kudos == 1002
        assert owner.evaluating_kudos == 2
        assert requester.kudos == 1000 - (4 + 1)

    def test_slow_worker_request_burns_an_extra_kudos(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A slow-worker request raises the requester's burn from one to two."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        form = _build_processing_form(db_session, requester, owner, kudos=3, slow_workers=True)

        form.record(form.kudos)
        settle_kudos()

        assert requester.kudos == 1000 - (3 + 2)


class TestInterrogationShadowProjection:
    """Shadow mode materializes interrogation settlement counters inline."""

    def test_shadow_mode_increments_worker_fulfilments_inline(self, db_session: Session, make_user: MakeUser) -> None:
        """Recording an interrogation raises the worker's fulfilment count without the applier.

        The generation path pairs its fulfilment stat event with an inline shim so
        the counter still moves before cutover; the interrogation path must do the
        same, so a shadow-mode fulfilment is visible immediately.
        """
        from horde.classes.base.kudos import set_kudos_ledger_mode
        from horde.enums import KudosLedgerMode

        set_kudos_ledger_mode(KudosLedgerMode.SHADOW)
        owner = make_user(kudos=1000)
        worker = InterrogationWorker(name=f"iw_{uuid.uuid4().hex[:8]}", user_id=owner.id)
        db_session.add(worker)
        db_session.flush()

        worker.record_interrogation(kudos=3, seconds_taken=1)

        assert worker.fulfilments == 1


class TestInterrogationUpfrontReservation:
    """Worker-required upfront checks use an atomic hold for the exact burn."""

    def test_slow_worker_burn_is_included_in_the_hold(self, db_session: Session, make_user: MakeUser) -> None:
        requester = make_user(kudos=29)  # Named floor 25 leaves four spendable.
        owner = make_user(kudos=1000)
        interrogation = Interrogation(user_id=requester.id, slow_workers=True)
        worker = InterrogationWorker(name=f"iw_{uuid.uuid4().hex[:8]}", user_id=owner.id)
        worker.require_upfront_kudos = True
        worker.prioritized_users = []
        db_session.add(worker)
        db_session.flush()
        form = InterrogationForms(i_id=interrogation.id, name="caption", kudos=3, state=State.WAITING)
        db_session.add(form)
        db_session.commit()

        assert form.pop(worker) is None
        assert db_session.query(KudosReservation).count() == 0
