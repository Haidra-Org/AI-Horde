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
schedule against. One elected AI-Horde instance refreshes it hourly and publishes a complete,
versioned snapshot to central Redis. Every API instance reads that same snapshot into local memory.

## Fleet distribution

Redis key `image_model_reference:snapshot:v1` is the fleet's last-known-good image reference. One
document contains the final canonical-plus-pending model view and the baseline catalog, with a SHA-256
revision over both. Replacing that key is atomic, so consumers cannot observe half a publication.

`REDIS_IP` is the authoritative read and lease endpoint. After its fenced transaction succeeds, the
publisher copies the same immutable document to every reachable independent master in
`REDIS_SERVERS`, preserving AI-Horde's active/passive failover topology. Redis key
`image_model_reference:publish_sequence:v1` carries the monotonically increasing generation on each
server; a delayed older publisher cannot regress a passive server. Consumers continue to read only
the current `REDIS_IP`, and the host-local db6 cache is never involved.

At cold start, an instance first reads Redis. When the key is absent, contenders use a fenced Redis
lease and exactly one fetches the remote sources; the others wait for its publication. After startup,
the AI-Horde quorum owner checks once a minute whether the served snapshot is degraded or more than
an hour old and, if so, performs the remote refresh; every instance polls Redis for a new revision. A
newly elected owner therefore takes over publication within a minute rather than a full interval. The
per-host Redis cache is deliberately bypassed for this control-plane value.

A remote, validation, or pending-model failure never clears or replaces the Redis value. Running
instances retain their in-memory snapshot during Redis failure. A process without central Redis
fails startup rather than independently fetching a potentially divergent view. Cold-start losers
continue contending until an abandoned publication lease can expire and one of them takes over; the
effective bootstrap timeout is therefore never shorter than the lease plus five seconds.

When the whole bootstrap window passes with no snapshot published (the PRIMARY is unreachable and
Redis holds nothing), contenders get one further lease-long window in which a publication may be
**degraded**: HMR's GitHub fallback for the canonical models, the baseline catalog packaged with
`horde_model_reference`, and no pending models. The document carries `degraded: true`, every process
that applies it logs an error, and the elected publisher treats a degraded document as always due, so
it keeps retrying (every ten seconds on failure) until the PRIMARY answers and a healthy document
replaces it. A degraded document is never published while any snapshot already exists.

Each process reports `horde.model_reference.snapshot_age` (seconds since the served document was
published) and `horde.model_reference.snapshot_degraded` on every Redis poll, and warns hourly once
the served snapshot is older than three hours, which is three missed publisher cycles. Unknown
top-level fields in a snapshot are ignored with a warning so an older consumer keeps loading a newer
publisher's document during a rolling deploy.

## Where the models come from

The loader drives `horde_model_reference`'s `ModelReferenceManager` in REPLICA mode. Two sources are
merged on every refresh:

- The **canonical** source (`horde`): the PRIMARY service named by
  `HORDE_MODEL_REFERENCE_PRIMARY_API_URL`. HMR may obtain a GitHub fallback internally, but the
  publisher detects that outcome and refuses to replace the fleet's last-known-good Redis snapshot.
- The **pending** source (`pending`): models sitting in the PRIMARY's pending queue. These are the
  beta models. They carry no special restrictions; a pending model is a known model.

Pending is listed ahead of canonical, so a pending record wins a name collision. That is how a model
is revised while it is still in beta.

The publisher constructs HMR with `PrefetchStrategy.NONE`; it does not run HMR's all-category
prefetch. A publication reads only the image baseline export, canonical `image_generation` category,
and (when enabled) pending `image_generation` category. `ASYNC` prefetch is intentionally not used:
it warms every category and requires an asyncio event loop, while this publisher runs from the
synchronous Flask/background-thread lifecycle. Startup rejects an HMR singleton that another
importer previously constructed with a different strategy, rather than silently inheriting `LAZY`.

Text models remain outside this pipeline. AI-Horde continues to read the legacy standalone
AI-Horde-text-model-reference JSON into each process at startup and hourly, dropping any entry without
a numeric parameter count rather than rejecting the payload; HMR's text categories are not queried by
the fleet image publisher.

Adding a model to the PRIMARY needs no change here, and neither does adding a *baseline*: the baseline
catalog is served alongside the models.

The publisher rejects an empty model reference, an empty baseline catalog, or a document whose
content does not match its revision before mutating Redis. If a model names a baseline missing from
the first catalog read, it re-fetches the catalog after fixing the model view to cover PRIMARY's
baseline-before-model publication race.

## The baseline catalog

The same manager serves a catalog of image baselines, exposed as
`model_reference.baseline_record(baseline)` and returning an `ImageBaselineRecord` or `None`. It states
what exists for a model family and what the horde charges for it, and is what
[baseline policy](baseline_policy.md) reads. The hourly refresh re-fetches it from the PRIMARY, so a
baseline published or repriced after this process started is picked up without a deployment. A failed
re-fetch is logged and leaves the complete Redis snapshot serving.

## What the loader exposes

Records stay typed as `ImageGenerationModelRecord` in memory:

| Member | Meaning |
| --- | --- |
| `reference` | `dict[str, ImageGenerationModelRecord]`, keyed by model name. Empty until a fleet snapshot is applied. |
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
| `HORDE_MODEL_REFERENCE_PRIMARY_API_URL` | `https://models.aihorde.net/api` | The PRIMARY the elected publisher reads. Fleet publication requires this service; GitHub fallback is not allowed to replace the last-known-good snapshot. |
| `AIWORKER_CACHE_HOME` | `models` | HMR's on-disk cache on the elected publisher. API consumers do not read it; a relative value resolves against the process working directory. |
| `HORDE_MODEL_REFERENCE_BOOTSTRAP_TIMEOUT` | `195` | Minimum seconds a cold-starting API process waits for the bootstrap publisher to populate Redis. The effective value is extended when necessary so it exceeds the publication lease by five seconds. |
| `HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS` | `180` | Redis publication-lease lifetime. A fetch that outlives its lease is fenced out and cannot overwrite a newer snapshot. |
| `HORDE_BETA_MODEL_CATEGORIES` | `image_generation` | Comma-separated categories to merge pending models into. An empty value disables beta. An unrecognized value is logged and skipped. |
| `HORDE_BETA_MODELS_API_KEY` | `0000000000` | A reader-level AI-Horde key for the pending reads. The PRIMARY accepts the anonymous key. An empty value disables beta. |

`horde_model_reference` reads its own `HORDE_MODEL_REFERENCE_*` settings directly. AI-Horde detects
and rejects HMR's GitHub fallback during fleet publication, because independently replacing a live
PRIMARY revision with the fallback would violate the last-known-good contract.

## Database mirror

`store_known_image_models` mirrors the reference into the `known_image_models` table each cycle.
Columns the record leaves optional but the table declares non-nullable (`version`, `style`, `tags`)
receive empty values, and `features_not_supported` has no counterpart in the record schema, so it is
always cleared.
