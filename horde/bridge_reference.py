import semver

from horde.consts import KNOWN_POST_PROCESSORS
from horde.logger import logger

BRIDGE_CAPABILITIES = {
    "AI Horde Worker reGen": {
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

BRIDGE_SAMPLERS = {  # TODO: Refactor along with schedulers
    "AI Horde Worker reGen": {
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


@logger.catch(reraise=True)
def check_bridge_capability(capability, bridge_agent):
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_CAPABILITIES:
        return False
    total_capabilities = set()
    # Because we start from 0
    for version in BRIDGE_CAPABILITIES[bridge_name]:
        checked_semver = semver.Version.parse(str(version), True)
        if checked_semver.compare(bridge_version) <= 0:
            total_capabilities.update(BRIDGE_CAPABILITIES[bridge_name][version])
    # logger.debug([total_capabilities, capability, capability in total_capabilities])
    return capability in total_capabilities


@logger.catch(reraise=True)
def get_supported_samplers(bridge_agent, karras=True):
    logger.debug(bridge_agent)
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_SAMPLERS:
        # When it's an unknown worker agent we treat it like AI Horde Worker
        bridge_name = "AI Horde Worker"
        bridge_version = 23
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
    return available_samplers


@logger.catch(reraise=True)
def check_sampler_capability(sampler, bridge_agent, karras=True):
    return sampler in get_supported_samplers(bridge_agent, karras)


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
    return available_pp


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
