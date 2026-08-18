"""AI-Horde policy tests over the SDK's unit-safe sampler work contract."""

from __future__ import annotations

import pytest
from horde_sdk.generation_parameters.image.sampler_work import (
    SamplerExecutionContractVersion,
    SamplerWorkUnitCount,
    TrajectoryStepCount,
)

from horde.sampler_work_policy import (
    estimate_request_sampler_work,
    maximum_request_sampler_work,
    maximum_request_trajectory_steps_for_work_budget,
    parse_sampler_execution_contract_version,
    sampler_work_request_from_payload,
)

pytestmark = pytest.mark.unit


def test_unknown_legacy_sampler_uses_first_order_compatibility_profile() -> None:
    request = sampler_work_request_from_payload({"sampler_name": "retired_sampler", "steps": 20})
    assert estimate_request_sampler_work(request).work_units == SamplerWorkUnitCount(20)


def test_adaptive_trajectory_and_estimate_remain_distinct() -> None:
    request = sampler_work_request_from_payload({"sampler_name": "k_dpm_adaptive", "steps": 5})
    assert request.trajectory_steps == TrajectoryStepCount(5)
    assert estimate_request_sampler_work(request).work_units == SamplerWorkUnitCount(40)


def test_adaptive_ceiling_requires_an_explicit_known_execution_contract() -> None:
    request = sampler_work_request_from_payload({"sampler_name": "k_dpm_adaptive", "steps": 20})
    assert maximum_request_sampler_work(request, execution_contract_version=None) is None
    assert parse_sampler_execution_contract_version("future_v9") is None
    assert parse_sampler_execution_contract_version(42) is None


@pytest.mark.parametrize(("order", "expected"), [(2, 50), (3, 75)])
def test_adaptive_ceiling_uses_the_requested_solver_order(order: int, expected: int) -> None:
    request = sampler_work_request_from_payload(
        {"sampler_name": "k_dpm_adaptive", "steps": 20, "sampler_order": order},
    )
    ceiling = maximum_request_sampler_work(
        request,
        execution_contract_version=SamplerExecutionContractVersion.V1,
    )
    assert ceiling is not None
    assert ceiling.work_units == SamplerWorkUnitCount(expected)


def test_second_order_budget_inversion_is_direct() -> None:
    request = sampler_work_request_from_payload({"sampler_name": "k_heun", "steps": 30})
    assert maximum_request_trajectory_steps_for_work_budget(
        request,
        work_budget=SamplerWorkUnitCount(40),
    ) == TrajectoryStepCount(20)


def test_adaptive_over_budget_request_has_no_step_only_downgrade() -> None:
    request = sampler_work_request_from_payload({"sampler_name": "k_dpm_adaptive", "steps": 30})
    assert (
        maximum_request_trajectory_steps_for_work_budget(
            request,
            work_budget=SamplerWorkUnitCount(20),
        )
        is None
    )
