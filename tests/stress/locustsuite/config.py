# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared runtime state and constants for the AI Horde Locust suite."""

# Tiny 1x1 transparent PNG for interrogation requests (raw base64, no data-URL
# prefix: /interrogate/async's validator expects either a URL or a bare base64
# payload).
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

# Parsed config populated at test start from CLI/env args
_config: dict = {}

# Response codes / API rcs that the AI-Horde API legitimately returns under
# load and which we therefore should NOT count as test failures. They get
# reported to Locust as successes (so they don't pollute the failure table)
# but are also tracked under a separate "[expected-…]" name so the operator
# can still see the rate-limit / maintenance / contention frequency in the
# Locust UI.
_EXPECTED_RC_RECOVER = {
    "ProfaneWorkerName",  # worker name happened to contain a banned token; pick a new one
    "WorkerMaintenance",  # the simulated worker was put in maintenance for dropping jobs
    "WorkerFlaggedMaintenance",  # the user was auto-flagged for suspicious activity
    "WorkerInviteOnly",  # public worker creation is invite-only on this deployment
    "TooManyWorkers",  # untrusted user exceeded the 3-worker cap: rotate to a different key
    "TooManyWorkersTrusted",  # trusted user exceeded the 20-worker cap
    "TooManySameIPs",  # the same IP is hosting too many workers
    "TooManyNewIPs",  # IP is too new to host workers yet
    "UnsafeIP",  # IP flagged by countermeasures
    "AnonForbiddenWorker",  # attempted worker action with anon API key
    "PolymorphicNameConflict",  # worker name collides with a different worker_class
    "WrongCredentials",  # the stored API key doesn't own this worker name anymore
}

_HOT_PROMPT = "a serene cyberpunk landscape at sunset, ultra detailed"
_HOT_TEXT_PROMPT = "Once upon a time in a faraway land,"
_INTERROGATION_FORMS = ["caption", "interrogation", "nsfw", "vectorize", "palette", "describe", "aesthetic"]

# Keep this list local to the black-box workload instead of importing the Horde
# application. Importing the app from a locustfile initializes server-side
# configuration and defeats the point of exercising a separately running target.
_NEW_IMAGE_SAMPLERS = (
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
)

# Deterministic cases for SamplerFeatureRequester. The first block makes every
# newly accepted sampler traverse async creation and worker matching. The
# remaining cases cover every explicit scheduler and solver-control field with
# combinations accepted by the public constraints document.
_IMAGE_SAMPLER_FEATURE_CASES = tuple(
    {
        "name": f"sampler-{sampler}",
        "params": {"sampler_name": sampler, "scheduler": "karras"},
    }
    for sampler in _NEW_IMAGE_SAMPLERS
) + (
    {"name": "scheduler-normal", "params": {"sampler_name": "uni_pc", "scheduler": "normal"}},
    {"name": "scheduler-simple", "params": {"sampler_name": "uni_pc", "scheduler": "simple"}},
    {"name": "scheduler-sgm-uniform", "params": {"sampler_name": "uni_pc", "scheduler": "sgm_uniform"}},
    {"name": "scheduler-exponential", "params": {"sampler_name": "uni_pc", "scheduler": "exponential"}},
    {"name": "scheduler-ddim-uniform", "params": {"sampler_name": "uni_pc", "scheduler": "ddim_uniform"}},
    {"name": "scheduler-beta", "params": {"sampler_name": "uni_pc", "scheduler": "beta"}},
    {
        "name": "scheduler-linear-quadratic",
        "params": {"sampler_name": "uni_pc", "scheduler": "linear_quadratic"},
    },
    {"name": "scheduler-kl-optimal", "params": {"sampler_name": "uni_pc", "scheduler": "kl_optimal"}},
    {
        "name": "scheduler-align-your-steps",
        "params": {"sampler_name": "k_euler", "scheduler": "align_your_steps"},
        "models": ("stable_diffusion",),
    },
    {
        "name": "scheduler-gits",
        "params": {"sampler_name": "k_euler", "scheduler": "gits"},
        "models": ("stable_diffusion",),
    },
    {
        "name": "solver-eta-noise-type",
        "params": {
            "sampler_name": "dpmpp_2m_sde",
            "scheduler": "beta",
            "sampler_eta": 0.8,
            "sampler_s_noise": 1.05,
            "sampler_solver_type": "heun",
        },
    },
    {
        "name": "solver-churn-window",
        "params": {
            "sampler_name": "k_euler",
            "scheduler": "exponential",
            "sampler_s_churn": 0.1,
            "sampler_s_noise": 1.05,
            "sampler_s_tmin": 0.0,
            "sampler_s_tmax": 10.0,
        },
    },
    {
        "name": "solver-order",
        "params": {"sampler_name": "deis", "scheduler": "sgm_uniform", "sampler_order": 3},
    },
    {
        "name": "flow-shift",
        "params": {"sampler_name": "k_euler", "scheduler": "simple", "flow_shift": 1.1},
        "models": ("Flux.1-Schnell fp8 (Compact)",),
    },
)
