# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

HORDE_VERSION = "5.1.6"
HORDE_API_VERSION = "2.5"

WHITELISTED_SERVICE_IPS = {
    "212.227.227.178",  # Turing Bot
    "5.189.169.230",  # Discord Bot
}

# And their extra kudos adjustments based on how expensive to process they are and/or how much extra horde resources they consume
KNOWN_POST_PROCESSORS = {
    "GFPGAN": 1,
    "RealESRGAN_x4plus": 1.05,
    "RealESRGAN_x2plus": 1.05,
    "RealESRGAN_x4plus_anime_6B": 1.05,
    "NMKD_Siax": 1.05,
    "4x_AnimeSharp": 1.05,
    "CodeFormers": 1,
    "strip_background": 1,
    # Modern permissively-licensed upscalers served
    "4xNomos8kSC": 1.05,
    "4xLSDIRplus": 1.05,
    "4xNomosWebPhoto_RealPLKSR": 1.05,
    "4xNomos2_realplksr_dysample": 1.05,
    "4xNomos2_hq_dat2": 1.05,
    "2xModernSpanimationV1": 1.05,
    # Modern permissively-licensed face restorers
    "GFPGANv1.3": 1,
    "RestoreFormer": 1,
}

# Control-map types the parameterized `annotation` alchemy form can produce. This is the closed set
# the image-utilities backend can serve. It spells the line detector `mlsd` (its real name) where
# legacy image-generation control_type still spells the same detector `hough`.
KNOWN_CONTROL_TYPES = [
    "canny",
    "hed",
    "depth",
    "mlsd",
    "openpose",
    "normal",
    "scribble",
    "fakescribbles",
    "seg",
    "binary",
    "standard_lineart",
    "lineart",
    "lineart_anime",
    "lineart_anime_denoise",
    "pidinet",
    "scribble_xdog",
    "scribble_pidinet",
    "teed",
    "pyracanny",
    "midas_depth",
    "zoe_depth",
    "depth_anything",
    "depth_anything_v2",
    "normal_bae",
    "oneformer_ade20k",
    "oneformer_coco",
    "color",
    "shuffle",
    "recolor_luminance",
    "recolor_intensity",
    "tile",
    "tile_ttplanet_guided",
    "tile_ttplanet_simple",
]

# The classic image-generation control types renderable by legacy workers (pre image-utilities).
# Old bridges spell the line detector `hough`; KNOWN_CONTROL_TYPES spells the same detector `mlsd`.
# A control_type outside this set is only dispatchable to bridge agents new enough to annotate it.
LEGACY_IMAGE_CONTROL_TYPES = [
    "canny",
    "hed",
    "depth",
    "normal",
    "openpose",
    "seg",
    "scribble",
    "fakescribbles",
    "hough",
]

# The full set the image-generation `control_type` field accepts: the unified KNOWN_CONTROL_TYPES
# plus the legacy `hough` alias for `mlsd`, kept so existing clients keep validating.
IMAGE_CONTROL_TYPES = [*KNOWN_CONTROL_TYPES, "hough"]

# Alchemy/interrogation forms whose result is an image delivered via R2 (upload URL minted on pop,
# short-lived result cache) rather than an inline JSON payload. Post-processors are image-output by
# nature; the controlnet `annotation` form also emits an image (the control map).
IMAGE_RESULT_ALCHEMY_FORMS = set(KNOWN_POST_PROCESSORS) | {"annotation"}

# Detector cost classes for the parameterized `annotation` alchemy form, sourced from the
# image-utilities annotator registry runtimes. Each control type maps to a per-tile kudos
# multiplier reflecting the horde resources its detector consumes:
#   - WEIGHTLESS: pure OpenCV/numpy detectors that load no model weights.
#   - WEIGHTED: small/medium annotator checkpoints (measured ~2-8s CPU).
#   - HUB: large ViT/transformers-hub detectors (measured ~13-65s CPU).
# An unlisted control type falls back to the middle WEIGHTED class.
ANNOTATION_KUDOS_BUCKET_WEIGHTLESS = 3
ANNOTATION_KUDOS_BUCKET_WEIGHTED = 5
ANNOTATION_KUDOS_BUCKET_HUB = 8
ANNOTATION_KUDOS_DEFAULT_BUCKET = ANNOTATION_KUDOS_BUCKET_WEIGHTED

ANNOTATION_DETECTOR_KUDOS_BUCKETS = {
    "canny": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "binary": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "scribble": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "scribble_xdog": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "pyracanny": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "color": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "shuffle": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "recolor_luminance": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "recolor_intensity": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "tile": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "tile_ttplanet_guided": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "tile_ttplanet_simple": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "standard_lineart": ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    "hed": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "fakescribbles": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "mlsd": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "openpose": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "depth": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "seg": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "lineart": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "lineart_anime": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "lineart_anime_denoise": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "pidinet": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "scribble_pidinet": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "teed": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "normal_bae": ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    "midas_depth": ANNOTATION_KUDOS_BUCKET_HUB,
    "zoe_depth": ANNOTATION_KUDOS_BUCKET_HUB,
    "depth_anything": ANNOTATION_KUDOS_BUCKET_HUB,
    "depth_anything_v2": ANNOTATION_KUDOS_BUCKET_HUB,
    "normal": ANNOTATION_KUDOS_BUCKET_HUB,
    "oneformer_ade20k": ANNOTATION_KUDOS_BUCKET_HUB,
    "oneformer_coco": ANNOTATION_KUDOS_BUCKET_HUB,
}


def annotation_detector_kudos_bucket(control_type):
    """Return the per-tile kudos multiplier for an annotation control type.

    Unknown or missing control types fall back to the middle WEIGHTED bucket so
    a novel detector is never priced below its likely cost class.
    """
    return ANNOTATION_DETECTOR_KUDOS_BUCKETS.get(control_type, ANNOTATION_KUDOS_DEFAULT_BUCKET)


KNOWN_UPSCALERS = [
    "RealESRGAN_x4plus",
    "RealESRGAN_x2plus",
    "RealESRGAN_x4plus_anime_6B",
    "NMKD_Siax",
    "4x_AnimeSharp",
    "4xNomos8kSC",
    "4xLSDIRplus",
    "4xNomosWebPhoto_RealPLKSR",
    "4xNomos2_realplksr_dysample",
    "4xNomos2_hq_dat2",
    "2xModernSpanimationV1",
]

# These are postprocessors which require some juice,
# So we want to reduce the batch amount when used
HEAVY_POST_PROCESSORS = {
    "RealESRGAN_x4plus",
    "RealESRGAN_x4plus_anime_6B",
    "NMKD_Siax",
    "4x_AnimeSharp",
    "CodeFormers",
    # The ESRGAN/DAT additions are large; the RealPLKSR/SPAN additions are efficient and omitted.
    "4xNomos8kSC",
    "4xLSDIRplus",
    "4xNomos2_hq_dat2",
    # RestoreFormer is a ~290MB transformer face restorer (heavier, like CodeFormers); GFPGANv1.3 mirrors
    # GFPGAN and stays out of the heavy set.
    "RestoreFormer",
}

# These models are very large in VRAM, so we increase the calculated MPS
# used to figure out batches by a set multiplier to reduce how many images are batched
# at a time when these models are used.
BASELINE_BATCHING_MULTIPLIERS = {
    "flux_1": 5,
    "qwen_image": 10,
    "z_image_turbo": 8,
}


KNOWN_SAMPLERS = {
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
    "k_dpmpp_sde",
    "DDIM",
    "lcm",
}

KNOWN_WORKFLOWS = {"qr_code"}

# These samplers perform double the steps per image
# As such we need to take it into account for the upfront kudos requirements
SECOND_ORDER_SAMPLERS = [
    "k_heun",
    "k_dpm_2",
    "k_dpm_2_a",
    "k_dpmpp_2s_a",
    "k_dpmpp_sde",
]

KNOWN_LCM_LORA_VERSIONS = {
    "246747",
    "247778",
    "268475",
    "243643",
    "225222",
    "219782",
    "363353",
}

KNOWN_LCM_LORA_IDS = {
    "195519",
    "216190",
    "324115",
}

WHITELISTED_VPN_IPS = [
    "212.227.227.178/32",  # Turing Bot
    "141.144.197.64/32",
    # Digital Ocean / Paperspace
    "172.83.13.4/32",
    # Google
    "8.8.4.0/24",
    "8.8.8.0/24",
    "8.34.208.0/20",
    "8.35.192.0/20",
    "23.236.48.0/20",
    "23.251.128.0/19",
    "34.0.0.0/15",
    "34.2.0.0/16",
    "34.3.0.0/23",
    "34.3.3.0/24",
    "34.3.4.0/24",
    "34.3.8.0/21",
    "34.3.16.0/20",
    "34.3.32.0/19",
    "34.3.64.0/18",
    "34.3.128.0/17",
    "34.4.0.0/14",
    "34.8.0.0/13",
    "34.16.0.0/12",
    "34.32.0.0/11",
    "34.64.0.0/10",
    "34.128.0.0/10",
    "35.184.0.0/13",
    "35.192.0.0/14",
    "35.196.0.0/15",
    "35.198.0.0/16",
    "35.199.0.0/17",
    "35.199.128.0/18",
    "35.200.0.0/13",
    "35.208.0.0/12",
    "35.224.0.0/12",
    "35.240.0.0/13",
    "64.15.112.0/20",
    "64.233.160.0/19",
    "66.22.228.0/23",
    "66.102.0.0/20",
    "66.249.64.0/19",
    "70.32.128.0/19",
    "72.14.192.0/18",
    "74.114.24.0/21",
    "74.125.0.0/16",
    "104.154.0.0/15",
    "104.196.0.0/14",
    "104.237.160.0/19",
    "107.167.160.0/19",
    "107.178.192.0/18",
    "108.59.80.0/20",
    "108.170.192.0/18",
    "108.177.0.0/17",
    "130.211.0.0/16",
    "136.112.0.0/12",
    "142.250.0.0/15",
    "146.148.0.0/17",
    "162.216.148.0/22",
    "162.222.176.0/21",
    "172.110.32.0/21",
    "172.217.0.0/16",
    "172.253.0.0/16",
    "173.194.0.0/16",
    "173.255.112.0/20",
    "192.158.28.0/22",
    "192.178.0.0/15",
    "193.186.4.0/24",
    "199.36.154.0/23",
    "199.36.156.0/24",
    "199.192.112.0/22",
    "199.223.232.0/21",
    "207.223.160.0/20",
    "208.65.152.0/22",
    "208.68.108.0/22",
    "208.81.188.0/22",
    "208.117.224.0/19",
    "209.85.128.0/17",
    "216.58.192.0/19",
    "216.73.80.0/20",
    "216.239.32.0/19",
]
