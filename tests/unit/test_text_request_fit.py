# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sizing a text request's context against its prompt.

Pure sizing: nothing here touches the database or the Flask app, so the rules are covered
independently of the endpoint that applies them.
"""

from typing import Any

import pytest

from horde import exceptions as e
from horde.classes.base.style_contract import StyleParameterPolicy
from horde.classes.kobold.request_fit import (
    CHARS_PER_TOKEN_ESTIMATE,
    MAX_CONTEXT_LENGTH_LIMIT,
    ContextFit,
    context_growth_upper_bound,
    estimate_prompt_tokens,
    fit_context_length,
    parse_context_fit,
    prompt_fits_context,
)


def policy_of(**declarations: Any) -> StyleParameterPolicy:
    """Build a parameter policy without a vocabulary to validate it against.

    Args:
        **declarations: The policy fields to set.

    Returns:
        The policy.
    """
    return StyleParameterPolicy(**declarations)


class TestPromptTokenEstimate:
    """Estimating a prompt's token count without running a tokenizer."""

    @pytest.mark.parametrize(
        ("prompt_length", "expected_tokens"),
        [(0, 0), (1, 1), (3, 1), (4, 2), (300, 100), (301, 101)],
    )
    def test_the_estimate_rounds_up(self, prompt_length: int, expected_tokens: int) -> None:
        assert estimate_prompt_tokens("x" * prompt_length) == expected_tokens

    def test_the_estimate_follows_the_declared_ratio(self) -> None:
        assert estimate_prompt_tokens("x" * (CHARS_PER_TOKEN_ESTIMATE * 42)) == 42


class TestContextFitParsing:
    """The request's context_fit, which defaults to the long-standing behaviour."""

    def test_an_absent_value_is_ignore(self) -> None:
        assert parse_context_fit(None) is ContextFit.IGNORE

    @pytest.mark.parametrize("raw_context_fit", ["ignore", "reject", "grow"])
    def test_each_declared_setting_is_accepted(self, raw_context_fit: str) -> None:
        assert parse_context_fit(raw_context_fit).value == raw_context_fit

    @pytest.mark.parametrize("raw_context_fit", ["shrink", "", 3])
    def test_anything_else_is_rejected(self, raw_context_fit: object) -> None:
        with pytest.raises(e.BadRequest):
            parse_context_fit(raw_context_fit)


class TestFitRule:
    """Whether a worker will have to cut the prompt down."""

    @pytest.mark.parametrize(
        ("estimated_prompt_tokens", "expected_fit"),
        [(783, True), (784, True), (785, False)],
    )
    def test_the_generated_tokens_count_against_the_context(
        self,
        estimated_prompt_tokens: int,
        expected_fit: bool,
    ) -> None:
        fits = prompt_fits_context(
            estimated_prompt_tokens=estimated_prompt_tokens,
            max_length=240,
            max_context_length=1024,
        )

        assert fits is expected_fit


class TestContextFitting:
    """Whether the prompt fits the context, and what happens when it does not."""

    def test_ignore_leaves_an_over_long_prompt_alone(self) -> None:
        fitted = fit_context_length(
            estimated_prompt_tokens=4000,
            max_length=240,
            max_context_length=1024,
            context_fit=ContextFit.IGNORE,
            upper_bound=MAX_CONTEXT_LENGTH_LIMIT,
        )

        assert fitted == 1024

    @pytest.mark.parametrize("context_fit", list(ContextFit))
    def test_a_prompt_that_fits_is_left_alone(self, context_fit: ContextFit) -> None:
        fitted = fit_context_length(
            estimated_prompt_tokens=700,
            max_length=240,
            max_context_length=1024,
            context_fit=context_fit,
            upper_bound=MAX_CONTEXT_LENGTH_LIMIT,
        )

        assert fitted == 1024

    def test_the_fit_is_exact_at_the_boundary(self) -> None:
        fitted = fit_context_length(
            estimated_prompt_tokens=784,
            max_length=240,
            max_context_length=1024,
            context_fit=ContextFit.REJECT,
            upper_bound=MAX_CONTEXT_LENGTH_LIMIT,
        )

        assert fitted == 1024

    def test_reject_refuses_an_over_long_prompt_with_the_three_figures(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            fit_context_length(
                estimated_prompt_tokens=2000,
                max_length=240,
                max_context_length=1024,
                context_fit=ContextFit.REJECT,
                upper_bound=MAX_CONTEXT_LENGTH_LIMIT,
            )

        assert rejection.value.rc == "PromptExceedsContext"
        assert "2000" in rejection.value.specific
        assert "240" in rejection.value.specific
        assert "1024" in rejection.value.specific

    @pytest.mark.parametrize(
        ("estimated_prompt_tokens", "expected_context_length"),
        [(1000, 2048), (1808, 2048), (1809, 4096), (4000, 8192)],
    )
    def test_grow_rounds_up_to_a_power_of_two(
        self,
        estimated_prompt_tokens: int,
        expected_context_length: int,
    ) -> None:
        fitted = fit_context_length(
            estimated_prompt_tokens=estimated_prompt_tokens,
            max_length=240,
            max_context_length=1024,
            context_fit=ContextFit.GROW,
            upper_bound=MAX_CONTEXT_LENGTH_LIMIT,
        )

        assert fitted == expected_context_length

    def test_grow_stops_at_the_bound_when_the_request_still_fits_under_it(self) -> None:
        fitted = fit_context_length(
            estimated_prompt_tokens=2000,
            max_length=240,
            max_context_length=1024,
            context_fit=ContextFit.GROW,
            upper_bound=3000,
        )

        assert fitted == 3000

    def test_grow_refuses_what_no_context_within_the_bound_fits(self) -> None:
        with pytest.raises(e.BadRequest) as rejection:
            fit_context_length(
                estimated_prompt_tokens=4000,
                max_length=240,
                max_context_length=1024,
                context_fit=ContextFit.GROW,
                upper_bound=4096,
            )

        assert rejection.value.rc == "PromptExceedsContext"
        assert "4096" in rejection.value.specific


class TestGrowthBound:
    """What limits growth: the params model, the style's ceiling and the worker pool."""

    def test_without_a_style_the_pool_bounds_growth(self) -> None:
        bound = context_growth_upper_bound(
            policy=None,
            highest_worker_max_context_length=8192,
            requested_max_context_length=2048,
        )

        assert bound == 8192

    def test_a_pool_past_the_params_model_is_held_to_the_params_model(self) -> None:
        bound = context_growth_upper_bound(
            policy=None,
            highest_worker_max_context_length=MAX_CONTEXT_LENGTH_LIMIT * 2,
            requested_max_context_length=2048,
        )

        assert bound == MAX_CONTEXT_LENGTH_LIMIT

    def test_the_styles_ceiling_bounds_growth(self) -> None:
        bound = context_growth_upper_bound(
            policy=policy_of(override="all", ceilings={"max_context_length": 4096}),
            highest_worker_max_context_length=8192,
            requested_max_context_length=2048,
        )

        assert bound == 4096

    def test_the_smallest_bound_applies(self) -> None:
        bound = context_growth_upper_bound(
            policy=policy_of(override="all", ceilings={"max_context_length": 16384}),
            highest_worker_max_context_length=8192,
            requested_max_context_length=2048,
        )

        assert bound == 8192

    def test_a_policy_without_that_ceiling_bounds_nothing(self) -> None:
        bound = context_growth_upper_bound(
            policy=policy_of(override="all", ceilings={"max_length": 512}),
            highest_worker_max_context_length=8192,
            requested_max_context_length=2048,
        )

        assert bound == 8192

    def test_with_no_online_worker_the_request_does_not_grow(self) -> None:
        bound = context_growth_upper_bound(
            policy=policy_of(override="all", ceilings={"max_context_length": 16384}),
            highest_worker_max_context_length=None,
            requested_max_context_length=2048,
        )

        assert bound == 2048

    def test_with_no_online_worker_an_over_long_prompt_is_refused_at_the_requested_context(self) -> None:
        bound = context_growth_upper_bound(
            policy=None,
            highest_worker_max_context_length=None,
            requested_max_context_length=2048,
        )

        with pytest.raises(e.BadRequest) as rejection:
            fit_context_length(
                estimated_prompt_tokens=3000,
                max_length=80,
                max_context_length=2048,
                context_fit=ContextFit.GROW,
                upper_bound=bound,
            )

        assert rejection.value.rc == "PromptExceedsContext"
        assert "does not fit a max_context_length of 2048" in rejection.value.specific
