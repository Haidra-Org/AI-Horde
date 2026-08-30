# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The in-memory image and text model references the API prices and validates requests against.

The image half reads horde-model-reference in REPLICA mode, overlaid with the PRIMARY's pending
queue (the beta models). The text half still reads the legacy text-reference JSON directly.
"""

from __future__ import annotations

import os
import time
from collections.abc import Collection
from dataclasses import dataclass
from importlib.resources import files
from typing import Final

import regex as re
import requests
from horde_model_reference import (
    HORDE_SOURCE_ID,
    MODEL_REFERENCE_CATEGORY,
    PENDING_SOURCE_ID,
    ImageBaselineCatalog,
    ImageBaselineRecord,
    ModelReferenceManager,
    PendingModelProvider,
    PrefetchStrategy,
    SourceSelector,
    horde_model_reference_settings,
)
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from horde_model_reference.model_reference_records import ImageGenerationModelRecord

from horde import metrics
from horde.logger import logger  # type: ignore[attr-defined]
from horde.model_reference_snapshot import (
    DEFAULT_PUBLISH_LOCK_SECONDS,
    ImageReferenceSnapshot,
    build_snapshot,
    redis_image_reference_snapshots,
)
from horde.vars import horde_instance_id

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

STALE_SNAPSHOT_SECONDS: Final[int] = 3 * 3600
"""Snapshot age past which a process warns; the publisher refreshes hourly, so this is three missed cycles."""

STALE_WARNING_INTERVAL_SECONDS: Final[int] = 3600
"""Minimum spacing between repeated stale-snapshot warnings from one process."""


def _packaged_baseline_catalog() -> ImageBaselineCatalog:
    """Return the baseline catalog shipped inside horde_model_reference."""
    return ImageBaselineCatalog.model_validate_json(
        files("horde_model_reference").joinpath("data", "baselines", "catalog.json").read_text(encoding="utf-8"),
    )


@dataclass(frozen=True)
class _ImageReferenceState:
    models: dict[str, ImageGenerationModelRecord]
    baselines: dict[str, ImageBaselineRecord]
    model_names: frozenset[str]
    nsfw_model_names: frozenset[str]
    revision: str | None
    published_at: int | None = None
    degraded: bool = False


@dataclass(frozen=True)
class _TextReferenceState:
    models: dict[str, dict[str, object]]
    model_names: frozenset[str]
    nsfw_model_names: frozenset[str]


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

    AI-Horde must own first construction: silently accepting HMR's default LAZY strategy would leave
    an all-category warm-up available to a later manager-level read.
    """
    if ModelReferenceManager.has_instance():
        manager = ModelReferenceManager.get_instance()
        if manager.prefetch_strategy is not PrefetchStrategy.NONE:
            raise RuntimeError(
                "HMR was initialized before AI-Horde with "
                f"PrefetchStrategy.{manager.prefetch_strategy.name}; expected PrefetchStrategy.NONE.",
            )
        return manager
    try:
        return ModelReferenceManager(prefetch_strategy=PrefetchStrategy.NONE)
    except RuntimeError as err:
        # A racing importer may have won singleton construction after has_instance().
        manager = ModelReferenceManager.get_instance()
        if manager.prefetch_strategy is not PrefetchStrategy.NONE:
            raise RuntimeError("HMR must be initialized with PrefetchStrategy.NONE in AI-Horde.") from err
        return manager


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


class ModelReference:
    quorum = None
    # Workaround because users lacking customizer role are getting models not in the reference stripped away.
    # However due to a racing or caching issue, this causes them to still pick jobs using those models
    # Need to investigate more to remove this workaround
    testing_models: dict[str, object] = {}
    no_q_regex = re.compile(r"[.,-][a-zA-Z0-9]+?-?Q(-[Ii]nt)?[2-9]{1,2}([_.-][0-9a-zA-Z]+)*")

    def __init__(self) -> None:
        """Start with HMR's packaged policy until production initialization loads Redis."""
        self._last_stale_warning: float | None = None
        self._image_state = _ImageReferenceState(
            models={},
            baselines=dict(_packaged_baseline_catalog().baselines),
            model_names=frozenset(),
            nsfw_model_names=frozenset(),
            revision=None,
        )
        self._text_state = _TextReferenceState(models={}, model_names=frozenset(), nsfw_model_names=frozenset())

    @property
    def _image_snapshot(
        self,
    ) -> tuple[dict[str, ImageGenerationModelRecord], dict[str, ImageBaselineRecord]]:
        """Compatibility view for existing policy helpers and isolated test fixtures."""
        state = self._image_state
        return state.models, state.baselines

    @_image_snapshot.setter
    def _image_snapshot(
        self,
        snapshot: tuple[dict[str, ImageGenerationModelRecord], dict[str, ImageBaselineRecord]] | None,
    ) -> None:
        models, baselines = snapshot if snapshot is not None else ({}, {})
        self._image_state = _ImageReferenceState(
            models=models,
            baselines=baselines,
            model_names=frozenset(models),
            nsfw_model_names=frozenset(name for name, record in models.items() if record.nsfw),
            revision=None,
        )

    @property
    def _snapshot_revision(self) -> str | None:
        return self._image_state.revision

    @property
    def reference(self) -> dict[str, ImageGenerationModelRecord] | None:
        """Return the model half of the currently published image-reference snapshot."""
        return self._image_state.models

    @reference.setter
    def reference(self, records: dict[str, ImageGenerationModelRecord] | None) -> None:
        """Replace the model half while retaining the catalog, primarily for test fixtures."""
        self._image_snapshot = (records or {}, self._image_state.baselines)

    @property
    def stable_diffusion_names(self) -> frozenset[str]:
        return self._image_state.model_names

    @property
    def controlnet_models(self) -> frozenset[str]:
        """Always empty: the reference no longer carries a controlnet model type."""
        return frozenset()

    @property
    def text_reference(self) -> dict[str, dict[str, object]]:
        return self._text_state.models

    @text_reference.setter
    def text_reference(self, records: dict[str, dict[str, object]] | None) -> None:
        models = records or {}
        self._text_state = _TextReferenceState(
            models=models,
            model_names=frozenset(models),
            nsfw_model_names=frozenset(name for name, record in models.items() if record.get("nsfw")),
        )

    @property
    def text_model_names(self) -> frozenset[str]:
        return self._text_state.model_names

    @property
    def nsfw_models(self) -> frozenset[str]:
        return self._image_state.nsfw_model_names | self._text_state.nsfw_model_names

    def _apply_snapshot(self, snapshot: ImageReferenceSnapshot) -> bool:
        """Publish one Redis revision into this process, returning whether it changed."""
        current = self._image_state
        if snapshot.revision == current.revision and snapshot.degraded == current.degraded:
            return False
        reference = dict(snapshot.models)
        baselines = dict(snapshot.baselines)
        self._image_state = _ImageReferenceState(
            models=reference,
            baselines=baselines,
            model_names=frozenset(reference),
            nsfw_model_names=frozenset(name for name, record in reference.items() if record.nsfw),
            revision=snapshot.revision,
            published_at=snapshot.published_at,
            degraded=snapshot.degraded,
        )
        if snapshot.degraded:
            logger.error(
                f"Serving a degraded fleet image-reference snapshot {snapshot.revision} built from fallback sources; "
                "the PRIMARY was unreachable when it was published.",
            )
        else:
            logger.info(
                f"Loaded fleet image-reference snapshot {snapshot.revision} ({len(reference)} models, {len(baselines)} baselines).",
            )
        return True

    def _report_snapshot_health(self) -> None:
        """Expose the served snapshot's age and degraded state, warning when the publisher has gone quiet."""
        state = self._image_state
        metrics.model_reference_snapshot_degraded.set(int(state.degraded))
        if state.published_at is None:
            return
        age = max(0, int(time.time()) - state.published_at)
        metrics.model_reference_snapshot_age_seconds.set(age)
        if age < STALE_SNAPSHOT_SECONDS:
            return
        now = time.monotonic()
        if self._last_stale_warning is None or now - self._last_stale_warning >= STALE_WARNING_INTERVAL_SECONDS:
            self._last_stale_warning = now
            logger.warning(
                f"The fleet image-reference snapshot {state.revision} is {age} seconds old; the elected publisher has not refreshed it.",
            )

    def refresh_from_redis(self, *, log_missing: bool = True) -> bool:
        """Load the central fleet snapshot, retaining local state on any Redis failure."""
        try:
            snapshot = redis_image_reference_snapshots.load()
        except Exception as err:
            logger.error(f"Failed to load the fleet image-reference snapshot from Redis: {err}")
            return False
        if snapshot is None:
            if log_missing:
                logger.warning("The fleet image-reference snapshot is absent from central Redis.")
            return False
        changed = self._apply_snapshot(snapshot)
        self._report_snapshot_health()
        return changed

    @staticmethod
    def _records_from_payload(payload: dict[str, object]) -> dict[str, ImageGenerationModelRecord]:
        """Validate a PRIMARY category response into its typed HMR records."""
        records: dict[str, ImageGenerationModelRecord] = {}
        for name, fields in payload.items():
            if not isinstance(fields, dict):
                raise ValueError(f"Image model {name!r} is not an object")
            records[name] = ImageGenerationModelRecord.model_validate({**fields, "name": name})
        return records

    def _fetch_fleet_snapshot(self, *, allow_degraded: bool = False) -> ImageReferenceSnapshot:
        """Fetch and validate the complete AI-Horde view without publishing partial state.

        With ``allow_degraded`` a PRIMARY outage substitutes fallback sources (HMR's GitHub
        mirror, the packaged baseline catalog, no pending models) and marks the document
        degraded. That is reserved for a fleet with no snapshot at all: replacing a live
        PRIMARY revision with fallback data would violate the last-known-good contract.
        """
        degraded = False
        manager = _get_reference_manager()
        backend = manager.backend
        fetch_baselines = getattr(backend, "fetch_image_baseline_export", None)
        if fetch_baselines is None:
            raise RuntimeError("The configured HMR backend cannot fetch the PRIMARY baseline export.")
        baseline_payload = fetch_baselines()
        if baseline_payload is None:
            if not allow_degraded:
                raise RuntimeError("The HMR PRIMARY did not return an image baseline export.")
            logger.error("The HMR PRIMARY did not return an image baseline export; using the packaged catalog.")
            degraded = True
            baseline_catalog = _packaged_baseline_catalog()
        else:
            baseline_catalog = ImageBaselineCatalog.model_validate(baseline_payload)
        if not baseline_catalog.baselines:
            raise RuntimeError("The HMR PRIMARY returned an empty image baseline export.")

        before_stats = backend.get_statistics()
        canonical_payload = backend.fetch_category(
            MODEL_REFERENCE_CATEGORY.image_generation,
            force_refresh=True,
        )
        after_stats = backend.get_statistics()
        if after_stats.get("github_fallbacks", 0) > before_stats.get("github_fallbacks", 0):
            if not allow_degraded:
                raise RuntimeError("The HMR PRIMARY was unavailable; refusing to replace Redis with the GitHub fallback.")
            logger.error("The HMR PRIMARY was unavailable; bootstrapping the fleet from the GitHub fallback.")
            degraded = True
        if canonical_payload is None:
            raise RuntimeError("The HMR PRIMARY did not return the canonical image reference.")
        canonical_records = self._records_from_payload(canonical_payload)
        if not canonical_records:
            raise RuntimeError("The HMR PRIMARY returned an empty canonical image reference.")

        reference = dict(canonical_records)
        beta_categories = _beta_model_categories()
        source = _image_reference_source(manager)
        if source != HORDE_SOURCE_ID:
            pending_provider = manager.get_provider(PENDING_SOURCE_ID)
            if pending_provider is None:
                raise RuntimeError("Beta models are enabled but the pending provider was not registered.")
            pending = pending_provider.fetch_category(
                MODEL_REFERENCE_CATEGORY.image_generation,
                force_refresh=True,
            )
            if pending is None:
                if not allow_degraded:
                    raise RuntimeError("The PRIMARY pending-model read failed; retaining the previous fleet snapshot.")
                logger.error("The PRIMARY pending-model read failed; bootstrapping without beta models.")
                degraded = True
                pending = {}
            # Pending wins name collisions, matching the request-visible beta semantics.
            for name, record in pending.items():
                reference[name] = ImageGenerationModelRecord.model_validate(record)

        missing_baselines = {str(record.baseline) for record in reference.values()} - baseline_catalog.baselines.keys()
        if missing_baselines and not degraded:
            # PRIMARY publishes a baseline before its model, but those resources are read separately.
            # Re-read the catalog after the model view is fixed so a publication between our first
            # and second request cannot expose its model without its newly published policy.
            refreshed_payload = fetch_baselines()
            if refreshed_payload is None:
                if not allow_degraded:
                    raise RuntimeError(
                        "The HMR PRIMARY did not return an image baseline export after a model/catalog race.",
                    )
                logger.error("The HMR PRIMARY stopped answering during bootstrap; keeping the first catalog read.")
                degraded = True
            else:
                baseline_catalog = ImageBaselineCatalog.model_validate(refreshed_payload)
                if not baseline_catalog.baselines:
                    raise RuntimeError(
                        "The HMR PRIMARY returned an empty image baseline export after a model/catalog race.",
                    )

        return build_snapshot(
            models=reference,
            baselines=dict(baseline_catalog.baselines),
            publisher=horde_instance_id,
            primary_api_url=horde_model_reference_settings.primary_api_url,
            beta_categories={category.value for category in beta_categories},
            degraded=degraded,
        )

    def publish_fleet_snapshot(self, *, allow_degraded: bool = False) -> bool:
        """Refresh the remote reference once and atomically distribute it through Redis."""
        try:
            snapshot = redis_image_reference_snapshots.publish(
                lambda: self._fetch_fleet_snapshot(allow_degraded=allow_degraded),
            )
        except Exception as err:
            logger.error(f"Failed to publish the fleet image-reference snapshot: {err}")
            return False
        if snapshot is None:
            return False
        self._apply_snapshot(snapshot)
        return True

    def _refresh_image_direct(self) -> None:
        """Retain the old direct loader only for isolated SQLite test processes."""
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
                missing_baselines = {str(record.baseline) for record in image_records} - baseline_records.keys()
                if missing_baselines:
                    # The PRIMARY may publish a baseline and then its model between our two HTTP
                    # reads. Re-read the catalog after the model set is fixed; publication order
                    # guarantees that the second catalog contains every legitimate model baseline.
                    # A failed retry retains the cached catalog and its conservative uncatalogued
                    # behavior; availability does not depend on both remote reads succeeding in the
                    # same cycle.
                    if manager.refresh_image_baselines():
                        baseline_records = manager.image_baseline_store.export().baselines
                reference = {record.name: record for record in image_records}
                # One pointer assignment publishes the coherent pair. If either fetch or validation
                # above fails, the previous snapshot continues serving unchanged.
                self._image_snapshot = (reference, baseline_records)

                break
            except Exception as e:
                logger.error(f"Error when retrieving the image model reference: {e}")

    def refresh_text_reference(self) -> None:
        """Refresh the legacy text reference, which is outside the HMR image snapshot."""
        for _riter in range(10):
            try:
                response = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_LLM_REFERENCE",
                        "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json",
                    ),
                    timeout=2,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("The text model reference is not an object.")
                staged: dict[str, dict[str, object]] = {}
                for name, record in payload.items():
                    if not isinstance(name, str) or not isinstance(record, dict):
                        raise ValueError(f"Invalid text model reference entry: {name!r}")
                    parameters = record.get("parameters")
                    if not isinstance(parameters, int | float | str):
                        raise ValueError(f"Text model {name!r} has no numeric parameter count.")
                    try:
                        int(parameters)
                    except (TypeError, ValueError) as err:
                        raise ValueError(f"Text model {name!r} has an invalid parameter count.") from err
                    staged[name] = record
                if not staged:
                    raise ValueError("Refusing to replace the text model reference with an empty response.")
                self.text_reference = staged
                break
            except Exception as err:
                logger.error(f"Error when downloading known models list: {err}")

    def call_function(self) -> None:
        """Perform the legacy direct refresh used by isolated tests and tools."""
        self._refresh_image_direct()
        self.refresh_text_reference()

    def initialize(self) -> None:
        """Synchronously acquire a fleet snapshot before this process serves requests."""
        if not redis_image_reference_snapshots.is_available():
            raise RuntimeError("Central Redis is unavailable; cannot initialize the fleet image reference.")
        if not self.refresh_from_redis():
            lease_seconds = int(
                os.getenv("HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS", str(DEFAULT_PUBLISH_LOCK_SECONDS)),
            )
            if lease_seconds <= 0:
                raise ValueError("HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS must be greater than zero.")
            configured_timeout = float(os.getenv("HORDE_MODEL_REFERENCE_BOOTSTRAP_TIMEOUT", "195"))
            if configured_timeout <= 0:
                raise ValueError("HORDE_MODEL_REFERENCE_BOOTSTRAP_TIMEOUT must be greater than zero.")
            bootstrap_timeout = max(configured_timeout, lease_seconds + 5)
            if bootstrap_timeout != configured_timeout:
                logger.warning(
                    "Extending the image-reference bootstrap timeout beyond its configured value so an abandoned "
                    "publication lease can expire and a waiting instance can take over.",
                )
            deadline = time.monotonic() + bootstrap_timeout
            # During a fleet-wide cold start, exactly one contender fetches. Losers keep
            # contending so one can recover after an abandoned lease expires.
            self._contend_for_bootstrap(deadline)
            if self._snapshot_revision is None:
                # The PRIMARY stayed unreachable for the whole window and nobody published.
                # A fleet with no reference at all is worse than one on fallback data, so allow
                # one more lease-long window in which a contender may publish a degraded document.
                logger.error(
                    "No fleet image-reference snapshot was published within the bootstrap window; "
                    "attempting a degraded bootstrap from fallback sources.",
                )
                self._contend_for_bootstrap(time.monotonic() + lease_seconds + 5, allow_degraded=True)
            if self._snapshot_revision is None:
                raise RuntimeError("No fleet image-reference snapshot is available in central Redis.")
        self._report_snapshot_health()
        self.refresh_text_reference()

    def _contend_for_bootstrap(self, deadline: float, *, allow_degraded: bool = False) -> None:
        while self._snapshot_revision is None and time.monotonic() < deadline:
            if self.publish_fleet_snapshot(allow_degraded=allow_degraded) or self.refresh_from_redis(log_missing=False):
                return
            time.sleep(1)

    def get_image_model_names(self) -> set[str]:
        """Return the names of every image model the reference carries."""
        return set(self.reference or {})

    def get_text_model_names(self) -> set[str]:
        return set(self._text_state.model_names)

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

    def get_text_model_multiplier(self, model_name: str) -> float:
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
        parameters = self.text_reference[model_name].get("parameters")
        if not isinstance(parameters, int | float | str):
            logger.warning(f"Text model {model_name!r} has no numeric parameter count; using multiplier 1.")
            return 1
        multiplier = int(parameters) / 1_000_000_000
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

    def is_known_text_model(self, model_name: str) -> bool:
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
        if any(m in self.nsfw_models for m in model_names):
            return True
        # if self.has_unknown_models(model_names):
        #     return True
        return False


model_reference = ModelReference()
