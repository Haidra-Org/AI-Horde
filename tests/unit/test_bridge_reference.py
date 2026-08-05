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
    CAPABILITY_EXPANDED_REGEN_VERSION,
    check_bridge_capability,
    check_sampler_capability,
    get_supported_samplers,
)
from horde.consts import EXTENDED_SAMPLERS, LEGACY_SAMPLERS, SOLVER_KNOB_SAMPLERS

pytestmark = pytest.mark.unit


def _regen_agent(version: int) -> str:
    return f"AI Horde Worker reGen:{version}:https://github.com/Haidra-Org/horde-worker-reGen"


class TestExtendedControlnetCapability:
    def test_classic_controlnet_available_on_old_regen(self):
        # The classic controlnet capability predates the extended gate.
        assert check_bridge_capability("controlnet", _regen_agent(13)) is True

    def test_extended_absent_below_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION - 1)
        assert check_bridge_capability("extended_controlnet", agent) is False

    def test_extended_present_at_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        assert check_bridge_capability("extended_controlnet", agent) is True

    def test_extended_present_above_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION + 5)
        assert check_bridge_capability("extended_controlnet", agent) is True

    def test_legacy_cpp_worker_never_gets_extended(self):
        # The legacy "AI Horde Worker" agent has no extended controlnet path at any version.
        assert check_bridge_capability("extended_controlnet", "AI Horde Worker:99:https://x") is False


class TestExtendedSamplers:
    def test_extended_samplers_absent_below_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION - 1)
        offered = get_supported_samplers(agent, karras=False)
        assert not (offered & EXTENDED_SAMPLERS), f"pre-threshold bridge was offered {offered & EXTENDED_SAMPLERS}"

    def test_extended_samplers_present_at_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        offered = get_supported_samplers(agent, karras=False)
        missing = EXTENDED_SAMPLERS - offered
        assert not missing, f"threshold bridge was not offered {missing}"

    def test_extended_samplers_offered_under_karras_too(self):
        # The backend picks the sigma schedule independently of the solver, so a karras request must
        # not lose the extended set.
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        missing = EXTENDED_SAMPLERS - get_supported_samplers(agent, karras=True)
        assert not missing, f"karras request was not offered {missing}"

    def test_classic_samplers_still_reach_old_bridges(self):
        # Old reGen bridges must keep every sampler they already rendered.
        offered = get_supported_samplers(_regen_agent(13), karras=False)
        for sampler in ("k_euler", "k_euler_a", "k_dpmpp_2m", "k_dpmpp_sde", "lcm"):
            assert sampler in offered, f"old bridge lost classic sampler {sampler}"

    def test_representative_extended_sampler_gates_by_version(self):
        assert check_sampler_capability("uni_pc", _regen_agent(13), karras=False) is False
        assert check_sampler_capability("uni_pc", _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION), karras=False) is True

    def test_legacy_cpp_worker_never_gets_extended_samplers(self):
        offered = get_supported_samplers("AI Horde Worker:99:https://x", karras=False)
        assert not (offered & EXTENDED_SAMPLERS)


class TestSchedulerFieldCapability:
    """Guards the shared version entry: two capabilities live under one key in BRIDGE_CAPABILITIES."""

    def test_absent_below_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION - 1)
        assert check_bridge_capability("scheduler", agent) is False

    def test_present_at_threshold(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        assert check_bridge_capability("scheduler", agent) is True

    def test_present_above_threshold(self):
        assert check_bridge_capability("scheduler", _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION + 5)) is True

    def test_sharing_a_version_entry_did_not_drop_the_other_capability(self):
        # A dict literal with a repeated key keeps only the last value, which would silently delete
        # whichever capability was declared first. Both must resolve at their own constant.
        assert check_bridge_capability("scheduler", _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)) is True
        assert check_bridge_capability("extended_controlnet", _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)) is True

    def test_legacy_cpp_worker_never_reads_the_field(self):
        assert check_bridge_capability("scheduler", "AI Horde Worker:99:https://x") is False


class TestOfferedSamplerSanity:
    def test_every_offered_sampler_is_an_accepted_sampler(self):
        # A bridge offered a sampler the API rejects can never be matched to a request.
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        offered = get_supported_samplers(agent, karras=False)
        unaccepted = offered - (LEGACY_SAMPLERS | EXTENDED_SAMPLERS | SOLVER_KNOB_SAMPLERS)
        assert not unaccepted, f"bridge offers samplers absent from KNOWN_SAMPLERS: {unaccepted}"


class TestSolverOptionsCapability:
    """The solver options, sigma generators, flow shift and newest samplers share one gate.

    Each fails the same silent way on a bridge that does not understand it: the setting is ignored and
    something other than what was asked for is rendered. None of them can be merely advertised.
    """

    def test_the_capabilities_are_absent_before_their_version(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION - 1)
        for capability in ("solver_options", "sigma_generators", "flow_shift", "solver_knob_samplers"):
            assert check_bridge_capability(capability, agent) is False, capability

    def test_the_capabilities_are_present_from_their_version(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        for capability in ("solver_options", "sigma_generators", "flow_shift", "solver_knob_samplers"):
            assert check_bridge_capability(capability, agent) is True, capability

    def test_the_capabilities_survive_a_later_version(self):
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION + 5)
        for capability in ("solver_options", "sigma_generators", "flow_shift", "solver_knob_samplers"):
            assert check_bridge_capability(capability, agent) is True, capability

    def test_the_older_capabilities_are_not_disturbed(self):
        # A repeated dict key would silently keep only the last entry, taking the older tier with it.
        agent = _regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION)
        assert check_bridge_capability("scheduler", agent) is True
        assert check_bridge_capability("extended_controlnet", agent) is True

    def test_a_non_regen_bridge_has_none_of_them(self):
        for capability in ("solver_options", "sigma_generators", "flow_shift", "solver_knob_samplers"):
            assert check_bridge_capability(capability, "AI Horde Worker:99:https://x") is False, capability


class TestSolverKnobSamplerGating:
    """The newest samplers only reach bridges whose backend maps them.

    An ungated dispatch does not fail loudly: a bridge that cannot name the solver falls back to its
    default one and returns an image from a sampler nobody requested.
    """

    def test_the_new_samplers_are_unavailable_before_their_version(self):
        available = get_supported_samplers(_regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION - 1), karras=True)
        for sampler in SOLVER_KNOB_SAMPLERS:
            assert sampler not in available, sampler

    def test_the_new_samplers_are_available_from_their_version(self):
        available = get_supported_samplers(_regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION), karras=True)
        for sampler in SOLVER_KNOB_SAMPLERS:
            assert sampler in available, sampler

    def test_the_earlier_tiers_remain_available(self):
        available = get_supported_samplers(_regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION), karras=True)
        for sampler in EXTENDED_SAMPLERS | LEGACY_SAMPLERS:
            assert sampler in available, sampler

    def test_only_the_new_tier_is_added_at_this_version(self):
        before = get_supported_samplers(_regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION - 1), karras=True)
        after = get_supported_samplers(_regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION), karras=True)
        assert set(after) - set(before) == set(EXTENDED_SAMPLERS | SOLVER_KNOB_SAMPLERS)

    def test_no_device_variant_is_ever_dispatchable(self):
        # These differ only in which device draws the noise, which is the worker's own concern.
        available = get_supported_samplers(_regen_agent(CAPABILITY_EXPANDED_REGEN_VERSION + 5), karras=False)
        assert not [sampler for sampler in available if sampler.endswith("_gpu")]
