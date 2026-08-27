<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Reference

How the running service behaves, down to the classes, caches, and gate orderings
involved. These pages are for reading alongside the code.

The concepts they assume (what a worker is, what a job's lifecycle means) are in
[`haidra-assets/docs`](../haidra-assets/docs/README.md).

## Documents

<!-- BEGIN GENERATED: documents (gen_doc_index.py) -->
| Document | Summary |
| --- | --- |
| [Kudos accounting reference](kudos_accounting.md) | The mutation and consumption contract: accounting events, projection targets, reservations, the lock order, and the read models. |
| [Samplers and schedulers reference](samplers_and_schedulers.md) | What sampler_name and scheduler select, why deterministic samplers agree once converged, measured steps-to-converge and cost, and the combinations known to fail. |
| [Image baseline policy reference](baseline_policy.md) | The two authorities a baseline-dependent request is checked against, what each one decides, and what applies to a baseline with no record. |
| [Image model reference loader reference](model_reference.md) | Where the image model reference comes from, how beta (pending) models are merged over it, the environment that configures both, and what the loader exposes. |
<!-- END GENERATED: documents -->
