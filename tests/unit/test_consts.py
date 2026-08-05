# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from horde.consts import (
    ANNOTATION_DETECTOR_KUDOS_BUCKETS,
    ANNOTATION_KUDOS_BUCKET_HUB,
    ANNOTATION_KUDOS_BUCKET_WEIGHTED,
    ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
    ANNOTATION_KUDOS_DEFAULT_BUCKET,
    EXTENDED_SAMPLERS,
    EXTENDED_SCHEDULERS,
    IMAGE_CONTROL_TYPES,
    KARRAS_FLAG_SCHEDULERS,
    KNOWN_CONTROL_TYPES,
    KNOWN_POST_PROCESSORS,
    KNOWN_SAMPLERS,
    KNOWN_SCHEDULERS,
    KNOWN_UPSCALERS,
    LEGACY_IMAGE_CONTROL_TYPES,
    LEGACY_SAMPLERS,
    LEGACY_SCHEDULERS,
    SIGMA_GENERATOR_SCHEDULERS,
    SOLVER_KNOB_SAMPLERS,
    annotation_detector_kudos_bucket,
    scheduler_for_request,
)


class TestKnownPostProcessors:
    def test_values_are_numeric(self):
        for name, value in KNOWN_POST_PROCESSORS.items():
            assert isinstance(value, (int, float)), f"{name} has non-numeric value: {value}"

    def test_not_empty(self):
        assert len(KNOWN_POST_PROCESSORS) > 0


class TestKnownSamplers:
    def test_not_empty(self):
        assert len(KNOWN_SAMPLERS) > 0

    def test_known_samplers_is_the_union_of_every_tier(self):
        assert KNOWN_SAMPLERS == LEGACY_SAMPLERS | EXTENDED_SAMPLERS | SOLVER_KNOB_SAMPLERS

    def test_tiers_do_not_overlap(self):
        # A sampler in two tiers would be gated at the wrong bridge version: either stranded from
        # workers that already render it, or dispatched to workers that cannot.
        assert not (LEGACY_SAMPLERS & EXTENDED_SAMPLERS)
        assert not (LEGACY_SAMPLERS & SOLVER_KNOB_SAMPLERS)
        assert not (EXTENDED_SAMPLERS & SOLVER_KNOB_SAMPLERS)

    def test_no_device_variants_are_accepted(self):
        # The `_gpu` spellings differ only in which device draws the noise, which is the worker's choice.
        for sampler in KNOWN_SAMPLERS:
            assert not sampler.endswith("_gpu"), f"'{sampler}' is a device variant and should not be requestable"

    def test_classic_samplers_stay_legacy(self):
        # These must never move tiers: old bridges render them and gating them would strand requests.
        for sampler in ("k_euler", "k_euler_a", "k_dpmpp_2m", "DDIM", "lcm"):
            assert sampler in LEGACY_SAMPLERS

    def test_unipc_is_extended_despite_backend_support(self):
        # The backend has always rendered these; only the accepted set kept them unreachable, so they
        # gate as extended rather than legacy because old bridges were never told about them.
        assert "uni_pc" in EXTENDED_SAMPLERS
        assert "uni_pc_bh2" in EXTENDED_SAMPLERS

    def test_extended_samplers_carry_backend_spelling(self):
        # Ruling: backend-native solvers keep their own names rather than taking the k_ prefix, which
        # denotes k-diffusion lineage. uni_pc* predate the ruling and are already unprefixed.
        for sampler in EXTENDED_SAMPLERS | SOLVER_KNOB_SAMPLERS:
            assert not sampler.startswith("k_"), f"Backend-native sampler '{sampler}' should not carry the k_ prefix"


class TestKnownSchedulers:
    def test_known_schedulers_is_the_union_of_every_tier(self):
        assert KNOWN_SCHEDULERS == LEGACY_SCHEDULERS | EXTENDED_SCHEDULERS | SIGMA_GENERATOR_SCHEDULERS

    def test_tiers_do_not_overlap(self):
        assert not (LEGACY_SCHEDULERS & EXTENDED_SCHEDULERS)
        assert not (LEGACY_SCHEDULERS & SIGMA_GENERATOR_SCHEDULERS)
        assert not (EXTENDED_SCHEDULERS & SIGMA_GENERATOR_SCHEDULERS)

    def test_legacy_tier_is_exactly_what_the_karras_flag_can_express(self):
        # The flag is a boolean, so it can name two schedules and no more. Anything else needs the field,
        # which is what makes the extended tier the gated one.
        assert LEGACY_SCHEDULERS == set(KARRAS_FLAG_SCHEDULERS.values())

    def test_flag_mapping_is_unchanged(self):
        # Ruled: the boolean keeps its existing meaning so no in-flight request changes output.
        assert KARRAS_FLAG_SCHEDULERS[True] == "karras"
        assert KARRAS_FLAG_SCHEDULERS[False] == "normal"


class TestSchedulerForRequest:
    def test_field_wins_over_the_flag(self):
        assert scheduler_for_request("beta", karras=True) == "beta"
        assert scheduler_for_request("sgm_uniform", karras=False) == "sgm_uniform"

    def test_absent_field_falls_back_to_the_flag(self):
        assert scheduler_for_request(None, karras=True) == "karras"
        assert scheduler_for_request(None, karras=False) == "normal"

    def test_unknown_field_falls_back_to_the_flag(self):
        # Request-time validation rejects a bad value; this is also read for already-stored payloads,
        # so it must degrade rather than raise.
        assert scheduler_for_request("not_a_schedule", karras=True) == "karras"
        assert scheduler_for_request("not_a_schedule", karras=False) == "normal"

    def test_every_known_schedule_survives_resolution(self):
        for schedule in KNOWN_SCHEDULERS:
            assert scheduler_for_request(schedule, karras=False) == schedule, schedule


class TestKnownUpscalers:
    def test_upscalers_are_post_processors(self):
        for upscaler in KNOWN_UPSCALERS:
            assert upscaler in KNOWN_POST_PROCESSORS, f"Upscaler '{upscaler}' not in KNOWN_POST_PROCESSORS"


class TestImageControlTypes:
    def test_image_control_types_derive_from_unified_list(self):
        # The image-generation enum is the unified control set plus the legacy `hough` alias.
        assert IMAGE_CONTROL_TYPES == [*KNOWN_CONTROL_TYPES, "hough"]

    def test_legacy_alias_hough_is_accepted(self):
        # Existing clients still send `hough` for the detector the unified list spells `mlsd`.
        assert "hough" in IMAGE_CONTROL_TYPES
        assert "mlsd" in IMAGE_CONTROL_TYPES

    def test_new_types_are_accepted_by_image_enum(self):
        for new_type in ("lineart", "teed", "depth_anything_v2"):
            assert new_type in IMAGE_CONTROL_TYPES

    def test_legacy_set_is_the_classic_nine(self):
        assert LEGACY_IMAGE_CONTROL_TYPES == [
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

    def test_legacy_types_are_all_dispatchable(self):
        # Every legacy type except the `hough` alias is present in the unified list verbatim.
        for legacy_type in LEGACY_IMAGE_CONTROL_TYPES:
            assert legacy_type in IMAGE_CONTROL_TYPES

    def test_mlsd_is_an_extended_type(self):
        # `mlsd` uses a spelling old workers do not understand, so it gates as a new type.
        assert "mlsd" not in LEGACY_IMAGE_CONTROL_TYPES


class TestAnnotationKudosBuckets:
    _VALID_BUCKETS = {
        ANNOTATION_KUDOS_BUCKET_WEIGHTLESS,
        ANNOTATION_KUDOS_BUCKET_WEIGHTED,
        ANNOTATION_KUDOS_BUCKET_HUB,
    }

    def test_every_known_control_type_has_an_explicit_bucket(self):
        # Lockstep guard: a new control type added to KNOWN_CONTROL_TYPES must be assigned a
        # detector cost class explicitly. Silent defaulting is only for unlisted/novel types.
        missing = [ct for ct in KNOWN_CONTROL_TYPES if ct not in ANNOTATION_DETECTOR_KUDOS_BUCKETS]
        assert not missing, f"KNOWN_CONTROL_TYPES with no explicit kudos bucket: {missing}"

    def test_bucket_map_has_no_unknown_control_types(self):
        # The mapping should not price detectors the server does not actually advertise.
        extra = [ct for ct in ANNOTATION_DETECTOR_KUDOS_BUCKETS if ct not in KNOWN_CONTROL_TYPES]
        assert not extra, f"Bucket map references non-KNOWN_CONTROL_TYPES: {extra}"

    def test_all_buckets_are_one_of_the_three_classes(self):
        for control_type, bucket in ANNOTATION_DETECTOR_KUDOS_BUCKETS.items():
            assert bucket in self._VALID_BUCKETS, f"{control_type} has out-of-class bucket {bucket}"

    def test_three_classes_are_strictly_ordered(self):
        assert ANNOTATION_KUDOS_BUCKET_WEIGHTLESS < ANNOTATION_KUDOS_BUCKET_WEIGHTED < ANNOTATION_KUDOS_BUCKET_HUB

    def test_unknown_type_falls_back_to_middle_bucket(self):
        assert annotation_detector_kudos_bucket("not_a_real_detector_xyz") == ANNOTATION_KUDOS_DEFAULT_BUCKET
        assert ANNOTATION_KUDOS_DEFAULT_BUCKET == ANNOTATION_KUDOS_BUCKET_WEIGHTED

    def test_missing_type_falls_back_to_middle_bucket(self):
        # A None control_type (no payload) must not raise and lands on the default.
        assert annotation_detector_kudos_bucket(None) == ANNOTATION_KUDOS_DEFAULT_BUCKET

    def test_helper_agrees_with_mapping_for_known_types(self):
        for control_type, bucket in ANNOTATION_DETECTOR_KUDOS_BUCKETS.items():
            assert annotation_detector_kudos_bucket(control_type) == bucket

    def test_hub_detector_prices_above_weightless_for_same_tiles(self):
        # Pricing uses the bucket: an oneformer (HUB) job pays more than a canny (WEIGHTLESS) job
        # for the same number of image tiles.
        image_tiles = 4
        canny_kudos = image_tiles * annotation_detector_kudos_bucket("canny")
        oneformer_kudos = image_tiles * annotation_detector_kudos_bucket("oneformer_ade20k")
        assert oneformer_kudos > canny_kudos

    def test_representative_class_members(self):
        assert annotation_detector_kudos_bucket("canny") == ANNOTATION_KUDOS_BUCKET_WEIGHTLESS
        assert annotation_detector_kudos_bucket("openpose") == ANNOTATION_KUDOS_BUCKET_WEIGHTED
        assert annotation_detector_kudos_bucket("depth_anything_v2") == ANNOTATION_KUDOS_BUCKET_HUB
