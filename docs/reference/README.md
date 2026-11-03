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
| [Prompt provenance reference](prompt_provenance.md) | What each stored prompt value means, who may read the original submission, and why a missing original is never filled in. |
| [Moderation operations reference](moderation_operations.md) | Moderation evidence, retention and object storage, with API fields for event review, promotion queues and worker suspicion history. |
| [Kudos accounting reference](kudos_accounting.md) | The mutation and consumption contract: accounting events, projection targets, reservations, the lock order, and the read models. |
| [Samplers and schedulers reference](samplers_and_schedulers.md) | What sampler_name and scheduler select, why deterministic samplers agree once converged, measured steps-to-converge and cost, and the combinations known to fail. |
| [Image baseline policy reference](baseline_policy.md) | The two authorities a baseline-dependent request is checked against, what each one decides, and what applies to a baseline with no record. |
| [Image model reference loader reference](model_reference.md) | Where the image model reference comes from, how beta (pending) models are merged over it, the environment that configures both, and what the loader exposes. |
| [Style contract reference](style_contract.md) | What an image or text style may declare about the params a request may set and the placeholders its prompt accepts, and how a text request is sized against the prompt it ends up sending. |
| [Chat requests reference](chat_requests.md) | Native chat routes, request fields, worker declarations, job payloads, submissions, pricing and progress streams, with implementation and test references. |
| [Chat wire examples](chat_wire_examples.md) | Checked JSON examples for native chat requests, OpenAI-compatible responses and worker pop, progress and submit routes. |
| [Chat front reference](chat_front.md) | OpenAI-compatible request and response formats, stream behavior, availability errors and telemetry for the horde.oai service. |
| [Outbound requests reference](outbound_requests.md) | Which client-supplied URLs the horde requests, the address check every hop passes, and the timeouts, redirect limits and byte caps of image fetches and webhooks. |
<!-- END GENERATED: documents -->
