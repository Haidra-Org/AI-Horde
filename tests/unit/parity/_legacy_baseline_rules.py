# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A frozen copy of the per-baseline rules the request path carried before the policy table.

Every expression here is transcribed from the hard-coded branches rather than rewritten, so a
difference the differential test reports is a difference in the new code and never a difference in
how this module chose to say the same thing. Nothing in this file may be "fixed": a rule that never
matched, or that read a dict by the wrong key, is part of the contract being measured against.

The baseline strings are the legacy spaced-and-underscored spellings the old model reference published,
which are what the old expressions were written against. Underscored spellings of the baselines the
legacy reference spelled with spaces never reached this code, so they are outside its domain.

Critical public members:

- ``LEGACY_RULE_PREDICATES`` maps a rule name to whether the old code rejected the request.
- ``legacy_first_rejection_rc`` returns the return code of the first rejection, in the old order.
- The ``legacy_*_multiplier``, ``legacy_resolution_floor`` and ``legacy_required_bridge_capability``
  functions are the old service-policy ladders.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any, Final

from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

# Transcribed from `main:horde/consts.py`.
LEGACY_BASELINE_BATCHING_MULTIPLIERS: Final[dict[str, int]] = {
    "flux_1": 5,
    "qwen_image": 10,
    "z_image_turbo": 8,
}

# Transcribed from `main:horde/consts.py`.
LEGACY_FLOW_SHIFT_BASELINES: Final[frozenset[KNOWN_IMAGE_GENERATION_BASELINE]] = frozenset(
    {
        KNOWN_IMAGE_GENERATION_BASELINE.flux_1,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_dev,
        KNOWN_IMAGE_GENERATION_BASELINE.qwen_image,
    },
)


def legacy_baseline_for_constraints(baseline_name: str | None) -> KNOWN_IMAGE_GENERATION_BASELINE | None:
    """Return the shared vocabulary's member for a legacy baseline string, or None.

    Transcribed from `main:horde/consts.py::baseline_for_constraints`.
    """
    if not baseline_name:
        return None
    for candidate in (baseline_name, baseline_name.replace(" ", "_")):
        try:
            return KNOWN_IMAGE_GENERATION_BASELINE(candidate)
        except ValueError:
            continue
    return None


def _rejects_flow_shift(baselines: Collection[str], params: Mapping[str, Any], source_processing: str | None) -> bool:
    """Return whether the old `validate_sampler_constraints` rejected this request's flow_shift."""
    del source_processing
    flow_shift = params.get("flow_shift")
    if flow_shift is None:
        return False
    constraint_baselines = {legacy_baseline_for_constraints(baseline) for baseline in baselines}
    return bool(constraint_baselines - LEGACY_FLOW_SHIFT_BASELINES)


def _rejects_hires_fix(baselines: Collection[str], params: Mapping[str, Any], source_processing: str | None) -> bool:
    """Return whether the old `validate_image_params` rejected this request's hires_fix."""
    del source_processing
    if params.get("hires_fix", False) is not True:
        return False
    return (
        any(baseline.startswith("flux_1") for baseline in baselines)
        or any(baseline.startswith("qwen_image") for baseline in baselines)
        or any(baseline.startswith("z_image_turbo") for baseline in baselines)
    )


def _rejects_transparent(baselines: Collection[str], params: Mapping[str, Any], source_processing: str | None) -> bool:
    """Return whether the old `validate_image_params` rejected this request's transparency."""
    del source_processing
    if params.get("transparent", False) is not True:
        return False
    return any(baseline not in ["stable_diffusion_xl", "stable diffusion 1"] for baseline in baselines)


def _rejects_qr_code(baselines: Collection[str], params: Mapping[str, Any], source_processing: str | None) -> bool:
    """Return whether the old `validate_image_params` rejected this request's qr_code workflow."""
    del source_processing
    if params.get("workflow") != "qr_code":
        return False
    return not all(baseline in ["stable diffusion 1", "stable_diffusion_xl"] for baseline in baselines)


def _rejects_sd2_control_type(
    baselines: Collection[str],
    params: Mapping[str, Any],
    source_processing: str | None,
) -> bool:
    """Return whether the old `ImageAsyncGenerate.validate` rejected this control type for SD2."""
    del source_processing
    if params.get("control_type") not in ["normal", "mlsd", "hough"]:
        return False
    return any(baseline.startswith("stable diffusion 2") for baseline in baselines)


def _rejects_controlnet(baselines: Collection[str], params: Mapping[str, Any], source_processing: str | None) -> bool:
    """Return whether the old `ImageAsyncGenerate.validate` rejected ControlNet for these baselines."""
    del source_processing
    if "control_type" not in params:
        return False
    return (
        any(baseline.startswith("stable_diffusion_xl") for baseline in baselines)
        or any(baseline.startswith("stable_cascade") for baseline in baselines)
        or any(baseline.startswith("flux_1") for baseline in baselines)
        or any(baseline.startswith("qwen_image") for baseline in baselines)
        or any(baseline.startswith("z_image_turbo") for baseline in baselines)
    )


def _rejects_remix(baselines: Collection[str], params: Mapping[str, Any], source_processing: str | None) -> bool:
    """Return whether the old `ImageAsyncGenerate.validate` rejected this remix request."""
    del params
    if source_processing != "remix":
        return False
    return any(not baseline.startswith("stable_cascade") for baseline in baselines)


LEGACY_RULE_PREDICATE = Callable[[Collection[str], Mapping[str, Any], str | None], bool]
"""Whether the old code rejected a request, given its baselines, payload and source processing."""

# Keyed by the rule names the policy table uses, so the differential compares like with like. The order
# is the old evaluation order: the `validation.py` rules in theirs, then the `stable.py` rules in theirs.
LEGACY_RULE_PREDICATES: Final[dict[str, LEGACY_RULE_PREDICATE]] = {
    "flow_shift_inapplicable": _rejects_flow_shift,
    "hires_fix_unsupported": _rejects_hires_fix,
    "transparency_unsupported": _rejects_transparent,
    "qr_code_workflow_unsupported": _rejects_qr_code,
    "control_type_unsupported": _rejects_sd2_control_type,
    "controlnet_unsupported": _rejects_controlnet,
    "remix_unsupported": _rejects_remix,
}

LEGACY_RULE_RETURN_CODES: Final[dict[str, str]] = {
    "flow_shift_inapplicable": "FlowShiftInapplicable",
    "hires_fix_unsupported": "HiResMismatch",
    "transparency_unsupported": "InvalidTransparencyModel",
    "qr_code_workflow_unsupported": "ControlNetMismatch.",
    "control_type_unsupported": "ControlNetUnsupported",
    "controlnet_unsupported": "ControlNetMismatch",
    "remix_unsupported": "InvalidRemix",
}


def legacy_rejecting_rules(
    baselines: Collection[str],
    *,
    params: Mapping[str, Any],
    source_processing: str | None = None,
) -> list[str]:
    """Return every rule the old code rejected this request under, in the old evaluation order."""
    return [rule_name for rule_name, predicate in LEGACY_RULE_PREDICATES.items() if predicate(baselines, params, source_processing)]


def legacy_first_rejection_rc(
    baselines: Collection[str],
    *,
    params: Mapping[str, Any],
    source_processing: str | None = None,
) -> str | None:
    """Return the return code the old code rejected this request with, or None where it accepted it."""
    rejecting = legacy_rejecting_rules(baselines, params=params, source_processing=source_processing)
    if not rejecting:
        return None
    return LEGACY_RULE_RETURN_CODES[rejecting[0]]


def legacy_gen_kudos_multiplier(baseline: str, *, hires_fix: bool, qr_code: bool) -> float:
    """Return the per-generation kudos factor the old `get_gen_kudos` ladder applied."""
    if baseline in ["stable_diffusion_xl"]:
        if qr_code:
            return 4
        return 2
    if baseline in ["stable_cascade"]:
        if hires_fix:
            return 7
        return 4
    if baseline in ["flux_1", "z_image_turbo"]:
        return 8
    if baseline in ["qwen_image"]:
        return 12
    return 1


def legacy_dry_run_quote_multiplier(baseline: str) -> float:
    """Return the factor the old `extrapolate_dry_run_kudos` ladder applied.

    The quote ladder had no workflow-dependent branches, so hires_fix and qr_code did not reach it.
    """
    if baseline in ["stable_diffusion_xl"]:
        return 2
    if baseline in ["stable_cascade"]:
        return 4
    if baseline in ["flux_1", "z_image_turbo"]:
        return 8
    if baseline in ["qwen_image"]:
        return 12
    return 1


def legacy_ttl_multiplier(baseline: str) -> int:
    """Return the lease multiplier the old `get_expiry_ttl` applied for the assigned model's baseline."""
    if baseline in ["flux_1", "qwen_image", "z_image_turbo"]:
        return 3
    return 1


def legacy_batching_multiplier(model_names: Collection[str]) -> int:
    """Return what the old `get_highest_model_batching_multiplier` returned.

    It looked up model names in a baseline-keyed table, so it returned 1 for every real request.
    """
    highest_multiplier = 1
    for model_name in model_names:
        if LEGACY_BASELINE_BATCHING_MULTIPLIERS.get(model_name, 1) > highest_multiplier:
            highest_multiplier = LEGACY_BASELINE_BATCHING_MULTIPLIERS[model_name]
    return highest_multiplier


def legacy_resolution_floor(baselines: Collection[str]) -> int:
    """Return the resolution floor the old `has_reasonable_resolution` raised max_res to.

    The SD2 branch compared against an underscored spelling the legacy reference never published, so it
    never matched.
    """
    floor = 0
    if any(baseline == "stable_diffusion_2" for baseline in baselines):
        floor = 768
    if any(baseline in ["stable_diffusion_xl", "stable_cascade", "flux_1"] for baseline in baselines):
        floor = max(floor, 1024)
    return floor


def legacy_required_bridge_capability(baseline: str) -> str | None:
    """Return the bridge capability the old `can_generate` gated this baseline on, if any."""
    if baseline == "flux_1":
        return "flux"
    return None
