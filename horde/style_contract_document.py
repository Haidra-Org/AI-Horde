# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publishes what a style may declare and how its prompt template is filled in.

A client that wants to offer style authoring, or to send a request under someone else's style, needs
the same vocabulary the validators use: which params a policy may hand over, which it may cap and
between what bounds, which placeholders the horde fills in, and what happens to the braces around
them. All of that is decided in code the client cannot import, so this module renders it.

The projection is built from the per-type
[`StyleContractVocabulary`][horde.classes.base.style_contract.StyleContractVocabulary] the style
endpoints validate against and from the formatting rules in
[`horde.classes.base.style_application`][horde.classes.base.style_application], so the published
answer and the enforced one come from the same place.

The endpoint serves [`published_style_contract`][horde.style_contract_document.published_style_contract],
which renders what [`compile_style_contract`][horde.style_contract_document.compile_style_contract]
builds.
"""

from __future__ import annotations

import functools
from typing import Any

from pydantic import BaseModel, ConfigDict

from horde.classes.base.style_application import PROTECTED_TEXT_PLACEHOLDER_PATTERN
from horde.classes.base.style_contract import StyleContractVocabulary
from horde.classes.kobold.request_fit import ContextFit

SCHEMA_VERSION = 1
"""The version of the published contract.

Raised whenever what is published changes meaning: a key added, removed or renamed, a rule stated
differently, or a rule the horde enforces differently than before. A client pins this and re-reads the
contract when it moves.
"""

TEXT_PLACEHOLDERS = ("p",)
"""The placeholders the horde fills in a text style's prompt: the request's own prompt."""

IMAGE_PLACEHOLDERS = ("p", "np")
"""The placeholders the horde fills in an image style's prompt: the request's prompt and its negative half."""

TEXT_BRACE_HANDLING = (
    "A doubled brace is one literal brace, '{p}' and the placeholders this style declares are filled "
    "in, and a placeholder nothing fills becomes the empty string. The protected patterns below are "
    "the exception and are left exactly as written."
)

IMAGE_BRACE_HANDLING = (
    "Every brace is literal except '{p}', '{np}' and the placeholders this style declares, so a "
    "wildcard or a workflow string in braces reaches the worker as written."
)

TEXT_PROTECTED_PATTERN_DESCRIPTION = (
    "An instruct placeholder a text backend fills in when it builds its own prompt, such as '{{[INPUT]}}' or '{{[OUTPUT]}}'."
)


class PublishedProtectedPattern(BaseModel):
    """Represents a run of template text that formatting leaves exactly as it was written."""

    model_config = ConfigDict(frozen=True)

    pattern: str
    """The pattern as a regular expression, matched against the style's prompt."""

    description: str
    """What the matching text is for."""


class PublishedParameterCeiling(BaseModel):
    """Represents the range a ceiling on one param may be set to."""

    model_config = ConfigDict(frozen=True)

    minimum: int | None
    """The lowest value a ceiling on this param may take, or null when the param has no lower bound."""

    maximum: int | None
    """The highest value a ceiling on this param may take, or null when the param has no upper bound."""


class PublishedStyleTypeContract(BaseModel):
    """Represents what one style type lets a style declare and how its prompt template is filled in."""

    model_config = ConfigDict(frozen=True)

    placeholders: tuple[str, ...]
    """The placeholders the horde fills in, written without braces."""

    declared_fields_are_placeholders: bool
    """Whether a placeholder this style type declares is filled from the request's ``template_fields``."""

    protected_patterns: tuple[PublishedProtectedPattern, ...]
    """Template text formatting leaves alone, empty for a type that protects nothing."""

    brace_handling: str
    """What the braces in this type's prompt template mean."""

    overridable_parameters: tuple[str, ...]
    """Every param a policy of this type may put in ``overridable``."""

    ceiling_parameters: dict[str, PublishedParameterCeiling]
    """The params a policy of this type may cap, each with the range its ceiling may take."""

    context_fit_modes: tuple[str, ...] | None
    """How a request of this type may be sized against its prompt, or null for a type that cannot be."""

    style_write_rate_limits: tuple[str, ...]
    """Every limit a client writing a style of this type is held to; all of them apply at once."""


class StyleContractDocument(BaseModel):
    """Represents the whole published style contract."""

    model_config = ConfigDict(frozen=True)

    schema_version: int
    """What version of the contract this is."""

    text: PublishedStyleTypeContract
    """What a text style may declare."""

    image: PublishedStyleTypeContract
    """What an image style may declare."""


def _serialize_ceilings(vocabulary: StyleContractVocabulary) -> dict[str, PublishedParameterCeiling]:
    """Return the params one style type may cap, each with the range its ceiling may take.

    Args:
        vocabulary: The style type's vocabulary.

    Returns:
        The ranges, keyed by the param each one caps.
    """
    return {
        parameter_name: PublishedParameterCeiling(minimum=bound.minimum, maximum=bound.maximum)
        for parameter_name, bound in sorted(vocabulary.ceiling_bounds.items())
    }


def compile_style_contract() -> StyleContractDocument:
    """Return the contract the style contract endpoint serves.

    Anything published here is a promise a client builds against: it offers the params a policy may
    name, writes prompts under the brace rules, and sends requests it expects to be accepted. Changing
    what a field means, adding one or dropping one therefore means raising ``SCHEMA_VERSION`` and
    rewriting the same rule in docs/reference/style_contract.md, and a rule stated here has to be the
    one the validators and the prompt formatting actually apply.

    Returns:
        The typed contract, which the endpoint serialises at the HTTP boundary.
    """
    # Imported here because the vocabularies and the rate limits are declared in the API layer, which
    # imports the endpoint this contract is served from.
    import horde.apis.limiter_api as lim
    from horde.apis.v2.kobold import text_style_contract_vocabulary
    from horde.apis.v2.kobold_styles import TEXT_STYLE_WRITE_WINDOW_RATE_LIMIT
    from horde.apis.v2.stable import image_style_contract_vocabulary

    text_vocabulary = text_style_contract_vocabulary()
    image_vocabulary = image_style_contract_vocabulary()

    return StyleContractDocument(
        schema_version=SCHEMA_VERSION,
        text=PublishedStyleTypeContract(
            placeholders=TEXT_PLACEHOLDERS,
            declared_fields_are_placeholders=True,
            protected_patterns=(
                PublishedProtectedPattern(
                    pattern=PROTECTED_TEXT_PLACEHOLDER_PATTERN.pattern,
                    description=TEXT_PROTECTED_PATTERN_DESCRIPTION,
                ),
            ),
            brace_handling=TEXT_BRACE_HANDLING,
            overridable_parameters=tuple(sorted(text_vocabulary.overridable_parameter_names)),
            ceiling_parameters=_serialize_ceilings(text_vocabulary),
            context_fit_modes=tuple(mode.value for mode in ContextFit),
            style_write_rate_limits=(TEXT_STYLE_WRITE_WINDOW_RATE_LIMIT, lim.REQUEST_2SEC_LIMIT_PER_IP),
        ),
        image=PublishedStyleTypeContract(
            placeholders=IMAGE_PLACEHOLDERS,
            declared_fields_are_placeholders=True,
            protected_patterns=(),
            brace_handling=IMAGE_BRACE_HANDLING,
            overridable_parameters=tuple(sorted(image_vocabulary.overridable_parameter_names)),
            ceiling_parameters=_serialize_ceilings(image_vocabulary),
            context_fit_modes=None,
            style_write_rate_limits=(lim.REQUEST_90MIN_LIMIT_PER_IP, lim.REQUEST_2SEC_LIMIT_PER_IP),
        ),
    )


@functools.cache
def published_style_contract() -> dict[str, Any]:
    """Return the contract the endpoint publishes, compiled and serialised once per process.

    The contract is a pure function of the installed code: no request input, no database read, nothing
    a running process can change. Holding it removes any window in which a process could publish a
    contract other than the one its own code enforces, and it is cheaper than fetching one from a
    shared cache.

    The returned mapping is shared by every caller and must not be mutated.

    Returns:
        The contract rendered to plain JSON types.
    """
    return compile_style_contract().model_dump(mode="json")
