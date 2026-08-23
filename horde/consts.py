# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_sdk.generation_parameters.image.constraints import (
    KNOWN_SAMPLER_SOLVER_TYPES,
)

HORDE_VERSION = "5.1.8"
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


# The samplers the `sampler_name` field accepts. Acceptance is not availability: a sampler is only
# dispatchable to bridge agents whose version advertises it (see BRIDGE_SAMPLERS), so an entry here
# that no online worker can render leaves the request queued rather than rejected.
LEGACY_SAMPLERS = {
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

# Solvers the image backend exposes beyond the classic set. These carry their backend's own spelling
# rather than the `k_` prefix of the k-diffusion block above, because they are backend-native solvers
# and the prefix would assert a k-diffusion lineage they do not have. `uni_pc`/`uni_pc_bh2` are not
# new to the backend at all: it has rendered them (and the kudos model has priced them) all along,
# and only their absence from the accepted set kept them unreachable.
#
# `dpmpp_3m_sde` carries a caveat worth knowing when reading pricing or support reports: it needs a
# low-noise sigma schedule, so it converges under `karras: true` and diverges to colour noise under
# `karras: false`. The constraint is the solver's own and is enforced backend-side.
EXTENDED_SAMPLERS = {
    "uni_pc",
    "uni_pc_bh2",
    "dpmpp_2m_sde",
    "dpmpp_3m_sde",
    "ddpm",
    "deis",
    "ipndm",
    "res_multistep",
    "gradient_estimation",
    "heunpp2",
    "er_sde",
    "sa_solver",
}

# The remainder of the image backend's non-`_gpu` solver list. Grouped apart from EXTENDED_SAMPLERS
# because they need a newer bridge: the release that maps EXTENDED_SAMPLERS does not name these, and a
# bridge that cannot name a solver falls back to its default one, returning an image from a sampler
# nobody asked for.
#
# The `_gpu` spellings the backend also offers are deliberately absent from every tier. They differ
# from their counterparts only in which device draws the sampling noise, which is the worker's own
# concern rather than something a request decides.
#
# The `*_cfg_pp` members apply the CFG++ correction and expect a far lower `cfg_scale` than the usual
# range. That is a quality expectation rather than a constraint, so it is warned about at validation
# rather than refused.
SOLVER_KNOB_SAMPLERS = {
    "euler_cfg_pp",
    "euler_ancestral_cfg_pp",
    "exp_heun_2_x0",
    "exp_heun_2_x0_sde",
    "dpmpp_2s_ancestral_cfg_pp",
    "dpmpp_2m_cfg_pp",
    "dpmpp_2m_sde_heun",
    "ipndm_v",
    "res_multistep_cfg_pp",
    "res_multistep_ancestral",
    "res_multistep_ancestral_cfg_pp",
    "gradient_estimation_cfg_pp",
    "seeds_2",
    "seeds_3",
    "sa_solver_pece",
}

KNOWN_SAMPLERS = LEGACY_SAMPLERS | EXTENDED_SAMPLERS | SOLVER_KNOB_SAMPLERS

# The sigma schedules a request may name. A schedule decides where in the noise range a sampler spends
# its steps rather than how many it takes, so it changes output character at no change in cost: karras
# concentrates steps at low sigmas (fine detail), exponential front-loads high-sigma removal
# (composition), and the effect is largest at low step counts where the spacing decides what resolves.
#
# `karras` and `normal` are the only two the legacy `karras` boolean can name, so they are the only two
# an old bridge can be asked for. The rest require a bridge that accepts the `scheduler` field.
LEGACY_SCHEDULERS = {
    "normal",
    "karras",
}

EXTENDED_SCHEDULERS = {
    "simple",
    "sgm_uniform",
    "exponential",
    "ddim_uniform",
    "beta",
    "linear_quadratic",
    "kl_optimal",
}

# Schedules the backend builds from a fixed table of sigmas rather than from the model, supplied by
# dedicated nodes rather than by its scheduler list. Two consequences follow, and both are enforced
# rather than advertised: they need a bridge that can drive those nodes, and each is only defined for
# the model families its table was built for, so requesting one against another family has no sigmas to
# sample on. The per-baseline restriction is read from horde_sdk rather than restated here.
SIGMA_GENERATOR_SCHEDULERS = {
    "align_your_steps",
    "gits",
}

KNOWN_SCHEDULERS = LEGACY_SCHEDULERS | EXTENDED_SCHEDULERS | SIGMA_GENERATOR_SCHEDULERS

# The per-request solver knobs, named as the image backend's solver functions name them. Which of these
# a given sampler actually accepts is not a fixed list: it is read per sampler from horde_sdk, because a
# knob a solver function does not declare is silently dropped by the backend rather than refused, and a
# request that quietly loses a setting it asked for has produced the wrong image.
SOLVER_KNOB_PARAMS = {
    "sampler_eta",
    "sampler_s_noise",
    "sampler_s_churn",
    "sampler_s_tmin",
    "sampler_s_tmax",
    "sampler_solver_type",
    "sampler_order",
}

# The timestep shift of a flow-matching model. Grouped apart from the solver knobs because it is a
# property of the model rather than of the solver, and only the flow-matching baselines have anything
# to shift.
FLOW_SHIFT_PARAM = "flow_shift"

# These bounds are the horde-engine 7.0.1 payload contract used by the reGen 17 beta.
FLOW_SHIFT_MIN = 0.0
FLOW_SHIFT_MAX = 100.0

# horde-engine 7.0.1 applies the shift to Flux-family graphs and to Qwen's existing AuraFlow node.
# Other graphs, including Z-Image, log a warning and ignore the value, so fail closed for those models.
FLOW_SHIFT_BASELINES = frozenset(
    {
        KNOWN_IMAGE_GENERATION_BASELINE.flux_1,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_dev,
        KNOWN_IMAGE_GENERATION_BASELINE.qwen_image,
    },
)

# Every `sampler_solver_type` value any sampler accepts, which is what the field can be validated
# against on its own. No sampler accepts all of them: the per-sampler vocabulary is the real constraint
# and is checked against horde_sdk at validation.
KNOWN_SOLVER_TYPES = {solver_type.value for solver_type in KNOWN_SAMPLER_SOLVER_TYPES}


def baseline_for_constraints(baseline_name):
    """Return the shared baseline vocabulary's name for a model reference baseline, or None.

    The model reference spells some baselines with spaces where the shared vocabulary uses underscores,
    so a plain lookup would miss exactly the two families the sigma-generator schedules are defined for.
    None means the baseline is not one the shared vocabulary knows, which leaves any per-baseline
    constraint unenforced rather than rejecting a model over a spelling.
    """
    if not baseline_name:
        return None
    for candidate in (baseline_name, baseline_name.replace(" ", "_")):
        try:
            return KNOWN_IMAGE_GENERATION_BASELINE(candidate)
        except ValueError:
            continue
    return None


SOLVER_OPTION_PARAMS = SOLVER_KNOB_PARAMS | {FLOW_SHIFT_PARAM}

# What `karras: true` and `karras: false` mean once a request can name a schedule outright. Ruling: the
# boolean keeps its existing meaning exactly, so no request already in flight changes its output. The
# field wins when both are supplied, because naming a schedule is the more specific instruction.
KARRAS_FLAG_SCHEDULERS = {True: "karras", False: "normal"}


def scheduler_for_request(scheduler, karras=True):
    """Return the schedule a request resolves to, preferring the field over the legacy flag.

    An unrecognised schedule falls back to the flag rather than raising: request-time validation is
    what rejects a bad value, and this is also read for already-stored payloads.
    """
    if scheduler in KNOWN_SCHEDULERS:
        return scheduler
    return KARRAS_FLAG_SCHEDULERS[bool(karras)]


KNOWN_WORKFLOWS = {"qr_code"}

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
