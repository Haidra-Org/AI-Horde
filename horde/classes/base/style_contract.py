# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validation for the optional parameter policy and template fields a style can declare.

A style normally replaces a request's params outright, which wastes budget on small requests and cuts
large ones short. ``parameter_policy`` lets a style say which params a request may still set and the
ceilings it may not exceed; ``template_fields`` lets a style declare the placeholders its prompt
accepts, so structured input does not have to be flattened into ``{p}``.

Both are stored as plain JSON on the style row. The models here define their shape once, for the
style endpoints that accept them and for the request path that applies them. Two rules depend on the
params model of the style's type: which param keys a policy may list, and which params it may cap
and within what range. Those travel in a [`StyleContractVocabulary`]
[horde.classes.base.style_contract.StyleContractVocabulary] through the pydantic validation context,
so this module does not import the API layer.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, NoReturn

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from horde import exceptions as e

MAX_TEMPLATE_FIELDS = 16
"""Maximum number of placeholders a single style may declare."""

TEMPLATE_FIELD_NAME_MAX_LENGTH = 32
"""Maximum length of a placeholder name. The pattern below enforces the same bound."""

TEMPLATE_FIELD_NAME_PATTERN = re.compile(rf"^[a-z][a-z0-9_]{{0,{TEMPLATE_FIELD_NAME_MAX_LENGTH - 1}}}$")
"""Placeholder names are lower snake_case identifiers so ``str.format_map`` can address them."""

RESERVED_TEMPLATE_FIELD_NAMES = frozenset({"p", "np"})
"""Placeholders the horde fills itself: the request's prompt and, for image styles, its negative prompt."""

TEMPLATE_FIELD_DESCRIPTION_MIN_LENGTH = 1
TEMPLATE_FIELD_DESCRIPTION_MAX_LENGTH = 200

NON_OVERRIDABLE_PARAMETER_NAMES = frozenset({"n"})
"""``n`` always comes from the request, so a policy cannot hand it over or hold it back."""

VOCABULARY_CONTEXT_KEY = "style_contract_vocabulary"
"""Validation-context key holding the vocabulary of the style type being validated."""

OVERRIDABLE_MISPLACED_ERROR_TYPE = "style_policy_overridable_misplaced"
"""Error type the translation below looks for, so this rejection keeps its own return code."""


class StyleParameterOverride(enum.StrEnum):
    """How much of a request's params body survives when a style is applied."""

    NONE = "none"
    """The style's params are used as they are. A request's params body is ignored, as it is for a style with no policy."""

    LISTED = "listed"
    """Only the params the policy lists may come from the request."""

    ALL = "all"
    """Any param may come from the request; the ceilings still apply."""


OVERRIDABLE_REQUIRED_MESSAGE = f"override '{StyleParameterOverride.LISTED.value}' must list at least one parameter in 'overridable'"
"""Used both when the list is absent and when it is empty; neither gives the mode anything to act on."""


@dataclass(frozen=True)
class ParameterCeilingBound:
    """The range a ceiling may be set to, taken from the params model of the param it caps."""

    minimum: int | None
    maximum: int | None


@dataclass(frozen=True)
class StyleContractVocabulary:
    """What one style type lets a policy talk about.

    Both entries come from that type's params model, so a policy can only mention params the request
    path will actually see, and a ceiling can only sit within the range its param accepts.
    """

    overridable_parameter_names: frozenset[str] = frozenset()
    """Every params-model key a policy may put in ``overridable``, without the ones a request always sets."""

    ceiling_bounds: Mapping[str, ParameterCeilingBound] = field(default_factory=dict)
    """The params a policy may cap, each with the range its ceiling may take."""


class StyleTemplateField(BaseModel):
    """Represents one placeholder a style's prompt accepts from the request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The placeholder as it appears in the prompt, without braces."""

    description: str = Field(
        min_length=TEMPLATE_FIELD_DESCRIPTION_MIN_LENGTH,
        max_length=TEMPLATE_FIELD_DESCRIPTION_MAX_LENGTH,
    )
    """What the style expects in this placeholder, shown to whoever fills it in."""

    required: StrictBool = False
    """When true, a request using this style must supply the field."""

    @field_validator("name")
    @classmethod
    def _name_is_an_unreserved_identifier(cls, name: str) -> str:
        """Reject a placeholder the horde fills itself or one ``str.format_map`` could not address.

        Args:
            name: The declared placeholder.

        Returns:
            The same placeholder.

        Raises:
            ValueError: If the placeholder is reserved or is not a lower snake_case identifier.
        """
        if name in RESERVED_TEMPLATE_FIELD_NAMES:
            reserved = ", ".join(sorted(RESERVED_TEMPLATE_FIELD_NAMES))
            raise ValueError(f"'{name}' is filled by the horde ({reserved}) and cannot be declared")

        if not TEMPLATE_FIELD_NAME_PATTERN.match(name):
            raise ValueError(
                f"'{name}' is not valid; use up to {TEMPLATE_FIELD_NAME_MAX_LENGTH} lowercase letters, "
                "digits and underscores, starting with a letter",
            )

        return name


class StyleTemplateFields(RootModel[list[StyleTemplateField]]):
    """Represents every placeholder a style declares, plus the rules that apply across the list."""

    root: Annotated[list[StyleTemplateField], Field(max_length=MAX_TEMPLATE_FIELDS)]

    @model_validator(mode="after")
    def _placeholders_are_unique(self) -> StyleTemplateFields:
        """Reject a placeholder declared twice, which would leave one of the declarations unreachable.

        Returns:
            The same declarations.

        Raises:
            ValueError: If a placeholder is declared more than once.
        """
        seen: set[str] = set()
        for declaration in self.root:
            if declaration.name in seen:
                raise ValueError(f"'{declaration.name}' is declared more than once")
            seen.add(declaration.name)

        return self


class StyleParameterPolicy(BaseModel):
    """Represents which request params a style lets through, and the limits it puts on them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    override: StyleParameterOverride = StyleParameterOverride.NONE
    """Which of the request's params are kept."""

    overridable: tuple[str, ...] | None = None
    """The params a request may set. Only read, and only accepted, under ``listed``."""

    ceilings: dict[str, StrictInt] | None = None
    """The largest value a request may ask for, per param.

    A ceiling limits the request's value only; the style's own params are never checked against it. A
    ceiling is only accepted on a param the mode hands to the request: ``n`` under every mode, since
    it always comes from the request; any param the style type can cap under ``all``; only the params
    in ``overridable`` under ``listed``; and nothing else under ``none``.
    """

    @model_validator(mode="before")
    @classmethod
    def _overridable_is_not_misplaced(cls, raw_policy: Any) -> Any:
        """Reject an ``overridable`` list under a mode that never reads it.

        This runs before field validation because the rejection is about where the key sits rather
        than what it holds, and it keeps its own return code whatever the list contains.

        Args:
            raw_policy: The policy object from the request body.

        Returns:
            The same policy, for the field validation that follows.

        Raises:
            ValueError: If the policy is not an object.
            pydantic_core.PydanticCustomError: If the list appears under a mode that does not read it.
        """
        if not isinstance(raw_policy, Mapping):
            raise ValueError("must be an object")

        # An explicit null means unset rather than a misplaced list: JSON has no other way to say
        # absence, and the model's own default is None.
        if raw_policy.get("overridable") is None:
            return raw_policy

        raw_override = raw_policy.get("override")
        # An unrecognised mode is rejected on its own by the field validation that follows.
        declared_mode = _known_override(raw_override)
        if declared_mode is None or declared_mode is StyleParameterOverride.LISTED:
            return raw_policy

        raise PydanticCustomError(
            OVERRIDABLE_MISPLACED_ERROR_TYPE,
            "'overridable' is only read under override '{listed}', not '{declared}'",
            {"listed": StyleParameterOverride.LISTED.value, "declared": declared_mode.value},
        )

    @field_validator("overridable")
    @classmethod
    def _overridable_holds_known_parameters(
        cls,
        overridable: tuple[str, ...] | None,
        info: ValidationInfo,
    ) -> tuple[str, ...] | None:
        """Drop repeated entries and reject anything the style type's params model does not define.

        The vocabulary arrives in the validation context, so a policy read back off a style row
        validates without the API layer being importable.

        Args:
            overridable: The declared param keys.
            info: Carries the vocabulary, when one was supplied.

        Returns:
            The keys in the order given, without repeats.

        Raises:
            ValueError: If a key is not a param a style of this type can hand over.
        """
        if overridable is None:
            return None

        vocabulary = _vocabulary(info)
        deduplicated: list[str] = []
        for parameter_name in overridable:
            if vocabulary is not None and parameter_name not in vocabulary.overridable_parameter_names:
                raise ValueError(f"'{parameter_name}' is not a parameter a style of this type can hand over")
            if parameter_name not in deduplicated:
                deduplicated.append(parameter_name)

        return tuple(deduplicated)

    @field_validator("ceilings")
    @classmethod
    def _ceilings_cap_known_parameters_within_their_range(
        cls,
        ceilings: dict[str, StrictInt] | None,
        info: ValidationInfo,
    ) -> dict[str, StrictInt] | None:
        """Reject a ceiling on a param this style type cannot cap, or one outside that param's range.

        A ceiling below the param's minimum could never be met and one above its maximum would never
        apply, so the range comes from the params model through the vocabulary.

        Args:
            ceilings: The declared ceilings, keyed by the param each one caps.
            info: Carries the vocabulary, when one was supplied.

        Returns:
            The same ceilings.

        Raises:
            ValueError: If a ceiling caps a param this style type does not allow a ceiling on, or
                falls outside that param's range.
        """
        if ceilings is None:
            return None

        vocabulary = _vocabulary(info)
        if vocabulary is None:
            return ceilings

        for parameter_name, ceiling in ceilings.items():
            bound = vocabulary.ceiling_bounds.get(parameter_name)
            if bound is None:
                allowed = ", ".join(sorted(vocabulary.ceiling_bounds))
                raise ValueError(f"'{parameter_name}' cannot be capped by a style of this type; it caps {allowed}")

            if bound.minimum is not None and ceiling < bound.minimum:
                raise ValueError(f"'{parameter_name}' cannot be capped below {bound.minimum}")

            if bound.maximum is not None and ceiling > bound.maximum:
                raise ValueError(f"'{parameter_name}' cannot be capped above {bound.maximum}")

        return ceilings

    @model_validator(mode="after")
    def _listed_mode_lists_something(self) -> StyleParameterPolicy:
        """Reject a ``listed`` policy with an empty list.

        ``listed`` with nothing listed behaves exactly like ``none``, and anyone reading the mode
        would expect at least one param to be settable.

        Returns:
            The same policy.

        Raises:
            ValueError: If the mode is ``listed`` and no param key is left after validation.
        """
        if self.override is StyleParameterOverride.LISTED and not self.overridable:
            raise ValueError(OVERRIDABLE_REQUIRED_MESSAGE)

        return self

    @model_validator(mode="after")
    def _ceilings_cap_parameters_the_mode_hands_over(self) -> StyleParameterPolicy:
        """Reject a ceiling on a param the mode never lets a request set.

        Such a ceiling would never be measured against anything, and a style author reading it back
        would expect it to hold. This rule needs only the policy itself, so it also holds for a policy
        validated without a vocabulary.

        Returns:
            The same policy.

        Raises:
            ValueError: If a ceiling caps a param the mode does not hand to the request.
        """
        for parameter_name in self.ceilings or {}:
            if parameter_name in NON_OVERRIDABLE_PARAMETER_NAMES or self.allows_client_parameter(parameter_name):
                continue

            raise ValueError(
                f"a ceiling on '{parameter_name}' would never apply under override '{self.override.value}', "
                "which does not let a request set it",
            )

        return self

    def ceiling_for(self, parameter_name: str) -> int | None:
        """Return the ceiling this policy puts on one param.

        Args:
            parameter_name: The params-model key to look up, such as ``max_length``.

        Returns:
            The ceiling, or None when the policy caps that param nowhere.
        """
        if self.ceilings is None:
            return None

        return self.ceilings.get(parameter_name)

    def allows_client_parameter(self, parameter_name: str) -> bool:
        """Report whether a request may set one param under this policy.

        Args:
            parameter_name: The params-model key the request carried.

        Returns:
            True when the request's value for that key may be copied over the style's.
        """
        if parameter_name in NON_OVERRIDABLE_PARAMETER_NAMES:
            return False

        if self.override is StyleParameterOverride.ALL:
            return True

        if self.override is StyleParameterOverride.LISTED:
            return parameter_name in (self.overridable or ())

        return False


def _known_override(raw_override: object) -> StyleParameterOverride | None:
    """Resolve a raw override value to a mode without rejecting an unrecognised one.

    Args:
        raw_override: The ``override`` value from the request body, possibly absent.

    Returns:
        The mode, the default when absent, or None when the value matches no mode.
    """
    if raw_override is None:
        return StyleParameterOverride.NONE

    if isinstance(raw_override, StyleParameterOverride):
        return raw_override

    if not isinstance(raw_override, str):
        return None

    try:
        return StyleParameterOverride(raw_override)
    except ValueError:
        return None


def _vocabulary(info: ValidationInfo) -> StyleContractVocabulary | None:
    """Read the style type's vocabulary out of the validation context, if one was supplied.

    Args:
        info: The validation info passed to a validator.

    Returns:
        The vocabulary, or None when the policy is being validated without one.
    """
    if not isinstance(info.context, Mapping):
        return None

    vocabulary = info.context.get(VOCABULARY_CONTEXT_KEY)
    if not isinstance(vocabulary, StyleContractVocabulary):
        return None

    return vocabulary


def _validation_reasons(error: ValidationError) -> str:
    """Return every reason a pydantic ValidationError gives, as one line for a client to read.

    Args:
        error: The error the model raised.

    Returns:
        The reasons, each prefixed with the location it applies to, separated by semicolons.
    """
    reasons: list[str] = []
    for detail in error.errors():
        # Pydantic prefixes anything a validator raised with "Value error, "; strip it so the client
        # gets the plain reason.
        reason = detail["msg"].removeprefix("Value error, ")
        location = ".".join(str(part) for part in detail["loc"])
        reasons.append(f"{location}: {reason}" if location else reason)

    return "; ".join(reasons)


def _as_bad_request(error: ValidationError, subject: str) -> NoReturn:
    """Translate a pydantic ValidationError into a horde BadRequest.

    Args:
        error: The error the model raised.
        subject: The request field being validated, used in the message.

    Raises:
        horde.exceptions.BadRequest: Always, listing every reason the model gave.
    """
    return_code = "BadRequest"
    if any(detail["type"] == OVERRIDABLE_MISPLACED_ERROR_TYPE for detail in error.errors()):
        return_code = "StylePolicyOverridableMisplaced"

    raise e.BadRequest(f"'{subject}' is not valid. {_validation_reasons(error)}.", rc=return_code)


def _as_invalid_stored_declaration(error: ValidationError, *, subject: str, style_name: str) -> NoReturn:
    """Translate a pydantic ValidationError raised by a stored declaration into a horde BadRequest.

    A declaration passed validation when it was written, and the rules or the style type's params
    model can change after that. The request cannot run under the style as stored, and the fault lies
    with the style, so the client gets a 400 naming it.

    Args:
        error: The error the model raised.
        subject: The style column that failed, used in the message.
        style_name: The style the declaration belongs to, used in the message.

    Raises:
        horde.exceptions.BadRequest: Always, with the ``StyleDeclarationInvalid`` return code.
    """
    raise e.BadRequest(
        f"Style '{style_name}' cannot be applied because its stored '{subject}' is not valid. {_validation_reasons(error)}.",
        rc="StyleDeclarationInvalid",
    )


def build_vocabulary(
    *,
    parameter_names: Iterable[str],
    ceiling_bounds: Mapping[str, ParameterCeilingBound],
) -> StyleContractVocabulary:
    """Create the vocabulary for one style type.

    Args:
        parameter_names: Every key of that type's params model.
        ceiling_bounds: The params that type lets a policy cap, each with the range its ceiling may take.

    Returns:
        The vocabulary to validate that type's policies against.
    """
    return StyleContractVocabulary(
        overridable_parameter_names=frozenset(parameter_names) - NON_OVERRIDABLE_PARAMETER_NAMES,
        ceiling_bounds=dict(ceiling_bounds),
    )


def parse_parameter_policy(raw_policy: object, *, vocabulary: StyleContractVocabulary) -> StyleParameterPolicy:
    """Validate a parameter policy sent to a style endpoint and return it as a model.

    Args:
        raw_policy: The ``parameter_policy`` object from the request body.
        vocabulary: What the style's type lets a policy talk about.

    Returns:
        The validated policy.

    Raises:
        horde.exceptions.BadRequest: If the policy is malformed, lists a param the type does not have,
            puts ``overridable`` under a mode that does not read it, caps a param the type cannot cap
            or one the mode does not hand to the request, or caps one outside its range.
    """
    try:
        return StyleParameterPolicy.model_validate(raw_policy, context={VOCABULARY_CONTEXT_KEY: vocabulary})
    except ValidationError as validation_error:
        _as_bad_request(validation_error, "parameter_policy")


def parse_template_fields(raw_fields: object) -> tuple[StyleTemplateField, ...]:
    """Validate a list of template field declarations sent to a style endpoint.

    A declared field does not have to appear in the style's prompt: a prompt can be rewritten without
    its declarations being rewritten alongside, and an unused declaration formats to the empty string.

    Args:
        raw_fields: The ``template_fields`` object from the request body.

    Returns:
        The validated declarations in the order given.

    Raises:
        horde.exceptions.BadRequest: If the list is malformed, too long, or holds a placeholder that
            is reserved, repeated, or not a lower snake_case identifier.
    """
    try:
        return tuple(StyleTemplateFields.model_validate(raw_fields).root)
    except ValidationError as validation_error:
        _as_bad_request(validation_error, "template_fields")


def serialize_parameter_policy(policy: StyleParameterPolicy) -> dict[str, Any]:
    """Render a policy as the JSON stored on the style row.

    Args:
        policy: The validated policy.

    Returns:
        A JSON-safe dict holding only the keys the policy actually sets.
    """
    return policy.model_dump(mode="json", exclude_none=True)


def serialize_template_fields(template_fields: Sequence[StyleTemplateField]) -> list[dict[str, Any]]:
    """Render template field declarations as the JSON stored on the style row.

    Args:
        template_fields: The validated declarations.

    Returns:
        A JSON-safe list, one object per declaration.
    """
    return [declaration.model_dump(mode="json") for declaration in template_fields]


def load_parameter_policy(
    stored_policy: object,
    *,
    vocabulary: StyleContractVocabulary,
    style_name: str,
) -> StyleParameterPolicy | None:
    """Read a policy back off a style row, validating it as the style endpoints would.

    Args:
        stored_policy: The ``parameter_policy`` column, which is null for a style that declares none.
        vocabulary: What the style's type lets a policy talk about.
        style_name: The style the policy belongs to, named in the rejection.

    Returns:
        The policy, or None when the style declares none.

    Raises:
        horde.exceptions.BadRequest: With the ``StyleDeclarationInvalid`` return code, if the stored
            policy no longer passes validation.
    """
    if stored_policy is None:
        return None

    try:
        return StyleParameterPolicy.model_validate(stored_policy, context={VOCABULARY_CONTEXT_KEY: vocabulary})
    except ValidationError as validation_error:
        _as_invalid_stored_declaration(validation_error, subject="parameter_policy", style_name=style_name)


def load_template_fields(stored_fields: object, *, style_name: str) -> tuple[StyleTemplateField, ...]:
    """Read template field declarations back off a style row, validating them as the style endpoints would.

    Args:
        stored_fields: The ``template_fields`` column, which is null for a style that declares none.
        style_name: The style the declarations belong to, named in the rejection.

    Returns:
        The declarations, empty when the style declares none.

    Raises:
        horde.exceptions.BadRequest: With the ``StyleDeclarationInvalid`` return code, if the stored
            declarations no longer pass validation.
    """
    if stored_fields is None:
        return ()

    try:
        return tuple(StyleTemplateFields.model_validate(stored_fields).root)
    except ValidationError as validation_error:
        _as_invalid_stored_declaration(validation_error, subject="template_fields", style_name=style_name)
