from horde.logger import logger

BRIDGE_CAPABILITIES = {
    "AI Horde Worker": {
        11: {
            "tiling",
        },
        10: {
            "CodeFormers",
        },
        8: {
            "r2",
        },
        6: {
            "karras",
        },
        4: {
            "painting",
        },
        2: {
            "img2img",
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
    return bridge_name,bridge_version


def check_bridge_capability(capability, bridge_agent):
    bridge_name, bridge_version = parse_bridge_agent(bridge_agent)
    logger.debug([bridge_name, bridge_version])
    if bridge_name not in BRIDGE_CAPABILITIES:
        return False
    total_capabilities = set()
    # Because we start from 0 
    for iter in range(bridge_version + 1):
        if iter in BRIDGE_CAPABILITIES[bridge_name]:
            total_capabilities.update(BRIDGE_CAPABILITIES[bridge_name][iter])
    logger.debug([total_capabilities, capability, capability in total_capabilities])
    return capability in total_capabilities
