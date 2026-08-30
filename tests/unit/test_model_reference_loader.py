# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify the image model reference loader, its baseline fallbacks, and its database mirror."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from horde_model_reference import (
    MODEL_REFERENCE_CATEGORY,
    PENDING_SOURCE_ID,
    ImageBaselineRecord,
    StaticModelProvider,
)
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_model_reference.model_reference_records import GenericModelRecord, ImageGenerationModelRecord

from horde import model_reference as model_reference_module
from tests.unit.model_reference_seed import make_image_record

pytestmark = pytest.mark.unit

CanonicalView = dict[MODEL_REFERENCE_CATEGORY, dict[str, GenericModelRecord] | None]


class _StubTextResponse:
    """Stand in for the text reference fetch so the loader never touches the network."""

    @staticmethod
    def json() -> dict[str, Any]:
        return {"text-model": {"parameters": 7_000_000_000, "nsfw": False}}

    @staticmethod
    def raise_for_status() -> None:
        return None


@pytest.fixture
def canonical_image_view(monkeypatch: pytest.MonkeyPatch) -> Iterator[CanonicalView]:
    """Serve the canonical source from an in-memory view and neutralize the text fetch.

    Mutating the yielded mapping controls what the canonical source returns.
    """
    manager = model_reference_module._get_reference_manager()
    canonical: CanonicalView = {MODEL_REFERENCE_CATEGORY.image_generation: {}}

    def _canonical_references(overwrite_existing: bool = False, *, safe_mode: bool = False) -> CanonicalView:
        return dict(canonical)

    monkeypatch.setattr(manager, "get_all_model_references_or_none", _canonical_references)
    monkeypatch.setattr(model_reference_module.requests, "get", lambda *args, **kwargs: _StubTextResponse())

    # ``call_function`` writes onto the process-wide reference, so it is captured and put back.
    loader = model_reference_module.model_reference
    previous_image_state = loader._image_state
    previous_text_state = loader._text_state

    yield canonical

    loader._image_state = previous_image_state
    loader._text_state = previous_text_state
    manager.unregister_provider(PENDING_SOURCE_ID)


def _register_pending(records: dict[str, ImageGenerationModelRecord]) -> None:
    """Register the given records as the pending (beta) overlay."""
    manager = model_reference_module._get_reference_manager()
    manager.register_provider(
        StaticModelProvider(PENDING_SOURCE_ID, {MODEL_REFERENCE_CATEGORY.image_generation: dict(records)}),
        replace=True,
    )


def test_pending_record_overrides_canonical_record_of_the_same_name(canonical_image_view: CanonicalView) -> None:
    """A pending model wins a name collision, which is how a model is revised before promotion."""
    canonical_image_view[MODEL_REFERENCE_CATEGORY.image_generation] = {
        "shared_model": make_image_record("shared_model", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1),
        "canonical_only": make_image_record("canonical_only", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl),
    }
    _register_pending(
        {
            "shared_model": make_image_record("shared_model", KNOWN_IMAGE_GENERATION_BASELINE.flux_1),
            "pending_only": make_image_record("pending_only", KNOWN_IMAGE_GENERATION_BASELINE.qwen_image, nsfw=True),
        },
    )

    model_reference_module.model_reference.call_function()
    reference = model_reference_module.model_reference

    assert set(reference.reference or {}) == {"shared_model", "canonical_only", "pending_only"}
    assert reference.stable_diffusion_names == {"shared_model", "canonical_only", "pending_only"}
    assert reference.get_model_baseline("shared_model") == KNOWN_IMAGE_GENERATION_BASELINE.flux_1
    assert reference.nsfw_models == {"pending_only"}
    assert reference.controlnet_models == set()


def test_arbitrary_baseline_string_is_returned_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record's baseline is passed through verbatim, never coerced to the enum or to a default."""
    future_record = ImageGenerationModelRecord(
        name="future_model",
        baseline="some_future_baseline",
        nsfw=False,
        inpainting=False,
    )
    monkeypatch.setattr(model_reference_module.model_reference, "reference", {"future_model": future_record})

    assert model_reference_module.model_reference.get_model_baseline("future_model") == "some_future_baseline"
    assert model_reference_module.model_reference.get_all_model_baselines(["future_model"]) == {
        "some_future_baseline",
    }


def test_a_pending_record_with_an_unknown_baseline_reaches_the_loaded_reference(
    canonical_image_view: CanonicalView,
) -> None:
    """A baseline this deployment's vocabulary predates does not keep its model out of the reference."""
    canonical_image_view[MODEL_REFERENCE_CATEGORY.image_generation] = {}
    _register_pending({"future_model": make_image_record("future_model", "some_future_baseline")})

    model_reference_module.model_reference.call_function()
    reference = model_reference_module.model_reference

    assert "future_model" in reference.stable_diffusion_names
    assert reference.get_model_baseline("future_model") == "some_future_baseline"


def test_models_and_baselines_are_published_as_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    canonical_image_view: CanonicalView,
) -> None:
    """A model naming a newly served baseline never becomes visible without that baseline record."""
    baseline = ImageBaselineRecord(name="future_baseline")
    canonical_image_view[MODEL_REFERENCE_CATEGORY.image_generation] = {
        "future_model": make_image_record("future_model", baseline.name),
    }
    manager = model_reference_module._get_reference_manager()
    monkeypatch.setattr(manager, "refresh_image_baselines", lambda: True)
    monkeypatch.setattr(manager.image_baseline_store, "export", lambda: SimpleNamespace(baselines={baseline.name: baseline}))

    model_reference_module.model_reference.call_function()

    assert model_reference_module.model_reference.get_model_baseline("future_model") == baseline.name
    assert model_reference_module.model_reference.baseline_record(baseline.name) == baseline


def test_failed_model_fetch_keeps_the_previous_complete_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refreshing the catalog alone cannot leak a half-new reference to request readers."""
    loader = model_reference_module.model_reference
    previous_state = loader._image_state
    manager = model_reference_module._get_reference_manager()
    future_baseline = ImageBaselineRecord(name="not_published")
    monkeypatch.setattr(manager, "refresh_image_baselines", lambda: True)
    monkeypatch.setattr(
        manager.image_baseline_store,
        "export",
        lambda: SimpleNamespace(baselines={future_baseline.name: future_baseline}),
    )
    monkeypatch.setattr(manager, "query", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fetch failed")))
    monkeypatch.setattr(model_reference_module.requests, "get", lambda *args, **kwargs: _StubTextResponse())

    loader.call_function()

    assert loader._image_state is previous_state
    assert loader.baseline_record(future_baseline.name) is None


def test_invalid_text_refresh_retains_complete_last_known_good(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = model_reference_module.ModelReference()
    previous = {"known-text-model": {"parameters": 7_000_000_000, "nsfw": True}}
    loader.text_reference = previous

    class InvalidResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> list[object]:
            return []

    monkeypatch.setattr(model_reference_module.requests, "get", lambda *args, **kwargs: InvalidResponse())

    loader.refresh_text_reference()

    assert loader.text_reference == previous
    assert loader.text_model_names == {"known-text-model"}
    assert loader.nsfw_models == {"known-text-model"}


def test_text_refresh_removes_retired_names_and_stale_nsfw_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = model_reference_module.ModelReference()
    loader.text_reference = {
        "retired-text-model": {"parameters": 7_000_000_000, "nsfw": True},
        "retained-text-model": {"parameters": 7_000_000_000, "nsfw": True},
    }

    class ReplacementResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, dict[str, object]]:
            return {"retained-text-model": {"parameters": 7_000_000_000, "nsfw": False}}

    monkeypatch.setattr(model_reference_module.requests, "get", lambda *args, **kwargs: ReplacementResponse())

    loader.refresh_text_reference()

    assert loader.text_model_names == {"retained-text-model"}
    assert loader.nsfw_models == set()


def test_empty_initial_text_state_is_safe() -> None:
    loader = model_reference_module.ModelReference()

    assert loader.get_text_model_names() == set()
    assert loader.get_text_model_multiplier("unknown-text-model") == 1


@pytest.mark.parametrize(
    ("model_name", "expected_baseline"),
    [
        ("Some Finetune [SDXL]", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl),
        ("Some Finetune [Flux]", KNOWN_IMAGE_GENERATION_BASELINE.flux_1),
        ("Some Finetune [Qwen]", KNOWN_IMAGE_GENERATION_BASELINE.qwen_image),
        ("Some Finetune [ZModel]", KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo),
        ("Some Finetune [ZImage]", KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo),
        ("Some Finetune", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1),
    ],
)
def test_baseline_falls_back_to_the_name_suffix(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    expected_baseline: KNOWN_IMAGE_GENERATION_BASELINE,
) -> None:
    """A customizer model absent from the reference is priced from the suffix its name declares."""
    monkeypatch.setattr(model_reference_module.model_reference, "reference", {})

    assert model_reference_module.model_reference.get_model_baseline(model_name) == expected_baseline


def test_record_mirrors_onto_the_database_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every non-nullable column receives a value even when the record leaves the field unset."""
    from horde.classes.stable import known_image_models

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(known_image_models, "add_known_image_model", _capture)

    record = ImageGenerationModelRecord(
        name="mirrored_model",
        baseline=KNOWN_IMAGE_GENERATION_BASELINE.qwen_image,
        nsfw=True,
        requirements={"max_steps": 30},
        size_on_disk_bytes=1234,
    )
    known_image_models.add_known_image_model_from_record(record, defer_commit=True)

    assert captured["name"] == "mirrored_model"
    assert captured["baseline"] == "qwen_image"
    assert captured["inpainting"] is False
    assert captured["version"] == ""
    assert captured["style"] == ""
    assert captured["tags"] == []
    assert captured["nsfw"] is True
    assert captured["requirements"] == {"max_steps": 30}
    assert captured["features_not_supported"] is None
    assert captured["size_on_disk_bytes"] == 1234
    assert captured["config"] == record.config.model_dump(mode="json")
    assert captured["defer_commit"] is True
