# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The two authorities a baseline-dependent request is checked against, and the policy read for it.

The served catalog states which weights and mechanisms exist for a family; `bridge_reference` states
which bridge releases drive them. A feature needs both, so each is exercised on its own here.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import pytest
from horde_model_reference import BaselineCapabilities, HordeBaselinePolicy, ImageBaselineRecord
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde.baseline_policy import PAR_HORDE_POLICY, baseline_violation, kudos_multiplier, policy
from horde.enums import BaselineFeature
from tests.unit.model_reference_seed import seed_baseline_catalog

pytestmark = pytest.mark.unit

# A baseline no vocabulary carries, standing in for one the model reference publishes ahead of this
# deployment.
UNCATALOGUED_BASELINE = "some_baseline_published_upstream"

SD1 = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1
SD2_768 = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768
SD2_512 = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512
SDXL = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl
CASCADE = KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade
FLUX_1 = KNOWN_IMAGE_GENERATION_BASELINE.flux_1
QWEN = KNOWN_IMAGE_GENERATION_BASELINE.qwen_image
Z_IMAGE = KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo
KREA2 = KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo
ANIMA = KNOWN_IMAGE_GENERATION_BASELINE.anima


@pytest.fixture(autouse=True)
def _pinned_baseline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Judge every case against the packaged catalog rather than against what PRIMARY serves now."""
    seed_baseline_catalog(monkeypatch)


def rejection_code(baselines: Collection[KNOWN_IMAGE_GENERATION_BASELINE | str], **kwargs: Any) -> str | None:
    """Return the code the policy rejects these baselines with, or None where it accepts them."""
    violation = baseline_violation(baselines, **kwargs)
    return violation[0] if violation is not None else None


def seed_one_baseline(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    capabilities: BaselineCapabilities,
    horde_policy: HordeBaselinePolicy | None = None,
) -> None:
    """Pin the catalog to a single record, for a capability the packaged catalog states of nothing."""
    from horde import model_reference as model_reference_module

    record = ImageBaselineRecord(
        name=name,
        capabilities=capabilities,
        horde_policy=horde_policy or HordeBaselinePolicy(),
    )
    monkeypatch.setattr(
        model_reference_module.model_reference,
        "baseline_record",
        lambda baseline: record if str(baseline) == name else None,
    )


class TestCatalogCapabilities:
    """K1: what the served record says exists for the family."""

    def test_a_family_with_no_control_weights_is_rejected(self) -> None:
        for baseline in (CASCADE, QWEN, Z_IMAGE, KREA2, ANIMA):
            assert rejection_code({baseline}, params={"control_type": "canny"}) == "ControlNetMismatch", baseline

    def test_a_control_type_with_no_published_weights_is_rejected(self) -> None:
        for baseline in (SD2_768, SD2_512):
            assert rejection_code({baseline}, params={"control_type": "mlsd"}) == "ControlNetUnsupported", baseline

    def test_a_control_type_the_family_does_have_weights_for_is_accepted(self) -> None:
        assert rejection_code({SD2_768}, params={"control_type": "canny"}) is None

    def test_the_unavailable_control_type_is_reported_before_the_missing_control_graph(self) -> None:
        # A request tripping both is refused for the more specific one, as it was before the catalog.
        assert rejection_code({SD2_768, CASCADE}, params={"control_type": "mlsd"}) == "ControlNetUnsupported"

    def test_a_family_with_no_layer_diffusion_weights_is_rejected(self) -> None:
        for baseline in (SD2_768, CASCADE, QWEN):
            assert rejection_code({baseline}, params={"transparent": True}) == "InvalidTransparencyModel", baseline

    def test_a_family_with_no_qr_code_weights_is_rejected(self) -> None:
        for baseline in (SD2_768, CASCADE, FLUX_1, QWEN, Z_IMAGE, KREA2, ANIMA):
            # The trailing full stop is part of the code clients already match on.
            assert rejection_code({baseline}, params={"workflow": "qr_code"}) == "ControlNetMismatch", baseline

    def test_the_families_with_qr_code_weights_are_accepted(self) -> None:
        assert rejection_code({SD1, SDXL}, params={"workflow": "qr_code"}) is None

    def test_only_a_family_with_a_remix_mechanism_accepts_remix(self) -> None:
        assert rejection_code({CASCADE}, params={}, source_processing="remix") is None
        for baseline in (SD1, SDXL, FLUX_1):
            assert rejection_code({baseline}, params={}, source_processing="remix") == "InvalidRemixModel", baseline

    def test_a_multi_model_request_needs_every_baseline_to_remix(self) -> None:
        assert rejection_code({CASCADE, SD1}, params={}, source_processing="remix") == "InvalidRemixModel"

    def test_a_family_flow_matching_is_meaningless_for_is_rejected(self) -> None:
        for baseline in (SD1, SDXL, Z_IMAGE):
            assert rejection_code({baseline}, params={"flow_shift": 1.1}) == "FlowShiftInapplicable", baseline

    def test_an_unset_flow_shift_is_never_a_violation(self) -> None:
        assert rejection_code({SD1}, params={"flow_shift": None}) is None


class TestBridgeSupport:
    """K2: whether any bridge release renders the feature on the family."""

    def test_a_family_no_release_renders_hires_fix_on_is_rejected(self) -> None:
        for baseline in (FLUX_1, QWEN, Z_IMAGE):
            assert rejection_code({baseline}, params={"hires_fix": True}) == "HiResMismatch", baseline

    def test_the_families_a_release_renders_hires_fix_on_are_accepted(self) -> None:
        assert rejection_code({SD1, SDXL, CASCADE}, params={"hires_fix": True}) is None

    def test_a_family_no_release_drives_control_weights_on_is_rejected(self) -> None:
        # SDXL has published control weights but no bridge release that runs them.
        assert rejection_code({SDXL}, params={"control_type": "canny"}) == "ControlNetMismatch"

    def test_a_family_no_release_renders_transparency_on_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_one_baseline(monkeypatch, UNCATALOGUED_BASELINE, BaselineCapabilities(transparent=True))
        assert rejection_code({UNCATALOGUED_BASELINE}, params={"transparent": True}) == "InvalidTransparencyModel"

    def test_a_family_no_release_applies_a_timestep_shift_on_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seed_one_baseline(monkeypatch, UNCATALOGUED_BASELINE, BaselineCapabilities(flow_matching=True))
        assert rejection_code({UNCATALOGUED_BASELINE}, params={"flow_shift": 1.1}) == "FlowShiftInapplicable"

    def test_the_flow_matching_families_a_release_shifts_are_accepted(self) -> None:
        for baseline in (
            FLUX_1,
            KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell,
            KNOWN_IMAGE_GENERATION_BASELINE.flux_dev,
            QWEN,
        ):
            assert rejection_code({baseline}, params={"flow_shift": 1.1}) is None, baseline


class TestUncataloguedBaseline:
    def test_a_plain_request_is_accepted(self) -> None:
        assert rejection_code({UNCATALOGUED_BASELINE}, params={"steps": 30, "cfg_scale": 7.5}) is None

    def test_workflows_without_bridge_gates_are_conservatively_refused(self) -> None:
        assert rejection_code({UNCATALOGUED_BASELINE}, params={"workflow": "qr_code"}) == "ControlNetMismatch"
        assert rejection_code({UNCATALOGUED_BASELINE}, params={}, source_processing="remix") == "InvalidRemixModel"

    @pytest.mark.parametrize(
        ("params", "expected_rc"),
        [
            ({"hires_fix": True}, "HiResMismatch"),
            ({"control_type": "canny"}, "ControlNetMismatch"),
            ({"transparent": True}, "InvalidTransparencyModel"),
            ({"flow_shift": 1.1}, "FlowShiftInapplicable"),
        ],
    )
    def test_the_features_no_release_renders_on_it_are_refused(self, params: dict[str, Any], expected_rc: str) -> None:
        assert rejection_code({UNCATALOGUED_BASELINE}, params=params) == expected_rc


class TestFeatureSelection:
    def test_only_the_features_the_caller_owns_are_evaluated(self) -> None:
        params = {"hires_fix": True, "control_type": "canny"}
        assert rejection_code({FLUX_1}, params=params, features=[BaselineFeature.HIRES_FIX]) == "HiResMismatch"
        assert rejection_code({FLUX_1}, params=params, features=[BaselineFeature.CONTROL_TYPE]) == "ControlNetMismatch"


class TestServicePolicy:
    @pytest.mark.parametrize(
        ("baseline", "hires_fix", "qr_code", "expected"),
        [
            (SD1, False, False, 1),
            (SD1, True, True, 1),
            (SDXL, False, False, 2),
            (SDXL, True, False, 2),
            (SDXL, False, True, 4),
            (CASCADE, False, False, 4),
            (CASCADE, True, False, 7),
            (CASCADE, False, True, 4),
            (FLUX_1, True, True, 8),
            (QWEN, False, False, 12),
            (UNCATALOGUED_BASELINE, False, False, 1),
            (UNCATALOGUED_BASELINE, True, True, 1),
        ],
    )
    def test_the_kudos_factor_follows_the_workflow_the_request_asked_for(
        self,
        baseline: KNOWN_IMAGE_GENERATION_BASELINE | str,
        hires_fix: bool,
        qr_code: bool,
        expected: float,
    ) -> None:
        assert kudos_multiplier(baseline, hires_fix=hires_fix, qr_code=qr_code) == expected

    @pytest.mark.parametrize(
        ("baseline", "batching", "ttl", "resolution_floor"),
        [
            (SD1, 1, 1, 0),
            (SD2_768, 1, 1, 768),
            (SDXL, 1, 1, 1024),
            (FLUX_1, 5, 3, 1024),
            (QWEN, 10, 3, 1024),
            (Z_IMAGE, 8, 3, 1024),
            (KREA2, 8, 3, 1024),
            (ANIMA, 8, 3, 1024),
        ],
    )
    def test_each_family_is_scheduled_as_its_record_states(
        self,
        baseline: KNOWN_IMAGE_GENERATION_BASELINE | str,
        batching: int,
        ttl: int,
        resolution_floor: int,
    ) -> None:
        baseline_policy = policy(baseline)
        assert (baseline_policy.batching, baseline_policy.ttl, baseline_policy.resolution_floor) == (
            batching,
            ttl,
            resolution_floor,
        )

    def test_an_uncatalogued_baseline_is_scheduled_and_priced_at_par(self) -> None:
        assert policy(UNCATALOGUED_BASELINE) is PAR_HORDE_POLICY
        assert policy(None) is PAR_HORDE_POLICY
        assert (PAR_HORDE_POLICY.kudos, PAR_HORDE_POLICY.batching, PAR_HORDE_POLICY.ttl) == (1, 1, 1)
        assert PAR_HORDE_POLICY.resolution_floor == 0
