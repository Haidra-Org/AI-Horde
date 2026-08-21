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
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.classes.stable import waiting_prompt as stable_waiting_prompt
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt


def _wp(n=2, kudos=10.0, models=()):
    return SimpleNamespace(
        n=n,
        models=list(models),
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
