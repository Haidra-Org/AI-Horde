# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Publishes the sampler constraints this API validates against, for clients that cannot import them.

The rules themselves live in horde_sdk, which reads them from the image backend's own solver
implementations. Python clients import that package directly; everything else needs them over the wire,
which is what this module renders. The shape deliberately mirrors the validation: the `hard` sections
are the rejections, so a client that honours them cannot construct a request this API refuses.

The document's type also lives in horde_sdk, as
:class:`~horde_sdk.generation_parameters.image.constraints_document.SamplerConstraintsDocument`, so a
python client parses the response back into the same models this module builds it from.

The public entry point is :func:`compile_sampler_constraints`.
"""

from __future__ import annotations

from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_sdk.generation_parameters.image.constraints import (
    CFG_PP_SAMPLERS,
    MEASURED_COST_RATIO_PROVENANCE,
    MEASURED_COST_RATIO_SOURCE,
    RECOMMENDED_SAMPLERS,
    REJECTED_SAMPLER_SCHEDULER_PAIRINGS,
    SAMPLER_CONSTRAINTS,
    SAMPLER_PRESENTATION_TIERS,
    SAMPLER_RECOMMENDATIONS,
    SCHEDULER_BASELINE_APPLICABILITY,
    NumericKnobRange,
    SamplerConstraints,
    SamplerRecommendation,
)
from horde_sdk.generation_parameters.image.constraints_document import (
    PublishedAdvisories,
    PublishedCostBasis,
    PublishedHardConstraints,
    PublishedKnobRange,
    PublishedPresentationTiers,
    PublishedRecommendation,
    PublishedRejectedPairing,
    PublishedSamplerRecord,
    SamplerConstraintsDocument,
)
from horde_sdk.generation_parameters.image.consts import KNOWN_IMAGE_SAMPLERS, KNOWN_IMAGE_SCHEDULERS

from horde.consts import KNOWN_SAMPLERS, KNOWN_SCHEDULERS
from horde.validation import CFG_PP_ADVISED_MAX_CFG_SCALE, SOLVER_KNOB_REQUEST_FIELDS

# JSON has no literal for infinity, and the encoders that emit one produce output strict parsers reject.
# An unbounded maximum, or a default of "no limit", therefore publishes null: for a bound that reads as
# "no upper limit", and for a default as "the solver applies no limit unless you set one".
_JSON_UNBOUNDED = None

_AUTHORITATIVE_NOTE = (
    "How many times a step runs the model, read from the backend's own solver "
    "implementations. This is what a request is priced and time-budgeted on."
)
_MEASURED_COST_RATIO_NOTE = (
    "Wall-clock cost per step relative to k_euler, measured on one card through the "
    "production render pipeline. Each figure is the slope of a fit of render time against "
    "step count, so it excludes the fixed overhead of a render. The two figures corroborate "
    "evaluations_per_step rather than replace it: pricing and time budgeting read "
    "evaluations_per_step, which is counted from the sampler implementations themselves and "
    "holds on any hardware. A sampler that sets its own step count publishes no ratio, "
    "because cost per requested step is not a quantity it has."
)
_MEASURED_COST_RATIO_SDXL_NOTE = (
    "Measured on a stable_diffusion_xl model at 1024x1024. The step is long enough that "
    "per-step host work is negligible, so this figure reflects what the sampler itself "
    "costs, and every sampler lands within a fifth of its evaluations_per_step."
)
_MEASURED_COST_RATIO_SD15_NOTE = (
    "Measured on a stable_diffusion_1 model at 512x512. The step there is short enough that "
    "per-step host work is a visible fraction of it, which biases the one-evaluation "
    "samplers upwards by up to a third. Read the stable_diffusion_xl figure for the "
    "sampler's own cost."
)
_PRESENTATION_TIER_NOTE = (
    "A presentation hint only. Every sampler is accepted, priced and dispatched identically "
    "whatever its tier. Clients may show the recommended tier by default and put the rest "
    "behind an advanced affordance; nothing in the advanced tier is deprecated."
)


def _bounded_or_null(value: float) -> float | None:
    """Return a numeric bound, or None where it is infinite and has no JSON representation."""
    if value == float("inf"):
        return _JSON_UNBOUNDED

    return value


def _serialize_knob_range(knob_range: NumericKnobRange) -> PublishedKnobRange:
    """Return one numeric knob's accepted range in the published shape."""
    return PublishedKnobRange(
        minimum=knob_range.minimum,
        maximum=_bounded_or_null(knob_range.maximum),
        default=_bounded_or_null(knob_range.default),
        integer_only=knob_range.integral,
    )


def _serialize_sampler(
    sampler: KNOWN_IMAGE_SAMPLERS,
    constraints: SamplerConstraints,
) -> PublishedSamplerRecord:
    """Return one sampler's knobs, cost, tier and vocabulary in the published shape."""
    return PublishedSamplerRecord(
        name=sampler,
        evaluations_per_step=constraints.evaluations_per_step,
        measured_cost_ratio_sd15=constraints.measured_cost_ratio_sd15,
        measured_cost_ratio_sdxl=constraints.measured_cost_ratio_sdxl,
        presentation_tier=SAMPLER_PRESENTATION_TIERS[sampler],
        solver_type_choices=list(constraints.solver_type_choices),
        accepted_settings={
            SOLVER_KNOB_REQUEST_FIELDS[knob]: _serialize_knob_range(knob_range)
            for knob, knob_range in sorted(constraints.numeric_knob_ranges.items())
            if knob in SOLVER_KNOB_REQUEST_FIELDS
        },
        applies_cfg_pp=sampler in CFG_PP_SAMPLERS,
    )


def _serialize_recommendation(recommendation: SamplerRecommendation) -> PublishedRecommendation:
    """Return one advisory statement with the provenance that qualifies it."""
    return PublishedRecommendation(
        samplers=list(recommendation.samplers),
        schedulers=list(recommendation.schedulers),
        provenance=recommendation.provenance,
        source=recommendation.source,
        summary=recommendation.summary,
    )


def _serialize_hard_constraints() -> PublishedHardConstraints:
    """Return the sections that mirror this API's rejections exactly."""
    rejected_pairings = [
        PublishedRejectedPairing(sampler=sampler, scheduler=scheduler)
        for sampler, scheduler in sorted(REJECTED_SAMPLER_SCHEDULER_PAIRINGS)
        if str(sampler) in KNOWN_SAMPLERS and str(scheduler) in KNOWN_SCHEDULERS
    ]

    scheduler_baselines: dict[KNOWN_IMAGE_SCHEDULERS, list[KNOWN_IMAGE_GENERATION_BASELINE]] = {
        scheduler: sorted(baselines)
        for scheduler, baselines in SCHEDULER_BASELINE_APPLICABILITY.items()
        if str(scheduler) in KNOWN_SCHEDULERS
    }

    return PublishedHardConstraints(
        rejected_sampler_scheduler_pairings=rejected_pairings,
        scheduler_baseline_applicability=scheduler_baselines,
    )


def _serialize_cost_basis() -> PublishedCostBasis:
    """Return what each published cost figure is, and which of them a request is charged against."""
    return PublishedCostBasis(
        authoritative_note=_AUTHORITATIVE_NOTE,
        measured_cost_ratio_provenance=MEASURED_COST_RATIO_PROVENANCE,
        measured_cost_ratio_source=MEASURED_COST_RATIO_SOURCE,
        measured_cost_ratio_note=_MEASURED_COST_RATIO_NOTE,
        measured_cost_ratio_sdxl_note=_MEASURED_COST_RATIO_SDXL_NOTE,
        measured_cost_ratio_sd15_note=_MEASURED_COST_RATIO_SD15_NOTE,
    )


def compile_sampler_constraints() -> SamplerConstraintsDocument:
    """Return the full constraints document served by the sampler constraints endpoint.

    Only samplers and schedulers this API actually accepts are listed, so a client reading this never
    offers a name the request models would reject as unknown.

    Returns:
        The typed document, which the endpoint serialises at the HTTP boundary.

    """
    samplers = {
        sampler: _serialize_sampler(sampler, constraints)
        for sampler, constraints in SAMPLER_CONSTRAINTS.items()
        if str(sampler) in KNOWN_SAMPLERS
    }

    return SamplerConstraintsDocument(
        samplers=samplers,
        hard_constraints=_serialize_hard_constraints(),
        recommendations=[_serialize_recommendation(recommendation) for recommendation in SAMPLER_RECOMMENDATIONS],
        advisories=PublishedAdvisories(cfg_pp_advised_max_cfg_scale=CFG_PP_ADVISED_MAX_CFG_SCALE),
        cost_basis=_serialize_cost_basis(),
        presentation_tiers=PublishedPresentationTiers(
            note=_PRESENTATION_TIER_NOTE,
            recommended=sorted(sampler for sampler in RECOMMENDED_SAMPLERS if str(sampler) in KNOWN_SAMPLERS),
        ),
    )
