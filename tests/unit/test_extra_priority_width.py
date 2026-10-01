# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Queue priority holds the requester's whole kudos balance.

Activating a waiting prompt and creating an interrogation copy the requester's kudos into ``extra_priority``. Kudos is a
BIGINT, so a balance above the 32-bit range must store unchanged; a 32-bit column rejects the write with
"integer out of range" and the request fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.stable.interrogation import Interrogation
from horde.flask import db

pytestmark = pytest.mark.unit

KUDOS_ABOVE_INT32 = 3_000_000_000


def _make_text_wp(user: Any) -> TextWaitingPrompt:
    wp = TextWaitingPrompt(
        [],
        [],
        prompt="a unit-test prompt",
        user_id=user.id,
        params={"n": 1, "max_length": 80, "max_context_length": 2048},
    )
    db.session.commit()
    return wp


def test_activation_stores_a_priority_above_the_32_bit_range(db_session, fake_redis, make_user):
    user = make_user(kudos=KUDOS_ABOVE_INT32)
    wp = _make_text_wp(user)

    wp.activate()
    db.session.expire(wp, ["extra_priority"])

    assert wp.extra_priority == KUDOS_ABOVE_INT32


def test_interrogation_stores_a_priority_above_the_32_bit_range(db_session, fake_redis, make_user):
    user = make_user(kudos=KUDOS_ABOVE_INT32)
    interrogation = Interrogation(user_id=user.id)
    db.session.commit()
    db.session.expire(interrogation, ["extra_priority"])

    assert interrogation.extra_priority == KUDOS_ABOVE_INT32
