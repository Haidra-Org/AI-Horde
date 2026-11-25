# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the dry-run kudos quote.

A dry run must quote what the same request costs when it is actually submitted.
``WaitingPrompt._activate`` charges a horde tax of 1 plus 5 per extra source
image, so the quote carries the same tax. The image line multiplies the
per-job kudos by a baseline factor, and the tax stays outside that factor
because activation charges it flat.

``WaitingPrompt.__init__`` needs a database session, so the formulas are exercised
by binding the methods to lightweight stubs rather than building an ORM graph.

The quote is cached under a hash of the request, computed once before validation to look a cached
quote up and once after to store it. The hash cases check that a style changes it and that validation
does not. The lookup cases check when a cached quote answers a dry run and how each lookup is counted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from flask_restx.reqparse import ParseResult

from horde import metrics
from horde.apis.v2.base import GenerateTemplate
from horde.apis.v2.kobold import TextAsyncGenerate
from horde.apis.v2.stable import ImageAsyncGenerate
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.classes.kobold.request_fit import CHARS_PER_TOKEN_ESTIMATE, ContextFit
from horde.classes.stable import waiting_prompt as stable_waiting_prompt
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.config import Config


def _wp(n=2, kudos=10.0, models=(), params=None):
    return SimpleNamespace(
        n=n,
        models=list(models),
        # The image quote reads the workflow features that have their own kudos multiplier.
        params=params if params is not None else {},
        calculate_kudos=lambda: kudos,
        calculate_extra_kudos_burn=lambda k: k,
    )


@pytest.fixture
def baseline(monkeypatch):
    def _set(name):
        monkeypatch.setattr(stable_waiting_prompt.model_reference, "get_model_baseline", lambda model_name: name)

    return _set


class TestBaseQuote:
    def test_no_extra_source_images_quotes_the_flat_request_tax(self):
        assert WaitingPrompt.extrapolate_dry_run_kudos(_wp()) == 21

    def test_extra_source_images_are_taxed_five_each(self):
        assert WaitingPrompt.extrapolate_dry_run_kudos(_wp(), extra_source_images_count=3) == 36

    def test_kudos_adjustment_joins_the_flat_tax(self):
        assert WaitingPrompt.extrapolate_dry_run_kudos(_wp(), kudos_adjustment=2) == 23

    def test_adjustment_and_extras_stack(self):
        assert WaitingPrompt.extrapolate_dry_run_kudos(_wp(), extra_source_images_count=3, kudos_adjustment=2) == 38


class TestImageQuote:
    def test_no_extra_source_images_quotes_the_flat_request_tax(self, baseline):
        baseline("stable_diffusion_1")
        assert ImageWaitingPrompt.extrapolate_dry_run_kudos(_wp()) == 21

    def test_extra_source_images_are_taxed_five_each(self, baseline):
        baseline("stable_diffusion_1")
        assert ImageWaitingPrompt.extrapolate_dry_run_kudos(_wp(), extra_source_images_count=3) == 36

    def test_baseline_multiplier_does_not_apply_to_the_tax(self, baseline):
        baseline("stable_diffusion_xl")
        assert ImageWaitingPrompt.extrapolate_dry_run_kudos(_wp(), extra_source_images_count=3) == 56

    def test_baseline_multiplier_does_not_apply_to_the_adjustment(self, baseline):
        baseline("stable_diffusion_xl")
        assert ImageWaitingPrompt.extrapolate_dry_run_kudos(_wp(), kudos_adjustment=2) == 43

    def test_the_qr_code_workflow_is_quoted_at_what_it_is_charged(self, baseline):
        # The quote reads the same per-baseline ladder the per-generation charge does, so a workflow
        # with its own multiplier there has it here.
        baseline("stable_diffusion_xl")
        assert ImageWaitingPrompt.extrapolate_dry_run_kudos(_wp(params={"workflow": "qr_code"})) == 81

    def test_the_cascade_two_pass_workflow_is_quoted_at_what_it_is_charged(self, baseline):
        baseline("stable_cascade")
        assert ImageWaitingPrompt.extrapolate_dry_run_kudos(_wp(params={"hires_fix": True})) == 141


def _generate_request(style=None, models=("a-model",)):
    """Build a stand-in for a generate resource as it stands at the pre-validate cache lookup."""
    request_args = SimpleNamespace(
        models=list(models),
        style=style,
        extra_source_images=None,
        source_processing="img2img",
        source_image=None,
        source_mask=None,
    )
    generate_request = SimpleNamespace(
        args=request_args,
        params={"n": 1, "max_length": 80},
        models=list(models),
        style_kudos=False,
    )
    generate_request.get_extra_source_images_count = lambda: GenerateTemplate.get_extra_source_images_count(generate_request)
    return generate_request


@pytest.mark.parametrize(
    "generate_class",
    [GenerateTemplate, ImageAsyncGenerate, TextAsyncGenerate],
    ids=["base", "image", "text"],
)
class TestQuoteCacheKey:
    def test_a_styled_request_hashes_apart_from_the_same_unstyled_request(self, generate_class):
        styled_hash = generate_class.get_hashed_params_dict(_generate_request(style="a-style"))
        unstyled_hash = generate_class.get_hashed_params_dict(_generate_request())

        assert styled_hash != unstyled_hash

    def test_the_style_surcharge_being_settled_does_not_change_the_hash(self, generate_class):
        generate_request = _generate_request(style="a-style")
        lookup_hash = generate_class.get_hashed_params_dict(generate_request)

        generate_request.style_kudos = True

        assert generate_class.get_hashed_params_dict(generate_request) == lookup_hash


class TestTextQuoteCacheKey:
    def test_the_styles_models_replacing_the_requested_ones_do_not_change_the_hash(self):
        generate_request = _generate_request(style="a-style")
        lookup_hash = TextAsyncGenerate.get_hashed_params_dict(generate_request)

        generate_request.models = ["a-style-model"]

        assert TextAsyncGenerate.get_hashed_params_dict(generate_request) == lookup_hash

    def test_extra_source_images_change_the_hash(self):
        without_extras = _generate_request()
        with_extras = _generate_request()
        with_extras.args.extra_source_images = [{"image": "an-image"}]

        assert TextAsyncGenerate.get_hashed_params_dict(with_extras) != TextAsyncGenerate.get_hashed_params_dict(without_extras)


class _CounterRecorder:
    """Stands in for a metric counter and keeps the attributes of every increment."""

    def __init__(self) -> None:
        self.attributes: list[dict[str, object]] = []

    def add(self, amount: int, attributes: dict[str, object]) -> None:
        self.attributes.append(attributes)


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> _CounterRecorder:
    recorder = _CounterRecorder()
    monkeypatch.setattr(metrics, "dry_run_cache_lookups", recorder)
    return recorder


def _text_dry_run(
    *,
    max_length: int = 80,
    max_context_length: int = 2048,
    prompt: str = "a short prompt",
    style: str | None = None,
    context_fit: ContextFit = ContextFit.IGNORE,
) -> TextAsyncGenerate:
    """Build a text dry run as it stands at the pre-validate cache lookup."""
    resource = TextAsyncGenerate()
    resource.args = ParseResult(
        dry_run=True,
        prompt=prompt,
        style=style,
        template_fields=None,
        slow_workers=True,
        workers=[],
    )
    resource.params = {
        "max_length": max_length,
        "max_context_length": max_context_length,
    }
    resource.context_fit = context_fit
    return resource


class TestDryRunCacheLookup:
    def test_a_cached_quote_answers_a_dry_run_that_needs_nothing_else(self, lookups):
        resource = _text_dry_run()

        assert resource.answer_dry_run_from_cache(b"12.5") is True
        assert resource.kudos == 12.5
        assert lookups.attributes == [
            {
                "horde.gentype": "text",
                "horde.outcome": "hit",
                "horde.bypass_reason": "none",
            },
        ]

    def test_a_dry_run_without_a_cached_quote_is_counted_as_a_miss(self, lookups):
        resource = _text_dry_run()

        assert resource.answer_dry_run_from_cache(None) is False
        assert [attributes["horde.outcome"] for attributes in lookups.attributes] == ["miss"]

    def test_a_bypassed_dry_run_ignores_a_cached_quote(self, lookups):
        resource = _text_dry_run(style="a-style")

        assert resource.answer_dry_run_from_cache(b"12.5") is False
        assert not hasattr(resource, "kudos") or resource.kudos != 12.5
        assert [(attributes["horde.outcome"], attributes["horde.bypass_reason"]) for attributes in lookups.attributes] == [
            ("bypassed", "style"),
        ]


class TestTextDryRunCacheBypass:
    def test_a_request_within_both_floors_can_use_the_cache(self):
        assert _text_dry_run().dry_run_cache_bypass() is None

    def test_a_style_bypasses_the_cache(self):
        assert _text_dry_run(style="a-style").dry_run_cache_bypass() is metrics.DryRunCacheBypass.STYLE

    def test_context_sizing_bypasses_the_cache(self):
        bypass = _text_dry_run(context_fit=ContextFit.GROW).dry_run_cache_bypass()

        assert bypass is metrics.DryRunCacheBypass.CONTEXT_FIT

    def test_a_max_length_over_the_output_floor_bypasses_the_cache(self):
        bypass = _text_dry_run(max_length=Config.text_free_output_floor + 1).dry_run_cache_bypass()

        assert bypass is metrics.DryRunCacheBypass.UPFRONT_KUDOS

    def test_a_processed_prompt_over_the_input_floor_bypasses_the_cache(self):
        """A prompt one token over the input floor, with room for all of it in the context, could need kudos."""
        prompt_tokens = Config.text_free_input_floor + 1
        bypass = _text_dry_run(
            max_context_length=prompt_tokens + 80,
            prompt="x" * (prompt_tokens * CHARS_PER_TOKEN_ESTIMATE),
        ).dry_run_cache_bypass()

        assert bypass is metrics.DryRunCacheBypass.UPFRONT_KUDOS
