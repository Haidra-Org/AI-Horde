from horde.logger import logger

BRIDGE_CAPABILITIES = {
    "AI Horde Worker": {
        11: {"tiling"},
        10: {"CodeFormers"},
        8: {"r2"},
        6: {"karras"},
        4: {"inpainting"},
        2: {"img2img"},
    }
}

BRIDGE_SAMPLERS = {
    "AI Horde Worker": {
        12: {
            "karras": {"k_dpmpp_sde"},
            "no karras": {}
        },
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
            "no karras": {}
        },
    }
}

def parse_bridge_agent(bridge_agent):
    try:
        bridge_name, bridge_version, _ = bridge_agent.split(":", 2)
        if not bridge_version.isdigit():
            bridge_version = 0
        bridge_version = int(bridge_version)
    except Exception:
        logger.debug(f"Could not parse bridge_agent: {bridge_agent}")
        bridge_name = "unknown"
        bridge_version = 0
    logger.debug([bridge_name, bridge_version])
    return bridge_name,bridge_version

def check_bridge_capability(capability, bridge_agent):
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_CAPABILITIES:
        return False
    total_capabilities = set()
    # Because we start from 0 
    for iter in range(bridge_version + 1):
        if iter in BRIDGE_CAPABILITIES[bridge_name]:
            total_capabilities.update(BRIDGE_CAPABILITIES[bridge_name][iter])
    logger.debug([total_capabilities, capability, capability in total_capabilities])
    return capability in total_capabilities

def check_sampler_capability(sampler, bridge_agent, karras=True):
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    if bridge_name not in BRIDGE_SAMPLERS:
        # When it's an unknown worker agent, we let it through.
        return True
    available_samplers = set()
    for iter in range(bridge_version + 1):
        if iter in BRIDGE_SAMPLERS[bridge_name]:
            available_samplers.update(BRIDGE_SAMPLERS[bridge_name][iter]["karras"])
            # If karras == True, only karras samplers can be used.
            # Else, all samplers can be used
            if not karras:
                available_samplers.update(BRIDGE_SAMPLERS[bridge_name][iter]["no karras"])
    logger.debug([available_samplers, sampler, sampler in available_samplers])
    return sampler in available_samplers
