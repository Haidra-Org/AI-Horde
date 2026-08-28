---
title: "Image model reference loader reference"
summary: "Where the image model reference comes from, how beta (pending) models are merged over it, the environment that configures both, and what the loader exposes."
topics: [generation, workers]
order: 90
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Image model reference loader reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

`horde.model_reference` holds the set of image models this service will price, validate, and
schedule against. It refreshes hourly and is the only place the model vocabulary enters the backend.

## Where the models come from

The loader drives `horde_model_reference`'s `ModelReferenceManager` in REPLICA mode. Two sources are
merged on every refresh:

- The **canonical** source (`horde`): the PRIMARY service named by
  `HORDE_MODEL_REFERENCE_PRIMARY_API_URL`, with a GitHub fallback when the PRIMARY is unreachable.
- The **pending** source (`pending`): models sitting in the PRIMARY's pending queue. These are the
  beta models. They carry no special restrictions; a pending model is a known model.

Pending is listed ahead of canonical, so a pending record wins a name collision. That is how a model
is revised while it is still in beta.

Adding a model to the PRIMARY needs no change here, and neither does adding a *baseline*: the baseline
catalog is served alongside the models.

## The baseline catalog

The same manager serves a catalog of image baselines, exposed as
`model_reference.baseline_record(baseline)` and returning an `ImageBaselineRecord` or `None`. It states
what exists for a model family and what the horde charges for it, and is what
[baseline policy](baseline_policy.md) reads. The hourly refresh re-fetches it from the PRIMARY, so a
baseline published or repriced after this process started is picked up without a deployment. A failed
re-fetch is logged and leaves the cached catalog serving; a replica that has never reached the PRIMARY
falls back to the catalog packaged with `horde_model_reference`.

## What the loader exposes

Records stay typed as `ImageGenerationModelRecord` in memory:

| Member | Meaning |
| --- | --- |
| `reference` | `dict[str, ImageGenerationModelRecord]`, keyed by model name. `None` until the first successful fetch. |
| `stable_diffusion_names` | Every image model name. No baseline allowlist is applied. |
| `nsfw_models` | Image and text models flagged NSFW. |
| `controlnet_models` | Always empty. The reference no longer carries a controlnet model type; the attribute is kept for callers. |
| `get_model_baseline(name)` | The record's baseline, verbatim. |
| `get_all_model_baselines(names)` | The set of baselines those names resolve to. |
| `get_model_requirements(name)` | The record's requirements, or `{}`. |
| `baseline_record(baseline)` | The served baseline record, or `None` where the catalog has no such name. |

A model the reference has never heard of still resolves to a baseline, from the suffix its name
declares: `[SDXL]`, `[Flux]`, `[Qwen]`, `[ZModel]` and `[ZImage]`, falling through to
`stable_diffusion_1`. Customizer-role users can request such a name, and it must still be priced. An
uncatalogued baseline is accepted for plain generation but receives no baseline-dependent workflow
capabilities by default.

A refresh stages the baseline catalog and model records before publishing them as one copy-on-write
snapshot, so request readers never see a new model paired with the previous catalog. If either side
fails to fetch or validate, the previous complete snapshot keeps serving traffic.

## Environment

| Variable | Default | Effect |
| --- | --- | --- |
| `HORDE_MODEL_REFERENCE_PRIMARY_API_URL` | `https://models.aihorde.net/api` | The PRIMARY the canonical and pending reads go to. Unset it to read GitHub only, which also disables beta. |
| `AIWORKER_CACHE_HOME` | `models` | Root of the on-disk reference cache. A relative value resolves against the process working directory, so a deployment should set it to an absolute path outside the checkout. |
| `HORDE_BETA_MODEL_CATEGORIES` | `image_generation` | Comma-separated categories to merge pending models into. An empty value disables beta. An unrecognized value is logged and skipped. |
| `HORDE_BETA_MODELS_API_KEY` | `0000000000` | A reader-level AI-Horde key for the pending reads. The PRIMARY accepts the anonymous key. An empty value disables beta. |

`horde_model_reference` reads its own `HORDE_MODEL_REFERENCE_*` settings directly, including the
cache lifetime and the GitHub fallback toggle. There is no AI-Horde-side override of the reference
URL.

## Database mirror

`store_known_image_models` mirrors the reference into the `known_image_models` table each cycle.
Columns the record leaves optional but the table declares non-nullable (`version`, `style`, `tags`)
receive empty values, and `features_not_supported` has no counterpart in the record schema, so it is
always cleared.
