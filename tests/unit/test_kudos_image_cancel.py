# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of settlement when an in-flight image generation is cancelled.

Cancelling a generation a worker is still processing is not free: the work
already in progress settles exactly as a normal submission would. The
contributing worker and its owner are credited the generation's kudos and the
requester is debited that kudos plus the request burn. A generation that has
already completed or faulted is inert: cancelling it settles nothing.

The image pop/submit HTTP lifecycle depends on S3-compatible object storage, so
cancellation settlement is characterized against the real
``ImageProcessingGeneration.cancel`` on a persisted ORM graph rather than driven
end to end over HTTP.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from horde.classes.base.user import User
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.enums import UserRoleTypes
from tests.fixture_types import MakeUser, MakeUserRole

GEN_KUDOS: int = 50
# calculate_extra_kudos_burn adds a flat +1 request burn (with slow_workers set,
# the non-slow 1.2x multiplier does not apply).
REQUEST_BURN: int = 1
# The worker/owner credit is scaled by the bridge multiplier; a non-official
# bridge (the default unknown agent) is rewarded at 0.75x. The requester debit is
# not scaled by the multiplier.
BRIDGE_MULTIPLIER: float = 0.75
WORKER_REWARD: float = GEN_KUDOS * BRIDGE_MULTIPLIER


@pytest.fixture(autouse=True)
def _trust_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUDOS_TRUST_THRESHOLD", "100000")


def _build_processing_generation(db_session: Session, requester: User, owner: User) -> ImageProcessingGeneration:
    """Create a persisted, still-processing image generation for the given requester and owner."""
    worker = ImageWorker(name=f"worker_{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db_session.add(worker)
    db_session.flush()

    wp = ImageWaitingPrompt(
        worker_ids=[],
        models=["stable_diffusion"],
        prompt="a test robot",
        user_id=requester.id,
        params={"width": 512, "height": 512, "steps": 8, "sampler_name": "k_euler_a"},
    )
    # Pin the per-generation reward and suppress the non-slow burn multiplier so the
    # settlement amounts are exact (test-bootstrap absolute set on the WP columns).
    wp.kudos = GEN_KUDOS
    wp.slow_workers = True
    db_session.flush()

    procgen = ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="stable_diffusion")
    db_session.flush()
    return procgen


class TestInFlightCancelSettles:
    """Cancelling a still-processing generation settles it like a submission."""

    def test_worker_and_owner_are_credited(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """Cancellation credits the worker and its owner the bridge-adjusted reward and returns it."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner)

        returned = procgen.cancel()

        assert procgen.worker.kudos == WORKER_REWARD
        assert owner.kudos == 1000 + WORKER_REWARD
        # cancel returns the bridge-adjusted worker reward.
        assert returned == WORKER_REWARD

    def test_requester_is_debited_gen_kudos_plus_burn(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """Cancellation debits the requester the generation kudos plus the request burn."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner)

        procgen.cancel()

        assert requester.kudos == 1000 - (GEN_KUDOS + REQUEST_BURN)

    def test_cancel_marks_the_generation_faulted(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """Cancellation marks the generation both faulted and cancelled."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner)

        procgen.cancel()

        assert procgen.faulted is True
        assert procgen.cancelled is True


class TestAlreadySettledCancelIsInert:
    """Cancelling an already-finished generation moves no kudos."""

    def test_faulted_generation_cancel_settles_nothing(
        self,
        db_session: Session,
        make_user: MakeUser,
        make_user_role: MakeUserRole,
    ) -> None:
        """Cancelling an already-faulted generation moves no kudos and returns nothing."""
        requester = make_user(kudos=1000)
        owner = make_user(kudos=1000)
        make_user_role(owner, UserRoleTypes.TRUSTED)
        procgen = _build_processing_generation(db_session, requester, owner)
        procgen.faulted = True
        db_session.flush()

        result = procgen.cancel()

        assert result is None
        assert procgen.worker.kudos == 0
        assert owner.kudos == 1000
        assert requester.kudos == 1000
