# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The in-memory image and text model references the API prices and validates requests against.

The image half reads horde-model-reference in REPLICA mode, overlaid with the PRIMARY's pending
queue (the beta models). The text half still reads the legacy text-reference JSON directly.
"""

from __future__ import annotations

import os
from collections.abc import Collection
from typing import Final

import regex as re
import requests
from horde_model_reference import (
    HORDE_SOURCE_ID,
    MODEL_REFERENCE_CATEGORY,
    PENDING_SOURCE_ID,
    ImageBaselineRecord,
    ModelReferenceManager,
    PendingModelProvider,
    PrefetchStrategy,
    SourceSelector,
    horde_model_reference_settings,
)
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_model_reference.model_reference_records import ImageGenerationModelRecord

from horde.logger import logger
from horde.threads import PrimaryTimedFunction

BETA_CATEGORIES_ENV_VAR: Final[str] = "HORDE_BETA_MODEL_CATEGORIES"
"""Comma-separated categories to merge pending (beta) models into. Empty disables beta."""

BETA_API_KEY_ENV_VAR: Final[str] = "HORDE_BETA_MODELS_API_KEY"
"""A reader-level AI-Horde API key authenticating the pending-model reads."""

DEFAULT_BETA_CATEGORIES: Final[str] = MODEL_REFERENCE_CATEGORY.image_generation.value
"""Image generation is the only category this API reads from the reference."""

ANONYMOUS_API_KEY: Final[str] = "0000000000"
"""The PRIMARY accepts the anonymous key for pending reads, so beta needs no dedicated credential."""

MODEL_REQUIREMENT_VALUE = int | float | str | list[int] | list[float] | list[str] | bool
"""One value a model record may publish as a request requirement."""

MODEL_NAME_BASELINE_SUFFIXES: Final[tuple[tuple[str, KNOWN_IMAGE_GENERATION_BASELINE], ...]] = (
    ("[SDXL]", KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl),
    ("[Flux]", KNOWN_IMAGE_GENERATION_BASELINE.flux_1),
    ("[Qwen]", KNOWN_IMAGE_GENERATION_BASELINE.qwen_image),
    ("[ZModel]", KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo),
    ("[ZImage]", KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo),
)
"""Baselines inferred from a customizer model name the reference has never heard of."""


def _beta_model_categories() -> set[MODEL_REFERENCE_CATEGORY]:
    """Return the categories opted into beta via the environment.

    An unknown value is logged and skipped, so a typo never takes the reference loader down.
    """
    configured_categories = os.getenv(BETA_CATEGORIES_ENV_VAR, DEFAULT_BETA_CATEGORIES)
    categories: set[MODEL_REFERENCE_CATEGORY] = set()
    for token in configured_categories.split(","):
        category_name = token.strip()
        if not category_name:
            continue
        try:
            categories.add(MODEL_REFERENCE_CATEGORY(category_name))
        except ValueError:
            logger.warning(
                f"Ignoring unknown category '{category_name}' in {BETA_CATEGORIES_ENV_VAR}; "
                f"valid values: {[category.value for category in MODEL_REFERENCE_CATEGORY]}",
            )
    return categories


def _build_pending_provider() -> PendingModelProvider | None:
    """Create the pending (beta) model provider from the environment, or None when beta is off.

    Each unconfigured case logs why, because beta silently not appearing is otherwise indistinguishable
    from the PRIMARY having no pending models.
    """
    categories = _beta_model_categories()
    if not categories:
        return None

    apikey = os.getenv(BETA_API_KEY_ENV_VAR, ANONYMOUS_API_KEY)
    if not apikey:
        logger.warning(f"{BETA_CATEGORIES_ENV_VAR} is set but {BETA_API_KEY_ENV_VAR} is empty; skipping beta models.")
        return None

    primary_api_url = horde_model_reference_settings.primary_api_url
    if not primary_api_url:
        logger.warning(
            "Beta models requested but HORDE_MODEL_REFERENCE_PRIMARY_API_URL is unset; "
            "pending models require a PRIMARY service and cannot be served from GitHub.",
        )
        return None

    logger.info(f"Beta models enabled for categories {sorted(c.value for c in categories)} via {primary_api_url}.")
    return PendingModelProvider(primary_api_url=primary_api_url, apikey=apikey, categories=categories)


def _get_reference_manager() -> ModelReferenceManager:
    """Return the process-wide model reference manager, constructing it on first use.

    Its settings lock on first construction, so an instance another importer already built is used
    as-is; it serves read-only queries the same either way.
    """
    if ModelReferenceManager.has_instance():
        return ModelReferenceManager.get_instance()
    try:
        return ModelReferenceManager(prefetch_strategy=PrefetchStrategy.NONE)
    except RuntimeError as err:
        logger.debug(f"Reusing the existing model reference manager: {err}")
        return ModelReferenceManager.get_instance()


def _image_reference_source(manager: ModelReferenceManager) -> SourceSelector:
    """Return the source selector to read the image reference with, registering the beta provider.

    Pending is listed first so a beta record wins a name collision, which is how a model is revised
    before promotion. Beta is additive, so an unbuildable provider degrades to canonical only.
    """
    if MODEL_REFERENCE_CATEGORY.image_generation not in _beta_model_categories():
        return HORDE_SOURCE_ID

    if manager.get_provider(PENDING_SOURCE_ID) is None:
        provider = _build_pending_provider()
        if provider is None:
            return HORDE_SOURCE_ID
        manager.register_provider(provider, replace=True)

    return [PENDING_SOURCE_ID, HORDE_SOURCE_ID]


class ModelReference(PrimaryTimedFunction):
    quorum = None
    _image_snapshot: (
        tuple[
            dict[str, ImageGenerationModelRecord],
            dict[str, ImageBaselineRecord],
        ]
        | None
    ) = None
    text_reference = None
    stable_diffusion_names: set[str] = set()
    text_model_names: set[str] = set()
    nsfw_models: set[str] = set()
    controlnet_models: set[str] = set()
    """Always empty: the reference no longer carries a controlnet model type. Kept for callers."""
    # Workaround because users lacking customizer role are getting models not in the reference stripped away.
    # However due to a racing or caching issue, this causes them to still pick jobs using those models
    # Need to investigate more to remove this workaround
    testing_models = {}
    no_q_regex = re.compile(r"[.,-][a-zA-Z0-9]+?-?Q(-[Ii]nt)?[2-9]{1,2}([_.-][0-9a-zA-Z]+)*")

    @property
    def reference(self) -> dict[str, ImageGenerationModelRecord] | None:
        """Return the model half of the currently published image-reference snapshot."""
        return self._image_snapshot[0] if self._image_snapshot is not None else None

    @reference.setter
    def reference(self, records: dict[str, ImageGenerationModelRecord] | None) -> None:
        """Replace the model half while retaining the catalog, primarily for test fixtures."""
        baselines = self._image_snapshot[1] if self._image_snapshot is not None else {}
        self._image_snapshot = (records or {}, baselines) if records is not None else None

    def call_function(self) -> None:
        """Retrieves to image and text model reference and stores in it a var"""
        # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
        # We don't want to report on any random model name a client might request
        for _attempt in range(10):
            try:
                manager = _get_reference_manager()
                # Fetch both resources into locals before publishing either. Readers then observe one
                # copy-on-write snapshot, never a new model paired with the previous baseline catalog.
                # Refreshing the catalog first also respects the PRIMARY's invariant that a baseline
                # must be published before a model may name it.
                if not manager.refresh_image_baselines():
                    logger.debug("The image baseline catalog was not refreshed; the cached one still serves.")
                baseline_records = manager.image_baseline_store.export().baselines
                source = _image_reference_source(manager)
                image_records = manager.query(MODEL_REFERENCE_CATEGORY.image_generation, source=source).to_list()
                reference = {record.name: record for record in image_records}
                # One pointer assignment publishes the coherent pair. If either fetch or validation
                # above fails, the previous snapshot continues serving unchanged.
                self._image_snapshot = (reference, baseline_records)
                self.stable_diffusion_names = set(reference)
                self.nsfw_models = {name for name, record in reference.items() if record.nsfw}

                break
            except Exception as e:
                logger.error(f"Error when retrieving the image model reference: {e}")

        for _riter in range(10):
            try:
                self.text_reference = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_LLM_REFERENCE",
                        "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json",
                    ),
                    timeout=2,
                ).json()
                # logger.debug(self.reference)
                self.text_model_names = set()
                for model in self.text_reference:
                    self.text_model_names.add(model)
                    if self.text_reference[model].get("nsfw"):
                        self.nsfw_models.add(model)
                break
            except Exception as err:
                logger.error(f"Error when downloading known models list: {err}")

    def get_image_model_names(self) -> set[str]:
        """Return the names of every image model the reference carries."""
        return set(self.reference or {})

    def get_text_model_names(self):
        return set(self.text_reference.keys())

    def get_model_baseline(self, model_name: str) -> KNOWN_IMAGE_GENERATION_BASELINE | str:
        """Return the model's baseline, inferred from its name suffix where the reference has no record."""
        model_record = (self.reference or {}).get(model_name)
        if model_record is not None:
            return model_record.baseline
        for name_suffix, baseline in MODEL_NAME_BASELINE_SUFFIXES:
            if name_suffix in model_name:
                return baseline
        return KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1

    def get_all_model_baselines(self, model_names: Collection[str]) -> set[KNOWN_IMAGE_GENERATION_BASELINE | str]:
        """Return the set of baselines these model names resolve to."""
        return {self.get_model_baseline(model_name) for model_name in model_names}

    def baseline_record(self, baseline: KNOWN_IMAGE_GENERATION_BASELINE | str | None) -> ImageBaselineRecord | None:
        """Return the served record for one baseline, or None where the catalog publishes no such name."""
        snapshot = self._image_snapshot
        if not baseline or snapshot is None:
            return None
        return snapshot[1].get(str(baseline))

    def get_model_requirements(self, model_name: str) -> dict[str, MODEL_REQUIREMENT_VALUE]:
        """Return the model's published request requirements, or an empty mapping where it has none."""
        model_record = (self.reference or {}).get(model_name)
        if model_record is None:
            return {}
        return model_record.requirements or {}

    def get_text_model_multiplier(self, model_name):
        # To avoid doing this calculations all the time
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        if not self.text_reference.get(model_name):
            model_name_no_q = self.no_q_regex.sub("", model_name)
            if model_name_no_q in self.get_text_model_names():
                model_name = model_name_no_q
            else:
                return 1
        multiplier = int(self.text_reference[model_name]["parameters"]) / 1_000_000_000
        logger.debug(f"{model_name} param multiplier: {multiplier}")
        return multiplier

    def has_inpainting_models(self, model_names: Collection[str]) -> bool:
        """Return whether any of these models is an inpainting model."""
        image_reference = self.reference or {}
        for model_name in model_names:
            model_record = image_reference.get(model_name)
            if model_record is not None and model_record.inpainting:
                return True
        return False

    def has_only_inpainting_models(self, model_names: Collection[str]) -> bool:
        """Return whether every one of these models is an inpainting model."""
        if len(model_names) == 0:
            return False
        image_reference = self.reference or {}
        for model_name in model_names:
            model_record = image_reference.get(model_name)
            if model_record is None or not model_record.inpainting:
                return False
        return True

    def is_known_image_model(self, model_name: str) -> bool:
        """Return whether the image reference carries a record under this name."""
        return model_name in self.get_image_model_names()

    def is_known_text_model(self, model_name):
        # If it's a named model, we check if we can find it without the username
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        if model_name in self.get_text_model_names():
            return True
        model_name_no_q = self.no_q_regex.sub("", model_name)
        if model_name_no_q in self.get_text_model_names():
            return True
        return False

    def has_unknown_models(self, model_names: Collection[str]) -> bool:
        """Return whether any of these models is absent from the image reference."""
        if len(model_names) == 0:
            return False
        if any(not self.is_known_image_model(m) for m in model_names):
            return True
        return False

    def has_nsfw_models(self, model_names: Collection[str]) -> bool:
        """Return whether any of these models is flagged NSFW."""
        if len(model_names) == 0:
            return False
        if any(m in model_reference.nsfw_models for m in model_names):
            return True
        # if self.has_unknown_models(model_names):
        #     return True
        return False


model_reference = ModelReference(3600, None)
model_reference.call_function()
