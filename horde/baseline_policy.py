# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which request features a model's baseline supports, and what the service charges for it.

Two authorities answer that and neither lives here: the served baseline record states which weights
and mechanisms exist for a family, and `bridge_reference` states which bridge releases render a
feature on it. A baseline with no record is permissive and priced at par, so a family published
ahead of this deployment reaches the worker holding the model rather than being refused here.

Critical public members:

- ``baseline_violation`` returns the rejection for the first feature a requested baseline cannot
  render.
- ``policy`` returns the served scheduling and pricing policy for a baseline.
- ``kudos_multiplier`` returns what one generation on a baseline costs relative to a par one.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Final

from horde_model_reference import BaselineCapabilities, HordeBaselinePolicy
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde.bridge_reference import bridge_supports
from horde.consts import FLOW_SHIFT_PARAM
from horde.enums import BaselineFeature
from horde.model_reference import model_reference

__all__ = [
    "ALL_BASELINE_FEATURES",
    "PAR_HORDE_POLICY",
    "PERMISSIVE_CAPABILITIES",
    "UNSUPPORTED_MODEL_RETURN_CODE",
    "baseline_violation",
    "kudos_multiplier",
    "policy",
]

ALL_BASELINE_FEATURES: Final[tuple[BaselineFeature, ...]] = tuple(BaselineFeature)
"""Evaluated in this order, which is the order the request path rejected these features in."""

UNSUPPORTED_MODEL_RETURN_CODE: Final[str] = "ControlNetUnsupported"
"""The one rejection that describes a model the request named rather than a field it set."""

PERMISSIVE_CAPABILITIES: Final[BaselineCapabilities] = BaselineCapabilities()
"""The capabilities a baseline the catalog has no record for is given."""

PAR_HORDE_POLICY: Final[HordeBaselinePolicy] = HordeBaselinePolicy()
"""What a baseline the catalog has no record for is priced and scheduled at."""


def _feature_violation(
    *,
    feature: BaselineFeature,
    baseline: str,
    params: Mapping[str, Any],
    source_processing: str | None,
) -> tuple[str, str] | None:
    """Return the return code and message when this baseline cannot render this feature, or None.

    A feature needs both the weights the catalog reports and a bridge release that drives them; a bridge
    that ignores a field renders something the request did not ask for rather than reporting an error.

    Args:
        feature: The request feature to evaluate.
        baseline: The baseline the job would run on.
        params: The generation payload.
        source_processing: The request's source processing mode, read by the remix feature.
    """
    record = model_reference.baseline_record(baseline)
    capabilities = record.capabilities if record is not None else PERMISSIVE_CAPABILITIES

    if feature == BaselineFeature.FLOW_SHIFT:
        flow_shift_is_set = params.get(FLOW_SHIFT_PARAM) is not None
        if flow_shift_is_set and not (capabilities.flow_matching and bridge_supports(feature, baseline)):
            return (
                "FlowShiftInapplicable",
                "flow_shift is only supported by model baselines whose backend graph applies it.",
            )
        return None

    if feature == BaselineFeature.HIRES_FIX:
        if params.get("hires_fix", False) is True and not bridge_supports(feature, baseline):
            return ("HiResMismatch", f"HiRes Fix does not work with {baseline} models currently.")
        return None

    if feature == BaselineFeature.TRANSPARENT:
        if params.get("transparent", False) is True and not (capabilities.transparent and bridge_supports(feature, baseline)):
            return ("InvalidTransparencyModel", f"Generating Transparent images is not possible for {baseline} models.")
        return None

    if feature == BaselineFeature.QR_CODE:
        if params.get("workflow") == "qr_code" and not capabilities.qr_code:
            # The trailing full stop is part of the code clients already match on.
            return ("ControlNetMismatch.", f"QR Code controlnet does not work with {baseline} models currently.")
        return None

    if feature == BaselineFeature.CONTROL_TYPE_UNAVAILABLE:
        control_type = params.get("control_type")
        if control_type is not None and control_type in capabilities.controlnet_types_unavailable:
            return (
                UNSUPPORTED_MODEL_RETURN_CODE,
                f"No current model available for the {control_type} ControlNet for {baseline} models.",
            )
        return None

    if feature == BaselineFeature.CONTROL_TYPE:
        if "control_type" in params and not (capabilities.controlnet and bridge_supports(feature, baseline)):
            return ("ControlNetMismatch", f"ControlNet does not work with {baseline} models currently.")
        return None

    if source_processing == "remix" and not capabilities.remix:
        return ("InvalidRemix", f"Image Remix is not available for {baseline} models.")
    return None


def baseline_violation(
    baselines: Collection[KNOWN_IMAGE_GENERATION_BASELINE | str],
    *,
    params: Mapping[str, Any],
    source_processing: str | None = None,
    features: Collection[BaselineFeature] = ALL_BASELINE_FEATURES,
) -> tuple[str, str] | None:
    """Return the return code and message of the first feature some requested baseline cannot render.

    The job may be dispatched for any of the named models, so a feature has to render on every one of
    their baselines. Features are evaluated in `ALL_BASELINE_FEATURES` order regardless of the order
    they are given in, so a request tripping two of them is always refused for the same one.

    Args:
        baselines: The baselines of the models the job may run on.
        params: The generation payload.
        source_processing: The request's source processing mode, read by the remix feature.
        features: The features to evaluate, for a call site that owns only some of them.

    Returns:
        The return code and message for the first violation, or None where the request is allowed.
    """
    for feature in ALL_BASELINE_FEATURES:
        if feature not in features:
            continue
        for baseline in sorted(str(requested_baseline) for requested_baseline in baselines):
            violation = _feature_violation(
                feature=feature,
                baseline=baseline,
                params=params,
                source_processing=source_processing,
            )
            if violation is not None:
                return violation
    return None


def policy(baseline: KNOWN_IMAGE_GENERATION_BASELINE | str | None) -> HordeBaselinePolicy:
    """Return the served scheduling and pricing policy for a baseline, or par where none is published."""
    record = model_reference.baseline_record(baseline)
    return record.horde_policy if record is not None else PAR_HORDE_POLICY


def kudos_multiplier(
    baseline: KNOWN_IMAGE_GENERATION_BASELINE | str | None,
    *,
    hires_fix: bool = False,
    qr_code: bool = False,
) -> float:
    """Return what one generation on this baseline costs relative to a par one.

    A feature that changes the shape of the render, rather than only its settings, has its own
    multiplier.

    Args:
        baseline: The baseline the generation runs on.
        hires_fix: Whether the request asked for the second pass.
        qr_code: Whether the request asked for the QR code workflow.
    """
    baseline_policy = policy(baseline)
    if qr_code and baseline_policy.kudos_qr_code is not None:
        return baseline_policy.kudos_qr_code
    if hires_fix and baseline_policy.kudos_hires is not None:
        return baseline_policy.kudos_hires
    return baseline_policy.kudos
