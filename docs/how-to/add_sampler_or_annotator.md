---
title: "Add a sampler, scheduler, solver control, or annotator"
summary: "Extend the image sampler and control-map vocabularies across hordelib, horde_sdk, AI-Horde, and the reGen bridge, in the order that keeps them compatible."
topics: [generation]
order: 30
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Add a sampler, scheduler, solver control, or annotator

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation)
<!-- END GENERATED: topics -->

Each of these vocabularies is a fixed set defined in four repositories. This guide covers which
repository owns which part, the order the changes have to land in, and what else needs updating
alongside the value itself.

The [samplers and schedulers reference](../reference/samplers_and_schedulers.md) covers what the
existing values mean and cost. [ADR 12](../decisions/0012-publish-versioned-sampler-contracts.md)
records why the SDK holds the single registry rather than each consumer keeping its own table.

## Who owns what

| Repository | Question it answers | Where the vocabulary lives |
| --- | --- | --- |
| [hordelib](https://github.com/Haidra-Org/hordelib) | Can the backend render it? | `hordelib/pipeline/constants.py`: `SAMPLERS_MAP`, `SCHEDULERS`, `SOLVER_TYPES`, `CONTROLNET_IMAGE_PREPROCESSOR_MAP` |
| [horde_sdk](https://github.com/Haidra-Org/horde_sdk) | What is it called, which settings apply, what does a step cost? | `horde_sdk/generation_parameters/image/consts.py`, `constraints.py`, `sampler_work.py` |
| AI-Horde (this repo) | Is the request valid, who may serve it, what does it charge? | `horde/consts.py`, `horde/bridge_reference.py`, `horde/classes/stable/kudos.py` |
| [horde-worker-reGen](https://github.com/Haidra-Org/horde-worker-reGen) | Which workers advertise it? | `horde_worker_regen/consts.py`, `bridgeData_template.yaml` |

Land the changes in that order. The service can accept a value before any worker renders it, which
queues jobs nothing can pop, so a new value reaches dispatch only behind a bridge capability gate. The
reverse is harmless: a worker able to render a value the service does not accept never receives one.

A bridge capability version is the reGen release major. `horde_worker_regen.__version__` is reported in
`bridge_agent`, and `check_bridge_capability` compares it as semver against the keys of
`BRIDGE_CAPABILITIES`. Version 17 is the release that added extended ControlNet, the `scheduler` field,
extended samplers, solver options, sigma generators, and flow shift.

## Add a sampler

1. **hordelib.** Add the horde name to `SAMPLERS_MAP` against its ComfyUI solver. `SAMPLERS_MAP` is the
   only place the horde spelling and the backend spelling meet. ComfyUI substitutes an unrecognised
   name rather than rejecting it, so the service gates on bridge version instead of relying on the
   worker to report the problem.
2. **horde_sdk.** Add the member to `KNOWN_IMAGE_SAMPLERS`, then a `SAMPLER_CONSTRAINTS` record (its
   applicable numeric knobs, ranges, and solver types) and a `SAMPLER_WORK_PROFILES` record (marginal
   model evaluations per trajectory step, fixed-rate or adaptive). Both lookups raise `KeyError` on a
   member with no record, so an incomplete addition raises instead of falling back to a default. Optionally
   set `SAMPLER_PRESENTATION_TIERS`, `RECOMMENDED_SAMPLERS`, a `SamplerRecommendation`, and any
   `REJECTED_SAMPLER_SCHEDULER_PAIRINGS`. Release the SDK.
3. **AI-Horde.** Add the name to `EXTENDED_SAMPLERS` or `SOLVER_KNOB_SAMPLERS` in `horde/consts.py`,
   whichever matches the oldest bridge release that maps it. `KNOWN_SAMPLERS` is derived from those
   sets, and the Swagger enum in `horde/apis/models/stable_v2.py` is derived from `KNOWN_SAMPLERS`, so
   neither needs a separate edit. Add the name to `BRIDGE_SAMPLERS` in `horde/bridge_reference.py`
   under the capability version that renders it.
4. **AI-Horde kudos.** Add an entry to `CANONICAL_KUDOS_SAMPLERS` in `horde/classes/stable/kudos.py`
   mapping the new name to the closest sampler already in `KudosModel.KNOWN_SAMPLERS`. That list is the
   frozen model's one-hot input and cannot grow without a retrain. A name in neither the map nor the
   frozen list is priced as `k_euler`, which underprices an expensive sampler.
5. **reGen.** Nothing per sampler beyond taking the SDK release, unless the sampler needs a bridge
   release that predates it. In that case add the new major to `BRIDGE_CAPABILITIES` and
   `BRIDGE_SAMPLERS` in this repository and gate dispatch on it.

## Add a scheduler

Schedulers split by how the backend produces the sigmas, and the split decides the gating.

1. **hordelib.** A schedule ComfyUI's `calculate_sigmas` accepts by name goes in `SCHEDULERS`. A
   schedule produced by a node goes in `SigmaGeneratorSchedule` and is carried by
   `hordelib/execution/sigma_schedules.py` instead, because the graphs take a schedule by name.
2. **horde_sdk.** Add it to `KNOWN_IMAGE_SCHEDULERS`, and to `SCHEDULER_BASELINE_APPLICABILITY` when it
   only works for some model baselines.
3. **AI-Horde.** Add it to `EXTENDED_SCHEDULERS` or `SIGMA_GENERATOR_SCHEDULERS` in `horde/consts.py`.
   The pop query and `Worker.can_generate` gate on `SIGMA_GENERATOR_SCHEDULERS`, so a sigma-generator
   schedule never reaches a bridge that would render a different schedule without reporting it.
   `LEGACY_SCHEDULERS` stays as it is: only `normal` and `karras` round-trip through the legacy
   `karras` flag.
4. **AI-Horde gating.** If the new schedule needs a bridge newer than the ones already gated, extend the
   conditions in `horde/database/functions.py` and `horde/classes/stable/worker.py` rather than adding a
   parallel check elsewhere. Both read the same constant sets.

## Add a solver control

A solver control is a numeric setting on the sampler rather than a name in a vocabulary.

1. **hordelib.** Add the option and its fallback bounds to `SOLVER_OPTION_FALLBACK_BOUNDS`, or the
   dedicated bounds constant where one exists (`FLOW_SHIFT_BOUNDS`).
2. **horde_sdk.** Add the knob to `SAMPLER_SOLVER_KNOB`, give it a `NumericKnobRange`, and list it on
   every `SAMPLER_CONSTRAINTS` record it applies to. Applicability is per sampler, so the empty case is
   the default and no edit is needed for samplers that ignore it.
3. **AI-Horde.** Add the request field to `horde/apis/models/stable_v2.py` and the parameter name to
   `SOLVER_KNOB_PARAMS` in `horde/consts.py`. Validation in `Validator.validate_sampler_constraints`
   reads the SDK registry, so range and applicability checks need no per-knob code. The pop query and
   `Worker.can_generate` iterate `SOLVER_KNOB_PARAMS`, so gating follows from the constant.
4. **Return codes.** A new rejection reason needs an entry in `horde/exceptions.py` and
   `README_return_codes.md`. Reuse `SamplerKnobInapplicable` or `SamplerKnobOutOfRange` where the
   failure is the same kind. A new code is worth adding only if a client would act on it differently.

## Add an annotation control type

An annotation control type appears on two surfaces: the `control_type` of an image-generation request,
and the payload of the Alchemy `annotation` form.

1. **hordelib.** Map the horde name to its preprocessor in `CONTROLNET_IMAGE_PREPROCESSOR_MAP`. Add a
   `CONTROLNET_MODEL_MAP` entry if image generation can condition on it, a
   `CONTROLNET_ANNOTATOR_DOWNLOAD_BYTES` estimate for the worker's disk preview, and the detector to the
   prefetch sets in `hordelib/preload.py`.
2. **horde_sdk.** Add it to `KNOWN_IMAGE_CONTROLNETS` and `KNOWN_ANNOTATION_CONTROL_TYPES`.
   `AI_HORDE_EXTENDED_IMAGE_CONTROL_TYPES` is derived by subtracting the legacy set, so an addition is
   automatically extended rather than classic.
3. **AI-Horde.** Add it to `KNOWN_CONTROL_TYPES` in `horde/consts.py`. `IMAGE_CONTROL_TYPES` and the
   Swagger enums follow. Do not add it to `LEGACY_IMAGE_CONTROL_TYPES`, which is the set old bridges
   can render; a new type there would be dispatched to workers that cannot produce it.
4. **AI-Horde pricing.** Add it to `ANNOTATION_DETECTOR_KUDOS_BUCKETS` in the cost class that matches
   what the detector loads: weightless for pure OpenCV or numpy, weighted for a small checkpoint, hub for
   a large transformers-hub model. An unlisted type falls back to the weighted class, so a heavy detector
   left unlisted is underpriced. If image generation can condition on it, also map it onto its closest
   classic cost class in `CANONICAL_KUDOS_CONTROL_TYPES`, for the same frozen one-hot reason as samplers.
   An unmapped type prices as no ControlNet at all.
5. **reGen.** `CLASSIC_CONTROL_TYPES` and `EXTENDED_CONTROL_TYPES` in `horde_worker_regen/consts.py` are
   derived from the SDK, so taking the SDK release is enough. Be aware that
   `allow_extended_controlnet` is a single boolean covering the whole extended set: a worker advertises
   it only once its annotators cover every extended type, so adding a type raises what every opted-in
   image worker is expected to serve. Alchemists advertise per type through `annotation_types`, so
   annotation work can be matched type by type where image generation cannot.

## Update a published contract or schema

Four versions govern this surface. They move independently: bumping the wrong one either forces an
unnecessary client update or lets an incompatible change through.

| Version | Where | Bump it when |
| --- | --- | --- |
| `SAMPLER_CONSTRAINTS_DOCUMENT_SCHEMA_VERSION` | `horde_sdk/generation_parameters/image/constraints_document.py` | The JSON shape of the published document changes in a way a client parsing the old shape cannot handle |
| `SamplerExecutionContractVersion` | `horde_sdk/generation_parameters/image/sampler_work.py` | The behavior a worker guarantees about adaptive execution changes |
| `CAPABILITY_EXPANDED_REGEN_VERSION` and `BRIDGE_CAPABILITIES` | `horde/bridge_reference.py` | A request field needs a bridge release that older workers do not have |
| `HORDE_VERSION` and a new `sql_statements/` file | `horde/consts.py` | The change adds or alters a database column |

Rules that follow from those being separate:

- Adding a sampler, scheduler, or control type to an existing document section is additive and does not
  bump the document schema version. Clients reject schema versions they do not understand, so a bump
  costs every client an update.
- A new execution contract version publishes its complete guarantee set in `execution_contracts`.
  Workers advertise one version string and the server maps it to that set. A missing, malformed, or
  future version proves no ceiling and fails closed. The server does not infer a version from any other
  field.
- New response fields go in `horde/apis/models/stable_v2.py` with an explicit default type. Swagger 2
  translation is checked over the complete served document, including operation-id uniqueness, by
  `tests/integration/test_swagger_contract.py`.
- A field a pre-17 bridge must not see is omitted from its pop payload rather than sent with a default,
  which preserves the legacy payload shape for strict clients.
  `tests/integration/test_image_scheduler_gating.py` covers this.
- Persisting a new worker capability requires a migration. Follow the existing pattern: idempotent,
  rerunnable, with a `.license` file beside it, and `CREATE INDEX CONCURRENTLY` kept out of any
  transaction wrapper.

## Chores that go with the change

- **Version pins.** A new SDK symbol means bumping the `horde_sdk` bound in `pyproject.toml` and
  `requirements.txt`, and regenerating `uv.lock`. reGen pins the SDK separately, so a worker-visible
  addition needs both bumps before the capability gate can open.
- **Tests.** `tests/unit/test_consts.py` pins the vocabularies, `tests/unit/test_sampler_constraints.py`
  compares the published document with the registry, `tests/unit/test_kudos_pricing.py` pins prices for
  pre-existing requests, and `tests/unit/test_bridge_reference.py` pins capability gating. The Locust
  sampler requester in `tests/stress/locustsuite/users/image.py` derives its cases from the SDK, so a new
  sampler enters load coverage without an edit.
- **Reference documentation.** `docs/reference/samplers_and_schedulers.md` carries the measured cost
  groupings. Where a sampler's marginal cost was not measured, record that rather than reusing a
  neighbouring sampler's number.
- **Frontends.** `extended_image_frontend.md` describes how a frontend builds controls from the
  published contract. An addition that fits the published shape needs no frontend change. One that does
  not fit is a sign the contract shape needs revisiting.
