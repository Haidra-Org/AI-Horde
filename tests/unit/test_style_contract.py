# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validation tests for the parameter policy and template fields a style may declare.

Pure validation: nothing here touches the database or the Flask app, so the rules are covered
independently of the endpoints that enforce them. Both style types are covered, because the
vocabulary a policy is validated against differs between them.
"""

from typing import Any

import pytest

from horde import exceptions as e
from horde.classes.base.style_contract import (
    MAX_TEMPLATE_FIELDS,
    TEMPLATE_FIELD_DESCRIPTION_MAX_LENGTH,
    TEMPLATE_FIELD_NAME_MAX_LENGTH,
    ParameterCeilingBound,
    StyleContractVocabulary,
    StyleParameterOverride,
    StyleParameterPolicy,
    build_vocabulary,
    load_parameter_policy,
    load_template_fields,
    parse_parameter_policy,
    parse_template_fields,
    serialize_parameter_policy,
    serialize_template_fields,
)

TEXT_VOCABULARY = build_vocabulary(
    parameter_names={"max_length", "max_context_length", "temperature", "top_p", "n"},
    ceiling_bounds={
        "max_length": ParameterCeilingBound(minimum=16, maximum=4096),
        "max_context_length": ParameterCeilingBound(minimum=80, maximum=1_048_576),
        "n": ParameterCeilingBound(minimum=1, maximum=20),
    },
)

IMAGE_VOCABULARY = build_vocabulary(
    parameter_names={"width", "height", "steps", "cfg_scale", "sampler_name", "n"},
    ceiling_bounds={
        "width": ParameterCeilingBound(minimum=64, maximum=3072),
        "height": ParameterCeilingBound(minimum=64, maximum=3072),
        "steps": ParameterCeilingBound(minimum=1, maximum=500),
        "n": ParameterCeilingBound(minimum=1, maximum=20),
    },
)


def parse_policy(raw_policy: Any, vocabulary: StyleContractVocabulary = TEXT_VOCABULARY) -> StyleParameterPolicy:
    """Parse a policy against one of the vocabularies defined in this module.

    Args:
        raw_policy: The policy object as an endpoint would receive it.
        vocabulary: The style type to validate against, text by default.

    Returns:
        The validated policy.
    """
    return parse_parameter_policy(raw_policy, vocabulary=vocabulary)


class TestVocabulary:
    """What each style type lets a policy talk about."""

    def test_the_request_count_is_never_overridable(self):
        assert "n" not in TEXT_VOCABULARY.overridable_parameter_names
        assert "n" not in IMAGE_VOCABULARY.overridable_parameter_names

    def test_each_type_caps_its_own_parameters(self):
        assert set(TEXT_VOCABULARY.ceiling_bounds) == {"max_length", "max_context_length", "n"}
        assert set(IMAGE_VOCABULARY.ceiling_bounds) == {"width", "height", "steps", "n"}


class TestParameterPolicyOverride:
    """The override mode a policy declares."""

    def test_absent_override_is_none(self):
        assert parse_policy({}).override is StyleParameterOverride.NONE

    @pytest.mark.parametrize("mode", [mode.value for mode in StyleParameterOverride])
    def test_every_declared_mode_is_accepted(self, mode: str):
        # Only 'listed' reads a list, and it requires a non-empty one.
        raw_policy: dict[str, Any] = {"override": mode}
        if mode == StyleParameterOverride.LISTED.value:
            raw_policy["overridable"] = ["max_length"]
        assert parse_policy(raw_policy).override.value == mode

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "sometimes"})

    def test_non_string_mode_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": 1})

    def test_unknown_policy_key_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "all", "max_length_ceiling": 512})

    def test_non_object_policy_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy(["listed"])


class TestParameterPolicyOverridable:
    """The list of params a policy lets a request set."""

    def test_listed_parameters_are_kept_in_order(self):
        policy = parse_policy({"override": "listed", "overridable": ["max_context_length", "max_length"]})
        assert policy.overridable == ("max_context_length", "max_length")

    def test_repeated_parameters_are_collapsed(self):
        policy = parse_policy({"override": "listed", "overridable": ["max_length", "max_length"]})
        assert policy.overridable == ("max_length",)

    def test_an_empty_list_under_listed_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "overridable": []})

    def test_a_missing_list_under_listed_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "ceilings": {"max_length": 512}})

    def test_absent_list_stays_absent(self):
        assert parse_policy({"override": "all"}).overridable is None

    @pytest.mark.parametrize("mode", ["none", "all"])
    def test_list_under_another_mode_is_misplaced(self, mode: str):
        with pytest.raises(e.BadRequest) as raised:
            parse_policy({"override": mode, "overridable": ["max_length"]})
        assert raised.value.rc == "StylePolicyOverridableMisplaced"

    def test_list_under_another_mode_is_misplaced_before_its_contents_are_read(self):
        with pytest.raises(e.BadRequest) as raised:
            parse_policy({"override": "all", "overridable": "not even a list"})
        assert raised.value.rc == "StylePolicyOverridableMisplaced"

    def test_unknown_parameter_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "overridable": ["max_tokens"]})

    def test_a_parameter_of_the_other_style_type_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "overridable": ["max_length"]}, IMAGE_VOCABULARY)

    def test_an_image_parameter_is_accepted_against_the_image_vocabulary(self):
        policy = parse_policy({"override": "listed", "overridable": ["width", "height"]}, IMAGE_VOCABULARY)
        assert policy.overridable == ("width", "height")

    def test_the_request_count_cannot_be_made_overridable(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "overridable": ["n"]})

    def test_non_string_entry_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "overridable": [{"name": "max_length"}]})

    def test_non_list_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"override": "listed", "overridable": "max_length"})


class TestParameterPolicyCeilings:
    """The ceilings a policy puts on the params it caps."""

    def test_ceilings_within_the_parameter_range_are_kept(self):
        policy = parse_policy({"ceilings": {"max_length": 512, "max_context_length": 4096}})
        assert policy.ceilings == {"max_length": 512, "max_context_length": 4096}

    def test_absent_ceilings_stay_absent(self):
        assert parse_policy({}).ceilings is None

    def test_image_ceilings_are_kept_against_the_image_vocabulary(self):
        policy = parse_policy({"ceilings": {"width": 1024, "steps": 30}}, IMAGE_VOCABULARY)
        assert policy.ceilings == {"width": 1024, "steps": 30}

    @pytest.mark.parametrize(
        ("parameter_name", "value"),
        [
            ("max_length", 15),
            ("max_length", 4097),
            ("max_context_length", 79),
            ("max_context_length", 1_048_577),
            ("n", 0),
            ("n", 21),
        ],
    )
    def test_ceilings_outside_the_parameter_range_are_rejected(self, parameter_name: str, value: int):
        with pytest.raises(e.BadRequest):
            parse_policy({"ceilings": {parameter_name: value}})

    def test_a_ceiling_on_a_parameter_this_type_cannot_cap_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"ceilings": {"temperature": 2}})

    def test_a_ceiling_from_the_other_style_type_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"ceilings": {"width": 1024}})
        with pytest.raises(e.BadRequest):
            parse_policy({"ceilings": {"max_length": 512}}, IMAGE_VOCABULARY)

    @pytest.mark.parametrize("value", [True, "512", 512.5, [512]])
    def test_non_integer_ceilings_are_rejected(self, value: object):
        with pytest.raises(e.BadRequest):
            parse_policy({"ceilings": {"max_length": value}})

    def test_a_non_object_ceilings_value_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_policy({"ceilings": ["max_length"]})

    def test_a_ceiling_the_mode_leaves_inert_is_accepted(self):
        # Under 'none' a request sets nothing, so nothing is measured against this ceiling. It is
        # still a valid thing to declare, and it starts applying as soon as the mode changes.
        policy = parse_policy({"override": "none", "ceilings": {"max_length": 512}})
        assert policy.ceiling_for("max_length") == 512

    def test_the_request_count_can_be_capped_under_every_mode(self):
        # The number of takes always comes from the request, so its ceiling is never inert.
        for mode in ("none", "all"):
            policy = parse_policy({"override": mode, "ceilings": {"n": 4}})
            assert policy.ceiling_for("n") == 4

    def test_ceiling_lookup_by_parameter(self):
        policy = parse_policy({"ceilings": {"max_length": 512}})
        assert policy.ceiling_for("max_length") == 512
        assert policy.ceiling_for("max_context_length") is None
        assert policy.ceiling_for("temperature") is None


class TestParameterPolicyApplication:
    """What each mode allows, as the request path asks it."""

    def test_none_lets_nothing_through(self):
        policy = parse_policy({"override": "none"})
        assert policy.allows_client_parameter("max_length") is False

    def test_all_lets_everything_but_the_request_count_through(self):
        policy = parse_policy({"override": "all"})
        assert policy.allows_client_parameter("max_length") is True
        assert policy.allows_client_parameter("temperature") is True
        assert policy.allows_client_parameter("n") is False

    def test_listed_lets_only_the_listed_parameters_through(self):
        policy = parse_policy({"override": "listed", "overridable": ["max_length"]})
        assert policy.allows_client_parameter("max_length") is True
        assert policy.allows_client_parameter("max_context_length") is False


class TestTemplateFields:
    """The placeholders a style declares."""

    def test_declarations_are_kept_in_order(self):
        declared = parse_template_fields(
            [
                {"name": "caption", "description": "What the picture shows", "required": True},
                {"name": "tags", "description": "Comma-separated tags"},
            ],
        )
        assert [declaration.name for declaration in declared] == ["caption", "tags"]
        assert declared[0].required is True
        assert declared[1].required is False

    def test_empty_list_declares_nothing(self):
        assert parse_template_fields([]) == ()

    def test_non_list_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields({"caption": "What the picture shows"})

    def test_non_object_entry_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields(["caption"])

    def test_unknown_declaration_key_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": "caption", "description": "ok", "default": "x"}])

    def test_more_than_the_limit_is_rejected(self):
        too_many = [{"name": f"field_{index}", "description": "ok"} for index in range(MAX_TEMPLATE_FIELDS + 1)]
        with pytest.raises(e.BadRequest):
            parse_template_fields(too_many)

    def test_exactly_the_limit_is_accepted(self):
        at_limit = [{"name": f"field_{index}", "description": "ok"} for index in range(MAX_TEMPLATE_FIELDS)]
        assert len(parse_template_fields(at_limit)) == MAX_TEMPLATE_FIELDS

    @pytest.mark.parametrize("placeholder", ["p", "np"])
    def test_reserved_placeholders_are_rejected(self, placeholder: str):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": placeholder, "description": "ok"}])

    @pytest.mark.parametrize(
        "placeholder",
        ["", "Caption", "1caption", "_caption", "cap tion", "caption-1", "caption!", "c" * (TEMPLATE_FIELD_NAME_MAX_LENGTH + 1)],
    )
    def test_malformed_placeholders_are_rejected(self, placeholder: str):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": placeholder, "description": "ok"}])

    @pytest.mark.parametrize("placeholder", ["c", "c" * TEMPLATE_FIELD_NAME_MAX_LENGTH, "caption_1", "a0_b"])
    def test_well_formed_placeholders_are_accepted(self, placeholder: str):
        assert parse_template_fields([{"name": placeholder, "description": "ok"}])[0].name == placeholder

    def test_missing_placeholder_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"description": "ok"}])

    def test_repeated_placeholders_are_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": "caption", "description": "ok"}, {"name": "caption", "description": "also ok"}])

    def test_missing_description_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": "caption"}])

    @pytest.mark.parametrize("description", ["", "d" * (TEMPLATE_FIELD_DESCRIPTION_MAX_LENGTH + 1)])
    def test_descriptions_outside_the_accepted_length_are_rejected(self, description: str):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": "caption", "description": description}])

    def test_description_at_the_length_limit_is_accepted(self):
        description = "d" * TEMPLATE_FIELD_DESCRIPTION_MAX_LENGTH
        assert parse_template_fields([{"name": "caption", "description": description}])[0].description == description

    def test_non_boolean_required_flag_is_rejected(self):
        with pytest.raises(e.BadRequest):
            parse_template_fields([{"name": "caption", "description": "ok", "required": "yes"}])


class TestRoundTrip:
    """Declarations stored on the style row read back unchanged."""

    def test_policy_stores_only_the_keys_it_sets(self):
        stored = serialize_parameter_policy(parse_policy({"override": "all", "ceilings": {"max_length": 512}}))
        assert stored == {"override": "all", "ceilings": {"max_length": 512}}

    def test_policy_reads_back_unchanged(self):
        policy = parse_policy(
            {"override": "listed", "overridable": ["max_length"], "ceilings": {"max_length": 512}},
        )
        assert load_parameter_policy(serialize_parameter_policy(policy)) == policy

    def test_an_image_policy_reads_back_unchanged(self):
        policy = parse_policy({"override": "all", "ceilings": {"width": 1024, "height": 1024}}, IMAGE_VOCABULARY)
        assert load_parameter_policy(serialize_parameter_policy(policy)) == policy

    def test_template_fields_read_back_unchanged(self):
        declared = parse_template_fields([{"name": "caption", "description": "What the picture shows", "required": True}])
        assert load_template_fields(serialize_template_fields(declared)) == declared

    def test_a_style_declaring_neither_reads_back_as_nothing(self):
        assert load_parameter_policy(None) is None
        assert load_template_fields(None) == ()
