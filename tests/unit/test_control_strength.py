# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Request-time enforcement of the ControlNet guidance weight.

The field weights a control map, so it means nothing on its own: a request that sets it without a
``control_type`` would be rendered as a plain generation with no sign that the setting was dropped.
Both rejections carry their own return code so a client can tell a missing dependency from a value
outside the accepted range without parsing the message.
"""

from __future__ import annotations

import pytest

from horde import exceptions as e
from horde.consts import CONTROL_STRENGTH_MAX, CONTROL_STRENGTH_MIN
from horde.exceptions import KNOWN_RC
from horde.validation import ParamValidator

pytestmark = pytest.mark.unit

MODELS = ["stable_diffusion"]


def rejection_code(params: dict) -> str | None:
    """Return the return code the control strength check rejects these params with, or None."""
    validator = ParamValidator(prompt="a prompt", models=MODELS, params=params, user=None)
    try:
        validator.validate_control_strength()
    except e.BadRequest as rejection:
        return rejection.rc
    return None


class TestControlStrengthDependency:
    def test_a_request_without_the_field_is_untouched(self):
        assert rejection_code({"control_type": "canny"}) is None

    def test_the_field_is_accepted_alongside_a_control_type(self):
        assert rejection_code({"control_type": "canny", "control_strength": 0.8}) is None

    def test_the_field_alone_is_rejected(self):
        assert rejection_code({"control_strength": 0.8}) == "ControlStrengthWithoutControlType"

    def test_a_null_control_type_does_not_satisfy_the_dependency(self):
        params = {"control_type": None, "control_strength": 0.8}
        assert rejection_code(params) == "ControlStrengthWithoutControlType"


class TestControlStrengthRange:
    @pytest.mark.parametrize("value", [CONTROL_STRENGTH_MIN, 1.0, CONTROL_STRENGTH_MAX])
    def test_the_accepted_range_is_inclusive(self, value: float):
        assert rejection_code({"control_type": "canny", "control_strength": value}) is None

    @pytest.mark.parametrize("value", [0.0, -1.0, 3.01, 10.0])
    def test_a_value_outside_the_range_is_rejected(self, value: float):
        assert rejection_code({"control_type": "canny", "control_strength": value}) == "ControlStrengthOutOfRange"

    def test_the_dependency_is_checked_before_the_range(self):
        # An out-of-range value with no control type names the missing dependency, which is the setting
        # the client has to resolve first.
        assert rejection_code({"control_strength": 99.0}) == "ControlStrengthWithoutControlType"


def test_both_return_codes_are_published():
    # A return code absent from KNOWN_RC is not documented to clients and cannot be branched on.
    assert "ControlStrengthWithoutControlType" in KNOWN_RC
    assert "ControlStrengthOutOfRange" in KNOWN_RC
