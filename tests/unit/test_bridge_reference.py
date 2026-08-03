# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capability and sampler gating in ``horde.bridge_reference``.

The extended controlnet gate keeps image-generation jobs whose ``control_type``
falls outside the classic set from being dispatched to bridge agents too old to
annotate them. A regression here would either strand the new control types (no
worker ever matches) or silently route them to workers that cannot render them.
The extended sampler gate does the same for ``sampler_name``, where an ungated
request does not fail loudly: a bridge whose backend lacks the sampler renders
its default one instead.
"""

from __future__ import annotations

import pytest

from horde.bridge_reference import (
    EXTENDED_CONTROLNET_REGEN_VERSION,
    EXTENDED_SAMPLERS_REGEN_VERSION,
    SCHEDULER_FIELD_REGEN_VERSION,
    check_bridge_capability,
    check_sampler_capability,
    get_supported_samplers,
)
from horde.consts import EXTENDED_SAMPLERS, LEGACY_SAMPLERS

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


class TestExtendedSamplers:
    def test_extended_samplers_absent_below_threshold(self):
        agent = _regen_agent(EXTENDED_SAMPLERS_REGEN_VERSION - 1)
        offered = get_supported_samplers(agent, karras=False)
        assert not (offered & EXTENDED_SAMPLERS), f"pre-threshold bridge was offered {offered & EXTENDED_SAMPLERS}"

    def test_extended_samplers_present_at_threshold(self):
        agent = _regen_agent(EXTENDED_SAMPLERS_REGEN_VERSION)
        offered = get_supported_samplers(agent, karras=False)
        missing = EXTENDED_SAMPLERS - offered
        assert not missing, f"threshold bridge was not offered {missing}"

    def test_extended_samplers_offered_under_karras_too(self):
        # The backend picks the sigma schedule independently of the solver, so a karras request must
        # not lose the extended set.
        agent = _regen_agent(EXTENDED_SAMPLERS_REGEN_VERSION)
        missing = EXTENDED_SAMPLERS - get_supported_samplers(agent, karras=True)
        assert not missing, f"karras request was not offered {missing}"

    def test_classic_samplers_still_reach_old_bridges(self):
        # Old reGen bridges must keep every sampler they already rendered.
        offered = get_supported_samplers(_regen_agent(13), karras=False)
        for sampler in ("k_euler", "k_euler_a", "k_dpmpp_2m", "k_dpmpp_sde", "lcm"):
            assert sampler in offered, f"old bridge lost classic sampler {sampler}"

    def test_representative_extended_sampler_gates_by_version(self):
        assert check_sampler_capability("uni_pc", _regen_agent(13), karras=False) is False
        assert check_sampler_capability("uni_pc", _regen_agent(EXTENDED_SAMPLERS_REGEN_VERSION), karras=False) is True

    def test_legacy_cpp_worker_never_gets_extended_samplers(self):
        offered = get_supported_samplers("AI Horde Worker:99:https://x", karras=False)
        assert not (offered & EXTENDED_SAMPLERS)


class TestSchedulerFieldCapability:
    """Guards the shared version entry: two capabilities live under one key in BRIDGE_CAPABILITIES."""

    def test_absent_below_threshold(self):
        agent = _regen_agent(SCHEDULER_FIELD_REGEN_VERSION - 1)
        assert check_bridge_capability("scheduler", agent) is False

    def test_present_at_threshold(self):
        agent = _regen_agent(SCHEDULER_FIELD_REGEN_VERSION)
        assert check_bridge_capability("scheduler", agent) is True

    def test_present_above_threshold(self):
        assert check_bridge_capability("scheduler", _regen_agent(SCHEDULER_FIELD_REGEN_VERSION + 5)) is True

    def test_sharing_a_version_entry_did_not_drop_the_other_capability(self):
        # A dict literal with a repeated key keeps only the last value, which would silently delete
        # whichever capability was declared first. Both must resolve at their own constant.
        assert check_bridge_capability("scheduler", _regen_agent(SCHEDULER_FIELD_REGEN_VERSION)) is True
        assert check_bridge_capability("extended_controlnet", _regen_agent(EXTENDED_CONTROLNET_REGEN_VERSION)) is True

    def test_legacy_cpp_worker_never_reads_the_field(self):
        assert check_bridge_capability("scheduler", "AI Horde Worker:99:https://x") is False


class TestOfferedSamplerSanity:
    def test_every_offered_sampler_is_an_accepted_sampler(self):
        # A bridge offered a sampler the API rejects can never be matched to a request.
        agent = _regen_agent(EXTENDED_SAMPLERS_REGEN_VERSION)
        offered = get_supported_samplers(agent, karras=False)
        unaccepted = offered - (LEGACY_SAMPLERS | EXTENDED_SAMPLERS)
        assert not unaccepted, f"bridge offers samplers absent from KNOWN_SAMPLERS: {unaccepted}"
