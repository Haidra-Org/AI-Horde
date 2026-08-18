# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AI-Horde policy over the SDK's sampler trajectory and work primitives.

The SDK owns portable units, sampler profiles, and execution contracts. This module owns only
AI-Horde decisions: its stable adaptive estimate and the compatibility translation from validated
legacy payload dictionaries.

Critical public members:

- ``AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY`` defines the service's adaptive accounting estimate.
- ``SamplerWorkRequest`` provides a typed boundary around legacy payload dictionaries.
- ``parse_sampler_execution_contract_version`` validates worker conformance claims fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from horde_sdk.generation_parameters.image.constraints import (
    SAMPLER_SOLVER_KNOB,
    NumericKnobRange,
    get_sampler_constraints,
)
from horde_sdk.generation_parameters.image.consts import KNOWN_IMAGE_SAMPLERS
from horde_sdk.generation_parameters.image.sampler_work import (
    SamplerExecutionContractVersion,
    SamplerWorkCeiling,
    SamplerWorkEstimate,
    SamplerWorkEstimationPolicy,
    SamplerWorkUnitCount,
    TrajectoryStepCount,
    estimate_sampler_work,
    maximum_sampler_work,
    maximum_trajectory_steps_for_work_budget,
)

__all__ = [
    "AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY",
    "SamplerWorkRequest",
    "estimate_request_sampler_work",
    "maximum_request_sampler_work",
    "maximum_request_trajectory_steps_for_work_budget",
    "parse_sampler_execution_contract_version",
    "sampler_work_request_from_payload",
]

AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY: Final[SamplerWorkEstimationPolicy] = SamplerWorkEstimationPolicy(
    adaptive_sampler_work_units={
        KNOWN_IMAGE_SAMPLERS.k_dpm_adaptive: SamplerWorkUnitCount(40),
    },
)
"""AI-Horde's stable request-level estimate for adaptive sampler accounting."""

_LEGACY_UNKNOWN_SAMPLER_FALLBACK: Final[KNOWN_IMAGE_SAMPLERS] = KNOWN_IMAGE_SAMPLERS.k_euler_a
_DPM_ADAPTIVE_ORDER_RANGE: Final[NumericKnobRange] = get_sampler_constraints(
    KNOWN_IMAGE_SAMPLERS.k_dpm_adaptive,
).numeric_knob_ranges[SAMPLER_SOLVER_KNOB.order]
_DEFAULT_DPM_ADAPTIVE_ORDER: Final[int] = int(_DPM_ADAPTIVE_ORDER_RANGE.default)


@dataclass(frozen=True, slots=True)
class SamplerWorkRequest:
    """Represents the sampler inputs that can affect operational work accounting."""

    sampler: KNOWN_IMAGE_SAMPLERS
    """Sampler selected by the request."""

    trajectory_steps: TrajectoryStepCount
    """Requested denoising-schedule length."""

    adaptive_work_units_per_iteration: int | None = None
    """Adaptive solver order, expressed as work units consumed per solver iteration."""


def _payload_integer(payload: Mapping[str, object], field: str, *, default: int) -> int:
    """Read an already-validated integer from a legacy payload without accepting booleans."""
    value = payload.get(field, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer, got {type(value).__name__}.")
    return value


def sampler_work_request_from_payload(payload: Mapping[str, object]) -> SamplerWorkRequest:
    """Translate a validated legacy image payload into unit-safe sampler work inputs.

    Unknown sampler strings retain AI-Horde's historic first-order compatibility behavior. The SDK
    registry itself stays strict and never accepts an unknown sampler.

    Args:
        payload: Validated legacy image-generation parameter mapping.

    Returns:
        Typed sampler work inputs.

    Raises:
        TypeError: If a sampler work field has the wrong runtime type.
        ValueError: If a numeric sampler work field is outside its accepted range.
    """
    raw_sampler = payload.get("sampler_name", str(_LEGACY_UNKNOWN_SAMPLER_FALLBACK))
    if not isinstance(raw_sampler, str):
        raise TypeError(f"sampler_name must be a string, got {type(raw_sampler).__name__}.")
    sampler = next(
        (known_sampler for known_sampler in KNOWN_IMAGE_SAMPLERS if known_sampler.value == raw_sampler),
        _LEGACY_UNKNOWN_SAMPLER_FALLBACK,
    )

    trajectory_steps = TrajectoryStepCount(_payload_integer(payload, "steps", default=30))
    adaptive_work_units_per_iteration = None
    if sampler is KNOWN_IMAGE_SAMPLERS.k_dpm_adaptive:
        adaptive_work_units_per_iteration = _payload_integer(
            payload,
            "sampler_order",
            default=_DEFAULT_DPM_ADAPTIVE_ORDER,
        )
        if not _DPM_ADAPTIVE_ORDER_RANGE.contains(float(adaptive_work_units_per_iteration)):
            raise ValueError(
                f"sampler_order {adaptive_work_units_per_iteration} is outside the adaptive sampler's accepted range.",
            )

    return SamplerWorkRequest(sampler, trajectory_steps, adaptive_work_units_per_iteration)


def estimate_request_sampler_work(request: SamplerWorkRequest) -> SamplerWorkEstimate:
    """Return AI-Horde's operational accounting estimate for one sampler request.

    Args:
        request: Typed sampler and trajectory inputs.

    Returns:
        The service's estimated sampler work.
    """
    return estimate_sampler_work(
        sampler=request.sampler,
        trajectory_steps=request.trajectory_steps,
        estimation_policy=AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY,
    )


def maximum_request_sampler_work(
    request: SamplerWorkRequest,
    *,
    execution_contract_version: SamplerExecutionContractVersion | None,
) -> SamplerWorkCeiling | None:
    """Return the backend-guaranteed work ceiling for a request when one is finite.

    Args:
        request: Typed sampler and trajectory inputs.
        execution_contract_version: Contract version advertised by the worker.

    Returns:
        A finite sampler work ceiling, or ``None`` when the worker proves no ceiling.
    """
    return maximum_sampler_work(
        sampler=request.sampler,
        trajectory_steps=request.trajectory_steps,
        execution_contract_version=execution_contract_version,
        adaptive_work_units_per_iteration=request.adaptive_work_units_per_iteration,
    )


def maximum_request_trajectory_steps_for_work_budget(
    request: SamplerWorkRequest,
    *,
    work_budget: SamplerWorkUnitCount,
) -> TrajectoryStepCount | None:
    """Return the greatest trajectory length that fits AI-Horde's estimated-work budget.

    Args:
        request: Typed sampler and trajectory inputs.
        work_budget: Maximum estimated work allowed by service policy.

    Returns:
        The greatest permitted trajectory length, or ``None`` when step reduction cannot fit the request.
    """
    return maximum_trajectory_steps_for_work_budget(
        sampler=request.sampler,
        requested_trajectory_steps=request.trajectory_steps,
        work_budget=work_budget,
        estimation_policy=AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY,
    )


def parse_sampler_execution_contract_version(advertised_version: object) -> SamplerExecutionContractVersion | None:
    """Parse a known execution contract version, treating missing or future versions as legacy.

    Args:
        advertised_version: Untrusted worker check-in value.

    Returns:
        A known SDK contract version, or ``None`` when the server cannot prove conformance.
    """
    if not isinstance(advertised_version, str):
        return None

    return next(
        (
            contract_version
            for contract_version in SamplerExecutionContractVersion
            if contract_version.value == advertised_version
        ),
        None,
    )
