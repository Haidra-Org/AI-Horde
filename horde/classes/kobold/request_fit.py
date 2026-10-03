# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sizing a text request's context against the prompt it ends up sending.

A text request sets how much context the model is given (``max_context_length``) and how many tokens
it wants back (``max_length``). When the prompt plus the tokens to generate do not fit the context,
the worker cuts the front of the prompt away, which a client applying a style cannot see coming,
since the style's template settled the final length.

``context_fit`` on the request chooses what happens instead.
[`estimate_prompt_tokens`][horde.classes.kobold.request_fit.estimate_prompt_tokens] sizes the prompt
without a tokenizer, [`prompt_fits_context`][horde.classes.kobold.request_fit.prompt_fits_context]
applies the fit rule, [`fit_context_length`][horde.classes.kobold.request_fit.fit_context_length]
returns the context to run under, and
[`context_growth_upper_bound`][horde.classes.kobold.request_fit.context_growth_upper_bound] limits
how far growth may go. Everything here is pure; the endpoint does the database reads and applies the
result.
"""

from __future__ import annotations

import enum
import math

from horde import exceptions as e
from horde.classes.base.style_contract import StyleParameterPolicy

MAX_LENGTH_PARAMETER = "max_length"
"""Params-model key for how many tokens to generate."""

MAX_CONTEXT_LENGTH_PARAMETER = "max_context_length"
"""Params-model key for how much context the model is given."""

CHARS_PER_TOKEN_ESTIMATE = 3
"""Characters per token. No tokenizer runs on the horde, so the estimate is a conservative one."""

DEFAULT_MAX_LENGTH = 80
"""The ``max_length`` a request that sets none runs under. The same default ``TextWaitingPrompt`` uses."""

DEFAULT_MAX_CONTEXT_LENGTH = 2048
"""The ``max_context_length`` a request that sets none runs under. The same default ``TextWaitingPrompt`` uses."""

MAX_LENGTH_LIMIT = 4096
"""The largest ``max_length`` the params model accepts."""

MAX_CONTEXT_LENGTH_LIMIT = 1_048_576
"""The largest ``max_context_length`` the params model accepts, and so the hard limit on growth."""


class ContextFit(enum.StrEnum):
    """What to do when a request's prompt does not fit the context it asked for."""

    IGNORE = "ignore"
    """Send it anyway; the worker cuts the front of the prompt away. This is the long-standing behaviour."""

    REJECT = "reject"
    """Refuse the request rather than let the worker cut the front of the prompt away."""

    GROW = "grow"
    """Raise ``max_context_length`` until the prompt fits, and charge for the larger context."""


class ContextFitOutcome(enum.StrEnum):
    """What sizing a text request against its prompt came to, as counted in ``horde.text.context_fit``."""

    FITS = "fits"
    """The prompt fit the requested context, so nothing changed."""

    SENT_OVERLONG = "sent_overlong"
    """The prompt did not fit, and under ``ignore`` it was sent anyway."""

    GROWN = "grown"
    """The context was raised so the prompt fits."""

    REFUSED = "refused"
    """The request was refused with ``PromptExceedsContext``."""


def parse_context_fit(raw_context_fit: object) -> ContextFit:
    """Resolve the request's ``context_fit``, defaulting to the long-standing behaviour.

    Args:
        raw_context_fit: The ``context_fit`` value from the request body, possibly absent.

    Returns:
        The setting to apply.

    Raises:
        horde.exceptions.BadRequest: If the value is not one of the accepted settings.
    """
    if raw_context_fit is None:
        return ContextFit.IGNORE

    if not isinstance(raw_context_fit, str):
        raise e.BadRequest("'context_fit' must be a string.")

    try:
        return ContextFit(raw_context_fit)
    except ValueError:
        accepted = ", ".join(setting.value for setting in ContextFit)
        raise e.BadRequest(f"'context_fit' must be one of: {accepted}.") from None


def estimate_prompt_tokens(prompt: str) -> int:
    """Return the estimated token count for a prompt.

    Args:
        prompt: The prompt as it will be sent, after any style templating.

    Returns:
        The estimated token count.
    """
    return math.ceil(len(prompt) / CHARS_PER_TOKEN_ESTIMATE)


def prompt_fits_context(*, estimated_prompt_tokens: int, max_length: int, max_context_length: int) -> bool:
    """Report whether the prompt and the tokens to be generated both fit the context.

    Args:
        estimated_prompt_tokens: The estimated token count of the prompt, after templating.
        max_length: How many tokens the request wants generated.
        max_context_length: The context the request set.

    Returns:
        True when the worker will not have to cut the prompt down.
    """
    return estimated_prompt_tokens + max_length <= max_context_length


def fit_context_length(
    *,
    estimated_prompt_tokens: int,
    max_length: int,
    max_context_length: int,
    context_fit: ContextFit,
    upper_bound: int,
) -> int:
    """Return the ``max_context_length`` the request should run under.

    Args:
        estimated_prompt_tokens: The estimated token count of the prompt, after templating.
        max_length: How many tokens the request wants generated.
        max_context_length: The context the request currently sets.
        context_fit: What to do when the prompt does not fit that context.
        upper_bound: The largest context growth may reach.

    Returns:
        The context to run under: the one asked for, or a larger one under ``grow``.

    Raises:
        horde.exceptions.BadRequest: Under ``reject`` when the prompt does not fit, and under ``grow``
            when no context within the bound fits it.
    """
    if context_fit is ContextFit.IGNORE:
        return max_context_length

    if prompt_fits_context(
        estimated_prompt_tokens=estimated_prompt_tokens,
        max_length=max_length,
        max_context_length=max_context_length,
    ):
        return max_context_length

    needed_context_length = estimated_prompt_tokens + max_length

    if context_fit is ContextFit.REJECT:
        raise _prompt_exceeds_context(estimated_prompt_tokens, max_length, max_context_length)

    if needed_context_length > upper_bound:
        raise _prompt_exceeds_context(estimated_prompt_tokens, max_length, upper_bound)

    # Workers advertise powers of two and the kudos curve is written against them, so growth lands on
    # one; the bound takes over when the next power of two is past it but the request still fits.
    return min(_smallest_power_of_two_at_or_above(needed_context_length), upper_bound)


def context_growth_upper_bound(
    *,
    policy: StyleParameterPolicy | None,
    highest_worker_max_context_length: int | None,
    requested_max_context_length: int,
) -> int:
    """Return the largest context growth may reach for one request.

    Growing past what any online worker advertises would leave the request waiting until it expires,
    so the worker pool limits growth alongside the params model and the style's own ceiling. When no
    online worker serves any of the request's models there is no size the pool is known to serve, so
    the request does not grow at all: the limit is the context it asked for, and a prompt that does
    not fit it is refused.

    Args:
        policy: The style's parameter policy, or None when no style applies or it declares none.
        highest_worker_max_context_length: The largest context advertised by an online worker serving
            one of the request's models, or None when no such worker is online.
        requested_max_context_length: The context the request set before any growth.

    Returns:
        The limit.
    """
    if highest_worker_max_context_length is None:
        return requested_max_context_length

    bounds = [MAX_CONTEXT_LENGTH_LIMIT, highest_worker_max_context_length]

    if policy is not None:
        style_ceiling = policy.ceiling_for(MAX_CONTEXT_LENGTH_PARAMETER)
        if style_ceiling is not None:
            bounds.append(style_ceiling)

    return min(bounds)


def _prompt_exceeds_context(estimated_prompt_tokens: int, max_length: int, max_context_length: int) -> e.BadRequest:
    """Build the rejection with the three figures that did not add up.

    Args:
        estimated_prompt_tokens: The estimated token count of the prompt.
        max_length: How many tokens the request wants generated.
        max_context_length: The context the prompt had to fit into.

    Returns:
        The exception for the caller to raise.
    """
    return e.BadRequest(
        f"The prompt is estimated at {estimated_prompt_tokens} tokens and the request wants {max_length} more, "
        f"which does not fit a max_context_length of {max_context_length}.",
        rc="PromptExceedsContext",
    )


def _smallest_power_of_two_at_or_above(value: int) -> int:
    """Return the smallest power of two that is at least the given value.

    Args:
        value: The size to round up, one or greater.

    Returns:
        The rounded size.
    """
    if value <= 1:
        return 1

    return 1 << (value - 1).bit_length()
