# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Kudos settlement invariants for the interrogation flows.

Each test pins a durable contract the interrogation credit paths must honour.
They live in their own module so the settlement invariants stay grouped and
independently runnable.

The pinned contracts:
  * An interrogation worker crossing its uptime threshold grants an untrusted
    owner exactly one reward, and the untrusted-owner bypass places that reward
    on the spendable balance without also minting evaluation escrow.
  * Cancelling an interrogation form the worker is still processing settles it
    like a submission: the worker and owner are credited and the requester is
    debited the form kudos plus burn.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from horde.classes.base.user import User
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.enums import State, UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole

INTERROGATION_UPTIME_REWARD: int = 40
FORM_KUDOS: int = 3
FORM_BURN: int = 1


@pytest.fixture(autouse=True)
def _trust_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")


def _primed_uptime_worker(db_session: Session, owner: User) -> InterrogationWorker:
    """Create an interrogation worker primed to cross its uptime reward threshold on the next check-in."""
    worker = InterrogationWorker(name=f"iw_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    worker.last_check_in = datetime.utcnow() - timedelta(seconds=40)
    worker.uptime = worker.uptime_reward_threshold - 10
    worker.last_reward_uptime = 0
    db_session.flush()
    return worker


def _processing_form(db_session: Session, requester: User, owner: User) -> InterrogationForms:
    """Create a persisted, still-processing interrogation form for the given requester and owner."""
    interrogation = Interrogation(user_id=requester.id, slow_workers=False)
    worker = InterrogationWorker(name=f"iw_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()
    # The legacy declarative InterrogationForms model has an untyped implicit
    # constructor, so each keyword is suppressed for pyrefly's benefit only.
    form = InterrogationForms(
        i_id=interrogation.id,
        name="caption",
        kudos=FORM_KUDOS,
        state=State.PROCESSING,
        worker_id=worker.id,
        initiated=datetime.utcnow(),
    )
    db_session.add(form)
    db_session.flush()
    return form


def test_interrogation_uptime_credits_untrusted_owner_balance_without_escrow(
    db_session: Session,
    make_user: MakeUser,
) -> None:
    """The untrusted-owner bypass places the whole interrogation uptime reward on the balance and mints no escrow."""
    owner = make_user(kudos=1000)  # untrusted, non-anonymous
    worker = _primed_uptime_worker(db_session, owner)

    worker.check_in(4, forms=["caption"], ipaddr="10.0.0.1")

    # The bypass routes the whole reward to the spendable balance...
    assert owner.kudos == 1000 + INTERROGATION_UPTIME_REWARD
    # ...and no evaluation escrow is created by the same crossing.
    assert owner.evaluating_kudos == 0


def test_interrogation_uptime_grants_untrusted_owner_exactly_one_reward(
    db_session: Session,
    make_user: MakeUser,
) -> None:
    """An interrogation uptime crossing grants an untrusted owner exactly one reward across balance and escrow."""
    owner = make_user(kudos=1000)
    worker = _primed_uptime_worker(db_session, owner)

    worker.check_in(4, forms=["caption"], ipaddr="10.0.0.1")

    total_gain = (owner.kudos - 1000) + owner.evaluating_kudos
    assert total_gain == INTERROGATION_UPTIME_REWARD


def test_cancelling_in_flight_interrogation_form_settles_like_a_submit(
    db_session: Session,
    make_user: MakeUser,
    make_user_role: MakeUserRole,
) -> None:
    """Cancelling a still-processing interrogation form settles it like a submission."""
    requester = make_user(kudos=1000)
    owner = make_user(kudos=1000)
    make_user_role(owner, UserRoleTypes.TRUSTED)
    form = _processing_form(db_session, requester, owner)

    form.cancel()

    assert form.worker.kudos == FORM_KUDOS
    assert owner.kudos == 1000 + FORM_KUDOS
    assert requester.kudos == 1000 - (FORM_KUDOS + FORM_BURN)
