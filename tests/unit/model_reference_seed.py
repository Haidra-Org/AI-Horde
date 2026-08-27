# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Seed the in-memory image model reference with real records for a test.

The reference is fetched over the network at import time, so any test whose subject reads it pins it
here instead, against the same record type the loader produces.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

import horde_model_reference
import pytest
from horde_model_reference import ImageBaselineCatalog
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_model_reference.model_reference_records import ImageGenerationModelRecord

_BOOTSTRAP_CATALOG_PATH = Path(horde_model_reference.__file__).resolve().parent / "data" / "baselines" / "catalog.json"

BOOTSTRAP_BASELINE_CATALOG: ImageBaselineCatalog = ImageBaselineCatalog.model_validate_json(
    _BOOTSTRAP_CATALOG_PATH.read_text(encoding="utf-8"),
)
"""The baseline catalog packaged with horde-model-reference, which a replica starts from."""


def seed_baseline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin baseline lookups to the packaged catalog for the duration of the test.

    The real lookup reads what the PRIMARY serves at the time, which no expectation can be written
    against.

    Args:
        monkeypatch (pytest.MonkeyPatch): The fixture whose teardown restores the served lookup.
    """
    from horde import model_reference as model_reference_module

    monkeypatch.setattr(
        model_reference_module.model_reference,
        "baseline_record",
        lambda baseline: BOOTSTRAP_BASELINE_CATALOG.baselines.get(str(baseline)) if baseline else None,
    )


def make_image_record(
    name: str,
    baseline: KNOWN_IMAGE_GENERATION_BASELINE | str,
    *,
    nsfw: bool = False,
    inpainting: bool = False,
) -> ImageGenerationModelRecord:
    """Create a minimal image generation record carrying only the fields the API reads."""
    return ImageGenerationModelRecord(name=name, baseline=baseline, nsfw=nsfw, inpainting=inpainting)


def seed_image_reference(
    monkeypatch: pytest.MonkeyPatch,
    models: dict[str, KNOWN_IMAGE_GENERATION_BASELINE | str],
    *,
    nsfw: Collection[str] = frozenset(),
    inpainting: Collection[str] = frozenset(),
) -> None:
    """Pin the image model reference to the given models for the duration of the test.

    The derived sets are rebuilt too, because worker matching and the NSFW countermeasures read
    those rather than the reference itself.

    Args:
        monkeypatch (pytest.MonkeyPatch): The fixture whose teardown restores the real reference.
        models (dict[str, KNOWN_IMAGE_GENERATION_BASELINE | str]): Model name to baseline.
        nsfw (Collection[str]): Which of the model names are NSFW.
        inpainting (Collection[str]): Which of the model names are inpainting models.
    """
    from horde import model_reference as model_reference_module

    reference = {
        model_name: make_image_record(
            model_name,
            baseline,
            nsfw=model_name in nsfw,
            inpainting=model_name in inpainting,
        )
        for model_name, baseline in models.items()
    }

    monkeypatch.setattr(model_reference_module.model_reference, "reference", reference)
    monkeypatch.setattr(model_reference_module.model_reference, "stable_diffusion_names", set(reference))
    monkeypatch.setattr(model_reference_module.model_reference, "nsfw_models", set(nsfw))
