# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import functools
from typing import Final

import semver
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde.consts import EXTENDED_SAMPLERS, KNOWN_POST_PROCESSORS, SOLVER_KNOB_SAMPLERS
from horde.enums import BaselineFeature
from horde.logger import logger

CAPABILITY_EXPANDED_REGEN_VERSION = 17
"""The reGen bridge version that expanded controlnet and sampler/scheduler capabilities

- `extended_controlnet`: the extended controlnet control types, rather than deriving them from the `controlnet` flag
- `scheduler`: the `scheduler` field, rather than deriving it from the `karras` flag
- `extended_samplers`: the extended samplers, rather than deriving them from the `karras` flag
- `solver_options`, `sigma_generators`, `flow_shift`, and `solver_knob_samplers` are advertised to
    clients that can read them, and gated at dispatch to bridges that understand them. A bridge that does
    not understand any one of them ignores it and renders something other than what was asked for rather
    than reporting an error, which is why each is gated at dispatch rather than merely advertised.
"""


CAPABILITY_CONTROL_STRENGTH_REGEN_VERSION = 18
"""The reGen bridge version that reads the `control_strength` field

A bridge older than the field applies its own default guidance weight and reports no error, so a job
carrying the field is gated at dispatch rather than merely advertised.
"""


BRIDGE_CAPABILITIES = {
    "AI Horde Worker reGen": {
        CAPABILITY_CONTROL_STRENGTH_REGEN_VERSION: {"control_strength"},
        CAPABILITY_EXPANDED_REGEN_VERSION: {
            "extended_controlnet",
            "scheduler",
            "solver_options",
            "sigma_generators",
            "flow_shift",
            "solver_knob_samplers",
        },
        13: {
            "4xNomos8kSC",
            "4xLSDIRplus",
            "4xNomosWebPhoto_RealPLKSR",
            "4xNomos2_realplksr_dysample",
            "4xNomos2_hq_dat2",
            "2xModernSpanimationV1",
            "GFPGANv1.3",
            "RestoreFormer",
        },
        9: {"flux"},
        8: {"layer_diffuse"},
        7: {"qr_code", "extra_texts", "workflow"},
        6: {"stable_cascade_2pass"},
        5: {"extra_source_images"},
        3: {"lora_versions"},
        2: {"textual_inversion", "lora"},
        1: {
            "img2img",
            "inpainting",
            "karras",
            "post-processing",
            "GFPGAN",
            "RealESRGAN_x4plus",
            "r2",
            "CodeFormers",
            "clip_skip",
            "r2_source",
            "controlnet",
            "strip_background",
            "return_control_map",
            "RealESRGAN_x4plus_anime_6B",
            "NMKD_Siax",
            "4x_AnimeSharp",
            "image_is_control",
            "RealESRGAN_x2plus",
            "hires_fix",
            "tiling",
        },
    },
    "AI Horde Worker": {
        24: {"textual_inversion"},
        23: {"image_is_control"},  # This used to be bridge version 16, but support was lost in the hordelib update
        22: {"lora"},
        21: {"RealESRGAN_x2plus"},
        19: {"NMKD_Siax", "4x_AnimeSharp"},
        18: {"strip_background", "return_control_map", "RealESRGAN_x4plus_anime_6B"},
        15: {"controlnet"},
        14: {"r2_source"},
        13: {"hires_fix", "clip_skip"},
        9: {"CodeFormers"},
        8: {"r2"},
        7: {"post-processing", "GFPGAN", "RealESRGAN_x4plus"},
        6: {"karras"},
        4: {"inpainting"},
        3: {"img2img"},
    },
    "SD-WebUI Stable Horde Worker Bridge": {
        4: {"clip_skip"},
        3: {"r2_source"},
        2: {"tiling"},
        1: {
            # "img2img",
            "inpainting",
            "karras",
            "r2",
            "CodeFormers",
        },
    },
    "HordeAutoWebBridge": {
        2: {
            "tiling",
        },
        1: {
            "painting",
            "img2img",
            "karras",
        },
    },
}

_SD1_AND_SD2_BASELINES: Final[frozenset[str]] = frozenset(
    {
        KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value,
        KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value,
        KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512.value,
    },
)
"""The baselines every ControlNet-era bridge release renders the same features on."""

BRIDGE_BASELINE_FEATURES: Final[dict[str, dict[int, dict[BaselineFeature, frozenset[str]]]]] = {
    "AI Horde Worker reGen": {
        1: {
            BaselineFeature.HIRES_FIX: _SD1_AND_SD2_BASELINES | {KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value},
            BaselineFeature.CONTROL_TYPE: _SD1_AND_SD2_BASELINES,
        },
        6: {BaselineFeature.HIRES_FIX: frozenset({KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value})},
        8: {
            BaselineFeature.TRANSPARENT: frozenset(
                {
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value,
                },
            ),
        },
        17: {
            BaselineFeature.FLOW_SHIFT: frozenset(
                {
                    KNOWN_IMAGE_GENERATION_BASELINE.flux_1.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value,
                },
            ),
        },
    },
    "AI Horde Worker": {
        13: {BaselineFeature.HIRES_FIX: _SD1_AND_SD2_BASELINES},
        15: {BaselineFeature.CONTROL_TYPE: _SD1_AND_SD2_BASELINES},
    },
}
"""Which baselines each bridge release renders a baseline-dependent feature on.

Cumulative by version, as `BRIDGE_CAPABILITIES` is: a worker gets the union of every version at or
below its own. A bridge that has no entry for a feature would ignore the field and render something
other than what was asked for, so the combination is refused rather than dispatched.
"""

BRIDGE_SAMPLERS = {  # TODO: Refactor along with schedulers
    "AI Horde Worker reGen": {
        # The backend applies the karras sigma schedule independently of which solver runs, so every
        # extended sampler is offered under both karras settings.
        CAPABILITY_EXPANDED_REGEN_VERSION: {"karras": SOLVER_KNOB_SAMPLERS | EXTENDED_SAMPLERS, "no karras": {}},
        3: {"karras": {"lcm"}, "no karras": {}},
        2: {
            "karras": {
                "k_lms",
                "k_heun",
                "k_euler",
                "k_euler_a",
                "k_dpm_2",
                "k_dpm_2_a",
                "k_dpm_fast",
                "k_dpm_adaptive",
                "k_dpmpp_2s_a",
                "k_dpmpp_2m",
                "k_dpmpp_sde",
                "dpmsolver",
                "DDIM",
            },
            "no karras": {},
        },
    },
    "AI Horde Worker": {
        17: {"karras": {}, "no karras": {"DDIM"}},
        12: {"karras": {"k_dpmpp_sde"}, "no karras": {}},
        11: {
            "karras": {
                "k_lms",
                "k_heun",
                "k_euler",
                "k_euler_a",
                "k_dpm_2",
                "k_dpm_2_a",
                "k_dpm_fast",
                "k_dpm_adaptive",
                "k_dpmpp_2s_a",
                "k_dpmpp_2m",
                "dpmsolver",
            },
            "no karras": {},
        },
    },
    "SD-WebUI Stable Horde Worker Bridge": {
        1: {
            "karras": {
                "k_lms",
                "k_heun",
                "k_euler",
                "k_euler_a",
                "k_dpm_2",
                "k_dpm_2_a",
                "k_dpm_fast",
                "k_dpm_adaptive",
                "k_dpmpp_2s_a",
                "k_dpmpp_2m",
                "k_dpmpp_sde",
            },
            "no karras": {
                "DDIM",
                "plms",
            },
        },
    },
    "HordeAutoWebBridge": {
        1: {
            "karras": {
                "k_lms",
                "k_dpm_2",
                "k_dpm_2_a",
                "k_dpmpp_2s_a",
                "k_dpmpp_2m",
                "dpmsolver",
            },
            "no karras": {
                "k_heun",
                "k_euler",
                "k_euler_a",
                "k_dpm_fast",
                "k_dpm_adaptive",
            },
        },
    },
}

LLM_VALIDATED_BACKENDS = {
    "AI Horde Worker",
    "AI Horde Worker~aphrodite~oai",
    "AI Horde Worker~aphrodite~kai",
    "KoboldCppEmbedWorker",
    "TabbyAPI",
}


@functools.lru_cache(maxsize=256)
@logger.catch(reraise=True)
def parse_bridge_agent(bridge_agent):
    try:
        bridge_name, bridge_version, _ = bridge_agent.split(":", 2)
        bridge_semver = semver.Version.parse(bridge_version, True)
        if not bridge_version.isdigit():
            bridge_version = 0
        bridge_version = int(bridge_version)
    except Exception as err:
        logger.debug(f"Could not parse bridge_agent '{bridge_agent}': {err}")
        bridge_name = "unknown"
        bridge_semver = semver.Version.parse("0", True)
    # logger.debug([bridge_name, bridge_version])
    return bridge_name, bridge_semver


@functools.lru_cache(maxsize=1024)
@logger.catch(reraise=True)
def get_bridge_capabilities(bridge_agent: str) -> frozenset[str]:
    """Return every feature capability supported by a bridge agent."""

    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_CAPABILITIES:
        return frozenset()
    total_capabilities: set[str] = set()
    for version in BRIDGE_CAPABILITIES[bridge_name]:
        checked_semver = semver.Version.parse(str(version), True)
        if checked_semver.compare(bridge_version) <= 0:
            total_capabilities.update(BRIDGE_CAPABILITIES[bridge_name][version])
    return frozenset(total_capabilities)


@functools.lru_cache(maxsize=1024)
@logger.catch(reraise=True)
def check_bridge_capability(capability: str, bridge_agent: str) -> bool:
    return capability in get_bridge_capabilities(bridge_agent)


@functools.lru_cache(maxsize=1024)
@logger.catch(reraise=True)
def bridge_supports(feature: BaselineFeature, baseline: str, bridge_agent: str | None = None) -> bool:
    """Return whether a bridge renders one baseline-dependent feature on one baseline.

    Args:
        feature: The baseline-dependent feature to check.
        baseline: The baseline the job would run on.
        bridge_agent: The worker's agent, or None to ask whether any known bridge kind at any version
            renders it, which decides whether the request is accepted.
    """
    if bridge_agent is None:
        return any(
            baseline in features.get(feature, frozenset())
            for bridge_versions in BRIDGE_BASELINE_FEATURES.values()
            for features in bridge_versions.values()
        )
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    bridge_versions = BRIDGE_BASELINE_FEATURES.get(bridge_name)
    if bridge_versions is None:
        return False
    for version, features in bridge_versions.items():
        checked_semver = semver.Version.parse(str(version), True)
        if checked_semver.compare(bridge_version) <= 0 and baseline in features.get(feature, frozenset()):
            return True
    return False


@logger.catch(reraise=True)
def is_backed_validated(bridge_agent: str) -> bool:
    bridge_name, _ = parse_bridge_agent(bridge_agent)
    return bridge_name in LLM_VALIDATED_BACKENDS


@functools.lru_cache(maxsize=256)
@logger.catch(reraise=True)
def get_supported_samplers(bridge_agent: str, karras: bool = True) -> frozenset[str]:
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_SAMPLERS:
        # When it's an unknown worker agent we treat it like AI Horde Worker
        bridge_name = "AI Horde Worker"
        bridge_version = semver.Version.parse("23.0.0", True)
    available_samplers = set()
    for version in BRIDGE_SAMPLERS[bridge_name]:
        checked_semver = semver.Version.parse(str(version), True)
        if checked_semver.compare(bridge_version) <= 0:
            available_samplers.update(BRIDGE_SAMPLERS[bridge_name][version]["karras"])
            # If karras == True, only karras samplers can be used.
            # Else, all samplers can be used
            if not karras:
                available_samplers.update(BRIDGE_SAMPLERS[bridge_name][version]["no karras"])
    # logger.debug([available_samplers, sampler, sampler in available_samplers])
    return frozenset(available_samplers)


@logger.catch(reraise=True)
def check_sampler_capability(sampler, bridge_agent, karras=True):
    return sampler in get_supported_samplers(bridge_agent, karras)


@functools.lru_cache(maxsize=256)
@logger.catch(reraise=True)
def get_supported_pp(bridge_agent):
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_SAMPLERS:
        # When it's an unknown worker agent we treat it like AI Horde Worker
        bridge_name = "AI Horde Worker"
        bridge_version = 23
    available_pp = set()
    for version in BRIDGE_CAPABILITIES[bridge_name]:
        checked_semver = semver.Version.parse(str(version), True)
        if checked_semver.compare(bridge_version) <= 0:
            for capability in BRIDGE_CAPABILITIES[bridge_name][version]:
                if capability in KNOWN_POST_PROCESSORS:
                    available_pp.add(capability)
    return frozenset(available_pp)


@logger.catch(reraise=True)
def get_latest_version(bridge_name):
    latest_semver = None
    for version in BRIDGE_CAPABILITIES[bridge_name]:
        chkver = semver.Version.parse(str(version), True)
        if latest_semver is None:
            latest_semver = semver.Version.parse(str(version), True)
        elif latest_semver.compare(chkver) < 0:
            latest_semver = chkver
    return latest_semver


@logger.catch(reraise=True)
def is_latest_bridge_version(bridge_agent):
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    latest_version = get_latest_version(bridge_name)
    return latest_version.compare(bridge_version) <= 0


@logger.catch(reraise=True)
def is_official_bridge_version(bridge_agent):
    bridge_name, _ = parse_bridge_agent(bridge_agent)
    return bridge_name in ["AI Horde Worker reGen", "AI Horde Worker"]
