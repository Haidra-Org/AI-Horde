# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capability gating in ``horde.bridge_reference.check_bridge_capability``.

The extended controlnet gate keeps image-generation jobs whose ``control_type``
falls outside the classic set from being dispatched to bridge agents too old to
annotate them. A regression here would either strand the new control types (no
worker ever matches) or silently route them to workers that cannot render them.
"""

from __future__ import annotations

import pytest

from horde.bridge_reference import EXTENDED_CONTROLNET_REGEN_VERSION, check_bridge_capability

pytestmark = pytest.mark.unit


def _regen_agent(version) -> str:
    return f"AI Horde Worker reGen:{version}:https://github.com/Haidra-Org/horde-worker-reGen"


class TestExtendedControlnetCapability:
    def test_classic_controlnet_available_on_old_regen(self):
        # The classic controlnet capability predates the extended gate.
        assert check_bridge_capability("controlnet", _regen_agent(13)) is True

    def test_extended_absent_below_threshold(self):
        agent = _regen_agent(EXTENDED_CONTROLNET_REGEN_VERSION - 1)
        assert check_bridge_capability("extended_controlnet", agent) is False

    def test_extended_present_at_threshold(self):
        agent = _regen_agent(EXTENDED_CONTROLNET_REGEN_VERSION)
        assert check_bridge_capability("extended_controlnet", agent) is True

    def test_extended_present_above_threshold(self):
        agent = _regen_agent(EXTENDED_CONTROLNET_REGEN_VERSION + 5)
        assert check_bridge_capability("extended_controlnet", agent) is True

    def test_legacy_cpp_worker_never_gets_extended(self):
        # The legacy "AI Horde Worker" agent has no extended controlnet path at any version.
        assert check_bridge_capability("extended_controlnet", "AI Horde Worker:99:https://x") is False
