# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for applying a style's declarations to the request that uses it.

Pure application: nothing here touches the database or the Flask app, so the rules are covered
independently of the endpoints that apply them.
"""

from typing import Any

import pytest

from horde import exceptions as e
from horde.classes.base.style_application import (
    MAX_TEMPLATE_FIELD_WORDS,
    count_literal_text_template_fields,
    format_text_style_prompt,
    merge_client_parameters,
    resolve_template_field_values,
    template_fields_fit,
    validate_text_template_fields,
)
from horde.classes.base.style_contract import StyleParameterPolicy, StyleTemplateField

STYLE_PARAMETERS: dict[str, Any] = {
    "max_length": 80,
    "max_context_length": 1024,
    "temperature": 0.7,
}

CLIENT_PARAMETERS: dict[str, Any] = {
    "n": 3,
    "max_length": 240,
    "temperature": 1.5,
}


def policy_of(**declarations: Any) -> StyleParameterPolicy:
    """Build a parameter policy without a vocabulary to validate it against.

    Args:
        **declarations: The policy fields to set.

    Returns:
        The policy.
    """
    return StyleParameterPolicy(**declarations)


class TestParameterMerge:
    """Which of a request's params are kept when a style is applied."""

    def test_a_style_without_a_policy_keeps_its_params(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters=CLIENT_PARAMETERS,
            policy=None,
        )

        assert merged == {"max_length": 80, "max_context_length": 1024, "temperature": 0.7, "n": 3}

    def test_override_none_ignores_the_client_params(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters=CLIENT_PARAMETERS,
            policy=policy_of(override="none"),
        )

        assert merged == {"max_length": 80, "max_context_length": 1024, "temperature": 0.7, "n": 3}

    def test_override_all_takes_every_client_param(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters=CLIENT_PARAMETERS,
            policy=policy_of(override="all"),
        )

        assert merged == {"max_length": 240, "max_context_length": 1024, "temperature": 1.5, "n": 3}

    def test_override_listed_takes_only_the_listed_params(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters={"n": 2, "max_length": 240},
            policy=policy_of(override="listed", overridable=("max_length",)),
        )

        assert merged == {"max_length": 240, "max_context_length": 1024, "temperature": 0.7, "n": 2}

    def test_override_listed_ignores_a_param_it_does_not_list(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters=CLIENT_PARAMETERS,
            policy=policy_of(override="listed", overridable=("max_length",)),
        )

        assert merged == {"max_length": 240, "max_context_length": 1024, "temperature": 0.7, "n": 3}

    @pytest.mark.parametrize("override", ["none", "listed", "all"])
    def test_the_request_count_comes_from_the_request_under_every_mode(self, override: str) -> None:
        policy = policy_of(override=override, overridable=("max_length",) if override == "listed" else None)

        merged = merge_client_parameters(
            style_parameters={"n": 9, "max_length": 80},
            client_parameters={"n": 4},
            policy=policy,
        )

        assert merged["n"] == 4

    def test_the_request_count_defaults_to_one(self) -> None:
        merged = merge_client_parameters(
            style_parameters={"n": 9, "max_length": 80},
            client_parameters={},
            policy=None,
        )

        assert merged["n"] == 1


class TestParameterCeilings:
    """A ceiling refuses a request rather than quietly repricing it."""

    def test_a_value_above_the_ceiling_is_rejected(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            merge_client_parameters(
                style_parameters=STYLE_PARAMETERS,
                client_parameters={"max_length": 1024},
                policy=policy_of(override="all", ceilings={"max_length": 512}),
            )

        assert rejection.value.rc == "StyleParameterAboveCeiling"
        assert "1024" in rejection.value.specific
        assert "512" in rejection.value.specific

    def test_a_value_at_the_ceiling_is_accepted(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters={"max_length": 512},
            policy=policy_of(override="all", ceilings={"max_length": 512}),
        )

        assert merged["max_length"] == 512

    def test_the_ceiling_leaves_the_styles_own_value_alone(self) -> None:
        merged = merge_client_parameters(
            style_parameters={"max_length": 1024},
            client_parameters={},
            policy=policy_of(override="all", ceilings={"max_length": 512}),
        )

        assert merged["max_length"] == 1024

    @pytest.mark.parametrize("override", ["none", "listed", "all"])
    def test_the_request_count_is_capped_under_every_mode(self, override: str) -> None:
        policy = policy_of(
            override=override,
            overridable=("max_length",) if override == "listed" else None,
            ceilings={"n": 2},
        )

        with pytest.raises(e.BadRequest) as rejection:
            merge_client_parameters(
                style_parameters=STYLE_PARAMETERS,
                client_parameters={"n": 5},
                policy=policy,
            )

        assert rejection.value.rc == "StyleParameterAboveCeiling"

    def test_an_unlisted_param_is_ignored_while_a_listed_one_is_held_to_its_ceiling(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            merge_client_parameters(
                style_parameters=STYLE_PARAMETERS,
                client_parameters={"max_length": 4096, "temperature": 1.5},
                policy=policy_of(
                    override="listed",
                    overridable=("max_length",),
                    ceilings={"max_length": 512},
                ),
            )

        assert rejection.value.rc == "StyleParameterAboveCeiling"
        assert "max_length" in rejection.value.specific


class TestTemplateFields:
    """What a request may put in a style's declared placeholders."""

    declared = (
        StyleTemplateField(name="caption", description="What the picture shows", required=True),
        StyleTemplateField(name="tags", description="Comma-separated tags", required=False),
    )

    def test_supplied_fields_are_returned_with_the_declared_names(self) -> None:
        resolved = resolve_template_field_values(
            declared_fields=self.declared,
            supplied_fields={"caption": "a lighthouse", "tags": "storm, night"},
        )

        assert resolved.values == {"caption": "a lighthouse", "tags": "storm, night"}
        assert resolved.declared_names == ("caption", "tags")

    def test_an_optional_field_may_be_left_out(self) -> None:
        resolved = resolve_template_field_values(
            declared_fields=self.declared,
            supplied_fields={"caption": "a lighthouse"},
        )

        assert resolved.values == {"caption": "a lighthouse"}

    def test_a_field_the_style_does_not_declare_is_rejected(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            resolve_template_field_values(
                declared_fields=self.declared,
                supplied_fields={"caption": "a lighthouse", "mood": "bleak"},
            )

        assert rejection.value.rc == "TemplateFieldUnknown"
        assert "mood" in rejection.value.specific

    def test_a_required_field_left_out_is_rejected(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            resolve_template_field_values(declared_fields=self.declared, supplied_fields={"tags": "storm"})

        assert rejection.value.rc == "TemplateFieldMissing"
        assert "caption" in rejection.value.specific

    def test_a_style_declaring_none_rejects_any_field(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            resolve_template_field_values(declared_fields=(), supplied_fields={"caption": "a lighthouse"})

        assert rejection.value.rc == "TemplateFieldUnknown"

    def test_a_request_supplying_none_is_accepted(self) -> None:
        resolved = resolve_template_field_values(declared_fields=(), supplied_fields=None)

        assert resolved.values == {}
        assert resolved.declared_names == ()

    def test_a_non_string_value_is_rejected(self) -> None:
        with pytest.raises(e.BadRequest):
            resolve_template_field_values(declared_fields=self.declared, supplied_fields={"caption": 12})

    def test_a_supplied_object_that_is_not_a_mapping_is_rejected(self) -> None:
        with pytest.raises(e.BadRequest):
            resolve_template_field_values(declared_fields=self.declared, supplied_fields=["caption"])

    def test_one_field_longer_than_a_prompt_is_rejected(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            resolve_template_field_values(
                declared_fields=self.declared,
                supplied_fields={"caption": "word " * (MAX_TEMPLATE_FIELD_WORDS + 1)},
            )

        assert rejection.value.rc == "InvalidPromptSize"

    def test_fields_longer_than_a_prompt_in_total_are_rejected(self) -> None:
        half_a_prompt = "word " * (MAX_TEMPLATE_FIELD_WORDS // 2 + 1)

        with pytest.raises(e.BadRequest) as rejection:
            resolve_template_field_values(
                declared_fields=self.declared,
                supplied_fields={"caption": half_a_prompt, "tags": half_a_prompt},
            )

        assert rejection.value.rc == "InvalidPromptSize"


class TestTemplateFieldsFit:
    """Which styles accept the template fields a request supplies, for drawing from a collection."""

    declared = (
        StyleTemplateField(name="caption", description="What the picture shows", required=True),
        StyleTemplateField(name="tags", description="Comma-separated tags", required=False),
    )

    def test_the_required_fields_alone_fit(self) -> None:
        assert template_fields_fit(declared_fields=self.declared, supplied_field_names=["caption"])

    def test_required_and_optional_fields_together_fit(self) -> None:
        assert template_fields_fit(declared_fields=self.declared, supplied_field_names=["caption", "tags"])

    def test_leaving_out_a_required_field_does_not_fit(self) -> None:
        assert not template_fields_fit(declared_fields=self.declared, supplied_field_names=["tags"])

    def test_a_field_the_style_does_not_declare_does_not_fit(self) -> None:
        assert not template_fields_fit(declared_fields=self.declared, supplied_field_names=["caption", "mood"])

    def test_a_style_declaring_nothing_fits_only_a_request_supplying_nothing(self) -> None:
        assert template_fields_fit(declared_fields=(), supplied_field_names=[])
        assert not template_fields_fit(declared_fields=(), supplied_field_names=["caption"])


class TestTextPromptFormatting:
    """What a text style's template turns into once the request's values are in it."""

    def test_the_prompt_and_the_declared_fields_are_filled_in(self) -> None:
        formatted = format_text_style_prompt(
            template="### Instruction:\n{p}\n\nTags: {tags}\n",
            prompt="describe a lighthouse",
            field_values={"tags": "dusk, storm"},
        )

        assert formatted == "### Instruction:\ndescribe a lighthouse\n\nTags: dusk, storm\n"

    def test_a_placeholder_nothing_fills_becomes_nothing(self) -> None:
        formatted = format_text_style_prompt(
            template="{p} [{tags}]",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "a lighthouse []"

    def test_an_instruct_placeholder_is_kept(self) -> None:
        formatted = format_text_style_prompt(
            template="{{[INPUT]}}{p}{{[OUTPUT]}}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "{{[INPUT]}}a lighthouse{{[OUTPUT]}}"

    def test_an_instruct_placeholder_beside_the_prompt_leaves_both_alone(self) -> None:
        formatted = format_text_style_prompt(
            template="{{[INPUT]}}{p}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "{{[INPUT]}}a lighthouse"

    def test_the_same_instruct_placeholder_twice_is_kept_both_times(self) -> None:
        formatted = format_text_style_prompt(
            template="{{[INPUT]}}{p}{{[INPUT]}}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "{{[INPUT]}}a lighthouse{{[INPUT]}}"

    def test_a_lowercase_token_is_not_kept(self) -> None:
        # Only the uppercase form is an instruct placeholder; everything else formats as it always has,
        # so the doubled braces collapse and the inner text is left where it was.
        formatted = format_text_style_prompt(
            template="{{[input]}}{p}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "{[input]}a lighthouse"

    def test_a_malformed_token_is_not_kept(self) -> None:
        formatted = format_text_style_prompt(
            template="{{[IN PUT]}}{p}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "{[IN PUT]}a lighthouse"

    def test_doubled_braces_elsewhere_still_become_one(self) -> None:
        formatted = format_text_style_prompt(
            template="{{literal}} {p} {{[INPUT]}}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == "{literal} a lighthouse {{[INPUT]}}"

    def test_a_supplied_value_that_looks_like_a_marker_is_left_as_it_is(self) -> None:
        # The marker standing in for an instruct placeholder is drawn per call, so a value cannot be
        # written to land on one and be turned into a placeholder.
        formatted = format_text_style_prompt(
            template="{{[INPUT]}}{p}",
            prompt="{{[OUTPUT]}} and {p} and {tags}",
            field_values={},
        )

        assert formatted == "{{[INPUT]}}{{[OUTPUT]}} and {p} and {tags}"

    @pytest.mark.parametrize(
        "field",
        [
            "{p:>20000}",
            "{p!r}",
            "{p.__class__}",
            "{p[0]}",
            "{}",
            "{0}",
        ],
    )
    def test_a_field_that_is_more_than_a_bare_name_is_sent_as_written(self, field: str) -> None:
        """A stored template may predate the write-time check, so such a field is left alone rather than run."""
        formatted = format_text_style_prompt(
            template=f"{field} then {{p}}",
            prompt="a lighthouse",
            field_values={},
        )

        assert formatted == f"{field} then a lighthouse"

    def test_a_format_spec_does_not_grow_the_prompt(self) -> None:
        formatted = format_text_style_prompt(
            template="{p:>20000}{p}",
            prompt="a lighthouse",
            field_values={},
        )

        assert len(formatted) == len("{p:>20000}a lighthouse")

    def test_a_stored_template_whose_braces_do_not_parse_is_a_client_error(self) -> None:
        with pytest.raises(e.BadRequest) as raised:
            format_text_style_prompt(template="{p} and a lone {", prompt="a lighthouse", field_values={})

        assert raised.value.rc == "StyleDeclarationInvalid"


class TestLiteralTemplateFieldCount:
    """How many fields of a stored text template are sent as written instead of filled in."""

    def test_bare_names_and_protected_placeholders_count_nothing(self) -> None:
        assert count_literal_text_template_fields("{{[INPUT]}}{p} {tags} {{literal}}") == 0

    def test_each_field_that_is_more_than_a_bare_name_counts(self) -> None:
        assert count_literal_text_template_fields("{p:>20} {p!r} {p.x} {} {p}") == 4

    def test_a_template_that_does_not_parse_counts_nothing(self) -> None:
        assert count_literal_text_template_fields("{p} {") == 0


class TestTextTemplateFieldValidation:
    """Which text templates a style may be written with."""

    @pytest.mark.parametrize(
        "template",
        [
            "{p}",
            "{p} [{tags}]",
            "{{literal}} {p}",
            "{{[INPUT]}}{p}{{[OUTPUT]}}",
            "{p} {Unknown_Name}",
        ],
    )
    def test_bare_names_and_literal_braces_are_accepted(self, template: str) -> None:
        validate_text_template_fields(template)

    @pytest.mark.parametrize(
        "template",
        [
            "{p:>20000}",
            "{p} {tags!s}",
            "{p.__class__}",
            "{p} {tags[0]}",
            "{p} {}",
            "{p} {1}",
            "{p} {",
            "{p} }",
        ],
    )
    def test_a_field_that_is_more_than_a_bare_name_or_a_lone_brace_is_refused(self, template: str) -> None:
        with pytest.raises(e.BadRequest) as raised:
            validate_text_template_fields(template)

        assert raised.value.rc == "StylePromptFieldInvalid"
