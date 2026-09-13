# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Applying a style's declarations to the request that uses it.

A style replaces a request's params outright, which wastes budget on a small request and cuts a large
one short. [`merge_client_parameters`][horde.classes.base.style_application.merge_client_parameters]
decides which of the request's params are kept, under the style's ``parameter_policy``, and holds
every value the request still controls to the ceilings that policy sets.
[`resolve_template_field_values`][horde.classes.base.style_application.resolve_template_field_values]
checks the values a request supplies for the placeholders its style declares.
[`format_text_style_prompt`][horde.classes.base.style_application.format_text_style_prompt] fills a
text style's template with both.

All three are pure: they take the style's declarations and the request's body and return what to build
the waiting prompt from, or raise. The request path does the database reads, the image side of prompt
formatting, and anything else the style replaces.
"""

from __future__ import annotations

import re
import secrets
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from horde import exceptions as e
from horde.classes.base.style_contract import (
    NON_OVERRIDABLE_PARAMETER_NAMES,
    StyleParameterOverride,
    StyleParameterPolicy,
    StyleTemplateField,
)

CLIENT_OWNED_PARAMETER = "n"
"""How many generations were asked for. It comes from the request under every mode, which is why it is
also in ``NON_OVERRIDABLE_PARAMETER_NAMES``: a policy can neither hand it over nor hold it back."""

DEFAULT_CLIENT_OWNED_PARAMETER_VALUE = 1
"""How many generations a request that sets no ``n`` gets."""

MAX_TEMPLATE_FIELD_WORDS = 7500
"""Word limit for filled-in template fields, per field and in total.

The limit the horde places on a prompt (``ParamValidator.validate_image_params``): a template field is
prompt text and reaches the model the same way.
"""

PROTECTED_TEXT_PLACEHOLDER_PATTERN = re.compile(r"\{\{\[[A-Z_]+\]\}\}")
"""Instruct placeholders a text backend fills in itself, such as ``{{[INPUT]}}`` and ``{{[OUTPUT]}}``.

The name between the brackets is uppercase letters and underscores. Anything else, including a
lowercase name, is ordinary template text and formats under the usual rules.
"""

SHIELD_MARKER_NONCE_BYTES = 8
"""Length of the random part of the marker that stands in for a protected placeholder."""


@dataclass(frozen=True)
class ResolvedTemplateFields:
    """The placeholders a style declares, together with the values a request supplied for them."""

    declared_names: tuple[str, ...]
    """Every placeholder the style declares, in the order it declares them."""

    values: dict[str, str]
    """The values the request supplied, keyed by placeholder. A placeholder left out is absent here."""


def merge_client_parameters(
    *,
    style_parameters: Mapping[str, Any],
    client_parameters: Mapping[str, Any],
    policy: StyleParameterPolicy | None,
) -> dict[str, Any]:
    """Return the params a styled request runs under.

    The style's params are the starting point. ``n`` always comes from the request, because it is how
    many generations were asked for rather than how they are made. The rest of the request's params
    are kept only as far as the style's policy allows, and every value the request ends up
    controlling is held to the ceilings the policy sets, ``n`` included.

    Args:
        style_parameters: The style's own params. The caller passes a copy; this does not mutate it.
        client_parameters: The params the request sent.
        policy: The style's parameter policy, or None for a style that declares none.

    Returns:
        The merged params.

    Raises:
        horde.exceptions.BadRequest: If the request set a param the policy does not make overridable,
            or set one above the ceiling the policy places on it.
    """
    merged_parameters = dict(style_parameters)
    client_controlled_parameters: dict[str, Any] = {
        CLIENT_OWNED_PARAMETER: client_parameters.get(CLIENT_OWNED_PARAMETER, DEFAULT_CLIENT_OWNED_PARAMETER_VALUE),
    }

    if policy is not None:
        for parameter_name, client_value in client_parameters.items():
            if parameter_name in NON_OVERRIDABLE_PARAMETER_NAMES:
                continue

            if policy.allows_client_parameter(parameter_name):
                client_controlled_parameters[parameter_name] = client_value
                continue

            if policy.override is StyleParameterOverride.LISTED:
                raise e.BadRequest(
                    f"This style does not allow '{parameter_name}' to be set.",
                    rc="StyleParameterNotOverridable",
                )
            # Under 'none' a params body has always been ignored rather than refused.

        for parameter_name, client_value in client_controlled_parameters.items():
            _raise_if_above_ceiling(policy, parameter_name, client_value)

    merged_parameters.update(client_controlled_parameters)
    return merged_parameters


def _raise_if_above_ceiling(policy: StyleParameterPolicy, parameter_name: str, client_value: Any) -> None:
    """Check one value the request controls against the ceiling the style puts on it.

    A value above the ceiling is refused rather than trimmed, because trimming would change what the
    request costs without saying so.

    Args:
        policy: The style's parameter policy.
        parameter_name: The params-model key the request set.
        client_value: The value the request set it to.

    Raises:
        horde.exceptions.BadRequest: If the value is above the ceiling.
    """
    ceiling = policy.ceiling_for(parameter_name)
    if ceiling is None:
        return

    # A ceiling is an integer bound. A value of another type is left to the params model to reject.
    if not isinstance(client_value, int) or isinstance(client_value, bool):
        return

    if client_value > ceiling:
        raise e.BadRequest(
            f"'{parameter_name}' is {client_value}, above the maximum of {ceiling} this style allows.",
            rc="StyleParameterAboveCeiling",
        )


def resolve_template_field_values(
    *,
    declared_fields: Sequence[StyleTemplateField],
    supplied_fields: Mapping[str, Any] | None,
) -> ResolvedTemplateFields:
    """Validate the template fields a request supplies against the ones its style declares.

    Args:
        declared_fields: The fields the style declares, empty for a style that declares none.
        supplied_fields: The ``template_fields`` object from the request body, possibly absent.

    Returns:
        The style's placeholders and the values supplied for them.

    Raises:
        horde.exceptions.BadRequest: If the object is malformed, includes a field the style does not
            declare, leaves out a required field, or goes past the word limit for a prompt.
    """
    declared_names = tuple(declaration.name for declaration in declared_fields)

    if supplied_fields is None:
        supplied_fields = {}
    elif not isinstance(supplied_fields, Mapping):
        raise e.BadRequest("'template_fields' must be an object of field names to strings.")

    field_values: dict[str, str] = {}
    total_words = 0
    for field_name, supplied_value in supplied_fields.items():
        if field_name not in declared_names:
            raise e.BadRequest(
                f"This style does not declare a template field called '{field_name}'.",
                rc="TemplateFieldUnknown",
            )

        if not isinstance(supplied_value, str):
            raise e.BadRequest(f"Template field '{field_name}' must be a string.")

        field_word_count = len(supplied_value.split())
        if field_word_count > MAX_TEMPLATE_FIELD_WORDS:
            raise e.BadRequest(
                f"Template field '{field_name}' is longer than the {MAX_TEMPLATE_FIELD_WORDS} word limit for a prompt.",
                rc="InvalidPromptSize",
            )

        total_words += field_word_count
        if total_words > MAX_TEMPLATE_FIELD_WORDS:
            raise e.BadRequest(
                f"'template_fields' is longer in total than the {MAX_TEMPLATE_FIELD_WORDS} word limit for a prompt.",
                rc="InvalidPromptSize",
            )

        field_values[field_name] = supplied_value

    for declaration in declared_fields:
        if declaration.required and declaration.name not in field_values:
            raise e.BadRequest(
                f"This style requires the template field '{declaration.name}': {declaration.description}",
                rc="TemplateFieldMissing",
            )

    return ResolvedTemplateFields(declared_names=declared_names, values=field_values)


def format_text_style_prompt(
    *,
    template: str,
    prompt: str,
    field_values: Mapping[str, str],
) -> str:
    """Fill a text style's prompt template with the request's prompt and its template field values.

    The whole rule for a text template:

    - ``{p}`` becomes the request's own prompt.
    - Each placeholder the style declares becomes the value the request supplied for it.
    - A placeholder nothing fills becomes the empty string rather than failing the request.
    - ``{{`` and ``}}`` are a literal brace each, as in any Python format string.
    - A token of the form ``{{[NAME]}}``, with uppercase letters and underscores between the brackets,
      is kept exactly as it was written.

    The last rule exists because those tokens are instruct placeholders the text backend fills in when
    it builds its own prompt, and formatting them as braces would strip them out of a template written
    against that backend.

    Args:
        template: The style's prompt template.
        prompt: The request's own prompt.
        field_values: The values the request supplied for the placeholders the style declares.

    Returns:
        The prompt the worker is handed.
    """
    # This rule is what the style contract endpoint publishes and what docs/reference/style_contract.md
    # sets out. Changing any part of it means raising the contract's schema version and rewriting both.
    shielded_template, protected_placeholders = _shield_protected_placeholders(template)
    formatted_prompt = shielded_template.format_map(defaultdict(str, field_values, p=prompt))
    return _restore_protected_placeholders(formatted_prompt, protected_placeholders)


def _shield_protected_placeholders(template: str) -> tuple[str, dict[str, str]]:
    """Replace each protected placeholder with a marker that formatting leaves alone.

    The marker carries a random part drawn per call, so text coming in from the request cannot pose as
    one and have itself turned into a placeholder on the way out.

    Args:
        template: The style's prompt template.

    Returns:
        The template with every protected placeholder replaced, and the placeholder each marker stands
        for, empty when the template holds none.
    """
    if not PROTECTED_TEXT_PLACEHOLDER_PATTERN.search(template):
        return template, {}

    nonce = secrets.token_hex(SHIELD_MARKER_NONCE_BYTES)
    protected_placeholders: dict[str, str] = {}

    def _marker_for(match: re.Match[str]) -> str:
        marker = f"{nonce}{len(protected_placeholders)}{nonce}"
        protected_placeholders[marker] = match.group(0)
        return marker

    return PROTECTED_TEXT_PLACEHOLDER_PATTERN.sub(_marker_for, template), protected_placeholders


def _restore_protected_placeholders(formatted_prompt: str, protected_placeholders: Mapping[str, str]) -> str:
    """Put each protected placeholder back where its marker sits.

    Args:
        formatted_prompt: The formatted prompt, still carrying the markers.
        protected_placeholders: The placeholder each marker stands for.

    Returns:
        The prompt with every marker replaced by the placeholder it stood for.
    """
    restored_prompt = formatted_prompt
    for marker, placeholder in protected_placeholders.items():
        restored_prompt = restored_prompt.replace(marker, placeholder)

    return restored_prompt
