# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hold the served baseline catalog and the bridge feature table to the rules they replaced.

The old rules were spread across five modules and several of them turned on string prefixes, so the
only trustworthy statement about what changed is an exhaustive one. Every request shape is evaluated
against both, and a difference is either named here as an adjudicated deviation or fails the run.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any, Final

import pytest
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde import exceptions as e
from horde.baseline_policy import (
    ALL_BASELINE_FEATURES,
    UNCATALOGUED_CAPABILITIES,
    baseline_violation,
    kudos_multiplier,
    policy,
)
from horde.bridge_reference import BRIDGE_BASELINE_FEATURES, bridge_supports
from horde.enums import BaselineFeature
from horde.validation import ParamValidator
from tests.unit.model_reference_seed import BOOTSTRAP_BASELINE_CATALOG, seed_baseline_catalog, seed_image_reference
from tests.unit.parity._legacy_baseline_rules import (
    legacy_batching_multiplier,
    legacy_dry_run_quote_multiplier,
    legacy_first_rejection_rc,
    legacy_gen_kudos_multiplier,
    legacy_rejecting_rules,
    legacy_required_bridge_capability,
    legacy_resolution_floor,
    legacy_ttl_multiplier,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _pinned_baseline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose every expectation from the packaged catalog rather than from what PRIMARY serves now."""
    seed_baseline_catalog(monkeypatch)


# The legacy spelling the old model reference published for each baseline, paired with the value the
# reference publishes for it now. The old expressions only ever saw the left-hand spellings, so the
# underscored forms of the space-spelled baselines are outside the differential's domain.
BASELINE_CASES: Final[tuple[tuple[str, str], ...]] = (
    ("infer", KNOWN_IMAGE_GENERATION_BASELINE.infer.value),
    ("stable diffusion 1", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value),
    ("stable diffusion 2", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value),
    ("stable diffusion 2 512", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512.value),
    ("stable_diffusion_xl", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value),
    ("stable_cascade", KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value),
    ("flux_1", KNOWN_IMAGE_GENERATION_BASELINE.flux_1.value),
    ("flux_schnell", KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value),
    ("flux_dev", KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value),
    ("qwen_image", KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value),
    ("z_image_turbo", KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo.value),
    ("krea2_turbo", KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value),
    ("anima", KNOWN_IMAGE_GENERATION_BASELINE.anima.value),
    ("some_future_baseline", "some_future_baseline"),
    ("krea3_turbo", "krea3_turbo"),
)

CONTROL_TYPE_VALUES: Final[tuple[str | None, ...]] = (None, "canny", "normal", "mlsd", "hough", "depth")
HIRES_FIX_VALUES: Final[tuple[bool | None, ...]] = (None, False, True)
TRANSPARENT_VALUES: Final[tuple[bool | None, ...]] = (None, False, True)
WORKFLOW_VALUES: Final[tuple[str | None, ...]] = (None, "qr_code")
FLOW_SHIFT_VALUES: Final[tuple[float | None, ...]] = (None, 3.0)
SOURCE_PROCESSING_VALUES: Final[tuple[str | None, ...]] = ("img2img", "inpainting", "remix", None)

# The old rule names the oracle reports, paired with the feature that decides the same request today.
LEGACY_RULE_FEATURES: Final[dict[str, BaselineFeature]] = {
    "flow_shift_inapplicable": BaselineFeature.FLOW_SHIFT,
    "hires_fix_unsupported": BaselineFeature.HIRES_FIX,
    "transparency_unsupported": BaselineFeature.TRANSPARENT,
    "qr_code_workflow_unsupported": BaselineFeature.QR_CODE,
    "control_type_unsupported": BaselineFeature.CONTROL_TYPE_UNAVAILABLE,
    "controlnet_unsupported": BaselineFeature.CONTROL_TYPE,
    "remix_unsupported": BaselineFeature.REMIX,
}

# The baselines whose names no old prefix test matched, so the old code let every feature through for
# them. The catalog and the bridge table state what they can render, which is the point of both.
UNMATCHED_BY_OLD_PREFIX_TESTS: Final[frozenset[str]] = frozenset(
    {
        KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value,
        KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value,
        KNOWN_IMAGE_GENERATION_BASELINE.anima.value,
    },
)

# Baselines that have no record or deliberately carry the same conservative capability set. Plain
# requests remain accepted, while feature workflows wait for explicit support.
CONSERVATIVE_BASELINES: Final[frozenset[str]] = frozenset(
    new_value
    for _, new_value in BASELINE_CASES
    if BOOTSTRAP_BASELINE_CATALOG.baselines.get(new_value) is None
    or BOOTSTRAP_BASELINE_CATALOG.baselines[new_value].capabilities == UNCATALOGUED_CAPABILITIES
)

# The baselines whose served policy departs from what the old ladders charged for them.
NEWLY_PRICED_BASELINES: Final[frozenset[str]] = frozenset(
    {
        KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value,
        KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value,
        KNOWN_IMAGE_GENERATION_BASELINE.anima.value,
    },
)

# The rules the old code enforced by naming the baselines that failed, so anything it had not heard of
# was accepted. Engine support is now stated per bridge release, which names no baseline it never ran.
RULES_OLD_ENFORCED_BY_DENYLIST: Final[frozenset[str]] = frozenset(
    {"hires_fix_unsupported", "controlnet_unsupported"},
)

DEVIATION_D4_PREFIX_MISS: Final[str] = "D4 old prefix tests missed this baseline"
DEVIATION_D8_NO_BRIDGE_RENDERS_IT: Final[str] = "D8 no bridge release renders this feature on an uncatalogued baseline"


def _build_params(
    control_type: str | None,
    hires_fix: bool | None,
    transparent: bool | None,
    workflow: str | None,
    flow_shift: float | None,
) -> dict[str, Any]:
    """Create a generation payload where None means the field was left out of the request.

    Presence matters on its own for `control_type`: the old ControlNet rules keyed off the field being
    in the payload rather than off its value.
    """
    params: dict[str, Any] = {}
    if control_type is not None:
        params["control_type"] = control_type
    if hires_fix is not None:
        params["hires_fix"] = hires_fix
    if transparent is not None:
        params["transparent"] = transparent
    if workflow is not None:
        params["workflow"] = workflow
    if flow_shift is not None:
        params["flow_shift"] = flow_shift
    return params


ALL_PARAM_COMBINATIONS: Final[tuple[tuple[int, dict[str, Any]], ...]] = tuple(
    enumerate(
        _build_params(*combination)
        for combination in itertools.product(
            CONTROL_TYPE_VALUES,
            HIRES_FIX_VALUES,
            TRANSPARENT_VALUES,
            WORKFLOW_VALUES,
            FLOW_SHIFT_VALUES,
        )
    ),
)

# Single-model requests and every ordered pair of distinct baselines, because a request naming several
# models is dispatched for whichever one a worker holds.
ALL_BASELINE_SELECTIONS: Final[tuple[tuple[tuple[str, str], ...], ...]] = tuple(
    [(baseline_case,) for baseline_case in BASELINE_CASES] + [pair for pair in itertools.permutations(BASELINE_CASES, 2)],
)


def _new_rejecting_rules(
    baselines: tuple[str, ...],
    params: Mapping[str, Any],
    source_processing: str | None,
) -> tuple[set[str], str | None]:
    """Return the old rule names the current code rejects this request under, and the first code."""
    rejecting = {
        rule_name
        for rule_name, feature in LEGACY_RULE_FEATURES.items()
        if baseline_violation(baselines, params=params, source_processing=source_processing, features=[feature]) is not None
    }
    violation = baseline_violation(baselines, params=params, source_processing=source_processing)
    return rejecting, (violation[0] if violation is not None else None)


def _classify_rule_difference(
    rule_name: str,
    selection: tuple[tuple[str, str], ...],
    params: Mapping[str, Any],
    source_processing: str | None,
    *,
    old_rejects: bool,
) -> str | None:
    """Return the adjudicated deviation a per-rule difference falls under, or None if it is new."""
    if old_rejects:
        return None

    if rule_name not in RULES_OLD_ENFORCED_BY_DENYLIST:
        return None
    offending_new_values = [
        new_value for _, new_value in selection if rule_name in _new_rejecting_rules((new_value,), params, source_processing)[0]
    ]
    if not offending_new_values:
        return None
    if all(new_value in UNMATCHED_BY_OLD_PREFIX_TESTS for new_value in offending_new_values):
        return DEVIATION_D4_PREFIX_MISS
    if all(new_value in UNMATCHED_BY_OLD_PREFIX_TESTS | CONSERVATIVE_BASELINES for new_value in offending_new_values):
        return DEVIATION_D8_NO_BRIDGE_RENDERS_IT
    return None


class TestEnumerationCoverage:
    def test_every_known_baseline_is_enumerated(self) -> None:
        enumerated = {new_value for _, new_value in BASELINE_CASES}
        assert {baseline.value for baseline in KNOWN_IMAGE_GENERATION_BASELINE} <= enumerated

    def test_the_enumeration_is_the_size_it_claims(self) -> None:
        assert len(ALL_PARAM_COMBINATIONS) * len(SOURCE_PROCESSING_VALUES) == 864
        assert len(ALL_BASELINE_SELECTIONS) == len(BASELINE_CASES) ** 2

    def test_every_feature_is_reachable_from_an_old_rule(self) -> None:
        assert set(LEGACY_RULE_FEATURES.values()) == set(ALL_BASELINE_FEATURES)


class TestPolicyRejectionParity:
    def test_every_request_shape_is_rejected_as_the_old_rules_rejected_it(self) -> None:
        unadjudicated: list[str] = []
        for selection in ALL_BASELINE_SELECTIONS:
            legacy_baselines = tuple(legacy for legacy, _ in selection)
            new_baselines = tuple(new_value for _, new_value in selection)
            for source_processing in SOURCE_PROCESSING_VALUES:
                for _, params in ALL_PARAM_COMBINATIONS:
                    old_rules = set(
                        legacy_rejecting_rules(legacy_baselines, params=params, source_processing=source_processing),
                    )
                    new_rules, new_first_rc = _new_rejecting_rules(new_baselines, params, source_processing)
                    old_first_rc = legacy_first_rejection_rc(
                        legacy_baselines,
                        params=params,
                        source_processing=source_processing,
                    )
                    differing_rules = old_rules ^ new_rules
                    for rule_name in sorted(differing_rules):
                        deviation = _classify_rule_difference(
                            rule_name,
                            selection,
                            params,
                            source_processing,
                            old_rejects=rule_name in old_rules,
                        )
                        if deviation is None:
                            unadjudicated.append(
                                f"rule={rule_name} baselines={new_baselines} params={params} "
                                f"source_processing={source_processing} old_rc={old_first_rc} new_rc={new_first_rc}",
                            )
                    if not differing_rules and old_first_rc != new_first_rc:
                        unadjudicated.append(
                            f"order baselines={new_baselines} params={params} "
                            f"source_processing={source_processing} old_rc={old_first_rc} new_rc={new_first_rc}",
                        )
        assert not unadjudicated, "\n".join(unadjudicated[:40])


class TestAdjudicatedRejectionDeviations:
    """Each deviation is asserted outright, so removing one fails here rather than passing silently."""

    @pytest.mark.parametrize("baseline", sorted(UNMATCHED_BY_OLD_PREFIX_TESTS))
    def test_d4_hires_fix_and_controlnet_now_reject_the_baselines_the_prefix_tests_missed(self, baseline: str) -> None:
        params = {"hires_fix": True}
        assert legacy_first_rejection_rc([baseline], params=params) is None
        assert _new_rejecting_rules((baseline,), params, None)[1] == "HiResMismatch"

        params = {"control_type": "canny"}
        assert legacy_first_rejection_rc([baseline], params=params) is None
        assert _new_rejecting_rules((baseline,), params, None)[1] == "ControlNetMismatch"

    @pytest.mark.parametrize("baseline", ["some_future_baseline", KNOWN_IMAGE_GENERATION_BASELINE.infer.value])
    def test_d8_an_uncatalogued_baseline_is_refused_the_features_no_bridge_renders_on_it(self, baseline: str) -> None:
        for params, expected_rc in (
            ({"hires_fix": True}, "HiResMismatch"),
            ({"control_type": "canny"}, "ControlNetMismatch"),
        ):
            assert legacy_first_rejection_rc([baseline], params=params) is None
            assert _new_rejecting_rules((baseline,), params, None)[1] == expected_rc

    @pytest.mark.parametrize("baseline", ["some_future_baseline", KNOWN_IMAGE_GENERATION_BASELINE.infer.value])
    def test_an_uncatalogued_baseline_keeps_being_refused_flow_shift_and_transparency(self, baseline: str) -> None:
        for params, expected_rc in (
            ({"flow_shift": 3.0}, "FlowShiftInapplicable"),
            ({"transparent": True}, "InvalidTransparencyModel"),
        ):
            assert legacy_first_rejection_rc([baseline], params=params) == expected_rc
            assert _new_rejecting_rules((baseline,), params, None)[1] == expected_rc

    def test_d7_the_sd2_control_type_subset_is_parity_rather_than_a_deviation(self) -> None:
        for legacy, new_value in (
            ("stable diffusion 2", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value),
            ("stable diffusion 2 512", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512.value),
        ):
            for control_type in ("normal", "mlsd", "hough"):
                params = {"control_type": control_type}
                assert legacy_first_rejection_rc([legacy], params=params) == "ControlNetUnsupported"
                assert _new_rejecting_rules((new_value,), params, None)[1] == "ControlNetUnsupported"
            for control_type in ("canny", "depth"):
                params = {"control_type": control_type}
                assert legacy_first_rejection_rc([legacy], params=params) is None
                assert _new_rejecting_rules((new_value,), params, None)[1] is None


class TestReturnCodeSurface:
    def test_the_rejections_carry_exactly_the_return_codes_clients_match_on(self) -> None:
        rejecting_requests = {
            "FlowShiftInapplicable": (KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo.value, {"flow_shift": 3.0}, None),
            "HiResMismatch": (KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value, {"hires_fix": True}, None),
            "InvalidTransparencyModel": (
                KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value,
                {"transparent": True},
                None,
            ),
            "ControlNetMismatch.": (KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value, {"workflow": "qr_code"}, None),
            "ControlNetUnsupported": (
                KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value,
                {"control_type": "mlsd"},
                None,
            ),
            "ControlNetMismatch": (
                KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value,
                {"control_type": "canny"},
                None,
            ),
            "InvalidRemix": (KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value, {}, "remix"),
        }
        for expected_rc, (baseline, params, source_processing) in rejecting_requests.items():
            assert _new_rejecting_rules((baseline,), params, source_processing)[1] == expected_rc


class TestKudosLadderParity:
    def test_the_per_generation_ladder_matches_except_for_the_newly_priced_baselines(self) -> None:
        mismatches: list[str] = []
        for legacy, new_value in BASELINE_CASES:
            for hires_fix, qr_code in itertools.product((False, True), repeat=2):
                old = legacy_gen_kudos_multiplier(legacy, hires_fix=hires_fix, qr_code=qr_code)
                new = kudos_multiplier(new_value, hires_fix=hires_fix, qr_code=qr_code)
                if old == new or new_value in NEWLY_PRICED_BASELINES:
                    continue
                mismatches.append(f"{new_value} hires_fix={hires_fix} qr_code={qr_code} old={old} new={new}")
        assert not mismatches, "\n".join(mismatches)

    def test_d4_the_newly_priced_baseline_is_priced_like_z_image(self) -> None:
        krea2 = KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value
        assert legacy_gen_kudos_multiplier("krea2_turbo", hires_fix=False, qr_code=False) == 1
        assert kudos_multiplier(krea2) == kudos_multiplier(KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo.value)
        assert kudos_multiplier(krea2) == 8

    @pytest.mark.parametrize(
        "baseline",
        [KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value, KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value],
    )
    def test_d9_the_flux_siblings_are_priced_and_scheduled_as_flux_1(self, baseline: str) -> None:
        assert legacy_gen_kudos_multiplier(baseline, hires_fix=False, qr_code=False) == 1
        assert legacy_ttl_multiplier(baseline) == 1
        assert legacy_resolution_floor([baseline]) == 0
        assert policy(baseline) == policy(KNOWN_IMAGE_GENERATION_BASELINE.flux_1.value)
        assert kudos_multiplier(baseline) == 8

    def test_d3_the_dry_run_quote_now_follows_the_same_workflow_branches_as_the_charge(self) -> None:
        quote_workflow_cases = {
            (KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value, False, True): (2, 4),
            (KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value, True, False): (4, 7),
        }
        for (new_value, hires_fix, qr_code), (expected_old, expected_new) in quote_workflow_cases.items():
            legacy = new_value
            assert legacy_dry_run_quote_multiplier(legacy) == expected_old
            assert kudos_multiplier(new_value, hires_fix=hires_fix, qr_code=qr_code) == expected_new

    def test_the_dry_run_quote_matches_elsewhere(self) -> None:
        mismatches: list[str] = []
        for legacy, new_value in BASELINE_CASES:
            for hires_fix, qr_code in itertools.product((False, True), repeat=2):
                old = legacy_dry_run_quote_multiplier(legacy)
                new = kudos_multiplier(new_value, hires_fix=hires_fix, qr_code=qr_code)
                if old == new or new_value in NEWLY_PRICED_BASELINES:
                    continue
                is_quote_workflow_deviation = new_value in (
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value,
                ) and (hires_fix or qr_code)
                if is_quote_workflow_deviation:
                    continue
                mismatches.append(f"{new_value} hires_fix={hires_fix} qr_code={qr_code} old={old} new={new}")
        assert not mismatches, "\n".join(mismatches)


class TestLeaseAndBatchingParity:
    def test_the_lease_multiplier_matches_except_for_the_newly_priced_baselines(self) -> None:
        mismatches: list[str] = []
        for legacy, new_value in BASELINE_CASES:
            old = legacy_ttl_multiplier(legacy)
            new = policy(new_value).ttl
            if old != new and new_value not in NEWLY_PRICED_BASELINES:
                mismatches.append(f"{new_value} old={old} new={new}")
        assert not mismatches, "\n".join(mismatches)

    def test_d4_the_newly_priced_baseline_holds_its_lease_like_z_image(self) -> None:
        assert legacy_ttl_multiplier("krea2_turbo") == 1
        assert policy(KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value).ttl == 3

    def test_d1_the_batching_multiplier_was_never_reached_by_a_model_name(self) -> None:
        model_names = ["Flux.1-Schnell fp8 (Compact)", "Qwen Image", "Z-Image Turbo"]
        assert legacy_batching_multiplier(model_names) == 1
        for baseline, expected in (
            (KNOWN_IMAGE_GENERATION_BASELINE.flux_1.value, 5),
            (KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value, 10),
            (KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo.value, 8),
            (KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value, 8),
        ):
            assert policy(baseline).batching == expected

    def test_the_batching_multiplier_of_a_multi_model_request_is_the_heaviest(self) -> None:
        baselines = [
            KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value,
            KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value,
        ]
        assert max(policy(baseline).batching for baseline in baselines) == 10


class TestResolutionFloorParity:
    def test_the_floor_matches_except_where_the_catalog_states_one_the_old_code_did_not(self) -> None:
        gained_floors = {
            KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value: 768,
            KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value: 1024,
            KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value: 1024,
            KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value: 1024,
            KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo.value: 1024,
            KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value: 1024,
            KNOWN_IMAGE_GENERATION_BASELINE.anima.value: 1024,
        }
        mismatches: list[str] = []
        for selection in ALL_BASELINE_SELECTIONS:
            old = legacy_resolution_floor([legacy for legacy, _ in selection])
            new_values = [new_value for _, new_value in selection]
            new = max(policy(new_value).resolution_floor for new_value in new_values)
            if old == new:
                continue
            explained_by_gained_floor = new == max(
                [gained_floors.get(new_value, 0) for new_value in new_values] + [old],
            )
            if explained_by_gained_floor:
                continue
            mismatches.append(f"{new_values} old={old} new={new}")
        assert not mismatches, "\n".join(mismatches)

    def test_d2_the_never_matching_sd2_branch_is_replaced_by_a_stated_floor(self) -> None:
        assert legacy_resolution_floor(["stable diffusion 2"]) == 0
        assert policy(KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value).resolution_floor == 768
        assert policy(KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512.value).resolution_floor == 0

    @pytest.mark.parametrize(
        "baseline",
        [
            KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value,
            KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo.value,
            KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo.value,
        ],
    )
    def test_d2_the_large_architectures_now_claim_the_floor_sdxl_already_had(self, baseline: str) -> None:
        assert legacy_resolution_floor([baseline]) == 0
        assert policy(baseline).resolution_floor == 1024


class TestBridgeFeatureTable:
    def test_the_flux_graph_gate_is_the_only_flat_capability_a_baseline_still_turns_on(self) -> None:
        for legacy, _ in BASELINE_CASES:
            expected_capability = "flux" if legacy == "flux_1" else None
            assert legacy_required_bridge_capability(legacy) == expected_capability

    @pytest.mark.parametrize(
        ("feature", "expected_baselines"),
        [
            (
                BaselineFeature.HIRES_FIX,
                {
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade.value,
                },
            ),
            (
                BaselineFeature.CONTROL_TYPE,
                {
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_512.value,
                },
            ),
            (
                BaselineFeature.TRANSPARENT,
                {
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl.value,
                },
            ),
            (
                BaselineFeature.FLOW_SHIFT,
                {
                    KNOWN_IMAGE_GENERATION_BASELINE.flux_1.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.flux_schnell.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.flux_dev.value,
                    KNOWN_IMAGE_GENERATION_BASELINE.qwen_image.value,
                },
            ),
        ],
    )
    def test_the_baselines_some_bridge_release_renders_a_feature_on(
        self,
        feature: BaselineFeature,
        expected_baselines: set[str],
    ) -> None:
        all_baselines = {new_value for _, new_value in BASELINE_CASES}
        assert {baseline for baseline in all_baselines if bridge_supports(feature, baseline)} == expected_baselines

    def test_a_worker_older_than_the_release_that_gained_a_feature_does_not_render_it(self) -> None:
        sd1 = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value
        assert not bridge_supports(BaselineFeature.TRANSPARENT, sd1, "AI Horde Worker reGen:7.0.0:")
        assert bridge_supports(BaselineFeature.TRANSPARENT, sd1, "AI Horde Worker reGen:8.0.0:")
        assert not bridge_supports(BaselineFeature.HIRES_FIX, sd1, "AI Horde Worker:12.0.0:")
        assert bridge_supports(BaselineFeature.HIRES_FIX, sd1, "AI Horde Worker:13.0.0:")

    def test_a_bridge_kind_that_never_rendered_these_features_supports_none_of_them(self) -> None:
        assert set(BRIDGE_BASELINE_FEATURES) == {"AI Horde Worker reGen", "AI Horde Worker"}
        sd1 = KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1.value
        for feature in (
            BaselineFeature.HIRES_FIX,
            BaselineFeature.CONTROL_TYPE,
            BaselineFeature.TRANSPARENT,
            BaselineFeature.FLOW_SHIFT,
        ):
            assert not bridge_supports(feature, sd1, "HordeAutoWebBridge:2.0.0:")


def _validator_rejection_code(
    monkeypatch: pytest.MonkeyPatch,
    baseline: KNOWN_IMAGE_GENERATION_BASELINE | str,
    params: dict[str, Any],
) -> str | None:
    """Return the code `validate_image_params` rejects a request naming one such model with."""
    seed_image_reference(monkeypatch, {"a_model": baseline})
    validator = ParamValidator(prompt="a prompt", models=["a_model"], params=params, user=None)
    try:
        validator.validate_image_params()
    except e.BadRequest as rejection:
        return rejection.rc
    return None


class TestParamValidatorWiring:
    """The rules `ParamValidator` owns, driven through the validator rather than the pure function."""

    def test_flow_shift_is_refused_for_a_baseline_whose_graph_ignores_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(
            monkeypatch,
            KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo,
            {"flow_shift": 3.0},
        )
        assert code == "FlowShiftInapplicable"

    def test_flow_shift_is_accepted_for_a_flow_matching_baseline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(monkeypatch, KNOWN_IMAGE_GENERATION_BASELINE.flux_1, {"flow_shift": 3.0})
        assert code is None

    def test_hires_fix_is_refused_for_a_single_pass_baseline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(monkeypatch, KNOWN_IMAGE_GENERATION_BASELINE.qwen_image, {"hires_fix": True})
        assert code == "HiResMismatch"

    def test_hires_fix_is_accepted_on_sd1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(
            monkeypatch,
            KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1,
            {"hires_fix": True},
        )
        assert code is None

    def test_transparency_is_refused_for_a_baseline_that_cannot_render_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(
            monkeypatch,
            KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade,
            {"transparent": True},
        )
        assert code == "InvalidTransparencyModel"

    def test_transparency_is_accepted_on_sdxl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(
            monkeypatch,
            KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl,
            {"transparent": True},
        )
        assert code is None

    def test_the_qr_code_workflow_is_refused_for_a_baseline_without_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(
            monkeypatch,
            KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade,
            {"workflow": "qr_code"},
        )
        assert code == "ControlNetMismatch."

    def test_the_qr_code_workflow_is_accepted_on_sd1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code = _validator_rejection_code(
            monkeypatch,
            KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1,
            {"workflow": "qr_code"},
        )
        assert code is None

    def test_an_uncatalogued_baseline_is_refused_a_feature_no_bridge_renders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A record keeps a baseline the installed vocabulary lacks, which no bridge release names."""
        code = _validator_rejection_code(monkeypatch, "some_future_baseline", {"hires_fix": True})
        assert code == "HiResMismatch"

    def test_an_uncatalogued_baseline_reaches_the_validator_with_conservative_workflows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code = _validator_rejection_code(monkeypatch, "some_future_baseline", {"workflow": "qr_code"})
        assert code == "ControlNetMismatch."
