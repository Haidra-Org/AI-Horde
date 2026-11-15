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

Adding an image-generation option requires coordinated changes to the backend, SDK and Horde service. The worker
bridge must then use releases containing those changes. Follow the procedure for your option to update validation
and pricing, and to prevent dispatch to workers that cannot support it.

Before starting, set up development checkouts of the affected repositories. Identify the backend implementation and
the first bridge release that support the addition. After integration, the published contract includes the new value,
and dispatch sends it only to compatible workers.

The [samplers and schedulers reference](../reference/samplers_and_schedulers.md) covers the meaning and cost of the
existing values. [ADR 12](../decisions/0012-publish-versioned-sampler-contracts.md) records why the SDK holds the
single registry, instead of each consumer keeping its own table.

## Who owns what

| Repository | Question it covers | Where the vocabulary lives |
| --- | --- | --- |
| [hordelib](https://github.com/Haidra-Org/hordelib) | Can the backend render it? | `hordelib/pipeline/constants.py`: `SAMPLERS_MAP`, `SCHEDULERS`, `SOLVER_TYPES`, `CONTROLNET_IMAGE_PREPROCESSOR_MAP` |
| [horde_sdk](https://github.com/Haidra-Org/horde_sdk) | What is it called, which settings apply, what does a step cost? | `horde_sdk/generation_parameters/image/consts.py`, `constraints.py`, `sampler_work.py` |
| AI-Horde (this repo) | Is the request valid, who may serve it, what does it charge? | `horde/consts.py`, `horde/bridge_reference.py`, `horde/classes/stable/kudos.py` |
| [horde-worker-reGen](https://github.com/Haidra-Org/horde-worker-reGen) | Which workers advertise it? | `horde_worker_regen/consts.py`, `bridgeData_template.yaml` |

Integrate changes in the order shown in the table. Gate dispatch by bridge capability so a request using the new
option reaches only compatible workers. If the service accepts an option before compatible workers are available,
requests using it remain queued. Workers can gain support before the service accepts the option without affecting
existing requests.

A bridge is the worker software that reports its supported features to the Horde. A bridge capability version
is the major version of a reGen release. `bridge_agent` reports
`horde_worker_regen.__version__`, and `check_bridge_capability` compares it as semver against the keys of
`BRIDGE_CAPABILITIES`. A worker has the capabilities of every key at or below its version. Version 17 is the release
that added extended ControlNet, the `scheduler` field, extended samplers, solver options, sigma generators and flow
shift. Version 18 added `control_strength`.

## Add a sampler

1. **hordelib.** Add the horde name to `SAMPLERS_MAP`, mapped to its ComfyUI solver. `SAMPLERS_MAP` is the only place
   that maps the horde spelling to the backend spelling. ComfyUI substitutes an unrecognised name and does not reject
   it. The service therefore gates on bridge version and does not rely on the worker to report the problem.
2. **horde_sdk.** Add the member to `KNOWN_IMAGE_SAMPLERS`. Then add a `SAMPLER_CONSTRAINTS` record (its applicable
   numeric knobs, ranges and solver types) and a `SAMPLER_WORK_PROFILES` record (marginal model evaluations per
   trajectory step, fixed-rate or adaptive). Both lookups raise `KeyError` for a member without a record, so an
   incomplete addition raises an error and does not fall back to a default. Optionally set
   `SAMPLER_PRESENTATION_TIERS`, `RECOMMENDED_SAMPLERS`, a `SamplerRecommendation`, and any
   `REJECTED_SAMPLER_SCHEDULER_PAIRINGS`. Release the SDK.
3. **AI-Horde.** Sampler dispatch is gated by `BRIDGE_SAMPLERS` in `horde/bridge_reference.py`. Its reGen entry at
   `CAPABILITY_EXPANDED_REGEN_VERSION` (17) offers every member of `EXTENDED_SAMPLERS` and `SOLVER_KNOB_SAMPLERS`
   from `horde/consts.py`.
   - If reGen 17 already maps the sampler, add the name to one of those two sets. `BRIDGE_SAMPLERS` then needs no
     edit.
   - If the first reGen release that maps it is newer than 17, add a new set to `horde/consts.py`, include it in
     `KNOWN_SAMPLERS`, and add a `BRIDGE_SAMPLERS` entry for that release's major version that offers the set.
     Adding the name to one of the existing sets would send it to reGen 17 workers, which render a different sampler
     without reporting an error.

   The Swagger enum in `horde/apis/models/stable_v2.py` is derived from `KNOWN_SAMPLERS`, so it does not need a
   separate edit.
4. **AI-Horde kudos.** Add an entry to `CANONICAL_KUDOS_SAMPLERS` in `horde/classes/stable/kudos.py` that maps the new
   name to the closest sampler already in `KudosModel.KNOWN_SAMPLERS`. That list is the one-hot input of the frozen
   model and cannot grow without a retrain. A name that is in neither the map nor the frozen list is priced as
   `k_euler`, which underprices an expensive sampler.
5. **reGen.** A sampler does not need any reGen code change. reGen takes the hordelib and SDK releases from steps 1
   and 2, and the release that ships them is the one gated in step 3.

## Add a scheduler

Schedulers fall into two groups by how the backend produces the sigmas. The group determines the gating.

1. **hordelib.** A schedule that ComfyUI's `calculate_sigmas` accepts by name goes in `SCHEDULERS`. A schedule
   produced by a node goes in `SigmaGeneratorSchedule` instead, and is implemented in
   `hordelib/execution/sigma_schedules.py`, because the graphs take a schedule by name.
2. **horde_sdk.** Add it to `KNOWN_IMAGE_SCHEDULERS`, and to `SCHEDULER_BASELINE_APPLICABILITY` when it works only
   for some model baselines.
3. **AI-Horde.** Add it to `EXTENDED_SCHEDULERS` or `SIGMA_GENERATOR_SCHEDULERS` in `horde/consts.py`. The pop query
   and `Worker.can_generate` send `EXTENDED_SCHEDULERS` only to bridges with the `scheduler` capability, and
   `SIGMA_GENERATOR_SCHEDULERS` only to bridges with `sigma_generators`. A schedule is therefore not sent to a bridge
   that would render a different schedule without reporting it. `LEGACY_SCHEDULERS` stays as it is. Only
   `normal` and `karras` round-trip through the legacy `karras` flag.
4. **AI-Horde gating.** If the new schedule needs a bridge newer than the ones already gated, extend the conditions in
   `horde/database/functions.py` and `horde/classes/stable/worker.py`. Do not add a parallel check elsewhere. Both
   files use the same constant sets.

## Add a solver control

A solver control changes a numeric sampler setting. Adding one requires a field and validation bounds rather than
a new sampler or scheduler name.

1. **hordelib.** Add the option and its fallback bounds to `SOLVER_OPTION_FALLBACK_BOUNDS`, or to the dedicated bounds
   constant where one exists (`FLOW_SHIFT_BOUNDS`).
2. **horde_sdk.** Add the knob to `SAMPLER_SOLVER_KNOB`, give it a `NumericKnobRange`, and list it on every
   `SAMPLER_CONSTRAINTS` record that it applies to. Applicability is per sampler and empty by default, so samplers
   that ignore the knob do not need an edit.
3. **AI-Horde.** Add the request field to `horde/apis/models/stable_v2.py` and the parameter name to
   `SOLVER_KNOB_PARAMS` in `horde/consts.py`. Validation in `Validator.validate_sampler_constraints` uses the SDK
   registry, so range and applicability checks do not need per-knob code. The pop query and `Worker.can_generate`
   iterate over `SOLVER_KNOB_PARAMS`, so gating follows from the constant.
4. **Return codes.** A new rejection reason needs an entry in `horde/exceptions.py` and `README_return_codes.md`. Reuse
   `SamplerKnobInapplicable` or `SamplerKnobOutOfRange` where the failure is of the same kind. A new code is worth
   adding only if a client would act on it differently.

## Add an annotation control type

The [annotation pricing constants](../reference/service_limits.md#accounting-units-floors-and-surcharges) identify
the per-tile cost classes and their fallback; adding a detector also requires choosing its cost class.

An annotation control type appears in two places: the `control_type` of an image-generation request, and the payload
of the Alchemy `annotation` form.

1. **hordelib.** Map the horde name to its preprocessor in `CONTROLNET_IMAGE_PREPROCESSOR_MAP`. Add a
   `CONTROLNET_MODEL_MAP` entry if image generation can condition on it, a `CONTROLNET_ANNOTATOR_DOWNLOAD_BYTES`
   estimate for the worker's disk preview, and the detector to the prefetch sets in `hordelib/preload.py`.
2. **horde_sdk.** Add it to `KNOWN_IMAGE_CONTROLNETS` and `KNOWN_ANNOTATION_CONTROL_TYPES`.
   `AI_HORDE_EXTENDED_IMAGE_CONTROL_TYPES` is derived by subtracting the legacy set, so a new type is automatically in
   the extended set and not in the classic set.
3. **AI-Horde.** Add it to `KNOWN_CONTROL_TYPES` in `horde/consts.py`. `IMAGE_CONTROL_TYPES` and the Swagger enums
   are derived from it. Do not add it to `LEGACY_IMAGE_CONTROL_TYPES`, which is the set that old bridges can render.
   A new type there would be dispatched to workers that cannot produce it.
4. **AI-Horde pricing.** Add it to `ANNOTATION_DETECTOR_KUDOS_BUCKETS` in `horde/consts.py`, in the cost class that
   matches what the detector loads: weightless for pure OpenCV or numpy, weighted for a small checkpoint, hub for a
   large transformers-hub model. An unlisted type falls back to the weighted class, so a heavy detector left unlisted is
   underpriced. If image generation can condition on it, also map it to its closest classic cost class in
   `CANONICAL_KUDOS_CONTROL_TYPES`. The reason is the same as for samplers: the one-hot input of the frozen model
   cannot grow. An unmapped type is priced as a request without ControlNet.
5. **reGen.** `CLASSIC_CONTROL_TYPES` and `EXTENDED_CONTROL_TYPES` in `horde_worker_regen/consts.py` are derived from
   the SDK, so taking the SDK release is enough. `allow_extended_controlnet` is a single boolean for the whole
   extended set. A worker advertises it only once its annotators cover every extended type, so adding a type raises
   the set that every opted-in image worker is expected to serve. Alchemists advertise per type through
   `annotation_types`, so annotation work can be matched type by type. Image generation cannot be matched that way.

## Update a published contract or schema

The published schema, execution guarantees and bridge capabilities have separate versions. Choose the version
change that matches the compatibility requirement. Changing the document schema version unnecessarily forces client
updates, while leaving it unchanged after an incompatible change allows clients to misinterpret the document.

| Version | Where | Bump it when |
| --- | --- | --- |
| `SAMPLER_CONSTRAINTS_DOCUMENT_SCHEMA_VERSION` | `horde_sdk/generation_parameters/image/constraints_document.py` | The JSON shape of the published document changes in a way that a client parsing the old shape cannot handle |
| `SamplerExecutionContractVersion` | `horde_sdk/generation_parameters/image/sampler_work.py` | A worker's guaranteed behavior for adaptive execution changes |
| A new `CAPABILITY_*_REGEN_VERSION` constant and its `BRIDGE_CAPABILITIES` entry | `horde/bridge_reference.py` | A request field needs a bridge release that older workers do not have. Add a constant for that release, as `CAPABILITY_CONTROL_STRENGTH_REGEN_VERSION` (18) does for `control_strength`. Raising an existing constant would withdraw its capabilities from bridges that already render them |
| `HORDE_VERSION` and a new `sql_statements/` file | `horde/consts.py` | The change adds or alters a database column |

Rules that follow from keeping these versions separate:

- Adding a sampler, scheduler or control type to an existing document section is additive and does not bump the
  document schema version. Clients reject schema versions they do not understand, so a bump costs every client an
  update.
- A new execution contract version publishes its complete guarantee set in `execution_contracts`. Workers advertise
  one version string, and the server maps it to that set. A missing, malformed or future version does not establish a
  ceiling, and the check fails closed. The server does not infer a version from any other field.
- New response fields go in `horde/apis/models/stable_v2.py` with an explicit default type.
  `tests/integration/test_swagger_contract.py` checks the Swagger 2 translation over the complete served document,
  including operation-id uniqueness.
- A field that a pre-17 bridge must not see is left out of its pop payload. It is not sent with a default. This keeps
  the legacy payload shape for strict clients. `tests/integration/test_image_scheduler_gating.py` covers this.
- Persisting a new worker capability requires a migration. Follow the existing pattern: idempotent, rerunnable, with a
  `.license` file beside it, and `CREATE INDEX CONCURRENTLY` kept out of any transaction wrapper.

## Related updates

- **Version pins.** A new SDK symbol means bumping the `horde_sdk` bound in `pyproject.toml` and `requirements.txt`,
  and regenerating `uv.lock`. reGen pins the SDK separately, so a worker-visible addition needs both bumps before the
  capability gate can open.
- **Publishing the change.** The sampler constraints endpoint compiles its document once per process and keeps it in
  memory. A response cache is not in front of it, so a deployed change is served as soon as the process restarts.
  No cache needs invalidating, and the deploy should not include a cache flush.
- **Tests.** `tests/unit/test_consts.py` pins the vocabularies. `tests/unit/test_sampler_constraints.py` compares the
  published document with the registry. `tests/unit/test_kudos_pricing.py` pins prices for existing requests.
  `tests/unit/test_bridge_reference.py` pins capability gating. The Locust sampler requester in
  `tests/stress/locustsuite/users/image.py` derives its cases from the SDK, so a new sampler enters load coverage
  without an edit.
- **Reference documentation.** `docs/reference/samplers_and_schedulers.md` records the measured cost groupings. Where
  a sampler's marginal cost was not measured, record that instead of reusing the number of a neighbouring sampler.
- **Frontends.** `extended_image_frontend.md` describes how a frontend builds controls from the published contract.
  An addition that fits the published shape does not need a frontend change. An addition that does not fit is a sign
  that the contract shape needs revisiting.

## Verification and reversal

Run the vocabulary, published-contract, pricing and bridge-gating tests listed above. Verify that the new value
appears in the served contract, is refused for or withheld from unsupported bridges, and reaches a compatible worker
with the expected payload. Verify solver controls at their bounds and with an inapplicable sampler.

To withdraw dispatch support, remove the new service or bridge-gating entry and check that existing values still pass
their tests. Keep published identifiers and execution-contract meanings stable. A released contract with changed
semantics requires the version change described above. Jobs already assigned keep their original payloads.
