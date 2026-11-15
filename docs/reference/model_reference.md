---
title: "Image model reference loader"
summary: "Where the image model reference comes from, how beta (pending) models are merged over it, the environment that configures both, and what the loader exposes."
topics: [generation, workers]
order: 90
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Image model reference loader

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

`horde.model_reference` provides image model records for request validation, pricing and scheduling. One
elected Horde API replica refreshes it hourly and publishes a complete, versioned snapshot to central Redis. Every Horde API
replica reads that same snapshot into local memory. [Fleet distribution](#fleet-distribution) covers how the snapshot
is published and kept current.

## Where the models come from

The image model reference is the dataset. `horde_model_reference` (HMR) is the dependency package that reads it,
and `horde.model_reference` is this repository's loader. The loader runs HMR's `ModelReferenceManager` in REPLICA
mode, as a read-only consumer of the PRIMARY model-reference service. Two sources are merged on every
refresh:

- The **canonical** source (`horde`): the PRIMARY service at `HORDE_MODEL_REFERENCE_PRIMARY_API_URL`. HMR may fall
  back to GitHub internally. The publisher detects that outcome and does not replace the fleet's last-known-good Redis
  snapshot.
- The **pending** source (`pending`): models in the PRIMARY's pending queue. These are the beta models. They do not
  have special restrictions. A pending model is a known model.

Pending is listed ahead of canonical, so a pending record takes precedence in a name collision. That is how a model is
revised while it is still in beta.

### Fetch scope and validation

The publisher constructs HMR with `PrefetchStrategy.NONE` to fetch only the categories needed by the image reference. A publication
reads only the image baseline export, the canonical `image_generation` category, and (when enabled) the pending
`image_generation` category. `ASYNC` prefetch would load every category and require an asyncio event loop, while
this publisher runs in synchronous Flask code and background threads. Startup rejects an HMR singleton created
with a different strategy, preventing an earlier import from changing the publisher's fetch behaviour.

Text models stay outside this pipeline. AI-Horde reads the legacy standalone AI-Horde-text-model-reference JSON into
each process at startup and hourly. Entries without a numeric parameter count are dropped, and the rest of the payload
is still accepted. The fleet image publisher does not query HMR's text categories.

Models and baselines added to the PRIMARY become available through refreshes. They do not require an AI-Horde code
change, since the same publication includes both the model records and the baseline catalog.

Before it changes Redis, the publisher rejects an empty model reference, an empty baseline catalog, or a document
whose content does not match its revision.

If a model references a baseline that was missing from the first catalog read,
the publisher fetches the catalog again after fixing the model view. This covers a race in the PRIMARY's publication
order, where a baseline is published before the model that uses it.

## The baseline catalog

The same manager serves a catalog of image baselines, exposed as `model_reference.baseline_record(baseline)`, which
returns an `ImageBaselineRecord` or `None`. The catalog states what exists for a model family and what the horde
charges for it. It is the source for [baseline policy](baseline_policy.md). The hourly refresh fetches it again from
the PRIMARY, so a baseline published or repriced after this process started is picked up without a deployment.

## What the loader exposes

Records stay typed as `ImageGenerationModelRecord` in memory:

| Member | Meaning |
| --- | --- |
| `reference` | `dict[str, ImageGenerationModelRecord]`, keyed by model name. Empty until a fleet snapshot is applied. |
| `stable_diffusion_names` | Every image model name. A baseline allowlist is not applied. |
| `nsfw_models` | Image and text models flagged NSFW. |
| `controlnet_models` | Always empty. The reference does not include a ControlNet model type. The attribute is kept for callers. |
| `get_model_baseline(name)` | The record's baseline, verbatim. |
| `get_all_model_baselines(names)` | The set of baselines that those names resolve to. |
| `get_model_requirements(name)` | The record's requirements, or `{}`. |
| `baseline_record(baseline)` | The served baseline record, or `None` when the catalog does not include that baseline. |

A model absent from the reference still resolves to a baseline from its name's suffix: `[SDXL]`, `[Flux]`, `[Qwen]`,
`[ZModel]` and `[ZImage]`, falling back to `stable_diffusion_1`. Customizer-role users can request such a name, and it
must still be priced. An uncatalogued baseline is accepted for plain generation, but by default it does not receive any
baseline-dependent workflow capabilities.

A refresh stages the baseline catalog and the model records, then publishes them as one copy-on-write snapshot. A
request therefore cannot read a new model paired with the previous catalog. If either side fails to fetch or validate,
the previous complete snapshot keeps serving traffic.

## Fleet distribution

Redis key `image_model_reference:snapshot:v1` holds the fleet's last-known-good image reference. One document contains
the final model view (canonical plus pending) and the baseline catalog, with a SHA-256 revision over both. Replacing
that key is atomic, so consumers cannot read half a publication.

`REDIS_IP` is the authoritative endpoint for reads and for the lease. After its fenced transaction succeeds, the
publisher copies the same immutable document to every reachable independent master in `REDIS_SERVERS`. This keeps
AI-Horde's active/passive failover topology. Redis key `image_model_reference:publish_sequence:v1` stores the
monotonically increasing generation on each server. A delayed older publisher cannot regress a passive server.
Consumers read only the current `REDIS_IP`. The host-local cache in Redis database 6 is bypassed for this shared
configuration value, so each replica reads the authoritative snapshot.

At cold start, a Horde API replica reads Redis first. When the key is absent, the starting Horde API replicas contend for a fenced
Redis lease, and exactly one of them fetches the remote sources. The others wait for its publication.

After startup, the AI-Horde quorum owner checks once a minute whether the served snapshot is degraded or more than an
hour old. If it is, the owner performs the remote refresh. Every Horde API replica polls Redis for a new revision every five
seconds. A newly elected owner therefore takes over publication within a minute, without waiting a full interval.

A remote, validation or pending-model failure does not clear or replace the Redis value. Running Horde API replicas keep their
in-memory snapshot while Redis fails. A process without central Redis fails startup, so it does not fetch its own view
that might diverge from the fleet's. Horde API replicas that lose the cold-start lease keep contending until an abandoned
publication lease can expire and one of them takes over. The effective bootstrap timeout is therefore at least the
lease plus five seconds.

### Degraded bootstrap

If the PRIMARY is unreachable and Redis does not contain a snapshot, startup can exhaust the bootstrap timeout.
The starting Horde API replicas then have one additional lease-length interval in which to publish a **degraded** snapshot.
This uses HMR's GitHub fallback for the canonical models and the
baseline catalog packaged with `horde_model_reference`. It does not include pending models.

The document includes `degraded: true`. Every process that applies it logs an error. A degraded document is always due
for refresh, so the elected publisher keeps retrying (every ten seconds on failure) until the PRIMARY responds and a
healthy document replaces it. A degraded document is not published while any snapshot already exists.

### Snapshot health

Each process reports `horde.model_reference.snapshot_age` (seconds since the served document was published) and
`horde.model_reference.snapshot_degraded` on every Redis poll. It logs a warning every hour once the served snapshot is
older than three hours, which is three missed publisher cycles. Unknown top-level fields in a snapshot are ignored with
a warning, so an older consumer can still load a newer publisher's document during a rolling deploy.

## Environment

| Variable | Default | Effect |
| --- | --- | --- |
| `HORDE_MODEL_REFERENCE_PRIMARY_API_URL` | `https://models.aihorde.net/api` | The PRIMARY read by the elected publisher. Fleet publication requires this service. The GitHub fallback is not allowed to replace the last-known-good snapshot. |
| `AIWORKER_CACHE_HOME` | `models` | HMR's on-disk cache on the elected publisher. API consumers do not read it. A relative value resolves against the process working directory. |
| `HORDE_MODEL_REFERENCE_BOOTSTRAP_TIMEOUT` | `195` | Minimum seconds that a cold-starting API process waits for the bootstrap publisher to populate Redis. The effective value is extended when necessary so that it exceeds the publication lease by five seconds. |
| `HORDE_MODEL_REFERENCE_PUBLISH_LOCK_SECONDS` | `180` | Lifetime of the Redis publication lease. A fetch that outlives its lease is fenced out and cannot overwrite a newer snapshot. |
| `HORDE_BETA_MODEL_CATEGORIES` | `image_generation` | Comma-separated categories to merge pending models into. An empty value disables beta. An unrecognized value is logged and skipped. |
| `HORDE_BETA_MODELS_API_KEY` | `0000000000` | A reader-level AI-Horde key for the pending reads. The PRIMARY accepts the anonymous key. An empty value disables beta. |

`horde_model_reference` reads its own `HORDE_MODEL_REFERENCE_*` settings directly. AI-Horde detects and rejects HMR's
GitHub fallback during fleet publication. Replacing a live PRIMARY revision with the fallback independently would break
the last-known-good contract.

## Database mirror

`store_known_image_models` mirrors the reference into the `known_image_models` table each cycle. Some columns are
optional in the record but non-nullable in the table (`version`, `style`, `tags`). Those columns receive empty values.
`features_not_supported` does not have a counterpart in the record schema, so it is always cleared.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Process view and baseline lookup | `horde/model_reference.py` | `ModelReference`, `ModelReference.baseline_record`, `ModelReference.get_model_baseline` |
| Redis snapshot reads | `horde/model_reference.py` | `ModelReference.refresh_from_redis` |
| Snapshot keys and publication lease | `horde/model_reference_snapshot.py` | `SNAPSHOT_KEY`, `PUBLISH_LOCK_KEY`, `PUBLISH_SEQUENCE_KEY`, `DEFAULT_PUBLISH_LOCK_SECONDS` |
| Fleet publication and refresh scheduling | `horde/model_reference.py`, `horde/database/threads.py` | `ModelReference.publish_fleet_snapshot`, `ModelReference.publish_fleet_snapshot_if_due`, `publish_model_reference_snapshot` |
| Remote sources and merged snapshot | `horde/model_reference.py` | `ModelReference._fetch_fleet_snapshot` |
| Legacy text reference refresh | `horde/model_reference.py` | `ModelReference.refresh_text_reference` |
| Database mirror | `horde/database/threads.py` | `store_known_image_models` |

## Contract tests

`tests/unit/test_model_reference_loader.py` covers pending-model precedence, baseline suffixes and database
mirroring. `tests/unit/test_model_reference_snapshot.py` covers coherent process views, publication fencing,
failover replication and retention of the last valid snapshot. Redis integration coverage is in
`tests/integration/test_model_reference_snapshot_redis.py`.
