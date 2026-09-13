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
    merge_client_parameters,
    resolve_template_field_values,
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

    def test_override_listed_rejects_a_param_it_does_not_list(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            merge_client_parameters(
                style_parameters=STYLE_PARAMETERS,
                client_parameters=CLIENT_PARAMETERS,
                policy=policy_of(override="listed", overridable=("max_length",)),
            )

        assert rejection.value.rc == "StyleParameterNotOverridable"
        assert "temperature" in rejection.value.specific

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

    def test_a_ceiling_on_a_param_the_mode_does_not_let_through_does_nothing(self) -> None:
        merged = merge_client_parameters(
            style_parameters=STYLE_PARAMETERS,
            client_parameters={"max_length": 4096},
            policy=policy_of(override="none", ceilings={"max_length": 512}),
        )

        assert merged["max_length"] == STYLE_PARAMETERS["max_length"]

    def test_a_param_the_style_refuses_outright_is_reported_before_a_ceiling(self) -> None:
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

        assert rejection.value.rc == "StyleParameterNotOverridable"


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
