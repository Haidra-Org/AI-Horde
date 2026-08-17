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

# The bridge-17 sampler tiers accepted by this API. The SDK supplies the
# per-sampler setting constraints; this short list defines which API additions
# the stress workload should exercise and which jobs a pre-17 worker must never
# receive.
_EXTENDED_IMAGE_SAMPLERS = (
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

_EXTENDED_SAMPLER_SETTING_FIELDS = frozenset(
    {
        "scheduler",
        "sampler_eta",
        "sampler_s_noise",
        "sampler_s_churn",
        "sampler_s_tmin",
        "sampler_s_tmax",
        "sampler_solver_type",
        "sampler_order",
        "flow_shift",
    },
)
