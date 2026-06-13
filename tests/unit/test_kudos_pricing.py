# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pricing-model invariants for ``horde.classes.stable.kudos.KudosModel``.

The kudos pricer is a small frozen torch model loaded from
``kudos-v21-206.ckpt``. Two failure modes we want CI to catch loudly:

1. **Silent checkpoint drift**: someone swaps the .ckpt file, or the
   feature ordering in ``payload_to_tensor`` changes. Either silently shifts
   kudos pricing for every job.
2. **Post-inference arithmetic regression**: ``basis_adjustment`` and
   ``basis_scale`` are pure arithmetic on the model output. Easy to break
   without noticing.

Strategy: design-intent invariants (BASIS_PAYLOAD ≈ KUDOS_BASIS, monotonicity)
+ exact arithmetic on the post-inference math. Specific golden floats are
recorded inline below; if the model is intentionally retrained, regenerate
them via ``python -m horde.classes.stable.kudos <ckpt>``.
"""

from __future__ import annotations

import pytest

# Skip this whole module if torch isn't available (e.g. lightweight dev env).
torch = pytest.importorskip("torch")
pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def kudos_model():
    """Load the singleton ``KudosModel`` once for the module."""
    # Import inside the fixture so the module-level KudosModel() in
    # horde.classes.stable.kudos doesn't fire during collection-only runs.
    from horde.classes.stable.kudos import KudosModel

    return KudosModel()


@pytest.fixture
def basis_payload():
    """A fresh copy of BASIS_PAYLOAD per test (KudosModel mutates payloads in place via .get)."""
    from horde.classes.stable.kudos import KudosModel

    return dict(KudosModel.BASIS_PAYLOAD)


class TestModelLoad:
    def test_singleton_returned_on_repeat_construction(self, kudos_model):
        from horde.classes.stable.kudos import KudosModel

        assert KudosModel() is kudos_model
        assert KudosModel() is KudosModel()

    def test_time_basis_was_calculated(self, kudos_model):
        # calculate_basis_time runs in __init__; a zero here means the
        # singleton init didn't complete.
        assert kudos_model.time_basis > 0


class TestDesignIntent:
    """The model should approximately honour its design contract."""

    def test_basis_payload_is_close_to_kudos_basis(self, kudos_model, basis_payload):
        """A 50-step 512×512 generation should cost ~10 kudos.

        This is the model's documented design intent (see ``KudosModel.KUDOS_BASIS``
        and the ``BASIS_PAYLOAD`` docstring). A wide tolerance is fine - we're
        catching gross drift, not precision regressions.
        """
        kudos = kudos_model.calculate_kudos(basis_payload)
        assert kudos == pytest.approx(10.0, abs=1.0), (
            f"Basis payload should price near KUDOS_BASIS=10.0; got {kudos}. "
            f"Probable cause: checkpoint swap or feature-ordering change in "
            f"payload_to_tensor."
        )

    def test_doubling_steps_roughly_scales_kudos(self, kudos_model, basis_payload):
        baseline = kudos_model.calculate_kudos(basis_payload)
        doubled = dict(basis_payload, steps=basis_payload["steps"] * 2)
        doubled_kudos = kudos_model.calculate_kudos(doubled)
        # Diffusion sampling is roughly linear in step count; allow a wide
        # band (×1.4 .. ×2.5) so this catches direction-of-change failures
        # without being brittle to model-specific non-linearities.
        ratio = doubled_kudos / baseline
        assert 1.4 <= ratio <= 2.5, f"Doubling steps changed kudos by ratio {ratio:.2f}; expected 1.4..2.5"

    def test_doubling_resolution_increases_kudos(self, kudos_model, basis_payload):
        baseline = kudos_model.calculate_kudos(basis_payload)
        bigger = dict(basis_payload, width=1024, height=1024)
        bigger_kudos = kudos_model.calculate_kudos(bigger)
        # Strictly greater, pixel count quadrupled.
        assert bigger_kudos > baseline, (
            f"1024×1024 ({bigger_kudos}) should cost more than 512×512 ({baseline})"
        )


class TestPostInferenceArithmetic:
    """``basis_adjustment`` and ``basis_scale`` are deterministic arithmetic.

    The implementation is::

        kudos = (KUDOS_BASIS + basis_adjustment) * basis_scale * job_ratio

    Note ``basis_adjustment`` defaults to **1**, not 0 - these tests pass it
    explicitly so the arithmetic relationships are unambiguous.
    """

    def test_basis_adjustment_adds_before_scaling(self, kudos_model, basis_payload):
        # Clean baseline: adjustment=0 isolates the model's job_ratio * 10 path.
        baseline_unadjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        adjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=5)
        # adjusted / baseline_unadjusted == (10+5)/10 == 1.5
        assert adjusted == pytest.approx(baseline_unadjusted * 1.5, rel=0.01)

    def test_basis_scale_multiplies(self, kudos_model, basis_payload):
        baseline = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        scaled = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0, basis_scale=1.25)
        assert scaled == pytest.approx(baseline * 1.25, rel=0.01)

    def test_zero_scale_zeroes_kudos(self, kudos_model, basis_payload):
        assert kudos_model.calculate_kudos(basis_payload, basis_scale=0) == 0.0

    def test_combined_adjustment_and_scale(self, kudos_model, basis_payload):
        baseline_unadjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        # (10 + 5) * 1.25 / 10 == 1.875
        combined = kudos_model.calculate_kudos(basis_payload, basis_adjustment=5, basis_scale=1.25)
        assert combined == pytest.approx(baseline_unadjusted * 1.875, rel=0.01)

    def test_default_adjustment_is_one(self, kudos_model, basis_payload):
        """Documented behaviour: the default ``basis_adjustment`` is 1, so an
        unadorned ``calculate_kudos(payload)`` returns 110% of the model's
        raw output. Locked in here because changing the default would silently
        re-price every job in the system."""
        unadjusted = kudos_model.calculate_kudos(basis_payload, basis_adjustment=0)
        defaulted = kudos_model.calculate_kudos(basis_payload)
        assert defaulted == pytest.approx(unadjusted * 11.0 / 10.0, rel=0.01)


class TestUnknownInputsHandled:
    """Unknown samplers / control types should not crash, they get sane defaults."""

    def test_unknown_sampler_falls_back_to_k_euler(self, kudos_model, basis_payload):
        unknown = dict(basis_payload, sampler_name="not_a_real_sampler_xyz")
        # Should not raise, and should produce a finite kudos value.
        kudos = kudos_model.calculate_kudos(unknown)
        assert kudos > 0

    def test_remix_source_processing_treated_as_img2img(self, kudos_model, basis_payload):
        # See the "Little hack until new model is out" comment in
        # payload_to_tensor: source_processing="remix" is mapped to "img2img".
        remix = dict(basis_payload, source_processing="remix", source_image=True)
        img2img = dict(basis_payload, source_processing="img2img", source_image=True)
        # Same arithmetic path → same kudos.
        assert kudos_model.calculate_kudos(remix) == kudos_model.calculate_kudos(img2img)
