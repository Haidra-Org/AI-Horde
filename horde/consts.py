HORDE_VERSION = "4.4.1"

WHITELISTED_SERVICE_IPS = {
    "212.227.227.178" # Turing Bot
}

# And their extra kudos adjustments based on how expensive to process they are and/or how much extra horde resources they consume
KNOWN_POST_PROCESSORS = {
    "GFPGAN": 1.0, 
    "RealESRGAN_x4plus": 1.3, 
    "RealESRGAN_x4plus_anime_6B": 1.3, 
    "CodeFormers": 1.3, 
    "strip_background": 1.2,
}

KNOWN_UPSCALERS = [
    "RealESRGAN_x4plus", 
    "RealESRGAN_x4plus_anime_6B", 
]