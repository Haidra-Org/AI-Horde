---
title: "Image baseline policy reference"
summary: "The two authorities a baseline-dependent request is checked against, what each one decides, and what applies to a baseline with no record."
topics: [generation, kudos, workers]
order: 90
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Image baseline policy reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [kudos](../topics.md#kudos), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

Two separate authorities decide what a request may ask of the baselines it names, and neither is a
table in this repository:

- **The served baseline catalog.** Published on models.aihorde.net and read through
  `model_reference.baseline_record(baseline)`. It states what exists for a model family: which
  ControlNet, layer-diffusion and QR-code weights have been published, whether the architecture has a
  remix mechanism, whether a flow-matching timestep shift is meaningful for it, and the kudos,
  batching, lease and resolution-floor policy the horde applies to it.
- **The bridge feature table.** `BRIDGE_BASELINE_FEATURES` in `horde/bridge_reference.py`, keyed by
  bridge version. It states which bridge releases actually render a feature on a baseline.

`horde/baseline_policy.py` reads both and nothing else. To add a baseline, see
[add an image baseline](../how-to/add_image_baseline.md).

## What each authority decides

| Feature | Catalog capability | Bridge feature | Rejection code |
| --- | --- | --- | --- |
| `flow_shift` | `flow_matching` | `flow_shift` | `FlowShiftInapplicable` |
| `hires_fix` | - | `hires_fix` | `HiResMismatch` |
| `transparent` | `transparent` | `transparent` | `InvalidTransparencyModel` |
| `workflow: qr_code` | `qr_code` | - | `ControlNetMismatch.` |
| `control_type` with no weights | `controlnet_types_unavailable` | - | `ControlNetUnsupported` |
| `control_type` | `controlnet` | `control_type` | `ControlNetMismatch` |
| `source_processing: remix` | `remix` | - | `InvalidRemix` |

A feature that both columns cover needs both to allow it. The features are evaluated in the order
above, so a request tripping two of them is refused for the first.

A request naming several models can be dispatched for any of them, so a feature is accepted only where
every requested baseline renders it.

## Which bridge releases render what

`bridge_supports(feature, baseline, bridge_agent=None)` is the only reader. It is cumulative by
version in the same way `get_bridge_capabilities` is: a worker gets the union of every version at or
below its own.

| Bridge | Version | Feature | Baselines |
| --- | --- | --- | --- |
| AI Horde Worker reGen | 1 | `hires_fix` | SD1, SD2 768, SD2 512, SDXL |
| AI Horde Worker reGen | 1 | `control_type` | SD1, SD2 768, SD2 512 |
| AI Horde Worker reGen | 6 | `hires_fix` | Stable Cascade |
| AI Horde Worker reGen | 8 | `transparent` | SD1, SDXL |
| AI Horde Worker reGen | 17 | `flow_shift` | Flux.1, Flux Schnell, Flux Dev, Qwen-Image |
| AI Horde Worker | 13 | `hires_fix` | SD1, SD2 768, SD2 512 |
| AI Horde Worker | 15 | `control_type` | SD1, SD2 768, SD2 512 |

Accepting a request asks whether *any* known bridge kind at any version renders the combination, so a
request is refused with 400 only where nothing in the fleet could ever run it. Dispatch asks the same
question of the worker's own agent, so an older worker holding the model is passed over rather than
sent a job it would render wrongly. The `flux` graph capability stays a flat capability checked at
dispatch alone.

## The horde policy on a baseline

Read with `policy(baseline)`, which returns the record's `horde_policy` or the par default.

| Field | Meaning |
| --- | --- |
| `kudos` | What one generation costs relative to a par baseline. |
| `kudos_qr_code`, `kudos_hires` | The factor where that workflow changes the shape of the render rather than only its settings. |
| `batching` | How much this baseline's cost inflates in the batch size calculation, which holds the batch count down for an architecture that wants most of a card. |
| `ttl` | How much longer than its sampler work implies a lease runs. |
| `resolution_floor` | The resolution every user reaches regardless of queue pressure. Zero claims no floor. |

## An uncatalogued baseline

A baseline the catalog publishes no record for, including one the installed `horde_model_reference`
vocabulary does not carry, is given conservative capabilities and priced at par. A plain txt2img
request remains accepted, while baseline-dependent workflows are refused until the catalog states
that the required weights or mechanism exist and, where applicable, a bridge release states that it
renders them.
