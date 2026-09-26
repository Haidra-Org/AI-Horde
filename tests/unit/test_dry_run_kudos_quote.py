# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the dry-run kudos quote.

A dry run must quote what the same request costs when it is actually submitted.
``WaitingPrompt._activate`` charges a horde tax of 1 plus 5 per extra source
image, so the quote carries the same tax. The image line multiplies the
per-job kudos by a baseline factor, and the tax stays outside that factor
because activation charges it flat.

``WaitingPrompt.__init__`` commits to the DB, so the formulas are exercised by
binding the methods to lightweight stubs rather than building a persisted ORM
graph.

The quote is cached under a hash of the request, computed once before validation to look a cached
quote up and once after to store it. The hash cases check that a style changes it and that validation
does not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from horde.apis.v2.base import GenerateTemplate
from horde.apis.v2.kobold import TextAsyncGenerate
from horde.apis.v2.stable import ImageAsyncGenerate
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.classes.stable import waiting_prompt as stable_waiting_prompt
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt


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
